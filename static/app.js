// Frontend: webcam capture / file upload -> POST /api/solve -> render.

const MAX_LONG_EDGE = 1568; // largest size Claude uses

const video = document.getElementById("video");
const canvas = document.getElementById("canvas");
const cameraOff = document.getElementById("camera-off");
const btnCamera = document.getElementById("btn-camera");
const btnCapture = document.getElementById("btn-capture");
const fileInput = document.getElementById("file-input");
const statusEl = document.getElementById("status");
const resultsEl = document.getElementById("results");
const answersEl = document.getElementById("answers");
const warningsEl = document.getElementById("warnings");

let stream = null;
let busy = false;

function setStatus(text, isError = false) {
  statusEl.hidden = !text;
  statusEl.textContent = text || "";
  statusEl.classList.toggle("error", isError);
}

btnCamera.addEventListener("click", async () => {
  if (stream) {
    stream.getTracks().forEach((t) => t.stop());
    stream = null;
    video.srcObject = null;
    cameraOff.hidden = false;
    btnCamera.textContent = "Turn on camera";
    btnCapture.disabled = true;
    return;
  }
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      video: { width: { ideal: 1920 }, height: { ideal: 1080 } },
      audio: false,
    });
  } catch (err) {
    setStatus(`Could not access the camera: ${err.message}`, true);
    return;
  }
  video.srcObject = stream;
  cameraOff.hidden = true;
  btnCamera.textContent = "Turn off camera";
  btnCapture.disabled = false;
  setStatus("");
});

btnCapture.addEventListener("click", () => {
  if (busy || !stream) return;
  const w = video.videoWidth;
  const h = video.videoHeight;
  if (!w || !h) {
    setStatus("The camera isn't providing an image yet; wait a second.", true);
    return;
  }
  const scale = Math.min(1, MAX_LONG_EDGE / Math.max(w, h));
  canvas.width = Math.round(w * scale);
  canvas.height = Math.round(h * scale);
  canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
  canvas.toBlob((blob) => sendImage(blob, "capture.jpg"), "image/jpeg", 0.92);
});

fileInput.addEventListener("change", () => {
  const file = fileInput.files[0];
  if (file && !busy) sendImage(file, file.name);
  fileInput.value = "";
});

async function sendImage(blob, filename) {
  busy = true;
  btnCapture.disabled = true;
  setStatus("Analyzing with Claude…");
  resultsEl.hidden = true;

  const form = new FormData();
  form.append("image", blob, filename);
  try {
    const resp = await fetch("/api/solve", { method: "POST", body: form });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      throw new Error(body.detail || `Error ${resp.status}`);
    }
    renderResult(await resp.json());
    setStatus("");
  } catch (err) {
    setStatus(`Error: ${err.message}`, true);
  } finally {
    busy = false;
    btnCapture.disabled = !stream;
  }
}

function renderResult(result) {
  answersEl.replaceChildren();
  warningsEl.replaceChildren();

  for (const a of result.answers) {
    const card = document.createElement("article");
    card.className = "answer-card";

    const title = document.createElement("h3");
    title.textContent = `${a.question_number}. ${a.question_text}`;

    const chosen = document.createElement("p");
    chosen.className = "chosen";
    chosen.textContent = a.chosen_option;
    const badge = document.createElement("span");
    badge.className = `badge ${a.confidence}`;
    badge.textContent = `${a.confidence} confidence`;
    chosen.appendChild(badge);

    const why = document.createElement("p");
    why.className = "reasoning";
    why.textContent = a.reasoning;

    card.append(title, chosen, why);
    answersEl.appendChild(card);
  }

  if (result.answers.length === 0) {
    const empty = document.createElement("p");
    empty.textContent = "No question could be read in the image.";
    answersEl.appendChild(empty);
  }

  for (const w of result.warnings) {
    const p = document.createElement("p");
    p.className = "warning";
    p.textContent = `⚠ ${w}`;
    warningsEl.appendChild(p);
  }

  resultsEl.hidden = false;
  resultsEl.scrollIntoView({ behavior: "smooth" });
}
