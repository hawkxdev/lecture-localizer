"""Speech normalization, window fitting, streaming timeline and AAC."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from lecture_localizer.media import MediaToolError, checked_run_strict
from lecture_localizer.storage import sha256_file

SAMPLES_PER_MS = 48
SAMPLE_RATE = 48_000
CHANNELS = 1
SAMPLE_FORMAT = 's16le'
BYTES_PER_SAMPLE = 2
NARRATION_BIT_RATE = 192_000
CONVERTIBLE_CODECS = frozenset({'pcm_s16le', 'mp3', 'opus'})
SUPPORTED_ORIGINAL_CHANNELS = frozenset({1, 2})
PRIMING_TOLERANCE_FRAMES = 4_096
_CHUNK_BYTES = 1 << 20

_AAC_PROFILE = 'LC'
_BIT_RATE_TOLERANCE = 0.05


class AudioError(RuntimeError):
    """A speech or audio preparation contract failed."""

    def __init__(self, message: str, kind: str = 'audio') -> None:
        """Record the failure message and its stable kind."""
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class NormalizedSpeech:
    """Normalized working PCM plus the integrity of its raw source."""

    raw_path: Path
    raw_sha256: str
    path: Path
    sha256: str
    frames: int
    sample_rate: int = SAMPLE_RATE
    channels: int = CHANNELS
    sample_format: str = SAMPLE_FORMAT

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON serializable handoff view."""
        return {
            'raw_path': str(self.raw_path),
            'raw_sha256': self.raw_sha256,
            'path': str(self.path),
            'sha256': self.sha256,
            'frames': self.frames,
            'sample_rate': self.sample_rate,
            'channels': self.channels,
            'sample_format': self.sample_format,
        }


@dataclass(frozen=True)
class SegmentPlacement:
    """One caller supplied speech placement request for a segment."""

    segment_id: str
    start_ms: int
    end_ms: int
    speech: NormalizedSpeech


@dataclass(frozen=True)
class SegmentFit:
    """One segment fitting outcome inside its integer sample window."""

    segment_id: str
    start_frame: int
    end_frame: int
    window_frames: int
    speech_frames: int
    required_speedup: float
    max_speedup: float
    disposition: str
    placed_frames: int = 0
    pcm_path: Path | None = None
    pcm_sha256: str | None = None
    measured_frames: int | None = None
    refusal: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON serializable handoff view."""
        return {
            'segment_id': self.segment_id,
            'start_frame': self.start_frame,
            'end_frame': self.end_frame,
            'window_frames': self.window_frames,
            'speech_frames': self.speech_frames,
            'required_speedup': self.required_speedup,
            'max_speedup': self.max_speedup,
            'disposition': self.disposition,
            'placed_frames': self.placed_frames,
            'pcm_path': None if self.pcm_path is None else str(self.pcm_path),
            'pcm_sha256': self.pcm_sha256,
            'measured_frames': self.measured_frames,
            'refusal': self.refusal,
        }


@dataclass(frozen=True)
class TimelineResult:
    """The streamed headerless PCM timeline and its segment fits."""

    path: Path
    sha256: str
    frames: int
    sample_rate: int = SAMPLE_RATE
    channels: int = CHANNELS
    sample_format: str = SAMPLE_FORMAT
    placements: tuple[SegmentFit, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON serializable handoff view."""
        return {
            'path': str(self.path),
            'sha256': self.sha256,
            'frames': self.frames,
            'sample_rate': self.sample_rate,
            'channels': self.channels,
            'sample_format': self.sample_format,
            'placements': [fit.to_dict() for fit in self.placements],
        }


@dataclass(frozen=True)
class EncodedAudio:
    """One prepared AAC artifact with its verified stream facts."""

    path: Path
    sha256: str
    disposition: str
    codec: str
    profile: str | None
    channels: int
    sample_rate: int
    bit_rate: int | None
    input_frames: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON serializable handoff view."""
        return {
            'path': str(self.path),
            'sha256': self.sha256,
            'disposition': self.disposition,
            'codec': self.codec,
            'profile': self.profile,
            'channels': self.channels,
            'sample_rate': self.sample_rate,
            'bit_rate': self.bit_rate,
            'input_frames': self.input_frames,
        }


def _validated_ms(value: Any, label: str) -> int:
    """Reject non integer or boolean millisecond values."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise AudioError(
            f'{label} must be an integer millisecond count, got {value!r}',
            kind='window',
        )
    return value


