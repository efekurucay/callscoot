#!/usr/bin/env python3
import argparse
import io
import json
import os
import queue
import select
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import wave
from pathlib import Path
from typing import Any

import callscoot

APP = "callscoot-agent"
AGENT_CONFIG_PATH = callscoot.CONFIG_DIR / "agent.json"
OPENAI_ENV_PATH = callscoot.CONFIG_DIR / "openai.env"
DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "rx_sink": "callscoot.agent.rx",
    "tx_sink": "callscoot.agent.tx",
    "sample_rate": 16000,
    "channels": 1,
    "echo_cancel": False,
    "system_prompt": "You are a concise Turkish phone assistant. Be helpful, short, and natural.",
    "history_turns": 6,
    "turn_silence_sec": 0.25,
    "min_speech_sec": 0.15,
    "vad_threshold": 550,
    "poll_interval_sec": 0.03,
    "summary_enabled": False,
    "announce_on_answer": None,
    "stt_provider": "mock",
    "stt_model": "whisper-1",
    "stt_language": "tr",
    "whisper_command": "whisper-cli",
    "whisper_model_path": None,
    "llm_provider": "mock",
    "llm_model": "gpt-4o-mini",
    "llm_temperature": 0.2,
    "tts_provider": "mock",
    "tts_model": "gpt-4o-mini-tts",
    "tts_voice": "alloy",
    "elevenlabs_api_key": None,
    "elevenlabs_voice_id": "pNInz6obpgDQGcFmaJgB",
    "elevenlabs_model": "eleven_flash_v2_5",
    "elevenlabs_stability": 0.4,
    "elevenlabs_similarity": 0.8,
    "elevenlabs_style": 0.0,
    "elevenlabs_output_format": "pcm_16000",
    "openai_api_key": None,
    "openai_base_url": "https://api.openai.com/v1",
    "ollama_base_url": "http://127.0.0.1:11434",
}


class AgentError(RuntimeError):
    pass


def log(message: str) -> None:
    print(f"[{APP}] {message}", flush=True)


def load_agent_config() -> dict[str, Any]:
    callscoot.ensure_dirs()
    cfg = DEFAULTS.copy()
    cfg.update(callscoot.load_json_file(AGENT_CONFIG_PATH, {}))
    return cfg


def save_agent_config(cfg: dict[str, Any]) -> None:
    callscoot.ensure_dirs()
    callscoot.save_json_file(AGENT_CONFIG_PATH, cfg)


def run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(cmd, check=False, text=True, capture_output=True)
    except FileNotFoundError as exc:
        raise AgentError(f"command not found: {cmd[0]}") from exc
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or f"exit code {result.returncode}").strip()
        raise AgentError(f"{' '.join(cmd)} -> {detail}")
    return result


def run_input(cmd: list[str], data: bytes, check: bool = True) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(cmd, input=data, check=False, capture_output=True)
    except FileNotFoundError as exc:
        raise AgentError(f"command not found: {cmd[0]}") from exc
    if check and result.returncode != 0:
        raw_detail = result.stderr or result.stdout
        detail = raw_detail.decode("utf-8", errors="ignore").strip() if raw_detail else f"exit code {result.returncode}"
        raise AgentError(f"{' '.join(cmd)} -> {detail}")
    return result


def load_simple_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    data: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip().strip('"').strip("'")
    return data


def openai_api_key(cfg: dict[str, Any]) -> str | None:
    return cfg.get("openai_api_key") or os.environ.get("OPENAI_API_KEY") or load_simple_env_file(OPENAI_ENV_PATH).get("OPENAI_API_KEY")


def http_json(url: str, payload: dict[str, Any], headers: dict[str, str] | None = None) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST")
    request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise AgentError(f"HTTP {exc.code} for {url}: {exc.read().decode('utf-8', errors='ignore')}") from exc
    except urllib.error.URLError as exc:
        raise AgentError(f"request failed for {url}: {exc}") from exc


def multipart_bytes_request(
    url: str,
    fields: dict[str, str],
    file_field: str,
    file_name: str,
    file_bytes: bytes,
    mime_type: str,
    headers: dict[str, str] | None = None,
) -> bytes:
    boundary = f"callscoot-{uuid.uuid4().hex}"
    body = bytearray()
    for key, value in fields.items():
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode())
        body.extend(str(value).encode())
        body.extend(b"\r\n")
    body.extend(f"--{boundary}\r\n".encode())
    body.extend(f'Content-Disposition: form-data; name="{file_field}"; filename="{file_name}"\r\n'.encode())
    body.extend(f"Content-Type: {mime_type}\r\n\r\n".encode())
    body.extend(file_bytes)
    body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode())

    request = urllib.request.Request(url, data=bytes(body), method="POST")
    request.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise AgentError(f"HTTP {exc.code} for {url}: {exc.read().decode('utf-8', errors='ignore')}") from exc
    except urllib.error.URLError as exc:
        raise AgentError(f"request failed for {url}: {exc}") from exc


def multipart_request(url: str, fields: dict[str, str], file_field: str, file_path: Path, mime_type: str, headers: dict[str, str] | None = None) -> bytes:
    return multipart_bytes_request(url, fields, file_field, file_path.name, file_path.read_bytes(), mime_type, headers=headers)


def wav_input_bytes(wav_input: Path | bytes) -> bytes:
    if isinstance(wav_input, Path):
        return wav_input.read_bytes()
    if isinstance(wav_input, bytearray):
        return bytes(wav_input)
    if isinstance(wav_input, bytes):
        return wav_input
    raise AgentError(f"unsupported wav input type: {type(wav_input)!r}")


