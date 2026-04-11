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
from agent_control import add_pending_call_request, list_pending_call_requests, queue_session_command

HOST = os.environ.get("CALLSCOOT_API_HOST", "127.0.0.1")
PORT = int(os.environ.get("CALLSCOOT_API_PORT", "8788"))
API_TOKEN = os.environ.get("CALLSCOOT_API_TOKEN")
EVENT_POLL_INTERVAL = 0.5
EVENT_STREAM_TIMEOUT_SEC = 60


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def normalize_session_id(value: str | None) -> str | None:
    if not value or value == "current":
        current = callscoot.load_current_call()
        return current.get("id") if current else None
    return value


def event_log_path(session_id: str) -> Path:
    return callscoot.call_session_dir(session_id) / "agent_events.jsonl"


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
                    },
                    "config": callscoot.load_config(),
                    "current_call": callscoot.load_current_call(),
                    "pending_call_requests": list_pending_call_requests(),
                },
            )
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
                dial_number(number)
                self._send_json(202, {"queued": True, "dialing": True, "request": request})
            else:
                self._send_json(202, {"queued": True, "dialing": False, "request": request})
            return

        if path == "/v1/current-call/contextual-update":
            current = callscoot.load_current_call()
            text = str(payload.get("text") or "").strip()
            if not current:
                self._send_json(409, {"error": "no active call"})
                return
            if not text:
                self._send_json(400, {"error": "text is required"})
                return
            command = queue_session_command(current["id"], "contextual_update", {"text": text})
            self._send_json(202, {"queued": True, "command": command})
            return

        if path == "/v1/current-call/user-message":
            current = callscoot.load_current_call()
            text = str(payload.get("text") or "").strip()
            if not current:
                self._send_json(409, {"error": "no active call"})
                return
            if not text:
                self._send_json(400, {"error": "text is required"})
                return
            command = queue_session_command(current["id"], "user_message", {"text": text})
            self._send_json(202, {"queued": True, "command": command})
            return

        if path == "/v1/current-call/hangup":
            if not callscoot.load_current_call():
                self._send_json(409, {"error": "no active call"})
                return
            hangup_current_call()
            self._send_json(202, {"queued": True})
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
        while time.time() - started < EVENT_STREAM_TIMEOUT_SEC:
            if path.exists():
                lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
                while sent < len(lines):
                    event = lines[sent]
                    self.wfile.write(f"data: {event}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    sent += 1
            current = callscoot.load_current_call()
            if current is None and sent > 0:
                break
            time.sleep(EVENT_POLL_INTERVAL)

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
