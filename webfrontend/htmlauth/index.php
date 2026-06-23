<?php
require_once "loxberry_system.php";
require_once "loxberry_web.php";

$L = LBSystem::readlanguage("language.ini");

$pluginconfigdir = LBPCONFIGDIR;
$pluginbindir = LBPBINDIR;
$plugindatadir = LBPDATADIR;
$configfile = "$pluginconfigdir/smartmii.json";
$pidfile = "/run/shm/smartmii.pid";
$python = "$plugindatadir/venv/bin/python3";

function daemon_is_running($pidfile) {
    if (!file_exists($pidfile)) return false;
    $pid = trim(file_get_contents($pidfile));
    return $pid && file_exists("/proc/$pid");
}

// --- AJAX handlers ---
if (isset($_POST['ajax'])) {
    header('Content-Type: application/json');
    $action = $_POST['ajax'];

    if ($action === 'save_config') {
        $config = json_decode($_POST['config'], true);
        if ($config === null) {
            echo json_encode(['success' => false, 'message' => $L['BASIC.MSG_SAVE_ERROR']]);
            exit;
        }
        $config['poll_interval'] = max(5, min(300, intval($config['poll_interval'])));
        $config['mqtt_prefix'] = preg_replace('/[^a-zA-Z0-9_\-]/', '', $config['mqtt_prefix']);
        if (empty($config['mqtt_prefix'])) {
            $config['mqtt_prefix'] = 'smartmii';
        }
        if (!isset($config['fans'])) {
            $config['fans'] = [];
        }
        $result = file_put_contents($configfile, json_encode($config, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES));
        if ($result !== false) {
            echo json_encode(['success' => true, 'message' => $L['BASIC.MSG_SAVED']]);
        } else {
            echo json_encode(['success' => false, 'message' => $L['BASIC.MSG_SAVE_ERROR']]);
        }
        exit;
    }

    if ($action === 'test_connection') {
        $raw_ip = $_POST['ip'] ?? '';
        $raw_token = $_POST['token'] ?? '';
        if (!preg_match('/^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$/', $raw_ip)) {
            echo json_encode(['success' => false, 'message' => $L['BASIC.MSG_INVALID_IP']]);
            exit;
        }
        if (!preg_match('/^[a-f0-9]{32}$/i', $raw_token)) {
            echo json_encode(['success' => false, 'message' => $L['BASIC.MSG_INVALID_TOKEN']]);
            exit;
        }
        $ip = escapeshellarg($raw_ip);
        $token = escapeshellarg($raw_token);
        $cmd = "$python -c \"from miio.integrations.fan.zhimi.zhimi_miot import FanZA5; f = FanZA5($ip, $token); print(f.status())\" 2>&1";
        $output = shell_exec($cmd);
        if ($output !== null && strpos($output, 'Error') === false && strpos($output, 'Traceback') === false) {
            echo json_encode(['success' => true, 'message' => $L['BASIC.MSG_TEST_OK'], 'output' => $output]);
        } else {
            echo json_encode(['success' => false, 'message' => $L['BASIC.MSG_TEST_FAIL'], 'output' => $output]);
        }
        exit;
    }

    if ($action === 'daemon_status') {
        echo json_encode(['running' => daemon_is_running($pidfile)]);
        exit;
    }

    if ($action === 'daemon_restart') {
        if (daemon_is_running($pidfile)) {
            $pid = trim(file_get_contents($pidfile));
            exec("kill $pid 2>/dev/null");
            sleep(1);
        }
        $daemon = "$pluginbindir/smartmii_daemon.py";
        $logdir = LBPLOGDIR;
        exec("$python $daemon --configdir $pluginconfigdir --logdir $logdir > /dev/null 2>&1 &");
        sleep(2);
        echo json_encode(['success' => true, 'running' => daemon_is_running($pidfile), 'message' => $L['BASIC.MSG_DAEMON_RESTARTED']]);
        exit;
    }

    if ($action === 'daemon_stop') {
        if (daemon_is_running($pidfile)) {
            $pid = trim(file_get_contents($pidfile));
            exec("kill $pid 2>/dev/null");
            sleep(1);
        }
        echo json_encode(['success' => true, 'message' => $L['BASIC.MSG_DAEMON_STOPPED']]);
        exit;
    }

    echo json_encode(['success' => false, 'message' => 'Unknown action']);
    exit;
}

// --- Load config ---
$config = ['mqtt_prefix' => 'smartmii', 'poll_interval' => 30, 'fans' => []];
if (file_exists($configfile)) {
    $loaded = json_decode(file_get_contents($configfile), true);
    if ($loaded !== null) {
        $config = $loaded;
    }
}

// --- Daemon status ---
$daemon_running = daemon_is_running($pidfile);

$statusProps = ['power', 'speed', 'fan_level', 'mode', 'oscillate', 'angle',
    'buzzer', 'child_lock', 'led_brightness', 'temperature', 'humidity',
    'delay_off', 'ionizer', 'speed_rpm', 'online'];
