"""Guarded synchronous xAI speech client with validated responses."""

from __future__ import annotations

import hashlib
import json
import os
import struct
import tempfile
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

from lecture_localizer.storage import move_new_file, sha256_bytes, sha256_file

TTS_URL = 'https://api.x.ai/v1/tts'
KEY_ENV_VAR = 'XAI_API_KEY'
VOICE_ID = 'eve'
LANGUAGE = 'ru'
SPEED = 1.0
WITH_TIMESTAMPS = False
WAV_CODEC = 'wav'
REQUIRED_SAMPLE_RATE_HZ = 48000
WAVE_FORMAT_PCM = 1
WAVE_FORMAT_EXTENSIBLE = 0xFFFE
PCM_SUBFORMAT_GUID = bytes.fromhex('0100000000001000800000aa00389b71')
SUPPORTED_BITS_PER_SAMPLE = frozenset({8, 16, 24, 32})
MAX_TEXT_CHARS = 60000
STREAMED_RIFF_SIZE = 0x80000023
STREAMED_DATA_SIZE = 0x7FFFFFFF
PCM_HEADER_BYTES = 44
PCM_FRAME_BYTES = 2
WAV_COPY_BYTES = 1 << 20
DEFAULT_CONNECT_TIMEOUT_SECONDS = 10.0
DEFAULT_READ_TIMEOUT_SECONDS = 120.0
DEFAULT_WRITE_TIMEOUT_SECONDS = 30.0
DEFAULT_POOL_TIMEOUT_SECONDS = 10.0
_FAILURE_EXCERPT_BYTES = 256


class SpeechRequestError(ValueError):
    """The approved speech request itself is invalid."""


