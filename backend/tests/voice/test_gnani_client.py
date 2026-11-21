"""
Tests for backend/app/voice/gnani_client.py

All tests run entirely in mock mode.
No real Gnani API calls are made.
No API keys are printed or exposed.
"""

import struct
import pytest
import pytest_asyncio

# Ensure backend is on the path (mirrors conftest.py approach)
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.voice.gnani_client import (
    GnaniSTTClient,
    GnaniTTSClient,
    GnaniEventType,
    GnaniError,
    GnaniConnectionError,
    GnaniAPIError,
    GnaniNotConnectedError,
    STTTranscript,
    STT_CHUNK_BYTES,
    STT_SAMPLE_RATE,
    STT_CHUNK_DURATION_S,
    _make_silence_wav,
    _is_mock_mode,
)
from app.core.config import settings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_wav_header(data: bytes) -> dict:
    """Parse a WAV file header and return key fields."""
    assert data[:4] == b"RIFF", "Not a RIFF file"
    assert data[8:12] == b"WAVE", "Not a WAVE file"
    # fmt chunk starts at byte 12
    assert data[12:16] == b"fmt ", "Missing fmt chunk"
    audio_format, num_channels, sample_rate = struct.unpack_from("<HHI", data, 20)
    bits_per_sample = struct.unpack_from("<H", data, 34)[0]
    return {
        "audio_format": audio_format,
        "num_channels": num_channels,
        "sample_rate": sample_rate,
        "bits_per_sample": bits_per_sample,
    }


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

class TestConstants:
    def test_chunk_bytes_is_1024(self):
        """Gnani docs: exactly 1024 bytes per PCM frame."""
        assert STT_CHUNK_BYTES == 1024

    def test_sample_rate_is_16000(self):
        """Gnani docs: 16 kHz recommended sample rate."""
        assert STT_SAMPLE_RATE == 16_000

    def test_chunk_duration_is_32ms(self):
        """1024 bytes / (16000 Hz * 2 bytes) = 0.032 s = 32 ms per chunk."""
        assert abs(STT_CHUNK_DURATION_S - 0.032) < 1e-6


# ---------------------------------------------------------------------------
# _make_silence_wav
# ---------------------------------------------------------------------------

class TestMakeSilenceWav:
    def test_returns_valid_riff_wave(self):
        wav = _make_silence_wav()
        assert wav[:4] == b"RIFF"
        assert wav[8:12] == b"WAVE"

    def test_pcm_format(self):
        wav = _make_silence_wav()
        hdr = parse_wav_header(wav)
        assert hdr["audio_format"] == 1,       "Expected PCM (format=1)"
        assert hdr["num_channels"] == 1,        "Expected mono"
        assert hdr["bits_per_sample"] == 16,    "Expected 16-bit"

    def test_sample_rate_in_header(self):
        wav = _make_silence_wav(sample_rate=16_000)
        hdr = parse_wav_header(wav)
        assert hdr["sample_rate"] == 16_000

    def test_duration_controls_size(self):
        wav_100ms = _make_silence_wav(duration_ms=100, sample_rate=16_000)
        wav_200ms = _make_silence_wav(duration_ms=200, sample_rate=16_000)
        # 200 ms should be roughly twice the size of 100 ms (PCM data only)
        assert len(wav_200ms) > len(wav_100ms)

    def test_non_zero_length(self):
        wav = _make_silence_wav(duration_ms=50)
        assert len(wav) > 44  # At least more than the header


# ---------------------------------------------------------------------------
# GnaniEventType
# ---------------------------------------------------------------------------

class TestGnaniEventType:
    def test_event_type_values(self):
        assert GnaniEventType.CONNECTED  == "connected"
        assert GnaniEventType.PROCESSING == "processing"
        assert GnaniEventType.TRANSCRIPT == "transcript"
        assert GnaniEventType.ERROR      == "error"


# ---------------------------------------------------------------------------
# STTTranscript dataclass
# ---------------------------------------------------------------------------