$cmdProps = ['power', 'speed', 'fan_level', 'mode', 'oscillate', 'angle',
    'buzzer', 'child_lock', 'led_brightness', 'ionizer', 'delay_off'];

// --- Page output ---
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
</style>

<div id="smartmii-msg" class="smartmii-msg"></div>

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

<!-- ========== TOKEN HELP ========== -->
<div data-role="collapsible" data-collapsed="true" data-content-theme="a">
    <h3><?= $L['BASIC.H_TOKEN_HELP'] ?></h3>
    <p><?= $L['BASIC.DESC_TOKEN_HELP'] ?></p>
    <a href="<?= $L['BASIC.LINK_TOKEN_EXTRACTOR'] ?>" target="_blank"
       data-role="button" data-inline="true" data-mini="true" data-icon="info">
        <?= $L['BASIC.LBL_TOKEN_LINK'] ?>
    </a>
</div>

<!-- ========== FANS ========== -->
<div data-role="collapsible" data-collapsed="false" data-content-theme="a">
    <h3><?= $L['BASIC.H_FANS'] ?></h3>

    <ul data-role="listview" data-inset="true" id="fan-list">
        <li data-role="list-divider"><?= $L['BASIC.H_FANS'] ?></li>
    </ul>

    <a href="javascript:void(0)" onclick="showFanForm(-1)" data-role="button" data-inline="true" data-mini="true" data-icon="plus">
        <?= $L['BASIC.BTN_ADD_FAN'] ?>
    </a>

    <!-- Fan Edit Form (hidden by default) -->
    <div id="fan-form" style="display:none; margin-top: 15px;">
        <div data-role="collapsible" data-collapsed="false" data-content-theme="a" id="fan-form-collapsible">
            <h3 id="fan-form-title"><?= $L['BASIC.BTN_ADD_FAN'] ?></h3>
            <input type="hidden" id="fan-edit-index" value="-1">

            <div data-role="fieldcontain">
                <label for="fan-name"><?= $L['BASIC.LBL_FAN_NAME'] ?></label>
                <input type="text" id="fan-name" data-mini="true"
                       placeholder="Living Room Fan" oninput="autoSlug()">
            </div>

            <div data-role="fieldcontain">
                <label for="fan-id"><?= $L['BASIC.LBL_FAN_ID'] ?></label>
                <input type="text" id="fan-id" data-mini="true"
                       placeholder="living_room_fan">
                <p class="hint"><?= $L['BASIC.DESC_FAN_ID'] ?></p>
            </div>

            <div data-role="fieldcontain">
                <label for="fan-ip"><?= $L['BASIC.LBL_FAN_IP'] ?></label>
                <input type="text" id="fan-ip" data-mini="true"
                       placeholder="192.168.1.50">
            </div>

            <div data-role="fieldcontain">
                <label for="fan-token"><?= $L['BASIC.LBL_FAN_TOKEN'] ?></label>
                <input type="text" id="fan-token" data-mini="true"
                       placeholder="abcdef1234567890abcdef1234567890" maxlength="32">
                <p class="hint"><?= $L['BASIC.DESC_FAN_TOKEN'] ?></p>
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
</div>

<!-- ========== MQTT TOPICS ========== -->
<div data-role="collapsible" data-collapsed="true" data-content-theme="a">
    <h3><?= $L['BASIC.H_MQTT_TOPICS'] ?></h3>
    <p><?= $L['BASIC.DESC_MQTT_TOPICS'] ?></p>
    <div id="mqtt-topics-container"></div>
</div>

<script>
var config = <?= json_encode($config) ?>;
var L = {
    confirm_delete: <?= json_encode($L['BASIC.MSG_CONFIRM_DELETE']) ?>,
    invalid_ip: <?= json_encode($L['BASIC.MSG_INVALID_IP']) ?>,
    invalid_token: <?= json_encode($L['BASIC.MSG_INVALID_TOKEN']) ?>,
    invalid_id: <?= json_encode($L['BASIC.MSG_INVALID_ID']) ?>,
    edit: <?= json_encode($L['BASIC.BTN_EDIT']) ?>,
    delete_btn: <?= json_encode($L['BASIC.BTN_DELETE']) ?>,
    add_fan: <?= json_encode($L['BASIC.BTN_ADD_FAN']) ?>,
    no_fans: "No fans configured yet.",
    status_topics: <?= json_encode($L['BASIC.LBL_STATUS_TOPICS']) ?>,
    cmd_topics: <?= json_encode($L['BASIC.LBL_CMD_TOPICS']) ?>,
    daemon_running: <?= json_encode($L['BASIC.LBL_DAEMON_RUNNING']) ?>,
    daemon_stopped: <?= json_encode($L['BASIC.LBL_DAEMON_STOPPED']) ?>
};

