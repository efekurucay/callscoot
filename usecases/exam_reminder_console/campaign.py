from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from analysis import analyze_session
from callscoot_bridge import (
    BridgeError,
    build_client,
    create_fallback_pending_request,
    fetch_callscoot_status,
    hangup_current_call,
    patch_callscoot_config,
    queue_outbound_call,
    safe_delete_pending_request,
    wait_for_matching_session_start,
    wait_for_session_end,
)
from prompting import build_contextual_update, build_dynamic_variables, build_opening_user_message
from storage import Database, utc_now_iso


class CampaignRunner:
    def __init__(self, db: Database):
        self.db = db
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._manual_queue: list[int] = []
        self._state: dict[str, Any] = {
            "running": False,
            "phase": "idle",
            "current_student_id": None,
            "current_student_name": None,
            "current_session_id": None,
            "last_message": "Hazır.",
            "last_error": None,
            "updated_at": utc_now_iso(),
        }

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return self.snapshot()
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run_loop, name="exam-reminder-runner", daemon=True)
            self._thread.start()
            self._update_state(running=True, phase="starting", last_message="Kampanya başlatıldı.", last_error=None)
            return self.snapshot()

    def stop(self) -> dict[str, Any]:
        self._stop_event.set()
        self._update_state(running=False, phase="stopping", last_message="Durdurma talebi gönderildi.")
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            snapshot = dict(self._state)
            snapshot["manual_queue"] = list(self._manual_queue)
            snapshot["manual_queue_length"] = len(self._manual_queue)
            return snapshot

    def dashboard(self) -> dict[str, Any]:
        return {
            "runner": self.snapshot(),
            "students": self.db.list_students(),
            "attempts": self.db.list_attempts(limit=50),
            "counts": self.db.queue_counts(),
            "settings": self.db.get_settings(),
        }

    def fetch_callscoot_status(self) -> dict[str, Any]:
        settings = self.db.get_settings()
        client = build_client(settings)
        return fetch_callscoot_status(client)

    def apply_callscoot_patch(self, patch: dict[str, Any] | None = None) -> dict[str, Any]:
        settings = self.db.get_settings()
        payload = patch or _load_patch_payload(settings)
        client = build_client(settings)
        response = patch_callscoot_config(client, payload)
        self._update_state(last_message="CallScoot ayarları UI üzerinden güncellendi.")
        return response

    def apply_runtime_config(self, settings: dict[str, Any] | None = None) -> dict[str, Any]:
        effective_settings = settings or self.db.get_settings()
        client = build_client(effective_settings)
        payload = _build_telephony_patch(effective_settings)
        response = patch_callscoot_config(client, payload)
        backend = str(payload.get("telephony_backend") or "adb")
        self._update_state(last_message=f"CallScoot çalışma modu güncellendi: {backend}")
        return response

    def hangup_active_call(self) -> dict[str, Any]:
        settings = self.db.get_settings()
        client = build_client(settings)
        response = hangup_current_call(client)
        self._update_state(last_message="Aktif çağrı kapatma komutu gönderildi.")
        return response

    def queue_manual_call(self, student_id: int) -> dict[str, Any]:
        student = self.db.prepare_student_for_manual_call(student_id)
        if not student:
            raise KeyError("student not found")
        should_start = False
        with self._lock:
            if self._state.get("current_student_id") == student_id and self._state.get("phase") in {"dialing", "waiting_session", "in_call"}:
                return {"queued": False, "student": student, "runner": self.snapshot(), "message": "Bu öğrenci zaten aktif olarak aranıyor."}
            if student_id not in self._manual_queue:
                self._manual_queue.append(student_id)
            self._update_state(last_message=f"{student['full_name']} için manuel arama kuyruğa alındı.", last_error=None)
            should_start = not (self._thread and self._thread.is_alive())
        if should_start:
            self.start()
        return {"queued": True, "student": student, "runner": self.snapshot(), "message": "Manuel arama kuyruğa alındı."}

    def _pop_manual_student(self) -> dict[str, Any] | None:
        with self._lock:
            while self._manual_queue:
                student_id = self._manual_queue.pop(0)
                student = self.db.get_student(student_id)
                if student:
                    return student
        return None

    def _run_loop(self) -> None:
        self._update_state(running=True, phase="idle", last_message="Kuyruk bekleniyor.", last_error=None)
        while not self._stop_event.is_set():
            student = self._pop_manual_student() or self.db.next_student_for_call()
            if not student:
                self._update_state(running=True, phase="idle", last_message="Aranacak bekleyen kayıt yok.")
                self._stop_event.wait(1.0)
                continue
            try:
                self._process_student(student)
            except Exception as exc:  # noqa: BLE001
                self._update_state(
                    running=True,
                    phase="error",
                    current_student_id=student.get("id"),
                    current_student_name=student.get("full_name"),
                    last_error=str(exc),
                    last_message=f"Öğrenci işlenirken hata: {exc}",
                )
                self._stop_event.wait(3.0)
            settings = self.db.get_settings()
            delay = max(0.0, float(settings.get("delay_between_calls_sec") or 0))
            if delay and not self._manual_queue:
                self._update_state(running=True, phase="cooldown", last_message=f"Bir sonraki arama için {delay:.0f}s bekleniyor.")
                self._stop_event.wait(delay)
        self._update_state(running=False, phase="stopped", current_student_id=None, current_student_name=None, current_session_id=None, last_message="Kampanya durdu.")

    def _process_student(self, student: dict[str, Any]) -> None:
        settings = self.db.get_settings()
        client = build_client(settings)
        self._maybe_apply_callscoot_patch(client, settings)

        started_student = self.db.mark_student_call_started(int(student["id"]))
        if not started_student:
            raise RuntimeError("öğrenci kaydı bulunamadı")
        attempt_no = int(started_student.get("attempt_count") or 1)

        dynamic_variables = build_dynamic_variables(started_student, settings)
        contextual_update = build_contextual_update(started_student, settings)
        opening_user_message = build_opening_user_message(started_student, settings)
        metadata = {
            "workflow": "exam_reminder_console",
            "student_id": started_student["id"],
            "student_name": started_student["full_name"],
            "exam_datetime": started_student["exam_datetime"],
            "attempt_no": attempt_no,
        }

        self._update_state(
            running=True,
            phase="dialing",
            current_student_id=started_student["id"],
            current_student_name=started_student["full_name"],
            current_session_id=None,
            last_message=f"{started_student['full_name']} aranıyor.",
            last_error=None,
        )

        response = queue_outbound_call(client, started_student, dynamic_variables, metadata)
        request_id = ((response.get("request") or {}).get("request_id") if isinstance(response, dict) else None)
        fallback_request = create_fallback_pending_request(client, dynamic_variables, metadata)
        fallback_request_id = ((fallback_request or {}).get("request_id") if isinstance(fallback_request, dict) else None)
        if isinstance(response, dict):
            response["fallback_request"] = fallback_request
        attempt_id = self.db.create_attempt(int(started_student["id"]), attempt_no, request_id, call_response=response)

        self.db.mark_student_waiting_session(int(started_student["id"]))
        self._update_state(phase="waiting_session", last_message="Çağrı oturumu bekleniyor.")

        session: dict[str, Any] | None = None
        session_id: str | None = None
        try:
            current = wait_for_matching_session_start(
                client,
                phone=str(started_student["phone"]),
                timeout_sec=float(settings.get("wait_for_session_start_sec") or 45),
                poll_interval_sec=1.0,
            )
            session_id = str(current.get("id") or "").strip() or None
            self.db.mark_student_in_call(int(started_student["id"]), session_id)
            self._update_state(phase="in_call", current_session_id=session_id, last_message=f"Aktif görüşme: {session_id or 'bilinmiyor'}")

            if bool(settings.get("auto_send_contextual_update", True)) and session_id:
                try:
                    client.send_contextual_update(contextual_update, session_id=session_id)
                except Exception as exc:  # noqa: BLE001
                    self._update_state(last_message=f"Contextual update gönderilemedi: {exc}")

            if bool(settings.get("auto_send_opening_user_message", True)) and session_id:
                delay_sec = max(0.0, float(settings.get("opening_message_delay_sec") or 0))
                if delay_sec:
                    time.sleep(delay_sec)
                try:
                    client.send_user_message(opening_user_message, session_id=session_id)
                    self._update_state(last_message="Agent açılış konuşması tetiklendi.")
                except Exception as exc:  # noqa: BLE001
                    self._update_state(last_message=f"Açılış mesajı gönderilemedi: {exc}")

            if session_id:
                session = wait_for_session_end(
                    client,
                    session_id=session_id,
                    timeout_sec=float(settings.get("wait_for_session_end_sec") or 600),
                    poll_interval_sec=2.0,
                )
            else:
                raise BridgeError("session id alınamadı")

            analysis = analyze_session(session, fallback_phrase=str(settings.get("fallback_phrase") or ""))
            analysis["session_id"] = session_id
            self._finalize_success(
                student=started_student,
                attempt_id=attempt_id,
                analysis=analysis,
                session=session,
                settings=settings,
            )
        except Exception as exc:  # noqa: BLE001
            self._finalize_error(
                student=started_student,
                attempt_id=attempt_id,
                request_id=request_id,
                error=exc,
                session_id=session_id,
                settings=settings,
            )
            return
        finally:
            safe_delete_pending_request(client, request_id)
            safe_delete_pending_request(client, fallback_request_id)
            self._update_state(current_session_id=None)

    def _finalize_success(
        self,
        *,
        student: dict[str, Any],
        attempt_id: int,
        analysis: dict[str, Any],
        session: dict[str, Any],
        settings: dict[str, Any],
    ) -> None:
        attempt_count = int(student.get("attempt_count") or 1)
        schedule_retry_at = None
        if not analysis.get("reached") and bool(settings.get("retry_unreached", True)) and attempt_count < int(settings.get("max_attempts") or 1):
            analysis["workflow_status"] = "retry_waiting"
            analysis["summary_text"] = "Ulaşılamadı, yeniden denenecek."
            schedule_retry_at = _retry_timestamp(int(settings.get("retry_delay_minutes") or 30))
        self.db.finish_attempt(
            attempt_id,
            session_id=analysis.get("session_id"),
            outcome_status=analysis.get("workflow_status"),
            reached=analysis.get("reached"),
            attendance_status=analysis.get("attendance_status"),
            unresolved_questions=analysis.get("unresolved_questions") or [],
            analysis=analysis,
            transcript=session.get("transcript") or [],
            meta=session.get("meta") or {},
            error_text=None,
        )
        self.db.mark_student_result(int(student["id"]), analysis, schedule_retry_at=schedule_retry_at)
        self._update_state(
            running=True,
            phase="completed",
            current_student_id=student.get("id"),
            current_student_name=student.get("full_name"),
            last_error=None,
            last_message=analysis.get("summary_text") or "Arama tamamlandı.",
        )

    def _finalize_error(
        self,
        *,
        student: dict[str, Any],
        attempt_id: int,
        request_id: str | None,
        error: Exception,
        session_id: str | None,
        settings: dict[str, Any],
    ) -> None:
        attempt_count = int((self.db.get_student(int(student["id"])) or {}).get("attempt_count") or 1)
        error_text = str(error)
        analysis = {
            "session_id": session_id,
            "reached": False,
            "attendance_status": None,
            "unresolved_questions": [],
            "workflow_status": "failed",
            "summary_text": f"Arama hatası: {error_text}",
            "operator_note": None,
            "error_text": error_text,
        }
        schedule_retry_at = None
        if bool(settings.get("retry_unreached", True)) and attempt_count < int(settings.get("max_attempts") or 1):
            analysis["workflow_status"] = "retry_waiting"
            analysis["summary_text"] = f"Arama başarısız oldu, yeniden denenecek: {error_text}"
            schedule_retry_at = _retry_timestamp(int(settings.get("retry_delay_minutes") or 30))
        self.db.finish_attempt(
            attempt_id,
            session_id=session_id,
            outcome_status=analysis.get("workflow_status"),
            reached=False,
            attendance_status=None,
            unresolved_questions=[],
            analysis=analysis,
            transcript=[],
            meta={},
            error_text=error_text,
        )
        self.db.mark_student_result(int(student["id"]), analysis, schedule_retry_at=schedule_retry_at)
        self._update_state(
            running=True,
            phase="error",
            current_student_id=student.get("id"),
            current_student_name=student.get("full_name"),
            last_error=error_text,
            last_message=analysis.get("summary_text") or error_text,
        )

    def _maybe_apply_callscoot_patch(self, client: Any, settings: dict[str, Any]) -> None:
        payload = _build_runtime_patch(settings)
        if not payload:
            return
        patch_callscoot_config(client, payload)

    def _update_state(self, **changes: Any) -> None:
        with self._lock:
            self._state.update(changes)
            self._state["updated_at"] = utc_now_iso()


