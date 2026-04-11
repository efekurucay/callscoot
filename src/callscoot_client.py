from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterator


class CallScootClientError(RuntimeError):
    def __init__(self, status: int | None, message: str, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


def _decode_json_bytes(raw: bytes) -> Any:
    text = raw.decode("utf-8") if raw else ""
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"raw": text}


def iter_sse_events(lines: Any) -> Iterator[dict[str, Any]]:
    data_lines: list[str] = []
    for raw in lines:
        line = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        line = line.rstrip("\r\n")
        if not line:
            if data_lines:
                payload = "\n".join(data_lines)
                try:
                    yield json.loads(payload)
                except json.JSONDecodeError:
                    yield {"data": payload}
                data_lines = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if data_lines:
        payload = "\n".join(data_lines)
        try:
            yield json.loads(payload)
        except json.JSONDecodeError:
            yield {"data": payload}


class CallScootClient:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8788",
        api_token: str | None = None,
        timeout_sec: float = 30.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_token = api_token or None
        self.timeout_sec = timeout_sec

    @classmethod
    def from_env(cls) -> "CallScootClient":
        host = os.environ.get("CALLSCOOT_API_HOST", "127.0.0.1")
        port = os.environ.get("CALLSCOOT_API_PORT", "8788")
        base_url = os.environ.get("CALLSCOOT_API_BASE", f"http://{host}:{port}")
        return cls(base_url=base_url, api_token=os.environ.get("CALLSCOOT_API_TOKEN"))

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(f"{self.base_url}{path}", data=data, method=method)
        if payload is not None:
            req.add_header("Content-Type", "application/json")
        if self.api_token:
            req.add_header("Authorization", f"Bearer {self.api_token}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                return _decode_json_bytes(resp.read())
        except urllib.error.HTTPError as exc:
            body = _decode_json_bytes(exc.read())
            message = body.get("error") if isinstance(body, dict) else str(exc)
            raise CallScootClientError(exc.code, str(message), body) from exc
        except urllib.error.URLError as exc:
            raise CallScootClientError(None, str(exc.reason)) from exc

    def _event_stream_response(self, session_id: str = "current") -> Any:
        query = urllib.parse.urlencode({"session_id": session_id})
        req = urllib.request.Request(f"{self.base_url}/v1/events/stream?{query}", method="GET")
        if self.api_token:
            req.add_header("Authorization", f"Bearer {self.api_token}")
        try:
            return urllib.request.urlopen(req, timeout=self.timeout_sec)
        except urllib.error.HTTPError as exc:
            body = _decode_json_bytes(exc.read())
            message = body.get("error") if isinstance(body, dict) else str(exc)
            raise CallScootClientError(exc.code, str(message), body) from exc
        except urllib.error.URLError as exc:
            raise CallScootClientError(None, str(exc.reason)) from exc

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/v1/health")

    def status(self) -> dict[str, Any]:
        return self._request("GET", "/v1/status")

    def get_config(self) -> dict[str, Any]:
        return self._request("GET", "/v1/config")

    def patch_config(self, **patch: Any) -> dict[str, Any]:
        return self._request("PATCH", "/v1/config", patch)

    def queue_outbound_call(
        self,
        number: str,
        dynamic_variables: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        ttl_sec: int = 300,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/outbound-calls",
            {
                "number": number,
                "dynamic_variables": dynamic_variables or {},
                "metadata": metadata or {},
                "ttl_sec": ttl_sec,
            },
        )

    def create_pending_call_request(
        self,
        target_number: str | None = None,
        dynamic_variables: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        ttl_sec: int = 300,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/pending-call-requests",
            {
                "target_number": target_number,
                "dynamic_variables": dynamic_variables or {},
                "metadata": metadata or {},
                "ttl_sec": ttl_sec,
            },
        )

    def list_pending_call_requests(self) -> dict[str, Any]:
        return self._request("GET", "/v1/pending-call-requests")

    def get_pending_call_request(self, request_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/pending-call-requests/{request_id}")

    def delete_pending_call_request(self, request_id: str) -> dict[str, Any]:
        return self._request("DELETE", f"/v1/pending-call-requests/{request_id}")

    def current_call(self) -> dict[str, Any] | None:
        return self._request("GET", "/v1/current-call").get("current_call")

    def list_calls(self, limit: int = 20) -> dict[str, Any]:
        query = urllib.parse.urlencode({"limit": limit})
        return self._request("GET", f"/v1/calls?{query}")

    def get_call(self, session_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/calls/{session_id}")

    def get_call_events(self, session_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/calls/{session_id}/events")

    def stream_events(self, session_id: str = "current") -> Iterator[dict[str, Any]]:
        response = self._event_stream_response(session_id=session_id)
        try:
            for event in iter_sse_events(response):
                yield event
        finally:
            response.close()

    def send_contextual_update(self, text: str, session_id: str | None = None) -> dict[str, Any]:
        path = "/v1/current-call/contextual-update" if not session_id else f"/v1/calls/{session_id}/contextual-update"
        return self._request("POST", path, {"text": text})

    def send_user_message(self, text: str, session_id: str | None = None) -> dict[str, Any]:
        path = "/v1/current-call/user-message" if not session_id else f"/v1/calls/{session_id}/user-message"
        return self._request("POST", path, {"text": text})

    def answer_current_call(self) -> dict[str, Any]:
        return self._request("POST", "/v1/current-call/answer")

    def hangup_current_call(self) -> dict[str, Any]:
        return self._request("POST", "/v1/current-call/hangup")

    def wait_for_session_start(self, timeout_sec: float = 30.0, poll_interval_sec: float = 1.0) -> str:
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            current = self.current_call()
            if current and current.get("id"):
                return str(current["id"])
            time.sleep(poll_interval_sec)
        raise TimeoutError("timed out waiting for session start")

    def wait_for_session_end(self, session_id: str, timeout_sec: float = 180.0, poll_interval_sec: float = 2.0) -> dict[str, Any]:
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            current = self.current_call()
            if not current or current.get("id") != session_id:
                return self.get_call(session_id)
            time.sleep(poll_interval_sec)
        raise TimeoutError(f"timed out waiting for session {session_id} to finish")