var statusProps = <?= json_encode($statusProps) ?>;
var cmdProps = <?= json_encode($cmdProps) ?>;

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
                    '<p>ID: <strong>' + escHtml(fan.id) + '</strong> &nbsp; IP: <strong>' + escHtml(fan.ip) + '</strong></p>' +
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

        html += '<p><strong>' + L.status_topics + ':</strong></p>';
        html += '<div class="smartmii-topics">';
        for (var j = 0; j < statusProps.length; j++) {
            html += prefix + '/' + fan.id + '/status/' + statusProps[j] + '\n';
        }
        html += '</div>';

        html += '<p><strong>' + L.cmd_topics + ':</strong></p>';
        html += '<div class="smartmii-topics">';
        for (var j = 0; j < cmdProps.length; j++) {
            html += prefix + '/' + fan.id + '/cmd/' + cmdProps[j] + '\n';
        }
        html += '</div>';

        container.innerHTML += html;
    }
}

function autoSlug() {
    var editIndex = parseInt(document.getElementById('fan-edit-index').value);
    if (editIndex >= 0) return;
    var name = document.getElementById('fan-name').value;
    var slug = name.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, '');
    $('#fan-id').val(slug).trigger('change');
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
        $('#fan-ip').val(fan.ip);
        $('#fan-token').val(fan.token);
        $('#fan-enabled').val(fan.enabled ? '1' : '0').flipswitch('refresh');
    } else {
        $('#fan-form-title').text(L.add_fan);
        $('#fan-name').val('');
        $('#fan-id').val('');
        $('#fan-ip').val('');
        $('#fan-token').val('');
        $('#fan-enabled').val('1').flipswitch('refresh');
    }
}

function hideFanForm() {
    $('#fan-form').hide();
}

function validateFanForm() {
    var ip = $('#fan-ip').val().trim();
    var token = $('#fan-token').val().trim();
    var id = $('#fan-id').val().trim();

    if (!/^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(ip)) {
        showMessage(L.invalid_ip, true);
        return false;
    }
    if (!/^[a-f0-9]{32}$/i.test(token)) {
        showMessage(L.invalid_token, true);
        return false;
    }
    if (!/^[a-z0-9_]+$/.test(id)) {
        showMessage(L.invalid_id, true);
        return false;
    }
    return true;
}

function saveFan() {
    if (!validateFanForm()) return;

    var index = parseInt(document.getElementById('fan-edit-index').value);
    var fan = {
        id: $('#fan-id').val().trim(),
        name: $('#fan-name').val().trim(),
        ip: $('#fan-ip').val().trim(),
        token: $('#fan-token').val().trim(),
        enabled: $('#fan-enabled').val() === '1'
    };

    if (index >= 0 && index < config.fans.length) {
        config.fans[index] = fan;
    } else {
        config.fans.push(fan);
    }

    saveConfig(function() {
        hideFanForm();
        renderFanList();
    });
}

function deleteFan(index) {
    if (!confirm(L.confirm_delete)) return;
    config.fans.splice(index, 1);
    saveConfig(function() {
        renderFanList();
    });
}

function saveSettings() {
    config.mqtt_prefix = $('#mqtt_prefix').val().trim();
    config.poll_interval = parseInt($('#poll_interval').val()) || 30;
    saveConfig(function() {
        renderMqttTopics();
    });
}

function saveConfig(callback) {
    $.ajax({
        url: 'index.php',
        method: 'POST',
        data: { ajax: 'save_config', config: JSON.stringify(config) },
        dataType: 'json',
        success: function(resp) {
            showMessage(resp.message, !resp.success);
            if (callback) callback();
        },
        error: function() {
            showMessage('Error', true);
        }
    });
}

function testConnection() {
    var ip = $('#fan-ip').val().trim();
    var token = $('#fan-token').val().trim();
    var el = document.getElementById('test-result');
    el.innerHTML = '<p>Testing...</p>';

    $.ajax({
        url: 'index.php',
        method: 'POST',
        data: { ajax: 'test_connection', ip: ip, token: token },
        dataType: 'json',
        success: function(resp) {
            var color = resp.success ? 'green' : 'red';
            el.innerHTML = '<p style="color:' + color + '"><strong>' + escHtml(resp.message) + '</strong></p>' +
                (resp.output ? '<pre style="font-size:0.8em; overflow-x:auto;">' + escHtml(resp.output) + '</pre>' : '');
        },
        error: function() {
            el.innerHTML = '<p style="color:red"><strong>Error</strong></p>';
        }
    });
}

function daemonRestart() {
    $.ajax({
        url: 'index.php',
        method: 'POST',
        data: { ajax: 'daemon_restart' },
        dataType: 'json',
        success: function(resp) {
            showMessage(resp.message, !resp.success);
            updateDaemonStatus(resp.running);
        }
    });
}

function daemonStop() {
    $.ajax({
        url: 'index.php',
        method: 'POST',
        data: { ajax: 'daemon_stop' },
        dataType: 'json',
        success: function(resp) {
            showMessage(resp.message, !resp.success);
            updateDaemonStatus(false);
        }
    });
}

function updateDaemonStatus(running) {
    var el = document.getElementById('daemon-status');
    el.style.color = running ? 'green' : 'red';
    el.textContent = running ? L.daemon_running : L.daemon_stopped;
}

$(document).ready(function() {
    renderFanList();
});
</script>

<?php
LBWeb::lbfooter();
?>
