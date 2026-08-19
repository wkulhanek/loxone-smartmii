#!/usr/bin/env python3
"""Xiaomi Cloud API client for MIoT device control.

Extracted from Xiaomi Cloud Tokens Extractor by Piotr Machowski.
Simplified for headless daemon use: login, session persistence, property get/set.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import random
import time

import requests

try:
    from Crypto.Cipher import ARC4
except ModuleNotFoundError:
    from Cryptodome.Cipher import ARC4

logger = logging.getLogger("xiaomi_cloud")


class XiaomiCloudClient:

    SERVERS = ["cn", "de", "us", "ru", "tw", "sg", "in", "i2"]

    def __init__(self, server="de"):
        self.server = server
        self._agent = self._generate_agent()
        self._device_id = self._generate_device_id()
        self._session = requests.Session()
        self._ssecurity = None
        self._user_id = None
        self._service_token = None
        self._sign = None

    # --- Session persistence ---

    def save_session(self, path):
        logger.debug("Saving session to %s", path)
        with open(path, "w") as f:
            json.dump({
                "userId": self._user_id,
                "serviceToken": self._service_token,
                "ssecurity": self._ssecurity,
            }, f)
        os.chmod(path, 0o600)

    def load_session(self, path):
        logger.debug("Loading session from %s", path)
        if not os.path.exists(path):
            logger.debug("Session file not found: %s", path)
            return False
        with open(path) as f:
            data = json.load(f)
        self._user_id = data.get("userId")
        self._service_token = data.get("serviceToken")
        self._ssecurity = data.get("ssecurity")
        ok = all([self._user_id, self._service_token, self._ssecurity])
        logger.debug("Session loaded: valid=%s, userId=%s", ok, self._user_id)
        return ok

    def save_login_state(self, path):
        """Save full client state (including HTTP session cookies) for multi-step login."""
        logger.debug("Saving login state to %s", path)
        with open(path, "w") as f:
            json.dump({
                "server": self.server,
                "agent": self._agent,
                "device_id": self._device_id,
                "cookies": dict(self._session.cookies),
                "ssecurity": self._ssecurity,
                "user_id": self._user_id,
                "service_token": self._service_token,
                "sign": self._sign,
                "login_fields": getattr(self, "_login_fields", None),
                "login_url": getattr(self, "_login_url", None),
                "login_headers": getattr(self, "_login_headers", None),
                "location": getattr(self, "_location", None),
                "2fa_notification_url": getattr(self, "_2fa_notification_url", None),
                "2fa_context": getattr(self, "_2fa_context", None),
            }, f)
        os.chmod(path, 0o600)

    def load_login_state(self, path):
        """Restore client state from a previous login step."""
        logger.debug("Loading login state from %s", path)
        if not os.path.exists(path):
            logger.debug("Login state file not found: %s", path)
            return False
        with open(path) as f:
            state = json.load(f)
        self.server = state["server"]
        self._agent = state["agent"]
        self._device_id = state["device_id"]
        self._session = requests.Session()
        self._session.cookies.update(state.get("cookies", {}))
        self._ssecurity = state["ssecurity"]
        self._user_id = state["user_id"]
        self._service_token = state["service_token"]
        self._sign = state["sign"]
        if state.get("login_fields"):
            self._login_fields = state["login_fields"]
        if state.get("login_url"):
            self._login_url = state["login_url"]
        if state.get("login_headers"):
            self._login_headers = state["login_headers"]
        if state.get("location"):
            self._location = state["location"]
        if state.get("2fa_notification_url"):
            self._2fa_notification_url = state["2fa_notification_url"]
        if state.get("2fa_context"):
            self._2fa_context = state["2fa_context"]
        return True

    def is_session_valid(self):
        if not self._ssecurity or not self._service_token:
            logger.debug("Session invalid: missing credentials")
            return False
        url = self._api_url() + "/v2/user/get_device_cnt"
        params = {"data": '{"fetch_own": true, "fetch_share": true}'}
        result = self._api_call(url, params)
        valid = result is not None and "result" in result
        logger.debug("Session validation: %s", "valid" if valid else "invalid")
        return valid

    # --- Login flow ---

    def login(self, username, password):
        logger.info("Starting login for user %s (server: %s)", username, self.server)
        self._session.cookies.set("sdkVersion", "accountsdk-18.8.15", domain="mi.com")
        self._session.cookies.set("sdkVersion", "accountsdk-18.8.15", domain="xiaomi.com")
        self._session.cookies.set("deviceId", self._device_id, domain="mi.com")
        self._session.cookies.set("deviceId", self._device_id, domain="xiaomi.com")

        if not self._login_step1(username):
            logger.error("Login step 1 failed for user %s", username)
            return {"status": "error", "message": "Invalid username"}

        result = self._login_step2(username, password)
        if result.get("status") == "captcha":
            logger.info("Login requires captcha")
            return result
        if result.get("status") == "2fa":
            logger.info("Login requires 2FA")
            return result
        if result.get("status") != "ok":
            logger.error("Login step 2 failed: %s", result.get("message", "unknown"))
            return result

        if not self._service_token and self._location:
            if not self._login_step3():
                logger.error("Login step 3 failed: could not get service token")
                return {"status": "error", "message": "Failed to get service token"}

        logger.info("Login successful for user %s", username)
        return {"status": "ok"}

    def submit_captcha(self, captcha_code):
        """Continue login after captcha. Call after login() returns status=captcha."""
        logger.info("Submitting captcha")
        result = self._login_step2_retry(captcha_code)
        if result.get("status") != "ok":
            logger.error("Captcha submission failed: %s", result.get("message", "unknown"))
            return result
        if not self._service_token and self._location:
            if not self._login_step3():
                logger.error("Post-captcha step 3 failed")
                return {"status": "error", "message": "Failed to get service token"}
        logger.info("Captcha accepted, login complete")
        return {"status": "ok"}

    def submit_2fa(self, code):
        """Continue login after 2FA. Call after login() returns status=2fa."""
        logger.info("Submitting 2FA code")
        if not self._do_2fa_verify(code):
            logger.error("2FA verification failed")
            return {"status": "error", "message": "2FA verification failed"}
        logger.info("2FA accepted, login complete")
        return {"status": "ok"}

    # --- MIoT API ---

    def get_properties(self, did, props):
        """Get device properties. props = list of (siid, piid) tuples.
        Returns dict of {(siid, piid): value} for successful reads."""
        logger.debug("get_properties did=%s props=%s", did, props)
        url = self._api_url() + "/miotspec/prop/get"
        params = {"data": json.dumps({"params": [
            {"did": str(did), "siid": s, "piid": p} for s, p in props
        ]})}
        result = self._api_call(url, params)
        if not result or "result" not in result:
            logger.error("get_properties failed for did=%s", did)
            return None
        values = {}
        for item in result["result"]:
            if item.get("code") == 0:
                values[(item["siid"], item["piid"])] = item["value"]
        logger.debug("get_properties did=%s result=%s", did, values)
        return values

    def set_property(self, did, siid, piid, value):
        """Set a single device property. Returns True on success."""
        logger.debug("set_property did=%s siid=%d piid=%d value=%s", did, siid, piid, value)
        url = self._api_url() + "/miotspec/prop/set"
        params = {"data": json.dumps({"params": [
            {"did": str(did), "siid": siid, "piid": piid, "value": value}
        ]})}
        result = self._api_call(url, params)
        if not result or "result" not in result:
            logger.error("set_property failed for did=%s siid=%d piid=%d", did, siid, piid)
            return False
        ok = result["result"][0].get("code") == 0
        logger.debug("set_property did=%s siid=%d piid=%d success=%s", did, siid, piid, ok)
        return ok

    def get_devices(self):
        """List all devices from the cloud account. Returns list of device dicts."""
        logger.debug("Discovering devices (server: %s)", self.server)
        url = self._api_url() + "/v2/homeroom/gethome"
        params = {"data": '{"fg": true, "fetch_share": true, "fetch_share_dev": true, "limit": 300, "app_ver": 7}'}
        homes_result = self._api_call(url, params)
        if not homes_result or "result" not in homes_result:
            logger.error("Device discovery failed: could not get homes")
            return None

        homes = homes_result["result"].get("homelist", [])
        logger.debug("Found %d homes", len(homes))
        devices = []
        for home in homes:
            url = self._api_url() + "/v2/home/home_device_list"
            params = {"data": json.dumps({
                "home_owner": self._user_id,
                "home_id": home["id"],
                "limit": 200,
                "get_split_device": True,
                "support_smart_home": True,
            })}
            dev_result = self._api_call(url, params)
            if dev_result and "result" in dev_result:
                for d in dev_result["result"].get("device_info", []) or []:
                    devices.append({
                        "name": d.get("name", ""),
                        "did": d.get("did", ""),
                        "model": d.get("model", ""),
                        "ip": d.get("localip", ""),
                        "mac": d.get("mac", ""),
                        "token": d.get("token", ""),
                    })
        logger.info("Device discovery complete: %d devices found", len(devices))
        return devices

    # --- Internal: API call with RC4 encryption ---

    def _api_url(self):
        c = self.server
        return "https://" + ("" if c == "cn" else (c + ".")) + "api.io.mi.com/app"

    def _api_call(self, url, params):
        logger.debug("API request: POST %s", url)
        headers = {
            "Accept-Encoding": "identity",
            "User-Agent": self._agent,
            "Content-Type": "application/x-www-form-urlencoded",
            "x-xiaomi-protocal-flag-cli": "PROTOCAL-HTTP2",
            "MIOT-ENCRYPT-ALGORITHM": "ENCRYPT-RC4",
        }
        cookies = {
            "userId": str(self._user_id),
            "yetAnotherServiceToken": str(self._service_token),
            "serviceToken": str(self._service_token),
            "locale": "en_GB",
            "timezone": "GMT+02:00",
            "is_daylight": "1",
            "dst_offset": "3600000",
            "channel": "MI_APP_STORE",
        }
        millis = round(time.time() * 1000)
        nonce = self._generate_nonce(millis)
        snonce = self._signed_nonce(nonce)
        fields = self._generate_enc_params(url, "POST", snonce, nonce, params, self._ssecurity)
        try:
            response = self._session.post(url, headers=headers, cookies=cookies, params=fields, timeout=10)
        except requests.RequestException as e:
            logger.error("API call failed: POST %s — %s", url, e)
            return None
        if response.status_code == 200:
            try:
                decoded = self._decrypt_rc4(self._signed_nonce(fields["_nonce"]), response.text)
                result = json.loads(decoded)
            except (ValueError, Exception) as e:
                logger.error("API response decode failed for POST %s: %s", url, e)
                return None
            logger.debug("API response: POST %s — %s", url, result)
            return result
        logger.error("API returned HTTP %d for POST %s", response.status_code, url)
        return None

    # --- Internal: login steps ---

    def _login_step1(self, username):
        logger.debug("Login step 1: serviceLogin for %s", username)
        url = "https://account.xiaomi.com/pass/serviceLogin?sid=xiaomiio&_json=true"
        headers = {"User-Agent": self._agent, "Content-Type": "application/x-www-form-urlencoded"}
        cookies = {"userId": username}
        response = self._session.get(url, headers=headers, cookies=cookies)
        if response.status_code != 200:
            logger.debug("Login step 1: HTTP %d", response.status_code)
            return False
        data = self._to_json(response.text)
        if "_sign" in data:
            self._sign = data["_sign"]
            logger.debug("Login step 1: got _sign")
            return True
        if "ssecurity" in data:
            self._ssecurity = data["ssecurity"]
            self._user_id = data["userId"]
            self._location = data.get("location")
            logger.debug("Login step 1: got ssecurity (cached session)")
            return True
        logger.debug("Login step 1: unexpected response keys: %s", list(data.keys()))
        return False

    def _login_step2(self, username, password):
        logger.debug("Login step 2: serviceLoginAuth2 for %s", username)
        url = "https://account.xiaomi.com/pass/serviceLoginAuth2"
        headers = {"User-Agent": self._agent, "Content-Type": "application/x-www-form-urlencoded"}
        self._login_fields = {
            "sid": "xiaomiio",
            "hash": hashlib.md5(password.encode()).hexdigest().upper(),
            "callback": "https://sts.api.io.mi.com/sts",
            "qs": "%3Fsid%3Dxiaomiio%26_json%3Dtrue",
            "user": username,
            "_sign": self._sign,
            "_json": "true",
        }
        self._login_url = url
        self._login_headers = headers
        response = self._session.post(url, headers=headers, params=self._login_fields, allow_redirects=False)
        if not response or response.status_code != 200:
            return {"status": "error", "message": "Login request failed"}
        data = self._to_json(response.text)

        if data.get("captchaUrl"):
            captcha_url = data["captchaUrl"]
            if captcha_url.startswith("/"):
                captcha_url = "https://account.xiaomi.com" + captcha_url
            img_resp = self._session.get(captcha_url)
            if img_resp.status_code == 200:
                import base64 as b64mod
                return {
                    "status": "captcha",
                    "image": b64mod.b64encode(img_resp.content).decode(),
                }
            return {"status": "error", "message": "Failed to fetch captcha image"}

        if "notificationUrl" in data:
            self._2fa_notification_url = data["notificationUrl"]
            self._2fa_context = None
            self._do_2fa_start()
            return {"status": "2fa"}

        if "ssecurity" in data and len(str(data["ssecurity"])) > 4:
            self._ssecurity = data["ssecurity"]
            self._user_id = data.get("userId")
            self._location = data.get("location")
            return {"status": "ok"}

        return {"status": "error", "message": "Login failed"}

    def _login_step2_retry(self, captcha_code):
        logger.debug("Login step 2 retry with captcha")
        self._login_fields["captCode"] = captcha_code
        response = self._session.post(
            self._login_url, headers=self._login_headers,
            params=self._login_fields, allow_redirects=False
        )
        if not response or response.status_code != 200:
            return {"status": "error", "message": "Login retry failed"}
        data = self._to_json(response.text)

        if data.get("code") == 87001:
            return {"status": "error", "message": "Invalid captcha"}

        if "notificationUrl" in data:
            self._2fa_notification_url = data["notificationUrl"]
            self._do_2fa_start()
            return {"status": "2fa"}

        if "ssecurity" in data and len(str(data["ssecurity"])) > 4:
            self._ssecurity = data["ssecurity"]
            self._user_id = data.get("userId")
            self._location = data.get("location")
            return {"status": "ok"}

        return {"status": "error", "message": "Login failed after captcha"}

    def _login_step3(self):
        logger.debug("Login step 3: fetching service token")
        headers = {"User-Agent": self._agent, "Content-Type": "application/x-www-form-urlencoded"}
        response = self._session.get(self._location, headers=headers)
        if response.status_code == 200:
            self._service_token = response.cookies.get("serviceToken")
            logger.debug("Login step 3: service token %s", "obtained" if self._service_token else "missing")
            return bool(self._service_token)
        logger.debug("Login step 3: HTTP %d", response.status_code)
        return False

    # --- Internal: 2FA flow ---

    def _do_2fa_start(self):
        import re
        from urllib.parse import parse_qs, urlparse
        logger.debug("2FA: starting email verification flow")
        headers = {"User-Agent": self._agent, "Content-Type": "application/x-www-form-urlencoded"}
        self._session.get(self._2fa_notification_url, headers=headers)
        self._2fa_context = parse_qs(urlparse(self._2fa_notification_url).query)["context"][0]
        self._session.get("https://account.xiaomi.com/identity/list", params={
            "sid": "xiaomiio", "context": self._2fa_context, "_locale": "en_US",
        }, headers=headers)
        self._session.post("https://account.xiaomi.com/identity/auth/sendEmailTicket", params={
            "_dc": str(int(time.time() * 1000)), "sid": "xiaomiio",
            "context": self._2fa_context, "mask": "0", "_locale": "en_US",
        }, data={
            "retry": "0", "icode": "", "_json": "true",
            "ick": self._session.cookies.get("ick", ""),
        }, headers=headers)

    def _do_2fa_verify(self, code):
        import re
        logger.debug("2FA: verifying email code")
        headers = {"User-Agent": self._agent, "Content-Type": "application/x-www-form-urlencoded"}
        r = self._session.post("https://account.xiaomi.com/identity/auth/verifyEmail", params={
            "_flag": "8", "_json": "true", "sid": "xiaomiio",
            "context": self._2fa_context, "mask": "0", "_locale": "en_US",
        }, data={
            "_flag": "8", "ticket": code, "trust": "false", "_json": "true",
            "ick": self._session.cookies.get("ick", ""),
        }, headers=headers)

        if r.status_code != 200:
            return False

        try:
            jr = r.json()
            finish_loc = jr.get("location")
        except Exception:
            finish_loc = r.headers.get("Location")
            if not finish_loc and r.text:
                m = re.search(r'https://account\.xiaomi\.com/identity/result/check\?[^"\']+', r.text)
                if m:
                    finish_loc = m.group(0)

        if not finish_loc:
            r0 = self._session.get(
                "https://account.xiaomi.com/identity/result/check",
                params={"sid": "xiaomiio", "context": self._2fa_context, "_locale": "en_US"},
                headers=headers, allow_redirects=False
            )
            if r0.status_code in (301, 302) and r0.headers.get("Location"):
                finish_loc = r0.headers["Location"]

        if not finish_loc:
            return False

        if "identity/result/check" in finish_loc:
            r = self._session.get(finish_loc, headers=headers, allow_redirects=False)
            end_url = r.headers.get("Location")
        else:
            end_url = finish_loc

        if not end_url:
            return False

        r = self._session.get(end_url, headers=headers, allow_redirects=False)
        if r.status_code == 200 and "Xiaomi Account - Tips" in r.text:
            r = self._session.get(end_url, headers=headers, allow_redirects=False)

        ext_prag = r.headers.get("extension-pragma")
        if ext_prag:
            try:
                ep = json.loads(ext_prag)
                if ep.get("ssecurity"):
                    self._ssecurity = ep["ssecurity"]
            except Exception:
                pass

        if not self._ssecurity:
            return False

        sts_url = r.headers.get("Location")
        if not sts_url and r.text:
            idx = r.text.find("https://sts.api.io.mi.com/sts")
            if idx != -1:
                end = r.text.find('"', idx)
                sts_url = r.text[idx:end if end != -1 else idx + 300]

        if not sts_url:
            return False

        r = self._session.get(sts_url, headers=headers, allow_redirects=True)
        if r.status_code != 200:
            return False

        self._service_token = self._session.cookies.get("serviceToken", domain=".sts.api.io.mi.com")
        if not self._service_token:
            return False

        for d in [".api.io.mi.com", ".io.mi.com", ".mi.com"]:
            self._session.cookies.set("serviceToken", self._service_token, domain=d)
            self._session.cookies.set("yetAnotherServiceToken", self._service_token, domain=d)

        self._user_id = (
            self._user_id
            or self._session.cookies.get("userId", domain=".xiaomi.com")
            or self._session.cookies.get("userId", domain=".sts.api.io.mi.com")
        )
        return True

    # --- Internal: crypto helpers ---

    def _signed_nonce(self, nonce):
        h = hashlib.sha256(base64.b64decode(self._ssecurity) + base64.b64decode(nonce))
        return base64.b64encode(h.digest()).decode("utf-8")

    @staticmethod
    def _generate_nonce(millis):
        nonce_bytes = os.urandom(8) + (int(millis / 60000)).to_bytes(4, byteorder="big")
        return base64.b64encode(nonce_bytes).decode()

    @staticmethod
    def _generate_agent():
        agent_id = "".join(chr(random.randint(65, 69)) for _ in range(13))
        random_text = "".join(chr(random.randint(97, 122)) for _ in range(18))
        return f"{random_text}-{agent_id} APP/com.xiaomi.mihome APPV/10.5.201"

    @staticmethod
    def _generate_device_id():
        return "".join(chr(random.randint(97, 122)) for _ in range(6))

    @staticmethod
    def _generate_enc_signature(url, method, signed_nonce, params):
        parts = [method.upper(), url.split("com")[1].replace("/app/", "/")]
        for k, v in params.items():
            parts.append(f"{k}={v}")
        parts.append(signed_nonce)
        return base64.b64encode(hashlib.sha1("&".join(parts).encode("utf-8")).digest()).decode()

    @staticmethod
    def _generate_enc_params(url, method, signed_nonce, nonce, params, ssecurity):
        params["rc4_hash__"] = XiaomiCloudClient._generate_enc_signature(url, method, signed_nonce, params)
        for k, v in params.items():
            params[k] = XiaomiCloudClient._encrypt_rc4(signed_nonce, v)
        params.update({
            "signature": XiaomiCloudClient._generate_enc_signature(url, method, signed_nonce, params),
            "ssecurity": ssecurity,
            "_nonce": nonce,
        })
        return params

    @staticmethod
    def _to_json(text):
        return json.loads(text.replace("&&&START&&&", ""))

    @staticmethod
    def _encrypt_rc4(password, payload):
        r = ARC4.new(base64.b64decode(password))
        r.encrypt(bytes(1024))
        return base64.b64encode(r.encrypt(payload.encode())).decode()

    @staticmethod
    def _decrypt_rc4(password, payload):
        r = ARC4.new(base64.b64decode(password))
        r.encrypt(bytes(1024))
        return r.encrypt(base64.b64decode(payload))
