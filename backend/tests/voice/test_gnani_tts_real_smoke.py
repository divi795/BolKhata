import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest
from app.core.config import settings
from app.voice.gnani_client import GnaniTTSClient


@pytest.mark.skipif(
    not settings.is_gnani_configured or settings.gnani_mock_mode,
    reason="Real Gnani TTS smoke test requires a configured API key and mock mode disabled",
)
@pytest.mark.asyncio
async def test_real_tts_smoke():
    assert settings.gnani_tts_model == "timbre-v3.0"
    assert settings.gnani_tts_voice == "Shivani"
    assert settings.gnani_tts_speed == 1.0

    client = GnaniTTSClient()
    audio = await client.synthesize("Namaste, yeh ek test hai.")

    assert isinstance(audio, (bytes, bytearray))
    assert len(audio) > 44
    assert bytes(audio)[:4] == b"RIFF"
    assert bytes(audio)[8:12] == b"WAVE"
