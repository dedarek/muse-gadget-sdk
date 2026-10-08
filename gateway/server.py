"""Authenticated, provider-neutral StickS3 WS/HTTP gateway. No Muse calls."""
import asyncio
import array
import hmac
import io
import json
import logging
import os
import sys
import shutil
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path

import httpx
from aiohttp import web, WSMsgType
from dotenv import load_dotenv

LOG = logging.getLogger("yyc.gateway")
MAX_AUDIO_BYTES = 16000 * 2 * 10
MAX_TEXT = 2048


class GatewayError(Exception):
    pass


@dataclass
class Settings:
    token: str
    provider: str = "openai-compatible"
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    token_field: str = "max_tokens"
    max_tokens: int = 160
    temperature: float | None = None
    top_p: float | None = None
    enable_thinking: bool | None = None
    asr_base: str = ""
    asr_provider: str = "openai-compatible"
    whisper_model: str = ""
    whisper_cli: str = "whisper-cli"
    asr_language: str = "zh"
    asr_key: str = ""
    asr_model: str = ""
    tts_base: str = ""
    tts_provider: str = "openai-compatible"
    say_voice: str = "Tingting"
    tts_key: str = ""
    tts_model: str = ""
    tts_voice: str = ""
    tts_rate: int = 24000

    @classmethod
    def from_env(cls):
        return cls(
            token=os.getenv("GATEWAY_TOKEN", ""),
            provider=os.getenv("MODEL_PROVIDER", "openai-compatible"),
            base_url=os.getenv("MODEL_BASE_URL", ""),
            api_key=os.getenv("MODEL_API_KEY", ""),
            model=os.getenv("MODEL_NAME", ""),
            token_field=os.getenv("MODEL_TOKEN_FIELD", "max_tokens"),
            max_tokens=int(os.getenv("MODEL_MAX_TOKENS", "160")),
            temperature=float(os.environ["MODEL_TEMPERATURE"]) if os.getenv("MODEL_TEMPERATURE") else None,
            top_p=float(os.environ["MODEL_TOP_P"]) if os.getenv("MODEL_TOP_P") else None,
            enable_thinking=os.environ["MODEL_ENABLE_THINKING"].lower() == "true" if os.getenv("MODEL_ENABLE_THINKING") else None,
            asr_base=os.getenv("ASR_BASE_URL", ""),
            asr_provider=os.getenv("ASR_PROVIDER", "openai-compatible"),
            whisper_model=os.getenv("WHISPER_MODEL_PATH", ""),
            whisper_cli=os.getenv("WHISPER_CLI", "whisper-cli"),
            asr_language=os.getenv("ASR_LANGUAGE", "zh"),
            asr_key=os.getenv("ASR_API_KEY", ""),
            asr_model=os.getenv("ASR_MODEL", ""),
            tts_base=os.getenv("TTS_BASE_URL", ""),
            tts_provider=os.getenv("TTS_PROVIDER", "openai-compatible"),
            say_voice=os.getenv("MACOS_SAY_VOICE", "Tingting"),
            tts_key=os.getenv("TTS_API_KEY", ""),
            tts_model=os.getenv("TTS_MODEL", ""),
            tts_voice=os.getenv("TTS_VOICE", ""),
            tts_rate=int(os.getenv("TTS_SAMPLE_RATE", "24000")),
        )


def endpoint(base, suffix):
    return base.rstrip("/") + "/" + suffix.lstrip("/")


def validate_text(value):
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > MAX_TEXT:
        raise GatewayError("text must be 1..2048 UTF-8 bytes")
    return value.strip()


def pcm_to_wav(pcm):
    if not pcm or len(pcm) % 2 or len(pcm) > MAX_AUDIO_BYTES:
        raise GatewayError("audio must be <=10s of 16 kHz mono PCM16LE")
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(pcm)
    return output.getvalue()


def resample_pcm(pcm, input_rate, output_rate=16000):
    if len(pcm) % 2 or not 8000 <= input_rate <= 96000:
        raise GatewayError("invalid TTS PCM format")
    src = array.array("h")
    src.frombytes(pcm)
    if sys.byteorder != "little":
        src.byteswap()
    count = int(len(src) * output_rate / input_rate)
    if count > output_rate * 30:
        raise GatewayError("TTS audio exceeds 30 seconds")
    if input_rate == output_rate:
        return pcm
    dst = array.array("h")
    for i in range(count):
        position = i * input_rate / output_rate
        low = int(position)
        frac = position - low
        high = min(low + 1, len(src) - 1)
        dst.append(round(src[low] * (1 - frac) + src[high] * frac))
    if sys.byteorder != "little":
        dst.byteswap()
    return dst.tobytes()


