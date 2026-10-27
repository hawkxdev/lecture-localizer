"""Strict translation import and complete input binding validation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

SUPPORTED_TRANSLATION_VERSION = 1
SUPPORTED_JOB_VERSION = 1


class ContractError(ValueError):
    """Input data violates the approved translation or job contract."""


@dataclass(frozen=True)
class TranslationSegment:
    """One reviewed translation segment bound to source cues."""

    id: str
    source_cues: tuple[int, ...]
    translation: str
    narration: str | None

    @property
    def resolved_narration(self) -> str:
        """Return the narration text or the full translation text."""
        return (
            self.narration if self.narration is not None else self.translation
        )


@dataclass(frozen=True)
class TranslationDocument:
    """Validated translation input with its exact source bytes."""

    schema_version: int
    video_sha256: str
    srt_sha256: str
    segments: tuple[TranslationSegment, ...]
    raw_bytes: bytes


def _build_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build a mapping while rejecting duplicate JSON keys."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError(f'duplicate JSON key: {key!r}')
        result[key] = value
    return result


def _reject_constant(name: str) -> Any:
    """Reject NaN and Infinity style JSON constants."""
    raise ContractError(f'nonstandard JSON constant: {name}')


def strict_json_loads(data: bytes | str) -> Any:
    """Parse JSON text with duplicate keys and constants rejected."""
    if isinstance(data, bytes):
        try:
            data = data.decode('utf-8')
        except UnicodeDecodeError as error:
            raise ContractError('input is not valid UTF-8') from error
    try:
        return json.loads(
            data,
            object_pairs_hook=_build_object,
            parse_constant=_reject_constant,
        )
    except ContractError:
        raise
    except ValueError as error:
        raise ContractError(f'input is not valid JSON: {error}') from error


def load_strict_json_file(path: str | Path) -> Any:
    """Read and strictly parse one JSON file."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        raise
    return strict_json_loads(data)


def load_schema(name: str) -> dict[str, Any]:
    """Load one packaged Draft 2020 12 schema resource."""
    resource = resources.files('lecture_localizer') / 'schemas' / name
    loaded = json.loads(resource.read_text(encoding='utf-8'))
    if not isinstance(loaded, dict):
        raise ContractError(f'packaged schema {name} is not an object')
    return loaded


def validate_instance(
    instance: Any, schema: dict[str, Any], label: str
) -> None:
    """Validate a document against a schema and report the first failure."""
    errors = sorted(
        Draft202012Validator(schema).iter_errors(instance),
        key=lambda error: [str(part) for part in error.absolute_path],
    )
    if errors:
        first = errors[0]
        location = '/'.join(str(part) for part in first.absolute_path)
        place = location if location else 'document root'
        raise ContractError(
            f'{label} fails schema validation at {place}: {first.message}'
        )


TRANSLATION_SCHEMA = load_schema('translation-v1.schema.json')
JOB_SCHEMA = load_schema('job-v1.schema.json')


def _require_nonblank(value: str, field: str, segment_id: str) -> None:
    """Reject whitespace only text fields."""
    if not value.strip():
        raise ContractError(
            f'segment {segment_id!r} field {field} must not be blank'
        )


def _normalize_cue(value: Any) -> int:
    """Normalize one schema valid cue number to an integer.

    JSON Schema integer semantics accept 1.0 and 1 as the same value,
    so integral floats are normalized here; fractional values never
    reach this point because the schema already rejected them.
    """
    if isinstance(value, float):
        return int(value)
    return int(value)


def load_translation_document(path: str | Path) -> TranslationDocument:
    """Strictly parse and validate translation input semantically."""
    raw_bytes = Path(path).read_bytes()
    data = strict_json_loads(raw_bytes)
    validate_instance(data, TRANSLATION_SCHEMA, 'translation')
    segments: list[TranslationSegment] = []
    seen_ids: set[str] = set()
    for raw_segment in data['segments']:
        segment_id = raw_segment['id']
        _require_nonblank(segment_id, 'id', segment_id)
        _require_nonblank(
            raw_segment['translation'], 'translation', segment_id
        )
        narration = raw_segment.get('narration')
        if narration is not None:
            _require_nonblank(narration, 'narration', segment_id)
        if segment_id in seen_ids:
            raise ContractError(f'duplicate segment id: {segment_id!r}')
        seen_ids.add(segment_id)
        segments.append(
            TranslationSegment(
                id=segment_id,
                source_cues=tuple(
                    _normalize_cue(value)
                    for value in raw_segment['source_cues']
                ),
                translation=raw_segment['translation'],
                narration=narration,
            )
        )
    return TranslationDocument(
        schema_version=data['schema_version'],
        video_sha256=data['source']['video_sha256'],
        srt_sha256=data['source']['srt_sha256'],
        segments=tuple(segments),
        raw_bytes=raw_bytes,
    )


def verify_binding(
    document: TranslationDocument, video_sha256: str, srt_sha256: str
) -> None:
    """Verify the translation source hashes match the supplied inputs.

    Digest spelling is compared case insensitively because uppercase and
    lowercase spell the same hexadecimal value; malformed digests are
    already rejected by the schema.
    """
    if document.video_sha256.lower() != video_sha256.lower():
        raise ContractError(
            'translation source binding mismatch: translation is bound to '
            f'video {document.video_sha256} but the supplied video hashes '
            f'to {video_sha256}; supply a translation bound to the current '
            'inputs'
        )
    if document.srt_sha256.lower() != srt_sha256.lower():
        raise ContractError(
            'translation source binding mismatch: translation is bound to '
            f'srt {document.srt_sha256} but the supplied SRT hashes to '
            f'{srt_sha256}; supply a translation bound to the current inputs'
        )


def load_job_manifest(path: str | Path) -> dict[str, Any]:
    """Strictly parse and schema check one job manifest."""
    data = load_strict_json_file(path)
    if (
        isinstance(data, dict)
        and data.get('schema_version') != SUPPORTED_JOB_VERSION
    ):
        version = data.get('schema_version')
        raise ContractError(
            f'unsupported job manifest schema_version {version!r}; this '
            f'version understands only {SUPPORTED_JOB_VERSION}'
        )
    validate_instance(data, JOB_SCHEMA, 'job manifest')
    manifest: dict[str, Any] = data
    return manifest
