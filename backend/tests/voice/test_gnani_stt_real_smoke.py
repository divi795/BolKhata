import asyncio
import math
import struct
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest

from app.core.config import settings
from app.voice.gnani_client import (
    GnaniSTTClient,
    STT_CHUNK_BYTES,
    STT_SAMPLE_RATE,
)


def _make_speech_pcm() -> bytes:
    wav_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            wav_path = tmp.name

        ps = (
            "Add-Type -AssemblyName System.Speech; "
            "$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            "$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo("
            "16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, "
            "[System.Speech.AudioFormat.AudioChannel]::Mono); "
            "$synth.Rate = -2; "
            "$synth.SelectVoice('Microsoft David Desktop'); "
            "$synth.SetOutputToWaveFile('%s', $fmt); "
            "$synth.Speak('Hello. This is a longer test sentence for speech recognition.'); "
            "$synth.Dispose();" % wav_path
        )

        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            check=True,
            capture_output=True,
        )

        with wave.open(wav_path, "rb") as wav:
            assert wav.getframerate() == 16000
            assert wav.getnchannels() == 1
            assert wav.getsampwidth() == 2
            pcm = wav.readframes(wav.getnframes())

        while len(pcm) % STT_CHUNK_BYTES:
            pcm += b"\x00\x00"

        return pcm
    finally:
        if wav_path:
            Path(wav_path).unlink(missing_ok=True)


@pytest.mark.skipif(
    not settings.is_gnani_configured or settings.gnani_mock_mode,
    reason="Real Gnani STT smoke test requires a configured API key and mock mode disabled",
)
@pytest.mark.asyncio
async def test_real_stt_smoke():
    pcm = _make_speech_pcm()

    async with GnaniSTTClient(language_code="indic-auto") as stt:
        for offset in range(0, len(pcm), STT_CHUNK_BYTES):
            chunk = pcm[offset : offset + STT_CHUNK_BYTES]
            assert len(chunk) == STT_CHUNK_BYTES
            await stt.send_audio_chunk(chunk)
            await asyncio.sleep(0.032)

        await stt.commit()

        transcript = None

        async def collect_one():
            nonlocal transcript
            async for item in stt.receive():
                transcript = item
                break

        await asyncio.wait_for(collect_one(), timeout=15.0)

        assert transcript is not None
        assert isinstance(transcript.text, str)
        assert transcript.text.strip(), "Gnani STT returned an empty transcript"
