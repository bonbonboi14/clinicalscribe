// Phone recording UI
const pairingCard = document.querySelector("#pairingCard");
const recordingCard = document.querySelector("#recordingCard");
const pairingCodeInput = document.querySelector("#pairingCodeInput");
const pairButton = document.querySelector("#pairButton");
const pairingStatus = document.querySelector("#pairingStatus");
const startRecordButton = document.querySelector("#startRecord");
const stopRecordButton = document.querySelector("#stopRecord");
const uploadButton = document.querySelector("#uploadButton");
const discardButton = document.querySelector("#discardButton");
const statusEl = document.querySelector("#status");
const timerEl = document.querySelector("#timer");
const progressEl = document.querySelector("#progress");

let pairingCode = null;
let mediaRecorder = null;
let recordedChunks = [];
let sessionId = null;
let recordingStartTime = null;
let timerInterval = null;

function setStatus(message, type = null) {
  statusEl.textContent = message;
  statusEl.className = type || "";
}

function setPairingStatus(message, type = null) {
  pairingStatus.textContent = message;
  pairingStatus.className = type || "";
}

function formatTime(seconds) {
  const mins = Math.floor(seconds / 60);
  const secs = seconds % 60;
  return `${String(mins).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
}

function updateTimer() {
  if (recordingStartTime) {
    const elapsed = Math.floor((Date.now() - recordingStartTime) / 1000);
    timerEl.textContent = formatTime(elapsed);
  }
}

// Pairing
pairButton.addEventListener("click", async () => {
  const code = pairingCodeInput.value.trim().toUpperCase();
  if (code.length !== 6) {
    setPairingStatus("Please enter a 6-digit code", "error");
    return;
  }

  try {
    pairButton.disabled = true;
    setPairingStatus("Validating code...");

    const response = await fetch(`/api/v1/phone/validate?code=${code}`);
    const data = await response.json();

    if (data.valid) {
      pairingCode = code;
      setPairingStatus("Connected to desktop!", "success");
      setTimeout(() => {
        pairingCard.classList.add("hidden");
        recordingCard.classList.remove("hidden");
      }, 1000);
    } else {
      setPairingStatus("Invalid or expired code", "error");
      pairButton.disabled = false;
    }
  } catch (error) {
    setPairingStatus(`Connection failed: ${error.message}`, "error");
    pairButton.disabled = false;
  }
});

// Auto-focus pairing input
pairingCodeInput.focus();
pairingCodeInput.addEventListener("input", (e) => {
  e.target.value = e.target.value.toUpperCase();
});

// Detect supported audio MIME types
function getSupportedMimeType() {
  const types = [
    "audio/webm;codecs=opus",
    "audio/webm",
    "audio/mp4",
    "audio/ogg;codecs=opus",
    "audio/ogg"
  ];
  for (const type of types) {
    if (MediaRecorder.isTypeSupported(type)) {
      return type;
    }
  }
  return null;
}

// Start recording
startRecordButton.addEventListener("click", async () => {
  try {
    const mimeType = getSupportedMimeType();
    if (!mimeType) {
      setStatus("No supported audio format found on this browser", "error");
      return;
    }

    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    mediaRecorder = new MediaRecorder(stream, { mimeType });
    recordedChunks = [];

    mediaRecorder.ondataavailable = (event) => {
      if (event.data.size > 0) {
        recordedChunks.push(event.data);
      }
    };

    mediaRecorder.onstop = () => {
      stream.getTracks().forEach(track => track.stop());
      if (timerInterval) {
        clearInterval(timerInterval);
        timerInterval = null;
      }
      uploadButton.classList.remove("hidden");
      discardButton.classList.remove("hidden");
      setStatus(`Recording stopped. ${recordedChunks.length} chunks captured.`);
    };

    mediaRecorder.start(1000); // Capture in 1-second chunks
    recordingStartTime = Date.now();
    timerInterval = setInterval(updateTimer, 1000);

    startRecordButton.classList.add("hidden");
    stopRecordButton.classList.remove("hidden");
    setStatus("🔴 Recording...");
  } catch (error) {
    if (error.name === "NotAllowedError") {
      setStatus("Microphone permission denied. Please allow microphone access.", "error");
    } else {
      setStatus(`Recording failed: ${error.message}`, "error");
    }
  }
});

// Stop recording
stopRecordButton.addEventListener("click", () => {
  if (mediaRecorder && mediaRecorder.state !== "inactive") {
    mediaRecorder.stop();
    stopRecordButton.classList.add("hidden");
  }
});

// Upload recording
uploadButton.addEventListener("click", async () => {
  if (recordedChunks.length === 0) {
    setStatus("No recording to upload", "error");
    return;
  }

  try {
    uploadButton.disabled = true;
    discardButton.disabled = true;
    setStatus("Creating session...");

    // Create session
    const mimeType = mediaRecorder.mimeType;
    const sessionResponse = await fetch("/api/v1/sessions", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Pairing-Code": pairingCode
      },
      body: JSON.stringify({ audio_content_type: mimeType })
    });

    if (!sessionResponse.ok) {
      const error = await sessionResponse.json();
      throw new Error(error.detail || `Session creation failed (${sessionResponse.status})`);
    }

    const session = await sessionResponse.json();
    sessionId = session.id;
    setStatus(`Uploading ${recordedChunks.length} chunks...`);

    // Upload chunks
    const totalChunks = recordedChunks.length;
    for (let i = 0; i < totalChunks; i++) {
      const chunk = recordedChunks[i];
      const arrayBuffer = await chunk.arrayBuffer();
      const uint8Array = new Uint8Array(arrayBuffer);

      // Compute SHA256
      const hashBuffer = await crypto.subtle.digest("SHA-256", uint8Array);
      const hashArray = Array.from(new Uint8Array(hashBuffer));
      const checksum = hashArray.map(b => b.toString(16).padStart(2, "0")).join("");

      const isFinal = i === totalChunks - 1;
      const chunkResponse = await fetch(
        `/api/v1/sessions/${sessionId}/chunks?sequence_number=${i}&is_final=${isFinal}`,
        {
          method: "POST",
          headers: {
            "Content-Type": chunk.type,
            "X-Chunk-SHA256": checksum,
            "X-Pairing-Code": pairingCode
          },
          body: chunk
        }
      );

      if (!chunkResponse.ok) {
        const error = await chunkResponse.json();
        throw new Error(error.detail || `Chunk ${i} upload failed (${chunkResponse.status})`);
      }

      progressEl.value = (i + 1) / totalChunks;
      setStatus(`Uploaded ${i + 1} of ${totalChunks} chunks...`);
    }

    setStatus("Upload complete! Transcription will begin on desktop.", "success");

    // Clear recorded data
    recordedChunks = [];
    recordingStartTime = null;
    timerEl.textContent = "00:00";
    progressEl.value = 0;

    // Reset UI
    setTimeout(() => {
      uploadButton.classList.add("hidden");
      discardButton.classList.add("hidden");
      startRecordButton.classList.remove("hidden");
      uploadButton.disabled = false;
      discardButton.disabled = false;
      setStatus("Ready to record");
    }, 3000);

  } catch (error) {
    setStatus(`Upload failed: ${error.message}`, "error");
    uploadButton.disabled = false;
    discardButton.disabled = false;
  }
});

// Discard recording
discardButton.addEventListener("click", () => {
  recordedChunks = [];
  recordingStartTime = null;
  timerEl.textContent = "00:00";
  progressEl.value = 0;
  uploadButton.classList.add("hidden");
  discardButton.classList.add("hidden");
  startRecordButton.classList.remove("hidden");
  setStatus("Recording discarded. Ready to record.");
});
