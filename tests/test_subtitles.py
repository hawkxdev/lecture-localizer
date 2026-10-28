"""Tests for ordinal SRT parsing and segment window derivation."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import DEFAULT_CUES, write_srt
from lecture_localizer.contracts import TranslationSegment
from lecture_localizer.subtitles import (
    SegmentWindow,
    SubtitleEntry,
    SubtitlesError,
    derive_windows,
    parse_srt,
    render_srt,
    russian_srt,
    russian_subtitle_entries,
)


def _segments(*specs: tuple[str, list[int]]) -> tuple[TranslationSegment, ...]:
    """Build translation segment refs from id and cue lists."""
    return tuple(
        TranslationSegment(
            id=segment_id,
            source_cues=tuple(cues),
            translation='т',
            narration=None,
        )
        for segment_id, cues in specs
    )


def test_parse_srt_numbers_cues_by_position_not_label(tmp_path: Path) -> None:
    path = tmp_path / 'scrambled.srt'
    path.write_text(
        '9\n00:00:00,000 --> 00:00:01,000\nFirst line.\n\n'
        '9\n00:00:01,000 --> 00:00:02,000\nSecond line.\n\n'
        'not-a-number\n00:00:02,000 --> 00:00:03,000\nThird line.\n',
        encoding='utf-8',
    )
    cues = parse_srt(path.read_text(encoding='utf-8'))
    assert [cue.position for cue in cues] == [1, 2, 3]
    assert [cue.text for cue in cues] == [
        'First line.',
        'Second line.',
        'Third line.',
    ]
    assert cues[0].start_ms == 0 and cues[0].end_ms == 1000


def test_parse_srt_accepts_missing_labels_and_dot_millis(
    tmp_path: Path,
) -> None:
    text = (
        '00:00:00.500 --> 00:00:01.250\nNo label here.\n\n'
        '00:00:01.250 --> 00:00:02.000\nSecond.\n'
    )
    cues = parse_srt(text)
    assert [cue.position for cue in cues] == [1, 2]
    assert cues[0].start_ms == 500 and cues[0].end_ms == 1250


def test_parse_srt_keeps_multiline_text_and_crlf(tmp_path: Path) -> None:
    text = (
        '1\r\n00:00:00,000 --> 00:00:01,000\r\nFirst line.\r\nSecond line.\r\n'
    )
    cues = parse_srt(text)
    assert cues[0].text == 'First line.\nSecond line.'


def test_parse_srt_rejects_blocks_without_timing(tmp_path: Path) -> None:
    with pytest.raises(SubtitlesError, match='without a timing line'):
        parse_srt('1\njust some text\n')


def test_parse_srt_rejects_malformed_timestamp(tmp_path: Path) -> None:
    with pytest.raises(SubtitlesError, match='malformed timestamp'):
        parse_srt('1\n00:00:00,00 --> 00:00:01,000\nText.\n')


def test_parse_srt_rejects_end_before_start(tmp_path: Path) -> None:
    with pytest.raises(SubtitlesError, match='after its start'):
        parse_srt('1\n00:00:02,000 --> 00:00:01,000\nText.\n')


def test_parse_srt_rejects_out_of_order_timings(tmp_path: Path) -> None:
    text = (
        '1\n00:00:01,000 --> 00:00:02,000\nLater start first.\n\n'
        '2\n00:00:00,500 --> 00:00:01,500\nEarlier start second.\n'
    )
    with pytest.raises(SubtitlesError, match='out of order'):
        parse_srt(text)


def test_parse_srt_rejects_empty_text(tmp_path: Path) -> None:
    with pytest.raises(SubtitlesError, match='empty text'):
        parse_srt('1\n00:00:00,000 --> 00:00:01,000\n\n\n2\n')


def test_parse_srt_rejects_more_than_one_label_line(tmp_path: Path) -> None:
    with pytest.raises(SubtitlesError, match='more than one line before'):
        parse_srt('1\nextra\n00:00:00,000 --> 00:00:01,000\nText.\n')


def test_parse_srt_rejects_empty_input(tmp_path: Path) -> None:
    with pytest.raises(SubtitlesError, match='no cues'):
        parse_srt('\n\n')


def test_derive_windows_groups_and_orders(tmp_path: Path) -> None:
    path = tmp_path / 'lec.srt'
    write_srt(
        path,
        [
            (0, 1000, 'One.'),
            (2000, 3000, 'Two.'),
            (3000, 5000, 'Three.'),
        ],
    )
    cues = parse_srt(path.read_text(encoding='utf-8'))
    windows = derive_windows(cues, _segments(('s001', [1, 2]), ('s002', [3])))
    assert windows['s001'] == SegmentWindow(start_ms=0, end_ms=3000)
    assert windows['s002'] == SegmentWindow(start_ms=3000, end_ms=5000)


def test_derive_windows_rejects_overlap_and_names_segments(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'lec.srt'
    write_srt(
        path,
        [
            (0, 1000, 'One.'),
            (500, 2000, 'Two.'),
        ],
    )
    cues = parse_srt(path.read_text(encoding='utf-8'))
    with pytest.raises(SubtitlesError, match="overlaps segment 's001'"):
        derive_windows(cues, _segments(('s001', [1]), ('s002', [2])))


def test_derive_windows_accepts_touching_windows(tmp_path: Path) -> None:
    path = tmp_path / 'lec.srt'
    write_srt(path, DEFAULT_CUES)
    cues = parse_srt(path.read_text(encoding='utf-8'))
    windows = derive_windows(cues, _segments(('s001', [1]), ('s002', [2])))
    assert windows['s002'].start_ms == windows['s001'].end_ms


def test_derive_windows_rejects_missing_coverage(tmp_path: Path) -> None:
    path = tmp_path / 'lec.srt'
    write_srt(path, DEFAULT_CUES)
    cues = parse_srt(path.read_text(encoding='utf-8'))
    with pytest.raises(SubtitlesError, match='missing: 1'):
        derive_windows(cues, _segments(('s001', [2])))


def test_derive_windows_rejects_repeated_cue_in_one_list(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'lec.srt'
    write_srt(path, DEFAULT_CUES)
    cues = parse_srt(path.read_text(encoding='utf-8'))
    with pytest.raises(SubtitlesError, match='repeats a source cue'):
        derive_windows(cues, _segments(('s001', [1, 1])))


def test_derive_windows_rejects_out_of_order_segments(tmp_path: Path) -> None:
    path = tmp_path / 'lec.srt'
    write_srt(path, DEFAULT_CUES)
    cues = parse_srt(path.read_text(encoding='utf-8'))
    with pytest.raises(SubtitlesError, match='starts before'):
        derive_windows(cues, _segments(('s001', [2]), ('s002', [1])))


def test_derive_windows_rejects_non_consecutive_cue_lists(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'lec.srt'
    write_srt(path, DEFAULT_CUES)
    cues = parse_srt(path.read_text(encoding='utf-8'))
    with pytest.raises(SubtitlesError, match='consecutive'):
        derive_windows(cues, _segments(('s001', [1, 3])))


def test_derive_windows_rejects_cues_beyond_count(tmp_path: Path) -> None:
    path = tmp_path / 'lec.srt'
    write_srt(path, DEFAULT_CUES)
    cues = parse_srt(path.read_text(encoding='utf-8'))
    with pytest.raises(SubtitlesError, match='beyond the 2 parsed cues'):
        derive_windows(cues, _segments(('s001', [1, 2, 3])))


def _translated_segments(
    *specs: tuple[str, list[int], str, str | None],
) -> tuple[TranslationSegment, ...]:
    """Build translated segment refs from id, cues, text, narration."""
    return tuple(
        TranslationSegment(
            id=segment_id,
            source_cues=tuple(cues),
            translation=translation,
            narration=narration,
        )
        for segment_id, cues, translation, narration in specs
    )


def test_render_srt_numbers_and_formats_entries() -> None:
    entries = [
        SubtitleEntry(
            start_ms=0,
            end_ms=1500,
            text='Первый полный перевод реплики.',
        ),
        SubtitleEntry(
            start_ms=3_661_500,
            end_ms=3_662_000,
            text='Вторая реплика перевода целиком.',
        ),
    ]
    assert render_srt(entries) == (
        '1\n00:00:00,000 --> 00:00:01,500\n'
        'Первый полный перевод реплики.\n\n'
        '2\n01:01:01,500 --> 01:01:02,000\n'
        'Вторая реплика перевода целиком.\n'
    )


def test_render_srt_round_trips_through_parse_srt() -> None:
    text = '\n'.join(['Первая строка перевода.', 'Вторая строка текста.'])
    entries = [SubtitleEntry(start_ms=500, end_ms=1250, text=text)]
    cues = parse_srt(render_srt(entries))
    assert [(cue.start_ms, cue.end_ms, cue.text) for cue in cues] == [
        (500, 1250, text)
    ]


def test_render_srt_rejects_nonpositive_window() -> None:
    with pytest.raises(SubtitlesError, match='after its start'):
        render_srt(
            [SubtitleEntry(start_ms=1000, end_ms=1000, text='Текст проверки.')]
        )


def test_render_srt_rejects_blank_text() -> None:
    with pytest.raises(SubtitlesError, match='blank text'):
        render_srt([SubtitleEntry(start_ms=0, end_ms=1000, text='   ')])


def test_russian_entries_use_full_translation_on_windows(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'lec.srt'
    write_srt(
        path,
        DEFAULT_CUES,
    )
    cues = parse_srt(path.read_text(encoding='utf-8'))
    segments = _translated_segments(
        ('s001', [1], 'Первый полный перевод.', 'Иная начитка.'),
        ('s002', [2], 'Второй полный перевод.', None),
    )
    entries = russian_subtitle_entries(cues, segments)
    assert entries == (
        SubtitleEntry(start_ms=0, end_ms=1000, text='Первый полный перевод.'),
        SubtitleEntry(
            start_ms=1000, end_ms=2000, text='Второй полный перевод.'
        ),
    )


def test_russian_entries_never_substitute_narration(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'lec.srt'
    write_srt(path, DEFAULT_CUES)
    cues = parse_srt(path.read_text(encoding='utf-8'))
    segments = _translated_segments(
        (
            's001',
            [1, 2],
            'Полный перевод обеих реплик целиком.',
            'Сокращённая начитка для речи.',
        ),
    )
    text = russian_srt(cues, segments)
    assert 'Полный перевод обеих реплик целиком.' in text
    assert 'Сокращённая начитка для речи.' not in text


def test_russian_entries_propagate_window_overlap(tmp_path: Path) -> None:
    path = tmp_path / 'lec.srt'
    write_srt(path, [(0, 1000, 'One.'), (500, 2000, 'Two.')])
    cues = parse_srt(path.read_text(encoding='utf-8'))
    segments = _translated_segments(
        ('s001', [1], 'Первый полный перевод.', None),
        ('s002', [2], 'Второй полный перевод.', None),
    )
    with pytest.raises(SubtitlesError, match='overlaps'):
        russian_subtitle_entries(cues, segments)
