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
from typing import Optional

import paho.mqtt.client as mqtt

from .models import QuizResult


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
        # (Re)subscribe on connect so subscriptions survive reconnects.
        for topic in self._subs:
            client.subscribe(topic, qos=self.qos)

    def _on_message(self, client, userdata, msg):
        cb = self._subs.get(msg.topic)
        if cb is None and self._subs:
            cb = next(iter(self._subs.values()))  # single-topic fallback
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
        self.client.connect(self.broker, self.port, keepalive=60)
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
