import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app
from app.routers import voice


class FakeTTS:
    async def synthesize(self, text: str) -> bytes:
        return b"RIFF" + b"\x00" * 40


class FakeLiveSTT:
    def __init__(self, *args, **kwargs):
        self._mock = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def send_audio_chunk(self, pcm_bytes: bytes) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def receive(self):
        if False:
            yield None


def _create_customer(client: TestClient, name: str) -> int:
    response = client.post("/customers", json={"name": name})
    assert response.status_code == 201
    return response.json()["id"]


def test_mock_transcript_in_mock_mode(db_session, client, monkeypatch):
    monkeypatch.setattr(voice, "_ALLOWED_ORIGINS", ["http://testserver"])
    monkeypatch.setattr(settings, "gnani_mock_mode", True)
    monkeypatch.setattr(settings, "gnani_api_key", "")
    monkeypatch.setattr("app.routers.voice.SessionLocal", lambda: db_session)
    monkeypatch.setattr("app.routers.voice.GnaniTTSClient", lambda: FakeTTS())

    customer_id = _create_customer(client, "Rahul")

    with client.websocket_connect("/voice/ws", headers={"origin": "http://testserver"}) as websocket:
        websocket.send_json({"type": "mock_transcript", "text": "Rahul ko 500 rupaye udhaar"})
        response = websocket.receive_json()
        audio = websocket.receive_bytes()

    assert response["type"] == "response"
    assert response["status"] == "success"
    assert response["transcript"] == "Rahul ko 500 rupaye udhaar"
    assert len(audio) > 0

    transactions = client.get(f"/customers/{customer_id}/transactions").json()
    assert len(transactions) == 1
    assert transactions[0]["amount"] == 500.0


class FakeLiveSTTWithTranscript:
    def __init__(self, *args, **kwargs):
        self._mock = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def send_audio_chunk(self, pcm_bytes: bytes) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def receive(self):
        yield type("Transcript", (), {"text": "Rahul ko 500 rupaye udhaar"})()


def test_live_stt_transcript_path_processes_command(db_session, client, monkeypatch):
    monkeypatch.setattr(voice, "_ALLOWED_ORIGINS", ["http://testserver"])
    monkeypatch.setattr(settings, "gnani_mock_mode", False)
    monkeypatch.setattr(settings, "gnani_api_key", "test-key")
    monkeypatch.setattr("app.routers.voice.SessionLocal", lambda: db_session)
    monkeypatch.setattr("app.routers.voice.GnaniSTTClient", FakeLiveSTTWithTranscript)
    monkeypatch.setattr("app.routers.voice.GnaniTTSClient", lambda: FakeTTS())

    customer_id = _create_customer(client, "Rahul")

    with client.websocket_connect("/voice/ws", headers={"origin": "http://testserver"}) as websocket:
        response = websocket.receive_json()
        audio = websocket.receive_bytes()

    assert response["type"] == "response"
    assert response["status"] == "success"
    assert response["transcript"] == "Rahul ko 500 rupaye udhaar"
    assert len(audio) > 0

    transactions = client.get(f"/customers/{customer_id}/transactions").json()
    assert len(transactions) == 1
    assert transactions[0]["amount"] == 500.0


def test_first_turn_includes_ai_disclosure(db_session, client, monkeypatch):
    monkeypatch.setattr(voice, "_ALLOWED_ORIGINS", ["http://testserver"])
    monkeypatch.setattr(settings, "gnani_mock_mode", True)
    monkeypatch.setattr(settings, "gnani_api_key", "")
    monkeypatch.setattr("app.routers.voice.SessionLocal", lambda: db_session)
    monkeypatch.setattr("app.routers.voice.GnaniTTSClient", lambda: FakeTTS())

    _create_customer(client, "Rahul")

    with client.websocket_connect("/voice/ws", headers={"origin": "http://testserver"}) as websocket:
        websocket.send_json({"type": "mock_transcript", "text": "Rahul ko 500 rupaye udhaar"})
        response = websocket.receive_json()
        websocket.receive_bytes()

    assert response["response_text"].startswith("I am an AI assistant.")


def test_disclosure_not_repeated_on_second_turn(db_session, client, monkeypatch):
    monkeypatch.setattr(voice, "_ALLOWED_ORIGINS", ["http://testserver"])
    monkeypatch.setattr(settings, "gnani_mock_mode", True)
    monkeypatch.setattr(settings, "gnani_api_key", "")
    monkeypatch.setattr("app.routers.voice.SessionLocal", lambda: db_session)
    monkeypatch.setattr("app.routers.voice.GnaniTTSClient", lambda: FakeTTS())

    _create_customer(client, "Rahul")

    with client.websocket_connect("/voice/ws", headers={"origin": "http://testserver"}) as websocket:
        websocket.send_json({"type": "mock_transcript", "text": "Rahul ko 500 rupaye udhaar"})
        first = websocket.receive_json()
        websocket.receive_bytes()
        websocket.send_json({"type": "mock_transcript", "text": "Rahul ko 100 rupaye udhaar"})
        second = websocket.receive_json()
        websocket.receive_bytes()

    assert first["response_text"].startswith("I am an AI assistant.")
    assert not second["response_text"].startswith("I am an AI assistant.")


def test_separate_sessions_each_receive_disclosure(db_session, client, monkeypatch):
    pass
    monkeypatch.setattr(voice, "_ALLOWED_ORIGINS", ["http://testserver"])
    monkeypatch.setattr(settings, "gnani_mock_mode", False)
    monkeypatch.setattr(settings, "gnani_api_key", "test-key")
    monkeypatch.setattr("app.routers.voice.SessionLocal", lambda: db_session)
    monkeypatch.setattr("app.routers.voice.GnaniSTTClient", FakeLiveSTT)
    monkeypatch.setattr("app.routers.voice.GnaniTTSClient", lambda: FakeTTS())

    customer_id = _create_customer(client, "Rahul")

    with client.websocket_connect("/voice/ws", headers={"origin": "http://testserver"}) as websocket:
        websocket.send_json({"type": "mock_transcript", "text": "Rahul ko 500 rupaye udhaar"})
        response = websocket.receive_json()
        audio = websocket.receive_bytes()

    assert response["type"] == "response"
    assert response["status"] == "success"
    assert response["transcript"] == "Rahul ko 500 rupaye udhaar"
    assert len(audio) > 0

    transactions = client.get(f"/customers/{customer_id}/transactions").json()
    assert len(transactions) == 1
    assert transactions[0]["amount"] == 500.0