"""Web version: FastAPI serves the static frontend and exposes the solver.

Usage:
    .venv\\Scripts\\python -m uvicorn server:app --port 8756

The browser captures the image (webcam or file) and sends it to POST /api/solve;
the brain is the same QuizSolver used by the desktop app. With a network camera
(CAMERA_URL), the server reads the stream itself: the browser shows an MJPEG
preview from GET /api/camera/stream and POST /api/camera/solve captures the
newest frame at full resolution. MQTT, the model API key and the camera URL can
be configured live from the web Settings panel (GET/POST /api/config).

Remote trigger: a message on MQTT_CAPTURE_TOPIC (default `satori/capture`, e.g.
from the satori-button) captures from the network camera and solves it; open
pages get the result through GET /api/events (Server-Sent Events), and the
device is acked on its `reply_to` topic.
"""

import asyncio
import json
import os
import sys
import traceback
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import cv2
from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from satori import (DEFAULT_CAPTURE_TOPIC, CaptureCommand, MqttPublisher, QuizResult,
                    QuizSolver, QuizSolverError)
from satori import config
from satori.camera import NetworkCamera, encode_jpeg

load_dotenv(config.ENV_PATH)

MAX_UPLOAD_BYTES = 10 * 1024 * 1024

# The MJPEG preview is only for aiming the camera; the capture sent to Claude
# is taken separately at full resolution.
PREVIEW_MAX_WIDTH = 960
PREVIEW_FPS = 12
PREVIEW_JPEG_QUALITY = 75

# Rebuilt whenever the config changes, so key/MQTT/camera edits take effect live.
solver = QuizSolver()
mqtt: Optional[MqttPublisher] = None
netcam: Optional[NetworkCamera] = None  # started lazily on first use

main_loop: Optional[asyncio.AbstractEventLoop] = None  # for MQTT-thread callbacks
event_clients: "set[asyncio.Queue]" = set()             # open /api/events streams
remote_busy = False                                     # a remote capture is running


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
            capture_topic = os.environ.get("MQTT_CAPTURE_TOPIC", DEFAULT_CAPTURE_TOPIC)
            mqtt.subscribe(capture_topic, on_capture_command)
            mqtt.connect()
            print(f"MQTT: publishing to {mqtt.broker}:{mqtt.port} topic '{mqtt.topic}'; "
                  f"listening for captures on '{capture_topic}'")
        except Exception as exc:  # noqa: BLE001
            print(f"MQTT: could not connect ({exc}); continuing without publishing.", file=sys.stderr)
            mqtt = None


def on_capture_command(topic: str, payload: bytes) -> None:
    """MQTT network thread: hand the command over to the event loop."""
    cmd = CaptureCommand.parse(payload)
    if not cmd.triggers_capture:
        if mqtt is not None:
            mqtt.ack(cmd, False, f"action '{cmd.action}' is not mapped")
        return
    print(f"MQTT: capture command received on '{topic}'.")
    if main_loop is not None:
        asyncio.run_coroutine_threadsafe(remote_capture(cmd), main_loop)


def broadcast(event: dict) -> None:
    """Push an event to every open /api/events stream (event-loop thread only)."""
    for q in list(event_clients):
        q.put_nowait(event)


async def remote_capture(cmd: CaptureCommand) -> None:
    global remote_busy
    if remote_busy:
        if mqtt is not None:
            mqtt.ack(cmd, False, "busy")
        return
    remote_busy = True
    broadcast({"type": "capture"})
    try:
        result = await run_solver(await capture_netcam_jpeg(), "image/jpeg")
    except HTTPException as exc:
        print(f"Remote capture failed: {exc.detail}", file=sys.stderr)
        broadcast({"type": "error", "detail": exc.detail})
        if mqtt is not None:
            mqtt.ack(cmd, False, str(exc.detail))
    else:
        broadcast({"type": "result", "result": result.model_dump()})
        if mqtt is not None:
            mqtt.ack(cmd, True, f"{len(result.answers)} answers")
    finally:
        remote_busy = False


def stop_netcam() -> None:
    global netcam
    if netcam is not None:
        netcam.release()
        netcam = None


def get_netcam() -> NetworkCamera:
    """The running network camera, starting it on first use."""
    global netcam
    url = os.environ.get("CAMERA_URL", "")
    if not url:
        raise HTTPException(status_code=404, detail="No network camera configured (Settings).")
    if netcam is None:
        print(f"Network camera: connecting to {url}")
        netcam = NetworkCamera(url)
    return netcam


@asynccontextmanager
async def lifespan(app: FastAPI):
    global main_loop
    main_loop = asyncio.get_running_loop()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("Warning: ANTHROPIC_API_KEY not set. Configure it in the web Settings panel.",
              file=sys.stderr)
    rebuild_mqtt()
    yield
    stop_netcam()
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
    return await run_solver(data, media_type)


def _preview_jpeg(frame) -> bytes:
    h, w = frame.shape[:2]
    if w > PREVIEW_MAX_WIDTH:
        frame = cv2.resize(frame, (PREVIEW_MAX_WIDTH, round(h * PREVIEW_MAX_WIDTH / w)),
                           interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, PREVIEW_JPEG_QUALITY])
    return buf.tobytes() if ok else b""


@app.get("/api/camera/stream")
async def camera_stream() -> StreamingResponse:
    cam = get_netcam()

    async def frames():
        # multipart/x-mixed-replace: browsers render it natively in an <img>.
        # Ends if the camera is replaced (URL changed in Settings).
        while netcam is cam:
            frame = cam.latest()
            if frame is not None:
                jpg = await run_in_threadpool(_preview_jpeg, frame)
                yield (b"--frame\r\nContent-Type: image/jpeg\r\n"
                       b"Content-Length: " + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n")
            await asyncio.sleep(1 / PREVIEW_FPS)

    return StreamingResponse(frames(), media_type="multipart/x-mixed-replace; boundary=frame",
                             headers={"Cache-Control": "no-store"})


async def capture_netcam_jpeg() -> bytes:
    """Newest full-resolution frame from the network camera, as JPEG."""
    cam = get_netcam()
    frame = cam.latest()
    if frame is None and await run_in_threadpool(cam.wait_first_frame):
        frame = cam.latest()
    if frame is None:
        raise HTTPException(status_code=503,
                            detail="No image from the network camera (is it on the network?).")
    return await run_in_threadpool(encode_jpeg, frame)


@app.post("/api/camera/solve", response_model=QuizResult)
async def camera_solve() -> QuizResult:
    return await run_solver(await capture_netcam_jpeg(), "image/jpeg")


@app.get("/api/events")
async def events() -> StreamingResponse:
    """Server-Sent Events: remote (MQTT) captures and their results."""
    queue: asyncio.Queue = asyncio.Queue()
    event_clients.add(queue)

    async def stream():
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"  # comment line; keeps proxies/idle timers happy
                    continue
                yield f"data: {json.dumps(event)}\n\n"
        finally:
            event_clients.discard(queue)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store"})


async def run_solver(data: bytes, media_type: str) -> QuizResult:
    """Solve an image and publish the answers over MQTT (if configured)."""
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
    camera_url: Optional[str] = None


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
    if changed["camera"]:
        await run_in_threadpool(stop_netcam)  # restarts with the new URL on next use
    return {"ok": True, "applied": changed, "config": config.public_config()}


app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")
