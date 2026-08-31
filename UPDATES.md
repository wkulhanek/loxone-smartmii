# Smartmii Plugin — Improvement Findings

Code review of the v1.0.4 codebase (2026-08-31). Each entry lists the issue,
location, why it matters, suggested fix, and priority. Work through these as
independent increments.

## Robustness

### R1. Daemon is unsupervised — crashes are permanent until reboot

- **Location:** `daemon/daemon`, `webfrontend/htmlauth/index.php:247`
- **Issue:** Any uncaught exception (or an OOM-kill) stops the daemon forever.
  The MQTT LWT publishes `offline` so Loxone *sees* it, but nothing restarts
  the process.
- **Fix:** Wrap the launch in a restart loop with backoff (e.g. `while true; do
  "$PYTHON" "$DAEMON" ...; sleep 5; done`) in both `daemon/daemon` and the
  web-UI `daemon_restart` path.

**Priority: high**

### R2. Non-atomic writes + no defensive parse can crash the daemon

- **Location:** `bin/xiaomi_cloud.py:53-65` (`load_session`),
  `bin/xiaomi_cloud.py:43-51` (`save_session`), `index.php:119`
  (`file_put_contents` config save), `bin/smartmii_daemon.py:360-375`
  (`check_config_changed`)
- **Issue:** Both the session file and the config file are written in place.
  The daemon polls mtimes every second and can read a half-written file.
  `load_session()` does not catch `JSONDecodeError`, and the exception is not
  caught in `init_cloud()` either → traceback kills the daemon.
- **Fix:** Write to a temp file + `os.replace()`/`rename()` in all three
  writers (Python `save_session`, PHP config save). Additionally wrap session
  load in `try/except` so a corrupt file degrades to "no session" instead of
  a crash.

**Priority: high**

### R3. Login HTTP calls have no timeout (web UI can hang)

- **Location:** `bin/xiaomi_cloud.py:311, 344, 353, 406, 421-432, 438-444,
  460-480`, `index.php:81-94` (`cloud_command`)
- **Issue:** `_login_step1/2/3`, the captcha image fetch, and the 2FA calls
  all omit `timeout=` on `requests`. The PHP side `proc_open` also has no
  timeout. A hung `account.xiaomi.com` connection hangs the web UI request
  until PHP's `max_execution_time`.
- **Fix:** Add `timeout=10` (or similar) to every `requests` call in
  `xiaomi_cloud.py`. In PHP, drain/close the proc after ~60 s and return an
  error response.

**Priority: high**

### R4. `self.cloud` used without a None check

- **Location:** `bin/smartmii_daemon.py:318`
- **Issue:** If the cloud session never loaded (e.g. user has not logged in
  yet), commands fail with a confusing `NoneType has no attribute
  'set_property'` log line instead of a clear cause.
- **Fix:** In `_execute_command()`, check `self.cloud is None` first and log
  "command dropped: cloud session not available".

**Priority: high**

### R5. Silent long-term session expiry

- **Location:** `bin/smartmii_daemon.py:421-434` (main loop),
  `bin/smartmii_daemon.py:329-354` (`_poll_fan`)
- **Issue:** If the Xiaomi session expires mid-run, `self.cloud` stays set and
  every poll just logs a `Failed to poll fan` warning forever. The client is
  only re-validated on startup, session-file change, or when it was never
  initialized. No alarm is raised to Loxone.
- **Fix:** Track consecutive all-fans-failed cycles. After N (e.g. 5),
  re-run `init_cloud()`; if that fails, log a prominent
  "Xiaomi session expired — please re-login via web UI" error and publish a
  sentinel value (e.g. `relogin`) on `{prefix}/daemon/status` so a Loxone
  program can raise an alarm.

**Priority: high**

### R6. No server-side validation of fan entries on config save

- **Location:** `index.php:107-128` (`save_config`)
- **Issue:** The prefix is sanitized, but `fans[]` is trusted from the
  client: a bad `id` (uppercase, dashes, slashes) breaks MQTT topic parsing,
  a non-numeric `did` is accepted, and `xiaomi_server` is not whitelisted.
  The JS validates, but a hand-crafted POST bypasses it.
- **Fix:** Server-side: `fan.id` against `^[a-z0-9_]+$`, `fan.did` numeric,
  `fan.name` length cap, `xiaomi_server` in
  `[cn, de, us, ru, tw, sg, in, i2]`.

**Priority: medium**

### R7. Boot script writes a wrong/stale PID file

- **Location:** `daemon/daemon:26-28`
- **Issue:** `su - loxberry ... -c "... & echo \$! > \"$PIDFILE\""` records
  the `su` wrapper PID, which the daemon later overwrites with its own PID
  (`bin/smartmii_daemon.py:401`). If the daemon dies in between, the
  pidfile points at the wrong process.
- **Fix:** Drop the `echo \$! > \"$PIDFILE\"` from `daemon/daemon`; the
  daemon already writes its own pidfile.

**Priority: medium**

### R8. Unbounded command queue, no coalescing

