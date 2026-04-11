from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import Enum
from typing import Any


class AgentState(str, Enum):
    IDLE = "idle"
    GREETING = "greeting"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    WAITING_TOOL = "waiting_tool"
    CONFIRMING = "confirming"
    WRAPPING_UP = "wrapping_up"
    ENDED = "ended"


@dataclass
class StateSnapshot:
    state: AgentState
    reason: str | None = None
    metadata: dict[str, Any] | None = None


class AgentStateMachine:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._state = AgentState.IDLE
        self._turn_count = 0
        self._active_tool: str | None = None

    def set_state(self, state: AgentState, reason: str | None = None, **metadata: Any) -> StateSnapshot:
        with self._lock:
            self._state = state
            if state == AgentState.SPEAKING:
                self._turn_count += 1
            if "active_tool" in metadata:
                self._active_tool = metadata.get("active_tool")
            return self.snapshot(reason=reason, **metadata)

    def snapshot(self, reason: str | None = None, **metadata: Any) -> StateSnapshot:
        with self._lock:
            payload = {
                "agent_turn_count": self._turn_count,
                "active_tool": self._active_tool,
                **metadata,
            }
            return StateSnapshot(state=self._state, reason=reason, metadata=payload)

    @property
    def state(self) -> AgentState:
        with self._lock:
            return self._state

    @property
    def turn_count(self) -> int:
        with self._lock:
            return self._turn_count

    @property
    def active_tool(self) -> str | None:
        with self._lock:
            return self._active_tool
