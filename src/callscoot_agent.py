#!/usr/bin/env python3
import argparse
import json
import os
import select
import subprocess
import tempfile
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
DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "rx_sink": "callscoot.agent.rx",
    "tx_sink": "callscoot.agent.tx",
    "sample_rate": 16000,
    "channels": 1,
    "echo_cancel": False,
    "system_prompt": "You are a concise Turkish phone assistant. Be helpful, short, and natural.",
    "history_turns": 12,
    "turn_silence_sec": 1.2,
    "min_speech_sec": 0.7,
    "vad_threshold": 550,
    "poll_interval_sec": 1.0,
    "summary_enabled": True,
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


def multipart_request(url: str, fields: dict[str, str], file_field: str, file_path: Path, mime_type: str, headers: dict[str, str] | None = None) -> bytes:
    boundary = f"callscoot-{uuid.uuid4().hex}"
    body = bytearray()
    for key, value in fields.items():
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode())
        body.extend(str(value).encode())
        body.extend(b"\r\n")
    body.extend(f"--{boundary}\r\n".encode())
    body.extend(
        f'Content-Disposition: form-data; name="{file_field}"; filename="{file_path.name}"\r\n'.encode()
    )
    body.extend(f"Content-Type: {mime_type}\r\n\r\n".encode())
    body.extend(file_path.read_bytes())
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


class MockTranscriber:
    def transcribe(self, wav_path: Path, cfg: dict[str, Any]) -> str:
        duration = wav_duration(wav_path)
        return f"[mock transcript {duration:.1f}s]"


class OpenAITranscriber:
    def transcribe(self, wav_path: Path, cfg: dict[str, Any]) -> str:
        api_key = cfg.get("openai_api_key") or os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise AgentError("OPENAI_API_KEY is missing")
        base = str(cfg.get("openai_base_url") or DEFAULTS["openai_base_url"]).rstrip("/")
        response = multipart_request(
            f"{base}/audio/transcriptions",
            {
                "model": str(cfg.get("stt_model") or DEFAULTS["stt_model"]),
                "language": str(cfg.get("stt_language") or DEFAULTS["stt_language"]),
            },
            "file",
            wav_path,
            "audio/wav",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        data = json.loads(response.decode("utf-8"))
        return str(data.get("text") or "").strip()


class WhisperCliTranscriber:
    def transcribe(self, wav_path: Path, cfg: dict[str, Any]) -> str:
        command = str(cfg.get("whisper_command") or DEFAULTS["whisper_command"])
        model_path = cfg.get("whisper_model_path")
        if not model_path:
            raise AgentError("whisper_model_path is missing")
        out_dir = Path(tempfile.mkdtemp(prefix="callscoot-whisper-"))
        out_base = out_dir / "segment"
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
        api_key = cfg.get("openai_api_key") or os.environ.get("OPENAI_API_KEY")
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
        api_key = cfg.get("openai_api_key") or os.environ.get("OPENAI_API_KEY")
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

    def feed(self, chunk: bytes) -> list[bytes]:
        if not chunk:
            return []
        rms = pcm_rms(chunk)
        segments: list[bytes] = []
        if rms >= self.threshold:
            if not self.in_speech:
                self.in_speech = True
                self.buffer = bytearray()
                self.silence_bytes = 0
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
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as fp:
        temp_path = Path(fp.name)
    try:
        with wave.open(str(temp_path), "wb") as wav_fp:
            wav_fp.setnchannels(1)
            wav_fp.setsampwidth(2)
            wav_fp.setframerate(sample_rate)
            wav_fp.writeframes(bytes(pcm))
        return temp_path.read_bytes()
    finally:
        temp_path.unlink(missing_ok=True)


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
    raise AgentError(f"unsupported TTS provider: {provider}")


def play_wav_bytes(wav_bytes: bytes, sink: str) -> None:
    temp_path = Path(tempfile.mktemp(prefix="callscoot-agent-", suffix=".wav"))
    temp_path.write_bytes(wav_bytes)
    try:
        run(["paplay", "--device", sink, str(temp_path)])
    finally:
        temp_path.unlink(missing_ok=True)


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
        log("capture started")

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

    def maybe_announce(self, session_id: str) -> None:
        announcement = self.cfg.get("announce_on_answer")
        if not announcement or self.announced_session_id == session_id:
            return
        audio = self.tts.synthesize(str(announcement), self.cfg)
        play_wav_bytes(audio, str(self.cfg["tx_sink"]))
        callscoot.append_call_event(session_id, "assistant_announcement", text=announcement)
        self.announced_session_id = session_id

    def handle_segment(self, pcm: bytes, session_id: str) -> None:
        with tempfile.NamedTemporaryFile(prefix="callscoot-agent-segment-", suffix=".wav", delete=False) as fp:
            wav_path = Path(fp.name)
        try:
            write_wav(wav_path, pcm, sample_rate=int(self.cfg["sample_rate"]), channels=int(self.cfg["channels"]))
            transcript = str(self.transcriber.transcribe(wav_path, self.cfg)).strip()
            if not transcript:
                return
            callscoot.append_call_transcript(session_id, "caller", transcript, provider=self.cfg.get("stt_provider"))
            self.history.append({"role": "user", "content": transcript})
            reply = str(self.llm.reply(build_messages(self.cfg, self.history), self.cfg)).strip()
            if not reply:
                return
            self.history.append({"role": "assistant", "content": reply})
            callscoot.append_call_transcript(session_id, "assistant", reply, provider=self.cfg.get("llm_provider"))
            wav_bytes = self.tts.synthesize(reply, self.cfg)
            play_wav_bytes(wav_bytes, str(self.cfg["tx_sink"]))
            callscoot.append_call_event(session_id, "assistant_reply", text=reply, tts_provider=self.cfg.get("tts_provider"))
        finally:
            wav_path.unlink(missing_ok=True)

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
                    if self.current_session_id:
                        self.finalize_session(self.current_session_id)
                    self.current_session_id = active_session_id
                    self.history = []
                    self.announced_session_id = None
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
                        chunk = os.read(self.capture.stdout.fileno(), 3200)
                        if chunk:
                            for segment in self.segmenter.feed(chunk):
                                self.handle_segment(segment, active_session_id)
                        else:
                            self.stop_capture()
                    continue
            else:
                if self.current_session_id:
                    if self.segmenter:
                        for segment in self.segmenter.flush():
                            self.handle_segment(segment, self.current_session_id)
                    self.stop_capture()
                    self.finalize_session(self.current_session_id)
                    self.current_session_id = None
                    self.history = []
                    self.announced_session_id = None
            time.sleep(float(self.cfg.get("poll_interval_sec") or 1.0))


def run_cmd(_: argparse.Namespace) -> None:
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
    p.add_argument("--tts-provider", choices=["mock", "openai", "espeak"])
    p.add_argument("--tts-model")
    p.add_argument("--tts-voice")
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