def _window_frames(start_ms: Any, end_ms: Any) -> tuple[int, int]:
    """Map integer milliseconds to exact 48 samples per ms frames."""
    start = _validated_ms(start_ms, 'segment start')
    end = _validated_ms(end_ms, 'segment end')
    if start < 0:
        raise AudioError(
            f'segment window start must be nonnegative, got {start} ms',
            kind='window',
        )
    if end <= start:
        raise AudioError(
            f'segment window must have a positive span, got {start}..{end} ms',
            kind='window',
        )
    return start * SAMPLES_PER_MS, end * SAMPLES_PER_MS


def _pcm_frames(path: Path) -> int:
    """Return the s16le frame count of one PCM file."""
    size = path.stat().st_size
    if size % BYTES_PER_SAMPLE:
        raise AudioError(
            f'PCM byte size is not a whole number of frames: {path}',
            kind='speech',
        )
    return size // BYTES_PER_SAMPLE


def _same_path(left: Path, right: Path) -> bool:
    """Report whether two paths name one filesystem location."""
    if left.exists() and right.exists():
        try:
            return left.samefile(right)
        except OSError:
            return False
    return left.resolve() == right.resolve()


@contextmanager
def _fresh_output(destination: Path, label: str) -> Iterator[Path]:
    """Yield a temporary path published to the destination on success.

    The destination is only created by the final hard link, so an
    existing destination is never modified and a failure or interrupt
    leaves no partial output behind.
    """
    parent = destination.parent
    try:
        fd, temp_name = tempfile.mkstemp(
            dir=parent, prefix=f'.{destination.name}.', suffix='.partial'
        )
    except OSError as error:
        raise AudioError(
            f'cannot prepare the {label} output inside {parent}: {error}',
            kind='destination',
        ) from error
    os.close(fd)
    temp = Path(temp_name)
    try:
        yield temp
        try:
            os.link(temp, destination)
        except FileExistsError as error:
            raise AudioError(
                f'{label} destination already exists: {destination}',
                kind='destination',
            ) from error
        except OSError as error:
            raise AudioError(
                f'cannot publish the {label} result to {destination}: {error}',
                kind='storage',
            ) from error
    finally:
        temp.unlink(missing_ok=True)


def _write_silence(handle: BinaryIO, frames: int) -> None:
    """Stream zero valued PCM frames in bounded chunks."""
    remaining = frames * BYTES_PER_SAMPLE
    while remaining > 0:
        step = min(_CHUNK_BYTES, remaining)
        handle.write(b'\x00' * step)
        remaining -= step


def _verify_pcm_record(path: Path, sha256_hex: str, frames: int) -> None:
    """Verify one PCM file against its recorded hash and frame count."""
    actual = sha256_file(path)
    if actual != sha256_hex:
        raise AudioError(
            f'PCM artifact {path} no longer matches its recorded'
            f' integrity: expected {sha256_hex}, found {actual}',
            kind='speech',
        )
    measured = _pcm_frames(path)
    if measured != frames:
        raise AudioError(
            f'PCM artifact {path} holds {measured} frames but its record'
            f' declares {frames}',
            kind='speech',
        )


def _verify_speech_artifact(speech: NormalizedSpeech) -> None:
    """Verify recorded integrity of one normalized speech artifact."""
    _verify_pcm_record(speech.path, speech.sha256, speech.frames)


def _copy_pcm(handle: BinaryIO, source: Path, frames: int) -> None:
    """Stream exactly the recorded PCM frames from one file."""
    expected = frames * BYTES_PER_SAMPLE
    actual = source.stat().st_size
    if actual != expected:
        raise AudioError(
            f'PCM file {source} holds {actual} bytes but its record'
            f' declares {expected} bytes for {frames} frames',
            kind='speech',
        )
    with open(source, 'rb') as pcm:
        shutil.copyfileobj(pcm, handle, length=_CHUNK_BYTES)


