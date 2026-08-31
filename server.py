"""Web version: FastAPI serves the static frontend and exposes the solver.

Usage:
    .venv\\Scripts\\python -m uvicorn server:app --port 8756

The browser captures the image (webcam or file) and sends it to POST /api/solve;
the brain is the same QuizSolver used by the desktop app. MQTT and the model API
key can be configured live from the web Settings panel (GET/POST /api/config).
"""

import os
import sys
import traceback
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from satori import MqttPublisher, QuizResult, QuizSolver, QuizSolverError
from satori import config

load_dotenv(config.ENV_PATH)

MAX_UPLOAD_BYTES = 10 * 1024 * 1024

# Rebuilt whenever the config changes, so key/MQTT edits take effect live.
solver = QuizSolver()
mqtt: Optional[MqttPublisher] = None


def rebuild_solver() -> None:
    global solver
    solver = QuizSolver()  # lazy client picks up the current ANTHROPIC_API_KEY


def rebuild_mqtt() -> None:
    global mqtt
    if mqtt is not None:
        try:
            mqtt.disconnect()
        except Exception:  # noqa: BLE001
            pass
        mqtt = None
    mqtt = MqttPublisher.from_env()
    if mqtt is not None:
        try:
            mqtt.connect()
            print(f"MQTT: publishing to {mqtt.broker}:{mqtt.port} topic '{mqtt.topic}'")
        except Exception as exc:  # noqa: BLE001
            print(f"MQTT: could not connect ({exc}); continuing without publishing.", file=sys.stderr)
            mqtt = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("Warning: ANTHROPIC_API_KEY not set. Configure it in the web Settings panel.",
              file=sys.stderr)
    rebuild_mqtt()
    yield
    if mqtt is not None:
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
    except Exception as exc:  # noqa: BLE001 - surface model/network errors cleanly
        traceback.print_exc()
        raise HTTPException(status_code=502, detail=f"{type(exc).__name__}: {exc}")
    if mqtt is not None:
        try:
            mqtt.publish(result)
        except Exception as exc:  # noqa: BLE001 - publishing must never break the response
            print(f"MQTT: failed to publish ({exc}).", file=sys.stderr)
    return result


class ConfigUpdate(BaseModel):
    anthropic_api_key: Optional[str] = None
    mqtt_broker: Optional[str] = None
    mqtt_port: Optional[str] = None
    mqtt_topic: Optional[str] = None
    mqtt_user: Optional[str] = None
    mqtt_password: Optional[str] = None
    mqtt_qos: Optional[str] = None


@app.get("/api/config")
async def get_config() -> dict:
    return config.public_config()


@app.post("/api/config")
async def post_config(update: ConfigUpdate) -> dict:
    # Only act on fields the client actually sent.
    changed = config.update_config(update.model_dump(exclude_unset=True))
    if changed["anthropic"]:
        rebuild_solver()
    if changed["mqtt"]:
        rebuild_mqtt()
    return {"ok": True, "applied": changed, "config": config.public_config()}


app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")
