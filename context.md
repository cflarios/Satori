# Satori — project context

Context document for humans and AI assistants working on this repo. It captures the
"why" that the code alone doesn't show. Keep it current when decisions change.

## What this is

Satori reads a multiple-choice quiz through a camera and answers it with Claude's
vision. There are two frontends over one shared core:

- **Desktop** (`app.py`, OpenCV): live webcam preview, press SPACE to capture.
- **Web** (`server.py`, FastAPI + `static/`): capture via `getUserMedia` in the
  browser, or upload a photo.

Both call the same `satori.QuizSolver`, so the "brain" is written once.

## Goal & scope

- **Goal:** given a photo of a quiz, return each question's chosen option, a short
  justification, and a confidence level — reliably and cheaply.
- **Intended use:** demos, solving your own forms, accessibility.
- **Out of scope (ethical line):** other people's real exams or graded assessments.
  Keep this in mind for any feature request.

## Architecture

```
camera/browser ──image──> QuizSolver.solve() ──> Claude (vision, JSON Schema) ──> QuizResult
                                                                                     │
                          desktop: print + save to captures/                        │
                          web: JSON response rendered as cards                       │
                          both: optional MQTT publish (retained) ───────────────────┘
```

- **`satori/solver.py`** — the core. Sends image(s) as base64 to `claude-opus-5`
  with a system prompt and a JSON Schema (`output_config.format`), validates the
  reply into `QuizResult`. UI- and camera-agnostic on purpose.
- **`satori/models.py`** — `Answer` / `QuizResult` (Pydantic). `confidence` is a
  `Literal["high","medium","low"]`; this enum is mirrored in the solver's schema,
  the web badge CSS classes, and the MQTT payload.
- **`satori/camera.py`** — OpenCV webcam wrapper (`Camera`), network stream reader
  (`NetworkCamera`: RTSP/HTTP on a background thread, keeps only the newest frame,
  auto-reconnects), `open_camera()` picks one from an index or URL,
  Laplacian-variance sharpness metric, JPEG encode with downscale to 1568 px.
- **`satori/publisher.py`** — optional MQTT publisher (`paho-mqtt`). `from_env()`
  returns `None` when `MQTT_BROKER` is unset, so MQTT is fully optional.
- **`app.py`** — desktop UI loop; analysis runs on a worker thread so the video
  doesn't freeze; saves capture + answers JSON to `captures/`. Triggers: SPACE
  (manual), auto-capture on stability (`A` toggles it), and a remote MQTT command
  on `satori/capture`. Press `S` for the settings dialog.
- **`server.py`** — FastAPI: `POST /api/solve` (multipart image) runs the solver in
  a threadpool; static files served at `/`. MQTT connect/disconnect via `lifespan`.
  Network camera (`CAMERA_URL`): `GET /api/camera/stream` (MJPEG preview, 960 px,
  ~12 fps) and `POST /api/camera/solve` (captures the newest full-res frame
  server-side). The reader starts lazily on first use and restarts when the URL
  changes in Settings. Also subscribes to `satori/capture`: remote captures use
  the network camera and are pushed to open pages via `GET /api/events` (SSE).
- **`satori/publisher.py`** also holds `CaptureCommand` (parses the optional
  `reply_to`/`seq`/`action` JSON of a capture command) and `MqttPublisher.ack()`.
- **`static/`** — `index.html`, `app.js`, `style.css`, `logo.svg` (favicon + header).

## Key decisions & rationale

- **Model `claude-opus-5`** with server-side refusal fallbacks (`fallbacks="default"`,
  beta `server-side-fallback-2026-07-01`). Opus reads text in imperfect photos well;
  cost is ~1–3¢/quiz. Switching to `claude-sonnet-5` is a one-line change if volume
  grows.
- **Structured output via JSON Schema**, validated with Pydantic — answers are always
  parseable; no brittle text parsing.
- **Resize to 1568 px before sending** — that's the most Claude uses; larger just
  wastes bandwidth. The browser resizes on a canvas; the desktop resizes in OpenCV.
- **Shared core, thin frontends** — this is what made the web version cheap to add
  and keeps a future refactor localized.
- **MQTT is optional and retained** — `retain=True` so a late-connecting ESP32 still
  gets the last answer. It never blocks or breaks the solve path.
- **Network camera read by the server, not the browser** — browsers can't play
  RTSP, and reading it server-side avoids an extra relay (go2rtc/OBS), gives a
  full-resolution capture instead of a preview frame, and works from any device
  on the LAN. The webcam path still captures in the browser.
- **Remote capture acks after solving, not on receipt** — the button's LED stays
  blue while Claude works and turns green/red on the real outcome. The ack goes to
  the `reply_to` the device sends, so Satori doesn't hard-code device topics.
  MQTT connects asynchronously so a broker that boots later (the ESP32) is picked up.
- **Credentials** via `.env` (git-ignored) or environment/`ant` profile. Never commit
  keys; prefer keys with an expiration date.

## Conventions

- Code, comments, UI copy, and prompts are **English**.
- `confidence` values are `high` / `medium` / `low` everywhere (schema, models, CSS
  class names, MQTT `conf`). Change all of them together if ever revised.
- Default MQTT topic is `satori/answers`.
- Web dev server runs on port 8756 (see `.claude/launch.json`).

## Current status

- Desktop and web frontends: working and tested end-to-end.
- MQTT publishing: implemented and unit-verified (payload shape).
- Network camera (V380 over RTSP) and remote capture from the satori-button
  (M5 Atom Lite) through the ESP32 broker: tested end-to-end, button → capture →
  Claude → ack on the LED + answers in the web page.
- Logo: `static/logo.svg` — enso + camera aperture + insight spark ("camera" logo).
  `static/eye.svg` is an unused alternate illustration kept for reference.

## Pending / next steps

- **ESP32 receiver** — subscribe to `satori/answers` and react to the answers. This
  is the immediate next task. Will also need a local Mosquitto broker configured to
  accept LAN connections (`listener 1883 0.0.0.0`, and either `allow_anonymous true`
  or the user/password already supported in `.env`).
- Possibly a simplified favicon mark for very small sizes.
- The web version does not persist captures like the desktop does (no `captures/`
  history) — add if a history/gallery is wanted.

## Running quickly

```powershell
py -m venv .venv; .venv\Scripts\activate; pip install -r requirements.txt
copy .env.example .env   # then paste ANTHROPIC_API_KEY
python app.py                                  # desktop
python -m uvicorn server:app --port 8756       # web -> http://localhost:8756
```
