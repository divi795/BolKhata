import asyncio
import time
import os
import logging
import json

logger = logging.getLogger(__name__)

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.database import SessionLocal
from app.voice.gnani_client import (
    GnaniAPIError,
    GnaniConnectionError,
    GnaniSTTClient,
    GnaniTTSClient,
    STT_CHUNK_BYTES,
)
from app.voice.orchestrator import process_voice_command, process_voice_commands, STATUS_SUCCESS, STATUS_NEEDS_CUSTOMER_CLARIFICATION

router = APIRouter()

_ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("ALLOWED_WS_ORIGINS", "http://127.0.0.1:5500,http://localhost:5500").split(",")
    if origin.strip()
]


@router.websocket("/voice/ws")
async def voice_ws(websocket: WebSocket) -> None:
    origin = websocket.headers.get("origin")
    if origin not in _ALLOWED_ORIGINS:
        await websocket.close(code=4403)
        return

    await websocket.accept()
    db = SessionLocal()
    audio_buffer = bytearray()
    last_transaction_id = None
    pending_customer_clarification = None
    disclosure_sent = False
    _HINDI_DISCLOSURE = "Main ek AI assistant hoon. Main aapki ledger entries record kar sakta hoon."
    _ENGLISH_DISCLOSURE = "I am an AI assistant. I can record your ledger entries."

    async def handle_transcripts(stt) -> None:
            nonlocal last_transaction_id
            nonlocal pending_customer_clarification
            nonlocal disclosure_sent
            async for transcript in stt.receive():
                if not transcript.text or not transcript.text.strip():
                    continue
                transcript_received_at = time.perf_counter()
                parse_started_at = time.perf_counter()
                result = process_voice_commands(db, transcript.text, pending_transaction_id=last_transaction_id, pending_customer_clarification=pending_customer_clarification)
                parse_completed_at = time.perf_counter()
                response_text = result.response_text
                if not disclosure_sent:
                    disclosure = _HINDI_DISCLOSURE if any("\u0900" <= ch <= "\u097f" for ch in transcript.text) else _ENGLISH_DISCLOSURE
                    response_text = disclosure + " " + response_text
                    disclosure_sent = True
                await websocket.send_json(
                    {
                        "type": "response",
                        "transcript": transcript.text,
                        "status": result.status,
                        "response_text": response_text,
                    }
                )
                tts_started_at = time.perf_counter()
                tts_client = GnaniTTSClient()
                first_chunk_at = None
                if not hasattr(tts_client, "synthesize_stream") or getattr(tts_client, "_mock", False):
                    wav_bytes = await tts_client.synthesize(response_text)
                    first_chunk_at = time.perf_counter()
                    await websocket.send_bytes(wav_bytes)
                else:
                    async for chunk in tts_client.synthesize_stream(response_text):
                        if first_chunk_at is None:
                            first_chunk_at = time.perf_counter()
                        await websocket.send_bytes(chunk)
                tts_completed_at = time.perf_counter()
                response_sent_at = time.perf_counter()
                first_chunk_ms = (first_chunk_at - tts_started_at) * 1000.0 if first_chunk_at is not None else 0.0
                logger.warning(
                    "Live voice latency ms: parse=%.1f tts=%.1f first_chunk=%.1f total=%.1f",
                    (parse_completed_at - parse_started_at) * 1000.0,
                    (tts_completed_at - tts_started_at) * 1000.0,
                    first_chunk_ms,
                    (response_sent_at - transcript_received_at) * 1000.0,
                )
                if result.last_transaction_id is not None:
                    last_transaction_id = result.last_transaction_id
                pending_customer_clarification = result.pending_customer_clarification if result.status == STATUS_NEEDS_CUSTOMER_CLARIFICATION else None

    try:
        async with GnaniSTTClient() as stt:
            is_mock = getattr(stt, "_mock", False)
            if is_mock:
                transcript_task = asyncio.create_task(asyncio.Event().wait())
            else:
                transcript_task = asyncio.create_task(handle_transcripts(stt))
            receive_task = asyncio.ensure_future(websocket.receive())
            try:
                while True:
                    done, _ = await asyncio.wait(
                        {receive_task, transcript_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if transcript_task in done and not is_mock:
                        exc = transcript_task.exception()
                        if exc is not None:
                            raise exc
                        # Transcript stream ended; do not restart. Keep session alive.
                        transcript_task = asyncio.create_task(asyncio.Event().wait())
                    if receive_task in done:
                        message = receive_task.result()
                        if message.get("type") == "websocket.disconnect":
                            break
                        receive_task = asyncio.ensure_future(websocket.receive())
                        if message.get("bytes") is not None:
                            audio_buffer.extend(message["bytes"])
                            while len(audio_buffer) >= STT_CHUNK_BYTES:
                                chunk = bytes(audio_buffer[:STT_CHUNK_BYTES])
                                del audio_buffer[:STT_CHUNK_BYTES]
                                await stt.send_audio_chunk(chunk)
                        elif message.get("text") is not None:
                            try:
                                control = json.loads(message["text"])
                            except json.JSONDecodeError:
                                control = {}
                            if not isinstance(control, dict):
                                continue
                            if control.get("type") == "commit":
                                if audio_buffer:
                                    remainder = bytes(audio_buffer)
                                    del audio_buffer[:]
                                    padded = remainder + b"\x00" * (STT_CHUNK_BYTES - len(remainder))
                                    await stt.send_audio_chunk(padded)
                                await stt.commit()
                            elif control.get("type") == "mock_transcript":
                                text = control.get("text", "")
                                if isinstance(text, str) and text.strip():
                                    transcript_received_at = time.perf_counter()
                                    parse_started_at = time.perf_counter()
                                    result = process_voice_commands(db, text, pending_transaction_id=last_transaction_id, pending_customer_clarification=pending_customer_clarification)
                                    parse_completed_at = time.perf_counter()
                                    response_text = result.response_text
                                    if not disclosure_sent:
                                        disclosure = _HINDI_DISCLOSURE if any("\u0900" <= ch <= "\u097f" for ch in text) else _ENGLISH_DISCLOSURE
                                        response_text = disclosure + " " + response_text
                                        disclosure_sent = True
                                    tts_started_at = time.perf_counter()
                                    tts_client = GnaniTTSClient()
                                    await websocket.send_json(
                                        {
                                            "type": "response",
                                            "transcript": text,
                                            "status": result.status,
                                            "response_text": response_text,
                                        }
                                    )
                                    first_chunk_at = None
                                    if not hasattr(tts_client, "synthesize_stream") or getattr(tts_client, "_mock", False):
                                        wav_bytes = await tts_client.synthesize(response_text)
                                        first_chunk_at = time.perf_counter()
                                        await websocket.send_bytes(wav_bytes)
                                    else:
                                        async for chunk in tts_client.synthesize_stream(response_text):
                                            if first_chunk_at is None:
                                                first_chunk_at = time.perf_counter()
                                            await websocket.send_bytes(chunk)
                                    tts_completed_at = time.perf_counter()
                                    response_sent_at = time.perf_counter()
                                    first_chunk_ms = (first_chunk_at - tts_started_at) * 1000.0 if first_chunk_at is not None else 0.0
                                    logger.warning(
                                        "Voice latency ms: parse=%.1f tts=%.1f first_chunk=%.1f total=%.1f",
                                        (parse_completed_at - parse_started_at) * 1000.0,
                                        (tts_completed_at - tts_started_at) * 1000.0,
                                        first_chunk_ms,
                                        (response_sent_at - transcript_received_at) * 1000.0,
                                    )
                                    if result.last_transaction_id is not None:
                                        last_transaction_id = result.last_transaction_id
                                    pending_customer_clarification = result.pending_customer_clarification if result.status == STATUS_NEEDS_CUSTOMER_CLARIFICATION else None
            except WebSocketDisconnect:
                pass
            finally:
                for task in (receive_task, transcript_task):
                    if not task.done():
                        task.cancel()
                for task in (receive_task, transcript_task):
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass
    except (GnaniAPIError, GnaniConnectionError) as exc:
        try:
            await websocket.send_json({"type": "error", "message": str(exc)})
        except Exception:
            pass
    except Exception as exc:  # noqa: BLE001
        try:
            await websocket.send_json({"type": "error", "message": str(exc)})
        except Exception:
            pass
    finally:
        db.close()
