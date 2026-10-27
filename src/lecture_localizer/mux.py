"""Explicit five stream MP4 assembly with persisted track flags."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lecture_localizer.media import (
    MediaToolError,
    checked_run_strict,
    discover_tool,
)
from lecture_localizer.settings import Settings

_MP4BOX_TRACK_RE = re.compile(r'^# Track \d+ Info - ID (\d+)')
_MP4BOX_MEDIA_TYPE_RE = re.compile(r'^Media Type: (\w+)')

_EXPECTED_MEDIA_TYPES = ('vide', 'soun', 'soun', 'sbtl', 'sbtl')


class MuxError(RuntimeError):
    """Container assembly input or staging contract is violated."""

    def __init__(self, message: str, kind: str = 'mux') -> None:
        """Record the failure message and its stable kind."""
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class TrackProfile:
    """Startup default flags applied to the candidate tracks."""

    narration_default: bool = True
    original_default: bool = False
    subtitles_default: bool = False
    subtitles_forced: bool = False


@dataclass(frozen=True)
class CopiedAudioStream:
    """Selected original audio stream copied from the video input."""

    stream_index: int


@dataclass(frozen=True)
class ConvertedAudioTrack:
    """Prepared original audio as a separate compatible AAC file."""

    path: Path


OriginalAudio = CopiedAudioStream | ConvertedAudioTrack


@dataclass(frozen=True)
class MuxRequest:
    """Explicit input mapping for one fresh staging candidate."""

    video_path: Path
    original_audio: OriginalAudio
    narration_path: Path
    english_srt_path: Path
    russian_srt_path: Path
    destination: Path
    profile: TrackProfile


@dataclass(frozen=True)
class TrackIds:
    """Actual persisted MP4Box track identifiers by stream role."""

    video: int
    original_audio: int
    narration: int
    english_subtitles: int
    russian_subtitles: int


@dataclass(frozen=True)
class MuxResult:
    """Fresh staging candidate plus the applied flag operations."""

    destination: Path
    ffmpeg_argv: tuple[str, ...]
    mp4box_argv: tuple[str, ...]
    track_ids: TrackIds


def _validate_profile(profile: TrackProfile) -> None:
    """Reject profiles the MP4Box toolchain cannot persist."""
    if profile.subtitles_forced:
        raise MuxError(
            'persisting forced subtitle flags is not supported by the'
            ' MP4Box toolchain; keep startup_subtitles_forced false',
            kind='profile',
        )


def _require_input_file(path: Path, role: str) -> None:
    """Require one existing regular input file."""
    if not path.is_file():
        raise MuxError(
            f'{role} input file is missing: {path}',
            kind='input',
        )


def _validate_paths(request: MuxRequest) -> None:
    """Validate the explicit input files and the fresh destination."""
    _require_input_file(request.video_path, 'video')
    if isinstance(request.original_audio, ConvertedAudioTrack):
        _require_input_file(request.original_audio.path, 'original audio')
    _require_input_file(request.narration_path, 'narration audio')
    _require_input_file(request.english_srt_path, 'english subtitles')
    _require_input_file(request.russian_srt_path, 'russian subtitles')
    if request.destination.exists():
        raise MuxError(
            f'refusing to overwrite the existing staging destination:'
            f' {request.destination}',
            kind='destination',
        )
    if not request.destination.parent.is_dir():
        raise MuxError(
            f'staging destination parent directory is missing:'
            f' {request.destination.parent}',
            kind='destination',
        )


def _probe_input_streams(
    ffprobe: Path, video_path: Path, timeout_seconds: float
) -> list[dict[str, Any]]:
    """Return index and codec type rows for one media input."""
    result = checked_run_strict(
        [
            str(ffprobe),
            '-v',
            'error',
            '-show_entries',
            'stream=index,codec_type',
            '-of',
            'json',
            str(video_path),
        ],
        timeout_seconds,
    )
    try:
        payload = json.loads(result.stdout)
        streams = payload['streams']
        if not isinstance(streams, list):
            raise TypeError('streams is not a list')
        return [
            {
                'index': int(stream['index']),
                'codec_type': str(stream['codec_type']),
            }
            for stream in streams
        ]
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        raise MediaToolError(
            f'unusable ffprobe stream output for {video_path}: {error}'
        ) from error


def _video_stream_index(
    streams: list[dict[str, Any]], video_path: Path
) -> int:
    """Return the single video stream index of one input."""
    video_indexes = [
        int(stream['index'])
        for stream in streams
        if stream['codec_type'] == 'video'
    ]
    if len(video_indexes) != 1:
        raise MuxError(
            f'video input must carry exactly one video stream, found'
            f' {len(video_indexes)} in {video_path}',
            kind='input',
        )
    return video_indexes[0]


def _validate_copied_stream(
    streams: list[dict[str, Any]], stream_index: int, video_path: Path
) -> None:
    """Require the selected copied stream to be an audio stream."""
    for stream in streams:
        if int(stream['index']) == stream_index:
            if stream['codec_type'] == 'audio':
                return
            raise MuxError(
                f'selected original audio stream {stream_index} in'
                f' {video_path} is not an audio stream',
                kind='input',
            )
    raise MuxError(
        f'selected original audio stream {stream_index} is absent in'
        f' {video_path}',
        kind='input',
    )


def _input_layout(
    request: MuxRequest, streams: list[dict[str, Any]]
) -> tuple[str, str, int]:
    """Return explicit map specifiers and the narration input index."""
    video_index = _video_stream_index(streams, request.video_path)
    video_map = f'0:{video_index}'
    if isinstance(request.original_audio, CopiedAudioStream):
        _validate_copied_stream(
            streams, request.original_audio.stream_index, request.video_path
        )
        return video_map, f'0:{request.original_audio.stream_index}', 1
    return video_map, '1:0', 2


def _ffmpeg_argv(
    request: MuxRequest,
    ffmpeg: Path,
    video_map: str,
    original_map: str,
    narration_input: int,
    output_path: Path,
) -> tuple[str, ...]:
    """Build the explicit five stream ffmpeg assembly command."""
    inputs = [str(request.video_path)]
    if isinstance(request.original_audio, ConvertedAudioTrack):
        inputs.append(str(request.original_audio.path))
    inputs.append(str(request.narration_path))
    inputs.append(str(request.english_srt_path))
    inputs.append(str(request.russian_srt_path))
    maps = (
        video_map,
        original_map,
        f'{narration_input}:0',
        f'{narration_input + 1}:0',
        f'{narration_input + 2}:0',
    )
    argv = [str(ffmpeg), '-hide_banner', '-loglevel', 'error']
    for path in inputs:
        argv.extend(['-i', path])
    for map_spec in maps:
        argv.extend(['-map', map_spec])
    # copyts workaround: without it the mov muxer rebases stream copy
    # starts to zero, shifting nonzero input timelines like 2.000000 to
    # 0.023047 and breaking original packet timeline preservation.
    argv.extend(
        [
            '-copyts',
            '-c:v',
            'copy',
            '-c:a',
            'copy',
            '-c:s',
            'mov_text',
            '-metadata:s:a:0',
            'language=eng',
            '-metadata:s:a:1',
            'language=rus',
            '-metadata:s:s:0',
            'language=eng',
            '-metadata:s:s:1',
            'language=rus',
            '-disposition:a:0',
            '0',
            '-disposition:a:1',
            '0',
            '-disposition:s:0',
            '0',
            '-disposition:s:1',
            '0',
            '-y',
            str(output_path),
        ]
    )
    return tuple(argv)


def _parse_track_ids(info_output: str) -> tuple[tuple[int, str], ...]:
    """Parse persisted track identifiers and media types in order."""
    tracks: list[tuple[int, str]] = []
    current_id: int | None = None
    for raw_line in info_output.splitlines():
        line = raw_line.strip()
        track_match = _MP4BOX_TRACK_RE.match(line)
        if track_match is not None:
            current_id = int(track_match.group(1))
            continue
        type_match = _MP4BOX_MEDIA_TYPE_RE.match(line)
        if type_match is not None and current_id is not None:
            tracks.append((current_id, type_match.group(1)))
            current_id = None
    return tuple(tracks)


def _resolve_track_ids(
    mp4box: Path, candidate: Path, timeout_seconds: float
) -> TrackIds:
    """Read actual track identifiers from the assembled candidate."""
    result = checked_run_strict(
        [str(mp4box), '-info', str(candidate)], timeout_seconds
    )
    tracks = _parse_track_ids(result.stdout + result.stderr)
    types = [media_type for _, media_type in tracks]
    if tuple(types) != _EXPECTED_MEDIA_TYPES:
        raise MuxError(
            'persisted candidate track layout'
            f' {types!r} does not match the five stream profile;'
            ' refusing to apply track flags blindly',
            kind='track-flags',
        )
    return TrackIds(
        video=tracks[0][0],
        original_audio=tracks[1][0],
        narration=tracks[2][0],
        english_subtitles=tracks[3][0],
        russian_subtitles=tracks[4][0],
    )


def _profile_operations(profile: TrackProfile, ids: TrackIds) -> list[str]:
    """Build MP4Box flag operations from the actual track identifiers."""
    operations: list[str] = []
    for enable, track_id in (
        (profile.original_default, ids.original_audio),
        (profile.narration_default, ids.narration),
        (profile.subtitles_default, ids.english_subtitles),
        (profile.subtitles_default, ids.russian_subtitles),
    ):
        operations.extend(['-enable' if enable else '-disable', str(track_id)])
    return operations


def _apply_track_flags(
    mp4box: Path,
    source: Path,
    output: Path,
    profile: TrackProfile,
    ids: TrackIds,
    timeout_seconds: float,
) -> tuple[str, ...]:
    """Apply profile flags by track id into the owned output file."""
    argv = (
        str(mp4box),
        *_profile_operations(profile, ids),
        str(source),
        '-out',
        str(output),
    )
    checked_run_strict(argv, timeout_seconds)
    return argv


def _owned_temp(destination: Path, marker: str) -> Path:
    """Create one owned empty mp4 temporary beside the destination.

    The mp4 suffix is required because FFmpeg selects its muxer from
    the output file extension and the MP4Box writer expects it too.
    """
    fd, name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f'.{destination.name}.{marker}-',
        suffix='.mp4',
    )
    os.close(fd)
    return Path(name)


def _remove_owned(path: Path | None) -> None:
    """Remove one owned temporary file when it still exists."""
    if path is not None:
        path.unlink(missing_ok=True)


def _deliver_no_clobber(staged: Path, destination: Path) -> None:
    """Publish the staged candidate without overwriting anything.

    The hard link is the atomic no-clobber handoff: it succeeds only
    when the destination is still absent, so a file created by a racer
    or a symlink placed at the destination stays untouched and the
    build fails instead of replacing foreign bytes.
    """
    try:
        os.link(staged, destination)
    except FileExistsError as error:
        raise MuxError(
            'the staging destination appeared during assembly; it is'
            f' preserved untouched and the candidate was not delivered:'
            f' {destination}',
            kind='destination',
        ) from error
    except OSError as error:
        raise MuxError(
            f'could not deliver the candidate to {destination}: {error}',
            kind='destination',
        ) from error


def build_candidate(request: MuxRequest, settings: Settings) -> MuxResult:
    """Assemble one fresh staging candidate and persist its flags.

    The video stream and original audio stream are copied without
    re-encoding, narration audio is copied as prepared, both subtitle
    files become mov_text tracks, and startup flags are applied through
    actual MP4Box track identifiers. The candidate is built in owned
    temporary files and delivered with an atomic no-clobber link, so
    inputs, existing destinations and foreign files created during
    assembly stay untouched; only owned staging files are cleaned.
    """
    _validate_profile(request.profile)
    _validate_paths(request)
    ffmpeg_temp: Path | None = None
    mp4box_temp: Path | None = None
    try:
        ffmpeg = discover_tool('ffmpeg', settings.ffmpeg_path)
        ffprobe = discover_tool('ffprobe', settings.ffprobe_path)
        mp4box = discover_tool('mp4box', settings.mp4box_path)
        timeout_seconds = settings.subprocess_timeout_seconds
        streams = _probe_input_streams(
            ffprobe, request.video_path, timeout_seconds
        )
        video_map, original_map, narration_input = _input_layout(
            request, streams
        )
        ffmpeg_temp = _owned_temp(request.destination, 'ffmpeg')
        mp4box_temp = _owned_temp(request.destination, 'mp4box')
        ffmpeg_argv = _ffmpeg_argv(
            request,
            ffmpeg,
            video_map,
            original_map,
            narration_input,
            ffmpeg_temp,
        )
        checked_run_strict(ffmpeg_argv, timeout_seconds)
        ids = _resolve_track_ids(mp4box, ffmpeg_temp, timeout_seconds)
        mp4box_argv = _apply_track_flags(
            mp4box,
            ffmpeg_temp,
            mp4box_temp,
            request.profile,
            ids,
            timeout_seconds,
        )
        _deliver_no_clobber(mp4box_temp, request.destination)
    except BaseException:
        _remove_owned(ffmpeg_temp)
        _remove_owned(mp4box_temp)
        raise
    _remove_owned(ffmpeg_temp)
    _remove_owned(mp4box_temp)
    return MuxResult(
        destination=request.destination,
        ffmpeg_argv=ffmpeg_argv,
        mp4box_argv=mp4box_argv,
        track_ids=ids,
    )