class TestSTTTranscript:
    def test_basic_creation(self):
        t = STTTranscript(text="Ramesh ko sau rupaye udhaar")
        assert t.text == "Ramesh ko sau rupaye udhaar"
        assert t.segment_index == 0
        assert t.audio_duration_ms == 0
        assert t.latency == 0
        assert t.detected_language_code == "hi-IN"
        assert t.raw == {}

    def test_full_creation(self):
        raw = {"type": "transcript", "text": "hello"}
        t = STTTranscript(
            text="hello",
            segment_index=3,
            audio_duration_ms=850,
            latency=120,
            detected_language_code="en-IN",
            raw=raw,
        )
        assert t.segment_index == 3
        assert t.audio_duration_ms == 850
        assert t.latency == 120
        assert t.detected_language_code == "en-IN"
        assert t.raw is raw


# ---------------------------------------------------------------------------
# GnaniSTTClient — mock mode
# ---------------------------------------------------------------------------

class TestGnaniSTTClientMock:
    """
    All tests use mock mode (no network).
    Mock mode is forced by passing mock=True OR by testing with empty key.
    Here we patch _is_mock_mode via monkeypatching the client's _mock attribute.
    """

    def _make_mock_client(self, **kwargs) -> GnaniSTTClient:
        client = GnaniSTTClient(**kwargs)
        client._mock = True  # Force mock regardless of .env key
        return client

    def test_instantiation_default_settings(self):
        client = GnaniSTTClient()
        assert client._language_code == settings.gnani_stt_language
        assert client._format == settings.gnani_stt_format
        assert client._topic is None

    def test_instantiation_custom_params(self):
        client = GnaniSTTClient(language_code="hi-IN", fmt="verbatim", topic="kirana shop")
        assert client._language_code == "hi-IN"
        assert client._format == "verbatim"
        assert client._topic == "kirana shop"

    def test_build_headers_contains_required_keys(self):
        client = GnaniSTTClient()
        headers = client._build_headers()
        # Verify required Gnani header names (from docs)
        assert "x-api-key-id" in headers
        assert "model" in headers
        assert "language_code" in headers
        assert "format" in headers

    def test_build_headers_model_value(self):
        client = GnaniSTTClient()
        headers = client._build_headers()
        assert headers["model"] == "prisma-v3.0"

    def test_build_headers_language_default(self):
        client = GnaniSTTClient()
        assert client._build_headers()["language_code"] == "indic-auto"

    def test_build_headers_custom_language(self):
        client = GnaniSTTClient(language_code="hi-IN")
        assert client._build_headers()["language_code"] == "hi-IN"

    def test_build_headers_topic_included_when_set(self):
        client = GnaniSTTClient(topic="grocery store")
        headers = client._build_headers()
        assert "topic" in headers
        assert headers["topic"] == "grocery store"

    def test_build_headers_topic_truncated_at_1600(self):
        long_topic = "x" * 2000
        client = GnaniSTTClient(topic=long_topic)
        headers = client._build_headers()
        assert len(headers["topic"]) == 1600

    def test_build_headers_no_topic_key_when_none(self):
        client = GnaniSTTClient()
        headers = client._build_headers()
        assert "topic" not in headers

    def test_api_key_not_empty_when_configured(self):
        """Key should be non-empty when is_gnani_configured is True."""
        if settings.is_gnani_configured:
            client = GnaniSTTClient()
            headers = client._build_headers()
            assert len(headers["x-api-key-id"]) > 0

    @pytest.mark.asyncio
    async def test_mock_context_manager_does_not_connect(self):
        """Mock mode must not open any WebSocket connection."""
        client = self._make_mock_client()
        async with client as stt:
            assert stt._ws is None   # No real connection opened

    @pytest.mark.asyncio
    async def test_send_audio_chunk_is_noop_in_mock(self):
        client = self._make_mock_client()
        async with client as stt:
            # Must not raise even with wrong-size chunk
            await stt.send_audio_chunk(b"\x00" * 1024)
            await stt.send_audio_chunk(b"\x00" * 512)

    @pytest.mark.asyncio
    async def test_commit_is_noop_in_mock(self):
        client = self._make_mock_client()
        async with client as stt:
            await stt.commit()   # Must not raise

    @pytest.mark.asyncio
    async def test_update_settings_is_noop_in_mock(self):
        client = self._make_mock_client()
        async with client as stt:
            await stt.update_settings(language_code="hi-IN")  # Must not raise

    @pytest.mark.asyncio
    async def test_receive_yields_one_mock_transcript(self):
        client = self._make_mock_client()
        async with client as stt:
            transcripts = []
            async for t in stt.receive():
                transcripts.append(t)
        assert len(transcripts) == 1
        t = transcripts[0]
        assert isinstance(t, STTTranscript)
        assert len(t.text) > 0
        assert t.detected_language_code == "hi-IN"

    @pytest.mark.asyncio
    async def test_receive_mock_transcript_has_valid_structure(self):
        client = self._make_mock_client()
        async with client as stt:
            async for t in stt.receive():
                assert isinstance(t.text, str)
                assert isinstance(t.segment_index, int)
                assert isinstance(t.audio_duration_ms, int)
                assert isinstance(t.latency, int)
                assert isinstance(t.detected_language_code, str)
                assert isinstance(t.raw, dict)

    @pytest.mark.asyncio
    async def test_not_connected_error_without_context_manager(self):
        client = GnaniSTTClient()
        client._mock = False  # Pretend real mode
        # _ws is None → should raise GnaniNotConnectedError
        with pytest.raises(GnaniNotConnectedError):
            await client.send_audio_chunk(b"\x00" * 1024)

    @pytest.mark.asyncio
    async def test_commit_raises_not_connected_without_context(self):
        client = GnaniSTTClient()
        client._mock = False
        with pytest.raises(GnaniNotConnectedError):
            await client.commit()


