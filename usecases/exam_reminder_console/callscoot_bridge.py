from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from callscoot_client import CallScootClient, CallScootClientError  # noqa: E402


def normalize_phone(value: str | None) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.startswith("+"):
        digits = "+" + "".join(ch for ch in text if ch.isdigit())
        return digits if len(digits) > 1 else None
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits or None


class BridgeError(RuntimeError):
    pass


def build_client(settings: dict[str, Any]) -> CallScootClient:
    return CallScootClient(
        base_url=str(settings.get("callscoot_api_base") or "http://127.0.0.1:8788").rstrip("/"),
        api_token=str(settings.get("callscoot_api_token") or "").strip() or None,
        timeout_sec=30.0,
    )


def queue_outbound_call(client: CallScootClient, student: dict[str, Any], dynamic_variables: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    try:
        return client.queue_outbound_call(
            str(student["phone"]),
            dynamic_variables=dynamic_variables,
            metadata=metadata,
            ttl_sec=300,
        )
    except CallScootClientError as exc:
        raise BridgeError(str(exc)) from exc


def create_fallback_pending_request(client: CallScootClient, dynamic_variables: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    try:
        return client.create_pending_call_request(
            target_number=None,
            dynamic_variables=dynamic_variables,
            metadata={**metadata, "fallback_unmatched": True},
            ttl_sec=300,
        )
    except CallScootClientError as exc:
        raise BridgeError(str(exc)) from exc


def wait_for_matching_session_start(
    client: CallScootClient,
    *,
    phone: str,
    timeout_sec: float,
    poll_interval_sec: float = 1.0,
) -> dict[str, Any]:
    deadline = time.time() + timeout_sec
    normalized_phone = normalize_phone(phone)
    observed: dict[str, Any] | None = None
    while time.time() < deadline:
        try:
            current = client.current_call()
        except CallScootClientError as exc:
            raise BridgeError(str(exc)) from exc
        if current and current.get("id"):
            observed = current
            current_number = normalize_phone(current.get("incoming_number"))
            if normalized_phone is None or current_number is None or current_number == normalized_phone:
                return current
        time.sleep(poll_interval_sec)
    if observed:
        return observed
    raise BridgeError("timed out waiting for call session to appear")


def wait_for_session_end(
    client: CallScootClient,
    *,
    session_id: str,
    timeout_sec: float,
    poll_interval_sec: float = 2.0,
) -> dict[str, Any]:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            current = client.current_call()
        except CallScootClientError as exc:
            raise BridgeError(str(exc)) from exc
        if not current or current.get("id") != session_id:
            try:
                return client.get_call(session_id)
            except CallScootClientError as exc:
                raise BridgeError(str(exc)) from exc
        time.sleep(poll_interval_sec)
    raise BridgeError(f"timed out waiting for session {session_id} to finish")


def safe_delete_pending_request(client: CallScootClient, request_id: str | None) -> None:
    if not request_id:
        return
    try:
        client.delete_pending_call_request(request_id)
    except Exception:
        return


def fetch_callscoot_status(client: CallScootClient) -> dict[str, Any]:
    try:
        return client.status()
    except CallScootClientError as exc:
        raise BridgeError(str(exc)) from exc


def patch_callscoot_config(client: CallScootClient, patch: dict[str, Any]) -> dict[str, Any]:
    try:
        return client.patch_config(**patch)
    except CallScootClientError as exc:
        raise BridgeError(str(exc)) from exc


def hangup_current_call(client: CallScootClient) -> dict[str, Any]:
    try:
        return client.hangup_current_call()
    except CallScootClientError as exc:
        raise BridgeError(str(exc)) from exc
