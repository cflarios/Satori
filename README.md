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
│   ├── camera.py       # Webcam, sharpness, JPEG encoding (desktop only)
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
python app.py --file photo.jpg   # test with an image, no camera
```

In the preview window:

- **SPACE** — capture the current frame and send it to Claude
- **Q / ESC** — quit
- The **sharpness** indicator turns green when the image is in focus; capture when
  it is green and the quiz fills the frame.

Answers are printed to the console and saved (along with the capture) in `captures/`.

## Usage — web

```powershell
python -m uvicorn server:app --port 8756
```

Open http://localhost:8756, turn on the camera (the browser asks for permission —
this works on localhost without HTTPS) or upload a photo. Answers are shown as cards
with the chosen option, the reasoning, and a confidence level.

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
