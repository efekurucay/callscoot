#!/usr/bin/env python3
"""
Minimal external app example for CallScoot.

What it does:
- reads leads from a CSV file
- queues one outbound call at a time through the local CallScoot API
- waits for the session to start and finish
- fetches the final session data
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
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from callscoot_client import CallScootClient, CallScootClientError

POLL_INTERVAL_SEC = 2.0
SESSION_TIMEOUT_SEC = 180


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def build_client() -> CallScootClient:
    base_url = os.environ.get("CALLSCOOT_API_BASE", "http://127.0.0.1:8788")
    return CallScootClient(base_url=base_url, api_token=os.environ.get("CALLSCOOT_API_TOKEN"))


def queue_call(client: CallScootClient, phone: str, name: str | None, company: str | None, row_id: str) -> dict[str, object]:
    return client.queue_outbound_call(
        phone,
        dynamic_variables={
            "campaign_name": "lead_campaign",
            "contact_name": name or "",
            "company_name": company or "",
        },
        metadata={
            "row_id": row_id,
        },
        ttl_sec=300,
    )


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
    client = build_client()
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
        queue_call(client, phone, name, company, row_id=str(index))
        session_id = client.wait_for_session_start()
        print(f"[lead-app] active session: {session_id}", flush=True)
        session = client.wait_for_session_end(session_id, timeout_sec=SESSION_TIMEOUT_SEC, poll_interval_sec=POLL_INTERVAL_SEC)
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
    except (CallScootClientError, TimeoutError) as exc:
        print(f"[lead-app] error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
