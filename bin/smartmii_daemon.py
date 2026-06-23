#!/usr/bin/env python3
"""Smartmii LoxBerry Plugin - Fan control daemon.

Polls Smartmi Standing Fan 3 (zhimi.fan.za5) devices via python-miio,
publishes status to MQTT, and accepts commands via MQTT.
"""

import argparse
import json
import logging
import logging.handlers
import os
import signal
import sys
import threading
import time

import paho.mqtt.client as mqtt
from miio.integrations.fan.zhimi.zhimi_miot import FanZA5, OperationModeFanZA5

logger = logging.getLogger("smartmii")

PIDFILE = "/run/shm/smartmii.pid"

STATUS_PROPERTIES = [
    "power", "speed", "fan_level", "mode", "oscillate", "angle",
    "buzzer", "child_lock", "led_brightness", "temperature",
    "humidity", "delay_off", "ionizer", "speed_rpm",
]

COMMAND_HANDLERS = [
    "power", "speed", "fan_level", "mode", "oscillate", "angle",
    "buzzer", "child_lock", "led_brightness", "ionizer", "delay_off",
]


def parse_bool(value):
    return value.lower() in ("on", "1", "true", "yes")


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


def load_config(config_path):
    with open(config_path) as f:
        return json.load(f)


def extract_status(status):
    return {
        "power": "1" if status.is_on else "0",
        "speed": str(status.fan_speed),
        "fan_level": str(status.fan_level),
        "mode": "natural" if status.mode == OperationModeFanZA5.Nature else "normal",
        "oscillate": "1" if status.oscillate else "0",
        "angle": str(status.angle),
        "buzzer": "1" if status.buzzer else "0",
        "child_lock": "1" if status.child_lock else "0",
        "led_brightness": str(status.led_brightness),
        "temperature": str(status.temperature),
        "humidity": str(status.humidity),
        "delay_off": str(status.delay_off_countdown),
        "ionizer": "1" if status.ionizer else "0",
        "speed_rpm": str(status.speed_rpm),
    }


