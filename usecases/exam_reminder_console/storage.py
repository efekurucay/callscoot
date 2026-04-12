from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "exam_reminder_console.sqlite3"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


DEFAULT_SETTINGS: dict[str, Any] = {
    "app_title": "DENEYAP Sınav Hatırlatma Console",
    "callscoot_api_base": "http://127.0.0.1:8788",
    "callscoot_api_token": "",
    "telephony_backend": "adb",
    "sip_server": "",
    "sip_username": "",
    "sip_password": "",
    "sip_port": 5060,
    "sip_transport": "udp",
    "sip_audio_mode": "agent",
    "campaign_name": "exam_reminder",
    "organization_name": "Antalya DENEYAP Teknoloji Atölyesi",
    "caller_name": "Bilgilendirme Ekibi",
    "caller_role": "sınav hatırlatma asistanı",
    "greeting_template": "Merhaba, sizleri {organization_name} tarafından arıyorum.",
    "briefing_template": "Yaklaşan sınavınızla ilgili hatırlatma yapmak için aradım. Öğrencimizin adı: {student_full_name}. Sınav gününüz: {exam_date}. Sınav yeriniz: {exam_location}. Sınav seansınız: {exam_session}. Lütfen sınav seansından en az 15 dakika önce sınav yerinde hazır bulunmayı unutmayın. Yanınızda kimlik ve sınav giriş belgesini getirmeyi ihmal etmeyin.",
    "attendance_question_template": "",
    "fallback_phrase": "Bu detaylı bilgiyi şu anda paylaşamıyorum. Not aldım, size tekrar dönüş yapılacak.",
    "closing_template": "Başarılar dilerim. İyi günler.",
    "additional_rules": "Türkçe konuş. Nazik, kısa ve net ol. Bilmediğin bilgiyi uydurma. Veli veya öğrenci detaylı bilgi isterse geri dönüş yapılacağını söyle ve soruyu not et.",
    "faq_text": "Kimlik gerekli mi ise: Yanında kimlik ve sınav giriş belgesi bulundurması gerektiğini söyle.\nDetaylı bilgi istenirse: Not alındığını ve tekrar dönüş yapılacağını söyle.",
    "max_attempts": 2,
    "retry_unreached": True,
    "retry_delay_minutes": 30,
    "delay_between_calls_sec": 5,
    "wait_for_session_start_sec": 45,
    "wait_for_session_end_sec": 600,
    "auto_send_contextual_update": True,
    "auto_send_opening_user_message": True,
    "opening_message_delay_sec": 0,
    "opening_instruction_template": "Çağrı bağlandı. Şimdi konuşmaya sen başla. İlk cümlen kesinlikle 'Merhaba, size nasıl yardımcı olabilirim?' olmasın. Açılışı şu cümleyle yap: {greeting_text} Ardından şu hatırlatma metnini doğal şekilde oku: {briefing_text} Detaylı soru gelirse şu fallback cümlesini kullan: {fallback_phrase} Kapanışı şu cümleyle yap: {closing_text}",
    "auto_apply_callscoot_patch": False,
    "callscoot_patch_payload": "{\n  \"echo_cancel\": false\n}",
}


TERMINAL_STATUSES = {
    "completed_attending",
    "completed_not_attending",
    "completed_uncertain",
    "completed_review_needed",
    "completed_follow_up",
    "unreachable",
    "failed",
}


