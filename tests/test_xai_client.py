"""Tests for the guarded speech client and its validated transport."""

from __future__ import annotations

import hashlib
import struct
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from lecture_localizer import xai_client
from lecture_localizer.xai_client import (
    TTS_URL,
    InvalidAudioResponse,
    PhaseTimeouts,
    ProviderRejectedRequest,
    SpeechClient,
    SpeechRequestError,
    TransportConnectFailure,
    TransportUncertainFailure,
    UnexpectedContentType,
    build_payload,
    request_signature,
    validate_posted_text,
)

SECRET_KEY = 'test-secret-key-do-not-leak'


def build_wav_bytes(
    samples: int = 8,
    channels: int = 1,
    rate: int = 48000,
    bits: int = 16,
    extensible: bool = False,
    extensible_body: bytes = b'',
) -> bytes:
    """Build one minimal valid PCM WAV payload for transport fakes."""
    width = bits // 8
    data = b'\x00' * (samples * channels * width)
    if extensible:
        fmt_body = (
            struct.pack(
                '<HHIIHH',
                0xFFFE,
                channels,
                rate,
                rate * channels * width,
                channels * width,
                bits,
            )
            + extensible_body
        )
    else:
        fmt_body = struct.pack(
            '<HHIIHH',
            1,
            channels,
            rate,
            rate * channels * width,
            channels * width,
            bits,
        )
    body = (
        b'WAVE'
        + b'fmt '
        + struct.pack('<I', len(fmt_body))
        + fmt_body
        + b'data'
        + struct.pack('<I', len(data))
        + data
    )
    return b'RIFF' + struct.pack('<I', len(body)) + body


def extensible_extension(
    cb_size: int = 22,
    subformat: bytes = bytes.fromhex('0100000000001000800000aa00389b71'),
) -> bytes:
    """Build one extensible fmt extension body."""
    return (
        struct.pack('<H', cb_size)
        + struct.pack('<H', 16)
        + struct.pack('<I', 3)
        + subformat
    )


TRUNCATED_EXTENSIBLE_BODY = b''


def truncate_wav(payload: bytes) -> bytes:
    """Cut a WAV payload so its declared data size overruns the file."""
    return payload[:-4]


class FakeResponse:
    """Canned streamed response for the fake transport."""

    def __init__(
        self,
        chunks: tuple[bytes, ...] = (),
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        fail_after: BaseException | None = None,
    ) -> None:
        """Store the scripted body, status, headers and mid body error."""
        self.status_code = status_code
        self.headers = headers or {'content-type': 'audio/wav'}
        self._chunks = chunks
        self._fail_after = fail_after

    def iter_bytes(self) -> Iterator[bytes]:
        """Yield scripted chunks and optionally raise mid body."""
        yielded = False
        for chunk in self._chunks:
            yield chunk
            yielded = True
        if self._fail_after is not None:
            raise self._fail_after
        if not yielded:
            return

    def close(self) -> None:
        """Release nothing; the fake holds no connection."""
        return None


class FakeTransport:
    """Recording transport returning one scripted response or error."""

    def __init__(self, outcome: Any) -> None:
        """Store the response object or the exception instance to raise."""
        self.outcome = outcome
        self.requests: list[dict[str, Any]] = []

    def __call__(
        self,
        *,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
        timeout: httpx.Timeout,
    ) -> Any:
        """Record the request and produce the scripted outcome."""
        self.requests.append(
            {
                'url': url,
                'payload': payload,
                'headers': dict(headers),
                'timeout': timeout,
            }
        )
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def make_client(transport: FakeTransport) -> SpeechClient:
    """Build one client bound to the fake transport and the test key."""
    return SpeechClient(SECRET_KEY, transport=transport)


def test_payload_matches_approved_mode() -> None:
    """The exact request payload equals the frozen approved mode."""
    assert build_payload('текст') == {
        'text': 'текст',
        'voice_id': 'eve',
        'language': 'ru',
        'speed': 1.0,
        'output_format': {'codec': 'wav', 'sample_rate': 48000},
        'with_timestamps': False,
    }


def test_request_signature_binds_the_text() -> None:
    """Distinct texts produce distinct stable signatures."""
    first = request_signature('Первая фраза.')
    second = request_signature('Вторая фраза.')
    assert first != second
    assert first == request_signature('Первая фраза.')
    assert len(first) == 64


