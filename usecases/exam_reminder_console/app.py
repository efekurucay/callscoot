#!/usr/bin/env python3
from __future__ import annotations

import csv
import io
import json
import os
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from campaign import CampaignRunner
from prompting import build_contextual_update, build_dynamic_variables, build_opening_user_message, build_recommended_elevenlabs_prompt
from storage import BASE_DIR, Database


HOST = os.environ.get("EXAM_REMINDER_HOST", "0.0.0.0")
PORT = int(os.environ.get("EXAM_REMINDER_PORT", "8899"))
STATIC_DIR = BASE_DIR / "static"
INDEX_HTML = BASE_DIR / "index.html"

DB = Database()
RUNNER = CampaignRunner(DB)


def json_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def detect_primary_ip() -> str | None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return str(sock.getsockname()[0])
    except OSError:
        return None
    finally:
        sock.close()


def access_urls() -> list[str]:
    urls: list[str] = []
    if HOST in {"0.0.0.0", "::", ""}:
        urls.append(f"http://127.0.0.1:{PORT}")
        detected_ip = detect_primary_ip()
        if detected_ip and detected_ip != "127.0.0.1":
            urls.append(f"http://{detected_ip}:{PORT}")
        return urls
    return [f"http://{HOST}:{PORT}"]


def parse_rows_from_text(raw_text: str) -> list[dict[str, Any]]:
    text = str(raw_text or "").strip()
    if not text:
        return []
    sample = text.splitlines()[0]
    delimiter = ";" if sample.count(";") > sample.count(",") else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    rows: list[dict[str, Any]] = []
    for row in reader:
        normalized = {str(key or "").strip().lower(): str(value or "").strip() for key, value in row.items()}
        first_name = normalized.get("first_name") or normalized.get("isim") or ""
        last_name = normalized.get("last_name") or normalized.get("soyisim") or normalized.get("soyad") or ""
        full_name = normalized.get("full_name") or normalized.get("ad soyad") or normalized.get("name") or ""
        phone = normalized.get("phone") or normalized.get("telefon") or normalized.get("phone_number") or ""
        exam_datetime = (
            normalized.get("exam_datetime")
            or normalized.get("exam_date_time")
            or normalized.get("sınav_tarihi_saati")
            or normalized.get("sinav_tarihi_saati")
            or normalized.get("sınav_tarihi")
            or normalized.get("exam_date")
            or ""
        )
        exam_location = (
            normalized.get("exam_location")
            or normalized.get("exam_room")
            or normalized.get("exam_place")
            or normalized.get("salon")
            or normalized.get("salon adı")
            or normalized.get("salon_adi")
            or normalized.get("sınav_yeri")
            or normalized.get("sinav_yeri")
            or ""
        )
        exam_session = (
            normalized.get("exam_session")
            or normalized.get("session")
            or normalized.get("session_info")
            or normalized.get("seans")
            or normalized.get("seans bilgisi")
            or normalized.get("seans_bilgisi")
            or ""
        )
        extra = {}
        if exam_location:
            extra["exam_location"] = exam_location
        if exam_session:
            extra["exam_session"] = exam_session
        rows.append(
            {
                "first_name": first_name,
                "last_name": last_name,
                "full_name": full_name,
                "phone": phone,
                "exam_datetime": exam_datetime,
                "extra": extra,
            }
        )
    return rows