async def local_command(*args, input_bytes=None):
    """No shell, no transcript in argv/logs, bounded execution and cleanup."""
    try:
        process = await asyncio.create_subprocess_exec(*args,
            stdin=asyncio.subprocess.PIPE if input_bytes is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except (FileNotFoundError, PermissionError) as error:
        raise GatewayError("local speech executable unavailable") from error
    try:
        stdout, _stderr = await asyncio.wait_for(process.communicate(input_bytes), timeout=60)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if process.returncode:
        raise GatewayError("local speech command failed; check installed voice/model")
    return stdout


def speech_tempdir():
    return tempfile.TemporaryDirectory(prefix="yyc-audio-", dir=os.getenv("SPEECH_TMP_DIR") or None)


def speech_readiness(cfg):
    if cfg.asr_provider == "whisper-cpp":
        asr = bool(cfg.whisper_model and Path(cfg.whisper_model).is_file() and shutil.which(cfg.whisper_cli))
    else:
        asr = bool(cfg.asr_base and cfg.asr_model)
    if cfg.tts_provider == "macos-say":
        tts = sys.platform == "darwin" and bool(shutil.which("say") and shutil.which("ffmpeg"))
    else:
        tts = bool(cfg.tts_base and cfg.tts_model and cfg.tts_voice)
    return {"asr_configured": asr, "tts_configured": tts,
            "asr_provider": cfg.asr_provider, "tts_provider": cfg.tts_provider}


class Providers:
    def __init__(self, cfg, client=None):
        self.cfg = cfg
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(90, connect=10), follow_redirects=False)
        self.owns_client = client is None
        self.local_speech_lock = asyncio.Semaphore(1)

    async def close(self):
        if self.owns_client:
            await self.client.aclose()

    async def chat(self, text):
        text = validate_text(text)
        c = self.cfg
        if not c.base_url or not c.model:
            raise GatewayError("model endpoint/name not configured; no cloud call made")
        system = ("You are a portable voice assistant that answers questions in conversation only. "
                  "Do not claim to operate devices, browse or schedule reminders. "
                  "Reply briefly in the user's language, preferably one sentence, no markdown.")
        if c.provider == "anthropic":
            if not c.api_key:
                raise GatewayError("Claude API key not configured")
            response = await self.client.post(endpoint(c.base_url, "messages"),
                headers={"x-api-key": c.api_key, "anthropic-version": "2023-06-01"},
                json={"model": c.model, "max_tokens": c.max_tokens, "system": system,
                      "messages": [{"role": "user", "content": text}]})
            response.raise_for_status()
            reply = "".join(x.get("text", "") for x in response.json().get("content", []) if x.get("type") == "text")
        elif c.provider == "openai-compatible":
            if c.token_field not in ("max_tokens", "max_completion_tokens"):
                raise GatewayError("invalid MODEL_TOKEN_FIELD")
            headers = {"Authorization": f"Bearer {c.api_key}"} if c.api_key else {}
            payload = {"model": c.model, "messages": [{"role": "system", "content": system},
                {"role": "user", "content": text}], c.token_field: c.max_tokens}
            if c.temperature is not None:
                payload["temperature"] = c.temperature
            if c.top_p is not None:
                payload["top_p"] = c.top_p
            if c.enable_thinking is not None:
                payload["chat_template_kwargs"] = {"enable_thinking": c.enable_thinking}
            response = await self.client.post(endpoint(c.base_url, "chat/completions"), headers=headers,
                json=payload)
            response.raise_for_status()
            reply = response.json()["choices"][0]["message"]["content"]
        else:
            raise GatewayError("MODEL_PROVIDER must be openai-compatible or anthropic")
        if not isinstance(reply, str) or not reply.strip():
            raise GatewayError("provider returned no text")
        # Fit one device JSON message including metadata. Truncate on a UTF-8 boundary.
        return reply.encode("utf-8")[:1800].decode("utf-8", "ignore")

    async def transcribe(self, pcm):
        c = self.cfg
        wav = pcm_to_wav(pcm)
        if c.asr_provider == "whisper-cpp":
            if not c.whisper_model or not Path(c.whisper_model).is_file():
                raise GatewayError("local Whisper model not configured")
            async with self.local_speech_lock:
                with speech_tempdir() as directory:
                    source = Path(directory) / "input.wav"
                    prefix = Path(directory) / "result"
                    source.write_bytes(wav)
                    await local_command(c.whisper_cli, "-m", c.whisper_model, "-f", str(source),
                        "-l", c.asr_language, "-t", "2", "-bs", "1", "-bo", "1",
                        "-nt", "-np", "-oj", "-of", str(prefix))
                    try:
                        result = json.loads(prefix.with_suffix(".json").read_text())
                        text = "".join(segment.get("text", "") for segment in result.get("transcription", []))
                    except (OSError, ValueError, AttributeError) as error:
                        raise GatewayError("local ASR returned invalid output") from error
                    return validate_text(text)
        if c.asr_provider != "openai-compatible":
            raise GatewayError("unsupported ASR_PROVIDER")
        if not c.asr_base or not c.asr_model:
            raise GatewayError("ASR not configured; microphone data was not sent to a provider")
        headers = {"Authorization": f"Bearer {c.asr_key}"} if c.asr_key else {}
        response = await self.client.post(endpoint(c.asr_base, "audio/transcriptions"), headers=headers,
            data={"model": c.asr_model}, files={"file": ("speech.wav", wav, "audio/wav")})
        response.raise_for_status()
        return validate_text(response.json().get("text"))

    async def speech(self, text):
        c = self.cfg
        if c.tts_provider == "macos-say":
            if sys.platform != "darwin":
                raise GatewayError("macos-say requires a Mac Gateway host")
            async with self.local_speech_lock:
                with speech_tempdir() as directory:
                    source = Path(directory) / "reply.aiff"
                    target = Path(directory) / "reply.pcm"
                    await local_command("say", "-v", c.say_voice, "-r", "190", "-o", str(source), "-f", "-",
                                        input_bytes=text.encode("utf-8"))
                    await local_command("ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                        "-i", str(source), "-t", "30", "-ac", "1", "-ar", "16000", "-f", "s16le", str(target))
                    pcm = target.read_bytes()
                    if not pcm or len(pcm) % 2 or len(pcm) > 16000 * 2 * 30:
                        raise GatewayError("local TTS returned invalid PCM")
                    return pcm
        if c.tts_provider != "openai-compatible":
            raise GatewayError("unsupported TTS_PROVIDER")
        if not c.tts_base or not c.tts_model or not c.tts_voice:
            return None
        headers = {"Authorization": f"Bearer {c.tts_key}"} if c.tts_key else {}
        response = await self.client.post(endpoint(c.tts_base, "audio/speech"), headers=headers,
            json={"model": c.tts_model, "input": text, "voice": c.tts_voice, "response_format": "pcm"})
        response.raise_for_status()
        if len(response.content) > 96000 * 2 * 30:
            raise GatewayError("TTS response too large")
        return resample_pcm(response.content, c.tts_rate)


def public_error(error):
    if isinstance(error, GatewayError):
        return str(error)
    if isinstance(error, httpx.HTTPStatusError):
        return f"provider returned HTTP {error.response.status_code}"
    if isinstance(error, (httpx.TimeoutException, asyncio.TimeoutError)):
        return "provider timeout"
    return "provider/transport error; check Gateway configuration"


def create_app(cfg, providers=None):
    if len(cfg.token) < 16 or any(x in cfg.token for x in "\r\n"):
        raise ValueError("GATEWAY_TOKEN must contain at least 16 characters and no newlines")
    provider = providers or Providers(cfg)
    inflight = asyncio.Semaphore(2)

    @web.middleware
    async def auth(request, handler):
        expected = "Bearer " + cfg.token
        if not hmac.compare_digest(request.headers.get("Authorization", ""), expected):
            return web.json_response({"type": "error", "message": "unauthorized"}, status=401)
        return await handler(request)

    app = web.Application(middlewares=[auth], client_max_size=MAX_AUDIO_BYTES + 4096)

    async def health(request):
        return web.json_response({"ok": True, "backend": "yyc", "muse_backend": False,
            "model_configured": bool(cfg.base_url and cfg.model),
            **speech_readiness(cfg)})

    async def chat(request):
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise GatewayError("request must be an object")
            text = validate_text(payload.get("text"))
            async with inflight:
                reply = await provider.chat(text)
            return web.json_response({"type": "reply", "text": reply})
        except (json.JSONDecodeError, UnicodeError):
            return web.json_response({"type": "error", "message": "invalid JSON"}, status=400)
        except Exception as error:
            return web.json_response({"type": "error", "message": public_error(error)}, status=503)

    async def ws_handler(request):
        ws = web.WebSocketResponse(max_msg_size=65536, heartbeat=20, receive_timeout=120)
        await ws.prepare(request)
        async def send_json(value):
            await ws.send_str(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
        audio = bytearray()
        recording = False
        turn = None
        await send_json({"type": "ready", "protocol": 1, "max_audio_seconds": 10})

        async def respond(text=None, pcm=None):
            try:
                async with inflight:
                    if pcm is not None:
                        text = await provider.transcribe(pcm)
                    reply = await provider.chat(text)
                    await send_json({"type": "reply", "text": reply})
                    speech = await provider.speech(reply)
                if speech:
                    await send_json({"type": "audio.start", "sample_rate": 16000, "channels": 1, "format": "pcm16le"})
                    for offset in range(0, len(speech), 1280):
                        await ws.send_bytes(speech[offset:offset + 1280])
                        # Playback-paced delivery avoids filling the device queue.
                        await asyncio.sleep(0.04)
                    await send_json({"type": "audio.end"})
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if not ws.closed:
                    await send_json({"type": "error", "message": public_error(error)})

        async for message in ws:
            try:
                if message.type == WSMsgType.TEXT:
                    obj = json.loads(message.data)
                    if not isinstance(obj, dict):
                        raise GatewayError("message must be an object")
                    kind = obj.get("type")
                    if kind == "telemetry":
                        # Don't persist audio, text, credentials or telemetry.
                        continue
                    if kind in ("text", "audio.start") and turn and not turn.done():
                        raise GatewayError("busy; wait for the current reply")
                    if kind == "text":
                        turn = asyncio.create_task(respond(text=validate_text(obj.get("text"))))
                    elif kind == "audio.start":
                        if obj.get("sample_rate") != 16000 or obj.get("channels") != 1:
                            raise GatewayError("only 16 kHz mono PCM16LE is supported")
                        audio.clear()
                        recording = True
                    elif kind == "audio.end":
                        if not recording or not audio:
                            raise GatewayError("no recording started")
                        recording = False
                        if turn and not turn.done():
                            audio.clear()
                            raise GatewayError("busy; wait for current reply")
                        turn = asyncio.create_task(respond(pcm=bytes(audio)))
                        audio.clear()
                    else:
                        raise GatewayError("unknown message type")
                elif message.type == WSMsgType.BINARY:
                    if not recording or len(message.data) % 2:
                        raise GatewayError("unexpected/invalid PCM data")
                    if len(audio) + len(message.data) > MAX_AUDIO_BYTES:
                        recording = False
                        audio.clear()
                        raise GatewayError("audio exceeds 10 seconds")
                    audio.extend(message.data)
                elif message.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                    break
            except (GatewayError, json.JSONDecodeError) as error:
                await send_json({"type": "error", "message": public_error(error)})
        if turn and not turn.done():
            turn.cancel()
            await asyncio.gather(turn, return_exceptions=True)
        return ws

    async def cleanup(app):
        await provider.close()

    app.router.add_get("/health", health)
    app.router.add_post("/v1/chat", chat)
    app.router.add_get("/ws", ws_handler)
    app.on_cleanup.append(cleanup)
    return app


if __name__ == "__main__":
    load_dotenv(Path(__file__).with_name(".env"))
    logging.basicConfig(level=logging.INFO)
    hosts = [h.strip() for h in os.getenv("HOST", "127.0.0.1").split(",") if h.strip()]
    web.run_app(create_app(Settings.from_env()), host=hosts,
                port=int(os.getenv("PORT", "8787")), access_log=None)
