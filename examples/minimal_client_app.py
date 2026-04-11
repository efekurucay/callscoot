#!/usr/bin/env python3
"""
Minimal CallScoot client example.

Usage:
  python3 examples/minimal_client_app.py +905551112233
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from callscoot_client import CallScootClient, CallScootClientError


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python3 examples/minimal_client_app.py <phone>", file=sys.stderr)
        return 2
    client = CallScootClient(
        base_url=os.environ.get("CALLSCOOT_API_BASE", "http://127.0.0.1:8788"),
        api_token=os.environ.get("CALLSCOOT_API_TOKEN"),
    )
    phone = sys.argv[1]
    try:
        client.health()
        client.queue_outbound_call(
            phone,
            dynamic_variables={
                "campaign_name": "minimal_demo",
            },
            metadata={
                "source": "examples/minimal_client_app.py",
            },
        )
        session_id = client.wait_for_session_start()
        print(f"[minimal-client] session started: {session_id}", flush=True)
        session = client.wait_for_session_end(session_id)
        summary = ((session.get("meta") or {}).get("summary") or "").strip()
        print(f"[minimal-client] session finished: {session_id}", flush=True)
        if summary:
            print(f"[minimal-client] summary: {summary}", flush=True)
        return 0
    except (CallScootClientError, TimeoutError) as exc:
        print(f"[minimal-client] error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
