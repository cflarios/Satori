"""Webcam capture with OpenCV: open, measure sharpness and encode JPEG.

Only the desktop app uses this module; the web version captures in the browser
and simply does not import this file.
"""

import sys
from typing import Optional

import cv2
import numpy as np

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
