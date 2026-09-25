# Satori — camera quiz solver

Capture a quiz with the camera and solve it with Claude (vision). Two frontends —
desktop and web — share the same "brain", and optionally publish the answers over
MQTT (e.g. to an ESP32). Built for demos, your own forms, and accessibility.

> *Satori* (悟り): sudden understanding — getting something at a glance.

## Structure

```
camera-ai/
├── app.py              # Desktop frontend (OpenCV): live preview + capture
├── server.py           # Web frontend (FastAPI): serves /static and POST /api/solve
├── static/             # Web UI (HTML/CSS/JS, capture via getUserMedia) + logo
├── satori/
│   ├── solver.py       # "Brain": image -> answers (Claude API)
│   ├── models.py       # Pydantic models (Answer, QuizResult)
│   ├── camera.py       # Webcam / network camera (RTSP), sharpness, JPEG encoding
│   └── publisher.py    # Optional MQTT publishing of the answers
├── captures/           # Created on the fly: .jpg captures + .json answers
└── requirements.txt
```

`satori/solver.py` does not depend on the camera or the UI: both frontends import
the same `QuizSolver.solve()`.

## Install

```powershell
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Credentials — copy the template and paste your API key:

```powershell
copy .env.example .env
notepad .env
```

The `.env` is in `.gitignore`, so it never lands in a repo. Recommended: create the
key at https://console.anthropic.com with an **expiration date**. If you prefer an
environment variable (`setx ANTHROPIC_API_KEY ...`) or `ant auth login`, those also
work and take precedence over the `.env`.

## Usage — desktop

```powershell
python app.py              # camera 0
python app.py --camera 1   # another camera
python app.py --camera rtsp://192.168.1.9:554/live/ch00_0   # network camera
python app.py --camera net       # network camera from CAMERA_URL in .env
python app.py --file photo.jpg   # test with an image, no camera
```

In the preview window:

- **SPACE** — capture the current frame and send it to Claude
- **A** — toggle auto-capture (on by default)
- **S** — open the settings dialog (API key + MQTT)
- **Q / ESC** — quit
- The **sharpness** indicator turns green when the image is in focus.

**Auto-capture** (no key needed): when the image is sharp and holds still for ~1s,
it captures on its own. It re-arms after the scene clearly moves, so the same page
isn't captured twice — put a quiz in view, hold steady, move it away, put the next.

Answers are printed to the console and saved (along with the capture) in `captures/`.

## Usage — web

```powershell
python -m uvicorn server:app --port 8756
```

Open http://localhost:8756, turn on the camera (the browser asks for permission —
this works on localhost without HTTPS) or upload a photo. Answers are shown as cards
with the chosen option, the reasoning, and a confidence level.

## Network camera (optional)

Besides a local webcam, Satori can use a Wi-Fi IP camera that exposes an RTSP (or
HTTP) stream. Set its URL as `CAMERA_URL` in the `.env` or in the web Settings
panel. For example, a V380 camera with RTSP enabled (a `ceshi.ini` file on its
microSD with `[CONST_PARAM]` / `rtsp=1`):

```
rtsp://192.168.1.9:554/live/ch00_0    # main stream (ch00_1 = low-res substream)
```

- **Web:** pick **Network camera** next to the camera button. The server reads the
  stream itself: the page shows an MJPEG preview (`GET /api/camera/stream`) and
  **Capture and solve** grabs the newest full-resolution frame on the server
  (`POST /api/camera/solve`). Because the server does the reading, this works from
  any device on the network, not only the PC.
- **Desktop:** `python app.py --camera net` (or pass the URL directly).

The stream is read on a background thread that keeps only the newest frame (so
captures are never seconds behind) and reconnects if the camera drops off the
Wi-Fi. RTSP is forced over TCP, since UDP smears frames on Wi-Fi.

## MQTT (optional)

If you set `MQTT_BROKER` in the `.env`, both frontends publish the answers to the
`MQTT_TOPIC` topic (default `satori/answers`) as compact JSON:

```json
{"ts": 1788205836, "count": 4, "answers": [{"n": 1, "ans": "b) Paris", "conf": "high"}]}
```

It is published with `retain=True`, so a client (e.g. an ESP32) receives the last
answer as soon as it connects. Use the PC's local-network IP as the broker (not
`127.0.0.1`, which an ESP32 could not reach). Without `MQTT_BROKER`, everything runs
the same without publishing anything.

### Remote capture trigger

Both apps also *subscribe* to a command topic and capture whenever a message lands
on it — so any external device (a hardware button, a phone, a script) can trigger a
scan over the network. The interface is intentionally minimal:

- **Broker**: the same one set in `MQTT_BROKER` (e.g. the ESP32 broker at
  `192.168.1.21:1883`). If the broker is down, Satori keeps retrying and connects as
  soon as it comes up.
- **Topic**: `satori/capture` (override with `MQTT_CAPTURE_TOPIC`).
- **Payload**: ignored — *any* message triggers one capture.
- **Optional confirmation**: send JSON with `reply_to` (a topic) and `seq`; once the
  capture is solved (or fails), Satori publishes `{"seq": N, "ok": true|false,
  "detail": "..."}` on `reply_to`. A JSON `action` other than `"click"` does not
  trigger a capture (it is acked with `ok: false`).

Where the capture comes from:

- **Desktop app**: the current frame of whatever camera it is using.
- **Web server**: the **network camera** (`CAMERA_URL`), read server-side — so no
  browser needs to be open. Open pages get the progress and the answers pushed
  through `GET /api/events` (Server-Sent Events) and render them as usual. Without a
  network camera the command is acked with `ok: false`.

Any MQTT publisher works. From a PC:

```bash
mosquitto_pub -h 192.168.1.21 -t satori/capture -m go
```

The ready-made button is [satori-button](https://github.com/cflarios/satori-button)
(M5Stack Atom Lite): a click publishes to `satori/capture` with `reply_to`, and its
LED stays blue while Claude analyzes, then turns green (solved) or red (failed).

## Technical notes

- Model: `claude-opus-5`, with server-side refusal fallbacks enabled (if the model
  declines, the API retries with a fallback model in the same call).
- Structured output (`output_config.format` with JSON Schema) validated with
  Pydantic — the model always returns parseable JSON.
- Images are resized to a max of 1568 px on the long edge before sending (that is the
  most Claude uses; sending more just wastes bandwidth).
- Rough cost: ~1–3 US cents per quiz.

## Scope

Intended for demos, solving your own forms, and accessibility. Not for use on other
people's real exams or graded assessments.
