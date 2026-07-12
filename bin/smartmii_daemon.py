#!/usr/bin/env python3
"""Smartmii LoxBerry Plugin - Fan control daemon.

Controls Smartmi Standing Fan 3 (zhimi.fan.za5) devices via Xiaomi Cloud API,
publishes status to MQTT, and accepts commands via MQTT.
"""

import argparse
import json
import logging
import logging.handlers
import os
import signal
import subprocess
import sys
import time

import paho.mqtt.client as mqtt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from xiaomi_cloud import XiaomiCloudClient

logger = logging.getLogger("smartmii")

PIDFILE = "/run/shm/smartmii.pid"

# LoxBerry syslog-style levels (0-7) → Python logging levels
LB_LOGLEVEL_MAP = {
    0: logging.CRITICAL + 10,  # off/emergency — suppress everything
    1: logging.CRITICAL,       # alert
    2: logging.CRITICAL,       # critical
    3: logging.ERROR,          # error
    4: logging.WARNING,        # warning
    5: logging.INFO,           # ok
    6: logging.INFO,           # info
    7: logging.DEBUG,          # debug
}


def get_loxberry_loglevel():
    """Read plugin loglevel from LoxBerry's plugin database via Perl."""
    try:
        result = subprocess.run(
            ["perl", "-e", "use LoxBerry::System; print LoxBerry::System::pluginloglevel();"],
            capture_output=True, text=True, timeout=5,
        )
        level = int(result.stdout.strip())
        return LB_LOGLEVEL_MAP.get(level, logging.INFO)
    except Exception:
        return None

# zhimi.fan.za5 MIoT SIID/PIID mapping — cloud-available properties only
STATUS_PROPS = [
    (2, 1, "power"),
    (2, 2, "fan_level"),
    (2, 3, "oscillate"),
    (2, 5, "angle"),
    (2, 7, "mode"),
    (5, 1, "buzzer"),
]

# command name -> (siid, piid, value_converter)
COMMAND_MAP = {
    "power":          (2, 1, None),
    "fan_level":      (2, 2, lambda v: max(1, min(4, int(v)))),
    "oscillate":      (2, 3, None),
    "angle":          (2, 5, lambda v: int(v) if int(v) in (30, 60, 90, 120) else None),
    "mode":           (2, 7, lambda v: 1 if v.lower() in ("straight", "1") else 0),
    "buzzer":         (5, 1, None),
}

STATUS_NAMES = [name for _, _, name in STATUS_PROPS]
COMMAND_NAMES = list(COMMAND_MAP.keys())


def parse_bool(value):
    return value.lower() in ("on", "1", "true", "yes")


def format_status_value(name, value):
    """Convert cloud API values to MQTT-friendly strings."""
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def get_mqtt_credentials():
    general_json = os.path.join(
        os.environ.get("LBSCONFIG", "/opt/loxberry/config/system"),
        "general.json",
    )
    try:
        with open(general_json) as f:
            cfg = json.load(f)
        m = cfg.get("Mqtt", {})
        return {
            "host": m.get("Brokerhost", "localhost"),
            "port": int(m.get("Brokerport", 1883)),
            "user": m.get("Brokeruser", ""),
            "pass": m.get("Brokerpass", ""),
        }
    except (FileNotFoundError, json.JSONDecodeError, KeyError) as e:
        logger.warning("Could not read MQTT credentials from %s: %s", general_json, e)
        return {"host": "localhost", "port": 1883, "user": "", "pass": ""}


