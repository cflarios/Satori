"""MQTT publishing of the answers to an ESP32 (or any other client).

Optional: if there is no MQTT_BROKER in the environment, from_env() returns None
and the apps work exactly as before, without MQTT.

Compact (JSON) payload intended for ArduinoJson on the ESP32:

    {
      "ts": 1730000000,
      "count": 4,
      "answers": [
        {"n": 1, "ans": "b) Paris", "conf": "high"},
        ...
      ]
    }
"""

import json
import os
import time
import uuid
from dataclasses import dataclass
from typing import Optional

import paho.mqtt.client as mqtt

from .models import QuizResult

DEFAULT_CAPTURE_TOPIC = "satori/capture"


@dataclass
class CaptureCommand:
    """A remote capture request received on the capture topic.

    The payload may be anything (the basic contract: any message = one capture).
    Devices that want confirmation send JSON with `reply_to` and `seq`; Satori
    then answers on `reply_to` with {"seq": N, "ok": bool, "detail": "..."} once
    the capture has been solved (or failed). A JSON `action` other than "click"
    (e.g. a button's double/long press) does not trigger a capture.
    """

    reply_to: Optional[str] = None
    seq: Optional[int] = None
    action: Optional[str] = None

    @classmethod
    def parse(cls, payload: bytes) -> "CaptureCommand":
        try:
            data = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        seq = data.get("seq")
        return cls(
            reply_to=data.get("reply_to") or None,
            seq=seq if isinstance(seq, int) else None,
            action=data.get("action"),
        )

    @property
    def triggers_capture(self) -> bool:
        return self.action in (None, "click")


class MqttPublisher:
    def __init__(
        self,
        broker: str,
        port: int = 1883,
        topic: str = "satori/answers",
        username: Optional[str] = None,
        password: Optional[str] = None,
        qos: int = 1,
        retain: bool = True,
    ):
        self.broker = broker
        self.port = port
        self.topic = topic
        self.qos = qos
        # retain=True: the ESP32 receives the last answer as soon as it connects,
        # even if it missed the live message.
        self.retain = retain

        # Unique per instance so the desktop and web apps can talk to the same
        # broker at once without evicting each other (MQTT bars duplicate ids).
        self.client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"satori-{uuid.uuid4().hex[:8]}",
        )
        if username:
            self.client.username_pw_set(username, password)
        # Retry the connection in the background; never blocks the analysis.
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)

        # topic -> callback(topic, payload). Re-subscribed on every (re)connect.
        self._subs = {}
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

    @classmethod
    def from_env(cls) -> Optional["MqttPublisher"]:
        """Build the publisher from environment variables, or None if no broker."""
        broker = os.environ.get("MQTT_BROKER")
        if not broker:
            return None
        return cls(
            broker=broker,
            port=int(os.environ.get("MQTT_PORT", "1883")),
            topic=os.environ.get("MQTT_TOPIC", "satori/answers"),
            username=os.environ.get("MQTT_USER") or None,
            password=os.environ.get("MQTT_PASSWORD") or None,
            qos=int(os.environ.get("MQTT_QOS", "1")),
        )

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code.is_failure:
            print(f"MQTT: broker {self.broker}:{self.port} refused the connection "
                  f"({reason_code}).")
            return
        print(f"MQTT: connected to {self.broker}:{self.port}")
        # (Re)subscribe on connect so subscriptions survive reconnects.
        for topic in self._subs:
            client.subscribe(topic, qos=self.qos)

    def _on_message(self, client, userdata, msg):
        # Subscriptions may use wildcards (e.g. "satori/+/event").
        cb = next((c for t, c in self._subs.items()
                   if mqtt.topic_matches_sub(t, msg.topic)), None)
        if cb:
            try:
                cb(msg.topic, msg.payload)
            except Exception as exc:  # noqa: BLE001 - a bad handler must not kill the loop
                print(f"MQTT: command handler error ({exc}).")

    def subscribe(self, topic: str, callback) -> None:
        """Call `callback(topic, payload_bytes)` on every message to `topic`."""
        self._subs[topic] = callback
        if self.client.is_connected():
            self.client.subscribe(topic, qos=self.qos)

    def connect(self) -> None:
        # Async: if the broker (e.g. the ESP32) is off, paho keeps retrying in the
        # background and subscribes once it comes up, instead of failing for good.
        self.client.connect_async(self.broker, self.port, keepalive=60)
        self.client.loop_start()

    def disconnect(self) -> None:
        self.client.loop_stop()
        self.client.disconnect()

    @staticmethod
    def _payload(result: QuizResult) -> str:
        return json.dumps(
            {
                "ts": int(time.time()),
                "count": len(result.answers),
                "answers": [
                    {"n": a.question_number, "ans": a.chosen_option, "conf": a.confidence}
                    for a in result.answers
                ],
            },
            ensure_ascii=False,
        )

    def publish(self, result: QuizResult) -> bool:
        """Publish the answers. Returns True if the message was queued for sending."""
        info = self.client.publish(
            self.topic, self._payload(result), qos=self.qos, retain=self.retain
        )
        return info.rc == mqtt.MQTT_ERR_SUCCESS

    def ack(self, cmd: CaptureCommand, ok: bool, detail: str = "") -> None:
        """Confirm a capture command to the device that sent it (if it asked)."""
        if not cmd.reply_to:
            return
        payload = {"seq": cmd.seq, "ok": ok, "detail": detail[:80]}
        self.client.publish(cmd.reply_to, json.dumps(payload), qos=1, retain=False)
