#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import callscoot
from agent_control import (
    add_pending_call_request,
    cancel_pending_call_request,
    get_pending_call_request,
    list_pending_call_requests,
    queue_session_command,
)

HOST = os.environ.get("CALLSCOOT_API_HOST", "127.0.0.1")
PORT = int(os.environ.get("CALLSCOOT_API_PORT", "8788"))
API_TOKEN = os.environ.get("CALLSCOOT_API_TOKEN")
EVENT_POLL_INTERVAL = 0.5
EVENT_STREAM_TIMEOUT_SEC = 300


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def normalize_session_id(value: str | None) -> str | None:
    if not value or value == "current":
        current = callscoot.load_current_call()
        return current.get("id") if current else None
    return value


def event_log_path(session_id: str) -> Path:
    return callscoot.call_session_dir(session_id) / "agent_events.jsonl"


def normalize_patch_config(payload: dict[str, Any]) -> dict[str, Any]:
    cfg = callscoot.load_config()
    allowed = set(callscoot.DEFAULTS)
    normalized: dict[str, Any] = {}
    for key, value in payload.items():
        if key not in allowed:
            continue
        if key == "target_device":
            normalized[key] = callscoot.normalize_mac(value) if value else None
        elif key in {"latency_msec", "auto_answer_delay_sec", "max_call_duration_sec", "discoverable_timeout"}:
            normalized[key] = max(0, int(value)) if value is not None else cfg.get(key)
        elif key in {"echo_cancel", "auto_answer", "auto_select_device", "auto_reject_blocked", "log_calls"}:
            normalized[key] = bool(value)
        elif key in {"allowed_callers", "blocked_callers", "business_days"}:
            normalized[key] = list(value or [])
        else:
            normalized[key] = value
    return normalized


def current_or_session_id(raw_session_id: str | None) -> str | None:
    return normalize_session_id(raw_session_id)


def service_is_active(name: str) -> bool:
    result = subprocess.run(["systemctl", "--user", "is-active", name], capture_output=True, text=True, check=False)
    return result.returncode == 0 and (result.stdout or "").strip() == "active"


def dial_number(number: str) -> None:
    cfg = callscoot.load_config()
    target_mac = callscoot.resolve_target_mac(cfg)
    callscoot.adb_cmd(["shell", "am", "start", "-a", "android.intent.action.CALL", "-d", f"tel:{number}"], None, cfg, target_mac=target_mac)


def hangup_current_call() -> None:
    cfg = callscoot.load_config()
    target_mac = callscoot.resolve_target_mac(cfg)
    callscoot.adb_cmd(["shell", "input", "keyevent", "KEYCODE_ENDCALL"], None, cfg, target_mac=target_mac)


