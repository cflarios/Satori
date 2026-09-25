// Frontend: webcam capture / network camera / file upload -> solve -> render.
//
// Two camera sources:
// - webcam:  getUserMedia in the browser; the frame is captured on a canvas and
//            uploaded to POST /api/solve.
// - network: the server reads the RTSP stream (CAMERA_URL); the browser shows
//            its MJPEG preview and POST /api/camera/solve captures server-side.

const MAX_LONG_EDGE = 1568; // largest size Claude uses

const video = document.getElementById("video");
const netcam = document.getElementById("netcam");
const canvas = document.getElementById("canvas");
const cameraOff = document.getElementById("camera-off");
const sourceSelect = document.getElementById("source");
const networkOption = sourceSelect.querySelector('option[value="network"]');
const btnCamera = document.getElementById("btn-camera");
const btnCapture = document.getElementById("btn-capture");
const fileInput = document.getElementById("file-input");
const statusEl = document.getElementById("status");
const resultsEl = document.getElementById("results");
const answersEl = document.getElementById("answers");
const warningsEl = document.getElementById("warnings");

let stream = null;      // webcam MediaStream while on
let netcamOn = false;   // network camera preview while on
let busy = false;

const cameraOn = () => Boolean(stream) || netcamOn;

function setStatus(text, isError = false) {
  statusEl.hidden = !text;
  statusEl.textContent = text || "";
  statusEl.classList.toggle("error", isError);
}

function setCameraUi(on) {
  cameraOff.hidden = on;
  btnCamera.textContent = on ? "Turn off camera" : "Turn on camera";
  btnCapture.disabled = !on || busy;
}

function stopCamera() {
  if (stream) {
    stream.getTracks().forEach((t) => t.stop());
    stream = null;
    video.srcObject = null;
  }
  if (netcamOn) {
    netcamOn = false;
    netcam.removeAttribute("src"); // closes the MJPEG connection
    netcam.hidden = true;
  }
  video.hidden = false;
  setCameraUi(false);
}

async function startWebcam() {
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
  setCameraUi(true);
  setStatus("");
}

function startNetcam() {
  netcamOn = true;
  video.hidden = true;
  netcam.hidden = false;
  // Cache-buster so re-enabling opens a fresh stream.
  netcam.src = `/api/camera/stream?t=${Date.now()}`;
  setCameraUi(true);
  setStatus("Connecting to the network camera…");
}

netcam.addEventListener("load", () => {
  if (netcamOn) setStatus("");
});
netcam.addEventListener("error", () => {
  if (!netcamOn) return;
  stopCamera();
  setStatus("Could not open the network camera. Check the URL in Settings.", true);
});

btnCamera.addEventListener("click", () => {
  if (cameraOn()) stopCamera();
  else if (sourceSelect.value === "network") startNetcam();
  else startWebcam();
});

sourceSelect.addEventListener("change", () => {
  if (cameraOn()) stopCamera();
});

// Enable the network source only when the server has a camera URL.
function applyCameraConfig(c) {
  const hasUrl = Boolean(c.camera_url);
  networkOption.disabled = !hasUrl;
  networkOption.textContent = hasUrl ? "Network camera" : "Network camera (set it in Settings)";
  if (!hasUrl && sourceSelect.value === "network") {
    sourceSelect.value = "webcam";
    if (netcamOn) stopCamera();
  }
}

fetch("/api/config")
  .then((r) => r.json())
  .then(applyCameraConfig)
  .catch(() => {});