class SmartmiDaemon:
    def __init__(self, config_path, log_dir, loglevel=None):
        self.config_path = config_path
        self.log_dir = log_dir
        self.cli_loglevel = loglevel
        self.config = {}
        self.fans = {}
        self.cloud = None
        self.mqtt_client = None
        self.running = False
        self.config_mtime = 0

    def setup_logging(self):
        os.makedirs(self.log_dir, exist_ok=True)
        log_file = os.path.join(self.log_dir, "smartmii.log")
        handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=1_000_000, backupCount=3
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
        )
        root = logging.getLogger()
        root.addHandler(handler)
        root.addHandler(logging.StreamHandler())

        lb_level = get_loxberry_loglevel()
        if lb_level is not None:
            level = lb_level
        elif self.cli_loglevel is not None:
            level = self.cli_loglevel
        else:
            level = logging.INFO
        root.setLevel(level)
        logger.info("Log level: %s", logging.getLevelName(level))

    def init_cloud(self):
        server = self.config.get("xiaomi_server", "de")
        self.cloud = XiaomiCloudClient(server=server)
        config_dir = os.path.dirname(self.config_path)
        session_file = os.path.join(config_dir, self.config.get("session_file", "xiaomi_session.json"))
        if not self.cloud.load_session(session_file):
            logger.error("No Xiaomi Cloud session found at %s. Please login via the web UI.", session_file)
            return False
        if not self.cloud.is_session_valid():
            logger.error("Xiaomi Cloud session expired. Please re-login via the web UI.")
            return False
        logger.info("Xiaomi Cloud session loaded (server: %s)", server)
        return True

    def load_and_apply_config(self):
        logger.debug("Loading config from %s", self.config_path)
        try:
            with open(self.config_path) as f:
                new_config = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError) as e:
            logger.error("Failed to load config: %s", e)
            return False

        self.config_mtime = os.path.getmtime(self.config_path)
        old_prefix = self.config.get("mqtt_prefix", "")
        self.config = new_config

        old_fan_ids = set(self.fans.keys())
        new_fan_ids = set()

        for fan_cfg in new_config.get("fans", []):
            if not all(k in fan_cfg for k in ("id", "did")):
                logger.error("Fan config missing required fields: %s", fan_cfg)
                continue
            if not fan_cfg.get("enabled", True):
                continue
            new_fan_ids.add(fan_cfg["id"])

        for fan_id in old_fan_ids - new_fan_ids:
            logger.info("Removing fan: %s", fan_id)
            self.fans.pop(fan_id, None)
            if self.mqtt_client and self.mqtt_client.is_connected():
                prefix = self.config.get("mqtt_prefix", "smartmii")
                self.mqtt_client.publish(f"{prefix}/{fan_id}/status/online", "0", retain=True)

        for fan_cfg in new_config.get("fans", []):
            if not all(k in fan_cfg for k in ("id", "did")):
                continue
            if not fan_cfg.get("enabled", True):
                continue
            fan_id = fan_cfg["id"]
            if fan_id not in self.fans:
                logger.info("Adding fan: %s (DID: %s)", fan_cfg.get("name", fan_id), fan_cfg["did"])
            self.fans[fan_id] = {
                "config": fan_cfg,
                "online": False,
                "last_status": {},
            }

        if self.mqtt_client and self.mqtt_client.is_connected():
            if old_prefix and old_prefix != self.config.get("mqtt_prefix", "smartmii"):
                self.mqtt_client.unsubscribe(f"{old_prefix}/+/cmd/#")
            prefix = self.config.get("mqtt_prefix", "smartmii")
            self.mqtt_client.subscribe(f"{prefix}/+/cmd/#")

        return True

    def connect_mqtt(self):
        creds = get_mqtt_credentials()
        self.mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="smartmii-daemon")
        if creds["user"]:
            self.mqtt_client.username_pw_set(creds["user"], creds["pass"])

        self.mqtt_client.on_connect = self._on_mqtt_connect
        self.mqtt_client.on_message = self._on_mqtt_message
        self.mqtt_client.on_disconnect = self._on_mqtt_disconnect

        will_prefix = self.config.get("mqtt_prefix", "smartmii")
        self.mqtt_client.will_set(f"{will_prefix}/daemon/status", "offline", retain=True)

        logger.info("Connecting to MQTT broker %s:%d", creds["host"], creds["port"])
        self.mqtt_client.connect(creds["host"], creds["port"], keepalive=60)
        self.mqtt_client.loop_start()

    def _on_mqtt_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            prefix = self.config.get("mqtt_prefix", "smartmii")
            logger.info("Connected to MQTT broker")
            logger.debug("Subscribing to %s/+/cmd/#", prefix)
            client.subscribe(f"{prefix}/+/cmd/#")
            client.publish(f"{prefix}/daemon/status", "online", retain=True)
        else:
            logger.error("MQTT connection failed: %s", reason_code)

    def _on_mqtt_disconnect(self, client, userdata, flags, reason_code, properties):
        if reason_code != 0:
            logger.warning("Unexpected MQTT disconnect: %s", reason_code)

    def _on_mqtt_message(self, client, userdata, msg):
        prefix = self.config.get("mqtt_prefix", "smartmii")
        topic = msg.topic
        payload = msg.payload.decode("utf-8", errors="replace").strip()
        logger.debug("MQTT message: %s = %s", topic, payload)

        if not topic.startswith(prefix + "/"):
            return

        parts = topic[len(prefix) + 1:].split("/")
        if len(parts) != 3 or parts[1] != "cmd":
            return

        fan_id = parts[0]
        command = parts[2]

        if fan_id not in self.fans:
            logger.warning("Command for unknown fan: %s", fan_id)
            return

        logger.info("Command: %s/%s = %r", fan_id, command, payload)
        self._execute_command(fan_id, command, payload)

    def _execute_command(self, fan_id, command, payload):
        fan_entry = self.fans.get(fan_id)
        if not fan_entry:
            return

        if command not in COMMAND_MAP:
            logger.warning("Unknown command: %s", command)
            return

        siid, piid, converter = COMMAND_MAP[command]
        did = fan_entry["config"]["did"]

        try:
            if converter:
                value = converter(payload)
                if value is None:
                    logger.warning("Invalid value for %s: %s", command, payload)
                    return
            elif command == "power":
                if payload.lower() == "toggle":
                    current = fan_entry.get("last_status", {}).get("power", "0")
                    value = current != "1"
                    logger.debug("Toggle power: current=%s, new=%s", current, value)
                else:
                    value = parse_bool(payload)
            else:
                value = parse_bool(payload)

            logger.debug("Sending %s/%s: did=%s siid=%d piid=%d value=%s", fan_id, command, did, siid, piid, value)
            success = self.cloud.set_property(did, siid, piid, value)
            if success:
                logger.info("Command %s/%s succeeded", fan_id, command)
                time.sleep(0.5)
                self._poll_fan(fan_id, fan_entry)
            else:
                logger.error("Command %s/%s failed via cloud API", fan_id, command)

        except Exception as e:
            logger.error("Command failed for %s/%s: %s", fan_id, command, e)

    def _poll_fan(self, fan_id, fan_entry):
        prefix = self.config.get("mqtt_prefix", "smartmii")
        base = f"{prefix}/{fan_id}/status"
        did = fan_entry["config"]["did"]

        logger.debug("Polling fan %s (DID: %s)", fan_id, did)
        prop_keys = [(s, p) for s, p, _ in STATUS_PROPS]
        values = self.cloud.get_properties(did, prop_keys)

        if values is None:
            logger.warning("Failed to poll fan %s", fan_id)
            if fan_entry["online"]:
                fan_entry["online"] = False
                self.mqtt_client.publish(f"{base}/online", "0", retain=True)
            return

        fan_entry["online"] = True
        self.mqtt_client.publish(f"{base}/online", "1", retain=True)

        for siid, piid, name in STATUS_PROPS:
            if (siid, piid) in values:
                mqtt_val = format_status_value(name, values[(siid, piid)])
                fan_entry["last_status"][name] = mqtt_val
                self.mqtt_client.publish(f"{base}/{name}", mqtt_val, retain=True)

        logger.debug("Poll %s: %s", fan_id, fan_entry["last_status"])

    def poll_all_fans(self):
        for fan_id, fan_entry in list(self.fans.items()):
            self._poll_fan(fan_id, fan_entry)

    def check_config_changed(self):
        try:
            mtime = os.path.getmtime(self.config_path)
            if mtime > self.config_mtime:
                logger.info("Config file changed, reloading...")
                self.load_and_apply_config()
        except OSError:
            pass

    def shutdown(self, signum=None, frame=None):
        logger.info("Shutting down...")
        self.running = False

        if self.mqtt_client:
            prefix = self.config.get("mqtt_prefix", "smartmii")
            for fan_id in self.fans:
                self.mqtt_client.publish(f"{prefix}/{fan_id}/status/online", "0", retain=True)
            self.mqtt_client.publish(f"{prefix}/daemon/status", "offline", retain=True)
            self.mqtt_client.disconnect()
            self.mqtt_client.loop_stop()

        try:
            os.remove(PIDFILE)
        except OSError:
            pass

        logger.info("Shutdown complete")
        sys.exit(0)

    def run(self):
        self.setup_logging()
        logger.info("Smartmii daemon starting")

        with open(PIDFILE, "w") as f:
            f.write(str(os.getpid()))

        signal.signal(signal.SIGTERM, self.shutdown)
        signal.signal(signal.SIGINT, self.shutdown)

        if not self.load_and_apply_config():
            logger.error("Failed to load initial config, exiting")
            sys.exit(1)

        if not self.init_cloud():
            logger.error("Cloud session not available, exiting")
            sys.exit(1)

        self.connect_mqtt()
        time.sleep(1)

        self.running = True
        last_poll = 0

        while self.running:
            now = time.time()
            poll_interval = self.config.get("poll_interval", 30)

            if now - last_poll >= poll_interval:
                self.poll_all_fans()
                last_poll = now

            self.check_config_changed()
            time.sleep(1)


def main():
    parser = argparse.ArgumentParser(description="Smartmii Fan Control Daemon")
    parser.add_argument("--configdir", default="/opt/loxberry/config/plugins/smartmii")
    parser.add_argument("--logdir", default="/opt/loxberry/log/plugins/smartmii")
    parser.add_argument("--loglevel", default=None, help="Override log level (DEBUG, INFO, WARNING, ERROR)")
    args = parser.parse_args()

    loglevel = getattr(logging, args.loglevel.upper(), None) if args.loglevel else None
    config_path = os.path.join(args.configdir, "smartmii.json")
    daemon = SmartmiDaemon(config_path, args.logdir, loglevel=loglevel)
    daemon.run()


if __name__ == "__main__":
    main()
