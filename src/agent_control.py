from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

import callscoot

CONTROL_DIR = callscoot.STATE_DIR / "control"
SESSION_COMMANDS_DIR = CONTROL_DIR / "session_commands"
PENDING_REQUESTS_PATH = CONTROL_DIR / "pending_call_requests.json"


def ensure_control_dirs() -> None:
    callscoot.ensure_dirs()
    CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    SESSION_COMMANDS_DIR.mkdir(parents=True, exist_ok=True)


def _load_pending_requests() -> list[dict[str, Any]]:
    ensure_control_dirs()
    return callscoot.load_json_file(PENDING_REQUESTS_PATH, [])


def _save_pending_requests(items: list[dict[str, Any]]) -> None:
    ensure_control_dirs()
    callscoot.save_json_file(PENDING_REQUESTS_PATH, items)


def list_pending_call_requests() -> list[dict[str, Any]]:
    now = time.time()
    existing = _load_pending_requests()
    items = [item for item in existing if float(item.get("expires_at") or 0) > now and not item.get("consumed")]
    if len(items) != len(existing):
        _save_pending_requests(items)
    return items


def add_pending_call_request(
    target_number: str | None,
    dynamic_variables: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    ttl_sec: int = 300,
) -> dict[str, Any]:
    ensure_control_dirs()
    request = {
        "request_id": str(uuid.uuid4()),
        "target_number": callscoot.normalize_phone_number(target_number),
        "dynamic_variables": dynamic_variables or {},
        "metadata": metadata or {},
        "created_at": time.time(),
        "expires_at": time.time() + ttl_sec,
        "consumed": False,
    }
    items = list_pending_call_requests()
    items.append(request)
    _save_pending_requests(items)
    return request


def claim_pending_call_request(call_info: dict[str, Any], session_id: str) -> dict[str, Any] | None:
    now = time.time()
    items = _load_pending_requests()
    valid = [item for item in items if float(item.get("expires_at") or 0) > now and not item.get("consumed")]
    number = callscoot.normalize_phone_number(call_info.get("incoming_number"))

    match: dict[str, Any] | None = None
    if number:
        match = next((item for item in valid if item.get("target_number") == number), None)
    if not match:
        unmatched = [item for item in valid if not item.get("target_number")]
        if len(unmatched) == 1:
            match = unmatched[0]

    if match:
        match["consumed"] = True
        match["claimed_at"] = now
        match["claimed_session_id"] = session_id
    _save_pending_requests(valid)
    return match


def queue_session_command(session_id: str, command_type: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    ensure_control_dirs()
    command = {
        "command_id": f"{int(time.time() * 1000)}-{uuid.uuid4().hex}",
        "session_id": session_id,
        "type": command_type,
        "payload": payload or {},
        "created_at": time.time(),
    }
    path = SESSION_COMMANDS_DIR / f"{command['command_id']}.json"
    callscoot.save_json_file(path, command)
    return command


def pop_session_commands(session_id: str) -> list[dict[str, Any]]:
    ensure_control_dirs()
    commands: list[dict[str, Any]] = []
    for path in sorted(SESSION_COMMANDS_DIR.glob("*.json")):
        command = callscoot.load_json_file(path, {})
        if command.get("session_id") != session_id:
            continue
        commands.append(command)
        path.unlink(missing_ok=True)
    return commands
