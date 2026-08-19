<?php
if (session_status() === PHP_SESSION_NONE) session_start();
if (empty($_SESSION['csrf_token'])) {
    $_SESSION['csrf_token'] = bin2hex(random_bytes(16));
}

require_once "loxberry_system.php";
require_once "loxberry_web.php";
require_once "loxberry_log.php";

$L = LBSystem::readlanguage("language.ini");

$pluginconfigdir = LBPCONFIGDIR;
$pluginbindir = LBPBINDIR;
$plugindatadir = LBPDATADIR;
$pluginlogdir = LBPLOGDIR;
$configfile = "$pluginconfigdir/smartmii.json";
$sessionfile = "$pluginconfigdir/xiaomi_session.json";
$pidfile = "/run/shm/smartmii.pid";
$python = "$plugindatadir/venv/bin/python3";
$cloud_login = "$pluginbindir/cloud_login.py";

$log = LBLog::newLog(["name" => "WebUI", "stderr" => 1, "addtime" => 1, "append" => 1]);

// --- Loxone XML export (before any HTML output) ---
if (isset($_GET['export']) && $_GET['export'] === 'loxone') {
    $config = json_decode(file_get_contents($configfile), true);
    if (!$config) { http_response_code(500); exit; }

    $prefix = htmlspecialchars($config['mqtt_prefix'] ?? 'smartmii');
    $props = [
        ['power',            'Power',            'false', 0, 1],
        ['fan_level',        'Fan Level',        'true',  0, 4],
        ['oscillate',      'Oscillate',      'false', 0, 1],
        ['angle',          'Angle',          'true',  0, 120],
        ['mode',           'Mode',           'true',  0, 1],
        ['buzzer',         'Buzzer',         'false', 0, 1],
        ['online',         'Online',         'false', 0, 1],
    ];

    $xml = '<?xml version="1.0" encoding="utf-8"?>' . "\n";
    $xml .= '<VirtualInUdp Title="Smartmii Fans (MQTT UDP)" Comment="" Address="" Port="11883">' . "\n";
    foreach ($config['fans'] ?? [] as $fan) {
        if (!($fan['enabled'] ?? true)) continue;
        $fname = htmlspecialchars($fan['name'] ?? $fan['id']);
        $fid = htmlspecialchars($fan['id']);
        foreach ($props as [$key, $label, $analog, $min, $max]) {
            $title = "$label $fname";
            $check = "MQTT:\\i$prefix/$fid/status/$key=\\i\\v";
            $hi = $analog === 'true' ? $max : 1;
            $xml .= "  <VirtualInUdpCmd\n"
                . "    Title=\"$title\"\n"
                . "    Comment=\"\"\n"
                . "    Address=\"\"\n"
                . "    Check=\"$check\"\n"
                . "    Signed=\"true\"\n"
                . "    Analog=\"$analog\"\n"
                . "    SourceValLow=\"0\"\n"
                . "    DestValLow=\"0\"\n"
                . "    SourceValHigh=\"$hi\"\n"
                . "    DestValHigh=\"$hi\"\n"
                . "    DefVal=\"0\"\n"
                . "    MinVal=\"$min\"\n"
                . "    MaxVal=\"$max\"/>\n";
        }
    }
    $xml .= '</VirtualInUdp>' . "\n";

    header('Content-Type: application/xml; charset=utf-8');
    header('Content-Disposition: attachment; filename="VI_MQTT_UDP_Smartmii.xml"');
    echo $xml;
    exit;
}

function daemon_is_running($pidfile) {
    if (!file_exists($pidfile)) return false;
    $pid = trim(file_get_contents($pidfile));
    return $pid && file_exists("/proc/$pid");
}

function cloud_command($python, $cloud_login, $input) {
    global $pluginlogdir;
    $desc = [0 => ['pipe', 'r'], 1 => ['pipe', 'w'], 2 => ['pipe', 'w']];
    $env = array_merge($_ENV, ['LBPLOGDIR' => $pluginlogdir]);
    $proc = proc_open("$python $cloud_login", $desc, $pipes, null, $env);
    if (!is_resource($proc)) return null;
    fwrite($pipes[0], json_encode($input) . "\n");
    fclose($pipes[0]);
    $output = stream_get_contents($pipes[1]);
    fclose($pipes[1]);
    fclose($pipes[2]);
    proc_close($proc);
    return json_decode(trim($output), true);
}

