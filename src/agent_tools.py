from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from agent_memory import MemoryStore

ToolFn = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolFn] = {}

    def register(self, name: str, func: ToolFn) -> None:
        self._tools[name] = func

    async def execute(self, name: str, parameters: dict[str, Any]) -> dict[str, Any]:
        if name not in self._tools:
            return {"status": "error", "message": f"unknown tool: {name}"}
        return await self._tools[name](parameters)

    def names(self) -> list[str]:
        return sorted(self._tools)


def build_default_tools(memory: MemoryStore) -> ToolRegistry:
    registry = ToolRegistry()

    async def get_caller_profile(parameters: dict[str, Any]) -> dict[str, Any]:
        caller_id = str(parameters.get("phone_number") or parameters.get("caller_id") or "").strip()
        profile = memory.get_profile(caller_id)
        if not profile:
            return {"status": "not_found", "caller_id": caller_id}
        return {
            "status": "success",
            "caller_id": profile.caller_id,
            "name": profile.name,
            "tier": profile.tier,
            "notes": profile.notes,
        }

    async def create_ticket(parameters: dict[str, Any]) -> dict[str, Any]:
        issue = str(parameters.get("issue_description") or parameters.get("summary") or "").strip()
        caller_id = str(parameters.get("caller_id") or "").strip() or None
        if caller_id and issue:
            memory.add_memory(caller_id, f"Support issue: {issue}", {"source": "tool:create_ticket"})
        ticket_id = f"TCK-{abs(hash((caller_id, issue))) % 100000:05d}"
        return {"status": "created", "ticket_id": ticket_id, "issue_description": issue}

    async def add_memories(parameters: dict[str, Any]) -> dict[str, Any]:
        caller_id = str(parameters.get("caller_id") or "").strip()
        text = str(parameters.get("text") or parameters.get("memory") or "").strip()
        if not caller_id or not text:
            return {"status": "error", "message": "caller_id and memory text required"}
        memory.add_memory(caller_id, text, {"source": "tool:add_memories"})
        return {"status": "stored"}

    async def retrieve_memories(parameters: dict[str, Any]) -> dict[str, Any]:
        caller_id = str(parameters.get("caller_id") or "").strip()
        limit = int(parameters.get("limit") or 5)
        return {"status": "success", "memories": memory.retrieve_memories(caller_id, limit=limit)}

    registry.register("get_caller_profile", get_caller_profile)
    registry.register("create_ticket", create_ticket)
    registry.register("add_memories", add_memories)
    registry.register("retrieve_memories", retrieve_memories)
    return registry


async def maybe_execute_tool_event(registry: ToolRegistry, message: dict[str, Any]) -> dict[str, Any] | None:
    tool_event = message.get("client_tool_call_event") or message.get("tool_call_event") or {}
    tool_name = tool_event.get("tool_name") or tool_event.get("name")
    if not tool_name:
        return None
    parameters = tool_event.get("parameters") or tool_event.get("arguments") or {}
    if isinstance(parameters, str):
        parameters = {"raw": parameters}
    if not isinstance(parameters, dict):
        parameters = {"value": parameters}
    return await registry.execute(str(tool_name), parameters)
