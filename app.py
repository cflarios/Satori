"""Desktop app: webcam preview, capture with SPACE and quiz solving with Claude.

Usage:
    python app.py                  # camera 0
    python app.py --camera 1       # another camera
    python app.py --file photo.jpg # test without a camera, using an existing image
"""

import argparse
import mimetypes
import os
import sys
import threading
from datetime import datetime
from pathlib import Path

import cv2
from dotenv import load_dotenv

from satori import MqttPublisher, QuizSolver, QuizSolverError, QuizResult
from satori import config
from satori.camera import Camera, SHARPNESS_THRESHOLD, encode_jpeg, sharpness

CAPTURES_DIR = Path(__file__).parent / "captures"


def print_result(result: QuizResult) -> None:
    print("\n" + "=" * 70)
    print(f"ANSWERS ({len(result.answers)} questions)")
    print("=" * 70)
    for a in result.answers:
        print(f"\n{a.question_number}. {a.question_text}")
        print(f"   Answer:  {a.chosen_option}   [confidence: {a.confidence}]")
        print(f"   Why:     {a.reasoning}")
    if result.warnings:
        print("\nWarnings:")
        for w in result.warnings:
            print(f"  - {w}")
    print("=" * 70 + "\n")


def save_capture(image_bytes: bytes, result: QuizResult) -> Path:
    CAPTURES_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    (CAPTURES_DIR / f"{stamp}.jpg").write_bytes(image_bytes)
    out = CAPTURES_DIR / f"{stamp}.json"
    out.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    return out


def solve_file(solver: QuizSolver, path: Path) -> int:
    media_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    print(f"Analyzing {path} ...")
    try:
        result = solver.solve(path.read_bytes(), media_type=media_type)
    except QuizSolverError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print_result(result)
    return 0