def _require_fresh(destination: Path, label: str) -> None:
    """Reject an existing destination before any work starts."""
    if destination.exists():
        raise AudioError(
            f'{label} destination already exists: {destination}',
            kind='destination',
        )


def _speech_tool_args() -> list[str]:
    """Return the raw PCM input format arguments for ffmpeg."""
    return [
        '-f',
        SAMPLE_FORMAT,
        '-ar',
        str(SAMPLE_RATE),
        '-ac',
        str(CHANNELS),
    ]


def _validate_wav_structure(source: Path) -> None:
    """Reject incomplete or malformed WAV structure before decoding.

    FFmpeg exits successfully on a structurally truncated file and
    silently returns only the decodable prefix, so completion is
    established from the declared RIFF and chunk sizes rather than
    from the tool exit code. Headers are walked with seeks and
    bounded reads, never by retaining payload bytes in memory.
    """
    try:
        actual_size = source.stat().st_size
        with open(source, 'rb') as handle:
            header = handle.read(12)
            if (
                len(header) < 12
                or header[0:4] != b'RIFF'
                or header[8:12] != b'WAVE'
            ):
                raise AudioError(
                    f'retained speech is not a RIFF WAVE file: {source}',
                    kind='speech',
                )
            riff_size = int.from_bytes(header[4:8], 'little')
            if riff_size != actual_size - 8:
                raise AudioError(
                    f'retained speech is truncated: the RIFF header'
                    f' declares {riff_size + 8} bytes but the file holds'
                    f' {actual_size}: {source}',
                    kind='speech',
                )
            position = 12
            found_data = False
            while position + 8 <= actual_size:
                handle.seek(position)
                chunk_header = handle.read(8)
                chunk_id = chunk_header[0:4]
                chunk_size = int.from_bytes(chunk_header[4:8], 'little')
                content_end = position + 8 + chunk_size
                if content_end > actual_size:
                    label = chunk_id.decode('ascii', 'replace')
                    raise AudioError(
                        f'retained speech is truncated inside its'
                        f' {label!r} chunk: {source}',
                        kind='speech',
                    )
                if chunk_id == b'data':
                    found_data = True
                position = content_end + (chunk_size & 1)
    except OSError as error:
        raise AudioError(
            f'retained speech is unreadable: {error}', kind='speech'
        ) from error
    if not found_data:
        raise AudioError(
            f'retained speech has no PCM data chunk: {source}',
            kind='speech',
        )


def normalize_wav(
    source: Path,
    destination: Path,
    *,
    ffmpeg_path: str,
    timeout_seconds: float,
) -> NormalizedSpeech:
    """Normalize a complete retained WAV to mono s16le PCM at 48 kHz.

    The raw response bytes are never modified; structural truncation
    and decoding failures are reported as invalid speech, never as a
    successful partial result or silence.
    """
    if not source.is_file():
        raise AudioError(
            f'retained speech file is missing: {source}', kind='speech'
        )
    _validate_wav_structure(source)
    if _same_path(source, destination):
        raise AudioError(
            'normalization source and destination name one location:'
            f' {source}',
            kind='destination',
        )
    _require_fresh(destination, 'normalized speech')
    with _fresh_output(destination, 'normalized speech') as temp:
        argv = [
            ffmpeg_path,
            '-hide_banner',
            '-loglevel',
            'error',
            '-y',
            '-i',
            str(source),
            '-vn',
            '-acodec',
            'pcm_s16le',
            '-ac',
            str(CHANNELS),
            '-ar',
            str(SAMPLE_RATE),
            '-f',
            SAMPLE_FORMAT,
            str(temp),
        ]
        try:
            checked_run_strict(argv, timeout_seconds)
        except MediaToolError as error:
            raise AudioError(
                f'retained speech is not decodable: {error}', kind='speech'
            ) from error
        frames = _pcm_frames(temp)
        if frames == 0:
            raise AudioError(
                f'retained speech decodes to zero frames: {source}',
                kind='speech',
            )
    return NormalizedSpeech(
        raw_path=source,
        raw_sha256=sha256_file(source),
        path=destination,
        sha256=sha256_file(destination),
        frames=frames,
    )