def wav_duration_bytes(wav_bytes: bytes) -> float:
    with wave.open(io.BytesIO(wav_bytes), "rb") as wav_fp:
        frames = wav_fp.getnframes()
        rate = wav_fp.getframerate()
        return frames / float(rate or 1)


def pcm_to_wav_bytes(pcm: bytes, sample_rate: int, channels: int = 1) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_fp:
        wav_fp.setnchannels(channels)
        wav_fp.setsampwidth(2)
        wav_fp.setframerate(sample_rate)
        wav_fp.writeframes(pcm)
    return buffer.getvalue()


def wav_bytes_to_pcm(wav_bytes: bytes) -> tuple[bytes, int, int, int]:
    with wave.open(io.BytesIO(wav_bytes), "rb") as wav_fp:
        channels = wav_fp.getnchannels()
        sample_width = wav_fp.getsampwidth()
        sample_rate = wav_fp.getframerate()
        pcm = wav_fp.readframes(wav_fp.getnframes())
    return pcm, sample_rate, channels, sample_width


class MockTranscriber:
    def transcribe(self, wav_input: Path | bytes, cfg: dict[str, Any]) -> str:
        duration = wav_duration_bytes(wav_input_bytes(wav_input))
        return f"[mock transcript {duration:.1f}s]"


