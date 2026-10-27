"""Tests for strict translation import and job manifest loading."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from conftest import (
    default_segments,
    translation_payload,
    write_translation,
)
from lecture_localizer.contracts import (
    ContractError,
    TranslationDocument,
    load_job_manifest,
    load_strict_json_file,
    load_translation_document,
    strict_json_loads,
    verify_binding,
)
from lecture_localizer.storage import sha256_bytes


def _document(path: Path) -> TranslationDocument:
    """Load one translation document for assertion helpers."""
    return load_translation_document(path)


def test_strict_parse_rejects_duplicate_keys() -> None:
    with pytest.raises(ContractError, match='duplicate JSON key'):
        strict_json_loads('{"a": 1, "a": 2}')


def test_strict_parse_rejects_nested_duplicate_keys() -> None:
    with pytest.raises(ContractError, match='duplicate JSON key'):
        strict_json_loads('{"a": {"b": 1, "b": 2}}')


def test_strict_parse_rejects_nonstandard_constants() -> None:
    with pytest.raises(ContractError, match='nonstandard JSON constant'):
        strict_json_loads('{"a": NaN}')


def test_strict_parse_rejects_broken_json() -> None:
    with pytest.raises(ContractError, match='not valid JSON'):
        strict_json_loads('{"a": ')


def test_strict_parse_rejects_non_utf8_bytes() -> None:
    with pytest.raises(ContractError, match='not valid UTF-8'):
        strict_json_loads(b'\xff\xfe{"a": 1}')


def test_packaged_schemas_are_valid_draft_2020_12() -> None:
    from lecture_localizer.contracts import JOB_SCHEMA, TRANSLATION_SCHEMA

    for schema in (TRANSLATION_SCHEMA, JOB_SCHEMA):
        assert schema['$schema'] == (
            'https://json-schema.org/draft/2020-12/schema'
        )
        Draft202012Validator.check_schema(schema)


def test_valid_translation_loads(tmp_path: Path) -> None:
    video = tmp_path / 'v.mp4'
    srt = tmp_path / 's.srt'
    video.write_bytes(b'video')
    srt.write_bytes(b'srt')
    translation = write_translation(
        tmp_path / 't.json', video, srt, default_segments()
    )
    document = _document(translation)
    assert len(document.segments) == 2
    assert document.segments[0].resolved_narration == (
        'Первая реплика для речи.'
    )
    assert document.segments[1].resolved_narration == 'Вторая реплика.'
    assert document.raw_bytes.startswith(b'{')
    assert document.schema_version == 1


@pytest.mark.parametrize(
    'mutate',
    [
        pytest.param(lambda d: d.update(extra_field=True), id='unknown field'),
        pytest.param(lambda d: d.update(schema_version=2), id='version 2'),
        pytest.param(
            lambda d: d.update(schema_version='1'), id='version string'
        ),
        pytest.param(lambda d: d.pop('segments'), id='missing segments'),
        pytest.param(
            lambda d: d['source'].update(video_sha256='short'), id='bad hash'
        ),
        pytest.param(
            lambda d: d['segments'][0].update(translation=None),
            id='null translation',
        ),
        pytest.param(
            lambda d: d['segments'][0].update(source_cues=[0]),
            id='zero cue',
        ),
        pytest.param(
            lambda d: d['segments'][0].update(source_cues=['1']),
            id='string cue',
        ),
        pytest.param(
            lambda d: d.update(source_language='fr'), id='wrong language'
        ),
        pytest.param(lambda d: d['segments'].clear(), id='empty segments'),
    ],
)
def test_translation_schema_rejections(
    tmp_path: Path, mutate: Callable[[dict[str, Any]], None]
) -> None:
    video = tmp_path / 'v.mp4'
    srt = tmp_path / 's.srt'
    video.write_bytes(b'video')
    srt.write_bytes(b'srt')
    payload = translation_payload('a' * 64, 'b' * 64, default_segments())
    mutate(payload)
    path = tmp_path / 'bad.json'
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    with pytest.raises(ContractError, match='fails schema validation'):
        load_translation_document(path)


@pytest.mark.parametrize(
    'mutate',
    [
        pytest.param(
            lambda d: d['segments'].append(dict(d['segments'][0])),
            id='duplicate id',
        ),
        pytest.param(
            lambda d: d['segments'][0].update(id='   '),
            id='blank id',
        ),
        pytest.param(
            lambda d: d['segments'][0].update(translation='  '),
            id='blank translation',
        ),
        pytest.param(
            lambda d: d['segments'][0].update(narration='   '),
            id='blank narration',
        ),
    ],
)
def test_translation_semantic_rejections(
    tmp_path: Path, mutate: Callable[[dict[str, Any]], None]
) -> None:
    payload = translation_payload('a' * 64, 'b' * 64, default_segments())
    mutate(payload)
    path = tmp_path / 'bad.json'
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    with pytest.raises(ContractError):
        load_translation_document(path)


def test_binding_mismatch_names_the_diverged_source(tmp_path: Path) -> None:
    payload = translation_payload('a' * 64, 'b' * 64, default_segments())
    path = tmp_path / 't.json'
    path.write_text(json.dumps(payload), encoding='utf-8')
    document = load_translation_document(path)
    with pytest.raises(ContractError, match='video'):
        verify_binding(document, 'c' * 64, 'b' * 64)
    with pytest.raises(ContractError, match='srt'):
        verify_binding(document, 'a' * 64, 'd' * 64)


def test_binding_success_is_silent(tmp_path: Path) -> None:
    payload = translation_payload('a' * 64, 'b' * 64, default_segments())
    path = tmp_path / 't.json'
    path.write_text(json.dumps(payload), encoding='utf-8')
    document = load_translation_document(path)
    verify_binding(document, 'a' * 64, 'b' * 64)


def test_missing_translation_file_raises_os_error(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        load_translation_document(tmp_path / 'absent.json')


def test_load_strict_json_file_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / 'ok.json'
    path.write_bytes('{"ключ": "значение"}'.encode())
    assert load_strict_json_file(path) == {'ключ': 'значение'}


def test_load_job_manifest_rejects_unknown_version(tmp_path: Path) -> None:
    path = tmp_path / 'manifest.json'
    manifest = {'schema_version': 2}
    path.write_text(json.dumps(manifest), encoding='utf-8')
    with pytest.raises(ContractError, match='unsupported job manifest'):
        load_job_manifest(path)


def test_load_job_manifest_rejects_schema_violations(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'manifest.json'
    manifest = {'schema_version': 1}
    path.write_text(json.dumps(manifest), encoding='utf-8')
    with pytest.raises(ContractError, match='job manifest fails schema'):
        load_job_manifest(path)


def test_manifest_hash_fields_use_hex_digests(tmp_path: Path) -> None:
    digest = sha256_bytes(b'test')
    assert len(digest) == 64


def test_uppercase_digests_are_accepted(tmp_path: Path) -> None:
    payload = translation_payload('A' * 64, 'B' * 64, default_segments())
    path = tmp_path / 'upper.json'
    path.write_text(json.dumps(payload), encoding='utf-8')
    document = load_translation_document(path)
    assert document.video_sha256 == 'A' * 64
    verify_binding(document, 'a' * 64, 'b' * 64)


def test_distinct_literal_ids_are_preserved(tmp_path: Path) -> None:
    segments = [
        {'id': 's1', 'source_cues': [1], 'translation': 'Первая.'},
        {'id': ' s1', 'source_cues': [2], 'translation': 'Вторая.'},
    ]
    payload = translation_payload('a' * 64, 'b' * 64, segments)
    path = tmp_path / 'ids.json'
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    document = load_translation_document(path)
    assert [segment.id for segment in document.segments] == ['s1', ' s1']


def test_true_duplicate_ids_are_rejected(tmp_path: Path) -> None:
    segments = [
        {'id': ' s1', 'source_cues': [1], 'translation': 'Первая.'},
        {'id': ' s1', 'source_cues': [2], 'translation': 'Вторая.'},
    ]
    payload = translation_payload('a' * 64, 'b' * 64, segments)
    path = tmp_path / 'dup.json'
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    with pytest.raises(ContractError, match='duplicate segment id'):
        load_translation_document(path)


def test_integral_float_cue_is_normalized(tmp_path: Path) -> None:
    segments = [
        {'id': 's1', 'source_cues': [1.0], 'translation': 'Первая.'},
        {'id': 's2', 'source_cues': [2.0], 'translation': 'Вторая.'},
    ]
    payload = translation_payload('a' * 64, 'b' * 64, segments)
    path = tmp_path / 'floats.json'
    path.write_text(json.dumps(payload), encoding='utf-8')
    document = load_translation_document(path)
    assert document.segments[0].source_cues == (1,)
    assert document.segments[1].source_cues == (2,)
    assert all(
        type(cue) is int
        for segment in document.segments
        for cue in segment.source_cues
    )


def test_fractional_cue_is_rejected_by_schema(tmp_path: Path) -> None:
    segments = [{'id': 's1', 'source_cues': [1.5], 'translation': 'X.'}]
    payload = translation_payload('a' * 64, 'b' * 64, segments)
    path = tmp_path / 'fractional.json'
    path.write_text(json.dumps(payload), encoding='utf-8')
    with pytest.raises(ContractError, match='fails schema validation'):
        load_translation_document(path)


def test_huge_integer_cue_fails_as_invalid_json(tmp_path: Path) -> None:
    huge_digits = '1' * 5000
    text = (
        '{"schema_version": 1,'
        ' "source": {"video_sha256": "' + 'a' * 64 + '",'
        ' "srt_sha256": "' + 'b' * 64 + '"},'
        ' "source_language": "en", "target_language": "ru",'
        ' "segments": [{"id": "s1", "source_cues": ['
        + huge_digits
        + '], "translation": "X."}]}'
    )
    path = tmp_path / 'huge.json'
    path.write_text(text, encoding='utf-8')
    with pytest.raises(ContractError, match='not valid JSON'):
        strict_json_loads(path.read_text(encoding='utf-8'))