def fit_segment(
    placement: SegmentPlacement,
    *,
    max_speedup: float,
    work_dir: Path,
    ffmpeg_path: str,
    timeout_seconds: float,
) -> SegmentFit:
    """Fit one speech recording into its window without trimming.

    Shorter speech keeps normal speed and is placed as is. Longer
    speech is fitted with the pitch preserving atempo filter only up
    to the configured maximum, and the fitted output is measured
    again: an overrun is refused, never trimmed. The supplied
    artifact is verified against its recorded integrity first, so
    replaced or truncated PCM is refused before any fitting or
    copying.
    """
    _verify_speech_artifact(placement.speech)
    start_frame, end_frame = _window_frames(
        placement.start_ms, placement.end_ms
    )
    window_frames = end_frame - start_frame
    speech_frames = placement.speech.frames
    required = max(1.0, speech_frames / window_frames)
    if required > max_speedup:
        return SegmentFit(
            segment_id=placement.segment_id,
            start_frame=start_frame,
            end_frame=end_frame,
            window_frames=window_frames,
            speech_frames=speech_frames,
            required_speedup=required,
            max_speedup=max_speedup,
            disposition='refused',
            refusal=(
                f'required speedup {required:.6f} exceeds the configured'
                f' maximum {max_speedup:.6f} for segment'
                f' {placement.segment_id!r}'
            ),
        )
    if speech_frames <= window_frames:
        return SegmentFit(
            segment_id=placement.segment_id,
            start_frame=start_frame,
            end_frame=end_frame,
            window_frames=window_frames,
            speech_frames=speech_frames,
            required_speedup=required,
            max_speedup=max_speedup,
            disposition='normal',
            placed_frames=speech_frames,
            pcm_path=placement.speech.path,
            pcm_sha256=placement.speech.sha256,
        )
    work_dir.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=work_dir, prefix='fitted-', suffix='.pcm')
    os.close(fd)
    fitted = Path(name)
    argv = [
        ffmpeg_path,
        '-hide_banner',
        '-loglevel',
        'error',
        *_speech_tool_args(),
        '-i',
        str(placement.speech.path),
        '-af',
        f'atempo={required:.6f}',
        *_speech_tool_args(),
        '-y',
        str(fitted),
    ]
    try:
        try:
            checked_run_strict(argv, timeout_seconds)
        except MediaToolError as error:
            raise AudioError(
                f'atempo fitting failed for segment'
                f' {placement.segment_id!r}: {error}',
                kind='tool',
            ) from error
        measured = _pcm_frames(fitted)
        if measured > window_frames:
            fitted.unlink(missing_ok=True)
            return SegmentFit(
                segment_id=placement.segment_id,
                start_frame=start_frame,
                end_frame=end_frame,
                window_frames=window_frames,
                speech_frames=speech_frames,
                required_speedup=required,
                max_speedup=max_speedup,
                disposition='refused',
                measured_frames=measured,
                refusal=(
                    f'post fit output of {measured} frames overruns the'
                    f' {window_frames} frame window of segment'
                    f' {placement.segment_id!r}; refusing without trimming'
                ),
            )
    except BaseException:
        fitted.unlink(missing_ok=True)
        raise
    return SegmentFit(
        segment_id=placement.segment_id,
        start_frame=start_frame,
        end_frame=end_frame,
        window_frames=window_frames,
        speech_frames=speech_frames,
        required_speedup=required,
        max_speedup=max_speedup,
        disposition='fitted',
        placed_frames=measured,
        pcm_path=fitted,
        pcm_sha256=sha256_file(fitted),
        measured_frames=measured,
    )