class DesktopApp:
    def __init__(self, camera_index: int):
        self.solver = QuizSolver()
        self.camera = Camera(camera_index)
        self.busy = False
        self.status = "SPACE capture | S settings | Q quit"
        self.mqtt = None
        self._setup_mqtt()

    def _setup_mqtt(self) -> None:
        """(Re)build the MQTT publisher from the current environment."""
        if self.mqtt is not None:
            try:
                self.mqtt.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self.mqtt = None
        self.mqtt = MqttPublisher.from_env()
        if self.mqtt:
            try:
                self.mqtt.connect()
                print(f"MQTT: publishing to {self.mqtt.broker}:{self.mqtt.port} "
                      f"topic '{self.mqtt.topic}'")
            except Exception as exc:
                print(f"MQTT: could not connect ({exc}); continuing without publishing.",
                      file=sys.stderr)
                self.mqtt = None

    def open_settings(self) -> None:
        """Open the modal settings dialog and apply any changes live."""
        updates = settings_dialog(config.public_config())
        if not updates:
            self.status = "Settings unchanged"
            return
        changed = config.update_config(updates)
        if changed["anthropic"]:
            self.solver = QuizSolver()
        if changed["mqtt"]:
            self._setup_mqtt()
        parts = [name for name, ok in (("API key", changed["anthropic"]),
                                       ("MQTT", changed["mqtt"])) if ok]
        self.status = "Saved: " + ", ".join(parts) if parts else "Settings saved"

    def _analyze(self, image_bytes: bytes) -> None:
        try:
            result = self.solver.solve(image_bytes)
        except QuizSolverError as exc:
            print(f"\nError: {exc}", file=sys.stderr)
            self.status = "Error - see console"
        except Exception as exc:  # network / API errors
            print(f"\nUnexpected error: {exc}", file=sys.stderr)
            self.status = "Error - see console"
        else:
            print_result(result)
            saved = save_capture(image_bytes, result)
            print(f"Capture and answers saved to {saved.parent}")
            if self.mqtt:
                try:
                    self.mqtt.publish(result)
                    print(f"MQTT: answers sent to '{self.mqtt.topic}'")
                except Exception as exc:
                    print(f"MQTT: failed to publish ({exc}).", file=sys.stderr)
            self.status = "Done - answers in console"
        finally:
            self.busy = False

    def run(self) -> int:
        window = "Satori - quizzes"
        cv2.namedWindow(window, cv2.WINDOW_NORMAL)
        try:
            while True:
                frame = self.camera.read()
                if frame is None:
                    print("The camera stopped delivering frames.", file=sys.stderr)
                    return 1

                sharp = sharpness(frame)
                display = frame.copy()
                ok_focus = sharp >= SHARPNESS_THRESHOLD
                color = (0, 200, 0) if ok_focus else (0, 0, 255)
                cv2.putText(display, f"Sharpness: {sharp:.0f} {'OK' if ok_focus else 'LOW'}",
                            (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2)
                label = "Analyzing..." if self.busy else self.status
                cv2.putText(display, label, (10, 65),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
                cv2.imshow(window, display)

                key = cv2.waitKey(1) & 0xFF
                if key in (ord("q"), ord("Q"), 27):
                    return 0
                if key in (ord("s"), ord("S")) and not self.busy:
                    self.open_settings()
                if key == ord(" ") and not self.busy:
                    if not ok_focus:
                        print("Notice: the image looks blurry; capturing anyway.")
                    self.busy = True
                    image_bytes = encode_jpeg(frame)
                    threading.Thread(target=self._analyze, args=(image_bytes,), daemon=True).start()
        finally:
            self.camera.release()
            cv2.destroyAllWindows()
            if self.mqtt:
                self.mqtt.disconnect()


def settings_dialog(current: dict):
    """Modal Tkinter settings form. Returns an updates dict to apply, or None.

    Secret fields (API key, MQTT password) map into the updates only when the
    user types something; a blank keeps the existing secret.
    """
    try:
        import tkinter as tk
        from tkinter import ttk
    except Exception as exc:  # noqa: BLE001 - headless / no Tk
        print(f"Settings UI unavailable (tkinter): {exc}", file=sys.stderr)
        return None

    state = {"save": False}
    root = tk.Tk()
    root.title("Satori - Settings")
    root.resizable(False, False)
    frm = ttk.Frame(root, padding=16)
    frm.grid()
    grey = "#888888"
    r = 0

    def header(text):
        nonlocal r
        ttk.Label(frm, text=text, font=("", 10, "bold")).grid(
            column=0, row=r, columnspan=2, sticky="w", pady=(8, 2)); r += 1

    def field(label, value="", show=None):
        nonlocal r
        ttk.Label(frm, text=label).grid(column=0, row=r, sticky="w", padx=(0, 10), pady=3)
        e = ttk.Entry(frm, width=36, show=show)
        e.insert(0, value)
        e.grid(column=1, row=r, pady=3); r += 1
        return e

    def note(text):
        nonlocal r
        ttk.Label(frm, text=text, foreground=grey).grid(column=1, row=r, sticky="w"); r += 1

    header("Model")
    apikey = field("Anthropic API key", show="•")
    note("already set - leave blank to keep it" if current.get("anthropic_api_key_set")
         else "no key set yet")

    ttk.Separator(frm, orient="horizontal").grid(
        column=0, row=r, columnspan=2, sticky="ew", pady=10); r += 1
    header("MQTT (optional)")
    broker = field("Broker host", current.get("mqtt_broker", ""))
    port = field("Port", current.get("mqtt_port", "1883"))
    topic = field("Topic", current.get("mqtt_topic", "satori/answers"))
    user = field("Username", current.get("mqtt_user", ""))
    passwd = field("Password", show="•")
    note("password set - blank keeps it" if current.get("mqtt_password_set")
         else "blank = anonymous broker")
    ttk.Label(frm, text="QoS").grid(column=0, row=r, sticky="w", padx=(0, 10), pady=3)
    qos = ttk.Combobox(frm, values=["0", "1"], width=5, state="readonly")
    qos.set(current.get("mqtt_qos", "0"))
    qos.grid(column=1, row=r, sticky="w", pady=3); r += 1

    def on_save():
        state["save"] = True
        state["v"] = {"apikey": apikey.get(), "broker": broker.get(), "port": port.get(),
                      "topic": topic.get(), "user": user.get(), "password": passwd.get(),
                      "qos": qos.get()}
        root.destroy()

    btns = ttk.Frame(frm)
    btns.grid(column=0, row=r, columnspan=2, sticky="e", pady=(14, 0))
    ttk.Button(btns, text="Cancel", command=root.destroy).grid(column=0, row=0, padx=6)
    ttk.Button(btns, text="Save", command=on_save).grid(column=1, row=0)

    apikey.focus_set()
    root.mainloop()

    if not state["save"]:
        return None
    v = state["v"]
    updates = {
        "mqtt_broker": v["broker"], "mqtt_port": v["port"], "mqtt_topic": v["topic"],
        "mqtt_user": v["user"], "mqtt_qos": v["qos"],
    }
    if v["apikey"].strip():
        updates["anthropic_api_key"] = v["apikey"].strip()
    if v["password"].strip():
        updates["mqtt_password"] = v["password"]
    return updates


def main() -> int:
    # Line-buffer stdout even when it is redirected (e.g. app launched by another
    # process); without this the answers stay held in the buffer.
    sys.stdout.reconfigure(line_buffering=True)

    # Load ANTHROPIC_API_KEY from the .env next to this file (if present).
    # An already-defined environment variable takes precedence over the .env.
    load_dotenv(config.ENV_PATH)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "Warning: ANTHROPIC_API_KEY not set. Press S in the app to set it, "
            "or copy .env.example to .env and paste your API key there.",
            file=sys.stderr,
        )

    parser = argparse.ArgumentParser(description="Solve quizzes seen through the webcam with Claude.")
    parser.add_argument("--camera", type=int, default=0, help="Camera index (default: 0)")
    parser.add_argument("--file", type=Path, help="Analyze an existing image instead of using the camera")
    args = parser.parse_args()

    if args.file:
        return solve_file(QuizSolver(), args.file)
    return DesktopApp(args.camera).run()


if __name__ == "__main__":
    sys.exit(main())