class SmartmiDaemon:
    def __init__(self, config_path, log_dir):
        self.config_path = config_path
        self.log_dir = log_dir
        self.config = {}
        self.fans = {}
        self.fan_locks = {}
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
            logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        )
        logger.addHandler(handler)
        logger.addHandler(logging.StreamHandler())
        logger.setLevel(logging.INFO)

    def load_and_apply_config(self):
        try:
            new_config = load_config(self.config_path)
        except (FileNotFoundError, json.JSONDecodeError) as e:
            logger.error("Failed to load config: %s", e)
            return False

        self.config_mtime = os.path.getmtime(self.config_path)
        old_prefix = self.config.get("mqtt_prefix", "")
        self.config = new_config

        old_fan_ids = set(self.fans.keys())
        new_fan_ids = {
            f["id"] for f in new_config.get("fans", []) if f.get("enabled", True)
        }

        for fan_id in old_fan_ids - new_fan_ids:
            logger.info("Removing fan: %s", fan_id)
            self.fans.pop(fan_id, None)
            self.fan_locks.pop(fan_id, None)
            if self.mqtt_client and self.mqtt_client.is_connected():
                prefix = self.config.get("mqtt_prefix", "smartmii")
                self.mqtt_client.publish(
                    f"{prefix}/{fan_id}/status/online", "0", retain=True
                )

        for fan_cfg in new_config.get("fans", []):
            if not all(k in fan_cfg for k in ("id", "ip", "token")):
                logger.error("Fan config missing required fields: %s", fan_cfg)
                continue
            if not fan_cfg.get("enabled", True):
                continue
            fan_id = fan_cfg["id"]
            if fan_id not in self.fans:
                logger.info("Adding fan: %s (%s)", fan_cfg["name"], fan_cfg["ip"])
                try:
                    self.fans[fan_id] = {
                        "device": FanZA5(fan_cfg["ip"], fan_cfg["token"]),
                        "config": fan_cfg,
                        "online": False,
                    }
                    self.fan_locks[fan_id] = threading.Lock()
                except Exception as e:
                    logger.error("Failed to create fan %s: %s", fan_id, e)
            else:
                existing = self.fans[fan_id]["config"]
                if existing["ip"] != fan_cfg["ip"] or existing["token"] != fan_cfg["token"]:
                    logger.info("Updating fan connection: %s", fan_id)
                    try:
                        self.fans[fan_id]["device"] = FanZA5(fan_cfg["ip"], fan_cfg["token"])
                        self.fans[fan_id]["config"] = fan_cfg
                    except Exception as e:
                        logger.error("Failed to update fan %s: %s", fan_id, e)

        if self.mqtt_client and self.mqtt_client.is_connected():
            if old_prefix and old_prefix != self.config.get("mqtt_prefix", "smartmii"):
                self.mqtt_client.unsubscribe(f"{old_prefix}/+/cmd/#")
            prefix = self.config.get("mqtt_prefix", "smartmii")
            self.mqtt_client.subscribe(f"{prefix}/+/cmd/#")

        return True

    def connect_mqtt(self):
        creds = get_mqtt_credentials()
        self.mqtt_client = mqtt.Client(client_id="smartmii-daemon")
        if creds["user"]:
            self.mqtt_client.username_pw_set(creds["user"], creds["pass"])

        self.mqtt_client.on_connect = self._on_mqtt_connect
        self.mqtt_client.on_message = self._on_mqtt_message
        self.mqtt_client.on_disconnect = self._on_mqtt_disconnect

        will_prefix = self.config.get("mqtt_prefix", "smartmii")
        self.mqtt_client.will_set(
            f"{will_prefix}/daemon/status", "offline", retain=True
        )

        logger.info("Connecting to MQTT broker %s:%d", creds["host"], creds["port"])
        self.mqtt_client.connect(creds["host"], creds["port"], keepalive=60)
        self.mqtt_client.loop_start()

    def _on_mqtt_connect(self, client, userdata, flags, rc):
        if rc == 0:
            logger.info("Connected to MQTT broker")
            prefix = self.config.get("mqtt_prefix", "smartmii")
            client.subscribe(f"{prefix}/+/cmd/#")
            client.publish(f"{prefix}/daemon/status", "online", retain=True)
        else:
            logger.error("MQTT connection failed with code %d", rc)

    def _on_mqtt_disconnect(self, client, userdata, rc):
        if rc != 0:
            logger.warning("Unexpected MQTT disconnect (rc=%d), will auto-reconnect", rc)

    def _on_mqtt_message(self, client, userdata, msg):
        prefix = self.config.get("mqtt_prefix", "smartmii")
        topic = msg.topic
        payload = msg.payload.decode("utf-8", errors="replace").strip()

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

        if not self.fans[fan_id]["online"]:
            logger.warning("Command for offline fan: %s", fan_id)
            return

        logger.info("Command: %s/%s = %s", fan_id, command, payload)
        threading.Thread(
            target=self._execute_command,
            args=(fan_id, command, payload),
            daemon=True,
        ).start()

    def _execute_command(self, fan_id, command, payload):
        fan_entry = self.fans.get(fan_id)
        if not fan_entry:
            return

        lock = self.fan_locks.get(fan_id)
        if not lock:
            return

        with lock:
            device = fan_entry["device"]
            try:
                self._dispatch_command(device, fan_entry, command, payload)
                time.sleep(0.5)
                self._poll_fan(fan_id, fan_entry)
            except Exception as e:
                logger.error("Command failed for %s/%s: %s", fan_id, command, e)

    def _dispatch_command(self, device, fan_entry, command, payload):
        if command == "power":
            if payload.lower() == "toggle":
                if fan_entry.get("last_status", {}).get("power") == "1":
                    device.off()
                else:
                    device.on()
            elif parse_bool(payload):
                device.on()
            else:
                device.off()

        elif command == "speed":
            device.set_speed(max(1, min(100, int(payload))))

        elif command == "fan_level":
            device.set_fan_level(max(1, min(4, int(payload))))

        elif command == "mode":
            if payload.lower() == "natural":
                device.set_mode(OperationModeFanZA5.Nature)
            else:
                device.set_mode(OperationModeFanZA5.Normal)

        elif command == "oscillate":
            device.set_oscillate(parse_bool(payload))

        elif command == "angle":
            angle = int(payload)
            if angle in (30, 60, 90, 120):
                device.set_angle(angle)
            else:
                logger.warning("Invalid angle: %s (must be 30, 60, 90, or 120)", angle)

        elif command == "buzzer":
            device.set_buzzer(parse_bool(payload))

        elif command == "child_lock":
            device.set_child_lock(parse_bool(payload))

        elif command == "led_brightness":
            device.set_led_brightness(max(0, min(100, int(payload))))

        elif command == "ionizer":
            device.set_ionizer(parse_bool(payload))

        elif command == "delay_off":
            device.delay_off(max(0, int(payload)))

        else:
            logger.warning("Unknown command: %s", command)

    def _poll_fan(self, fan_id, fan_entry):
        prefix = self.config.get("mqtt_prefix", "smartmii")
        base = f"{prefix}/{fan_id}/status"
        device = fan_entry["device"]

        try:
            status = device.status()
            values = extract_status(status)
            values["online"] = "1"
            fan_entry["online"] = True
            fan_entry["last_status"] = values

            for key, value in values.items():
                self.mqtt_client.publish(f"{base}/{key}", value, retain=True)

        except Exception as e:
            logger.warning("Failed to poll fan %s: %s", fan_id, e)
            if fan_entry["online"]:
                fan_entry["online"] = False
                self.mqtt_client.publish(f"{base}/online", "0", retain=True)

    def poll_all_fans(self):
        for fan_id, fan_entry in list(self.fans.items()):
            lock = self.fan_locks.get(fan_id)
            if lock and lock.acquire(blocking=False):
                try:
                    self._poll_fan(fan_id, fan_entry)
                finally:
                    lock.release()

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
                self.mqtt_client.publish(
                    f"{prefix}/{fan_id}/status/online", "0", retain=True
                )
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
    parser.add_argument(
        "--configdir",
        default="/opt/loxberry/config/plugins/smartmii",
        help="Path to plugin config directory",
    )
    parser.add_argument(
        "--logdir",
        default="/opt/loxberry/log/plugins/smartmii",
        help="Path to plugin log directory",
    )
    args = parser.parse_args()

    config_path = os.path.join(args.configdir, "smartmii.json")
    daemon = SmartmiDaemon(config_path, args.logdir)
    daemon.run()


if __name__ == "__main__":
    main()
