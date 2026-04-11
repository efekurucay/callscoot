#!/usr/bin/env python3
"""
Minimal external app example for CallScoot.

What it does:
- reads leads from a CSV file
- queues one outbound call at a time through the local CallScoot API
- listens to structured call events
- waits for the session to finish
- writes outcome fields back into the same CSV

CSV input columns expected:
- phone
- name
- company (optional)

Output columns written by this script:
- call_status
- session_id
- summary
- last_user_text
- last_agent_text
- completed_at

Example:
  python3 examples/lead_campaign_app.py examples/leads.csv
"""

from __future__ import annotations

import csv
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

API_BASE = "http://127.0.0.1:8788"
POLL_INTERVAL_SEC = 2.0
SESSION_TIMEOUT_SEC = 180


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def http_json(method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(f"{API_BASE}{path}", data=data, method=method)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode("utf-8")
        return json.loads(body) if body else {}


def queue_call(phone: str, name: str | None, company: str | None, row_id: str) -> dict[str, Any]:
    return http_json(
        "POST",
        "/v1/outbound-calls",
        {
            "number": phone,
            "dynamic_variables": {
                "campaign_name": "lead_campaign",
                "contact_name": name or "",
                "company_name": company or "",
            },
            "metadata": {
                "row_id": row_id,
            },
            "ttl_sec": 300,
        },
    )


def current_call() -> dict[str, Any] | None:
    return http_json("GET", "/v1/current-call").get("current_call")


def get_call(session_id: str) -> dict[str, Any]:
    return http_json("GET", f"/v1/calls/{session_id}")


def wait_for_session_start(timeout_sec: int = 30) -> str:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        current = current_call()
        if current and current.get("id"):
            return str(current["id"])
        time.sleep(1.0)
    raise TimeoutError("timed out waiting for session start")


def wait_for_session_end(session_id: str, timeout_sec: int = SESSION_TIMEOUT_SEC) -> dict[str, Any]:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        current = current_call()
        if not current or current.get("id") != session_id:
            return get_call(session_id)
        time.sleep(POLL_INTERVAL_SEC)
    raise TimeoutError(f"timed out waiting for session {session_id} to finish")


def extract_outcome(session: dict[str, Any]) -> dict[str, str]:
    transcript = session.get("transcript") or []
    meta = session.get("meta") or {}
    last_user = next((item["text"] for item in reversed(transcript) if item.get("speaker") == "caller"), "")
    last_agent = next((item["text"] for item in reversed(transcript) if item.get("speaker") == "assistant"), "")
    return {
        "call_status": "completed",
        "session_id": str(meta.get("id") or ""),
        "summary": str(meta.get("summary") or ""),
        "last_user_text": last_user,
        "last_agent_text": last_agent,
        "completed_at": utc_now_iso(),
    }


def process_leads(csv_path: Path) -> None:
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    fieldnames = list(rows[0].keys()) if rows else ["phone", "name", "company"]
    for extra in ["call_status", "session_id", "summary", "last_user_text", "last_agent_text", "completed_at"]:
        if extra not in fieldnames:
            fieldnames.append(extra)

    for index, row in enumerate(rows, start=1):
        if (row.get("call_status") or "").strip().lower() == "completed":
            continue
        phone = (row.get("phone") or "").strip()
        if not phone:
            row["call_status"] = "missing_phone"
            continue
        name = (row.get("name") or "").strip() or None
        company = (row.get("company") or "").strip() or None
        print(f"[lead-app] queueing call {index}: {phone} ({name or 'unknown'})", flush=True)
        queue_call(phone, name, company, row_id=str(index))
        session_id = wait_for_session_start()
        print(f"[lead-app] active session: {session_id}", flush=True)
        session = wait_for_session_end(session_id)
        row.update(extract_outcome(session))
        with csv_path.open("w", newline="", encoding="utf-8") as fp:
            writer = csv.DictWriter(fp, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        print(f"[lead-app] completed session {session_id}", flush=True)
        time.sleep(2.0)


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python3 examples/lead_campaign_app.py <leads.csv>", file=sys.stderr)
        return 2
    csv_path = Path(sys.argv[1]).expanduser().resolve()
    if not csv_path.exists():
        print(f"file not found: {csv_path}", file=sys.stderr)
        return 1
    try:
        process_leads(csv_path)
        return 0
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"[lead-app] error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
