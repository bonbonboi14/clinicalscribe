const statusEl = document.querySelector("#status");
const detailEl = document.querySelector("#detail");
const progressEl = document.querySelector("#progress");
const startButton = document.querySelector("#start");
const stopButton = document.querySelector("#stop");

let recorder;
let activeSessionId = localStorage.getItem("clinicalScribeSessionId");
let nextSequence = 0;
let recordingStopped = false;
let uploadRunning = false;
let uploadRequested = false;

const databaseReady = new Promise((resolve, reject) => {
  const request = indexedDB.open("clinical-scribe-recordings", 1);
  request.onupgradeneeded = () => {
    const store = request.result.createObjectStore("chunks", { keyPath: ["sessionId", "sequence"] });
    store.createIndex("session", "sessionId");
  };
  request.onsuccess = () => resolve(request.result);
  request.onerror = () => reject(request.error);
});

function setStatus(message) { statusEl.textContent = message; }

async function transaction(mode, action) {
  const db = await databaseReady;
  return new Promise((resolve, reject) => {
    const tx = db.transaction("chunks", mode);
    action(tx.objectStore("chunks"));
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
}

async function putChunk(chunk) {
  await transaction("readwrite", store => store.put(chunk));
}

async function sessionChunks(sessionId) {
  const db = await databaseReady;
  return new Promise((resolve, reject) => {
    const request = db.transaction("chunks", "readonly").objectStore("chunks").index("session").getAll(sessionId);
    request.onsuccess = () => resolve(request.result.sort((a, b) => a.sequence - b.sequence));
    request.onerror = () => reject(request.error);
  });
}

async function clearSession(sessionId) {
  const chunks = await sessionChunks(sessionId);
  await transaction("readwrite", store => chunks.forEach(chunk => store.delete([sessionId, chunk.sequence])));
}

async function digest(blob) {
  const hash = await crypto.subtle.digest("SHA-256", await blob.arrayBuffer());
  return [...new Uint8Array(hash)].map(value => value.toString(16).padStart(2, "0")).join("");
}

async function createSession(mimeType) {
  const externalId = document.querySelector("#externalId").value.trim();
  const fullName = document.querySelector("#fullName").value.trim();
  const sourceLanguage = document.querySelector("#sourceLanguage").value.trim();
  const patient = externalId || fullName ? { external_id: externalId || null, full_name: fullName || null } : null;
  const response = await fetch("/api/v1/sessions", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ patient, source_language: sourceLanguage || null, audio_content_type: mimeType })
  });
  if (!response.ok) throw new Error(`Session creation failed (${response.status})`);
  return response.json();
}

async function uploadPending() {
  if (!activeSessionId) return;
  if (uploadRunning) {
    uploadRequested = true;
    return;
  }
  uploadRunning = true;
  uploadRequested = false;
  try {
    const chunks = await sessionChunks(activeSessionId);
    if (!chunks.length) return;
    progressEl.max = chunks.length;
    let uploaded = 0;
    for (const chunk of chunks) {
      const checksum = await digest(chunk.blob);
      const query = new URLSearchParams({ sequence_number: chunk.sequence, is_final: String(chunk.isFinal) });
      const response = await fetch(`/api/v1/sessions/${activeSessionId}/chunks?${query}`, {
        method: "POST",
        headers: { "Content-Type": chunk.mimeType || "application/octet-stream", "X-Chunk-SHA256": checksum },
        body: chunk.blob
      });
      if (!response.ok) throw new Error(`Chunk ${chunk.sequence} failed (${response.status})`);
      progressEl.value = ++uploaded;
      setStatus(`Uploaded ${uploaded} of ${chunks.length} local chunks.`);
    }
    const response = await fetch(`/api/v1/sessions/${activeSessionId}/upload`);
    if (!response.ok) throw new Error("Could not read upload status");
    const state = await response.json();
    detailEl.textContent = JSON.stringify(state, null, 2);
    if (state.assembled) {
      await clearSession(activeSessionId);
      setStatus("Recording safely assembled on this PC.");
      localStorage.setItem("clinicalScribeLastSessionId", activeSessionId);
      document.querySelector("#reviewSessionId").value = activeSessionId;
      localStorage.removeItem("clinicalScribeSessionId");
      activeSessionId = null;
      startButton.disabled = false;
    } else if (state.missing_chunks.length) {
      setStatus(`Waiting for missing chunks: ${state.missing_chunks.join(", ")}`);
    } else if (!recordingStopped) {
      setStatus("Recording locally; uploaded chunks remain recoverable.");
    }
  } catch (error) {
    setStatus(`Upload paused. Audio remains local. ${error.message}`);
  } finally {
    uploadRunning = false;
    if (uploadRequested) uploadPending();
  }
}