// --- AJAX handlers ---
if (isset($_POST['ajax'])) {
    header('Content-Type: application/json');
    $action = $_POST['ajax'];
    if (!isset($_POST['csrf_token']) || !hash_equals($_SESSION['csrf_token'], $_POST['csrf_token'])) {
        http_response_code(403);
        echo json_encode(['success' => false, 'message' => 'CSRF validation failed']);
        exit;
    }
    LOGSTART("AJAX: $action");

    if ($action === 'save_config') {
        $config = json_decode($_POST['config'], true);
        if ($config === null) {
            LOGERR("Config save failed: invalid JSON");
            LOGEND("");
            echo json_encode(['success' => false, 'message' => $L['BASIC.MSG_SAVE_ERROR']]);
            exit;
        }
        $config['poll_interval'] = max(5, min(300, intval($config['poll_interval'])));
        $config['mqtt_prefix'] = preg_replace('/[^a-zA-Z0-9_\-]/', '', $config['mqtt_prefix']);
        if (empty($config['mqtt_prefix'])) $config['mqtt_prefix'] = 'smartmii';
        if (!isset($config['fans'])) $config['fans'] = [];
        $result = file_put_contents($configfile, json_encode($config, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES));
        if ($result !== false) {
            LOGINF("Config saved (" . count($config['fans']) . " fans, prefix: " . $config['mqtt_prefix'] . ")");
        } else {
            LOGERR("Config save failed: write error");
        }
        LOGEND("");
        echo json_encode(['success' => $result !== false, 'message' => $result !== false ? $L['BASIC.MSG_SAVED'] : $L['BASIC.MSG_SAVE_ERROR']]);
        exit;
    }

    if ($action === 'cloud_login') {
        LOGINF("Cloud login for " . ($_POST['username'] ?? '') . " (server: " . ($_POST['server'] ?? 'de') . ")");
        $result = cloud_command($python, $cloud_login, [
            'action' => 'login',
            'username' => $_POST['username'] ?? '',
            'password' => $_POST['password'] ?? '',
            'server' => $_POST['server'] ?? 'de',
            'session_file' => $sessionfile,
        ]);
        if (!$result) { LOGERR("Cloud login script failed"); }
        else { LOGDEB("Cloud login result: " . ($result['status'] ?? 'null')); }
        LOGEND("");
        echo json_encode($result ?: ['status' => 'error', 'message' => 'Login script failed']);
        exit;
    }

    if ($action === 'cloud_captcha') {
        LOGINF("Captcha submission");
        $result = cloud_command($python, $cloud_login, [
            'action' => 'captcha',
            'code' => $_POST['code'] ?? '',
            'session_file' => $sessionfile,
        ]);
        if (!$result) { LOGERR("Captcha script failed"); }
        else { LOGDEB("Captcha result: " . ($result['status'] ?? 'null')); }
        LOGEND("");
        echo json_encode($result ?: ['status' => 'error', 'message' => 'Captcha submission failed']);
        exit;
    }

    if ($action === 'cloud_2fa') {
        LOGINF("2FA code submission");
        $result = cloud_command($python, $cloud_login, [
            'action' => '2fa',
            'code' => $_POST['code'] ?? '',
            'session_file' => $sessionfile,
        ]);
        if (!$result) { LOGERR("2FA script failed"); }
        else { LOGDEB("2FA result: " . ($result['status'] ?? 'null')); }
        LOGEND("");
        echo json_encode($result ?: ['status' => 'error', 'message' => '2FA submission failed']);
        exit;
    }

    if ($action === 'cloud_discover') {
        LOGINF("Device discovery (server: " . ($_POST['server'] ?? 'de') . ")");
        $result = cloud_command($python, $cloud_login, [
            'action' => 'discover',
            'session_file' => $sessionfile,
            'server' => $_POST['server'] ?? 'de',
        ]);
        if ($result && $result['status'] === 'ok') {
            LOGINF("Discovery: " . count($result['devices'] ?? []) . " devices found");
        } else {
            LOGERR("Discovery failed: " . ($result['message'] ?? 'unknown'));
        }
        LOGEND("");
        echo json_encode($result ?: ['status' => 'error', 'message' => 'Discovery failed']);
        exit;
    }

    if ($action === 'test_connection') {
        $did = $_POST['did'] ?? '';
        if (!preg_match('/^\d+$/', $did)) {
            LOGERR("Connection test: invalid DID '$did'");
            LOGEND("");
            echo json_encode(['success' => false, 'message' => $L['BASIC.MSG_INVALID_DID']]);
            exit;
        }
        LOGINF("Connection test for DID $did");
        $result = cloud_command($python, $cloud_login, [
            'action' => 'test',
            'session_file' => $sessionfile,
            'server' => $_POST['server'] ?? 'de',
            'did' => $did,
        ]);
        if ($result && $result['status'] === 'ok') {
            LOGINF("Test OK: " . $result['message']);
            echo json_encode(['success' => true, 'message' => $result['message']]);
        } else {
            LOGERR("Test failed: " . ($result['message'] ?? $L['BASIC.MSG_TEST_FAIL']));
            echo json_encode(['success' => false, 'message' => $result['message'] ?? $L['BASIC.MSG_TEST_FAIL']]);
        }
        LOGEND("");
        exit;
    }

    if ($action === 'cloud_validate') {
        LOGDEB("Session validation");
        $result = cloud_command($python, $cloud_login, [
            'action' => 'validate',
            'session_file' => $sessionfile,
            'server' => $_POST['server'] ?? 'de',
        ]);
        LOGDEB("Session valid: " . json_encode($result['valid'] ?? false));
        LOGEND("");
        echo json_encode($result ?: ['status' => 'error', 'valid' => false]);
        exit;
    }

    if ($action === 'daemon_status') {
        LOGDEB("Daemon status check");
        LOGEND("");
        echo json_encode(['running' => daemon_is_running($pidfile)]);
        exit;
    }

    if ($action === 'daemon_restart') {
        LOGINF("Daemon restart requested");
        if (daemon_is_running($pidfile)) {
            $pid = intval(trim(file_get_contents($pidfile)));
            exec("kill $pid 2>/dev/null");
            LOGINF("Stopped old daemon (PID $pid)");
            sleep(1);
        }
        $daemon = "$pluginbindir/smartmii_daemon.py";
        $logdir = LBPLOGDIR;
        exec("$python $daemon --configdir $pluginconfigdir --logdir $logdir > /dev/null 2>&1 &");
        sleep(2);
        $running = daemon_is_running($pidfile);
        if ($running) { LOGINF("Daemon started"); } else { LOGERR("Daemon failed to start"); }
        LOGEND("");
        echo json_encode(['success' => true, 'running' => $running, 'message' => $L['BASIC.MSG_DAEMON_RESTARTED']]);
        exit;
    }

    if ($action === 'daemon_stop') {
        LOGINF("Daemon stop requested");
        if (daemon_is_running($pidfile)) {
            $pid = intval(trim(file_get_contents($pidfile)));
            exec("kill $pid 2>/dev/null");
            LOGINF("Daemon stopped (PID $pid)");
            sleep(1);
        }
        LOGEND("");
        echo json_encode(['success' => true, 'message' => $L['BASIC.MSG_DAEMON_STOPPED']]);
        exit;
    }

    LOGERR("Unknown AJAX action: $action");
    LOGEND("");
    echo json_encode(['success' => false, 'message' => 'Unknown action']);
    exit;
}