def test_empty_and_oversized_text_are_rejected() -> None:
    """Text validation rejects empty and beyond limit payloads."""
    with pytest.raises(SpeechRequestError, match='must not be empty'):
        validate_posted_text('')
    with pytest.raises(SpeechRequestError, match=r'60[0-9]+ characters'):
        validate_posted_text('я' * (xai_client.MAX_TEXT_CHARS + 1))
    validate_posted_text('я' * xai_client.MAX_TEXT_CHARS)


def test_transport_receives_the_exact_request(tmp_path: Path) -> None:
    """The transport sees the approved URL, payload and headers."""
    transport = FakeTransport(FakeResponse(chunks=(build_wav_bytes(),)))
    client = make_client(transport)
    client.synthesize(text='Проверка.', sink=tmp_path / 'sink.tmp')
    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert request['url'] == TTS_URL
    assert request['payload']['text'] == 'Проверка.'
    assert request['headers']['Authorization'] == f'Bearer {SECRET_KEY}'
    assert request['headers']['Content-Type'] == 'application/json'
    timeout = request['timeout']
    assert timeout.connect == 10.0
    assert timeout.read == 120.0
    assert timeout.write == 30.0
    assert timeout.pool == 10.0


def test_complete_response_is_streamed_and_hashed(tmp_path: Path) -> None:
    """A valid response lands in the sink with an exact receipt."""
    wav = build_wav_bytes(samples=16)
    transport = FakeTransport(FakeResponse(chunks=(wav[:10], wav[10:])))
    sink = tmp_path / 'raw.wav'
    client = make_client(transport)
    receipt = client.synthesize(text='Проверка.', sink=sink)
    assert sink.read_bytes() == wav
    assert receipt.status_code == 200
    assert receipt.content_type == 'audio/wav'
    assert receipt.byte_count == len(wav)
    assert receipt.sha256 == hashlib.sha256(wav).hexdigest()
    assert receipt.wav.channels == 1
    assert receipt.wav.sample_rate_hz == 48000
    assert receipt.wav.bits_per_sample == 16
    assert receipt.wav.data_bytes == 16 * 2


def test_non_success_response_is_definitive(tmp_path: Path) -> None:
    """An HTTP error response becomes a definitive provider refusal."""
    transport = FakeTransport(
        FakeResponse(
            chunks=(b'{"error": "quota"}',),
            status_code=429,
            headers={'content-type': 'application/json'},
        )
    )
    sink = tmp_path / 'raw.bin'
    with pytest.raises(ProviderRejectedRequest) as raised:
        make_client(transport).synthesize(text='Проверка.', sink=sink)
    assert raised.value.kind == 'http_status'
    assert raised.value.evidence.status_code == 429
    assert 'quota' in raised.value.evidence.excerpt
    assert sink.read_bytes() == b'{"error": "quota"}'


def test_wrong_content_type_is_definitive(tmp_path: Path) -> None:
    """A 200 body with a non WAV media type is refused definitively."""
    transport = FakeTransport(
        FakeResponse(
            chunks=(build_wav_bytes(),),
            headers={'content-type': 'audio/mpeg'},
        )
    )
    with pytest.raises(UnexpectedContentType) as raised:
        make_client(transport).synthesize(
            text='Проверка.', sink=tmp_path / 'raw.bin'
        )
    assert raised.value.evidence.content_type == 'audio/mpeg'


@pytest.mark.parametrize(
    'payload',
    [
        b'not a wav at all',
        b'RIFF\x00\x00\x00\x00',
        build_wav_bytes()[:-4],
        build_wav_bytes(rate=24000),
    ],
    ids=['garbage', 'short', 'truncated', 'wrong_rate'],
)
def test_invalid_audio_bodies_are_refused(
    tmp_path: Path, payload: bytes
) -> None:
    """Wrong, short, truncated or misrated WAV bodies never pass."""
    transport = FakeTransport(FakeResponse(chunks=(payload,)))
    with pytest.raises(InvalidAudioResponse):
        make_client(transport).synthesize(
            text='Проверка.', sink=tmp_path / 'raw.wav'
        )