def _discard_disposable(fits: Sequence[SegmentFit]) -> None:
    """Remove temporary fitted outputs after a refused timeline."""
    for fit in fits:
        if fit.disposition == 'fitted' and fit.pcm_path is not None:
            fit.pcm_path.unlink(missing_ok=True)


def _validated_geometry(
    placements: Sequence[SegmentPlacement], total_frames: Any
) -> list[tuple[int, int]]:
    """Validate ordered nonoverlapping windows inside the timeline."""
    if isinstance(total_frames, bool) or not isinstance(total_frames, int):
        raise AudioError(
            f'timeline frame count must be an integer, got {total_frames!r}',
            kind='window',
        )
    if total_frames <= 0:
        raise AudioError(
            f'timeline frame count must be positive, got {total_frames}',
            kind='window',
        )
    geometry: list[tuple[int, int]] = []
    cursor = 0
    for placement in placements:
        start_frame, end_frame = _window_frames(
            placement.start_ms, placement.end_ms
        )
        if start_frame < cursor:
            raise AudioError(
                f'segment {placement.segment_id!r} window overlaps or'
                f' precedes the previous window end at frame {cursor}',
                kind='window',
            )
        if end_frame > total_frames:
            raise AudioError(
                f'segment {placement.segment_id!r} window ends at frame'
                f' {end_frame} beyond the {total_frames} frame timeline',
                kind='window',
            )
        geometry.append((start_frame, end_frame))
        cursor = end_frame
    return geometry


def stream_timeline(
    placements: Sequence[SegmentPlacement],
    *,
    total_frames: int,
    destination: Path,
    max_speedup: float,
    work_dir: Path,
    ffmpeg_path: str,
    timeout_seconds: float,
) -> TimelineResult:
    """Stream complete segments and silence into one headerless PCM file.

    Every segment is fitted first, so a refusal is reported before the
    destination is touched. Silence fills unused window time, gaps and
    the trailing timeline, and data is written in bounded chunks
    without holding the whole lecture in memory.
    """
    _validated_geometry(placements, total_frames)
    fits: list[SegmentFit] = []
    try:
        for placement in placements:
            fits.append(
                fit_segment(
                    placement,
                    max_speedup=max_speedup,
                    work_dir=work_dir,
                    ffmpeg_path=ffmpeg_path,
                    timeout_seconds=timeout_seconds,
                )
            )
    except BaseException:
        _discard_disposable(fits)
        raise
    refused = [fit for fit in fits if fit.disposition == 'refused']
    if refused:
        _discard_disposable(fits)
        details = ' | '.join(
            f'segment {fit.segment_id!r}: {fit.refusal}' for fit in refused
        )
        raise AudioError(
            f'timeline refused for {len(refused)} segment(s): {details}',
            kind='fit',
        )
    _require_fresh(destination, 'timeline')
    with (
        _fresh_output(destination, 'timeline') as temp,
        open(temp, 'wb') as handle,
    ):
        try:
            cursor = 0
            for fit in fits:
                assert fit.pcm_path is not None
                assert fit.pcm_sha256 is not None
                _write_silence(handle, fit.start_frame - cursor)
                _verify_pcm_record(
                    fit.pcm_path, fit.pcm_sha256, fit.placed_frames
                )
                _copy_pcm(handle, fit.pcm_path, fit.placed_frames)
                cursor = fit.start_frame + fit.placed_frames
            _write_silence(handle, total_frames - cursor)
            handle.flush()
            os.fsync(handle.fileno())
        except OSError as error:
            raise AudioError(
                f'timeline streaming failed for {destination}: {error}',
                kind='storage',
            ) from error
    return TimelineResult(
        path=destination,
        sha256=sha256_file(destination),
        frames=total_frames,
        placements=tuple(fits),
    )


