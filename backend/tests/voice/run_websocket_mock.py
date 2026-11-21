import asyncio
import json
import websockets


async def test_voice_websocket():
    uri = "ws://127.0.0.1:8010/voice/ws"

    async with websockets.connect(
        uri,
        origin="http://localhost:8010"
    ) as websocket:

        print("WebSocket connected!")

        # Mock voice transcript bhejo
        await websocket.send(json.dumps({
            "type": "mock_transcript",
            "text": "Rahul Sharma ki transaction history batao"
        }))

        # Backend ka JSON response receive karo
        response = await websocket.recv()

        if isinstance(response, str):
            data = json.loads(response)
            print("\nBackend response:")
            print(json.dumps(data, indent=4, ensure_ascii=False))

        # Mock TTS ka WAV audio receive karo
        audio = await websocket.recv()

        if isinstance(audio, bytes):
            print("\nAudio received!")
            print("Audio size:", len(audio), "bytes")
            print("WAV header:", audio[:4])

            assert audio[:4] == b"RIFF"
            print("WAV validation: PASSED")


if __name__ == "__main__":
    asyncio.run(test_voice_websocket())