// --- Load config ---
$config = ['mqtt_prefix' => 'smartmii', 'poll_interval' => 30, 'xiaomi_server' => 'de', 'fans' => []];
if (file_exists($configfile)) {
    $loaded = json_decode(file_get_contents($configfile), true);
    if ($loaded !== null) $config = $loaded;
}

$daemon_running = daemon_is_running($pidfile);
$session_exists = file_exists($sessionfile);

$statusProps = ['power', 'fan_level', 'oscillate', 'angle', 'mode', 'buzzer', 'online'];
$cmdProps = ['power', 'fan_level', 'oscillate', 'angle', 'mode', 'buzzer'];

$template_title = $L['BASIC.PLUGIN_TITLE'];
$helplink = "https://github.com/wkulhane/loxone-smartmii";
$helptemplate = "";

LBWeb::lbheader($template_title, $helplink, $helptemplate);
?>

<style>
.smartmii-msg { padding: 10px; margin: 10px 0; border-radius: 4px; display: none; }
.smartmii-msg-ok { background: #d4edda; color: #155724; border: 1px solid #c3e6cb; }
.smartmii-msg-err { background: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }
.smartmii-topics { background: #f0f0f0; padding: 10px; border-radius: 4px; font-family: monospace; font-size: 0.85em; margin: 5px 0; white-space: pre-wrap; word-break: break-all; }
.session-ok { color: green; font-weight: bold; }
.session-err { color: red; font-weight: bold; }
</style>

<div id="smartmii-msg" class="smartmii-msg"></div>

<!-- ========== XIAOMI CLOUD LOGIN ========== -->
<div data-role="collapsible" data-collapsed="<?= $session_exists ? 'true' : 'false' ?>" data-content-theme="a">
    <h3><?= $L['BASIC.H_CLOUD_LOGIN'] ?></h3>

    <p id="session-status">
        <?php if ($session_exists): ?>
            <span class="session-ok"><?= $L['BASIC.LBL_SESSION_VALID'] ?></span>
        <?php else: ?>
            <span class="session-err"><?= $L['BASIC.LBL_SESSION_NONE'] ?></span>
        <?php endif; ?>
    </p>

    <div data-role="fieldcontain">
        <label for="xi-server"><?= $L['BASIC.LBL_XI_SERVER'] ?></label>
        <select id="xi-server" data-mini="true">
            <?php foreach (['cn','de','us','ru','tw','sg','in','i2'] as $s): ?>
                <option value="<?= $s ?>" <?= ($config['xiaomi_server'] ?? 'de') === $s ? 'selected' : '' ?>><?= $s ?></option>
            <?php endforeach; ?>
        </select>
    </div>

    <div data-role="fieldcontain">
        <label for="xi-user"><?= $L['BASIC.LBL_XI_USER'] ?></label>
        <input type="text" id="xi-user" data-mini="true" placeholder="email@example.com">
    </div>

    <div data-role="fieldcontain">
        <label for="xi-pass"><?= $L['BASIC.LBL_XI_PASS'] ?></label>
        <input type="password" id="xi-pass" data-mini="true">
    </div>

    <a href="javascript:void(0)" onclick="cloudLogin()" data-role="button" data-inline="true" data-mini="true" data-icon="lock">
        <?= $L['BASIC.BTN_LOGIN'] ?>
    </a>

    <div id="captcha-area" style="display:none; margin-top: 10px;">
        <p><strong><?= $L['BASIC.LBL_CAPTCHA'] ?></strong></p>
        <img id="captcha-img" style="margin: 5px 0; max-width: 300px;">
        <div data-role="fieldcontain">
            <label for="captcha-code"><?= $L['BASIC.LBL_CAPTCHA_CODE'] ?></label>
            <input type="text" id="captcha-code" data-mini="true">
        </div>
        <a href="javascript:void(0)" onclick="submitCaptcha()" data-role="button" data-inline="true" data-mini="true" data-icon="check">
            <?= $L['BASIC.BTN_SUBMIT'] ?>
        </a>
    </div>

    <div id="twofa-area" style="display:none; margin-top: 10px;">
        <p><strong><?= $L['BASIC.LBL_2FA'] ?></strong></p>
        <div data-role="fieldcontain">
            <label for="twofa-code"><?= $L['BASIC.LBL_2FA_CODE'] ?></label>
            <input type="text" id="twofa-code" data-mini="true">
        </div>
        <a href="javascript:void(0)" onclick="submit2FA()" data-role="button" data-inline="true" data-mini="true" data-icon="check">
            <?= $L['BASIC.BTN_SUBMIT'] ?>
        </a>
    </div>

    <div id="login-status" style="margin-top: 8px;"></div>
</div>

<!-- ========== SETTINGS ========== -->
<div data-role="collapsible" data-collapsed="false" data-content-theme="a">
    <h3><?= $L['BASIC.H_SETTINGS'] ?></h3>

    <div data-role="fieldcontain">
        <label for="mqtt_prefix"><?= $L['BASIC.LBL_MQTT_PREFIX'] ?></label>
        <input type="text" id="mqtt_prefix" data-mini="true"
               value="<?= htmlspecialchars($config['mqtt_prefix']) ?>"
               placeholder="smartmii">
        <p class="hint"><?= $L['BASIC.DESC_MQTT_PREFIX'] ?></p>
    </div>

    <div data-role="fieldcontain">
        <label for="poll_interval"><?= $L['BASIC.LBL_POLL_INTERVAL'] ?></label>
        <input type="number" id="poll_interval" data-mini="true"
               value="<?= intval($config['poll_interval']) ?>"
               min="5" max="300">
        <p class="hint"><?= $L['BASIC.DESC_POLL_INTERVAL'] ?></p>
    </div>

    <a href="javascript:void(0)" onclick="saveSettings()" data-role="button" data-inline="true" data-mini="true" data-icon="check">
        <?= $L['BASIC.BTN_SAVE_SETTINGS'] ?>
    </a>

    <h4><?= $L['BASIC.H_DAEMON'] ?></h4>
    <p>Status:
        <strong id="daemon-status" style="color: <?= $daemon_running ? 'green' : 'red' ?>">
            <?= $daemon_running ? $L['BASIC.LBL_DAEMON_RUNNING'] : $L['BASIC.LBL_DAEMON_STOPPED'] ?>
        </strong>
    </p>
    <div data-role="controlgroup" data-type="horizontal" data-mini="true">
        <a href="javascript:void(0)" onclick="daemonRestart()" data-role="button" data-icon="refresh">
            <?= $L['BASIC.BTN_RESTART'] ?>
        </a>
        <a href="javascript:void(0)" onclick="daemonStop()" data-role="button" data-icon="power">
            <?= $L['BASIC.BTN_STOP'] ?>
        </a>
    </div>
</div>

<!-- ========== FANS ========== -->
<div data-role="collapsible" data-collapsed="false" data-content-theme="a">
    <h3><?= $L['BASIC.H_FANS'] ?></h3>

    <a href="javascript:void(0)" onclick="discoverDevices()" data-role="button" data-inline="true" data-mini="true" data-icon="search">
        <?= $L['BASIC.BTN_DISCOVER'] ?>
    </a>

    <ul data-role="listview" data-inset="true" id="fan-list">
        <li data-role="list-divider"><?= $L['BASIC.H_FANS'] ?></li>
    </ul>

    <a href="javascript:void(0)" onclick="showFanForm(-1)" data-role="button" data-inline="true" data-mini="true" data-icon="plus">
        <?= $L['BASIC.BTN_ADD_FAN'] ?>
    </a>

    <div id="fan-form" style="display:none; margin-top: 15px;">
        <div data-role="collapsible" data-collapsed="false" data-content-theme="a" id="fan-form-collapsible">
            <h3 id="fan-form-title"><?= $L['BASIC.BTN_ADD_FAN'] ?></h3>
            <input type="hidden" id="fan-edit-index" value="-1">

            <div data-role="fieldcontain">
                <label for="fan-name"><?= $L['BASIC.LBL_FAN_NAME'] ?></label>
                <input type="text" id="fan-name" data-mini="true" placeholder="Living Room Fan" oninput="autoSlug()">
            </div>

            <div data-role="fieldcontain">
                <label for="fan-id"><?= $L['BASIC.LBL_FAN_ID'] ?></label>
                <input type="text" id="fan-id" data-mini="true" placeholder="living_room_fan">
                <p class="hint"><?= $L['BASIC.DESC_FAN_ID'] ?></p>
            </div>

            <div data-role="fieldcontain">
                <label for="fan-did"><?= $L['BASIC.LBL_FAN_DID'] ?></label>
                <input type="text" id="fan-did" data-mini="true" placeholder="624024618">
                <p class="hint"><?= $L['BASIC.DESC_FAN_DID'] ?></p>
            </div>

            <div data-role="fieldcontain">
                <label for="fan-enabled"><?= $L['BASIC.LBL_FAN_ENABLED'] ?></label>
                <select id="fan-enabled" data-role="flipswitch" data-mini="true">
                    <option value="0">Off</option>
                    <option value="1" selected>On</option>
                </select>
            </div>

            <div data-role="controlgroup" data-type="horizontal" data-mini="true">
                <a href="javascript:void(0)" onclick="saveFan()" data-role="button" data-icon="check">
                    <?= $L['BASIC.BTN_SAVE'] ?>
                </a>
                <a href="javascript:void(0)" onclick="testConnection()" data-role="button" data-icon="gear">
                    <?= $L['BASIC.BTN_TEST'] ?>
                </a>
                <a href="javascript:void(0)" onclick="hideFanForm()" data-role="button" data-icon="delete">
                    <?= $L['BASIC.BTN_CANCEL'] ?>
                </a>
            </div>
            <div id="test-result" style="margin-top: 8px;"></div>
        </div>
    </div>

    <!-- Discover results popup -->
    <div id="discover-results" style="display:none; margin-top: 15px;">
        <div data-role="collapsible" data-collapsed="false" data-content-theme="a">
            <h3><?= $L['BASIC.H_DISCOVERED'] ?></h3>
            <ul data-role="listview" data-inset="true" id="discover-list"></ul>
        </div>
    </div>
</div>

<!-- ========== MQTT TOPICS ========== -->
<div data-role="collapsible" data-collapsed="true" data-content-theme="a">
    <h3><?= $L['BASIC.H_MQTT_TOPICS'] ?></h3>
    <p><?= $L['BASIC.DESC_MQTT_TOPICS'] ?></p>
    <div id="mqtt-topics-container"></div>

    <div data-role="fieldcontain">
        <label>Download XML-Template for uploading to Loxone:</label>
        <a href="index.php?export=loxone" data-role="button" data-inline="true" data-mini="true">VI_MQTT_UDP_Smartmii.xml</a>
    </div>
</div>

<script>
var _csrfToken = <?= json_encode($_SESSION['csrf_token']) ?>;
$.ajaxSetup({
    beforeSend: function(xhr, settings) {
        var method = (settings.method || settings.type || '').toUpperCase();
        if (method === 'POST') {
            var token = 'csrf_token=' + encodeURIComponent(_csrfToken);
            settings.data = settings.data ? settings.data + '&' + token : token;
        }
    }
});

var config = <?= json_encode($config) ?>;
var L = {
    confirm_delete: <?= json_encode($L['BASIC.MSG_CONFIRM_DELETE']) ?>,
    invalid_did: <?= json_encode($L['BASIC.MSG_INVALID_DID']) ?>,
    invalid_id: <?= json_encode($L['BASIC.MSG_INVALID_ID']) ?>,
    edit: <?= json_encode($L['BASIC.BTN_EDIT']) ?>,
    delete_btn: <?= json_encode($L['BASIC.BTN_DELETE']) ?>,
    add_fan: <?= json_encode($L['BASIC.BTN_ADD_FAN']) ?>,
    no_fans: "No fans configured yet.",
    status_topics: <?= json_encode($L['BASIC.LBL_STATUS_TOPICS']) ?>,
    cmd_topics: <?= json_encode($L['BASIC.LBL_CMD_TOPICS']) ?>,
    daemon_running: <?= json_encode($L['BASIC.LBL_DAEMON_RUNNING']) ?>,
    daemon_stopped: <?= json_encode($L['BASIC.LBL_DAEMON_STOPPED']) ?>,
    session_valid: <?= json_encode($L['BASIC.LBL_SESSION_VALID']) ?>,
    session_none: <?= json_encode($L['BASIC.LBL_SESSION_NONE']) ?>
};

var statusProps = <?= json_encode($statusProps) ?>;
var cmdProps = <?= json_encode($cmdProps) ?>;

// ponytail: login state for multi-step flow
var loginState = {};

function showMessage(text, isError) {
    var el = document.getElementById('smartmii-msg');
    el.textContent = text;
    el.className = 'smartmii-msg ' + (isError ? 'smartmii-msg-err' : 'smartmii-msg-ok');
    el.style.display = 'block';
    setTimeout(function() { el.style.display = 'none'; }, 5000);
}

function escHtml(str) {
    var div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

// --- Cloud Login ---

function cloudLogin() {
    loginState = {
        username: $('#xi-user').val().trim(),
        password: $('#xi-pass').val(),
        server: $('#xi-server').val()
    };
    $('#login-status').html('<p>Logging in...</p>');
    $('#captcha-area').hide();
    $('#twofa-area').hide();

    $.ajax({
        url: 'index.php', method: 'POST',
        data: { ajax: 'cloud_login', username: loginState.username, password: loginState.password, server: loginState.server },
        dataType: 'json',
        success: function(resp) {
            handleLoginResponse(resp);
        },
        error: function() { $('#login-status').html('<p style="color:red">Request failed</p>'); }
    });
}

function handleLoginResponse(resp) {
    if (resp.status === 'ok') {
        $('#login-status').html('<p style="color:green"><strong>Login successful!</strong></p>');
        $('#captcha-area').hide();
        $('#twofa-area').hide();
        $('#session-status').html('<span class="session-ok">' + L.session_valid + '</span>');
        config.xiaomi_server = loginState.server;
        saveConfig();
    } else if (resp.status === 'captcha') {
        $('#captcha-img').attr('src', 'data:image/jpeg;base64,' + resp.image);
        $('#captcha-area').show();
        $('#captcha-code').val('');
        $('#login-status').html('<p>Please enter the captcha code.</p>');
    } else if (resp.status === '2fa') {
        $('#twofa-area').show();
        $('#twofa-code').val('');
        $('#login-status').html('<p>Please enter the 2FA code from your email.</p>');
    } else {
        $('#login-status').html('<p style="color:red">' + escHtml(resp.message || 'Login failed') + '</p>');
    }
}

function submitCaptcha() {
    var code = $('#captcha-code').val().trim();
    $('#login-status').html('<p>Submitting captcha...</p>');

    $.ajax({
        url: 'index.php', method: 'POST',
        data: { ajax: 'cloud_captcha', code: code },
        dataType: 'json',
        success: function(resp) { handleLoginResponse(resp); },
        error: function() { $('#login-status').html('<p style="color:red">Request failed</p>'); }
    });
}

function submit2FA() {
    var code = $('#twofa-code').val().trim();
    $('#login-status').html('<p>Submitting 2FA code...</p>');

    $.ajax({
        url: 'index.php', method: 'POST',
        data: { ajax: 'cloud_2fa', code: code },
        dataType: 'json',
        success: function(resp) { handleLoginResponse(resp); },
        error: function() { $('#login-status').html('<p style="color:red">Request failed</p>'); }
    });
}

// --- Device Discovery ---

function discoverDevices() {
    showMessage('Discovering devices...', false);
    $.ajax({
        url: 'index.php', method: 'POST',
        data: { ajax: 'cloud_discover', server: config.xiaomi_server || 'de' },
        dataType: 'json',
        success: function(resp) {
            if (resp.status === 'ok' && resp.devices) {
                var list = $('#discover-list');
                list.empty();
                if (resp.devices.length === 0) {
                    list.append('<li>No devices found.</li>');
                } else {
                    for (var i = 0; i < resp.devices.length; i++) {
                        var d = resp.devices[i];
                        list.append(
                            '<li><a href="javascript:void(0)">' +
                            '<h3>' + escHtml(d.name) + '</h3>' +
                            '<p>DID: <strong>' + escHtml(d.did) + '</strong> Model: ' + escHtml(d.model) + '</p>' +
                            '</a>' +
                            '<a href="javascript:void(0)" onclick="addDiscoveredFan(' + i + ')" data-icon="plus">Add</a>' +
                            '</li>'
                        );
                    }
                }
                list.listview('refresh');
                window._discoveredDevices = resp.devices;
                $('#discover-results').show();
            } else {
                showMessage(resp.message || 'Discovery failed', true);
            }
        },
        error: function() { showMessage('Discovery request failed', true); }
    });
}

function addDiscoveredFan(index) {
    var d = window._discoveredDevices[index];
    var slug = slugify(d.name);
    config.fans.push({ id: slug, name: d.name, did: d.did, enabled: true });
    saveConfig(function() { renderFanList(); });
}

// --- Fan List ---

function renderFanList() {
    var list = $('#fan-list');
    list.find('li:not([data-role="list-divider"])').remove();

    if (config.fans.length === 0) {
        list.append('<li><em>' + L.no_fans + '</em></li>');
    } else {
        for (var i = 0; i < config.fans.length; i++) {
            var fan = config.fans[i];
            var enabled = fan.enabled ? '<span style="color:green">&#9679;</span>' : '<span style="color:gray">&#9679;</span>';
            var li = '<li>' +
                '<a href="javascript:void(0)" onclick="showFanForm(' + i + ')">' +
                    '<h3>' + escHtml(fan.name) + ' ' + enabled + '</h3>' +
                    '<p>ID: <strong>' + escHtml(fan.id) + '</strong> &nbsp; DID: <strong>' + escHtml(fan.did) + '</strong></p>' +
                '</a>' +
                '<a href="javascript:void(0)" onclick="deleteFan(' + i + ')" data-icon="delete" data-theme="a">' +
                    L.delete_btn +
                '</a>' +
            '</li>';
            list.append(li);
        }
    }
    list.listview('refresh');
    renderMqttTopics();
}

function renderMqttTopics() {
    var container = document.getElementById('mqtt-topics-container');
    container.innerHTML = '';
    var prefix = config.mqtt_prefix || 'smartmii';

    if (config.fans.length === 0) {
        container.innerHTML = '<p><em>' + L.no_fans + '</em></p>';
        return;
    }

    for (var i = 0; i < config.fans.length; i++) {
        var fan = config.fans[i];
        var html = '<h4>' + escHtml(fan.name) + ' (' + escHtml(fan.id) + ')</h4>';
        html += '<p><strong>' + L.status_topics + ':</strong></p><div class="smartmii-topics">';
        for (var j = 0; j < statusProps.length; j++) html += prefix + '/' + fan.id + '/status/' + statusProps[j] + '\n';
        html += '</div>';
        html += '<p><strong>' + L.cmd_topics + ':</strong></p><div class="smartmii-topics">';
        for (var j = 0; j < cmdProps.length; j++) html += prefix + '/' + fan.id + '/cmd/' + cmdProps[j] + '\n';
        html += '</div>';
        container.innerHTML += html;
    }
}

function slugify(name) {
    var map = {'ä':'ae','ö':'oe','ü':'ue','ß':'ss'};
    var s = name.toLowerCase().replace(/[äöüß]/g, function(c) { return map[c]; });
    return s.replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, '');
}

function autoSlug() {
    var editIndex = parseInt(document.getElementById('fan-edit-index').value);
    if (editIndex >= 0) return;
    $('#fan-id').val(slugify(document.getElementById('fan-name').value)).trigger('change');
}

function showFanForm(index) {
    $('#fan-form').show();
    document.getElementById('fan-edit-index').value = index;
    document.getElementById('test-result').innerHTML = '';

    if (index >= 0 && index < config.fans.length) {
        var fan = config.fans[index];
        $('#fan-form-title').text(L.edit + ': ' + fan.name);
        $('#fan-name').val(fan.name);
        $('#fan-id').val(fan.id);
        $('#fan-did').val(fan.did);
        $('#fan-enabled').val(fan.enabled ? '1' : '0').flipswitch('refresh');
    } else {
        $('#fan-form-title').text(L.add_fan);
        $('#fan-name').val('');
        $('#fan-id').val('');
        $('#fan-did').val('');
        $('#fan-enabled').val('1').flipswitch('refresh');
    }
}

function hideFanForm() { $('#fan-form').hide(); }

function validateFanForm() {
    var did = $('#fan-did').val().trim();
    var id = $('#fan-id').val().trim();
    if (!/^\d+$/.test(did)) { showMessage(L.invalid_did, true); return false; }
    if (!/^[a-z0-9_]+$/.test(id)) { showMessage(L.invalid_id, true); return false; }
    return true;
}

function saveFan() {
    if (!validateFanForm()) return;
    var index = parseInt(document.getElementById('fan-edit-index').value);
    var fan = {
        id: $('#fan-id').val().trim(),
        name: $('#fan-name').val().trim(),
        did: $('#fan-did').val().trim(),
        enabled: $('#fan-enabled').val() === '1'
    };
    if (index >= 0 && index < config.fans.length) {
        config.fans[index] = fan;
    } else {
        config.fans.push(fan);
    }
    saveConfig(function() { hideFanForm(); renderFanList(); });
}

function deleteFan(index) {
    if (!confirm(L.confirm_delete)) return;
    config.fans.splice(index, 1);
    saveConfig(function() { renderFanList(); });
}

function saveSettings() {
    config.mqtt_prefix = $('#mqtt_prefix').val().trim();
    config.poll_interval = parseInt($('#poll_interval').val()) || 30;
    config.xiaomi_server = $('#xi-server').val();
    saveConfig(function() { renderMqttTopics(); });
}

function saveConfig(callback) {
    $.ajax({
        url: 'index.php', method: 'POST',
        data: { ajax: 'save_config', config: JSON.stringify(config) },
        dataType: 'json',
        success: function(resp) {
            showMessage(resp.message, !resp.success);
            if (callback) callback();
        },
        error: function() { showMessage('Error', true); }
    });
}

function testConnection() {
    var did = $('#fan-did').val().trim();
    var el = document.getElementById('test-result');
    el.innerHTML = '<p>Testing...</p>';
    $.ajax({
        url: 'index.php', method: 'POST',
        data: { ajax: 'test_connection', did: did, server: config.xiaomi_server || 'de' },
        dataType: 'json',
        success: function(resp) {
            var color = resp.success ? 'green' : 'red';
            el.innerHTML = '<p style="color:' + color + '"><strong>' + escHtml(resp.message) + '</strong></p>';
        },
        error: function() { el.innerHTML = '<p style="color:red"><strong>Error</strong></p>'; }
    });
}

function daemonRestart() {
    $.ajax({
        url: 'index.php', method: 'POST', data: { ajax: 'daemon_restart' }, dataType: 'json',
        success: function(resp) { showMessage(resp.message, !resp.success); updateDaemonStatus(resp.running); }
    });
}

function daemonStop() {
    $.ajax({
        url: 'index.php', method: 'POST', data: { ajax: 'daemon_stop' }, dataType: 'json',
        success: function(resp) { showMessage(resp.message, !resp.success); updateDaemonStatus(false); }
    });
}

function updateDaemonStatus(running) {
    var el = document.getElementById('daemon-status');
    el.style.color = running ? 'green' : 'red';
    el.textContent = running ? L.daemon_running : L.daemon_stopped;
}

$(document).ready(function() { renderFanList(); });
</script>

<?php
LBWeb::lbfooter();
?>