btnCapture.addEventListener("click", () => {
  if (busy || !cameraOn()) return;
  if (netcamOn) {
    // Server grabs the newest full-resolution frame itself.
    solve("/api/camera/solve");
    return;
  }
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

function sendImage(blob, filename) {
  const form = new FormData();
  form.append("image", blob, filename);
  solve("/api/solve", form);
}

// POSTs to a solve endpoint (optionally with an image form) and renders the result.
async function solve(url, body) {
  busy = true;
  btnCapture.disabled = true;
  setStatus("Analyzing with Claude…");
  resultsEl.hidden = true;

  try {
    const resp = await fetch(url, { method: "POST", body });
    if (!resp.ok) {
      const detail = await resp.json().catch(() => ({}));
      throw new Error(detail.detail || `Error ${resp.status}`);
    }
    renderResult(await resp.json());
    setStatus("");
  } catch (err) {
    setStatus(`Error: ${err.message}`, true);
  } finally {
    busy = false;
    btnCapture.disabled = !cameraOn();
  }
}

// Remote captures (MQTT button): the server captures from the network camera
// and pushes progress + result here, whatever source this page is using.
const events = new EventSource("/api/events");
events.addEventListener("message", (e) => {
  const ev = JSON.parse(e.data);
  if (ev.type === "capture") {
    busy = true;
    btnCapture.disabled = true;
    resultsEl.hidden = true;
    setStatus("Remote capture — analyzing with Claude…");
  } else if (ev.type === "result") {
    renderResult(ev.result);
    setStatus("");
  } else if (ev.type === "error") {
    setStatus(`Remote capture error: ${ev.detail}`, true);
  }
  if (ev.type !== "capture") {
    busy = false;
    btnCapture.disabled = !cameraOn();
  }
});

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

// --- Settings sidebar -----------------------------------------------------
const btnSettings = document.getElementById("btn-settings");
const btnSettingsClose = document.getElementById("btn-settings-close");
const settingsEl = document.getElementById("settings");
const settingsBackdrop = document.getElementById("settings-backdrop");
const settingsForm = document.getElementById("settings-form");
const settingsStatus = document.getElementById("settings-status");
const cfg = {
  apikey: document.getElementById("cfg-apikey"),
  apikeyHint: document.getElementById("apikey-hint"),
  broker: document.getElementById("cfg-broker"),
  port: document.getElementById("cfg-port"),
  topic: document.getElementById("cfg-topic"),
  user: document.getElementById("cfg-user"),
  pass: document.getElementById("cfg-pass"),
  passHint: document.getElementById("pass-hint"),
  qos: document.getElementById("cfg-qos"),
  cameraUrl: document.getElementById("cfg-camera-url"),
};

function setSettingsStatus(text, isError = false) {
  settingsStatus.hidden = !text;
  settingsStatus.textContent = text || "";
  settingsStatus.classList.toggle("error", isError);
}

function fillSettings(c) {
  cfg.broker.value = c.mqtt_broker || "";
  cfg.port.value = c.mqtt_port || "1883";
  cfg.topic.value = c.mqtt_topic || "satori/answers";
  cfg.user.value = c.mqtt_user || "";
  cfg.qos.value = c.mqtt_qos || "0";
  cfg.cameraUrl.value = c.camera_url || "";
  applyCameraConfig(c);
  cfg.apikey.value = "";
  cfg.pass.value = "";
  cfg.apikeyHint.textContent = c.anthropic_api_key_set
    ? "A key is already set — leave blank to keep it."
    : "No key set yet.";
  cfg.passHint.textContent = c.mqtt_password_set
    ? "A password is already set — leave blank to keep it."
    : "Leave blank for an anonymous broker.";
}

function closeSettings() {
  settingsEl.classList.remove("open");
  settingsEl.setAttribute("aria-hidden", "true");
  settingsBackdrop.hidden = true;
}

async function openSettings() {
  settingsEl.classList.add("open");
  settingsEl.setAttribute("aria-hidden", "false");
  settingsBackdrop.hidden = false;
  setSettingsStatus("");
  try {
    const resp = await fetch("/api/config");
    fillSettings(await resp.json());
  } catch (err) {
    setSettingsStatus(`Could not load settings: ${err.message}`, true);
  }
}

btnSettings.addEventListener("click", () => {
  if (settingsEl.classList.contains("open")) closeSettings();
  else openSettings();
});
btnSettingsClose.addEventListener("click", closeSettings);
settingsBackdrop.addEventListener("click", closeSettings);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && settingsEl.classList.contains("open")) closeSettings();
});

settingsForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  // MQTT (non-secret) fields are always sent; secrets only when typed.
  const body = {
    mqtt_broker: cfg.broker.value.trim(),
    mqtt_port: cfg.port.value.trim() || "1883",
    mqtt_topic: cfg.topic.value.trim() || "satori/answers",
    mqtt_user: cfg.user.value.trim(),
    mqtt_qos: cfg.qos.value,
    camera_url: cfg.cameraUrl.value.trim(),
  };
  if (cfg.apikey.value.trim()) body.anthropic_api_key = cfg.apikey.value.trim();
  if (cfg.pass.value) body.mqtt_password = cfg.pass.value;

  setSettingsStatus("Saving…");
  try {
    const resp = await fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!resp.ok) {
      const b = await resp.json().catch(() => ({}));
      throw new Error(b.detail || `Error ${resp.status}`);
    }
    const data = await resp.json();
    fillSettings(data.config);
    const bits = [];
    if (data.applied.anthropic) bits.push("API key");
    if (data.applied.mqtt) bits.push("MQTT");
    if (data.applied.camera) {
      bits.push("network camera");
      // The server closed the old stream; reopen the preview on the new URL.
      if (netcamOn) {
        stopCamera();
        if (!networkOption.disabled) startNetcam();
      }
    }
    setSettingsStatus(bits.length ? `Saved and applied: ${bits.join(", ")}.` : "Saved.");
  } catch (err) {
    setSettingsStatus(`Error: ${err.message}`, true);
  }
});
