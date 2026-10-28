"""Checks for the public spoken example source boundary."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path
from typing import Any
from urllib.error import URLError

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/make-spoken-example.py'


@pytest.fixture
def example() -> Any:
    """Load the standalone source recipe."""
    spec = importlib.util.spec_from_file_location('spoken_example', SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def fabricated_source(
    example: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[dict[str, Any], Path]:
    """Create original tones for boundary tests without corpus downloads."""
    audio = tmp_path / 'original-audio'
    audio.mkdir()
    description = example.load_description()
    description['video']['duration_seconds'] = 2
    description['segments'] = description['segments'][:1]
    segment = description['segments'][0]
    segment['start_ms'], segment['end_ms'] = 0, 2000
    target = audio / f'{segment["source_id"]}.flac'
    result = subprocess.run(
        [
            'ffmpeg',
            '-nostdin',
            '-v',
            'error',
            '-n',
            '-f',
            'lavfi',
            '-i',
            'sine=frequency=440:sample_rate=16000:duration=0.25',
            '-c:a',
            'flac',
            str(target),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    body = target.read_bytes()
    segment['bytes'] = len(body)
    segment['sha256'] = hashlib.sha256(body).hexdigest()
    monkeypatch.setattr(example, 'load_description', lambda: description)
    return description, audio


def metadata_for(description: dict[str, Any]) -> dict[str, Any]:
    """Describe fabricated source bytes at the expected public origin."""
    source = description['source']
    return {
        'rows': [
            {
                'row_idx': segment['row_index'],
                'row': {
                    'id': segment['source_id'],
                    'text': segment['source_text'],
                    'file': '/ignored/foreign/private/path',
                    'audio': [
                        {
                            'src': 'https://datasets-server.huggingface.co/cached-assets/'
                            f'{source["dataset"]}/--/{"a" * 40}/--/'
                            f'{source["config"]}/{source["split"]}/'
                            f'{segment["row_index"]}/audio/audio.flac?secret=ephemeral',
                            'type': 'audio/flac',
                        }
                    ],
                },
            }
            for segment in description['segments']
        ]
    }


def test_shipped_description_has_reviewed_identity(example: Any) -> None:
    description = example.load_description()
    assert [s['source_id'] for s in description['segments']] == [
        '1272-128104-0000',
        '1272-128104-0001',
        '1272-128104-0002',
    ]
    assert sum(len(s['translation']) for s in description['segments']) == 318
    assert description['source']['license'] == 'CC-BY-4.0'


@pytest.mark.parametrize('field', ['id', 'text', 'origin', 'path', 'shape'])
def test_changed_public_identity_stops_before_audio_request(
    example: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    description = example.load_description()
    metadata = metadata_for(description)
    row = metadata['rows'][0]['row']
    if field in ('id', 'text'):
        row[field] = 'changed'
    elif field == 'origin':
        row['audio'][0]['src'] = 'https://other.example/audio.flac'
    elif field == 'path':
        row['audio'][0]['src'] = row['audio'][0]['src'].replace(
            '/0/audio/', '/99/audio/'
        )
    else:
        row['audio'] = []
    calls = []

    def read(opener: Any, url: str, limit: int) -> bytes:
        """Record metadata access and forbid a subsequent audio fetch."""
        calls.append(url)
        assert len(calls) == 1
        return json.dumps(metadata).encode()

    monkeypatch.setattr(example, 'public_opener', lambda host: object())
    monkeypatch.setattr(example, 'read_public', read)
    with pytest.raises(example.ExampleError):
        example.obtain_audio(tmp_path, description, None)
    assert len(calls) == 1 and '/rows?' in calls[0]
    assert list(tmp_path.iterdir()) == []


def test_public_provenance_omits_signed_query_and_foreign_path(
    example: Any,
    fabricated_source: tuple[dict[str, Any], Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    description, audio = fabricated_source
    calls = []

    def read(opener: Any, url: str, limit: int) -> bytes:
        """Return exact fabricated metadata and FLAC responses."""
        calls.append(url)
        if '/rows?' in url:
            return json.dumps(metadata_for(description)).encode()
        return next(audio.glob('*.flac')).read_bytes()

    monkeypatch.setattr(example, 'public_opener', lambda host: object())
    monkeypatch.setattr(example, 'read_public', read)
    output = tmp_path / 'public'
    output.mkdir()
    receipts = example.obtain_audio(output, description, None)
    serialized = json.dumps(receipts)
    assert len(calls) == 2
    assert 'ephemeral' not in serialized and '/ignored/' not in serialized
    assert receipts[0]['served_revision'] == 'a' * 40
    assert (
        receipts[0]['served_revision']
        != description['source']['reference_revision']
    )


@pytest.mark.parametrize('change', ['content', 'size', 'magic'])
def test_explicit_audio_must_match_exact_reviewed_bytes(
    example: Any,
    fabricated_source: tuple[dict[str, Any], Path],
    tmp_path: Path,
    change: str,
) -> None:
    description, audio = fabricated_source
    source_file = next(audio.glob('*.flac'))
    body = source_file.read_bytes()
    if change == 'size':
        body += b'x'
    elif change == 'magic':
        body = b'fake' + body[4:]
        description['segments'][0]['sha256'] = hashlib.sha256(body).hexdigest()
    else:
        body = body[:-1] + bytes([body[-1] ^ 1])
    source_file.write_bytes(body)
    with pytest.raises(example.ExampleError, match='identity mismatch'):
        example.obtain_audio(tmp_path, description, audio)
    assert not (tmp_path / source_file.name).exists()


@pytest.mark.parametrize('body,status', [(b'abc', 200), (b'x', 403)])
def test_public_response_is_bounded_and_requires_success(
    example: Any,
    body: bytes,
    status: int,
) -> None:
    class Response:
        """Expose a small HTTP response with a strict read limit."""

        def __enter__(self) -> Any:
            """Open the fake response."""
            return self

        def __exit__(self, *args: Any) -> None:
            """Close the fake response."""

        def read(self, limit: int) -> bytes:
            """Expose at most the requested response bytes."""
            assert limit == 3
            return body[:limit]

    response = Response()
    response.status = status  # type: ignore[attr-defined]

    class Opener:
        """Provide one fake response."""

        def open(self, request: Any, timeout: int) -> Response:
            """Assert token free public retrieval."""
            assert request.headers == {
                'User-agent': 'lecture-localizer-example/1'
            }
            return response

    with pytest.raises(example.ExampleError):
        example.read_public(Opener(), 'https://example.com/input', 2)


def test_network_failure_is_not_retried(example: Any) -> None:
    calls = []

    class Opener:
        """Fail a public request once."""

        def open(self, *args: Any, **kwargs: Any) -> None:
            """Record the attempted public retrieval."""
            calls.append(1)
            raise URLError('offline')

    with pytest.raises(example.ExampleError, match='without retry'):
        example.read_public(Opener(), 'https://example.com/input', 2)
    assert calls == [1]


@pytest.mark.parametrize(
    'url',
    [
        'http://example.com/a',
        'https://user:pass@example.com/a',
        'https://example.com:8443/a',
        'https://example.com/a#fragment',
        'https://other.example/a',
    ],
)
def test_redirect_rejects_other_origins(example: Any, url: str) -> None:
    with pytest.raises(example.ExampleError):
        example.SameHostRedirects('example.com').redirect_request(
            None, None, 302, 'redirect', {}, url
        )


def test_existing_destination_and_source_checkout_are_preserved(
    example: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / 'precious.bin'
    marker.write_bytes(b'preserve')
    with pytest.raises(FileExistsError):
        example.create_example(tmp_path)
    assert marker.read_bytes() == b'preserve'
    target = SCRIPT.parent / 'example-must-not-appear'
    with pytest.raises(example.ExampleError, match='outside'):
        example.create_example(target)
    assert not target.exists()


def test_offline_generation_binds_current_inputs_and_is_repeatable(
    example: Any,
    fabricated_source: tuple[dict[str, Any], Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, audio = fabricated_source

    def forbidden(*args: Any, **kwargs: Any) -> None:
        """Reject every network and credential subprocess attempt."""
        pytest.fail('offline source reuse attempted network access')

    monkeypatch.setattr(example, 'public_opener', forbidden)
    monkeypatch.setenv('XAI_API_KEY', 'not-a-real-key')
    original_run = example.subprocess.run
    observed = []

    def guarded(argv: list[str], **kwargs: Any) -> Any:
        """Permit only media subprocesses with credentials removed."""
        assert Path(argv[0]).name in ('ffmpeg', 'ffprobe')
        assert 'XAI_API_KEY' not in kwargs['env']
        observed.append(Path(argv[0]).name)
        return original_run(argv, **kwargs)

    monkeypatch.setattr(example.subprocess, 'run', guarded)
    outputs = [tmp_path / 'first', tmp_path / 'second']
    for target in outputs:
        result = example.create_example(target, audio)
        assert result['provider_calls'] == 0 and result['playback'] is False
        translation = json.loads((target / 'translation.json').read_text())
        for name, field in [
            ('spoken-source.mp4', 'video_sha256'),
            ('source.srt', 'srt_sha256'),
        ]:
            assert (
                translation['source'][field]
                == hashlib.sha256((target / name).read_bytes()).hexdigest()
            )
        assert (target / 'NOTICE.md').read_bytes() == (
            SCRIPT.with_name('SPOKEN_EXAMPLE_NOTICE.md').read_bytes()
        )
        assert not (target / '.example-incomplete').exists()
    for name in ('spoken-source.mp4', 'source.srt', 'translation.json'):
        assert (outputs[0] / name).read_bytes() == (
            outputs[1] / name
        ).read_bytes()
    assert observed.count('ffmpeg') == 4


def test_failed_generation_preserves_partial_inputs(
    example: Any,
    fabricated_source: tuple[dict[str, Any], Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, audio = fabricated_source

    def fail(argv: list[str], directory: Path) -> str:
        """Fail the media inspection after preserving exact input bytes."""
        raise example.ExampleError('inspection failure')

    monkeypatch.setattr(example, 'tool_run', fail)
    target = tmp_path / 'failed'
    with pytest.raises(example.ExampleError, match='inspection failure'):
        example.create_example(target, audio)
    assert (target / '.example-incomplete').is_file()
    assert next(target.glob('*.flac')).read_bytes() == (
        next(audio.glob('*.flac')).read_bytes()
    )


def test_dangling_destination_link_is_not_followed(
    example: Any,
    tmp_path: Path,
) -> None:
    target = tmp_path / 'absent'
    link = tmp_path / 'link'
    link.symlink_to(target)
    with pytest.raises(FileExistsError):
        example.create_example(link)
    assert link.is_symlink() and not target.exists()


def test_valid_bytes_with_wrong_audio_profile_are_refused(
    example: Any,
    fabricated_source: tuple[dict[str, Any], Path],
    tmp_path: Path,
) -> None:
    description, audio = fabricated_source
    description['source']['sample_rate_hz'] = 48000
    target = tmp_path / 'wrong-profile'
    with pytest.raises(example.ExampleError, match='recording profile'):
        example.create_example(target, audio)
    assert (target / '.example-incomplete').exists()
    assert not (target / 'spoken-source.mp4').exists()


def test_malformed_windows_fail_before_output(
    example: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    description = copy.deepcopy(example.load_description())
    description['segments'][0]['end_ms'] = 0
    config = tmp_path / 'invalid.json'
    config.write_text(json.dumps(description))
    monkeypatch.setattr(example, 'CONFIG_FILE', config)
    target = tmp_path / 'uncreated'
    with pytest.raises(example.ExampleError, match='cue windows'):
        example.create_example(target)
    assert not target.exists()