# ---------------------------------------------------------------------------
# GnaniSTTClient — real-mode message discriminator (mocked websocket)
# ---------------------------------------------------------------------------

class _FakeWebSocket:
    def __init__(self, messages):
        self._messages = list(messages)
    def __aiter__(self):
        return self
    async def __anext__(self):
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)


class TestSTTTypeDiscriminator:
    def _make_real_client(self):
        client = GnaniSTTClient()
        client._mock = False
        return client

    @pytest.mark.asyncio
    async def test_type_transcript_is_yielded(self):
        client = self._make_real_client()
        client._ws = _FakeWebSocket([
            '{"type":"connected"}',
            '{"type":"processing","timestamp":1}',
            '{"type":"transcript","text":"hello","segment_index":2,"audio_duration_ms":10,"latency":5,"detected_language_code":"en-IN"}',
        ])
        transcripts = []
        async for t in client.receive():
            transcripts.append(t)
        assert len(transcripts) == 1
        assert transcripts[0].text == "hello"
        assert transcripts[0].segment_index == 2
        assert transcripts[0].detected_language_code == "en-IN"

    @pytest.mark.asyncio
    async def test_type_error_raises_gnani_api_error(self):
        client = self._make_real_client()
        client._ws = _FakeWebSocket([
            '{"type":"error","message":"boom"}',
        ])
        with pytest.raises(GnaniAPIError):
            async for _ in client.receive():
                pass

    @pytest.mark.asyncio
    async def test_connected_and_processing_are_ignored(self):
        client = self._make_real_client()
        client._ws = _FakeWebSocket([
            '{"type":"connected"}',
            '{"type":"processing","timestamp":1}',
        ])
        transcripts = []
        async for t in client.receive():
            transcripts.append(t)
        assert transcripts == []


# ---------------------------------------------------------------------------
# GnaniTTSClient — mock mode
# ---------------------------------------------------------------------------

