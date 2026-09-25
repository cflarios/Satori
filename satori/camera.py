"""Camera capture with OpenCV: open, measure sharpness and encode JPEG.

`Camera` wraps a local webcam (desktop app). `NetworkCamera` reads an RTSP/HTTP
stream (e.g. a Wi-Fi IP camera) and is used by both the desktop app and the web
server; with a local webcam the web version captures in the browser instead.
"""

import os
import sys
import threading
import time
from typing import Optional, Union

# RTSP over TCP: UDP drops packets on Wi-Fi and shows up as smeared frames.
# Must be set before the first VideoCapture opens a stream.
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

import cv2  # noqa: E402
import numpy as np  # noqa: E402

# Claude downscales images to ~1568 px on the long edge; sending more does not help.
MAX_LONG_EDGE = 1568

# Laplacian variance below which the image is considered blurry.
# Empirical value for printed text at ~1080p; adjust if your camera differs.
SHARPNESS_THRESHOLD = 60.0

# Auto-capture tuning (mean per-pixel frame difference, 0..255 on a small gray).
# Below STABILITY = the scene is holding still; above MOTION = it clearly moved
# (used to re-arm for the next document). Adjust if your lighting is noisy.
STABILITY_THRESHOLD = 2.5
MOTION_THRESHOLD = 6.0


class Camera:
    def __init__(self, index: int = 0, width: int = 1920, height: int = 1080):
        backend = cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_ANY
        self.cap = cv2.VideoCapture(index, backend)
        if not self.cap.isOpened():
            raise RuntimeError(
                f"Could not open camera {index}. Try --camera 1 (or another index)."
            )
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)

    def read(self) -> Optional[np.ndarray]:
        ok, frame = self.cap.read()
        return frame if ok else None

    def release(self) -> None:
        self.cap.release()


class NetworkCamera:
    """Network stream (RTSP/HTTP) read on a background thread.

    OpenCV buffers network streams, so a reader that falls behind shows frames
    seconds old. The thread drains the stream continuously and keeps only the
    latest frame. Reconnects on its own if the camera drops off the Wi-Fi.

    - `read()` waits for the next new frame, like a webcam (desktop loop).
    - `latest()` returns the newest frame immediately (web snapshot/preview).
    """

    RECONNECT_DELAY = 2.0

    def __init__(self, url: str):
        self.url = url
        self._frame: Optional[np.ndarray] = None
        self._frame_time = 0.0
        self._seq = 0        # increments on every new frame
        self._read_seq = 0   # last seq handed out by read()
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="netcam", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)
            if not cap.isOpened():
                print(f"Network camera: could not open {self.url}; retrying...", file=sys.stderr)
                cap.release()
                self._stop.wait(self.RECONNECT_DELAY)
                continue
            while not self._stop.is_set():
                ok, frame = cap.read()
                if not ok:
                    print("Network camera: stream lost; reconnecting...", file=sys.stderr)
                    break
                with self._cond:
                    self._frame = frame
                    self._frame_time = time.time()
                    self._seq += 1
                    self._cond.notify_all()
            cap.release()
            self._stop.wait(self.RECONNECT_DELAY)

    def latest(self, max_age: float = 2.0) -> Optional[np.ndarray]:
        """Newest frame, or None if there is none newer than `max_age` seconds."""
        with self._cond:
            if self._frame is None or time.time() - self._frame_time > max_age:
                return None
            return self._frame.copy()

    # Long enough to ride out a reconnect (2 s delay + ~5 s to reopen RTSP).
    def read(self, timeout: float = 15.0) -> Optional[np.ndarray]:
        """Wait for a frame newer than the last one read; None on timeout."""
        with self._cond:
            if not self._cond.wait_for(lambda: self._seq > self._read_seq, timeout):
                return None
            self._read_seq = self._seq
            return self._frame.copy()

    def wait_first_frame(self, timeout: float = 15.0) -> bool:
        """Block until a frame arrives (opening an RTSP stream takes a few seconds)."""
        with self._cond:
            return self._cond.wait_for(lambda: self._seq > 0, timeout)

    def release(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)


def open_camera(source: Union[int, str]) -> Union[Camera, NetworkCamera]:
    """Webcam index (int or digit string) -> Camera; URL -> NetworkCamera."""
    if isinstance(source, int) or str(source).isdigit():
        return Camera(int(source))
    cam = NetworkCamera(str(source))
    if not cam.wait_first_frame():
        cam.release()
        raise RuntimeError(f"No image from network camera {source} (is it on the network?).")
    return cam


def sharpness(frame: np.ndarray) -> float:
    """Laplacian variance: higher = sharper."""
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    # Downscaling before measuring keeps the metric stable and cheap per frame.
    small = cv2.resize(gray, (0, 0), fx=0.5, fy=0.5)
    return float(cv2.Laplacian(small, cv2.CV_64F).var())


def small_gray(frame: np.ndarray, width: int = 160) -> np.ndarray:
    """Tiny grayscale version of the frame, for cheap frame-to-frame comparison."""
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape[:2]
    return cv2.resize(gray, (width, max(1, round(h * width / w))), interpolation=cv2.INTER_AREA)


def frame_change(prev_small: np.ndarray, cur_small: np.ndarray) -> float:
    """Mean absolute difference between two small_gray frames (0..255)."""
    return float(cv2.absdiff(prev_small, cur_small).mean())


def encode_jpeg(frame: np.ndarray, quality: int = 92) -> bytes:
    """Resize to the largest size Claude uses and encode as JPEG."""
    h, w = frame.shape[:2]
    long_edge = max(h, w)
    if long_edge > MAX_LONG_EDGE:
        scale = MAX_LONG_EDGE / long_edge
        frame = cv2.resize(frame, (round(w * scale), round(h * scale)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise RuntimeError("Could not encode the frame to JPEG.")
    return buf.tobytes()
