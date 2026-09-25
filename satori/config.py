"""Read/write runtime configuration in the .env file.

Backs the web Settings panel. Secrets (API key, MQTT password) are written but
never read back to the client: `public_config()` only reports whether they are
set, not their values.
"""

import os
from pathlib import Path
from typing import Dict

from dotenv import dotenv_values, set_key

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"

# Map of web field name -> .env key for the non-secret MQTT settings.
_MQTT_FIELDS = {
    "mqtt_broker": "MQTT_BROKER",
    "mqtt_port": "MQTT_PORT",
    "mqtt_topic": "MQTT_TOPIC",
    "mqtt_user": "MQTT_USER",
    "mqtt_qos": "MQTT_QOS",
}


def _file_values() -> Dict[str, str]:
    return dict(dotenv_values(ENV_PATH)) if ENV_PATH.exists() else {}


def _get(key: str, default: str = "") -> str:
    # os.environ wins (it reflects runtime updates); fall back to the file.
    vals = _file_values()
    return os.environ.get(key) or vals.get(key) or default


def _is_set(key: str) -> bool:
    vals = _file_values()
    return bool(os.environ.get(key) or vals.get(key))


def public_config() -> dict:
    """Non-secret config plus booleans telling whether the secrets are set."""
    return {
        "anthropic_api_key_set": _is_set("ANTHROPIC_API_KEY"),
        "mqtt_broker": _get("MQTT_BROKER"),
        "mqtt_port": _get("MQTT_PORT", "1883"),
        "mqtt_topic": _get("MQTT_TOPIC", "satori/answers"),
        "mqtt_user": _get("MQTT_USER"),
        "mqtt_qos": _get("MQTT_QOS", "0"),
        "mqtt_password_set": _is_set("MQTT_PASSWORD"),
        "camera_url": _get("CAMERA_URL"),
    }


def _write(key: str, value: str) -> None:
    ENV_PATH.touch(exist_ok=True)
    set_key(str(ENV_PATH), key, value)
    os.environ[key] = value


def _normalize_broker(value: str) -> str:
    # Accept a pasted URL and reduce it to a bare host paho can use.
    value = value.replace("mqtt://", "").replace("tcp://", "")
    value = value.split("/")[0].split(":")[0]
    return value.strip()


def update_config(updates: dict) -> Dict[str, bool]:
    """Apply only the fields present in `updates`.

    Secret fields (`anthropic_api_key`, `mqtt_password`) are written only when a
    non-empty value is given; a blank keeps the existing secret. Non-secret MQTT
    fields are written as-is (a blank broker clears/disables MQTT).

    Returns which subsystems changed so the caller can rebuild them.
    """
    changed = {"anthropic": False, "mqtt": False, "camera": False}

    # The form always sends the URL; only flag a change so the stream isn't
    # restarted on every unrelated save.
    if "camera_url" in updates:
        url = (updates["camera_url"] or "").strip()
        if url != _get("CAMERA_URL"):
            _write("CAMERA_URL", url)
            changed["camera"] = True

    if updates.get("anthropic_api_key", "").strip():
        _write("ANTHROPIC_API_KEY", updates["anthropic_api_key"].strip())
        changed["anthropic"] = True

    for field, env_key in _MQTT_FIELDS.items():
        if field in updates:
            value = (updates[field] or "").strip()
            if env_key == "MQTT_BROKER" and value:
                value = _normalize_broker(value)
            _write(env_key, value)
            changed["mqtt"] = True

    if updates.get("mqtt_password", "").strip():
        _write("MQTT_PASSWORD", updates["mqtt_password"])
        changed["mqtt"] = True

    return changed