class Handler(BaseHTTPRequestHandler):
    server_version = "ExamReminderConsole/1.0"

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8") or "{}")

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, status: int, text: str, content_type: str = "text/plain; charset=utf-8") -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, path: Path, content_type: str) -> None:
        if not path.exists() or not path.is_file():
            self.send_error(404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            self._serve_file(INDEX_HTML, "text/html; charset=utf-8")
            return
        if path == "/static/app.js":
            self._serve_file(STATIC_DIR / "app.js", "application/javascript; charset=utf-8")
            return
        if path == "/static/style.css":
            self._serve_file(STATIC_DIR / "style.css", "text/css; charset=utf-8")
            return
        if path == "/api/dashboard":
            self._send_json(
                200,
                {
                    **RUNNER.dashboard(),
                    "recommended_elevenlabs_prompt": build_recommended_elevenlabs_prompt(),
                    "access_urls": access_urls(),
                },
            )
            return
        if path == "/api/config":
            self._send_json(
                200,
                {
                    "settings": DB.get_settings(),
                    "recommended_elevenlabs_prompt": build_recommended_elevenlabs_prompt(),
                    "access_urls": access_urls(),
                },
            )
            return
        if path == "/api/callscoot/status":
            try:
                self._send_json(200, {"status": RUNNER.fetch_callscoot_status()})
            except Exception as exc:  # noqa: BLE001
                self._send_json(502, {"error": str(exc)})
            return
        if path == "/api/prompt-preview":
            query = parse_qs(parsed.query)
            student_id = int((query.get("student_id") or ["0"])[0] or 0)
            student = DB.get_student(student_id) if student_id else None
            if not student:
                students = DB.list_students()
                student = students[0] if students else None
            if not student:
                self._send_json(404, {"error": "preview için öğrenci bulunamadı"})
                return
            settings = DB.get_settings()
            self._send_json(
                200,
                {
                    "student": student,
                    "dynamic_variables": build_dynamic_variables(student, settings),
                    "contextual_update": build_contextual_update(student, settings),
                    "opening_user_message": build_opening_user_message(student, settings),
                    "recommended_elevenlabs_prompt": build_recommended_elevenlabs_prompt(),
                },
            )
            return
        if path == "/api/export.csv":
            rows = DB.export_rows()
            output = io.StringIO()
            fieldnames = list(rows[0].keys()) if rows else [
                "id",
                "full_name",
                "phone",
                "exam_datetime",
                "workflow_status",
                "reached",
                "attendance_status",
                "unresolved_questions",
                "result_summary",
                "attempt_count",
                "last_session_id",
                "last_error",
                "last_called_at",
                "completed_at",
            ]
            writer = csv.DictWriter(output, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
            text = output.getvalue()
            body = text.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="exam-reminder-results.csv"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            payload = self._read_json()
        except json.JSONDecodeError:
            self._send_json(400, {"error": "invalid json"})
            return

        if path == "/api/config":
            settings = DB.update_settings(payload)
            self._send_json(200, {"saved": True, "settings": settings})
            return

        if path == "/api/campaign/start":
            self._send_json(200, {"runner": RUNNER.start()})
            return

        if path == "/api/campaign/stop":
            self._send_json(200, {"runner": RUNNER.stop()})
            return

        if path == "/api/current-call/hangup":
            try:
                response = RUNNER.hangup_active_call()
            except Exception as exc:  # noqa: BLE001
                self._send_json(502, {"error": str(exc)})
                return
            self._send_json(200, {"ok": True, "response": response, "runner": RUNNER.snapshot()})
            return

        if path == "/api/callscoot/apply-runtime":
            merged_settings = DB.get_settings()
            if isinstance(payload, dict) and payload:
                merged_settings.update(payload)
            try:
                response = RUNNER.apply_runtime_config(merged_settings)
            except Exception as exc:  # noqa: BLE001
                self._send_json(502, {"error": str(exc)})
                return
            if isinstance(payload, dict) and payload:
                DB.update_settings(payload)
            self._send_json(200, {"saved": True, "settings": DB.get_settings(), "response": response})
            return

        if path == "/api/callscoot/apply-patch":
            patch = payload.get("patch") if isinstance(payload, dict) else None
            try:
                response = RUNNER.apply_callscoot_patch(patch if isinstance(patch, dict) else None)
            except Exception as exc:  # noqa: BLE001
                self._send_json(502, {"error": str(exc)})
                return
            self._send_json(200, {"saved": True, "response": response})
            return

        if path == "/api/students":
            student_payload = dict(payload)
            extra = dict(student_payload.get("extra") or {})
            for key in ["exam_location", "exam_session"]:
                value = str(student_payload.get(key) or "").strip()
                if value:
                    extra[key] = value
            if extra:
                student_payload["extra"] = extra
            try:
                student = DB.create_student(student_payload)
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            self._send_json(201, {"student": student})
            return

        if path == "/api/students/import":
            rows = parse_rows_from_text(str(payload.get("csv_text") or ""))
            students = DB.import_students(rows, replace_existing=bool(payload.get("replace_existing")))
            self._send_json(200, {"count": len(rows), "students": students})
            return

        if path.startswith("/api/students/") and path.endswith("/reset"):
            student_id = int(path.split("/")[3])
            student = DB.reset_student(student_id)
            if not student:
                self._send_json(404, {"error": "student not found"})
                return
            self._send_json(200, {"student": student})
            return

        if path.startswith("/api/students/") and path.endswith("/note"):
            student_id = int(path.split("/")[3])
            student = DB.set_student_note(student_id, str(payload.get("operator_note") or ""))
            if not student:
                self._send_json(404, {"error": "student not found"})
                return
            self._send_json(200, {"student": student})
            return

        if path.startswith("/api/students/") and path.endswith("/call-now"):
            student_id = int(path.split("/")[3])
            try:
                response = RUNNER.queue_manual_call(student_id)
            except KeyError:
                self._send_json(404, {"error": "student not found"})
                return
            self._send_json(200, response)
            return

        self.send_error(404)

    def do_DELETE(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/students/"):
            try:
                student_id = int(path.split("/")[3])
            except Exception:
                self._send_json(400, {"error": "invalid student id"})
                return
            deleted = DB.delete_student(student_id)
            if not deleted:
                self._send_json(404, {"error": "student not found"})
                return
            self._send_json(200, {"deleted": True, "student_id": student_id})
            return
        self.send_error(404)

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"[exam-reminder-console] listening on http://{HOST}:{PORT}", flush=True)
    for url in access_urls():
        print(f"[exam-reminder-console] access: {url}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        RUNNER.stop()
        server.server_close()


if __name__ == "__main__":
    main()
