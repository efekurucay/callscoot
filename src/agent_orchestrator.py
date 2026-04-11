#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import signal
import threading
from pathlib import Path
from typing import Any

import callscoot
import websockets

from agent_events import EventLogger
from agent_memory import MemoryStore
from agent_state import AgentState, AgentStateMachine
from agent_tools import ToolRegistry, build_default_tools
from audio_bridge import PulseAudioCapture, PulseAudioPlayback, drain_audio_queue

logger = logging.getLogger(__name__)

RX_SOURCE = "callscoot.agent.rx.monitor"
TX_SINK = "callscoot.agent.tx"
ENV_PATHS = [Path.cwd() / ".env", callscoot.CONFIG_DIR / "elevenlabs.env", callscoot.CONFIG_DIR / "elevenagents.env"]


class ElevenAgentsError(RuntimeError):
    pass


def configure_logging() -> None:
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="[elevenagents] %(message)s")


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_env() -> None:
    for path in ENV_PATHS:
        load_env_file(path)


def get_env(name: str) -> str:
    load_env()
    value = os.environ.get(name)
    if not value:
        raise ElevenAgentsError(f"missing required environment variable: {name}")
    return value


def get_signed_url(agent_id: str, api_key: str) -> str:
    import urllib.error
    import urllib.request

    url = f"https://api.elevenlabs.io/v1/convai/conversation/get-signed-url?agent_id={agent_id}"
    req = urllib.request.Request(url, headers={"xi-api-key": api_key})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        raise ElevenAgentsError(f"signed URL HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise ElevenAgentsError(f"signed URL request failed: {exc}") from exc
    signed_url = data.get("signed_url")
    if not signed_url:
        raise ElevenAgentsError("signed_url missing in ElevenLabs response")
    return str(signed_url)


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


def bootstrap_audio() -> None:
    ensure_null_sink("callscoot.agent.rx", "CallScoot-AI-RX", 16000, 1)
    ensure_null_sink("callscoot.agent.tx", "CallScoot-AI-TX", 16000, 1)
    main_cfg = callscoot.load_config()
    main_cfg["local_sink"] = "callscoot.agent.rx"
    main_cfg["local_source"] = "callscoot.agent.tx.monitor"
    main_cfg["echo_cancel"] = False
    callscoot.save_config(main_cfg)


def detect_active_call(main_cfg: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    target_mac = callscoot.resolve_target_mac(main_cfg)
    info = {"state": None, "incoming_number": None, "direction": None, "adb_serial": None, "target_mac": target_mac}
    serial = callscoot.adb_serial(None, main_cfg, target_mac=target_mac)
    if serial:
        info = callscoot.android_call_info(None, main_cfg, target_mac=target_mac)
        info["target_mac"] = target_mac
    pair = callscoot.choose_pair(target_mac)
    return info, pair


def _first_text(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def build_dynamic_variables(call_info: dict[str, Any], memory: MemoryStore) -> dict[str, Any]:
    caller_id = callscoot.normalize_phone_number(call_info.get("incoming_number"))
    profile = memory.get_profile(caller_id)
    memories = memory.retrieve_memories(caller_id, limit=3)
    memory_summary = " | ".join(item["memory"] for item in reversed(memories))
    variables = {
        "caller_id": caller_id or "unknown",
        "caller_name": (profile.name if profile else None) or "Bilinmiyor",
        "caller_tier": (profile.tier if profile else None) or "standard",
        "caller_notes": (profile.notes if profile else None) or "",
        "caller_memory_summary": memory_summary,
    }
    return {key: value for key, value in variables.items() if value is not None}


def build_simple_summary(session_id: str) -> str:
    session = callscoot.read_call_session(session_id)
    transcript = session.get("transcript") or []
    if not transcript:
        return "No transcript captured."
    last_user = next((item["text"] for item in reversed(transcript) if item.get("speaker") == "caller"), "")
    last_agent = next((item["text"] for item in reversed(transcript) if item.get("speaker") == "assistant"), "")
    return f"Turns: {len(transcript)} | Last user: {last_user or 'n/a'} | Last agent: {last_agent or 'n/a'}"


async def run_elevenagents_session(
    session_id: str,
    agent_id: str,
    api_key: str,
    audio_queue,
    playback: PulseAudioPlayback,
    stop_flag: threading.Event,
    event_logger: EventLogger,
    state: AgentStateMachine,
    tools: ToolRegistry,
    dynamic_variables: dict[str, Any],
) -> None:
    state.set_state(AgentState.GREETING, reason="session_connecting")
    event_logger.emit("call_started", payload={"dynamic_variables": dynamic_variables})
    logger.info("[WS] requesting signed URL")
    ws_url = get_signed_url(agent_id, api_key)
    logger.info("[WS] connecting")

    async with websockets.connect(ws_url, ping_interval=20, ping_timeout=30, close_timeout=10) as ws:
        logger.info("[WS] connected")
        event_logger.emit("ws_connected")
        callscoot.append_call_event(session_id, "elevenagents_connected")

        await ws.send(
            json.dumps(
                {
                    "type": "conversation_initiation_client_data",
                    "conversation_config_override": {
                        "agent": {"language": "tr"},
                        "tts": {"optimize_streaming_latency": 4},
                    },
                    "dynamic_variables": dynamic_variables,
                }
            )
        )

        loop = asyncio.get_running_loop()

        async def sender() -> None:
            while not stop_flag.is_set():
                try:
                    chunk = await loop.run_in_executor(None, lambda: audio_queue.get(timeout=0.05))
                except queue.Empty:
                    await asyncio.sleep(0.01)
                    continue
                try:
                    await ws.send(json.dumps({"user_audio_chunk": chunk}))
                except websockets.exceptions.ConnectionClosed:
                    return
                finally:
                    audio_queue.task_done()

        async def receiver() -> None:
            while True:
                try:
                    raw_msg = await ws.recv()
                except websockets.exceptions.ConnectionClosed:
                    return

                try:
                    msg = json.loads(raw_msg)
                except json.JSONDecodeError:
                    continue

                msg_type = str(msg.get("type") or "")
                if msg_type == "conversation_initiation_metadata":
                    meta = msg.get("conversation_initiation_metadata_event", {})
                    conversation_id = meta.get("conversation_id")
                    logger.info("[WS] session started: %s", conversation_id)
                    event_logger.emit("conversation_started", payload={"conversation_id": conversation_id})
                    callscoot.append_call_event(session_id, "elevenagents_session_started", conversation_id=conversation_id)
                    state.set_state(AgentState.LISTENING, reason="conversation_started")
                elif msg_type == "audio":
                    state.set_state(AgentState.SPEAKING, reason="audio_chunk")
                    audio_event = msg.get("audio_event", {})
                    audio_b64 = audio_event.get("audio_base_64") or audio_event.get("audio_base64") or ""
                    if audio_b64:
                        playback.write(str(audio_b64))
                elif msg_type == "interruption":
                    state.set_state(AgentState.LISTENING, reason="interruption")
                    reason = _first_text((msg.get("interruption_event") or {}).get("reason"), msg.get("reason"))
                    playback.silence()
                    logger.info("[WS] interruption: %s", reason or "unknown")
                    event_logger.emit("interruption", payload={"reason": reason or None})
                    callscoot.append_call_event(session_id, "elevenagents_interruption", reason=reason or None)
                elif msg_type in {"tentative_user_transcript", "vad_score", "internal_turn_probability", "internal_tentative_agent_response"}:
                    continue
                elif msg_type == "user_transcript":
                    state.set_state(AgentState.THINKING, reason="user_transcript")
                    event = msg.get("user_transcription_event") or msg.get("user_transcript_event") or {}
                    text = _first_text(event.get("user_transcript"), event.get("transcript"), msg.get("user_transcript"), msg.get("transcript"))
                    if text:
                        logger.info("[User] %s", text)
                        event_logger.emit("transcript_final", source="user", payload={"text": text})
                        callscoot.append_call_transcript(session_id, "caller", text, provider="elevenagents")
                elif msg_type == "agent_response":
                    state.set_state(AgentState.SPEAKING, reason="agent_response")
                    event = msg.get("agent_response_event") or {}
                    text = _first_text(event.get("agent_response"), event.get("text"), msg.get("agent_response"), msg.get("text"))
                    if text:
                        logger.info("[Agent] %s", text)
                        event_logger.emit("agent_response_finished", source="agent", payload={"text": text})
                        callscoot.append_call_transcript(session_id, "assistant", text, provider="elevenagents")
                elif msg_type == "agent_response_correction":
                    corrected = msg.get("agent_response_correction_event") or {}
                    event_logger.emit("agent_response_correction", source="agent", payload=corrected if isinstance(corrected, dict) else {})
                elif msg_type == "agent_response_metadata":
                    metadata = msg.get("agent_response_metadata_event") or {}
                    event_logger.emit("agent_response_started", source="agent", payload=metadata if isinstance(metadata, dict) else {})
                elif msg_type == "ping":
                    payload: dict[str, Any] = {"type": "pong"}
                    ping_event = msg.get("ping_event") or {}
                    event_id = ping_event.get("event_id") or msg.get("event_id")
                    if event_id:
                        payload["event_id"] = event_id
                    try:
                        await ws.send(json.dumps(payload))
                    except websockets.exceptions.ConnectionClosed:
                        return
                elif msg_type == "client_tool_call":
                    state.set_state(AgentState.WAITING_TOOL, reason="client_tool_call")
                    tool_call = msg.get("client_tool_call") or {}
                    tool_name = str(tool_call.get("tool_name") or "")
                    parameters = tool_call.get("parameters") or {}
                    if not isinstance(parameters, dict):
                        parameters = {"value": parameters}
                    event_logger.emit("tool_call_started", payload={"tool_name": tool_name, "parameters": parameters})
                    result = await tools.execute(tool_name, parameters)
                    response = {
                        "type": "client_tool_result",
                        "tool_call_id": parameters.get("tool_call_id"),
                        "result": result,
                        "is_error": bool(result.get("status") == "error") if isinstance(result, dict) else False,
                    }
                    await ws.send(json.dumps(response))
                    event_logger.emit("tool_call_finished", payload={"tool_name": tool_name, "result": result})
                    state.set_state(AgentState.THINKING, reason="tool_result")
                elif msg_type == "mcp_tool_call":
                    event_logger.emit("tool_call_started", payload={"tool_type": "mcp", "raw": msg})
                elif msg_type == "client_error":
                    event_logger.emit("client_error", payload=msg)
                else:
                    event_logger.emit("unhandled_event", payload={"type": msg_type})

        async def stopper() -> None:
            await loop.run_in_executor(None, stop_flag.wait)
            await ws.close()

        tasks = [asyncio.create_task(sender()), asyncio.create_task(receiver()), asyncio.create_task(stopper())]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in done:
            exc = task.exception() if not task.cancelled() else None
            if exc:
                raise exc


class ElevenAgentsCallBridge:
    def __init__(self, session_id: str, call_info: dict[str, Any], agent_id: str, api_key: str, memory: MemoryStore) -> None:
        self.session_id = session_id
        self.call_info = call_info
        self.agent_id = agent_id
        self.api_key = api_key
        self.memory = memory
        self.audio_queue: queue.Queue[str] = queue.Queue(maxsize=32)
        self.stop_flag = threading.Event()
        self.capture = PulseAudioCapture(RX_SOURCE, self.audio_queue, self.stop_flag)
        self.playback = PulseAudioPlayback(TX_SINK)
        self.state = AgentStateMachine()
        self.event_logger = EventLogger(session_id, self.state, caller_id=callscoot.normalize_phone_number(call_info.get("incoming_number")))
        self.tools = build_default_tools(memory)
        self.thread: threading.Thread | None = None
        self.error: BaseException | None = None
        self.reconnects = 0
        self.dynamic_variables = build_dynamic_variables(call_info, memory)

    def start(self) -> None:
        self.playback.start()
        self.capture.start()
        self.thread = threading.Thread(target=self._run_session, name="elevenagents-session", daemon=True)
        self.thread.start()
        self.event_logger.emit("agent_session_started", payload={"mode": "elevenagents", "tools": self.tools.names()})
        callscoot.append_call_event(self.session_id, "agent_session_started", config={"mode": "elevenagents", "tools": self.tools.names()})

    def _run_session(self) -> None:
        try:
            while not self.stop_flag.is_set():
                try:
                    asyncio.run(
                        run_elevenagents_session(
                            self.session_id,
                            self.agent_id,
                            self.api_key,
                            self.audio_queue,
                            self.playback,
                            self.stop_flag,
                            self.event_logger,
                            self.state,
                            self.tools,
                            self.dynamic_variables,
                        )
                    )
                    if not self.stop_flag.is_set():
                        self.reconnects += 1
                        logger.info("[WS] session closed, reconnecting (%s)", self.reconnects)
                        self.event_logger.emit("ws_reconnect", payload={"count": self.reconnects})
                        callscoot.append_call_event(self.session_id, "elevenagents_reconnect", count=self.reconnects)
                        drain_audio_queue(self.audio_queue)
                        self.playback.start()
                        self.stop_flag.wait(0.5)
                except Exception as exc:  # noqa: BLE001
                    self.error = exc
                    self.reconnects += 1
                    logger.info("[WS] session error: %s", exc)
                    self.event_logger.emit("client_error", payload={"error": str(exc), "reconnect": self.reconnects})
                    callscoot.append_call_event(self.session_id, "elevenagents_error", error=str(exc), reconnect=self.reconnects)
                    if self.stop_flag.is_set():
                        break
                    drain_audio_queue(self.audio_queue)
                    self.playback.start()
                    self.stop_flag.wait(1.0)
        finally:
            self.capture.stop()
            self.playback.stop()
            self.state.set_state(AgentState.ENDED, reason="bridge_stopped")
            self.event_logger.emit("call_ended")
            self._persist_summary()

    def _persist_summary(self) -> None:
        caller_id = callscoot.normalize_phone_number(self.call_info.get("incoming_number"))
        summary = build_simple_summary(self.session_id)
        self.memory.save_summary(self.session_id, caller_id, summary, structured={"mode": "elevenagents"})
        if caller_id:
            self.memory.add_memory(caller_id, summary, {"source": "call_summary", "session_id": self.session_id})
        callscoot.update_call_session(self.session_id, summary=summary)
        (callscoot.call_session_dir(self.session_id) / "summary.txt").write_text(summary + "\n", encoding="utf-8")

    def stop(self) -> None:
        self.stop_flag.set()
        self.capture.stop()
        self.playback.stop()
        if self.thread:
            self.thread.join(timeout=3)


def run_forever() -> None:
    configure_logging()
    bootstrap_audio()
    agent_id = get_env("ELEVENLABS_AGENT_ID")
    api_key = get_env("ELEVENLABS_API_KEY")
    memory = MemoryStore()

    stop_main = threading.Event()

    def _signal_handler(signum: int, _frame: Any) -> None:
        logger.info("signal %s received, shutting down", signum)
        stop_main.set()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    logger.info("elevenagents loop started")
    bridge: ElevenAgentsCallBridge | None = None
    current_session_id: str | None = None

    while not stop_main.is_set():
        try:
            main_cfg = callscoot.load_config()
            call_info, pair = detect_active_call(main_cfg)
            current = callscoot.load_current_call()
            active_session_id = current.get("id") if current else None
            call_state = call_info.get("state")
            route_active = bool(pair)

            if call_state == "offhook" and route_active and active_session_id:
                if current_session_id != active_session_id or bridge is None:
                    if bridge and current_session_id:
                        bridge.stop()
                    current_session_id = active_session_id
                    bridge = ElevenAgentsCallBridge(active_session_id, call_info, agent_id, api_key, memory)
                    bridge.start()
            else:
                if bridge:
                    bridge.stop()
                    bridge = None
                current_session_id = None
            stop_main.wait(0.1)
        except callscoot.CommandError as exc:
            logger.info("callscoot error: %s", exc)
            stop_main.wait(1.0)

    if bridge:
        bridge.stop()


if __name__ == "__main__":
    run_forever()
