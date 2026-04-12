from __future__ import annotations

from datetime import datetime
from typing import Any


TURKISH_WEEKDAYS = {
    0: "Pazartesi",
    1: "Salı",
    2: "Çarşamba",
    3: "Perşembe",
    4: "Cuma",
    5: "Cumartesi",
    6: "Pazar",
}


def split_full_name(full_name: str) -> tuple[str, str]:
    parts = [part for part in str(full_name or "").strip().split() if part]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def format_exam_datetime(exam_datetime: str) -> tuple[str, str, str]:
    raw = str(exam_datetime or "").strip()
    if not raw:
        return "", "", ""
    for fmt in ["%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%d.%m.%Y %H:%M", "%d/%m/%Y %H:%M"]:
        try:
            dt = datetime.strptime(raw, fmt)
            exam_date = f"{dt.strftime('%d.%m.%Y')} {TURKISH_WEEKDAYS[dt.weekday()]}"
            return exam_date, dt.strftime("%H:%M"), dt.isoformat(timespec="minutes")
        except ValueError:
            continue
    return raw, "", raw


def render_template(template: str, variables: dict[str, Any]) -> str:
    data = {key: "" if value is None else value for key, value in variables.items()}
    try:
        return str(template or "").format(**data).strip()
    except Exception:
        return str(template or "").strip()