def _probe_audio_facts(
    ffprobe_path: str,
    media: Path,
    stream_specifier: str,
    timeout_seconds: float,
) -> tuple[dict[str, Any], int | None]:
    """Return selected audio stream facts and the container bit rate."""
    argv = [
        ffprobe_path,
        '-v',
        'error',
        '-select_streams',
        stream_specifier,
        '-show_entries',
        'stream=codec_name,profile,channels,sample_rate,bit_rate:'
        'format=bit_rate',
        '-of',
        'json',
        str(media),
    ]
    try:
        result = checked_run_strict(argv, timeout_seconds)
    except MediaToolError as error:
        raise AudioError(
            f'audio stream facts are unavailable for {media}: {error}',
            kind='tool',
        ) from error
    try:
        payload = json.loads(result.stdout)
        streams = payload['streams']
        if len(streams) != 1:
            raise AudioError(
                f'expected exactly one selected audio stream in {media},'
                f' selection {stream_specifier!r} matched {len(streams)}',
                kind='profile',
            )
        stream = streams[0]
        format_bit_rate = payload.get('format', {}).get('bit_rate')
    except (ValueError, KeyError, TypeError) as error:
        raise AudioError(
            f'unusable ffprobe audio facts for {media}: {error}',
            kind='tool',
        ) from error
    declared: int | None
    try:
        declared = None if format_bit_rate is None else int(format_bit_rate)
    except (TypeError, ValueError) as error:
        raise AudioError(
            f'unusable ffprobe bit rate for {media}: {error}', kind='tool'
        ) from error
    return stream, declared


def _verified_aac(
    destination: Path,
    disposition: str,
    expected_channels: int,
    input_frames: int | None,
    facts: dict[str, Any],
    container_bit_rate: int | None,
    enforce_profile: bool,
) -> EncodedAudio:
    """Verify prepared AAC facts and build the result record.

    The measured average bit rate is retained in the record but never
    enforced here: lossy encoders undershoot the request for tone or
    sparse content, so only a dense probe can support a bit rate
    assertion, and that choice belongs to the caller of this module.
    """
    codec = facts.get('codec_name')
    profile = facts.get('profile')
    channels = facts.get('channels')
    sample_rate = facts.get('sample_rate')
    bit_rate = facts.get('bit_rate')
    if codec != 'aac' or profile != _AAC_PROFILE:
        raise AudioError(
            f'prepared audio in {destination} is not AAC LC:'
            f' codec {codec!r} profile {profile!r}',
            kind='profile',
        )
    if channels != expected_channels or channels not in (
        1,
        2,
    ):
        raise AudioError(
            f'prepared audio in {destination} has {channels!r} channels,'
            f' expected {expected_channels}',
            kind='profile',
        )
    try:
        measured_rate = None if sample_rate is None else int(sample_rate)
    except (TypeError, ValueError) as error:
        raise AudioError(
            f'prepared audio in {destination} has unusable sample rate'
            f' {sample_rate!r}',
            kind='profile',
        ) from error
    if measured_rate is None or measured_rate <= 0:
        raise AudioError(
            f'prepared audio in {destination} has unusable sample rate'
            f' {sample_rate!r}',
            kind='profile',
        )
    if enforce_profile and measured_rate != SAMPLE_RATE:
        raise AudioError(
            f'prepared audio in {destination} has sample rate'
            f' {sample_rate!r}, expected {SAMPLE_RATE}',
            kind='profile',
        )
    declared = bit_rate if bit_rate is not None else container_bit_rate
    measured: int | None = None
    if declared is not None:
        measured = int(declared)
    return EncodedAudio(
        path=destination,
        sha256=sha256_file(destination),
        disposition=disposition,
        codec='aac',
        profile=profile,
        channels=int(channels),
        sample_rate=measured_rate,
        bit_rate=measured,
        input_frames=input_frames,
    )