class Handler(BaseHTTPRequestHandler):
    server_version = "CallScootAPI/1.0"

    def _authorize(self) -> bool:
        if not API_TOKEN:
            return True
        header = self.headers.get("Authorization") or ""
        expected = f"Bearer {API_TOKEN}"
        if header.strip() == expected:
            return True
        self._send_json(401, {"error": "unauthorized"})
        return False

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8") or "{}")

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = _json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _queue_session_text_command(self, session_id: str | None, command_type: str, text: str) -> tuple[int, dict[str, Any]]:
        resolved = current_or_session_id(session_id)
        if not resolved:
            return 409, {"error": "no active call"}
        if not text:
            return 400, {"error": "text is required"}
        command = queue_session_command(resolved, command_type, {"text": text})
        return 202, {"queued": True, "command": command}

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path in {"/health", "/v1/health"}:
            self._send_json(200, {"ok": True})
            return
        if not self._authorize():
            return
        if path == "/v1/status":
            self._send_json(
                200,
                {
                    "services": {
                        "callscoot-daemon": service_is_active("callscoot-daemon.service"),
                        "callscoot-agent": service_is_active("callscoot-agent.service"),
                        "callscoot-api": service_is_active("callscoot-api.service"),
                    },
                    "config": callscoot.load_config(),
                    "current_call": callscoot.load_current_call(),
                    "pending_call_requests": list_pending_call_requests(),
                },
            )
            return
        if path == "/v1/config":
            self._send_json(200, {"config": callscoot.load_config()})
            return
        if path == "/v1/current-call":
            self._send_json(200, {"current_call": callscoot.load_current_call()})
            return
        if path == "/v1/calls":
            query = parse_qs(parsed.query)
            limit = int((query.get("limit") or [20])[0])
            self._send_json(200, {"calls": callscoot.list_call_sessions(limit=limit)})
            return
        if path == "/v1/pending-call-requests":
            self._send_json(200, {"pending_call_requests": list_pending_call_requests()})
            return
        if path == "/v1/events/stream":
            session_id = normalize_session_id((parse_qs(parsed.query).get("session_id") or ["current"])[0])
            if not session_id:
                self._send_json(404, {"error": "no active session"})
                return
            self._stream_events(session_id)
            return
        if path.startswith("/v1/pending-call-requests/"):
            parts = [part for part in path.split("/") if part]
            if len(parts) == 3:
                request = get_pending_call_request(parts[2])
                if not request:
                    self._send_json(404, {"error": "pending request not found"})
                else:
                    self._send_json(200, {"request": request})
                return
        if path.startswith("/v1/calls/"):
            parts = [part for part in path.split("/") if part]
            if len(parts) == 3:
                session_id = parts[2]
                try:
                    self._send_json(200, callscoot.read_call_session(session_id))
                except FileNotFoundError:
                    self._send_json(404, {"error": "session not found"})
                return
            if len(parts) == 4 and parts[3] == "events":
                session_id = parts[2]
                path_obj = event_log_path(session_id)
                events = []
                if path_obj.exists():
                    events = [json.loads(line) for line in path_obj.read_text(encoding="utf-8").splitlines() if line.strip()]
                self._send_json(200, {"session_id": session_id, "events": events})
                return
        self._send_json(404, {"error": "not found"})

    def do_PATCH(self) -> None:  # noqa: N802
        if not self._authorize():
            return
        parsed = urlparse(self.path)
        if parsed.path != "/v1/config":
            self._send_json(404, {"error": "not found"})
            return
        try:
            payload = self._read_json()
        except json.JSONDecodeError:
            self._send_json(400, {"error": "invalid json"})
            return
        cfg = callscoot.load_config()
        cfg.update(normalize_patch_config(payload))
        callscoot.save_config(cfg)
        self._send_json(200, {"saved": True, "config": cfg})

    def do_DELETE(self) -> None:  # noqa: N802
        if not self._authorize():
            return
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith("/v1/pending-call-requests/"):
            parts = [part for part in path.split("/") if part]
            if len(parts) == 3 and cancel_pending_call_request(parts[2]):
                self._send_json(200, {"deleted": True, "request_id": parts[2]})
            else:
                self._send_json(404, {"error": "pending request not found"})
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorize():
            return
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            payload = self._read_json()
        except json.JSONDecodeError:
            self._send_json(400, {"error": "invalid json"})
            return

        if path in {"/v1/outbound-calls", "/v1/pending-call-requests"}:
            number = str(payload.get("number") or payload.get("target_number") or "").strip() or None
            request = add_pending_call_request(
                target_number=number,
                dynamic_variables=payload.get("dynamic_variables") or {},
                metadata=payload.get("metadata") or {},
                ttl_sec=int(payload.get("ttl_sec") or 300),
            )
            if path == "/v1/outbound-calls":
                if not number:
                    self._send_json(400, {"error": "number is required"})
                    return
                try:
                    dial_number(number)
                except callscoot.CommandError as exc:
                    self._send_json(502, {"error": str(exc), "request": request})
                    return
                self._send_json(202, {"queued": True, "dialing": True, "request": request})
            else:
                self._send_json(202, {"queued": True, "dialing": False, "request": request})
            return

        if path == "/v1/current-call/contextual-update":
            status, response = self._queue_session_text_command("current", "contextual_update", str(payload.get("text") or "").strip())
            self._send_json(status, response)
            return

        if path == "/v1/current-call/user-message":
            status, response = self._queue_session_text_command("current", "user_message", str(payload.get("text") or "").strip())
            self._send_json(status, response)
            return

        if path == "/v1/current-call/hangup":
            if not callscoot.load_current_call():
                self._send_json(409, {"error": "no active call"})
                return
            try:
                hangup_current_call()
            except callscoot.CommandError as exc:
                self._send_json(502, {"error": str(exc)})
                return
            self._send_json(202, {"queued": True})
            return

        if path.startswith("/v1/calls/"):
            parts = [part for part in path.split("/") if part]
            if len(parts) == 4 and parts[3] in {"contextual-update", "user-message"}:
                text = str(payload.get("text") or "").strip()
                command_type = "contextual_update" if parts[3] == "contextual-update" else "user_message"
                status, response = self._queue_session_text_command(parts[2], command_type, text)
                self._send_json(status, response)
                return

        self._send_json(404, {"error": "not found"})

    def _stream_events(self, session_id: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        path = event_log_path(session_id)
        sent = 0
        started = time.time()
        try:
            while time.time() - started < EVENT_STREAM_TIMEOUT_SEC:
                if path.exists():
                    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
                    while sent < len(lines):
                        event = lines[sent]
                        self.wfile.write(f"data: {event}\n\n".encode("utf-8"))
                        self.wfile.flush()
                        sent += 1
                self.wfile.write(b": heartbeat\n\n")
                self.wfile.flush()
                current = callscoot.load_current_call()
                if current is None and sent > 0:
                    break
                time.sleep(EVENT_POLL_INTERVAL)
        except (BrokenPipeError, ConnectionResetError):
            return

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def main() -> None:
    callscoot.ensure_dirs()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"[callscoot-api] listening on http://{HOST}:{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
