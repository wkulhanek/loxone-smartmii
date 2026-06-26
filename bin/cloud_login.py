#!/usr/bin/env python3
"""Interactive Xiaomi Cloud login via JSON stdin/stdout.

Each action is a separate invocation (separate PHP AJAX request = separate process).
Login state is persisted to a state file between steps so the HTTP session cookies
survive across the captcha and 2FA steps.

Actions:
  login    — start login, may return captcha/2fa/ok
  captcha  — submit captcha code (restores state from prior login step)
  2fa      — submit 2FA email code (restores state from prior login/captcha step)
  discover — list devices using saved session
  test     — test connection to a device
  validate — check if session is still valid
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from xiaomi_cloud import XiaomiCloudClient

STATE_FILE = os.path.join(tempfile.gettempdir(), "smartmii_login_state.pkl")


def main():
    line = sys.stdin.readline()
    if not line:
        return

    try:
        msg = json.loads(line.strip())
    except json.JSONDecodeError:
        respond({"status": "error", "message": "Invalid JSON"})
        return

    action = msg.get("action")

    if action == "login":
        client = XiaomiCloudClient(server=msg.get("server", "de"))
        result = client.login(msg["username"], msg["password"])
        if result["status"] == "ok":
            client.save_session(msg["session_file"])
            _cleanup_state()
        else:
            client.save_login_state(STATE_FILE)
        respond(result)

    elif action == "captcha":
        client = XiaomiCloudClient()
        if not client.load_login_state(STATE_FILE):
            respond({"status": "error", "message": "No login in progress"})
            return
        result = client.submit_captcha(msg["code"])
        if result["status"] == "ok":
            client.save_session(msg.get("session_file", ""))
            _cleanup_state()
        else:
            client.save_login_state(STATE_FILE)
        respond(result)

    elif action == "2fa":
        client = XiaomiCloudClient()
        if not client.load_login_state(STATE_FILE):
            respond({"status": "error", "message": "No login in progress"})
            return
        result = client.submit_2fa(msg["code"])
        if result["status"] == "ok":
            client.save_session(msg.get("session_file", ""))
            _cleanup_state()
        else:
            client.save_login_state(STATE_FILE)
        respond(result)

    elif action == "discover":
        client = XiaomiCloudClient(server=msg.get("server", "de"))
        if not client.load_session(msg["session_file"]):
            respond({"status": "error", "message": "No session found. Please login first."})
            return
        devices = client.get_devices()
        if devices is None:
            respond({"status": "error", "message": "Failed to get devices. Session may be expired."})
        else:
            respond({"status": "ok", "devices": devices})

    elif action == "test":
        client = XiaomiCloudClient(server=msg.get("server", "de"))
        if not client.load_session(msg["session_file"]):
            respond({"status": "error", "message": "No session found."})
            return
        props = client.get_properties(msg["did"], [(2, 1)])
        if props is None:
            respond({"status": "error", "message": "Failed to query device. Session may be expired."})
        else:
            power = props.get((2, 1), "unknown")
            respond({"status": "ok", "message": f"Connected. Power: {'on' if power else 'off'}"})

    elif action == "validate":
        client = XiaomiCloudClient(server=msg.get("server", "de"))
        if not client.load_session(msg["session_file"]):
            respond({"status": "error", "valid": False})
            return
        valid = client.is_session_valid()
        respond({"status": "ok", "valid": valid})

    else:
        respond({"status": "error", "message": f"Unknown action: {action}"})


def _cleanup_state():
    try:
        os.remove(STATE_FILE)
    except OSError:
        pass


def respond(data):
    sys.stdout.write(json.dumps(data) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