class OpenAITranscriber:
    def transcribe(self, wav_input: Path | bytes, cfg: dict[str, Any]) -> str:
        api_key = openai_api_key(cfg)
        if not api_key:
            raise AgentError("OPENAI_API_KEY is missing")
        base = str(cfg.get("openai_base_url") or DEFAULTS["openai_base_url"]).rstrip("/")
        response = multipart_bytes_request(
            f"{base}/audio/transcriptions",
            {
                "model": str(cfg.get("stt_model") or DEFAULTS["stt_model"]),
                "language": str(cfg.get("stt_language") or DEFAULTS["stt_language"]),
            },
            "file",
            "segment.wav",
            wav_input_bytes(wav_input),
            "audio/wav",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        data = json.loads(response.decode("utf-8"))
        return str(data.get("text") or "").strip()


class WhisperCliTranscriber:
    def transcribe(self, wav_input: Path | bytes, cfg: dict[str, Any]) -> str:
        command = str(cfg.get("whisper_command") or DEFAULTS["whisper_command"])
        model_path = cfg.get("whisper_model_path")
        if not model_path:
            raise AgentError("whisper_model_path is missing")
        with tempfile.NamedTemporaryFile(prefix="callscoot-whisper-in-", suffix=".wav", delete=False) as fp:
            wav_path = Path(fp.name)
            fp.write(wav_input_bytes(wav_input))
        out_dir = Path(tempfile.mkdtemp(prefix="callscoot-whisper-"))
        out_base = out_dir / "segment"
        try:
            cmd = [
                command,
                "-m",
                str(model_path),
                "-f",
                str(wav_path),
                "-l",
                str(cfg.get("stt_language") or DEFAULTS["stt_language"]),
                "-otxt",
                "-of",
                str(out_base),
                "-np",
            ]
            run(cmd)
            text_path = out_base.with_suffix(".txt")
            return text_path.read_text(encoding="utf-8", errors="ignore").strip() if text_path.exists() else ""
        finally:
            wav_path.unlink(missing_ok=True)


class MockLLM:
    def reply(self, messages: list[dict[str, str]], cfg: dict[str, Any]) -> str:
        last_user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        return f"Mock yanıt: {last_user[:120]}".strip()

    def summarize(self, transcript: list[dict[str, Any]], cfg: dict[str, Any]) -> str:
        caller_turns = [entry["text"] for entry in transcript if entry.get("speaker") == "caller"]
        assistant_turns = [entry["text"] for entry in transcript if entry.get("speaker") == "assistant"]
        return (
            f"Mock summary\n"
            f"- caller_turns: {len(caller_turns)}\n"
            f"- assistant_turns: {len(assistant_turns)}\n"
            f"- last_caller: {(caller_turns[-1] if caller_turns else 'n/a')}"
        )


class OpenAILLM:
    def _chat(self, messages: list[dict[str, str]], cfg: dict[str, Any]) -> str:
        api_key = openai_api_key(cfg)
        if not api_key:
            raise AgentError("OPENAI_API_KEY is missing")
        base = str(cfg.get("openai_base_url") or DEFAULTS["openai_base_url"]).rstrip("/")
        data = http_json(
            f"{base}/chat/completions",
            {
                "model": str(cfg.get("llm_model") or DEFAULTS["llm_model"]),
                "temperature": float(cfg.get("llm_temperature") or DEFAULTS["llm_temperature"]),
                "messages": messages,
            },
            headers={"Authorization": f"Bearer {api_key}"},
        )
        choices = data.get("choices") or []
        if not choices:
            return ""
        message = choices[0].get("message") or {}
        return str(message.get("content") or "").strip()

    def reply(self, messages: list[dict[str, str]], cfg: dict[str, Any]) -> str:
        return self._chat(messages, cfg)

    def summarize(self, transcript: list[dict[str, Any]], cfg: dict[str, Any]) -> str:
        transcript_text = "\n".join(f"{entry.get('speaker')}: {entry.get('text')}" for entry in transcript)
        messages = [
            {"role": "system", "content": "Summarize this phone call briefly in Turkish with bullet points."},
            {"role": "user", "content": transcript_text or "No transcript available."},
        ]
        return self._chat(messages, cfg)


class OllamaLLM:
    def reply(self, messages: list[dict[str, str]], cfg: dict[str, Any]) -> str:
        base = str(cfg.get("ollama_base_url") or DEFAULTS["ollama_base_url"]).rstrip("/")
        data = http_json(
            f"{base}/api/chat",
            {
                "model": str(cfg.get("llm_model") or "llama3.1:8b"),
                "messages": messages,
                "stream": False,
            },
        )
        message = data.get("message") or {}
        return str(message.get("content") or "").strip()

    def summarize(self, transcript: list[dict[str, Any]], cfg: dict[str, Any]) -> str:
        transcript_text = "\n".join(f"{entry.get('speaker')}: {entry.get('text')}" for entry in transcript)
        return self.reply(
            [
                {"role": "system", "content": "Summarize this phone call briefly in Turkish with bullet points."},
                {"role": "user", "content": transcript_text or "No transcript available."},
            ],
            cfg,
        )


class MockTTS:
    def synthesize(self, text: str, cfg: dict[str, Any]) -> bytes:
        duration = max(0.4, min(2.0, len(text) * 0.03))
        return sine_wav_bytes(duration_sec=duration, sample_rate=int(cfg.get("sample_rate") or 16000), frequency=660)


class OpenAITTS:
    def synthesize(self, text: str, cfg: dict[str, Any]) -> bytes:
        api_key = openai_api_key(cfg)
        if not api_key:
            raise AgentError("OPENAI_API_KEY is missing")
        base = str(cfg.get("openai_base_url") or DEFAULTS["openai_base_url"]).rstrip("/")
        payload = {
            "model": str(cfg.get("tts_model") or DEFAULTS["tts_model"]),
            "voice": str(cfg.get("tts_voice") or DEFAULTS["tts_voice"]),
            "input": text,
            "response_format": "wav",
        }
        request = urllib.request.Request(
            f"{base}/audio/speech",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
        )
        request.add_header("Content-Type", "application/json")
        request.add_header("Authorization", f"Bearer {api_key}")
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            raise AgentError(f"HTTP {exc.code} for OpenAI TTS: {exc.read().decode('utf-8', errors='ignore')}") from exc
        except urllib.error.URLError as exc:
            raise AgentError(f"OpenAI TTS request failed: {exc}") from exc


class EspeakTTS:
    def synthesize(self, text: str, cfg: dict[str, Any]) -> bytes:
        wav_path = Path(tempfile.mktemp(prefix="callscoot-espeak-", suffix=".wav"))
        run(["espeak-ng", "-s", "160", "-w", str(wav_path), text])
        data = wav_path.read_bytes()
        wav_path.unlink(missing_ok=True)
        return data


class ElevenLabsTTS:
    """
    ElevenLabs HTTP Streaming TTS sağlayıcısı.

    /v1/text-to-speech/{voice_id}/stream endpoint'ini kullanır.
    output_format=pcm_16000 ile raw S16LE PCM döner, ardından
    bu veri WAV başlığı eklenerek döndürülür.

    Stdlib'den başka bağımlılık gerektirmez.
    """

    BASE_URL = "https://api.elevenlabs.io/v1"

    def synthesize(self, text: str, cfg: dict[str, Any]) -> bytes:
        api_key = cfg.get("elevenlabs_api_key") or os.environ.get("ELEVENLABS_API_KEY")
        if not api_key:
            raise AgentError(
                "ElevenLabs API anahtarı eksik. "
                "'callscoot-agent configure --elevenlabs-api-key KEY' "
                "veya ELEVENLABS_API_KEY ortam değişkeni ile ver."
            )

        voice_id = cfg.get("elevenlabs_voice_id") or DEFAULTS["elevenlabs_voice_id"]
        model_id = cfg.get("elevenlabs_model") or DEFAULTS["elevenlabs_model"]
        out_fmt = cfg.get("elevenlabs_output_format") or DEFAULTS["elevenlabs_output_format"]

        payload = {
            "text": text,
            "model_id": model_id,
            "voice_settings": {
                "stability": float(cfg.get("elevenlabs_stability", DEFAULTS["elevenlabs_stability"])),
                "similarity_boost": float(cfg.get("elevenlabs_similarity", DEFAULTS["elevenlabs_similarity"])),
                "style": float(cfg.get("elevenlabs_style", DEFAULTS["elevenlabs_style"])),
                "use_speaker_boost": False,
            },
        }

        url = f"{self.BASE_URL}/text-to-speech/{voice_id}/stream?output_format={urllib.parse.quote(str(out_fmt))}"
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
        )
        req.add_header("Content-Type", "application/json")
        req.add_header("xi-api-key", str(api_key))

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                content_type = str(resp.headers.get("content-type") or "")
                pcm_bytes = resp.read()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="ignore")
            raise AgentError(f"ElevenLabs TTS HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise AgentError(f"ElevenLabs TTS bağlantı hatası: {exc}") from exc

        if "audio/pcm" not in content_type:
            raise AgentError(f"ElevenLabs beklenmeyen içerik tipi döndürdü: {content_type or 'unknown'}")

        sample_rate = self._parse_sample_rate(str(out_fmt))
        return self._pcm_to_wav(pcm_bytes, sample_rate=sample_rate, channels=1)

    @staticmethod
    def _parse_sample_rate(output_format: str) -> int:
        mapping = {
            "pcm_16000": 16000,
            "pcm_22050": 22050,
            "pcm_24000": 24000,
            "pcm_44100": 44100,
        }
        return mapping.get(output_format, 16000)

    @staticmethod
    def _pcm_to_wav(pcm: bytes, sample_rate: int, channels: int = 1) -> bytes:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(channels)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(pcm)
        return buf.getvalue()


def pcm_rms(chunk: bytes) -> int:
    if not chunk:
        return 0
    import array
    import math

    samples = array.array("h")
    samples.frombytes(chunk[: len(chunk) - (len(chunk) % 2)])
    if not samples:
        return 0
    power = sum(sample * sample for sample in samples) / len(samples)
    return int(math.sqrt(power))


class SpeechSegmenter:
    def __init__(self, sample_rate: int, threshold: int, min_speech_sec: float, silence_sec: float) -> None:
        self.sample_rate = sample_rate
        self.threshold = threshold
        self.min_speech_bytes = int(sample_rate * 2 * min_speech_sec)
        self.silence_bytes_limit = int(sample_rate * 2 * silence_sec)
        self.buffer = bytearray()
        self.in_speech = False
        self.silence_bytes = 0
        self.just_started_speech = False

    def feed(self, chunk: bytes) -> list[bytes]:
        self.just_started_speech = False
        if not chunk:
            return []
        rms = pcm_rms(chunk)
        segments: list[bytes] = []
        if rms >= self.threshold:
            if not self.in_speech:
                self.in_speech = True
                self.buffer = bytearray()
                self.silence_bytes = 0
                self.just_started_speech = True
            self.buffer.extend(chunk)
            self.silence_bytes = 0
            return segments

        if self.in_speech:
            self.buffer.extend(chunk)
            self.silence_bytes += len(chunk)
            if self.silence_bytes >= self.silence_bytes_limit:
                data = bytes(self.buffer)
                self.buffer = bytearray()
                self.in_speech = False
                self.silence_bytes = 0
                if len(data) >= self.min_speech_bytes:
                    segments.append(data)
        return segments

    def flush(self) -> list[bytes]:
        if self.in_speech and len(self.buffer) >= self.min_speech_bytes:
            data = bytes(self.buffer)
            self.buffer = bytearray()
            self.in_speech = False
            self.silence_bytes = 0
            return [data]
        self.buffer = bytearray()
        self.in_speech = False
        self.silence_bytes = 0
        return []


def sine_wav_bytes(duration_sec: float, sample_rate: int = 16000, frequency: int = 660) -> bytes:
    import math
    import struct

    frame_count = max(1, int(duration_sec * sample_rate))
    pcm = bytearray()
    for index in range(frame_count):
        value = int(14000 * math.sin(2 * math.pi * frequency * index / sample_rate))
        pcm.extend(struct.pack("<h", value))
    return pcm_to_wav_bytes(bytes(pcm), sample_rate=sample_rate, channels=1)


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as wav_fp:
        frames = wav_fp.getnframes()
        rate = wav_fp.getframerate()
        return frames / float(rate or 1)


def write_wav(path: Path, pcm: bytes, sample_rate: int, channels: int = 1) -> None:
    with wave.open(str(path), "wb") as wav_fp:
        wav_fp.setnchannels(channels)
        wav_fp.setsampwidth(2)
        wav_fp.setframerate(sample_rate)
        wav_fp.writeframes(pcm)


def ensure_null_sink(name: str, description: str, sample_rate: int, channels: int) -> None:
    if callscoot.sink_exists(name):
        return
    callscoot.pulse_module_load(
        "module-null-sink",
        sink_name=name,
        sink_properties=f"device.description={description}",
        rate=sample_rate,
        channels=channels,
        channel_map="mono" if channels == 1 else None,
    )


def bootstrap_audio(cfg: dict[str, Any], update_callscoot_cfg: bool = True) -> dict[str, Any]:
    ensure_null_sink(str(cfg["rx_sink"]), "CallScoot-AI-RX", int(cfg["sample_rate"]), int(cfg["channels"]))
    ensure_null_sink(str(cfg["tx_sink"]), "CallScoot-AI-TX", int(cfg["sample_rate"]), int(cfg["channels"]))
    if update_callscoot_cfg:
        main_cfg = callscoot.load_config()
        main_cfg["local_sink"] = cfg["rx_sink"]
        main_cfg["local_source"] = f"{cfg['tx_sink']}.monitor"
        main_cfg["echo_cancel"] = bool(cfg.get("echo_cancel", False))
        callscoot.save_config(main_cfg)
    return {
        "rx_sink": cfg["rx_sink"],
        "rx_monitor": f"{cfg['rx_sink']}.monitor",
        "tx_sink": cfg["tx_sink"],
        "tx_monitor": f"{cfg['tx_sink']}.monitor",
    }


def build_transcriber(cfg: dict[str, Any]):
    provider = str(cfg.get("stt_provider") or DEFAULTS["stt_provider"])
    if provider == "mock":
        return MockTranscriber()
    if provider == "openai":
        return OpenAITranscriber()
    if provider == "whisper_cli":
        return WhisperCliTranscriber()
    raise AgentError(f"unsupported STT provider: {provider}")


def build_llm(cfg: dict[str, Any]):
    provider = str(cfg.get("llm_provider") or DEFAULTS["llm_provider"])
    if provider == "mock":
        return MockLLM()
    if provider == "openai":
        return OpenAILLM()
    if provider == "ollama":
        return OllamaLLM()
    raise AgentError(f"unsupported LLM provider: {provider}")


def build_tts(cfg: dict[str, Any]):
    provider = str(cfg.get("tts_provider") or DEFAULTS["tts_provider"])
    if provider == "mock":
        return MockTTS()
    if provider == "openai":
        return OpenAITTS()
    if provider == "espeak":
        return EspeakTTS()
    if provider == "elevenlabs":
        return ElevenLabsTTS()
    raise AgentError(f"unsupported TTS provider: {provider}")


def pulse_sample_format(sample_width: int) -> str:
    formats = {1: "u8", 2: "s16le", 4: "s32le"}
    fmt = formats.get(sample_width)
    if not fmt:
        raise AgentError(f"unsupported PCM sample width: {sample_width}")
    return fmt


def queue_put_latest(work_queue: queue.Queue, item: Any) -> None:
    try:
        work_queue.put_nowait(item)
        return
    except queue.Full:
        try:
            work_queue.get_nowait()
            work_queue.task_done()
        except queue.Empty:
            pass
    work_queue.put_nowait(item)


def drain_queue(work_queue: queue.Queue) -> None:
    while True:
        try:
            work_queue.get_nowait()
            work_queue.task_done()
        except queue.Empty:
            return


class PlaybackPipe:
    def __init__(self, sink: str) -> None:
        self.sink = sink
        self.proc: subprocess.Popen | None = None
        self.rate: int | None = None
        self.channels: int | None = None
        self.sample_width: int | None = None
        self.lock = threading.Lock()

    def _stop_locked(self) -> None:
        if self.proc and self.proc.poll() is None:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.terminate()
            try:
                self.proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=1)
        self.proc = None
        self.rate = None
        self.channels = None
        self.sample_width = None

    def _start_locked(self, rate: int, channels: int, sample_width: int) -> None:
        self.proc = subprocess.Popen(
            [
                "pacat",
                "--playback",
                f"--device={self.sink}",
                f"--rate={rate}",
                f"--channels={channels}",
                f"--format={pulse_sample_format(sample_width)}",
                "--raw",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.rate = rate
        self.channels = channels
        self.sample_width = sample_width

    def interrupt(self) -> None:
        with self.lock:
            self._stop_locked()

    def close(self) -> None:
        self.interrupt()

    def write_wav_bytes(self, wav_bytes: bytes) -> None:
        pcm, rate, channels, sample_width = wav_bytes_to_pcm(wav_bytes)
        self.write_pcm_bytes(pcm, rate, channels, sample_width)

    def write_pcm_bytes(self, pcm: bytes, rate: int, channels: int, sample_width: int = 2) -> None:
        with self.lock:
            if (
                self.proc is None
                or self.proc.poll() is not None
                or self.rate != rate
                or self.channels != channels
                or self.sample_width != sample_width
            ):
                self._stop_locked()
                self._start_locked(rate, channels, sample_width)
            assert self.proc and self.proc.stdin
            try:
                self.proc.stdin.write(pcm)
                self.proc.stdin.flush()
            except BrokenPipeError:
                self._stop_locked()
                self._start_locked(rate, channels, sample_width)
                assert self.proc and self.proc.stdin
                self.proc.stdin.write(pcm)
                self.proc.stdin.flush()


def play_wav_bytes(wav_bytes: bytes, sink: str) -> None:
    run_input(["pacat", "--playback", f"--device={sink}", "--file-format=wav"], wav_bytes)


def build_messages(cfg: dict[str, Any], history: list[dict[str, str]]) -> list[dict[str, str]]:
    turns = int(cfg.get("history_turns") or DEFAULTS["history_turns"])
    return [{"role": "system", "content": str(cfg.get("system_prompt") or DEFAULTS["system_prompt"])}] + history[-turns:]


def detect_active_call(main_cfg: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    target_mac = callscoot.resolve_target_mac(main_cfg)
    info = {"state": None, "incoming_number": None, "direction": None, "adb_serial": None, "target_mac": target_mac}
    serial = callscoot.adb_serial(None, main_cfg, target_mac=target_mac)
    if serial:
        info = callscoot.android_call_info(None, main_cfg, target_mac=target_mac)
        info["target_mac"] = target_mac
    pair = callscoot.choose_pair(target_mac)
    return info, pair


def summarize_session(session_id: str, llm: Any, cfg: dict[str, Any]) -> str:
    session = callscoot.read_call_session(session_id)
    transcript = session.get("transcript") or []
    if not transcript:
        return "No transcript captured."
    try:
        return str(llm.summarize(transcript, cfg)).strip()
    except Exception as exc:  # noqa: BLE001
        return f"Summary unavailable: {exc}"


def respond_once(text: str, cfg: dict[str, Any]) -> dict[str, Any]:
    llm = build_llm(cfg)
    tts = build_tts(cfg)
    messages = build_messages(cfg, [{"role": "user", "content": text}])
    reply = llm.reply(messages, cfg)
    audio = tts.synthesize(reply, cfg)
    return {"input": text, "reply": reply, "audio_bytes": len(audio)}


def configure_cmd(args: argparse.Namespace) -> None:
    cfg = load_agent_config()
    changed = False
    fields = [
        "enabled",
        "rx_sink",
        "tx_sink",
        "sample_rate",
        "channels",
        "echo_cancel",
        "system_prompt",
        "history_turns",
        "turn_silence_sec",
        "min_speech_sec",
        "vad_threshold",
        "poll_interval_sec",
        "summary_enabled",
        "announce_on_answer",
        "stt_provider",
        "stt_model",
        "stt_language",
        "whisper_command",
        "whisper_model_path",
        "llm_provider",
        "llm_model",
        "llm_temperature",
        "tts_provider",
        "tts_model",
        "tts_voice",
        "elevenlabs_api_key",
        "elevenlabs_voice_id",
        "elevenlabs_model",
        "elevenlabs_stability",
        "elevenlabs_similarity",
        "elevenlabs_style",
        "elevenlabs_output_format",
        "openai_api_key",
        "openai_base_url",
        "ollama_base_url",
    ]
    for field in fields:
        value = getattr(args, field, None)
        if value is not None:
            cfg[field] = value
            changed = True
    if isinstance(cfg.get("enabled"), str):
        cfg["enabled"] = cfg["enabled"] == "on"
    if isinstance(cfg.get("echo_cancel"), str):
        cfg["echo_cancel"] = cfg["echo_cancel"] == "on"
    if isinstance(cfg.get("summary_enabled"), str):
        cfg["summary_enabled"] = cfg["summary_enabled"] == "on"
    if changed:
        save_agent_config(cfg)
    print(json.dumps(cfg, indent=2))


def status_cmd(_: argparse.Namespace) -> None:
    cfg = load_agent_config()
    main_cfg = callscoot.load_config()
    print(
        json.dumps(
            {
                "agent_config": cfg,
                "callscoot_config": main_cfg,
                "audio": {
                    "rx_sink_exists": callscoot.sink_exists(str(cfg["rx_sink"])),
                    "rx_monitor_exists": callscoot.source_exists(f"{cfg['rx_sink']}.monitor"),
                    "tx_sink_exists": callscoot.sink_exists(str(cfg["tx_sink"])),
                    "tx_monitor_exists": callscoot.source_exists(f"{cfg['tx_sink']}.monitor"),
                },
                "current_call": callscoot.load_current_call(),
                "openai_env_path": str(OPENAI_ENV_PATH),
                "openai_key_configured": bool(openai_api_key(cfg)),
                "elevenlabs_key_configured": bool(
                    cfg.get("elevenlabs_api_key") or os.environ.get("ELEVENLABS_API_KEY")
                ),
            },
            indent=2,
        )
    )


def bootstrap_cmd(args: argparse.Namespace) -> None:
    cfg = load_agent_config()
    result = bootstrap_audio(cfg, update_callscoot_cfg=not args.no_configure_callscoot)
    print(json.dumps(result, indent=2))


def reply_cmd(args: argparse.Namespace) -> None:
    cfg = load_agent_config()
    result = respond_once(args.text, cfg)
    print(json.dumps(result, indent=2))


def speak_cmd(args: argparse.Namespace) -> None:
    cfg = load_agent_config()
    tts = build_tts(cfg)
    audio = tts.synthesize(args.text, cfg)
    play_wav_bytes(audio, str(cfg["tx_sink"]))
    print(json.dumps({"spoken": True, "bytes": len(audio)}, indent=2))


class AgentRuntime:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self.main_cfg = callscoot.load_config()
        self.transcriber = build_transcriber(cfg)
        self.llm = build_llm(cfg)
        self.tts = build_tts(cfg)
        self.capture: subprocess.Popen | None = None
        self.segmenter: SpeechSegmenter | None = None
        self.history: list[dict[str, str]] = []
        self.current_session_id: str | None = None
        self.announced_session_id: str | None = None
        self.capture_q: queue.Queue = queue.Queue(maxsize=8)
        self.reply_q: queue.Queue = queue.Queue(maxsize=8)
        self.state_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.generation = 0
        self.tx = PlaybackPipe(str(self.cfg["tx_sink"]))
        frame_bytes = int(self.cfg["channels"]) * 2
        self.read_chunk_bytes = max(frame_bytes, int(int(self.cfg["sample_rate"]) * frame_bytes * 0.05))
        self.segment_worker = threading.Thread(target=self.segment_worker_loop, name="callscoot-segment-worker", daemon=True)
        self.playback_worker = threading.Thread(target=self.playback_worker_loop, name="callscoot-playback-worker", daemon=True)
        self.segment_worker.start()
        self.playback_worker.start()

    def start_capture(self) -> None:
        monitor = f"{self.cfg['rx_sink']}.monitor"
        if self.capture and self.capture.poll() is None:
            return
        self.capture = subprocess.Popen(
            [
                "pacat",
                "--record",
                f"--device={monitor}",
                f"--rate={self.cfg['sample_rate']}",
                f"--channels={self.cfg['channels']}",
                "--format=s16le",
                "--raw",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self.segmenter = SpeechSegmenter(
            sample_rate=int(self.cfg["sample_rate"]),
            threshold=int(self.cfg["vad_threshold"]),
            min_speech_sec=float(self.cfg["min_speech_sec"]),
            silence_sec=float(self.cfg["turn_silence_sec"]),
        )
        log(f"capture started (chunk={self.read_chunk_bytes} bytes)")

    def stop_capture(self) -> None:
        if self.capture and self.capture.poll() is None:
            self.capture.terminate()
            try:
                self.capture.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.capture.kill()
                self.capture.wait(timeout=2)
        self.capture = None
        self.segmenter = None

    def is_active_generation(self, session_id: str, generation: int) -> bool:
        with self.state_lock:
            return self.current_session_id == session_id and self.generation == generation

    def activate_session(self, session_id: str) -> None:
        drain_queue(self.capture_q)
        drain_queue(self.reply_q)
        self.tx.interrupt()
        with self.state_lock:
            self.generation += 1
            self.current_session_id = session_id
            self.history = []
            self.announced_session_id = None

    def finish_session(self, session_id: str) -> None:
        drain_queue(self.capture_q)
        drain_queue(self.reply_q)
        self.tx.interrupt()
        with self.state_lock:
            self.generation += 1
            if self.current_session_id == session_id:
                self.current_session_id = None
            self.history = []
            self.announced_session_id = None
        self.finalize_session(session_id)

    def barge_in(self, session_id: str) -> None:
        with self.state_lock:
            if self.current_session_id != session_id:
                return
            self.generation += 1
            generation = self.generation
        drain_queue(self.capture_q)
        drain_queue(self.reply_q)
        self.tx.interrupt()
        log(f"barge-in: cancelled pending audio for session {session_id} (generation={generation})")

    def maybe_announce(self, session_id: str) -> None:
        announcement = self.cfg.get("announce_on_answer")
        if not announcement:
            return
        with self.state_lock:
            if self.current_session_id != session_id or self.announced_session_id == session_id:
                return
            generation = self.generation
            self.announced_session_id = session_id
        queue_put_latest(
            self.reply_q,
            {
                "session_id": session_id,
                "generation": generation,
                "text": str(announcement),
                "event_type": "assistant_announcement",
                "metadata": {},
            },
        )

    def queue_segment(self, pcm: bytes, session_id: str) -> None:
        with self.state_lock:
            generation = self.generation
        queue_put_latest(self.capture_q, {"session_id": session_id, "generation": generation, "pcm": pcm})

    def process_segment(self, task: dict[str, Any]) -> None:
        session_id = str(task["session_id"])
        generation = int(task["generation"])
        pcm = bytes(task["pcm"])
        if not self.is_active_generation(session_id, generation):
            return

        started = time.perf_counter()
        wav_bytes = pcm_to_wav_bytes(pcm, sample_rate=int(self.cfg["sample_rate"]), channels=int(self.cfg["channels"]))

        stt_started = time.perf_counter()
        transcript = str(self.transcriber.transcribe(wav_bytes, self.cfg)).strip()
        stt_ms = (time.perf_counter() - stt_started) * 1000
        if not transcript or not self.is_active_generation(session_id, generation):
            return

        with self.state_lock:
            if self.current_session_id != session_id or self.generation != generation:
                return
            self.history.append({"role": "user", "content": transcript})
            messages = build_messages(self.cfg, list(self.history))
        callscoot.append_call_transcript(session_id, "caller", transcript, provider=self.cfg.get("stt_provider"))

        llm_started = time.perf_counter()
        reply = str(self.llm.reply(messages, self.cfg)).strip()
        llm_ms = (time.perf_counter() - llm_started) * 1000
        if not reply or not self.is_active_generation(session_id, generation):
            return

        with self.state_lock:
            if self.current_session_id != session_id or self.generation != generation:
                return
            self.history.append({"role": "assistant", "content": reply})
        callscoot.append_call_transcript(session_id, "assistant", reply, provider=self.cfg.get("llm_provider"))
        queue_put_latest(
            self.reply_q,
            {
                "session_id": session_id,
                "generation": generation,
                "text": reply,
                "event_type": "assistant_reply",
                "metadata": {"tts_provider": self.cfg.get("tts_provider")},
            },
        )
        total_ms = (time.perf_counter() - started) * 1000
        log(f"segment timings: stt={stt_ms:.0f}ms llm={llm_ms:.0f}ms total={total_ms:.0f}ms")

    def segment_worker_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                task = self.capture_q.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self.process_segment(task)
            except Exception as exc:  # noqa: BLE001
                log(f"segment worker error: {exc}")
            finally:
                self.capture_q.task_done()

    def playback_worker_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                task = self.reply_q.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                session_id = str(task["session_id"])
                generation = int(task["generation"])
                text = str(task["text"])
                event_type = str(task.get("event_type") or "assistant_reply")
                metadata = dict(task.get("metadata") or {})
                if not self.is_active_generation(session_id, generation):
                    continue
                tts_started = time.perf_counter()
                wav_bytes = self.tts.synthesize(text, self.cfg)
                tts_ms = (time.perf_counter() - tts_started) * 1000
                if not self.is_active_generation(session_id, generation):
                    continue
                play_started = time.perf_counter()
                self.tx.write_wav_bytes(wav_bytes)
                play_ms = (time.perf_counter() - play_started) * 1000
                if self.is_active_generation(session_id, generation):
                    callscoot.append_call_event(session_id, event_type, text=text, **metadata)
                log(f"reply timings: tts={tts_ms:.0f}ms play={play_ms:.0f}ms bytes={len(wav_bytes)}")
            except Exception as exc:  # noqa: BLE001
                log(f"playback worker error: {exc}")
            finally:
                self.reply_q.task_done()

    def finalize_session(self, session_id: str) -> None:
        if not session_id or not bool(self.cfg.get("summary_enabled", True)):
            return
        summary = summarize_session(session_id, self.llm, self.cfg)
        callscoot.update_call_session(session_id, summary=summary)
        summary_path = callscoot.call_session_dir(session_id) / "summary.txt"
        summary_path.write_text(summary + "\n", encoding="utf-8")
        callscoot.append_call_event(session_id, "assistant_summary", summary=summary)

    def run(self) -> None:
        bootstrap_audio(self.cfg, update_callscoot_cfg=True)
        log("agent loop started")
        while True:
            self.main_cfg = callscoot.load_config()
            call_info, pair = detect_active_call(self.main_cfg)
            current = callscoot.load_current_call()
            active_session_id = current.get("id") if current else None
            call_state = call_info.get("state")
            route_active = bool(pair)
            if call_state == "offhook" and route_active and active_session_id:
                if self.current_session_id != active_session_id:
                    previous_session_id = self.current_session_id
                    self.stop_capture()
                    if previous_session_id:
                        self.finish_session(previous_session_id)
                    self.activate_session(active_session_id)
                    callscoot.append_call_event(active_session_id, "agent_session_started", config={
                        "stt_provider": self.cfg.get("stt_provider"),
                        "llm_provider": self.cfg.get("llm_provider"),
                        "tts_provider": self.cfg.get("tts_provider"),
                    })
                self.start_capture()
                self.maybe_announce(active_session_id)
                if self.capture and self.capture.stdout and self.segmenter:
                    ready, _, _ = select.select([self.capture.stdout], [], [], float(self.cfg.get("poll_interval_sec") or 1.0))
                    if ready:
                        chunk = os.read(self.capture.stdout.fileno(), self.read_chunk_bytes)
                        if chunk:
                            segments = self.segmenter.feed(chunk)
                            if self.segmenter.just_started_speech:
                                self.barge_in(active_session_id)
                            for segment in segments:
                                self.queue_segment(segment, active_session_id)
                        else:
                            self.stop_capture()
                    continue
            else:
                if self.current_session_id:
                    finished_session_id = self.current_session_id
                    self.stop_capture()
                    self.finish_session(finished_session_id)
            time.sleep(float(self.cfg.get("poll_interval_sec") or 1.0))


def run_cmd(args: argparse.Namespace) -> None:
    if getattr(args, "mode", "classic") == "elevenagents":
        try:
            from elevenlabs_agent import run_forever as run_elevenagents_forever
        except ImportError as exc:
            raise AgentError("elevenagents mode requires optional dependencies from requirements.txt") from exc
        run_elevenagents_forever()
        return
    cfg = load_agent_config()
    AgentRuntime(cfg).run()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=APP, description="AI pipeline for CallScoot")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("configure", help="show or update agent config")
    p.add_argument("--enabled", choices=["on", "off"])
    p.add_argument("--rx-sink")
    p.add_argument("--tx-sink")
    p.add_argument("--sample-rate", type=int)
    p.add_argument("--channels", type=int)
    p.add_argument("--echo-cancel", choices=["on", "off"])
    p.add_argument("--system-prompt")
    p.add_argument("--history-turns", type=int)
    p.add_argument("--turn-silence-sec", type=float)
    p.add_argument("--min-speech-sec", type=float)
    p.add_argument("--vad-threshold", type=int)
    p.add_argument("--poll-interval-sec", type=float)
    p.add_argument("--summary-enabled", choices=["on", "off"])
    p.add_argument("--announce-on-answer")
    p.add_argument("--stt-provider", choices=["mock", "openai", "whisper_cli"])
    p.add_argument("--stt-model")
    p.add_argument("--stt-language")
    p.add_argument("--whisper-command")
    p.add_argument("--whisper-model-path")
    p.add_argument("--llm-provider", choices=["mock", "openai", "ollama"])
    p.add_argument("--llm-model")
    p.add_argument("--llm-temperature", type=float)
    p.add_argument("--tts-provider", choices=["mock", "openai", "espeak", "elevenlabs"])
    p.add_argument("--tts-model")
    p.add_argument("--tts-voice")
    p.add_argument("--elevenlabs-api-key")
    p.add_argument("--elevenlabs-voice-id")
    p.add_argument("--elevenlabs-model")
    p.add_argument("--elevenlabs-stability", type=float)
    p.add_argument("--elevenlabs-similarity", type=float)
    p.add_argument("--elevenlabs-style", type=float)
    p.add_argument("--elevenlabs-output-format", choices=["pcm_16000", "pcm_22050", "pcm_24000"])
    p.add_argument("--openai-api-key")
    p.add_argument("--openai-base-url")
    p.add_argument("--ollama-base-url")
    p.set_defaults(func=configure_cmd)

    p = sub.add_parser("status", help="show agent status")
    p.set_defaults(func=status_cmd)

    p = sub.add_parser("bootstrap-audio", help="create virtual sinks and point CallScoot at them")
    p.add_argument("--no-configure-callscoot", action="store_true")
    p.set_defaults(func=bootstrap_cmd)

    p = sub.add_parser("reply", help="run the LLM/TTS pipeline once for a text input")
    p.add_argument("text")
    p.set_defaults(func=reply_cmd)

    p = sub.add_parser("speak", help="synthesize text into the TX sink")
    p.add_argument("text")
    p.set_defaults(func=speak_cmd)

    p = sub.add_parser("run", help="run the continuous call agent loop")
    p.add_argument("--mode", choices=["classic", "elevenagents"], default="classic")
    p.set_defaults(func=run_cmd)

    return parser


def main() -> int:
    try:
        parser = build_parser()
        args = parser.parse_args()
        args.func(args)
        return 0
    except (AgentError, callscoot.CommandError) as exc:
        log(str(exc))
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