class TestGnaniTTSClientMock:

    def _make_mock_client(self, **kwargs) -> GnaniTTSClient:
        client = GnaniTTSClient(**kwargs)
        client._mock = True
        return client

    def test_instantiation_default_settings(self):
        client = GnaniTTSClient()
        assert client._voice == settings.gnani_tts_voice
        assert client._language == settings.gnani_tts_language
        assert client._speed == settings.gnani_tts_speed
        assert client._sample_rate == settings.gnani_tts_sample_rate

    def test_instantiation_custom_params(self):
        client = GnaniTTSClient(voice="Nalini", language="en-IN", speed=1.5, sample_rate=24000)
        assert client._voice == "Nalini"
        assert client._language == "en-IN"
        assert client._speed == 1.5
        assert client._sample_rate == 24000

    def test_build_request_payload_required_keys(self):
        client = GnaniTTSClient()
        payload = client._build_request_payload("hello")
        # Verified field names from Gnani TTS docs
        assert "text" in payload
        assert "model" in payload
        assert "voice" in payload
        assert "language" in payload
        assert "speed" in payload
        assert "audio_config" in payload

    def test_build_request_payload_model_value(self):
        client = GnaniTTSClient()
        payload = client._build_request_payload("test")
        assert payload["model"] == "timbre-v3.0"

    def test_build_request_payload_audio_config_fields(self):
        """Verified exact audio_config field names from Gnani TTS documentation."""
        client = GnaniTTSClient()
        ac = client._build_request_payload("test")["audio_config"]
        assert "sample_rate" in ac
        assert "num_channels" in ac
        assert "sample_width" in ac
        assert "encoding" in ac
        assert "container" in ac

    def test_build_request_payload_audio_config_values(self):
        client = GnaniTTSClient(sample_rate=16000)
        ac = client._build_request_payload("test")["audio_config"]
        assert ac["num_channels"] == 1          # Always mono per Gnani docs
        assert ac["sample_width"] == 2          # Always 16-bit per Gnani docs
        assert ac["encoding"] == "linear_pcm"   # Gnani docs value
        assert ac["container"] == "wav"          # Gnani docs value
        assert ac["sample_rate"] == 16000

    def test_build_request_payload_text_preserved(self):
        client = GnaniTTSClient()
        text = "Ramesh ka baaki hai do sau rupaye"
        payload = client._build_request_payload(text)
        assert payload["text"] == text

    @pytest.mark.asyncio
    async def test_mock_synthesize_returns_wav_bytes(self):
        client = self._make_mock_client()
        wav = await client.synthesize("Ramesh ko sau rupaye udhaar")
        assert isinstance(wav, bytes)
        assert len(wav) > 44  # More than WAV header

    @pytest.mark.asyncio
    async def test_mock_synthesize_valid_wav_header(self):
        client = self._make_mock_client()
        wav = await client.synthesize("test")
        assert wav[:4] == b"RIFF"
        assert wav[8:12] == b"WAVE"

    @pytest.mark.asyncio
    async def test_mock_synthesize_empty_text_returns_silence(self):
        client = self._make_mock_client()
        wav = await client.synthesize("")
        assert wav[:4] == b"RIFF"

    @pytest.mark.asyncio
    async def test_mock_synthesize_whitespace_text_returns_silence(self):
        client = self._make_mock_client()
        wav = await client.synthesize("   ")
        assert wav[:4] == b"RIFF"

    @pytest.mark.asyncio
    async def test_mock_synthesize_hindi_text(self):
        client = self._make_mock_client()
        wav = await client.synthesize("नमस्ते! Ramesh bhai, aapka baaki hai.")
        assert isinstance(wav, bytes)
        assert len(wav) > 0

    @pytest.mark.asyncio
    async def test_mock_synthesize_expression_tags(self):
        """Timbre v3.0 supports expression tags – should not break mock."""
        client = self._make_mock_client()
        wav = await client.synthesize("Theek hai <pauses> do sau rupaye jama ho gaye.")
        assert isinstance(wav, bytes)


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------

class TestExceptions:
    def test_all_errors_inherit_gnani_error(self):
        assert issubclass(GnaniConnectionError, GnaniError)
        assert issubclass(GnaniAPIError, GnaniError)
        assert issubclass(GnaniNotConnectedError, GnaniError)

    def test_gnani_error_inherits_exception(self):
        assert issubclass(GnaniError, Exception)

    def test_can_raise_and_catch_connection_error(self):
        with pytest.raises(GnaniError):
            raise GnaniConnectionError("network down")

    def test_can_raise_and_catch_api_error(self):
        with pytest.raises(GnaniAPIError):
            raise GnaniAPIError("HTTP 401")

    def test_can_raise_and_catch_not_connected(self):
        with pytest.raises(GnaniNotConnectedError):
            raise GnaniNotConnectedError("not connected")