class Database:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or DB_PATH
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self.seed_default_settings()
        self.recover_incomplete_runs()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with closing(self._connect()) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS students (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    first_name TEXT,
                    last_name TEXT,
                    full_name TEXT NOT NULL,
                    phone TEXT NOT NULL,
                    exam_datetime TEXT NOT NULL,
                    call_order INTEGER NOT NULL DEFAULT 0,
                    workflow_status TEXT NOT NULL DEFAULT 'pending',
                    reached INTEGER,
                    attendance_status TEXT,
                    unresolved_questions TEXT,
                    result_summary TEXT,
                    operator_note TEXT,
                    last_session_id TEXT,
                    last_error TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT,
                    last_called_at TEXT,
                    completed_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    extra_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS call_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    student_id INTEGER NOT NULL,
                    attempt_no INTEGER NOT NULL,
                    request_id TEXT,
                    call_response_json TEXT,
                    session_id TEXT,
                    outcome_status TEXT,
                    reached INTEGER,
                    attendance_status TEXT,
                    unresolved_questions TEXT,
                    analysis_json TEXT,
                    transcript_json TEXT,
                    meta_json TEXT,
                    error_text TEXT,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    FOREIGN KEY(student_id) REFERENCES students(id)
                );
                """
            )
            conn.commit()

    def seed_default_settings(self) -> None:
        with self._lock:
            with closing(self._connect()) as conn:
                for key, value in DEFAULT_SETTINGS.items():
                    conn.execute(
                        "INSERT OR IGNORE INTO settings(key, value_json) VALUES (?, ?)",
                        (key, json.dumps(value, ensure_ascii=False)),
                    )
                conn.commit()

    def recover_incomplete_runs(self) -> None:
        now = utc_now_iso()
        with self._lock:
            with closing(self._connect()) as conn:
                conn.execute(
                    """
                    UPDATE students
                    SET workflow_status = CASE
                        WHEN attempt_count > 0 THEN 'retry_waiting'
                        ELSE 'pending'
                    END,
                        updated_at = ?,
                        next_attempt_at = COALESCE(next_attempt_at, ?)
                    WHERE workflow_status IN ('dialing', 'waiting_session', 'in_call')
                    """,
                    (now, now),
                )
                conn.commit()

    def get_settings(self) -> dict[str, Any]:
        with self._lock:
            with closing(self._connect()) as conn:
                rows = conn.execute("SELECT key, value_json FROM settings").fetchall()
        data = DEFAULT_SETTINGS.copy()
        for row in rows:
            data[row["key"]] = json.loads(row["value_json"])
        return data

    def update_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            with closing(self._connect()) as conn:
                for key, value in patch.items():
                    conn.execute(
                        "INSERT INTO settings(key, value_json) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json",
                        (key, json.dumps(value, ensure_ascii=False)),
                    )
                conn.commit()
        return self.get_settings()

    def list_students(self) -> list[dict[str, Any]]:
        with self._lock:
            with closing(self._connect()) as conn:
                rows = conn.execute(
                    "SELECT * FROM students ORDER BY call_order ASC, id ASC"
                ).fetchall()
        return [self._row_to_student(row) for row in rows]

    def get_student(self, student_id: int) -> dict[str, Any] | None:
        with self._lock:
            with closing(self._connect()) as conn:
                row = conn.execute("SELECT * FROM students WHERE id = ?", (student_id,)).fetchone()
        return self._row_to_student(row) if row else None

    def create_student(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = utc_now_iso()
        full_name = str(payload.get("full_name") or "").strip()
        if not full_name:
            first = str(payload.get("first_name") or "").strip()
            last = str(payload.get("last_name") or "").strip()
            full_name = " ".join(part for part in [first, last] if part).strip()
        if not full_name:
            raise ValueError("full_name is required")
        phone = str(payload.get("phone") or "").strip()
        exam_datetime = str(payload.get("exam_datetime") or "").strip()
        if not phone or not exam_datetime:
            raise ValueError("phone and exam_datetime are required")
        call_order = self._next_call_order() if payload.get("call_order") is None else int(payload["call_order"])
        extra = dict(payload.get("extra") or {})
        for key in ["exam_location", "exam_session"]:
            value = str(payload.get(key) or "").strip()
            if value:
                extra[key] = value
        with self._lock:
            with closing(self._connect()) as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO students(
                        first_name, last_name, full_name, phone, exam_datetime, call_order,
                        workflow_status, created_at, updated_at, next_attempt_at, extra_json
                    ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
                    """,
                    (
                        str(payload.get("first_name") or "").strip() or None,
                        str(payload.get("last_name") or "").strip() or None,
                        full_name,
                        phone,
                        exam_datetime,
                        call_order,
                        now,
                        now,
                        now,
                        json.dumps(extra, ensure_ascii=False),
                    ),
                )
                conn.commit()
                student_id = int(cursor.lastrowid)
        student = self.get_student(student_id)
        assert student is not None
        return student

    def import_students(self, rows: list[dict[str, Any]], replace_existing: bool = False) -> list[dict[str, Any]]:
        imported: list[dict[str, Any]] = []
        with self._lock:
            with closing(self._connect()) as conn:
                now = utc_now_iso()
                if replace_existing:
                    conn.execute("DELETE FROM call_attempts")
                    conn.execute("DELETE FROM students")
                next_order = 1
                existing_max = conn.execute("SELECT COALESCE(MAX(call_order), 0) FROM students").fetchone()[0]
                if not replace_existing:
                    next_order = int(existing_max) + 1
                for row in rows:
                    full_name = str(row.get("full_name") or "").strip()
                    first_name = str(row.get("first_name") or "").strip() or None
                    last_name = str(row.get("last_name") or "").strip() or None
                    if not full_name:
                        full_name = " ".join(part for part in [first_name or "", last_name or ""] if part).strip()
                    if not full_name:
                        continue
                    phone = str(row.get("phone") or "").strip()
                    exam_datetime = str(row.get("exam_datetime") or "").strip()
                    if not phone or not exam_datetime:
                        continue
                    extra = row.get("extra") or {}
                    cursor = conn.execute(
                        """
                        INSERT INTO students(
                            first_name, last_name, full_name, phone, exam_datetime, call_order,
                            workflow_status, created_at, updated_at, next_attempt_at, extra_json
                        ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
                        """,
                        (
                            first_name,
                            last_name,
                            full_name,
                            phone,
                            exam_datetime,
                            next_order,
                            now,
                            now,
                            now,
                            json.dumps(extra, ensure_ascii=False),
                        ),
                    )
                    imported.append({"id": int(cursor.lastrowid)})
                    next_order += 1
                conn.commit()
        return self.list_students()

    def delete_student(self, student_id: int) -> bool:
        with self._lock:
            with closing(self._connect()) as conn:
                conn.execute("DELETE FROM call_attempts WHERE student_id = ?", (student_id,))
                cursor = conn.execute("DELETE FROM students WHERE id = ?", (student_id,))
                conn.commit()
                return cursor.rowcount > 0

    def reset_student(self, student_id: int) -> dict[str, Any] | None:
        now = utc_now_iso()
        with self._lock:
            with closing(self._connect()) as conn:
                conn.execute(
                    """
                    UPDATE students
                    SET workflow_status = 'pending',
                        reached = NULL,
                        attendance_status = NULL,
                        unresolved_questions = NULL,
                        result_summary = NULL,
                        operator_note = NULL,
                        last_session_id = NULL,
                        last_error = NULL,
                        attempt_count = 0,
                        next_attempt_at = ?,
                        last_called_at = NULL,
                        completed_at = NULL,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (now, now, student_id),
                )
                conn.execute("DELETE FROM call_attempts WHERE student_id = ?", (student_id,))
                conn.commit()
        return self.get_student(student_id)

    def set_student_note(self, student_id: int, operator_note: str) -> dict[str, Any] | None:
        now = utc_now_iso()
        with self._lock:
            with closing(self._connect()) as conn:
                conn.execute(
                    "UPDATE students SET operator_note = ?, updated_at = ? WHERE id = ?",
                    (operator_note, now, student_id),
                )
                conn.commit()
        return self.get_student(student_id)

    def prepare_student_for_manual_call(self, student_id: int) -> dict[str, Any] | None:
        now = utc_now_iso()
        with self._lock:
            with closing(self._connect()) as conn:
                conn.execute(
                    """
                    UPDATE students
                    SET workflow_status = 'pending',
                        reached = NULL,
                        attendance_status = NULL,
                        unresolved_questions = NULL,
                        result_summary = NULL,
                        last_session_id = NULL,
                        last_error = NULL,
                        next_attempt_at = ?,
                        completed_at = NULL,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (now, now, student_id),
                )
                conn.commit()
        return self.get_student(student_id)

    def next_student_for_call(self) -> dict[str, Any] | None:
        now = utc_now_iso()
        eligible = {"pending", "retry_waiting"}
        with self._lock:
            with closing(self._connect()) as conn:
                rows = conn.execute(
                    "SELECT * FROM students ORDER BY call_order ASC, id ASC"
                ).fetchall()
        for row in rows:
            student = self._row_to_student(row)
            if student["workflow_status"] not in eligible:
                continue
            next_attempt_at = str(student.get("next_attempt_at") or now)
            if next_attempt_at > now:
                continue
            return student
        return None

    def mark_student_call_started(self, student_id: int) -> dict[str, Any] | None:
        now = utc_now_iso()
        with self._lock:
            with closing(self._connect()) as conn:
                conn.execute(
                    """
                    UPDATE students
                    SET workflow_status = 'dialing',
                        attempt_count = attempt_count + 1,
                        last_called_at = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (now, now, student_id),
                )
                conn.commit()
        return self.get_student(student_id)

    def mark_student_waiting_session(self, student_id: int) -> dict[str, Any] | None:
        now = utc_now_iso()
        with self._lock:
            with closing(self._connect()) as conn:
                conn.execute(
                    "UPDATE students SET workflow_status = 'waiting_session', updated_at = ? WHERE id = ?",
                    (now, student_id),
                )
                conn.commit()
        return self.get_student(student_id)

    def mark_student_in_call(self, student_id: int, session_id: str | None) -> dict[str, Any] | None:
        now = utc_now_iso()
        with self._lock:
            with closing(self._connect()) as conn:
                conn.execute(
                    "UPDATE students SET workflow_status = 'in_call', last_session_id = ?, updated_at = ? WHERE id = ?",
                    (session_id, now, student_id),
                )
                conn.commit()
        return self.get_student(student_id)

    def mark_student_result(self, student_id: int, analysis: dict[str, Any], schedule_retry_at: str | None = None) -> dict[str, Any] | None:
        now = utc_now_iso()
        unresolved_text = "\n".join(analysis.get("unresolved_questions") or []) or None
        completed_at = now if analysis.get("workflow_status") in TERMINAL_STATUSES else None
        with self._lock:
            with closing(self._connect()) as conn:
                conn.execute(
                    """
                    UPDATE students
                    SET workflow_status = ?,
                        reached = ?,
                        attendance_status = ?,
                        unresolved_questions = ?,
                        result_summary = ?,
                        operator_note = COALESCE(operator_note, ?),
                        last_session_id = COALESCE(?, last_session_id),
                        last_error = ?,
                        next_attempt_at = ?,
                        completed_at = COALESCE(?, completed_at),
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        analysis.get("workflow_status"),
                        1 if analysis.get("reached") else 0 if analysis.get("reached") is False else None,
                        analysis.get("attendance_status"),
                        unresolved_text,
                        analysis.get("summary_text"),
                        analysis.get("operator_note"),
                        analysis.get("session_id"),
                        analysis.get("error_text"),
                        schedule_retry_at,
                        completed_at,
                        now,
                        student_id,
                    ),
                )
                conn.commit()
        return self.get_student(student_id)

    def create_attempt(self, student_id: int, attempt_no: int, request_id: str | None, call_response: dict[str, Any] | None = None) -> int:
        now = utc_now_iso()
        with self._lock:
            with closing(self._connect()) as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO call_attempts(student_id, attempt_no, request_id, call_response_json, started_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (student_id, attempt_no, request_id, json.dumps(call_response or {}, ensure_ascii=False), now),
                )
                conn.commit()
                return int(cursor.lastrowid)

    def finish_attempt(
        self,
        attempt_id: int,
        *,
        session_id: str | None = None,
        outcome_status: str | None = None,
        reached: bool | None = None,
        attendance_status: str | None = None,
        unresolved_questions: list[str] | None = None,
        analysis: dict[str, Any] | None = None,
        transcript: list[dict[str, Any]] | None = None,
        meta: dict[str, Any] | None = None,
        error_text: str | None = None,
    ) -> None:
        ended_at = utc_now_iso()
        with self._lock:
            with closing(self._connect()) as conn:
                conn.execute(
                    """
                    UPDATE call_attempts
                    SET session_id = ?,
                        outcome_status = ?,
                        reached = ?,
                        attendance_status = ?,
                        unresolved_questions = ?,
                        analysis_json = ?,
                        transcript_json = ?,
                        meta_json = ?,
                        error_text = ?,
                        ended_at = ?
                    WHERE id = ?
                    """,
                    (
                        session_id,
                        outcome_status,
                        1 if reached else 0 if reached is False else None,
                        attendance_status,
                        json.dumps(unresolved_questions or [], ensure_ascii=False),
                        json.dumps(analysis or {}, ensure_ascii=False),
                        json.dumps(transcript or [], ensure_ascii=False),
                        json.dumps(meta or {}, ensure_ascii=False),
                        error_text,
                        ended_at,
                        attempt_id,
                    ),
                )
                conn.commit()

    def list_attempts(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            with closing(self._connect()) as conn:
                rows = conn.execute(
                    """
                    SELECT a.*, s.full_name, s.phone
                    FROM call_attempts a
                    JOIN students s ON s.id = a.student_id
                    ORDER BY a.id DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        return [self._row_to_attempt(row) for row in rows]

    def queue_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for student in self.list_students():
            key = str(student.get("workflow_status") or "unknown")
            counts[key] = counts.get(key, 0) + 1
        return counts

    def export_rows(self) -> list[dict[str, Any]]:
        rows = []
        for student in self.list_students():
            rows.append(
                {
                    "id": student["id"],
                    "full_name": student["full_name"],
                    "phone": student["phone"],
                    "exam_datetime": student["exam_datetime"],
                    "exam_location": str((student.get("extra") or {}).get("exam_location") or ""),
                    "exam_session": str((student.get("extra") or {}).get("exam_session") or ""),
                    "workflow_status": student["workflow_status"],
                    "reached": student.get("reached"),
                    "attendance_status": student.get("attendance_status"),
                    "unresolved_questions": student.get("unresolved_questions") or "",
                    "result_summary": student.get("result_summary") or "",
                    "attempt_count": student.get("attempt_count"),
                    "last_session_id": student.get("last_session_id") or "",
                    "last_error": student.get("last_error") or "",
                    "last_called_at": student.get("last_called_at") or "",
                    "completed_at": student.get("completed_at") or "",
                }
            )
        return rows

    def _next_call_order(self) -> int:
        with self._lock:
            with closing(self._connect()) as conn:
                row = conn.execute("SELECT COALESCE(MAX(call_order), 0) AS value FROM students").fetchone()
        return int(row["value"] or 0) + 1

    def _row_to_student(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "first_name": row["first_name"],
            "last_name": row["last_name"],
            "full_name": row["full_name"],
            "phone": row["phone"],
            "exam_datetime": row["exam_datetime"],
            "call_order": int(row["call_order"] or 0),
            "workflow_status": row["workflow_status"],
            "reached": None if row["reached"] is None else bool(row["reached"]),
            "attendance_status": row["attendance_status"],
            "unresolved_questions": row["unresolved_questions"],
            "result_summary": row["result_summary"],
            "operator_note": row["operator_note"],
            "last_session_id": row["last_session_id"],
            "last_error": row["last_error"],
            "attempt_count": int(row["attempt_count"] or 0),
            "next_attempt_at": row["next_attempt_at"],
            "last_called_at": row["last_called_at"],
            "completed_at": row["completed_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "extra": json.loads(row["extra_json"] or "{}"),
        }

    def _row_to_attempt(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "student_id": int(row["student_id"]),
            "student_name": row["full_name"],
            "phone": row["phone"],
            "attempt_no": int(row["attempt_no"] or 0),
            "request_id": row["request_id"],
            "call_response": json.loads(row["call_response_json"] or "{}"),
            "session_id": row["session_id"],
            "outcome_status": row["outcome_status"],
            "reached": None if row["reached"] is None else bool(row["reached"]),
            "attendance_status": row["attendance_status"],
            "unresolved_questions": json.loads(row["unresolved_questions"] or "[]"),
            "analysis": json.loads(row["analysis_json"] or "{}"),
            "transcript": json.loads(row["transcript_json"] or "[]"),
            "meta": json.loads(row["meta_json"] or "{}"),
            "error_text": row["error_text"],
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
        }
