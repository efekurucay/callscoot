from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

import callscoot
from agent_state import AgentStateMachine


EVENT_LOG_NAME = "agent_events.jsonl"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class EventLogger:
    def __init__(self, session_id: str, state_machine: AgentStateMachine, caller_id: str | None = None) -> None:
        self.session_id = session_id
        self.state_machine = state_machine
        self.caller_id = caller_id
        self.path = callscoot.call_session_dir(session_id) / EVENT_LOG_NAME

    def emit(self, event_type: str, source: str = "system", payload: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
        snapshot = self.state_machine.snapshot()
        event = {
            "event_id": str(uuid.uuid4()),
            "call_id": self.session_id,
            "caller_id": self.caller_id,
            "timestamp": utc_now_iso(),
            "event_type": event_type,
            "source": source,
            "payload": payload or {},
            "context": {
                "current_state": snapshot.state.value,
                "agent_turn_count": (snapshot.metadata or {}).get("agent_turn_count", 0),
                "active_tool": (snapshot.metadata or {}).get("active_tool"),
            },
            **extra,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(event, ensure_ascii=False) + "\n")
        return event