startButton.addEventListener("click", async () => {
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    recorder = new MediaRecorder(stream);
    const session = await createSession(recorder.mimeType);
    activeSessionId = session.id;
    localStorage.setItem("clinicalScribeSessionId", activeSessionId);
    nextSequence = 0;
    recordingStopped = false;
    recorder.ondataavailable = async event => {
      if (!event.data.size) return;
      await putChunk({ sessionId: activeSessionId, sequence: nextSequence++, blob: event.data, mimeType: recorder.mimeType, isFinal: false });
      setStatus(`${nextSequence} audio chunks saved locally.`);
      if (navigator.onLine) uploadPending();
    };
    recorder.onstop = async () => {
      recordingStopped = true;
      const chunks = await sessionChunks(activeSessionId);
      if (chunks.length) {
        const last = chunks[chunks.length - 1];
        last.isFinal = true;
        await putChunk(last);
      }
      stream.getTracks().forEach(track => track.stop());
      await uploadPending();
    };
    recorder.start(5000);
    startButton.disabled = true;
    stopButton.disabled = false;
    setStatus("Recording. Each chunk is stored locally before upload.");
  } catch (error) {
    stream?.getTracks().forEach(track => track.stop());
    setStatus(`Could not start: ${error.message}`);
  }
});

stopButton.addEventListener("click", () => {
  stopButton.disabled = true;
  if (recorder?.state === "recording") recorder.stop();
});

document.querySelector("#retry").addEventListener("click", uploadPending);
window.addEventListener("online", uploadPending);
if (activeSessionId) {
  startButton.disabled = true;
  setStatus("Recoverable local recording found. Retrying upload.");
  uploadPending();
}

const reviewSessionInput = document.querySelector("#reviewSessionId");
const reviewStatus = document.querySelector("#reviewStatus");
const speakersEl = document.querySelector("#speakers");
const conversationEl = document.querySelector("#conversation");
const saveReviewButton = document.querySelector("#saveReview");
let currentReview = null;
reviewSessionInput.value ||= activeSessionId || localStorage.getItem("clinicalScribeLastSessionId") || "";

function roleOptions(selected) {
  return ["UNKNOWN", "DOCTOR", "PATIENT", "OTHER"]
    .map(role => `<option value="${role}" ${role === selected ? "selected" : ""}>${role[0] + role.slice(1).toLowerCase()}</option>`)
    .join("");
}

function escapeHtml(value) {
  const node = document.createElement("span");
  node.textContent = value ?? "";
  return node.innerHTML;
}

