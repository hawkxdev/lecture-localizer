"""Ordinal SRT parsing, timing validation and segment window derivation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

_TIMESTAMP_RE = re.compile(r'^\s*(\d{2,}):(\d{2}):(\d{2})[,.](\d{3})\s*$')
_TIMING_SEPARATOR = ' --> '
_MS_PER_SECOND = 1000
_MS_PER_MINUTE = 60 * _MS_PER_SECOND
_MS_PER_HOUR = 60 * _MS_PER_MINUTE


class SegmentRef(Protocol):
    """Minimal read view of one translation segment for windowing."""

    @property
    def id(self) -> str:
        """Return the stable segment identifier."""
        ...

    @property
    def source_cues(self) -> tuple[int, ...]:
        """Return the ordered source cue positions."""
        ...


class SubtitlesError(ValueError):
    """Source subtitle input is malformed or inconsistent."""


@dataclass(frozen=True)
class SourceCue:
    """One source subtitle cue identified by its ordinal position."""

    position: int
    start_ms: int
    end_ms: int
    text: str


@dataclass(frozen=True)
class SegmentWindow:
    """Positive time window derived from one translation segment."""

    start_ms: int
    end_ms: int

    @property
    def duration_ms(self) -> int:
        """Return the positive window length in milliseconds."""
        return self.end_ms - self.start_ms


def _parse_timestamp(raw: str, line: str) -> int:
    """Parse one SRT timestamp into integer milliseconds."""
    match = _TIMESTAMP_RE.match(raw)
    if match is None:
        raise SubtitlesError(
            f'malformed timestamp {raw!r} in timing line: {line!r}'
        )
    hours, minutes, seconds, millis = (int(part) for part in match.groups())
    if minutes > 59 or seconds > 59:
        raise SubtitlesError(
            f'out of range timestamp {raw!r} in timing line: {line!r}'
        )
    return (
        hours * _MS_PER_HOUR
        + minutes * _MS_PER_MINUTE
        + seconds * _MS_PER_SECOND
        + millis
    )


def _parse_timing_line(line: str) -> tuple[int, int]:
    """Parse one cue timing line into start and end milliseconds."""
    start_raw, separator, end_raw = line.partition(_TIMING_SEPARATOR)
    if not separator:
        raise SubtitlesError(f'malformed timing line: {line!r}')
    start_ms = _parse_timestamp(start_raw, line)
    end_ms = _parse_timestamp(end_raw, line)
    if end_ms <= start_ms:
        raise SubtitlesError(
            f'cue end must be after its start in timing line: {line!r}'
        )
    return start_ms, end_ms


def _parse_blocks(text: str) -> list[list[str]]:
    """Split subtitle text into nonempty blocks of lines."""
    blocks: list[list[str]] = []
    current: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip('\r').rstrip()
        if not line.strip():
            if current:
                blocks.append(current)
                current = []
            continue
        current.append(line)
    if current:
        blocks.append(current)
    return blocks


def parse_srt(text: str) -> tuple[SourceCue, ...]:
    """Parse SRT text into cues numbered by ordinal position.

    Printed cue labels are never used as identity; the position of a
    cue inside the file defines its source cue number from one.
    """
    cues: list[SourceCue] = []
    previous_start: int | None = None
    for block in _parse_blocks(text):
        timing_index: int | None = None
        for index, line in enumerate(block):
            if _TIMING_SEPARATOR in line:
                timing_index = index
                break
        if timing_index is None:
            raise SubtitlesError(
                f'cue block without a timing line: {block[:1]!r}'
            )
        if timing_index > 1:
            raise SubtitlesError(
                'cue block has more than one line before its timing line:'
                f' {block[:timing_index]!r}'
            )
        text_lines = block[timing_index + 1 :]
        if not text_lines:
            raise SubtitlesError(
                f'cue block has empty text at timing line: '
                f'{block[timing_index]!r}'
            )
        start_ms, end_ms = _parse_timing_line(block[timing_index])
        if previous_start is not None and start_ms < previous_start:
            raise SubtitlesError(
                f'cue timings are out of order at timing line: '
                f'{block[timing_index]!r}'
            )
        previous_start = start_ms
        position = len(cues) + 1
        cues.append(
            SourceCue(
                position=position,
                start_ms=start_ms,
                end_ms=end_ms,
                text='\n'.join(text_lines),
            )
        )
    if not cues:
        raise SubtitlesError('source subtitles contain no cues')
    return tuple(cues)


def _validate_cue_numbers(
    segments: tuple[SegmentRef, ...], cue_count: int
) -> None:
    """Validate ordered consecutive cue references for every segment."""
    seen: set[int] = set()
    previous_first: int | None = None
    for segment in segments:
        numbers = segment.source_cues
        if len(numbers) != len(set(numbers)):
            raise SubtitlesError(
                f'segment {segment.id!r} repeats a source cue reference'
            )
        expected = list(range(numbers[0], numbers[0] + len(numbers)))
        if list(numbers) != expected:
            raise SubtitlesError(
                f'segment {segment.id!r} source cues must be consecutive'
                f' ascending positive integers: {numbers!r}'
            )
        if previous_first is not None and numbers[0] <= previous_first:
            raise SubtitlesError(
                f'segment {segment.id!r} starts before the previous segment'
            )
        previous_first = numbers[0]
        for number in numbers:
            if number > cue_count:
                raise SubtitlesError(
                    f'segment {segment.id!r} references source cue'
                    f' {number} beyond the {cue_count} parsed cues'
                )
            if number in seen:
                raise SubtitlesError(
                    f'source cue {number} is covered more than once'
                )
            seen.add(number)
    missing = [
        str(number) for number in range(1, cue_count + 1) if number not in seen
    ]
    if missing:
        raise SubtitlesError(
            f'source cues are not covered completely, missing: '
            f'{", ".join(missing)}'
        )


def derive_windows(
    cues: tuple[SourceCue, ...],
    segments: tuple[SegmentRef, ...],
) -> dict[str, SegmentWindow]:
    """Derive positive nonoverlapping windows for ordered segments."""
    _validate_cue_numbers(segments, len(cues))
    by_position = {cue.position: cue for cue in cues}
    windows: dict[str, SegmentWindow] = {}
    previous_end: int | None = None
    previous_id: str | None = None
    for segment in segments:
        group = [by_position[number] for number in segment.source_cues]
        start_ms = min(cue.start_ms for cue in group)
        end_ms = max(cue.end_ms for cue in group)
        if previous_end is not None and start_ms < previous_end:
            raise SubtitlesError(
                f'segment {segment.id!r} window overlaps segment'
                f' {previous_id!r}'
            )
        windows[segment.id] = SegmentWindow(start_ms, end_ms)
        previous_end = end_ms
        previous_id = segment.id
    return windows
