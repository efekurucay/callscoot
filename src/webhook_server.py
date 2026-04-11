#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import hmac
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import callscoot
from agent_memory import MemoryStore

HOST = os.environ.get("CALLSCOOT_WEBHOOK_HOST", "127.0.0.1")
PORT = int(os.environ.get("CALLSCOOT_WEBHOOK_PORT", "8787"))
WEBHOOK_SECRET = os.environ.get("ELEVENLABS_WEBHOOK_SECRET")


def verify_signature(raw_body: bytes, signature: str | None) -> bool:
    if not WEBHOOK_SECRET:
        return True
    if not signature:
        return False
    digest = hmac.new(WEBHOOK_SECRET.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature.strip(), digest)


class Handler(BaseHTTPRequestHandler):
    server_version = "CallScootWebhook/1.0"

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/webhook":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or "0")
        raw_body = self.rfile.read(length)
        signature = self.headers.get("elevenlabs-signature")
        if not verify_signature(raw_body, signature):
            self.send_error(401, "invalid signature")
            return
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except json.JSONDecodeError:
            self.send_error(400, "invalid json")
            return

        callscoot.ensure_dirs()
        out_dir = callscoot.STATE_DIR / "webhooks"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "post_call_transcription.jsonl"
        with out_path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(payload, ensure_ascii=False) + "\n")

        session_id = payload.get("conversation_id") or payload.get("session_id")
        caller_id = payload.get("caller_id") or payload.get("from")
        summary = payload.get("summary") or payload.get("analysis") or payload.get("structured_data")
        if session_id and summary:
            MemoryStore().save_summary(str(session_id), str(caller_id) if caller_id else None, json.dumps(summary, ensure_ascii=False) if isinstance(summary, dict) else str(summary), payload if isinstance(payload, dict) else {})

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"[webhook-server] listening on http://{HOST}:{PORT}/webhook", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
