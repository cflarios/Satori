"""Web version: FastAPI serves the static frontend and exposes the solver.

Usage:
    .venv\\Scripts\\python -m uvicorn server:app --port 8756

The browser captures the image (webcam or file) and sends it to POST /api/solve;
the brain is the same QuizSolver used by the desktop app.
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.staticfiles import StaticFiles

from satori import MqttPublisher, QuizResult, QuizSolver, QuizSolverError

load_dotenv(Path(__file__).parent / ".env")
if not os.environ.get("ANTHROPIC_API_KEY"):
    print(
        "Warning: ANTHROPIC_API_KEY not found (no .env either). "
        "Requests to /api/solve will fail.",
        file=sys.stderr,
    )

MAX_UPLOAD_BYTES = 10 * 1024 * 1024

solver = QuizSolver()
mqtt = MqttPublisher.from_env()


@asynccontextmanager
async def lifespan(app: FastAPI):
    if mqtt:
        try:
            mqtt.connect()
            print(f"MQTT: publishing to {mqtt.broker}:{mqtt.port} topic '{mqtt.topic}'")
        except Exception as exc:  # noqa: BLE001
            print(f"MQTT: could not connect ({exc}); continuing without publishing.", file=sys.stderr)
    yield
    if mqtt:
        mqtt.disconnect()


app = FastAPI(title="Satori", lifespan=lifespan)


@app.post("/api/solve", response_model=QuizResult)
async def solve(image: UploadFile = File(...)) -> QuizResult:
    data = await image.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty image.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Image too large (max 10 MB).")
    media_type = image.content_type or "image/jpeg"
    if not media_type.startswith("image/"):
        raise HTTPException(status_code=415, detail=f"Unsupported type: {media_type}")
    try:
        # The solver is synchronous; use a threadpool so the event loop is not blocked.
        result = await run_in_threadpool(solver.solve, data, media_type)
    except QuizSolverError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if mqtt:
        try:
            mqtt.publish(result)
        except Exception as exc:  # noqa: BLE001 - publishing must never break the response
            print(f"MQTT: failed to publish ({exc}).", file=sys.stderr)
    return result


app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")
