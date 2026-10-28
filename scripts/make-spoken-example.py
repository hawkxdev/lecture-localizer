"""Create reviewed spoken example inputs from verified public recordings."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
import ssl
import subprocess
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, cast
from urllib.error import URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    OpenerDirector,
    ProxyHandler,
    Request,
    build_opener,
)

from lecture_localizer.contracts import load_strict_json_file
from lecture_localizer.media import scrubbed_environment
from lecture_localizer.storage import create_new_file

CONFIG_FILE = Path(__file__).with_name('spoken-example-v1.json')
NETWORK_TIMEOUT_SECONDS = 30
TOOL_TIMEOUT_SECONDS = 120
MAX_METADATA_BYTES = 1 << 20


class ExampleError(ValueError):
    """An example input or public source is unsuitable."""


def _https_host(url: str) -> str:
    """Require HTTPS without embedded credentials or fragments."""
    parsed = urlsplit(url)
    if (
        parsed.scheme != 'https'
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or parsed.port not in (None, 443)
    ):
        raise ExampleError('public source must be an ordinary HTTPS URL')
    return parsed.hostname


class SameHostRedirects(HTTPRedirectHandler):
    """Keep public retrieval inside the configured source origin."""

    def __init__(self, host: str) -> None:
        """Retain the one allowed public host."""
        self.host = host

    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> Any:
        """Reject an unexpected redirect before sending another request."""
        if _https_host(newurl) != self.host:
            raise ExampleError('public source redirect changed its origin')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def load_description() -> dict[str, Any]:
    """Validate the shipped example description before any output."""
    data = load_strict_json_file(CONFIG_FILE)
    if (
        not isinstance(data, dict)
        or set(data)
        != {
            'schema_version',
            'source',
            'video',
            'pricing',
            'segments',
            'notice_file',
        }
        or type(data['schema_version']) is not int
        or data['schema_version'] != 1
    ):
        raise ExampleError('unknown or malformed spoken example description')
    source = data['source']
    if set(source) != {
        'dataset',
        'reference_revision',
        'config',
        'split',
        'rows_url',
        'corpus_url',
        'license',
        'license_url',
        'sample_rate_hz',
        'channels',
    }:
        raise ExampleError('unknown source description fields')
    _https_host(source['rows_url'])
    if (
        urlsplit(source['rows_url']).path != '/rows'
        or urlsplit(source['rows_url']).query
    ):
        raise ExampleError('source must use the documented rows endpoint')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', source['dataset']):
        raise ExampleError('source dataset identity is malformed')
    for name in ('config', 'split'):
        if not re.fullmatch(r'[A-Za-z0-9_-]+', source[name]):
            raise ExampleError('source subset identity is malformed')
    if not re.fullmatch(r'[0-9a-f]{40}', source['reference_revision']):
        raise ExampleError('source reference revision is malformed')
    video = data['video']
    if set(video) != {'duration_seconds', 'width', 'height', 'frame_rate'}:
        raise ExampleError('unknown video description fields')
    if any(type(v) is not int or v <= 0 for v in video.values()):
        raise ExampleError('video parameters must be positive integers')
    segments = data['segments']
    if not isinstance(segments, list) or not 1 <= len(segments) <= 100:
        raise ExampleError(
            'example must have between one and one hundred cues'
        )
    ids = set()
    rows = set()
    previous_end = 0
    for segment in segments:
        if set(segment) != {
            'id',
            'source_id',
            'row_index',
            'bytes',
            'sha256',
            'source_text',
            'translation',
            'start_ms',
            'end_ms',
        }:
            raise ExampleError('unknown segment description fields')
        if (
            not isinstance(segment['id'], str)
            or not segment['id'].strip()
            or segment['id'] in ids
        ):
            raise ExampleError('segment identity must be unique and nonempty')
        if not re.fullmatch(r'[0-9]+-[0-9]+-[0-9]+', segment['source_id']):
            raise ExampleError('source identity is not a safe recording name')
        if not re.fullmatch(r'[0-9a-f]{64}', segment['sha256']):
            raise ExampleError('source hash is malformed')
        row = segment['row_index']
        if type(row) is not int or not 0 <= row < 100 or row in rows:
            raise ExampleError(
                'source row positions must be unique and bounded'
            )
        if (
            type(segment['bytes']) is not int
            or not 0 < segment['bytes'] <= MAX_METADATA_BYTES
        ):
            raise ExampleError('recording byte limit is invalid')
        start, end = segment['start_ms'], segment['end_ms']
        if (
            type(start) is not int
            or type(end) is not int
            or start < previous_end
            or end <= start
            or end > video['duration_seconds'] * 1000
        ):
            raise ExampleError(
                'source cue windows must be ordered inside the video'
            )
        if any(
            not isinstance(segment[k], str) or not segment[k].strip()
            for k in ('source_text', 'translation')
        ):
            raise ExampleError('both complete texts are required')
        ids.add(segment['id'])
        rows.add(row)
        previous_end = end
    if Path(data['notice_file']).name != data['notice_file']:
        raise ExampleError('notice must be adjacent to the generator')
    return cast(dict[str, Any], data)


def public_opener(host: str) -> OpenerDirector:
    """Use verified TLS without account tokens or proxy discovery."""
    return build_opener(
        ProxyHandler({}),
        SameHostRedirects(host),
        HTTPSHandler(context=ssl.create_default_context()),
    )


def read_public(opener: OpenerDirector, url: str, limit: int) -> bytes:
    """Read one bounded public response without automatic retries."""
    request = Request(
        url, headers={'User-Agent': 'lecture-localizer-example/1'}
    )
    try:
        with opener.open(request, timeout=NETWORK_TIMEOUT_SECONDS) as response:
            if response.status != 200:
                raise ExampleError('public source did not return HTTP 200')
            body = cast(bytes, response.read(limit + 1))
    except URLError as error:
        raise ExampleError(
            'public source retrieval failed without retry'
        ) from error
    if len(body) > limit:
        raise ExampleError('public source exceeded its byte limit')
    return body


def _validate_audio_bytes(body: bytes, segment: dict[str, Any]) -> None:
    """Bind a complete FLAC to its exact reviewed source."""
    if (
        len(body) != segment['bytes']
        or not body.startswith(b'fLaC')
        or hashlib.sha256(body).hexdigest() != segment['sha256']
    ):
        raise ExampleError(
            f'source byte identity mismatch: {segment["source_id"]}'
        )


def obtain_audio(
    destination: Path, description: dict[str, Any], audio_dir: Path | None
) -> list[dict[str, str]]:
    """Obtain only the exact reviewed recordings and retain their origin."""
    source = description['source']
    served: dict[str, tuple[str, str]] = {}
    opener = None
    if audio_dir is None:
        host = _https_host(source['rows_url'])
        opener = public_opener(host)
        query = urlencode(
            {
                'dataset': source['dataset'],
                'config': source['config'],
                'split': source['split'],
                'offset': 0,
                'length': max(s['row_index'] for s in description['segments'])
                + 1,
            }
        )
        metadata = json.loads(
            read_public(
                opener, source['rows_url'] + '?' + query, MAX_METADATA_BYTES
            )
        )
        for segment in description['segments']:
            matches = [
                e['row']
                for e in metadata['rows']
                if e['row_idx'] == segment['row_index']
            ]
            if len(matches) != 1:
                raise ExampleError('public metadata lacks the required row')
            row = matches[0]
            if (
                row['id'] != segment['source_id']
                or row['text'] != segment['source_text']
            ):
                raise ExampleError('public source ID or reviewed text changed')
            if not isinstance(row['audio'], list) or len(row['audio']) != 1:
                raise ExampleError(
                    'public source has an unexpected audio shape'
                )
            url = row['audio'][0]['src']
            if _https_host(url) != host:
                raise ExampleError('public audio URL changed its origin')
            pattern = (
                f'/cached-assets/{re.escape(source["dataset"])}/--/'
                r'([0-9a-f]{40})/--/'
                f'{re.escape(source["config"])}/{re.escape(source["split"])}/'
                f'{segment["row_index"]}/audio/audio.flac'
            )
            match = re.fullmatch(pattern, urlsplit(url).path)
            if match is None:
                raise ExampleError(
                    'public audio path changed its source identity'
                )
            served[segment['source_id']] = (url, match.group(1))
    receipts = []
    for segment in description['segments']:
        filename = f'{segment["source_id"]}.flac'
        if audio_dir is None:
            assert opener is not None
            url, revision = served[segment['source_id']]
            body = read_public(opener, url, segment['bytes'])
        else:
            with (audio_dir / filename).open('rb') as handle:
                body = handle.read(segment['bytes'] + 1)
            revision = 'explicit-directory-verified-by-sha256'
        _validate_audio_bytes(body, segment)
        create_new_file(destination / filename, body)
        receipts.append(
            {
                'id': segment['source_id'],
                'filename': filename,
                'sha256': segment['sha256'],
                'served_revision': revision,
            }
        )
    return receipts


def tool_run(argv: list[str], directory: Path) -> str:
    """Run one bounded native tool with credential values scrubbed."""
    result = subprocess.run(
        argv,
        cwd=directory,
        capture_output=True,
        text=True,
        timeout=TOOL_TIMEOUT_SECONDS,
        env=scrubbed_environment(),
    )
    if result.returncode:
        raise ExampleError(
            f'media tool failed ({result.returncode}): {result.stderr}'
        )
    return result.stdout


def timestamp(milliseconds: int) -> str:
    """Render an exact SRT cue boundary."""
    seconds, millis = divmod(milliseconds, 1000)
    minutes, second = divmod(seconds, 60)
    hour, minute = divmod(minutes, 60)
    return f'{hour:02}:{minute:02}:{second:02},{millis:03}'


def write_json(path: Path, value: Any) -> None:
    """Write one fresh public example output."""
    create_new_file(
        path, (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode()
    )


def create_example(
    destination: Path, audio_dir: Path | None = None
) -> dict[str, Any]:
    """Generate a fresh spoken input without calling a speech provider."""
    description = load_description()
    destination = destination.expanduser()
    if destination.exists() or destination.is_symlink():
        raise FileExistsError('refusing to write into an existing destination')
    destination = destination.resolve()
    checkout = Path(__file__).resolve().parents[1]
    if destination.is_relative_to(checkout):
        raise ExampleError(
            'example destination must stay outside the source checkout'
        )
    ffmpeg, ffprobe = shutil.which('ffmpeg'), shutil.which('ffprobe')
    if ffmpeg is None or ffprobe is None:
        raise ExampleError(
            'install ffmpeg and ffprobe before creating the example'
        )
    destination.mkdir(parents=True, exist_ok=False)
    marker = destination / '.example-incomplete'
    create_new_file(
        marker,
        b'Example creation has not completed. '
        b'Preserve files and use a fresh destination.\n',
    )
    audio = obtain_audio(destination, description, audio_dir)
    segments = description['segments']
    for segment in segments:
        probe = json.loads(
            tool_run(
                [
                    ffprobe,
                    '-v',
                    'error',
                    '-show_entries',
                    'format=duration:stream=codec_name,sample_rate,channels',
                    '-of',
                    'json',
                    f'{segment["source_id"]}.flac',
                ],
                destination,
            )
        )
        duration = float(probe['format']['duration'])
        stream = probe['streams'][0]
        if (
            len(probe['streams']) != 1
            or stream['codec_name'] != 'flac'
            or int(stream['sample_rate'])
            != description['source']['sample_rate_hz']
            or stream['channels'] != description['source']['channels']
            or not math.isfinite(duration)
            or duration <= 0
            or duration * 1000 > segment['end_ms'] - segment['start_ms']
        ):
            raise ExampleError(
                'recording profile or full duration does not match its window'
            )
    video = description['video']
    argv = [
        ffmpeg,
        '-nostdin',
        '-v',
        'error',
        '-n',
        '-f',
        'lavfi',
        '-i',
        f'testsrc2=size={video["width"]}x{video["height"]}:rate={video["frame_rate"]}:duration={video["duration_seconds"]}',
    ]
    for segment in segments:
        argv += ['-i', f'{segment["source_id"]}.flac']
    filters = [
        f'[{i + 1}:a]adelay={s["start_ms"]}:all=1[a{i + 1}]'
        for i, s in enumerate(segments)
    ]
    filters.append(
        ''.join(f'[a{i + 1}]' for i in range(len(segments)))
        + f'amix=inputs={len(segments)}:duration=longest:normalize=0,'
        + f'apad=whole_dur={video["duration_seconds"]}[out]'
    )
    argv += [
        '-filter_complex',
        ';'.join(filters),
        '-map',
        '0:v',
        '-map',
        '[out]',
        '-t',
        str(video['duration_seconds']),
        '-c:v',
        'libx264',
        '-preset',
        'ultrafast',
        '-pix_fmt',
        'yuv420p',
        '-c:a',
        'aac',
        '-ar',
        '48000',
        '-ac',
        '1',
        '-b:a',
        '192k',
        '-metadata:s:a:0',
        'language=eng',
        'spoken-source.mp4',
    ]
    tool_run(argv, destination)
    srt = (
        '\n\n'.join(
            f'{i + 1}\n{timestamp(s["start_ms"])} --> '
            f'{timestamp(s["end_ms"])}\n{s["source_text"]}'
            for i, s in enumerate(segments)
        )
        + '\n'
    )
    create_new_file(destination / 'source.srt', srt.encode())

    def sha(name: str) -> str:
        """Bind a generated input to its current bytes."""
        return hashlib.sha256((destination / name).read_bytes()).hexdigest()

    translation = {
        'schema_version': 1,
        'source': {
            'video_sha256': sha('spoken-source.mp4'),
            'srt_sha256': sha('source.srt'),
        },
        'source_language': 'en',
        'target_language': 'ru',
        'segments': [
            {
                'id': s['id'],
                'source_cues': [i + 1],
                'translation': s['translation'],
            }
            for i, s in enumerate(segments)
        ],
    }
    write_json(destination / 'translation.json', translation)
    try:
        rate = Decimal(description['pricing']['usd_per_million_chars'])
    except InvalidOperation as error:
        raise ExampleError(
            'price snapshot must be a finite positive decimal'
        ) from error
    if not rate.is_finite() or rate <= 0:
        raise ExampleError('price snapshot must be a finite positive decimal')
    counts = [len(s['translation']) for s in segments]
    per_request = rate * max(counts) / Decimal(1000000)
    limits = {
        'max_requests': len(segments),
        'max_text_chars': sum(counts),
        'max_cost_estimate': float(per_request * len(segments)),
        'price_per_request': float(per_request),
        'price_basis': (
            'Example only, not permission. '
            f'Snapshot {description["pricing"]["observed_on"]}: '
            f'{rate} USD per 1000000 chars; reserve at most '
            f'{max(counts)} chars per request. Verify current pricing at '
            f'{description["pricing"]["url"]} before any paid run.'
        ),
    }
    write_json(destination / 'limits.example.json', limits)
    notice = Path(__file__).with_name(description['notice_file']).read_bytes()
    create_new_file(destination / 'NOTICE.md', notice)
    result = {
        'schema_version': 1,
        'source': description['source'],
        'audio': audio,
        'input_sha256': translation['source'],
        'segments': len(segments),
        'text_chars': sum(counts),
        'price_snapshot': description['pricing'],
        'estimated_text_cost_usd': str(rate * sum(counts) / Decimal(1000000)),
        'media_tools': {
            'ffmpeg': tool_run([ffmpeg, '-version'], destination).splitlines()[
                0
            ],
            'ffprobe': tool_run(
                [ffprobe, '-version'], destination
            ).splitlines()[0],
        },
        'provider_calls': 0,
        'playback': False,
    }
    write_json(destination / 'example.json', result)
    marker.unlink()
    return result


def main(argv: list[str] | None = None) -> int:
    """Create spoken inputs without overwriting or synthesizing speech."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        'destination', type=Path, help='fresh directory outside the checkout'
    )
    parser.add_argument(
        '--audio-dir',
        type=Path,
        help='explicit previously downloaded public FLAC files',
    )
    args = parser.parse_args(argv)
    try:
        result = create_example(args.destination, args.audio_dir)
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.SubprocessError,
    ) as error:
        print(f'example creation failed: {error}', file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