def parse_faq_text(faq_text: str) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for line in str(faq_text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if "=>" in line:
            question, answer = line.split("=>", 1)
        elif ":" in line:
            question, answer = line.split(":", 1)
        else:
            question, answer = line, ""
        items.append({"question": question.strip(" -\t"), "answer": answer.strip()})
    return items


def student_variables(student: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    first_name = str(student.get("first_name") or "").strip()
    last_name = str(student.get("last_name") or "").strip()
    if not first_name and not last_name:
        first_name, last_name = split_full_name(str(student.get("full_name") or ""))
    exam_date, exam_time, exam_datetime_iso = format_exam_datetime(str(student.get("exam_datetime") or ""))
    extra = student.get("extra") or {}
    exam_location = str(extra.get("exam_location") or extra.get("salon") or "").strip() or "kayıtta belirtilmedi"
    exam_session = str(extra.get("exam_session") or extra.get("session") or exam_time or "").strip() or "kayıtta belirtilmedi"
    variables = {
        "campaign_name": settings.get("campaign_name"),
        "organization_name": settings.get("organization_name"),
        "caller_name": settings.get("caller_name"),
        "caller_role": settings.get("caller_role"),
        "student_full_name": student.get("full_name"),
        "student_first_name": first_name,
        "student_last_name": last_name,
        "student_phone": student.get("phone"),
        "exam_datetime": student.get("exam_datetime"),
        "exam_datetime_iso": exam_datetime_iso,
        "exam_date": exam_date,
        "exam_time": exam_time,
        "exam_location": exam_location,
        "exam_session": exam_session,
        "fallback_phrase": settings.get("fallback_phrase"),
        "closing_template": settings.get("closing_template"),
    }
    variables["greeting_text"] = render_template(str(settings.get("greeting_template") or ""), variables)
    variables["briefing_text"] = render_template(str(settings.get("briefing_template") or ""), variables)
    variables["attendance_question_text"] = render_template(str(settings.get("attendance_question_template") or ""), variables)
    variables["closing_text"] = render_template(str(settings.get("closing_template") or ""), variables)
    return variables


def build_dynamic_variables(student: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    variables = student_variables(student, settings)
    faq_items = parse_faq_text(str(settings.get("faq_text") or ""))
    variables["faq_count"] = len(faq_items)
    variables["faq_text"] = "\n".join(
        f"- Soru: {item['question']} | Cevap: {item['answer']}" for item in faq_items
    )
    variables["additional_rules"] = settings.get("additional_rules")
    variables["goal"] = "Öğrenciye sınav tarihini, yerini ve seans bilgisini hatırlat; detaylı soru gelirse not alıp geri dönüş yapılacağını belirt."
    variables["unresolved_policy"] = (
        "Cevabını bilmediğin soruda tahmin etme. Fallback phrase cümlesini kullan, soruyu not et ve nazikçe kapat."
    )
    return variables


def build_opening_user_message(student: dict[str, Any], settings: dict[str, Any]) -> str:
    variables = build_dynamic_variables(student, settings)
    template = str(settings.get("opening_instruction_template") or "").strip()
    if not template:
        if str(variables.get("attendance_question_text") or "").strip():
            template = (
                "Çağrı bağlandı. Şimdi konuşmaya sen başla. İlk cümlen kesinlikle 'Merhaba, size nasıl yardımcı olabilirim?' olmasın. "
                "Açılışı şu bilgiyle kur: {greeting_text} Ardından şu bilgiyi ver: {briefing_text} Sonra şu soruyu sor: {attendance_question_text}."
            )
        else:
            template = (
                "Çağrı bağlandı. Şimdi konuşmaya sen başla. İlk cümlen kesinlikle 'Merhaba, size nasıl yardımcı olabilirim?' olmasın. "
                "Açılışı şu bilgiyle kur: {greeting_text} Ardından şu bilgilendirmeyi yap: {briefing_text}. Sonunda şu kapanışı kullan: {closing_text}."
            )
    return render_template(template, variables)


def build_contextual_update(student: dict[str, Any], settings: dict[str, Any]) -> str:
    variables = build_dynamic_variables(student, settings)
    faq_items = parse_faq_text(str(settings.get("faq_text") or ""))
    faq_block = "\n".join(
        f"- {item['question']}: {item['answer']}" for item in faq_items
    ) or "- Hazır bir SSS girilmedi. Yalnızca verilen öğrenci/sınav bilgisini kullan."
    sections = [
        "Bu arama bir sınav hatırlatma / bilgilendirme aramasıdır.",
        f"Öğrenci: {variables['student_full_name']} | Telefon: {variables['student_phone']}",
        f"Kurum: {variables['organization_name']}",
        "Konuşma dili: Türkçe",
        "Amaçlar:",
        "1. Kendini tanıt.",
        f"2. Şu bilgilendirmeyi doğal biçimde aktar: {variables['briefing_text']}",
        "3. Çok kısa ve net konuş; gereksiz uzatma.",
        "4. İlk mesajda sadece kurum adına aradığını söyle. İlk mesajda sınav tarihi, yer, seans gibi detaylara girme.",
        "5. İlk mesajdan sonra durup karşı tarafın tepkisini bekle.",
        "6. Karşı taraf konuşmaya başlarsa hemen sus ve dinle; söz kesme.",
        "7. Karşı taraf onay verirse veya buyurun/evet derse sonraki turda sınav bilgilendirmesini yap.",
        "8. Sorular gelirse sadece verilen bilgi ve SSS ile cevap ver.",
        f"9. Cevabı bilmiyorsan şu cümleyi anlamı bozulmadan kullan: {variables['fallback_phrase']}",
        "10. Veli veya öğrenci detaylı bilgi isterse geri dönüş yapılacağını söyle ve konuşmayı nazikçe kapat.",
        "11. 'Merhaba, size nasıl yardımcı olabilirim?' diye açma.",
    ]
    if str(variables.get("attendance_question_text") or "").strip():
        sections.extend(
            [
                "8. Bilgilendirme sonrasında şu soruyu sor:",
                variables["attendance_question_text"],
            ]
        )
    sections.extend(
        [
            "Kullanılacak açılış:",
            variables["greeting_text"],
            "Bilgilendirme metni:",
            variables["briefing_text"],
            "Kapanış:",
            variables["closing_text"],
            "SSS:",
            faq_block,
            "Ek kurallar:",
            str(settings.get("additional_rules") or "").strip() or "-",
        ]
    )
    return "\n".join(section for section in sections if str(section).strip())


def build_recommended_elevenlabs_prompt() -> str:
    return (
        "Sen Türkçe konuşan bir öğrenci işleri telefon asistanısın. "
        "Her aramada client uygulamasından gelen dynamic variables ve contextual update talimatlarını birincil bağlam olarak kullan. "
        "Amaç öğrenciyi sınav tarihi/saat bilgisi hakkında bilgilendirmek, katılım durumunu netleştirmek ve sadece verilen SSS bilgileriyle soruları cevaplamaktır. "
        "Bilmediğin bilgiyi uydurma. Gerektiğinde fallback phrase cümlesini kullan ve ekibin geri dönüş sağlayacağını söyle."
    )
