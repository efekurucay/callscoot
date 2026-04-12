from __future__ import annotations

import re
from typing import Any


POSITIVE_PATTERNS = [
    r"\bkatıl(acağım|irim|ırım|ırım)\b",
    r"\bgelece(ğim|gim)\b",
    r"\borada olacağım\b",
    r"\buygun\b",
    r"\bev(et|e+t)\b",
    r"\btamam\b",
]

NEGATIVE_PATTERNS = [
    r"\bkatıl(a?mayacağım|amam|amam)\b",
    r"\bgel(e?meyeceğim|emem)\b",
    r"\bhayır\b",
    r"\buygun değil\b",
    r"\biptal\b",
    r"\bkatılamam\b",
]

UNCERTAIN_PATTERNS = [
    r"\bemin değilim\b",
    r"\bhenüz net değil\b",
    r"\bbelli değil\b",
    r"\bbelki\b",
    r"\bsonra haber\b",
    r"\bkararsızım\b",
]

QUESTION_PATTERNS = [
    r"\?",
    r"\bnerede\b",
    r"\bhangi\b",
    r"\bkaçta\b",
    r"\bne zaman\b",
    r"\bnasıl\b",
    r"\bzorunlu mu\b",
    r"\bgerekli mi\b",
    r"\bgetirmem gerekiyor mu\b",
]

FALLBACK_MARKERS = [
    "tekrar dönüş",
    "not aldım",
    "not alıyorum",
    "şu anda net paylaşamıyorum",
    "ekibimiz size dönüş",
    "kontrol edip",
    "daha sonra bilgi",
]


def _normalize_text(value: str | None) -> str:
    text = str(value or "").strip().lower()
    text = text.replace("İ", "i").replace("I", "ı")
    return re.sub(r"\s+", " ", text)


def _matches_any(text: str, patterns: list[str]) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


def classify_attendance(text: str) -> str | None:
    normalized = _normalize_text(text)
    if not normalized:
        return None
    if _matches_any(normalized, NEGATIVE_PATTERNS):
        return "not_attending"
    if _matches_any(normalized, UNCERTAIN_PATTERNS):
        return "uncertain"
    if _matches_any(normalized, POSITIVE_PATTERNS):
        return "attending"
    return None


def looks_like_question(text: str) -> bool:
    normalized = _normalize_text(text)
    return bool(normalized and _matches_any(normalized, QUESTION_PATTERNS))


def is_fallback_response(text: str, fallback_phrase: str | None = None) -> bool:
    normalized = _normalize_text(text)
    phrase = _normalize_text(fallback_phrase)
    if phrase and phrase in normalized:
        return True
    return any(marker in normalized for marker in FALLBACK_MARKERS)


def summarize_analysis(analysis: dict[str, Any]) -> str:
    if not analysis.get("reached"):
        if analysis.get("workflow_status") == "retry_waiting":
            return "Ulaşılamadı, yeniden denenecek."
        return "Ulaşılamadı."
    attendance = analysis.get("attendance_status")
    if attendance == "attending":
        base = "Öğrenci sınava katılacağını belirtti."
    elif attendance == "not_attending":
        base = "Öğrenci sınava katılamayacağını belirtti."
    elif attendance == "uncertain":
        base = "Öğrencinin katılım durumu henüz net değil."
    else:
        base = "Öğrenciye ulaşıldı ancak katılım durumu net alınamadı."
    unresolved = analysis.get("unresolved_questions") or []
    if unresolved:
        base += f" {len(unresolved)} adet geri dönüş notu var."
    return base


def analyze_session(session: dict[str, Any], *, fallback_phrase: str | None = None) -> dict[str, Any]:
    meta = session.get("meta") or {}
    transcript = list(session.get("transcript") or [])
    caller_turns = [item for item in transcript if str(item.get("speaker") or "").lower() == "caller"]
    assistant_turns = [item for item in transcript if str(item.get("speaker") or "").lower() == "assistant"]

    reached = len(caller_turns) > 0
    attendance_status = None
    for item in caller_turns:
        verdict = classify_attendance(str(item.get("text") or ""))
        if verdict:
            attendance_status = verdict

    unresolved_questions: list[str] = []
    for index, turn in enumerate(transcript):
        speaker = str(turn.get("speaker") or "").lower()
        text = str(turn.get("text") or "").strip()
        if speaker != "caller" or not looks_like_question(text):
            continue
        next_assistant = None
        for follow in transcript[index + 1 : index + 4]:
            if str(follow.get("speaker") or "").lower() == "assistant":
                next_assistant = str(follow.get("text") or "")
                break
        if next_assistant and is_fallback_response(next_assistant, fallback_phrase=fallback_phrase):
            unresolved_questions.append(text)

    if not reached:
        workflow_status = "unreachable"
    elif unresolved_questions and attendance_status in {"attending", "not_attending", "uncertain"}:
        workflow_status = "completed_follow_up"
    elif attendance_status == "attending":
        workflow_status = "completed_attending"
    elif attendance_status == "not_attending":
        workflow_status = "completed_not_attending"
    elif attendance_status == "uncertain":
        workflow_status = "completed_uncertain"
    else:
        workflow_status = "completed_review_needed"

    result = {
        "session_id": meta.get("id"),
        "reached": reached,
        "attendance_status": attendance_status,
        "unresolved_questions": unresolved_questions,
        "workflow_status": workflow_status,
        "caller_turn_count": len(caller_turns),
        "assistant_turn_count": len(assistant_turns),
        "summary_text": "",
        "operator_note": None,
        "error_text": None,
    }
    result["summary_text"] = summarize_analysis(result)
    if unresolved_questions:
        result["operator_note"] = "\n".join(unresolved_questions)
    return result
