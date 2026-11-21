"""
BolKhata – Gnani Voice AI Client
==================================
Implements two clients verified against official Gnani Vachana API documentation:

  GnaniSTTClient  – Prisma v3.0 real-time WebSocket STT
                    Endpoint : wss://api.vachana.ai/stt/v3/stream
                    Auth     : header  x-api-key-id: <key>
                    Audio    : binary PCM s16le mono, 1024-byte chunks at 16 kHz

  GnaniTTSClient  – Timbre v3.0 REST synthesis
                    Endpoint : POST https://api.vachana.ai/api/v1/tts/inference
                    Auth     : header  X-API-Key-ID: <key>
                    Response : raw WAV binary (application/octet-stream)

Both clients read credentials exclusively from app.core.config.settings.
No API key is ever logged, printed, or included in exception messages.

Mock mode:
  - Activated when settings.gnani_mock_mode is True, OR
  - Automatically activated when settings.gnani_api_key is empty.
  - Mock STT yields a canned transcript event without opening any network connection.
  - Mock TTS returns a minimal valid WAV file (44-byte header, silence).
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import struct
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import AsyncIterator, Optional

import httpx
import websockets
from websockets.exceptions import ConnectionClosedError, WebSocketException

from app.core.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants (from Gnani documentation)
# ---------------------------------------------------------------------------

_STT_WS_ENDPOINT = settings.gnani_stt_ws_endpoint   # wss://api.vachana.ai/stt/v3/stream
_TTS_REST_ENDPOINT = settings.gnani_tts_rest_endpoint  # https://api.vachana.ai/api/v1/tts/inference

# Audio streaming constants verified from Gnani docs:
#   PCM s16le mono, 1024-byte chunks = 512 samples at 16 kHz = 32 ms per chunk
STT_CHUNK_BYTES: int = 1024
STT_SAMPLE_RATE: int = 16_000
STT_CHUNK_DURATION_S: float = STT_CHUNK_BYTES / (STT_SAMPLE_RATE * 2)  # 0.032 s

# HTTP / WebSocket timeouts
_CONNECT_TIMEOUT_S: float = 10.0
_TTS_REQUEST_TIMEOUT_S: float = 30.0


# ---------------------------------------------------------------------------
# Shared data models
# ---------------------------------------------------------------------------

class GnaniEventType(str, Enum):
    """Server-side event types sent by Gnani STT WebSocket."""
    CONNECTED   = "connected"    # Initial handshake confirmation
    PROCESSING  = "processing"  # VAD detected speech start / end-of-speech boundary
    TRANSCRIPT  = "transcript"  # Finalized speech segment text ready
    ERROR       = "error"       # Server-side error


@dataclass
class STTTranscript:
    """
    A finalized speech segment returned by Prisma v3.0.

    Verified field names from Gnani WebSocket server response:
        event               : "transcript"
        text                : str    – finalized transcript text
        segment_index       : int    – sequential segment counter in this session
        audio_duration_ms   : int    – length of audio that was transcribed (ms)
        latency             : int    – server processing latency (ms)
        detected_language_code : str – language identified (Prisma v3.0, indic-auto)
    """
    text: str
    segment_index: int = 0
    audio_duration_ms: int = 0
    latency: int = 0
    detected_language_code: str = "hi-IN"
    raw: dict = field(default_factory=dict)


@dataclass
class STTError:
    """An error event from the Gnani STT WebSocket server."""
    message: str
    raw: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _is_mock_mode() -> bool:
    """
    Returns True when mock mode should be used.
    Mock is active if explicitly enabled or if no API key is configured.
    The API key value itself is never inspected beyond checking emptiness.
    """
    return settings.gnani_mock_mode or not settings.is_gnani_configured


def _make_silence_wav(
    duration_ms: int = 100,
    sample_rate: int = 16_000,
    num_channels: int = 1,
    sample_width: int = 2,
) -> bytes:
    """
    Generates a minimal valid WAV file containing silence.
    Used as the mock TTS response so callers receive parseable audio bytes.
    """
    num_samples = int(sample_rate * duration_ms / 1000)
    pcm_data = b"\x00" * (num_samples * num_channels * sample_width)
    byte_rate = sample_rate * num_channels * sample_width
    block_align = num_channels * sample_width

    header = struct.pack(
        "<4sI4s"       # RIFF chunk
        "4sIHHIIHH"    # fmt  sub-chunk
        "4sI",         # data sub-chunk header
        b"RIFF",
        36 + len(pcm_data),
        b"WAVE",
        b"fmt ",
        16,            # sub-chunk size (PCM)
        1,             # audio format (PCM = 1)
        num_channels,
        sample_rate,
        byte_rate,
        block_align,
        sample_width * 8,  # bits per sample
        b"data",
        len(pcm_data),
    )
    return header + pcm_data


# ---------------------------------------------------------------------------
# GnaniSTTClient
# ---------------------------------------------------------------------------

class GnaniSTTClient:
    """
    Prisma v3.0 real-time Speech-to-Text client.

    Wire format verified from Gnani documentation:
    ─────────────────────────────────────────────
    Connection:
        URL     : wss://api.vachana.ai/stt/v3/stream
        Headers : x-api-key-id = <GNANI_API_KEY>
                  model        = prisma-v3.0
                  language_code= indic-auto  (or hi-IN / en-IN)
                  format       = transcribe  (or verbatim / codemix / transliterate)

    Client → Server:
        Binary frames  : raw PCM s16le mono, 1024 bytes each (~32 ms at 16 kHz)
        JSON text frame: {"type": "commit"} to force-flush current utterance
        JSON text frame: {"type": "update_settings", "language_code": "hi-IN", ...}

    Server → Client JSON events:
        {"event": "connected",   "sample_rate": 16000, "chunk_size": 1024}
        {"event": "processing",  "timestamp": <ms>}
        {"event": "transcript",  "text": "...", "segment_index": 0,
                                 "audio_duration_ms": 1420, "latency": 185,
                                 "detected_language_code": "hi-IN"}
        {"event": "error",       "message": "...", "timestamp": <ms>}

    Usage (async context manager):
        async with GnaniSTTClient() as stt:
            await stt.send_audio_chunk(pcm_bytes)
            await stt.commit()                    # flush utterance
            async for transcript in stt.receive():
                print(transcript.text)
    """

    def __init__(
        self,
        language_code: Optional[str] = None,
        fmt: Optional[str] = None,
        topic: Optional[str] = None,
    ) -> None:
        self._language_code = language_code or settings.gnani_stt_language
        self._format = fmt or settings.gnani_stt_format
        self._topic = topic
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._mock = _is_mock_mode()

    # ── Context manager ──────────────────────────────────────────────────

    async def __aenter__(self) -> "GnaniSTTClient":
        if self._mock:
            logger.info("[GnaniSTT] Mock mode active – no WebSocket connection opened")
            return self

        headers = self._build_headers()
        logger.info(
            "[GnaniSTT] Connecting to %s  model=%s  lang=%s  format=%s",
            _STT_WS_ENDPOINT,
            settings.gnani_stt_model,
            self._language_code,
            self._format,
        )
        try:
            self._ws = await asyncio.wait_for(
                websockets.connect(
                    _STT_WS_ENDPOINT,
                    additional_headers=headers,
                    # Disable ping/pong timeout for streaming – audio is continuous
                    ping_interval=None,
                    ping_timeout=None,
                ),
                timeout=_CONNECT_TIMEOUT_S,
            )
            # Wait for the initial "connected" confirmation from the server
            raw = await asyncio.wait_for(self._ws.recv(), timeout=5.0)
            msg = json.loads(raw) if isinstance(raw, str) else {}
            logger.info("[GnaniSTT] Connected. Server config: %s", msg)
        except asyncio.TimeoutError as exc:
            raise GnaniConnectionError(
                f"Timed out connecting to Gnani STT after {_CONNECT_TIMEOUT_S}s"
            ) from exc
        except WebSocketException as exc:
            raise GnaniConnectionError(
                f"WebSocket error connecting to Gnani STT: {type(exc).__name__}"
            ) from exc
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()

    # ── Public API ───────────────────────────────────────────────────────

    async def send_audio_chunk(self, pcm_bytes: bytes) -> None:
        """
        Stream one PCM chunk to Gnani.
        pcm_bytes must be exactly STT_CHUNK_BYTES (1024) bytes of s16le mono PCM.
        In mock mode this is a no-op.
        """
        if self._mock:
            return
        if not self._ws:
            raise GnaniNotConnectedError("STT client is not connected. Use as async context manager.")
        try:
            await self._ws.send(pcm_bytes)
        except ConnectionClosedError as exc:
            raise GnaniConnectionError("STT WebSocket connection closed unexpectedly") from exc

    async def commit(self) -> None:
        """
        Send a commit control frame to force the server to finalize the current utterance.
        Useful for push-to-talk UX where the user has finished speaking.
        """
        if self._mock:
            return
        if not self._ws:
            raise GnaniNotConnectedError("STT client is not connected.")
        await self._ws.send(json.dumps({"type": "commit"}))
        logger.debug("[GnaniSTT] Sent commit frame")

    async def update_settings(self, **kwargs) -> None:
        """
        Update streaming settings mid-session without reconnecting.
        Valid kwargs: language_code, format, keyterms, topic, previous_text
        """
        if self._mock:
            return
        if not self._ws:
            raise GnaniNotConnectedError("STT client is not connected.")
        payload = {"type": "update_settings", **kwargs}
        await self._ws.send(json.dumps(payload))
        logger.debug("[GnaniSTT] Sent update_settings: %s", list(kwargs.keys()))

    async def receive(self) -> AsyncIterator[STTTranscript]:
        """
        Async generator that yields STTTranscript objects as they arrive from the server.
        In mock mode, yields one fixed transcript and stops.
        """
        if self._mock:
            yield STTTranscript(
                text="mock transcript: Ramesh ko sau rupaye udhaar",
                segment_index=0,
                audio_duration_ms=1000,
                latency=0,
                detected_language_code="hi-IN",
                raw={"type": "transcript", "text": "mock transcript: Ramesh ko sau rupaye udhaar"},
            )
            return

        if not self._ws:
            raise GnaniNotConnectedError("STT client is not connected.")

        async for raw_msg in self._ws:
            if isinstance(raw_msg, bytes):
                # Gnani docs: server sends JSON text; ignore unexpected binary
                logger.debug("[GnaniSTT] Received unexpected binary frame, skipping")
                continue

            try:
                msg = json.loads(raw_msg)
            except json.JSONDecodeError:
                logger.warning("[GnaniSTT] Received non-JSON text frame: %r", raw_msg[:80])
                continue

            event = msg.get("type", "")

            if event == GnaniEventType.TRANSCRIPT:
                yield STTTranscript(
                    text=msg.get("text", ""),
                    segment_index=msg.get("segment_index", 0),
                    audio_duration_ms=msg.get("audio_duration_ms", 0),
                    latency=msg.get("latency", 0),
                    detected_language_code=msg.get("detected_language_code", self._language_code),
                    raw=msg,
                )

            elif event == GnaniEventType.PROCESSING:
                logger.debug("[GnaniSTT] VAD processing event at t=%s", msg.get("timestamp"))

            elif event == GnaniEventType.CONNECTED:
                # Can arrive if server re-sends; just log
                logger.info("[GnaniSTT] Re-received connected event: %s", msg)

            elif event == GnaniEventType.ERROR:
                err_msg = msg.get("message", "Unknown Gnani STT error")
                logger.error("[GnaniSTT] Server error event: %s", err_msg)
                raise GnaniAPIError(f"Gnani STT server error: {err_msg}")

            else:
                logger.debug("[GnaniSTT] Unrecognised event type %r – ignoring", event)

    async def close(self) -> None:
        """Gracefully close the WebSocket connection."""
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass  # Already closed is fine
            finally:
                self._ws = None
                logger.info("[GnaniSTT] WebSocket closed")

    # ── Internals ────────────────────────────────────────────────────────

    def _build_headers(self) -> dict:
        """
        Constructs WebSocket upgrade headers.
        The API key is read once from settings and never logged.

        Verified header names from Gnani docs:
            x-api-key-id   – authentication (required)
            model          – prisma-v3.0 (recommended, defaults to v2.5 if omitted)
            language_code  – indic-auto / hi-IN / en-IN (required)
            format         – transcribe / verbatim / codemix / transliterate
            topic          – optional context hint (up to 1600 chars)
        """
        headers: dict = {
            "x-api-key-id": settings.gnani_api_key,
            "model": settings.gnani_stt_model,
            "language_code": self._language_code,
            "format": self._format,
        }
        if self._topic:
            headers["topic"] = self._topic[:1600]  # Gnani limit
        return headers


# ---------------------------------------------------------------------------
# GnaniTTSClient
# ---------------------------------------------------------------------------

class GnaniTTSClient:
    """
    Timbre v3.0 Text-to-Speech REST client.

    Wire format verified from Gnani documentation:
    ─────────────────────────────────────────────
    Endpoint : POST https://api.vachana.ai/api/v1/tts/inference
    Auth     : header  X-API-Key-ID: <GNANI_API_KEY>
    Content  : application/json

    Request body:
        {
            "text":     "नमस्ते! आप कैसे हैं?",
            "model":    "timbre-v3.0",
            "voice":    "Pranav",
            "language": "hi-IN",
            "speed":    1.0,
            "audio_config": {
                "sample_rate":  16000,
                "num_channels": 1,
                "sample_width": 2,
                "encoding":     "linear_pcm",
                "container":    "wav"
            }
        }

    Response:
        200 OK – raw binary audio bytes (WAV container)
        4xx/5xx – JSON error body

    Timbre v3.0 supports inline expression tags in text:
        <laugh>, <sigh>, <whispers>, <pauses>

    Usage:
        tts = GnaniTTSClient()
        wav_bytes = await tts.synthesize("Ramesh ka baaki hai do sau rupaye")
    """

    def __init__(
        self,
        voice: Optional[str] = None,
        language: Optional[str] = None,
        speed: Optional[float] = None,
        sample_rate: Optional[int] = None,
    ) -> None:
        self._voice = voice or settings.gnani_tts_voice
        self._language = language or settings.gnani_tts_language
        self._speed = speed if speed is not None else settings.gnani_tts_speed
        self._sample_rate = sample_rate or settings.gnani_tts_sample_rate
        self._mock = _is_mock_mode()

    # ── Public API ───────────────────────────────────────────────────────

    async def synthesize(self, text: str) -> bytes:
        """
        Convert text to speech and return raw WAV bytes.

        In mock mode returns a minimal silence WAV so callers can proceed
        without network access or API credits.

        Args:
            text: The text to synthesize. May include Timbre v3.0 expression
                  tags (<laugh>, <sigh>, <whispers>, <pauses>) for natural
                  kirana-style responses.

        Returns:
            bytes: Raw WAV audio data.

        Raises:
            GnaniAPIError: On non-200 HTTP response from Gnani.
            GnaniConnectionError: On network-level failure.
        """
        if not text or not text.strip():
            logger.warning("[GnaniTTS] synthesize() called with empty text, returning silence")
            return _make_silence_wav()

        if self._mock:
            logger.info("[GnaniTTS] Mock mode – returning silence WAV for text: %r", text[:60])
            return _make_silence_wav(duration_ms=200)

        payload = self._build_request_payload(text)
        headers = {
            "X-API-Key-ID": settings.gnani_api_key,  # Exact header name per Gnani docs
            "Content-Type": "application/json",
            "Accept": "application/octet-stream",
        }

        logger.info(
            "[GnaniTTS] Synthesizing %d chars  voice=%s  lang=%s  speed=%s",
            len(text),
            self._voice,
            self._language,
            self._speed,
        )

        try:
            async with httpx.AsyncClient(timeout=_TTS_REQUEST_TIMEOUT_S) as client:
                response = await client.post(
                    _TTS_REST_ENDPOINT,
                    headers=headers,
                    json=payload,
                )
        except httpx.TimeoutException as exc:
            raise GnaniConnectionError(
                f"Gnani TTS request timed out after {_TTS_REQUEST_TIMEOUT_S}s"
            ) from exc
        except httpx.RequestError as exc:
            raise GnaniConnectionError(
                f"Gnani TTS network error: {type(exc).__name__}"
            ) from exc

        if response.status_code != 200:
            # Log status code only – never log response body which may hint at key
            logger.error(
                "[GnaniTTS] HTTP %d from TTS endpoint. Check API key and request payload.",
                response.status_code,
            )
            raise GnaniAPIError(
                f"Gnani TTS returned HTTP {response.status_code}. "
                "Verify API key and request parameters."
            )

        audio_bytes = response.content
        logger.info("[GnaniTTS] Received %d bytes of audio", len(audio_bytes))
        return audio_bytes

    # ── Internals ────────────────────────────────────────────────────────

    def _build_request_payload(self, text: str) -> dict:
        """
        Builds the verified Gnani Timbre v3.0 request JSON.

        Exact field names from Gnani official documentation:
            text, model, voice, language, speed, audio_config
            audio_config: sample_rate, num_channels, sample_width, encoding, container
        """
        return {
            "text": text,
            "model": settings.gnani_tts_model,         # "timbre-v3.0"
            "voice": self._voice,                       # e.g. "Pranav"
            "language": self._language,                 # e.g. "hi-IN"
            "speed": self._speed,                       # 0.5 – 2.0
            "audio_config": {
                "sample_rate": self._sample_rate,       # e.g. 16000
                "num_channels": 1,                      # Mono (Gnani docs: always 1)
                "sample_width": 2,                      # 16-bit (Gnani docs: always 2)
                "encoding": "linear_pcm",               # Gnani docs: "linear_pcm"
                "container": "wav",                     # Gnani docs: "wav"
            },
        }


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class GnaniError(Exception):
    """Base class for all Gnani client errors."""


class GnaniConnectionError(GnaniError):
    """Raised when a network or WebSocket connection cannot be established or is lost."""


class GnaniAPIError(GnaniError):
    """Raised when Gnani returns an error response (HTTP 4xx/5xx or error event)."""


class GnaniNotConnectedError(GnaniError):
    """Raised when a method is called before the client is connected."""
