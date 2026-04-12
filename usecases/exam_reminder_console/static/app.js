const state = {
  dashboard: null,
  selectedStudentId: null,
  settingsInitialized: false,
};

async function fetchJSON(url, options = {}) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const text = await response.text();
  let data = {};
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    data = { raw: text };
  }
  if (!response.ok) {
    throw new Error(data.error || data.raw || `HTTP ${response.status}`);
  }
  return data;
}

function showToast(message, isError = false) {
  const toast = document.getElementById("toast");
  toast.textContent = message;
  toast.className = isError ? "show error" : "show";
  window.clearTimeout(showToast._timer);
  showToast._timer = window.setTimeout(() => {
    toast.className = "";
  }, 3000);
}

function boolText(value) {
  if (value === true) return "Evet";
  if (value === false) return "Hayır";
  return "-";
}

function statusLabel(value) {
  const map = {
    pending: "Bekliyor",
    retry_waiting: "Tekrar denenecek",
    dialing: "Aranıyor",
    waiting_session: "Bağlanıyor",
    in_call: "Görüşmede",
    attending: "Katılacak",
    not_attending: "Katılmayacak",
    uncertain: "Belirsiz",
    completed_attending: "Katılacak",
    completed_not_attending: "Katılmayacak",
    completed_uncertain: "Belirsiz",
    completed_review_needed: "Gözden geçir",
    completed_follow_up: "Geri dönüş gerekli",
    unreachable: "Ulaşılamadı",
    failed: "Hata",
  };
  return map[value] || value || "-";
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function renderRunnerStatus(runner) {
  const container = document.getElementById("runnerStatus");
  container.innerHTML = `
    <div><strong>Çalışıyor</strong><span>${runner.running ? "Evet" : "Hayır"}</span></div>
    <div><strong>Faz</strong><span>${escapeHtml(runner.phase || "-")}</span></div>
    <div><strong>Aktif öğrenci</strong><span>${escapeHtml(runner.current_student_name || "-")}</span></div>
    <div><strong>Aktif session</strong><span>${escapeHtml(runner.current_session_id || "-")}</span></div>
    <div><strong>Manuel kuyruk</strong><span>${escapeHtml(runner.manual_queue_length || 0)}</span></div>
    <div><strong>Son mesaj</strong><span>${escapeHtml(runner.last_message || "-")}</span></div>
    <div><strong>Son hata</strong><span>${escapeHtml(runner.last_error || "-")}</span></div>
  `;
}

function renderAccessUrls(urls) {
  const container = document.getElementById("accessUrls");
  const rows = urls || [];
  if (!rows.length) {
    container.innerHTML = "";
    return;
  }
  container.innerHTML = rows.map((url) => `<span class="badge"><strong>Erişim:</strong> <a href="${escapeHtml(url)}" target="_blank" rel="noreferrer">${escapeHtml(url)}</a></span>`).join("");
}

function renderCounts(counts) {
  const bar = document.getElementById("countsBar");
  const entries = Object.entries(counts || {});
  if (!entries.length) {
    bar.innerHTML = `<span class="badge">Kayıt yok</span>`;
    return;
  }
  bar.innerHTML = entries
    .map(([key, value]) => `<span class="badge"><strong>${escapeHtml(key)}</strong>: ${value}</span>`)
    .join("");
}

function renderStudents(students) {
  const tbody = document.getElementById("studentsBody");
  tbody.innerHTML = students
    .map((student) => {
      const selected = state.selectedStudentId === student.id ? "selected-row" : "";
      const secondary = [
        student.attendance_status ? `Katılım: ${statusLabel(student.attendance_status)}` : null,
        student.attempt_count ? `Deneme: ${student.attempt_count}` : null,
        student.last_session_id ? `Session: ${student.last_session_id}` : null,
      ].filter(Boolean).join(" · ");
      return `
        <tr class="${selected}">
          <td>
            <strong>${escapeHtml(student.full_name)}</strong>
            <div class="muted small-text">${secondary || "-"}</div>
          </td>
          <td>${escapeHtml(student.phone)}</td>
          <td>${escapeHtml([student.exam_datetime, student.extra?.exam_location, student.extra?.exam_session].filter(Boolean).join(" · ") || "-")}</td>
          <td><span class="status-pill">${escapeHtml(statusLabel(student.workflow_status))}</span></td>
          <td>${escapeHtml(student.result_summary || student.unresolved_questions || student.last_error || "-")}</td>
          <td>
            <div class="row-actions">
              <button data-action="call-now" data-id="${student.id}" class="success-link">Şimdi Ara</button>
              <button data-action="preview" data-id="${student.id}">Prompt</button>
              <button data-action="note" data-id="${student.id}">Not</button>
              <button data-action="reset" data-id="${student.id}">Reset</button>
              <button data-action="delete" data-id="${student.id}" class="danger-link">Sil</button>
            </div>
          </td>
        </tr>
      `;
    })
    .join("");
}

function renderAttempts(attempts) {
  const tbody = document.getElementById("attemptsBody");
  tbody.innerHTML = attempts
    .map(
      (attempt) => `
        <tr>
          <td>${escapeHtml(attempt.student_name)}</td>
          <td>${escapeHtml(attempt.attempt_no)}</td>
          <td>${escapeHtml(attempt.session_id || "-")}</td>
          <td>${escapeHtml(statusLabel(attempt.outcome_status || "-"))}</td>
          <td>${escapeHtml(attempt.error_text || "-")}</td>
          <td>${escapeHtml(attempt.started_at || "-")}</td>
        </tr>
      `,
    )
    .join("");
}

function updateTelephonyFieldVisibility() {
  const select = document.getElementById("telephonyBackendSelect");
  const block = document.getElementById("sipFieldsBlock");
  if (!select || !block) return;
  const showSip = ["sip", "auto"].includes(String(select.value || "").toLowerCase());
  block.style.display = showSip ? "grid" : "none";
}

function fillSettingsForm(settings, force = false) {
  const form = document.getElementById("settingsForm");
  const active = document.activeElement;
  if (!force && active && form.contains(active)) {
    return;
  }
  for (const [key, value] of Object.entries(settings)) {
    const input = form.elements.namedItem(key);
    if (!input) continue;
    input.value = value ?? "";
  }
  updateTelephonyFieldVisibility();
  state.settingsInitialized = true;
}

async function loadDashboard() {
  const data = await fetchJSON("/api/dashboard");
  state.dashboard = data;
  document.title = `${data.settings.app_title || "Exam Reminder Console"}`;
  document.querySelector("h1").textContent = data.settings.app_title || "Exam Reminder Console";
  renderRunnerStatus(data.runner);
  renderCounts(data.counts);
  renderStudents(data.students || []);
  renderAttempts(data.attempts || []);
  renderAccessUrls(data.access_urls || []);
  fillSettingsForm(data.settings || {}, !state.settingsInitialized);
  document.getElementById("recommendedPrompt").textContent = data.recommended_elevenlabs_prompt || "";
  if (!state.selectedStudentId && data.students && data.students.length) {
    state.selectedStudentId = data.students[0].id;
  }
  if (state.selectedStudentId) {
    await loadPromptPreview(state.selectedStudentId);
  }
}

async function loadPromptPreview(studentId) {
  const data = await fetchJSON(`/api/prompt-preview?student_id=${studentId}`);
  state.selectedStudentId = studentId;
  document.getElementById("promptPreview").textContent = `${data.contextual_update}\n\n--- opening_user_message ---\n${data.opening_user_message}\n\n--- dynamic_variables ---\n${JSON.stringify(data.dynamic_variables, null, 2)}`;
  renderStudents(state.dashboard?.students || []);
}

function buildSettingsPayload(form) {
  const payload = Object.fromEntries(new FormData(form).entries());
  payload.retry_unreached = payload.retry_unreached === "true";
  payload.auto_send_contextual_update = payload.auto_send_contextual_update === "true";
  payload.auto_send_opening_user_message = payload.auto_send_opening_user_message === "true";
  payload.auto_apply_callscoot_patch = payload.auto_apply_callscoot_patch === "true";
  payload.max_attempts = Number(payload.max_attempts || 1);
  payload.retry_delay_minutes = Number(payload.retry_delay_minutes || 0);
  payload.delay_between_calls_sec = Number(payload.delay_between_calls_sec || 0);
  payload.wait_for_session_start_sec = Number(payload.wait_for_session_start_sec || 45);
  payload.wait_for_session_end_sec = Number(payload.wait_for_session_end_sec || 600);
  payload.opening_message_delay_sec = Number(payload.opening_message_delay_sec || 0);
  payload.sip_port = Number(payload.sip_port || 5060);
  return payload;
}

async function handleSettingsSubmit(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const payload = buildSettingsPayload(form);
  await fetchJSON("/api/config", { method: "POST", body: JSON.stringify(payload) });
  showToast("Ayarlar kaydedildi");
  await loadDashboard();
}

async function handleStudentSubmit(event) {
  event.preventDefault();
  const formData = new FormData(event.currentTarget);
  const payload = Object.fromEntries(formData.entries());
  await fetchJSON("/api/students", { method: "POST", body: JSON.stringify(payload) });
  event.currentTarget.reset();
  showToast("Kayıt eklendi");
  await loadDashboard();
}

async function handleImportSubmit(event) {
  event.preventDefault();
  const formData = new FormData(event.currentTarget);
  const payload = {
    csv_text: formData.get("csv_text"),
    replace_existing: formData.get("replace_existing") === "on",
  };
  await fetchJSON("/api/students/import", { method: "POST", body: JSON.stringify(payload) });
  showToast("Liste içe aktarıldı");
  await loadDashboard();
}

async function handleTableAction(event) {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const action = button.dataset.action;
  const id = Number(button.dataset.id);
  if (!id) return;

  if (action === "preview") {
    await loadPromptPreview(id);
    return;
  }

  if (action === "call-now") {
    await fetchJSON(`/api/students/${id}/call-now`, { method: "POST", body: JSON.stringify({}) });
    showToast("Manuel arama kuyruğa alındı");
    await loadDashboard();
    return;
  }

  if (action === "reset") {
    await fetchJSON(`/api/students/${id}/reset`, { method: "POST", body: JSON.stringify({}) });
    showToast("Kayıt resetlendi");
    await loadDashboard();
    return;
  }

  if (action === "delete") {
    if (!window.confirm("Kayıt silinsin mi?")) return;
    await fetchJSON(`/api/students/${id}`, { method: "DELETE" });
    showToast("Kayıt silindi");
    await loadDashboard();
    return;
  }

  if (action === "note") {
    const note = window.prompt("Operatör notu", "") ?? "";
    await fetchJSON(`/api/students/${id}/note`, { method: "POST", body: JSON.stringify({ operator_note: note }) });
    showToast("Not kaydedildi");
    await loadDashboard();
  }
}

async function handleStart() {
  await fetchJSON("/api/campaign/start", { method: "POST", body: JSON.stringify({}) });
  showToast("Kampanya başlatıldı");
  await loadDashboard();
}

async function handleStop() {
  await fetchJSON("/api/campaign/stop", { method: "POST", body: JSON.stringify({}) });
  showToast("Kampanya durduruldu");
  await loadDashboard();
}

async function handleHangupCurrentCall() {
  await fetchJSON("/api/current-call/hangup", { method: "POST", body: JSON.stringify({}) });
  showToast("Aktif çağrı için kapatma komutu gönderildi");
  await loadDashboard();
}

async function handleFetchCallScoot() {
  const data = await fetchJSON("/api/callscoot/status");
  document.getElementById("callscootStatus").textContent = JSON.stringify(data.status, null, 2);
}

async function handleApplyRuntime() {
  const form = document.getElementById("settingsForm");
  const payload = buildSettingsPayload(form);
  const data = await fetchJSON("/api/callscoot/apply-runtime", { method: "POST", body: JSON.stringify(payload) });
  document.getElementById("callscootStatus").textContent = JSON.stringify(data.response, null, 2);
  showToast("Seçili telephony modu CallScoot'a uygulandı");
  await loadDashboard();
}

async function handleApplyPatch() {
  const form = document.getElementById("settingsForm");
  const raw = form.elements.namedItem("callscoot_patch_payload").value;
  let patch = null;
  if (raw.trim()) {
    try {
      patch = JSON.parse(raw);
    } catch (error) {
      showToast(`JSON parse hatası: ${error.message}`, true);
      return;
    }
  }
  const data = await fetchJSON("/api/callscoot/apply-patch", { method: "POST", body: JSON.stringify({ patch }) });
  document.getElementById("callscootStatus").textContent = JSON.stringify(data.response, null, 2);
  showToast("CallScoot patch uygulandı");
}

function bindEvents() {
  document.getElementById("settingsForm").addEventListener("submit", (event) => {
    handleSettingsSubmit(event).catch((error) => showToast(error.message, true));
  });
  document.getElementById("studentForm").addEventListener("submit", (event) => {
    handleStudentSubmit(event).catch((error) => showToast(error.message, true));
  });
  document.getElementById("importForm").addEventListener("submit", (event) => {
    handleImportSubmit(event).catch((error) => showToast(error.message, true));
  });
  document.getElementById("studentsBody").addEventListener("click", (event) => {
    handleTableAction(event).catch((error) => showToast(error.message, true));
  });
  document.getElementById("startCampaignBtn").addEventListener("click", () => {
    handleStart().catch((error) => showToast(error.message, true));
  });
  document.getElementById("stopCampaignBtn").addEventListener("click", () => {
    handleStop().catch((error) => showToast(error.message, true));
  });
  document.getElementById("hangupCallBtn").addEventListener("click", () => {
    handleHangupCurrentCall().catch((error) => showToast(error.message, true));
  });
  document.getElementById("fetchCallScootBtn").addEventListener("click", () => {
    handleFetchCallScoot().catch((error) => showToast(error.message, true));
  });
  document.getElementById("applyRuntimeBtn").addEventListener("click", () => {
    handleApplyRuntime().catch((error) => showToast(error.message, true));
  });
  document.getElementById("applyPatchBtn").addEventListener("click", () => {
    handleApplyPatch().catch((error) => showToast(error.message, true));
  });
  document.getElementById("telephonyBackendSelect").addEventListener("change", updateTelephonyFieldVisibility);
  document.getElementById("refreshBtn").addEventListener("click", () => {
    loadDashboard().catch((error) => showToast(error.message, true));
  });
  document.getElementById("csvFileInput").addEventListener("change", async (event) => {
    const file = event.target.files?.[0];
    if (!file) return;
    const text = await file.text();
    document.getElementById("csvText").value = text;
  });
}

async function init() {
  bindEvents();
  await loadDashboard();
  setInterval(() => {
    loadDashboard().catch(() => {});
  }, 5000);
}

document.addEventListener("DOMContentLoaded", () => {
  init().catch((error) => showToast(error.message, true));
});
