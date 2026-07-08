# Smartmii Plugin — Development Updates

## 2026-06-26: Initial Approach

Started with local LAN control using the `python-miio` library to communicate directly with Smartmi Standing Fan 3 (zhimi.fan.za5) devices over UDP port 54321.

**Problem:** The fans sit on a dedicated IoT VLAN behind a UniFi firewall. While outbound UDP packets reach the fans, the return traffic is blocked — making local control impossible without firewall changes. Diagnostic tests (raw UDP handshake, python-miio generic discovery, FanZA5 class) all failed with empty responses or "Unable to discover the device."

## 2026-06-26: Switch to Xiaomi Cloud API

Pivoted to controlling the fans through the Xiaomi Cloud API instead of local LAN. This works reliably across VLANs since all communication goes through Xiaomi's servers via HTTPS.

**Changes:**
- Created `bin/xiaomi_cloud.py` — cloud API client extracted from the Xiaomi Cloud Tokens Extractor project, with login flow (captcha + 2FA), session persistence, and MIoT property get/set via RC4-encrypted API calls
- Created `bin/cloud_login.py` — multi-step login helper for the PHP web UI, with pickle-based state persistence between HTTP requests (needed because each AJAX call spawns a new Python process)
- Rewrote `bin/smartmii_daemon.py` — replaced python-miio with cloud API calls using SIID/PIID property mapping
- Rewrote `webfrontend/htmlauth/index.php` — added Xiaomi Cloud login section (with captcha image display and 2FA code input), device discovery, DID-based fan configuration, and Loxone Config XML template export
- Updated dependencies from `python-miio` to `pycryptodome` + `requests`

**Login flow pitfalls discovered:**
- Xiaomi rate-limits verification codes — too many captcha/2FA attempts in one day means waiting ~24 hours
- `CookieConflictError` when pickling session cookies — fixed by using `cookies.copy()` instead of `dict(cookies)` to preserve per-domain cookie jars
- `_login_headers` not persisted between login and captcha steps — caused silent crash on captcha submission

## 2026-06-26: Property Mapping Fixes

Tested each SIID/PIID combination against the actual device to verify what works via the cloud API.

**Discoveries:**
- SIID 2/PIID 3 was labeled "mode" but is actually **oscillate** (horizontal swing on/off)
- SIID 2/PIID 7 is the real **mode** (0=natural wind, 1=straight wind)
- SIID 2/PIID 4 does nothing via cloud — removed
- **buzzer** (SIID 5/PIID 1) requires `bool`, not `int` — sending `int` silently fails
- **child_lock** (SIID 6/PIID 1) does not work via cloud API — removed
- **delay_off** (SIID 3/PIID 1) only works as a bool toggle, not settable in minutes — removed (too limited)
- **led_brightness** (SIID 7/PIID 1) is read-only via cloud, set does nothing — removed

**Final verified property set:**

| SIID | PIID | Property | Type | Notes |
|------|------|----------|------|-------|
| 2 | 1 | power | bool | on/off |
| 2 | 2 | fan_level | int | 1-4 |
| 2 | 3 | oscillate | bool | horizontal swing |
| 2 | 5 | angle | int | 30/60/90/120 degrees |
| 2 | 7 | mode | int | 0=natural, 1=straight |
| 5 | 1 | buzzer | bool | button sound feedback |

## 2026-06-26: UI Improvements

- Added "+" button on discovered devices for one-click fan addition (instead of opening edit form and hiding the list)
- Added umlaut-aware slugify for fan IDs (ü→ue, ö→oe, ä→ae, ß→ss)
- Added Loxone Config XML template download button (`VI_MQTT_UDP_Smartmii.xml`) with human-readable formatting and per-property value ranges

## 2026-06-26: Documentation

- Added comprehensive `README.adoc` covering installation, configuration, MQTT reference, Loxone integration, and known issues

## 2026-07-08: LoxBerry Logging Integration

Added proper logging throughout the plugin using LoxBerry's logging infrastructure.

**Changes:**
- Enabled `CUSTOM_LOGLEVELS=true` in `plugin.cfg` — log level is now configurable from LoxBerry's Plugin Management page
- `bin/smartmii_daemon.py` — reads LoxBerry's configured loglevel via Perl one-liner, maps syslog levels 0-7 to Python logging levels, added `--loglevel` CLI fallback for development. Added DEBUG logging for poll cycles, MQTT messages, command execution, config reloads
- `bin/xiaomi_cloud.py` — added DEBUG logging to every API method (get_properties, set_property, get_devices, login flow steps, session save/load) and to the central `_api_call()` method (request URL + response body)
- `bin/cloud_login.py` — added logging setup reading `LBPLOGDIR` env var, each action (login/captcha/2fa/discover/test/validate) now logged at INFO with errors at ERROR
- `webfrontend/htmlauth/index.php` — integrated `loxberry_log.php` with "WebUI" log sessions (LOGSTART/LOGEND per AJAX request), logs config saves, cloud login, daemon control, and device discovery. Passes LBPLOGDIR to Python subprocess

**Log level mapping (LoxBerry → Python):**

| LoxBerry | Python | What is logged |
|----------|--------|---------------|
| 7 (Debug) | DEBUG | Every cloud API request/response, MQTT messages, poll results |
| 6 (Info) | INFO | Login, discovery, daemon start/stop, commands |
| 3 (Error) | ERROR | API failures, login errors, config errors |
| 0 (Off) | disabled | Nothing |