function renderReview(review) {
  currentReview = review;
  speakersEl.innerHTML = `<h3>Detected speakers</h3>${review.speakers.map(speaker => `
    <div class="speaker-editor" data-speaker-id="${speaker.id}">
      <label>Detected voice<input value="${escapeHtml(speaker.diarization_label)}" disabled></label>
      <label>Display name<input class="speaker-name" value="${escapeHtml(speaker.display_name || "")}"></label>
      <label>Clinical role<select class="speaker-role">${roleOptions(speaker.role)}</select></label>
      <label>Merge voice into<select class="speaker-merge"><option value="">Do not merge</option>${review.speakers.filter(item => item.id !== speaker.id).map(item => `<option value="${item.id}">${escapeHtml(item.display_name || item.diarization_label)}</option>`).join("")}</select></label>
    </div>`).join("")}`;
  const speakerOptions = review.speakers.map(speaker =>
    `<option value="${speaker.id}">${escapeHtml(speaker.display_name || speaker.diarization_label)} — ${speaker.role}</option>`
  ).join("");
  conversationEl.innerHTML = `<h3>Conversation</h3>${review.segments.map(segment => `
    <article class="segment" data-segment-id="${segment.segment_id}">
      <div class="segment-head">
        <select class="segment-speaker">${speakerOptions}</select>
        <span class="metadata">${(segment.start_ms / 1000).toFixed(1)}–${(segment.end_ms / 1000).toFixed(1)}s · ${escapeHtml(segment.source_language)} · confidence ${(segment.diarization_confidence * 100).toFixed(0)}%</span>
      </div>
      <p class="source-text" lang="${escapeHtml(segment.source_language)}">${escapeHtml(segment.original_text)}</p>
      ${segment.english_text && segment.english_text !== segment.clean_text ? `<p class="english-text"><strong>English:</strong> ${escapeHtml(segment.english_text)}</p>` : ""}
      <span class="metadata">${escapeHtml(segment.translation_status)}</span>
    </article>`).join("")}`;
  review.segments.forEach(segment => {
    conversationEl.querySelector(`[data-segment-id="${segment.segment_id}"] .segment-speaker`).value = segment.speaker_id;
  });
  saveReviewButton.hidden = false;
  reviewStatus.textContent = `${review.segments.length} segments, ${review.speakers.length} detected speakers. Source text preserved.`;
}

async function loadReview() {
  const sessionId = reviewSessionInput.value.trim();
  if (!sessionId) return;
  reviewStatus.textContent = "Loading review…";
  const response = await fetch(`/api/v1/sessions/${encodeURIComponent(sessionId)}/speaker-review`);
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    reviewStatus.textContent = error.detail || `Review unavailable (${response.status}). The worker may still be processing.`;
    return;
  }
  renderReview(await response.json());
  localStorage.setItem("clinicalScribeLastSessionId", sessionId);
}

document.querySelector("#loadReview").addEventListener("click", () => loadReview().catch(error => {
  reviewStatus.textContent = `Could not load review: ${error.message}`;
}));

saveReviewButton.addEventListener("click", async () => {
  if (!currentReview) return;
  saveReviewButton.disabled = true;
  reviewStatus.textContent = "Saving an audited correction revision…";
  try {
    const speakers = [...speakersEl.querySelectorAll(".speaker-editor")].map(editor => ({
      speaker_id: editor.dataset.speakerId,
      display_name: editor.querySelector(".speaker-name").value.trim() || null,
      role: editor.querySelector(".speaker-role").value,
      merge_into_speaker_id: editor.querySelector(".speaker-merge").value || null
    }));
    const segment_assignments = [...conversationEl.querySelectorAll(".segment")].map(segment => ({
      segment_id: segment.dataset.segmentId,
      speaker_id: segment.querySelector(".segment-speaker").value
    }));
    const response = await fetch(`/api/v1/sessions/${currentReview.session_id}/speaker-corrections`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ actor: "local-clinician", speakers, segment_assignments })
    });
    if (!response.ok) throw new Error((await response.json()).detail || `Save failed (${response.status})`);
    renderReview(await response.json());
    reviewStatus.textContent = "Corrections saved as a new audited revision.";
  } catch (error) {
    reviewStatus.textContent = `Could not save: ${error.message}`;
  } finally {
    saveReviewButton.disabled = false;
  }
});