@pytest.mark.parametrize(
    'extensible_body',
    [
        b'',
        struct.pack('<H', 0)
        + struct.pack('<H', 16)
        + struct.pack('<I', 3)
        + bytes.fromhex('0100000000001000800000aa00389b71'),
        struct.pack('<H', 22)
        + struct.pack('<H', 16)
        + struct.pack('<I', 3)
        + bytes.fromhex('0300000000001000800000aa00389b71'),
    ],
    ids=[
        'truncated_size16_no_extension',
        'cbSize_zero',
        'non_pcm_subformat',
    ],
)
def test_malformed_extensible_wav_is_refused(
    tmp_path: Path, extensible_body: bytes
) -> None:
    """An undecodable extensible header never yields a receipt."""
    payload = build_wav_bytes(extensible=True, extensible_body=extensible_body)
    transport = FakeTransport(FakeResponse(chunks=(payload,)))
    sink = tmp_path / 'raw.wav'
    with pytest.raises(InvalidAudioResponse):
        make_client(transport).synthesize(text='Проверка.', sink=sink)
    assert sink.read_bytes() == payload


def test_complete_extensible_pcm_wav_is_accepted(tmp_path: Path) -> None:
    """A complete extensible PCM header passes with recorded facts."""
    payload = build_wav_bytes(
        extensible=True, extensible_body=extensible_extension()
    )
    transport = FakeTransport(FakeResponse(chunks=(payload,)))
    receipt = make_client(transport).synthesize(
        text='Проверка.', sink=tmp_path / 'raw.wav'
    )
    assert receipt.wav.audio_format == 0xFFFE
    assert receipt.wav.sample_rate_hz == 48000
    assert receipt.wav.data_bytes == 16


def test_read_timeout_is_uncertain(tmp_path: Path) -> None:
    """An inactivity timeout leaves the provider outcome unknown."""
    transport = FakeTransport(httpx.ReadTimeout('peer went quiet'))
    with pytest.raises(TransportUncertainFailure) as raised:
        make_client(transport).synthesize(
            text='Проверка.', sink=tmp_path / 'raw.bin'
        )
    assert raised.value.kind == 'inactivity_timeout'


def test_connect_failure_is_definitive(tmp_path: Path) -> None:
    """A connection that never formed is a definitive failure."""
    transport = FakeTransport(httpx.ConnectError('refused'))
    with pytest.raises(TransportConnectFailure):
        make_client(transport).synthesize(
            text='Проверка.', sink=tmp_path / 'raw.bin'
        )


def test_partial_body_is_uncertain(tmp_path: Path) -> None:
    """A body cut mid stream stays visibly uncertain with evidence."""
    wav = build_wav_bytes(samples=16)
    transport = FakeTransport(
        FakeResponse(
            chunks=(wav[:12],),
            fail_after=httpx.RemoteProtocolError('peer closed early'),
        )
    )
    sink = tmp_path / 'raw.bin'
    with pytest.raises(TransportUncertainFailure) as raised:
        make_client(transport).synthesize(text='Проверка.', sink=sink)
    assert raised.value.kind == 'partial_response'
    assert sink.read_bytes() == wav[:12]


def test_write_interruption_is_uncertain(tmp_path: Path) -> None:
    """A failed request write phase never claims a known outcome."""
    transport = FakeTransport(httpx.WriteTimeout('stalled upload'))
    with pytest.raises(TransportUncertainFailure) as raised:
        make_client(transport).synthesize(
            text='Проверка.', sink=tmp_path / 'raw.bin'
        )
    assert raised.value.kind == 'write_interrupted'


def test_key_never_leaks_into_failure_output(tmp_path: Path) -> None:
    """No failure path exposes the key in messages or evidence."""
    transport = FakeTransport(
        FakeResponse(
            chunks=(b'service error',),
            status_code=500,
            headers={'content-type': 'text/plain'},
        )
    )
    sink = tmp_path / 'raw.bin'
    with pytest.raises(ProviderRejectedRequest) as raised:
        make_client(transport).synthesize(text='Проверка.', sink=sink)
    rendered = str(raised.value) + repr(raised.value.evidence.to_dict())
    assert SECRET_KEY not in rendered
    assert SECRET_KEY not in sink.read_bytes().decode(
        'utf-8', errors='replace'
    )


def test_empty_api_key_is_rejected() -> None:
    """The client refuses to run with an empty key value."""
    with pytest.raises(SpeechRequestError, match='must not be empty'):
        SpeechClient('')


def test_phase_timeouts_are_validated() -> None:
    """Nonpositive or non finite phase values are rejected."""
    with pytest.raises(SpeechRequestError):
        PhaseTimeouts(read_seconds=0)
    with pytest.raises(SpeechRequestError):
        PhaseTimeouts(connect_seconds=float('inf'))
    timeouts = PhaseTimeouts(connect_seconds=5)
    httpx_timeout = timeouts.to_httpx()
    assert httpx_timeout.connect == 5.0
    assert httpx_timeout.read == 120.0