def _retry_timestamp(delay_minutes: int) -> str:
    dt = datetime.now(timezone.utc) + timedelta(minutes=max(0, int(delay_minutes)))
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _load_patch_payload(settings: dict[str, Any]) -> dict[str, Any]:
    raw = str(settings.get("callscoot_patch_payload") or "").strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
        return payload if isinstance(payload, dict) else {}
    except json.JSONDecodeError:
        return {}


def _build_telephony_patch(settings: dict[str, Any]) -> dict[str, Any]:
    backend = str(settings.get("telephony_backend") or "adb").strip().lower() or "adb"
    if backend not in {"adb", "sip", "auto"}:
        backend = "adb"
    patch: dict[str, Any] = {"telephony_backend": backend}
    sip_server = str(settings.get("sip_server") or "").strip()
    sip_username = str(settings.get("sip_username") or "").strip()
    sip_password = str(settings.get("sip_password") or "").strip()
    should_include_sip = backend == "sip" or (backend == "auto" and any([sip_server, sip_username, sip_password]))
    if should_include_sip:
        if backend == "sip" and (not sip_server or not sip_username or not sip_password):
            raise BridgeError("SIP modu için sip_server, sip_username ve sip_password alanlarını doldurun")
        transport = str(settings.get("sip_transport") or "udp").strip().lower() or "udp"
        if transport not in {"udp", "tcp", "tls"}:
            transport = "udp"
        audio_mode = str(settings.get("sip_audio_mode") or "agent").strip().lower() or "agent"
        if audio_mode not in {"direct", "agent"}:
            audio_mode = "agent"
        try:
            sip_port = int(settings.get("sip_port") or 5060)
        except (TypeError, ValueError):
            sip_port = 5060
        patch.update(
            {
                "sip_server": sip_server or None,
                "sip_username": sip_username or None,
                "sip_password": sip_password or None,
                "sip_port": sip_port,
                "sip_transport": transport,
                "sip_audio_mode": audio_mode,
            }
        )
    return patch


def _build_runtime_patch(settings: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if bool(settings.get("auto_apply_callscoot_patch", False)):
        payload.update(_load_patch_payload(settings))
    payload.update(_build_telephony_patch(settings))
    return payload
