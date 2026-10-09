import asyncio
import json
import struct
import unittest
import sys
import tempfile
from unittest.mock import patch
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
from aiohttp.test_utils import TestClient, TestServer
from server import (Settings, Providers, create_app, GatewayError,
                    pcm_to_wav, resample_pcm, validate_text, MAX_AUDIO_BYTES)

TOKEN = "test-only-token-not-a-real-secret"


class FakeProviders:
    def __init__(self):
        self.texts = []
        self.audio = []
    async def chat(self, text):
        self.texts.append(text)
        return "测试回复"
    async def transcribe(self, pcm):
        self.audio.append(pcm)
        return "语音测试"
    async def speech(self, text):
        return None
    async def close(self):
        pass


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.provider = FakeProviders()
        self.client = TestClient(TestServer(create_app(Settings(TOKEN), self.provider)))
        await self.client.start_server()
        self.headers = {"Authorization": "Bearer " + TOKEN}
    async def asyncTearDown(self):
        await self.client.close()
    async def test_auth_and_health(self):
        self.assertEqual((await self.client.get("/health")).status, 401)
        response = await self.client.get("/health", headers=self.headers)
        data = await response.json()
        self.assertFalse(data["muse_backend"])
        self.assertFalse(data["model_configured"])
    async def test_http_chat(self):
        r = await self.client.post("/v1/chat", headers=self.headers, json={"text": "你好"})
        self.assertEqual(r.status, 200)
        self.assertEqual((await r.json())["text"], "测试回复")
        self.assertEqual(self.provider.texts, ["你好"])
    async def test_http_invalid_json(self):
        r = await self.client.post("/v1/chat", headers=self.headers, data="{broken")
        self.assertEqual(r.status, 400)
    async def test_ws_text(self):
        ws = await self.client.ws_connect("/ws", headers=self.headers)
        self.assertEqual((await ws.receive_json())["type"], "ready")
        await ws.send_json({"type": "text", "text": "hello"})
        self.assertEqual((await ws.receive_json())["type"], "reply")
        self.assertEqual(self.provider.texts, ["hello"])
        await ws.close()
    async def test_ws_recording(self):
        ws = await self.client.ws_connect("/ws", headers=self.headers)
        await ws.receive_json()
        await ws.send_json({"type": "audio.start", "sample_rate": 16000, "channels": 1})
        await ws.send_bytes(b"\x01\0" * 1600)
        await ws.send_json({"type": "audio.end"})
        reply = await ws.receive_json()
        self.assertEqual(reply["type"], "reply")
        self.assertEqual(len(self.provider.audio[0]), 3200)
        await ws.close()
    async def test_ws_audio_reply(self):
        async def speech(text):
            return bytes(2560)
        self.provider.speech = speech
        ws = await self.client.ws_connect("/ws", headers=self.headers)
        await ws.receive_json()
        await ws.send_json({"type": "text", "text": "hello"})
        self.assertEqual((await ws.receive_json())["type"], "reply")
        start = await ws.receive_json()
        self.assertEqual(start["sample_rate"], 16000)
        self.assertEqual(start["channels"], 1)
        payload = (await ws.receive()).data + (await ws.receive()).data
        self.assertEqual(len(payload), 2560)
        self.assertEqual((await ws.receive_json())["type"], "audio.end")
        await ws.close()
    async def test_invalid_audio_and_json(self):
        ws = await self.client.ws_connect("/ws", headers=self.headers)
        await ws.receive_json()
        for message in ("{broken", "[]"):
            await ws.send_str(message)
            self.assertEqual((await ws.receive_json())["type"], "error")
        await ws.send_bytes(b"\0\0")
        self.assertEqual((await ws.receive_json())["type"], "error")
        await ws.send_json({"type": "audio.start", "sample_rate": 24000, "channels": 1})
        self.assertEqual((await ws.receive_json())["type"], "error")
        await ws.close()
    async def test_audio_limit(self):
        ws = await self.client.ws_connect("/ws", headers=self.headers)
        await ws.receive_json()
        await ws.send_json({"type": "audio.start", "sample_rate": 16000, "channels": 1})
        for _ in range(6):
            await ws.send_bytes(bytes(60000))
        self.assertEqual((await ws.receive_json())["type"], "error")
        self.assertEqual(self.provider.audio, [])
        await ws.close()
    async def test_busy(self):
        hold = asyncio.Event()
        async def slow(text):
            await hold.wait()
            return "ok"
        self.provider.chat = slow
        ws = await self.client.ws_connect("/ws", headers=self.headers)
        await ws.receive_json()
        await ws.send_json({"type": "text", "text": "first"})
        await ws.send_json({"type": "text", "text": "second"})
        error = await ws.receive_json()
        self.assertIn("busy", error["message"])
        hold.set()
        self.assertEqual((await ws.receive_json())["type"], "reply")
        await ws.close()


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_chinese_input_prompt_and_reply_are_simplified(self):
        def handler(request):
            payload=json.loads(request.content)
            messages=payload.get("messages", [])
            self.assertEqual(messages[-1]["content"], "请介绍自己")
            self.assertIn("Simplified Chinese", payload.get("system", messages[0]["content"]))
            if request.url.path.endswith("messages"):
                return httpx.Response(200, json={"content":[{"type":"text", "text":"這是語音助手的測試。"}]})
            return httpx.Response(200, json={"choices":[{"message":{"content":"這是語音助手的測試。"}}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            for backend in ("anthropic", "openai-compatible"):
                cfg=Settings(TOKEN, provider=backend, base_url="https://test.invalid/v1", api_key="test-key", model="test")
                self.assertEqual(await Providers(cfg,client).chat("請介紹自己"), "这是语音助手的测试。")

    async def test_cloud_asr_is_simplified(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200,json={"text":"你好，請簡單介紹你自己。"}))) as client:
            cfg=Settings(TOKEN,asr_base="https://test.invalid/v1",asr_model="test")
            self.assertEqual(await Providers(cfg,client).transcribe(bytes(3200)), "你好，请简单介绍你自己。")

    async def test_custom_model_parameters(self):
        def handler(request):
            data = json.loads(request.content)
            self.assertEqual(data["max_tokens"], 512)
            self.assertEqual(data["temperature"], .7)
            self.assertEqual(data["top_p"], .95)
            self.assertEqual(data["chat_template_kwargs"], {"enable_thinking": False})
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            cfg = Settings(TOKEN, base_url="https://test.invalid/v1", model="test-model", max_tokens=512,
                temperature=.7, top_p=.95, enable_thinking=False)
            self.assertEqual(await Providers(cfg, client).chat("hello"), "ok")
    async def test_local_whisper_temporary_files_removed(self):
        seen = []
        async def command(*args, **kwargs):
            wav = Path(args[args.index("-f") + 1])
            self.assertEqual(wav.read_bytes()[:4], b"RIFF")
            seen.append(wav)
            prefix = Path(args[args.index("-of") + 1])
            prefix.with_suffix(".json").write_text(json.dumps({"transcription": [{"text": " 本地識別測試 "}]}))
        with tempfile.TemporaryDirectory() as directory:
            model = Path(directory) / "test-model.bin"
            model.write_bytes(b"mocked model")
            cfg = Settings(TOKEN, asr_provider="whisper-cpp", whisper_model=str(model))
            p = Providers(cfg)
            with patch("server.local_command", side_effect=command):
                self.assertEqual(await p.transcribe(bytes(3200)), "本地识别测试")
            self.assertTrue(all(not path.exists() for path in seen))
            await p.close()
    async def test_local_tts_passes_text_stdin_and_cleans_up(self):
        seen = []
        async def command(*args, **kwargs):
            if args[0] == "say":
                self.assertEqual(kwargs["input_bytes"], "你好".encode())
                self.assertNotIn("你好", args)
                target = Path(args[args.index("-o") + 1])
                target.write_bytes(b"mock AIFF")
            else:
                self.assertEqual(args[0], "ffmpeg")
                target = Path(args[-1])
                target.write_bytes(bytes(3200))
            seen.append(target)
        p = Providers(Settings(TOKEN, tts_provider="macos-say"))
        with patch("server.sys.platform", "darwin"), patch("server.local_command", side_effect=command):
            self.assertEqual(len(await p.speech("你好")), 3200)
        self.assertTrue(all(not path.exists() for path in seen))
        await p.close()
    async def test_missing_local_asr_model_is_error(self):
        p = Providers(Settings(TOKEN, asr_provider="whisper-cpp"))
        with self.assertRaises(GatewayError):
            await p.transcribe(bytes(3200))
        await p.close()
    async def test_openai_compatible(self):
        def handler(request):
            self.assertEqual(request.url.path, "/v1/chat/completions")
            self.assertEqual(request.headers["authorization"], "Bearer test-key")
            data = json.loads(request.content)
            self.assertEqual(data["model"], "test-model")
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            p = Providers(Settings(TOKEN, base_url="https://test.invalid/v1", api_key="test-key", model="test-model"), client)
            self.assertEqual(await p.chat("hello"), "ok")
    async def test_claude(self):
        def handler(request):
            self.assertEqual(request.url.path, "/v1/messages")
            self.assertEqual(request.headers["x-api-key"], "test-key")
            self.assertEqual(json.loads(request.content)["max_tokens"], 160)
            return httpx.Response(200, json={"content": [{"type": "text", "text": "Claude test"}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            p = Providers(Settings(TOKEN, provider="anthropic", base_url="https://test.invalid/v1", api_key="test-key", model="test-model"), client)
            self.assertEqual(await p.chat("hello"), "Claude test")
    async def test_missing_model_does_not_call(self):
        p = Providers(Settings(TOKEN))
        with self.assertRaises(GatewayError):
            await p.chat("hello")
        with self.assertRaises(GatewayError):
            await p.transcribe(bytes(3200))
        await p.close()
    async def test_asr_and_tts_format(self):
        def handler(request):
            if request.url.path.endswith("transcriptions"):
                self.assertIn(b"RIFF", request.content)
                return httpx.Response(200, json={"text": "ASR test"})
            self.assertEqual(json.loads(request.content)["response_format"], "pcm")
            return httpx.Response(200, content=bytes(48000))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            cfg = Settings(TOKEN, asr_base="https://test.invalid/v1", asr_model="test-asr",
                tts_base="https://test.invalid/v1", tts_model="test-tts", tts_voice="test-voice")
            p = Providers(cfg, client)
            self.assertEqual(await p.transcribe(bytes(3200)), "ASR test")
            self.assertEqual(len(await p.speech("hello")), 32000)


class FormatTests(unittest.TestCase):
    def test_audio_and_limits(self):
        self.assertEqual(pcm_to_wav(bytes(3200))[:4], b"RIFF")
        for invalid in (b"", b"\0", bytes(MAX_AUDIO_BYTES + 2)):
            with self.assertRaises(GatewayError):
                pcm_to_wav(invalid)
        self.assertEqual(resample_pcm(struct.pack("<hhh", 100, 200, 300), 24000), struct.pack("<hh", 100, 250))
    def test_unicode_limit(self):
        with self.assertRaises(GatewayError):
            validate_text("中" * 700)
    def test_weak_token_rejected(self):
        with self.assertRaises(ValueError):
            create_app(Settings("short"))
    def test_oversized_same_rate_tts_rejected(self):
        with self.assertRaises(GatewayError):
            resample_pcm(bytes(16000 * 2 * 31), 16000)


if __name__ == "__main__":
    unittest.main()