def encode_narration_aac(
    source: Path,
    destination: Path,
    *,
    ffmpeg_path: str,
    ffprobe_path: str,
    timeout_seconds: float,
) -> EncodedAudio:
    """Encode narration PCM as mono AAC LC at 48 kHz and 192 kbit/s.

    The encoded stream carries codec priming and padding, so its
    decoded length may exceed the input frames by a small documented
    tolerance; decoded samples are never equated to a PCM hash.
    """
    _require_fresh(destination, 'narration AAC')
    with _fresh_output(destination, 'narration AAC') as temp:
        argv = [
            ffmpeg_path,
            '-hide_banner',
            '-loglevel',
            'error',
            '-y',
            *_speech_tool_args(),
            '-i',
            str(source),
            '-vn',
            '-c:a',
            'aac',
            '-b:a',
            f'{NARRATION_BIT_RATE // 1000}k',
            '-ar',
            str(SAMPLE_RATE),
            '-ac',
            str(CHANNELS),
            '-f',
            'ipod',
            str(temp),
        ]
        try:
            checked_run_strict(argv, timeout_seconds)
        except MediaToolError as error:
            raise AudioError(
                f'narration AAC encoding failed: {error}', kind='tool'
            ) from error
    frames: int | None = None
    if source.suffix == '.pcm':
        frames = _pcm_frames(source)
    facts, container_bit_rate = _probe_audio_facts(
        ffprobe_path, destination, 'a:0', timeout_seconds
    )
    return _verified_aac(
        destination,
        'converted',
        CHANNELS,
        frames,
        facts,
        container_bit_rate,
        enforce_profile=True,
    )


def prepare_original_audio(
    source: Path,
    stream_index: int,
    destination: Path,
    *,
    ffmpeg_path: str,
    ffprobe_path: str,
    timeout_seconds: float,
) -> EncodedAudio:
    """Copy compatible original AAC LC or convert it to AAC LC.

    PCM s16le, MP3 and Opus originals are converted at 48 kHz and
    192 kbit/s while their one or two channels are preserved. A
    compatible AAC LC original stays on the packet copy path without
    resampling or re-encoding.
    """
    if isinstance(stream_index, bool) or not isinstance(stream_index, int):
        raise AudioError(
            f'original audio stream index must be an integer, got'
            f' {stream_index!r}',
            kind='profile',
        )
    if not source.is_file():
        raise AudioError(
            f'original media file is missing: {source}', kind='profile'
        )
    _require_fresh(destination, 'original audio')
    facts, _source_bit_rate = _probe_audio_facts(
        ffprobe_path, source, str(stream_index), timeout_seconds
    )
    codec = facts.get('codec_name')
    profile = facts.get('profile')
    channels = facts.get('channels')
    if channels not in SUPPORTED_ORIGINAL_CHANNELS:
        raise AudioError(
            f'original audio stream {stream_index} in {source} has'
            f' {channels!r} channels; only mono and stereo are supported',
            kind='profile',
        )
    if codec == 'aac':
        if profile != _AAC_PROFILE:
            raise AudioError(
                f'original AAC stream {stream_index} in {source} has'
                f' profile {profile!r}; only AAC LC is copied',
                kind='profile',
            )
        mode = 'copied'
        codec_args = ['-c:a', 'copy']
    elif codec in CONVERTIBLE_CODECS:
        mode = 'converted'
        codec_args = [
            '-c:a',
            'aac',
            '-b:a',
            f'{NARRATION_BIT_RATE // 1000}k',
            '-ar',
            str(SAMPLE_RATE),
            '-ac',
            str(channels),
        ]
    else:
        raise AudioError(
            f'original audio stream {stream_index} in {source} uses'
            f' unsupported codec {codec!r}',
            kind='profile',
        )
    with _fresh_output(destination, 'original audio') as temp:
        argv = [
            ffmpeg_path,
            '-hide_banner',
            '-loglevel',
            'error',
            '-y',
            '-i',
            str(source),
            '-vn',
            '-map',
            f'0:{stream_index}',
            *codec_args,
            '-f',
            'ipod',
            str(temp),
        ]
        try:
            checked_run_strict(argv, timeout_seconds)
        except MediaToolError as error:
            raise AudioError(
                f'original audio preparation failed for {source}: {error}',
                kind='tool',
            ) from error
    result_facts, result_container_bit_rate = _probe_audio_facts(
        ffprobe_path, destination, 'a:0', timeout_seconds
    )
    return _verified_aac(
        destination,
        mode,
        int(channels),
        None,
        result_facts,
        result_container_bit_rate,
        enforce_profile=mode == 'converted',
    )