- **Location:** `bin/smartmii_daemon.py:122, 222-228, 283`
- **Issue:** An MQTT burst (e.g. Loxone re-publishing on program download)
  queues one cloud round-trip per message, each with up to a 10 s API
  timeout. The queue grows without bound.
- **Fix:** Cap the queue (drop oldest with a warning) and coalesce duplicate
  `(fan_id, command)` entries so a burst collapses into one cloud call per
  fan/command.

**Priority: low**

### R9. Any config save resets all fans' in-memory state

- **Location:** `bin/smartmii_daemon.py:200-212` (`load_and_apply_config`)
- **Issue:** Every reload rebuilds all fan entries, wiping `last_status` and
  `online` even when only `poll_interval` or `mqtt_prefix` changed. A
  `toggle` command right after an unrelated save assumes power=off.
- **Fix:** For fans whose entry is unchanged, keep the existing
  `online`/`last_status` instead of resetting.

**Priority: low**

### R10. Minor error-handling polish

- `bin/xiaomi_cloud.py:296` — `except (ValueError, Exception)` is redundant;
  use `except Exception`.
- `bin/smartmii_daemon.py:69, 70` — non-numeric MQTT payload for
  `fan_level`/`angle` surfaces as a raw `ValueError` log line; catch and log
  "invalid value for <command>: <payload>" like the other converters.

**Priority: low**

## Performance

### P1. Every poll republishes all retained topics even when unchanged

- **Location:** `bin/smartmii_daemon.py:329-354` (`_poll_fan`)
- **Issue:** With the 30 s default, each fan republishes ~7 retained topics
  every cycle regardless of whether anything changed. All of it flows
  through the Loxone MQTT gateway.
- **Fix:** Publish only on change. Keep one full publish on daemon start,
  after MQTT (re)connect, and periodically (e.g. every 10th cycle) to heal
  broker-side retained state after a broker restart.

**Priority: medium**

### P2. Serial polling

- **Location:** `bin/smartmii_daemon.py:356-358` (`poll_all_fans`)
- **Issue:** N fans × up to 10 s API timeout per cycle; a slow or dead cloud
  stalls the entire poll cycle for all fans.
- **Fix (optional):** Poll fans concurrently with a small
  `ThreadPoolExecutor`. Marginal benefit for 1-4 fans.

**Priority: low**

## User-friendliness

### U1. No way to check session validity in the web UI

- **Location:** `index.php:217-228` (`cloud_validate` — exists but unused),
  `index.php:310-316` (`session-status`)
- **Issue:** "Session active" is displayed purely from file existence; the
  session may actually be expired. The `cloud_validate` AJAX action exists
  but no UI element calls it.
- **Fix:** Add a "Check session" button (or auto-check on page load when a
  session file exists) that displays valid/expired.

**Priority: medium**

### U2. README "Session expiration" section is stale

- **Location:** `README.adoc:119-120`
- **Issue:** Says the daemon "logs an error and exits" when the session
  expires. It actually keeps running, retries, and auto-recovers when the
  user re-logins (session mtime change triggers `init_cloud`).
- **Fix:** Correct the wording; also document that changing the server
  region requires re-login.

**Priority: low**

### U3. `toggle` can toggle the wrong way

- **Location:** `bin/smartmii_daemon.py:303-307`, `README.adoc`
- **Issue:** `toggle` uses `last_status["power"]`, which is up to one poll
  interval stale. If the fan was changed from the Mi Home app, the toggle
  direction can be wrong. Inherent to cloud polling.
- **Fix:** Document in the MQTT Reference section.

**Priority: low**

### U4. Duplicate fan IDs silently shadow each other

- **Location:** `index.php` (`addDiscoveredFan`, `saveFan`),
  `bin/smartmii_daemon.py:200-212`
- **Issue:** Two fans with the same name produce the same slug; the second
  silently shadows the first in the daemon's fan dict.
- **Fix:** Duplicate-id/did check in the JS (refuse or append suffix);
  warning log in the daemon on duplicate entries.

**Priority: low**

## Security

### S1. Discovery returns the cloud device token to the browser

- **Location:** `bin/xiaomi_cloud.py:246-254` (`get_devices`)
- **Issue:** Each discovered device's `token` (plus `ip`/`mac`) is passed
  through to the web UI JSON. The token is sensitive (grants local MIoT
  control) and is never used by the plugin, which is cloud-API-only.
- **Fix:** Drop `token` (and optionally `ip`/`mac`) from the discovery
  response.

**Priority: medium**

## Housekeeping

### H1. `prerelease.cfg` is byte-identical to `release.cfg`

- **Location:** `prerelease.cfg`, `release.cfg`,
  `.github/workflows/release.yml:45`
- **Issue:** The release workflow always copies `release.cfg` to
  `prerelease.cfg`, so LoxBerry's "prerelease" auto-update channel always
  equals stable.
- **Fix:** Either stop shipping the copy (remove the `PRERELEASECFG` line
  from `plugin.cfg`) or actually maintain prerelease artifacts.

**Priority: low**