@dataclass(frozen=True)
class PhaseTimeouts:
    """Finite phase inactivity limits for one speech request.

    These bound connect, write, read and pool inactivity phases; they
    are not a guaranteed wall clock limit for the complete response.
    """

    connect_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS
    read_seconds: float = DEFAULT_READ_TIMEOUT_SECONDS
    write_seconds: float = DEFAULT_WRITE_TIMEOUT_SECONDS
    pool_seconds: float = DEFAULT_POOL_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        """Reject non finite or nonpositive phase values."""
        for name in (
            'connect_seconds',
            'read_seconds',
            'write_seconds',
            'pool_seconds',
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise SpeechRequestError(f'{name} must be a number')
            if (
                value <= 0
                or value != value
                or value
                in (
                    float('inf'),
                    float('-inf'),
                )
            ):
                raise SpeechRequestError(f'{name} must be finite and positive')

    def to_httpx(self) -> httpx.Timeout:
        """Return the equivalent httpx phase timeout object."""
        return httpx.Timeout(
            connect=self.connect_seconds,
            read=self.read_seconds,
            write=self.write_seconds,
            pool=self.pool_seconds,
        )


@dataclass(frozen=True)
class WavFacts:
    """Parsed header facts of one validated WAV response."""

    audio_format: int
    channels: int
    sample_rate_hz: int
    bits_per_sample: int
    data_bytes: int

    def to_dict(self) -> dict[str, int]:
        """Return a JSON serializable view."""
        return {
            'audio_format': self.audio_format,
            'channels': self.channels,
            'sample_rate_hz': self.sample_rate_hz,
            'bits_per_sample': self.bits_per_sample,
            'data_bytes': self.data_bytes,
        }


@dataclass(frozen=True)
class SynthesisReceipt:
    """Factual receipt of one complete validated speech response."""

    status_code: int
    content_type: str
    byte_count: int
    sha256: str
    wav: WavFacts


@dataclass(frozen=True)
class FailureEvidence:
    """Safe factual evidence about a failed or uncertain attempt."""

    status_code: int | None
    content_type: str | None
    byte_count: int
    sha256: str
    excerpt: str

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON serializable view."""
        return {
            'status_code': self.status_code,
            'content_type': self.content_type,
            'bytes': self.byte_count,
            'sha256': self.sha256,
            'excerpt': self.excerpt,
        }


class DefinitiveSpeechFailure(RuntimeError):
    """A speech attempt ended with a known non success outcome."""

    kind = 'definitive_failure'

    def __init__(self, detail: str, evidence: FailureEvidence) -> None:
        """Record the failure detail and its safe evidence."""
        super().__init__(detail)
        self.detail = detail
        self.evidence = evidence


class UncertainSpeechFailure(RuntimeError):
    """A speech attempt ended without a known outcome."""

    def __init__(self, kind: str, detail: str) -> None:
        """Record the uncertainty kind and its detail."""
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


class ProviderRejectedRequest(DefinitiveSpeechFailure):
    """The provider returned a complete non success HTTP response."""

    kind = 'http_status'


class UnexpectedContentType(DefinitiveSpeechFailure):
    """A 200 response arrived with an unexpected content type."""

    kind = 'unexpected_content_type'


class InvalidAudioResponse(DefinitiveSpeechFailure):
    """A complete response is not a decodable approved WAV body."""

    kind = 'invalid_audio'


class TransportConnectFailure(DefinitiveSpeechFailure):
    """The connection was never established, so nothing was sent."""

    kind = 'connect_failure'


class TransportUncertainFailure(UncertainSpeechFailure):
    """A transport interruption left the provider outcome unknown."""


class TransportResponse(Protocol):
    """Minimal streamed response surface consumed by the client."""

    @property
    def status_code(self) -> int:
        """Return the HTTP status code."""
        ...

    @property
    def headers(self) -> Mapping[str, str]:
        """Return the response headers."""
        ...

    def iter_bytes(self) -> Iterator[bytes]:
        """Yield the response body in chunks."""
        ...

    def close(self) -> None:
        """Release the underlying connection resources."""
        ...


class Transport(Protocol):
    """Callable transport seam used by tests and the default HTTP path."""

    def __call__(
        self,
        *,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
        timeout: httpx.Timeout,
    ) -> TransportResponse:
        """Perform one POST request and return its streamed response."""
        ...


def build_payload(text: str) -> dict[str, Any]:
    """Return the exact approved request payload for one frozen text."""
    return {
        'text': text,
        'voice_id': VOICE_ID,
        'language': LANGUAGE,
        'speed': SPEED,
        'output_format': {
            'codec': WAV_CODEC,
            'sample_rate': REQUIRED_SAMPLE_RATE_HZ,
        },
        'with_timestamps': WITH_TIMESTAMPS,
    }


def request_signature(text: str) -> str:
    """Hash the exact request payload into a stable signature."""
    canonical = json.dumps(
        build_payload(text),
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    )
    return sha256_bytes(canonical.encode('utf-8'))


def validate_posted_text(text: str) -> None:
    """Reject empty or oversized narration before any paid operation."""
    if not text:
        raise SpeechRequestError('posted text must not be empty')
    if len(text) > MAX_TEXT_CHARS:
        raise SpeechRequestError(
            f'posted text has {len(text)} characters, above the documented'
            f' provider limit of {MAX_TEXT_CHARS}; split or revise the'
            ' reviewed fragment externally, the application never'
            ' shortens or splits reviewed text'
        )


class _HttpxStreamedResponse:
    """Adapter closing the httpx stream and client after consumption."""

    def __init__(self, response: httpx.Response, client: httpx.Client) -> None:
        """Hold one streamed response and its owning client."""
        self._response = response
        self._client = client

    @property
    def status_code(self) -> int:
        """Return the HTTP status code."""
        return self._response.status_code

    @property
    def headers(self) -> Mapping[str, str]:
        """Return the response headers."""
        return self._response.headers

    def iter_bytes(self) -> Iterator[bytes]:
        """Yield body chunks from the httpx stream."""
        return self._response.iter_bytes()

    def close(self) -> None:
        """Close the stream and the underlying client."""
        self._response.close()
        self._client.close()


def httpx_transport(
    *,
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout: httpx.Timeout,
) -> TransportResponse:
    """Send one verified TLS POST request with finite phase timeouts."""
    client = httpx.Client(verify=True, timeout=timeout)
    try:
        request = client.build_request(
            'POST', url, json=payload, headers=headers
        )
        response = client.send(request, stream=True)
    except BaseException:
        client.close()
        raise
    return _HttpxStreamedResponse(response, client)


def _map_transport_error(
    error: httpx.HTTPError,
) -> DefinitiveSpeechFailure | UncertainSpeechFailure:
    """Classify one httpx failure as definitive or uncertain."""
    if isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout)):
        return TransportConnectFailure(
            'the provider connection was never established',
            FailureEvidence(None, None, 0, sha256_bytes(b''), ''),
        )
    if isinstance(error, httpx.PoolTimeout):
        return TransportConnectFailure(
            'no pooled connection became available in time',
            FailureEvidence(None, None, 0, sha256_bytes(b''), ''),
        )
    if isinstance(error, (httpx.WriteError, httpx.WriteTimeout)):
        return TransportUncertainFailure(
            'write_interrupted',
            f'the request write phase was interrupted: {error}',
        )
    if isinstance(error, (httpx.ReadTimeout, httpx.ReadError)):
        return TransportUncertainFailure(
            'inactivity_timeout',
            f'the response read phase exceeded its inactivity limit: {error}',
        )
    if isinstance(error, (httpx.RemoteProtocolError, httpx.DecodingError)):
        return TransportUncertainFailure(
            'partial_response',
            f'the response ended before a complete body arrived: {error}',
        )
    return TransportUncertainFailure(
        'unknown_transport_failure',
        f'the transport failed with an unclassified error: {error}',
    )


def _excerpt_from(path: Path) -> tuple[int, str, str]:
    """Return byte count, sha256 and a short text excerpt of a sink."""
    data = path.read_bytes() if path.exists() else b''
    excerpt = data[:_FAILURE_EXCERPT_BYTES].decode('utf-8', errors='replace')
    return len(data), sha256_bytes(data), excerpt


class SpeechClient:
    """Synchronous guarded client for the approved xAI speech mode."""

    def __init__(
        self,
        api_key: str,
        transport: Transport | None = None,
        timeouts: PhaseTimeouts | None = None,
    ) -> None:
        """Bind the key, an optional test transport and phase timeouts."""
        if not api_key:
            raise SpeechRequestError('the speech API key must not be empty')
        self._api_key = api_key
        self._transport: Transport = transport or httpx_transport
        self._timeouts = timeouts or PhaseTimeouts()

    def _sanitize(self, text: str | None) -> str:
        """Remove the configured credential from rendered diagnostics."""
        if not text:
            return ''
        return text.replace(self._api_key, '[redacted]')

    def synthesize(self, *, text: str, sink: Path) -> SynthesisReceipt:
        """Post one request, stream the body to sink and validate it.

        The complete body is streamed to sink with an incremental
        digest; only a fully received 200 audio/wav body that parses as
        a decodable WAV at the approved sample rate returns a receipt.
        Any other outcome raises a definitive or uncertain typed
        failure while the received bytes remain available in the sink.
        Reflected provider metadata and transport messages are
        sanitized so no rendered diagnostic carries the credential.
        """
        validate_posted_text(text)
        payload = build_payload(text)
        headers = {
            'Authorization': f'Bearer {self._api_key}',
            'Content-Type': 'application/json',
        }
        timeout = self._timeouts.to_httpx()
        received = 0
        digest = hashlib.sha256()
        status: int | None = None
        content_type: str | None = None
        sink.parent.mkdir(parents=True, exist_ok=True)
        response: TransportResponse | None = None
        try:
            with open(sink, 'wb') as handle:
                try:
                    response = self._transport(
                        url=TTS_URL,
                        payload=payload,
                        headers=headers,
                        timeout=timeout,
                    )
                    status = response.status_code
                    content_type = response.headers.get('content-type')
                    for chunk in response.iter_bytes():
                        handle.write(chunk)
                        digest.update(chunk)
                        received += len(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
                finally:
                    if response is not None:
                        response.close()
        except httpx.HTTPError as error:
            error.args = (self._sanitize(str(error)),)
            failure = _map_transport_error(error)
            failure.detail = self._sanitize(failure.detail)
            failure.args = (failure.detail,)
            raise failure from error
        evidence = FailureEvidence(
            status_code=status,
            content_type=self._sanitize(content_type),
            byte_count=received,
            sha256=digest.hexdigest(),
            excerpt='',
        )
        if status != 200:
            _, _, excerpt = _excerpt_from(sink)
            failed = FailureEvidence(
                status_code=status,
                content_type=self._sanitize(content_type),
                byte_count=received,
                sha256=evidence.sha256,
                excerpt=self._sanitize(excerpt),
            )
            raise ProviderRejectedRequest(
                f'the provider answered HTTP {status} instead of 200',
                failed,
            )
        media_type = (content_type or '').split(';')[0].strip().lower()
        if media_type != 'audio/wav':
            _, _, excerpt = _excerpt_from(sink)
            raise UnexpectedContentType(
                'a 200 response arrived with content type'
                f' {self._sanitize(content_type)!r} instead of audio/wav',
                FailureEvidence(
                    status_code=status,
                    content_type=self._sanitize(content_type),
                    byte_count=received,
                    sha256=evidence.sha256,
                    excerpt=self._sanitize(excerpt),
                ),
            )
        try:
            facts = parse_completed_wav(sink)
        except InvalidAudioResponse as error:
            _, _, excerpt = _excerpt_from(sink)
            raise InvalidAudioResponse(
                error.detail,
                FailureEvidence(
                    status_code=status,
                    content_type=self._sanitize(content_type),
                    byte_count=received,
                    sha256=evidence.sha256,
                    excerpt=self._sanitize(excerpt),
                ),
            ) from error
        return SynthesisReceipt(
            status_code=status,
            content_type=media_type,
            byte_count=received,
            sha256=evidence.sha256,
            wav=facts,
        )


def parse_wav_header(path: Path) -> WavFacts:
    """Strictly parse one WAV file header and validate approved facts."""
    try:
        with open(path, 'rb') as handle:
            return _parse_wav_stream(handle, path.stat().st_size)
    except InvalidAudioResponse:
        raise
    except OSError as error:
        raise InvalidAudioResponse(
            f'the response sink could not be read: {error}',
            FailureEvidence(None, None, 0, sha256_bytes(b''), ''),
        ) from error


def _streamed_pcm_header() -> bytes:
    """Describe the supported indeterminate PCM envelope."""
    return struct.pack(
        '<4sI4s4sIHHIIHH4sI',
        b'RIFF',
        STREAMED_RIFF_SIZE,
        b'WAVE',
        b'fmt ',
        16,
        WAVE_FORMAT_PCM,
        1,
        REQUIRED_SAMPLE_RATE_HZ,
        REQUIRED_SAMPLE_RATE_HZ * PCM_FRAME_BYTES,
        PCM_FRAME_BYTES,
        16,
        b'data',
        STREAMED_DATA_SIZE,
    )


def has_streamed_pcm_header(path: Path) -> bool:
    """Identify an envelope without asserting transport completion."""
    with path.open('rb') as handle:
        return handle.read(PCM_HEADER_BYTES) == _streamed_pcm_header()


def parse_completed_wav(path: Path) -> WavFacts:
    """Validate a fully received response without changing its bytes."""
    try:
        return parse_wav_header(path)
    except InvalidAudioResponse as error:
        with path.open('rb') as handle:
            header = handle.read(PCM_HEADER_BYTES)
            size = os.fstat(handle.fileno()).st_size
        if header != _streamed_pcm_header():
            raise
        data_bytes = size - PCM_HEADER_BYTES
        if (
            data_bytes <= 0
            or data_bytes >= STREAMED_DATA_SIZE
            or data_bytes % PCM_FRAME_BYTES
        ):
            raise _invalid(
                'streamed PCM must contain nonempty whole frames'
            ) from error
        return WavFacts(
            WAVE_FORMAT_PCM, 1, REQUIRED_SAMPLE_RATE_HZ, 16, data_bytes
        )


def canonicalize_completed_wav(
    source: Path, destination: Path, *, source_sha256: str
) -> WavFacts:
    """Materialize a distinct canonical WAV with verified source identity."""
    facts = parse_completed_wav(source)
    if source.resolve() == destination.resolve() or destination.is_symlink():
        raise _invalid('canonical WAV destination must be a distinct file')
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=destination.parent, suffix='.wav')
    temporary_path = Path(temporary)
    digest = hashlib.sha256()
    try:
        with os.fdopen(fd, 'wb') as writer, source.open('rb') as reader:
            header = reader.read(PCM_HEADER_BYTES)
            digest.update(header)
            if header == _streamed_pcm_header():
                canonical = bytearray(header)
                struct.pack_into('<I', canonical, 4, facts.data_bytes + 36)
                struct.pack_into('<I', canonical, 40, facts.data_bytes)
                writer.write(canonical)
            else:
                writer.write(header)
            while chunk := reader.read(WAV_COPY_BYTES):
                digest.update(chunk)
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        if digest.hexdigest() != source_sha256:
            raise _invalid('completed response source hash changed')
        canonical_facts = parse_wav_header(temporary_path)
        if canonical_facts != facts:
            raise _invalid('canonical WAV facts disagree with source PCM')
        if destination.exists():
            if sha256_file(destination) != sha256_file(temporary_path):
                raise _invalid('existing canonical WAV has different bytes')
        else:
            move_new_file(temporary_path, destination)
    finally:
        temporary_path.unlink(missing_ok=True)
    return facts


def validate_canonical_wav(
    source: Path, canonical: Path, *, source_sha256: str
) -> WavFacts:
    """Verify the complete derivation without writing either file."""
    facts = parse_completed_wav(source)
    if parse_wav_header(canonical) != facts:
        raise _invalid('canonical WAV facts disagree with source PCM')
    digest = hashlib.sha256()
    with source.open('rb') as reader, canonical.open('rb') as derived:
        header = reader.read(PCM_HEADER_BYTES)
        digest.update(header)
        expected = bytearray(header)
        if header == _streamed_pcm_header():
            struct.pack_into('<I', expected, 4, facts.data_bytes + 36)
            struct.pack_into('<I', expected, 40, facts.data_bytes)
        if derived.read(PCM_HEADER_BYTES) != expected:
            raise _invalid('canonical WAV header disagrees with source')
        while chunk := reader.read(WAV_COPY_BYTES):
            digest.update(chunk)
            if derived.read(len(chunk)) != chunk:
                raise _invalid('canonical WAV does not preserve source PCM')
        if derived.read(1):
            raise _invalid('canonical WAV has unexpected trailing bytes')
    if digest.hexdigest() != source_sha256:
        raise _invalid('completed response source hash changed')
    return facts


def _parse_wav_stream(handle: Any, file_size: int) -> WavFacts:
    """Walk RIFF chunks of one open WAV file and validate the header."""
    head = handle.read(12)
    if len(head) < 12 or head[0:4] != b'RIFF' or head[8:12] != b'WAVE':
        raise _invalid('missing RIFF WAVE header')
    riff_size = struct.unpack('<I', head[4:8])[0]
    fmt: WavFacts | None = None
    data_bytes: int | None = None
    position = 12
    while position + 8 <= file_size:
        handle.seek(position)
        header = handle.read(8)
        if len(header) < 8:
            raise _invalid('truncated chunk header')
        chunk_id = header[0:4]
        chunk_size = struct.unpack('<I', header[4:8])[0]
        body_start = position + 8
        if chunk_id == b'fmt ':
            if chunk_size < 16:
                raise _invalid('fmt chunk shorter than 16 bytes')
            body = handle.read(min(chunk_size, 64))
            if len(body) < 16:
                raise _invalid('truncated fmt chunk')
            (
                audio_format,
                channels,
                rate,
                byte_rate,
                block_align,
                bits,
            ) = struct.unpack('<HHIIHH', body[:16])
            if channels < 1:
                raise _invalid('implausible channel count')
            if bits not in SUPPORTED_BITS_PER_SAMPLE:
                raise _invalid(
                    f'unsupported bits per sample {bits}; expected one'
                    ' of 8, 16, 24 or 32 bit PCM'
                )
            if block_align != channels * (bits // 8):
                raise _invalid(
                    f'block align {block_align} does not match'
                    f' {channels} channels of {bits} bit samples'
                )
            if byte_rate != rate * block_align:
                raise _invalid(
                    f'byte rate {byte_rate} does not match the declared'
                    f' sample rate {rate} and block align {block_align}'
                )
            if audio_format == WAVE_FORMAT_EXTENSIBLE:
                _validate_extensible_pcm(chunk_size, body)
            elif audio_format != WAVE_FORMAT_PCM:
                raise _invalid(
                    f'unsupported WAV format tag {audio_format}; expected'
                    ' PCM or complete extensible PCM'
                )
            fmt = WavFacts(
                audio_format=audio_format,
                channels=channels,
                sample_rate_hz=rate,
                bits_per_sample=bits,
                data_bytes=0,
            )
        elif chunk_id == b'data':
            if fmt is None:
                raise _invalid('data chunk precedes the fmt chunk')
            remaining = file_size - body_start
            if chunk_size > remaining:
                raise _invalid(
                    'declared data size exceeds the received bytes;'
                    ' the response is truncated'
                )
            data_bytes = chunk_size
            break
        position = body_start + chunk_size + (chunk_size % 2)
    if fmt is None:
        raise _invalid('no fmt chunk found')
    if data_bytes is None:
        raise _invalid('no data chunk found')
    if data_bytes == 0:
        raise _invalid(
            'the data chunk is empty, so the response carries no speech'
        )
    if data_bytes % (fmt.channels * (fmt.bits_per_sample // 8)) != 0:
        raise _invalid(
            'declared data size is not a whole number of complete'
            ' sample frames'
        )
    if riff_size is not None and riff_size + 8 > file_size:
        raise _invalid(
            'the declared RIFF size exceeds the received bytes; the'
            ' response is truncated'
        )
    if fmt.sample_rate_hz != REQUIRED_SAMPLE_RATE_HZ:
        raise _invalid(
            f'sample rate {fmt.sample_rate_hz} does not match the'
            f' approved {REQUIRED_SAMPLE_RATE_HZ} mode'
        )
    return WavFacts(
        audio_format=fmt.audio_format,
        channels=fmt.channels,
        sample_rate_hz=fmt.sample_rate_hz,
        bits_per_sample=fmt.bits_per_sample,
        data_bytes=data_bytes,
    )


def _validate_extensible_pcm(chunk_size: int, body: bytes) -> None:
    """Require a complete extensible header naming the PCM subformat.

    A truncated extensible fmt chunk without the cbSize extension and
    the PCM SubFormat GUID is not a decodable WAVE body even though a
    plausible 16 byte base header is present.
    """
    if chunk_size < 40 or len(body) < 40:
        raise _invalid(
            'extensible fmt chunk lacks the required format extension'
        )
    cb_size = struct.unpack('<H', body[16:18])[0]
    if cb_size < 22:
        raise _invalid(
            f'extensible fmt cbSize {cb_size} is shorter than the'
            ' required 22 byte extension'
        )
    subformat = body[24:40]
    if subformat != PCM_SUBFORMAT_GUID:
        raise _invalid('extensible SubFormat GUID is not linear PCM')


def _invalid(detail: str) -> InvalidAudioResponse:
    """Build an invalid audio failure without file evidence."""
    return InvalidAudioResponse(
        detail,
        FailureEvidence(None, None, 0, sha256_bytes(b''), ''),
    )
