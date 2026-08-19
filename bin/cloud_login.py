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
import logging
import logging.handlers
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from xiaomi_cloud import XiaomiCloudClient

logger = logging.getLogger("smartmii")

STATE_FILE = os.path.join(tempfile.gettempdir(), "smartmii_login_state.json")

LB_LOGLEVEL_MAP = {
    0: logging.CRITICAL + 10, 1: logging.CRITICAL, 2: logging.CRITICAL,
    3: logging.ERROR, 4: logging.WARNING, 5: logging.INFO, 6: logging.INFO,
    7: logging.DEBUG,
}


def setup_logging():
    logdir = os.environ.get("LBPLOGDIR", "")
    if not logdir:
        return
    os.makedirs(logdir, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        os.path.join(logdir, "smartmii.log"), maxBytes=1_000_000, backupCount=3,
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
    logging.getLogger().addHandler(handler)

    try:
        result = subprocess.run(
            ["perl", "-e", "use LoxBerry::System; print LoxBerry::System::pluginloglevel();"],
            capture_output=True, text=True, timeout=5,
        )
        level = LB_LOGLEVEL_MAP.get(int(result.stdout.strip()), logging.INFO)
    except Exception:
        level = logging.INFO
    logging.getLogger().setLevel(level)
    # Propagate to xiaomi_cloud logger
    logging.getLogger("xiaomi_cloud").setLevel(level)


def main():
    setup_logging()

    line = sys.stdin.readline()
    if not line:
        return

    try:
        msg = json.loads(line.strip())
    except json.JSONDecodeError:
        respond({"status": "error", "message": "Invalid JSON"})
        return

    action = msg.get("action")
    logger.debug("cloud_login action=%s", action)

    if action == "login":
        logger.info("Login request for user %s (server: %s)", msg.get("username"), msg.get("server", "de"))
        client = XiaomiCloudClient(server=msg.get("server", "de"))
        result = client.login(msg["username"], msg["password"])
        if result["status"] == "ok":
            client.save_session(msg["session_file"])
            _cleanup_state()
            logger.info("Login successful")
        else:
            client.save_login_state(STATE_FILE)
            logger.info("Login result: %s", result["status"])
        respond(result)

    elif action == "captcha":
        logger.info("Captcha submission")
        client = XiaomiCloudClient()
        if not client.load_login_state(STATE_FILE):
            logger.error("Captcha: no login state found")
            respond({"status": "error", "message": "No login in progress"})
            return
        result = client.submit_captcha(msg["code"])
        if result["status"] == "ok":
            client.save_session(msg.get("session_file", ""))
            _cleanup_state()
            logger.info("Captcha accepted, login complete")
        else:
            client.save_login_state(STATE_FILE)
            logger.info("Captcha result: %s", result.get("message", result["status"]))
        respond(result)

    elif action == "2fa":
        logger.info("2FA code submission")
        client = XiaomiCloudClient()
        if not client.load_login_state(STATE_FILE):
            logger.error("2FA: no login state found")
            respond({"status": "error", "message": "No login in progress"})
            return
        result = client.submit_2fa(msg["code"])
        if result["status"] == "ok":
            client.save_session(msg.get("session_file", ""))
            _cleanup_state()
            logger.info("2FA accepted, login complete")
        else:
            client.save_login_state(STATE_FILE)
            logger.error("2FA failed: %s", result.get("message", "unknown"))
        respond(result)

    elif action == "discover":
        logger.info("Device discovery (server: %s)", msg.get("server", "de"))
        client = XiaomiCloudClient(server=msg.get("server", "de"))
        if not client.load_session(msg["session_file"]):
            logger.error("Discovery: no session found")
            respond({"status": "error", "message": "No session found. Please login first."})
            return
        devices = client.get_devices()
        if devices is None:
            logger.error("Discovery failed")
            respond({"status": "error", "message": "Failed to get devices. Session may be expired."})
        else:
            logger.info("Discovery: %d devices found", len(devices))
            respond({"status": "ok", "devices": devices})

    elif action == "test":
        logger.info("Connection test for DID %s", msg.get("did"))
        client = XiaomiCloudClient(server=msg.get("server", "de"))
        if not client.load_session(msg["session_file"]):
            logger.error("Test: no session found")
            respond({"status": "error", "message": "No session found."})
            return
        props = client.get_properties(msg["did"], [(2, 1)])
        if props is None:
            logger.error("Test: failed to query DID %s", msg.get("did"))
            respond({"status": "error", "message": "Failed to query device. Session may be expired."})
        else:
            power = props.get((2, 1), "unknown")
            logger.info("Test: DID %s power=%s", msg.get("did"), power)
            respond({"status": "ok", "message": f"Connected. Power: {'on' if power else 'off'}"})

    elif action == "validate":
        logger.info("Session validation")
        client = XiaomiCloudClient(server=msg.get("server", "de"))
        if not client.load_session(msg["session_file"]):
            logger.info("Validation: no session file")
            respond({"status": "error", "valid": False})
            return
        valid = client.is_session_valid()
        logger.info("Session valid: %s", valid)
        respond({"status": "ok", "valid": valid})

    else:
        logger.error("Unknown action: %s", action)
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
