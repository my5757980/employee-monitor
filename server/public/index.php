<?php
// Employee Monitor - the one entry point. Routes the agent's API, the admin's
// API, and serves the dashboard. Deploys anywhere PHP + MySQL run; docroot = this
// folder (public/).

require __DIR__ . '/../src/db.php';
require __DIR__ . '/../src/http.php';

$cfg    = em_config();
$method = $_SERVER['REQUEST_METHOD'];
$path   = parse_url($_SERVER['REQUEST_URI'], PHP_URL_PATH);
$path   = rtrim($path, '/');
if ($path === '') {
    $path = '/';
}

// ------- CORS for the agent (it may POST from any employee PC) -------
if (strpos($path, '/api/agent') === 0) {
    header('Access-Control-Allow-Origin: *');
    header('Access-Control-Allow-Headers: Authorization, Content-Type, X-Device-Token');
    header('Access-Control-Allow-Methods: POST, GET, OPTIONS');
    if ($method === 'OPTIONS') {
        http_response_code(204);
        exit;
    }
}

try {
    route($path, $method, $cfg);
} catch (Throwable $e) {
    error_log('[empmon] ' . $e->getMessage());
    fail('Server error.', 500);
}

function route(string $path, string $method, array $cfg): void
{
    // ============ AGENT API (employee computers) ============
    if ($path === '/api/agent/enroll' && $method === 'POST') {
        $b   = body();
        $key = strtoupper(trim($b['enroll_key'] ?? ''));
        $row = q1('SELECT * FROM enroll_keys WHERE enroll_key = ?', [$key]);
        if (!$row) {
            fail('Invalid enrollment key.', 403);
        }
        $deviceToken = token();
        $id = x(
            'INSERT INTO devices (device_token, hostname, employee_name, os, agent_version, last_seen)
             VALUES (?,?,?,?,?, NOW())',
            [
                $deviceToken,
                mb_substr($b['hostname'] ?? '', 0, 190),
                mb_substr($b['employee_name'] ?? ($row['label'] ?: ''), 0, 190),
                mb_substr($b['os'] ?? '', 0, 190),
                mb_substr($b['agent_version'] ?? '', 0, 32),
            ]
        );
        x('UPDATE enroll_keys SET used_by = ? WHERE id = ?', [$id, $row['id']]);
        log_event($id, 'enrolled', $b['hostname'] ?? '');
        json_out([
            'ok'             => true,
            'device_token'   => $deviceToken,
            'device_id'      => $id,
            'sample_seconds' => $cfg['sample_seconds'],
            'consent_notice' => $cfg['consent_notice'],
        ]);
    }

    if ($path === '/api/agent/ping' && $method === 'POST') {
        $dev = require_device();
        $b   = body();
        x('UPDATE devices SET last_seen = NOW(), consent_ack = GREATEST(consent_ack, ?) WHERE id = ?',
            [isset($b['consent_ack']) ? (int) $b['consent_ack'] : 0, $dev['id']]);

        // Hand over any pending actions, once (mark them 'sent' so they don't repeat).
        $cmds = q('SELECT id, type, payload FROM commands WHERE device_id = ? AND status = ? ORDER BY id', [$dev['id'], 'pending']);
        if ($cmds) {
            $ids = implode(',', array_map(fn ($c) => (int) $c['id'], $cmds));
            db()->exec("UPDATE commands SET status = 'sent' WHERE id IN ($ids)");
        }
        $pol = q1('SELECT blocked_sites, blocked_apps FROM policies WHERE device_id = ?', [$dev['id']]);

        json_out([
            'ok'             => true,
            'sample_seconds' => $cfg['sample_seconds'],
            'consent_notice' => $cfg['consent_notice'],
            'commands'       => $cmds,
            'policy'         => [
                'blocked_sites' => lines($pol['blocked_sites'] ?? ''),
                'blocked_apps'  => lines($pol['blocked_apps'] ?? ''),
            ],
        ]);
    }

    if ($path === '/api/agent/command-result' && $method === 'POST') {
        $dev = require_device();
        $b   = body();
        $id  = (int) ($b['id'] ?? 0);
        $ok  = !empty($b['ok']);
        x('UPDATE commands SET status = ?, result = ?, done_at = NOW() WHERE id = ? AND device_id = ?',
            [$ok ? 'done' : 'failed', mb_substr($b['result'] ?? '', 0, 255), $id, $dev['id']]);
        json_out(['ok' => true]);
    }

    if ($path === '/api/agent/activity' && $method === 'POST') {
        $dev     = require_device();
        $b       = body();
        $samples = $b['samples'] ?? [$b];     // one sample, or a batch
        $n = 0;
        foreach ($samples as $s) {
            if (!is_array($s)) {
                continue;
            }
            $ts = isset($s['ts']) ? date('Y-m-d H:i:s', (int) $s['ts']) : date('Y-m-d H:i:s');
            x('INSERT INTO activity (device_id, ts, active_app, window_title, website, is_idle)
               VALUES (?,?,?,?,?,?)',
                [
                    $dev['id'], $ts,
                    mb_substr($s['active_app'] ?? '', 0, 190),
                    mb_substr($s['window_title'] ?? '', 0, 512),
                    mb_substr($s['website'] ?? '', 0, 255),
                    !empty($s['is_idle']) ? 1 : 0,
                ]);
            $n++;
        }
        x('UPDATE devices SET last_seen = NOW() WHERE id = ?', [$dev['id']]);
        json_out(['ok' => true, 'saved' => $n]);
    }

    if ($path === '/api/agent/screenshot' && $method === 'POST') {
        $dev = require_device();
        if (empty($_FILES['shot']['tmp_name']) || !is_uploaded_file($_FILES['shot']['tmp_name'])) {
            fail('No image.', 400);
        }
        $cfg = em_config();
        $dir = $cfg['data_dir'] . '/shots/' . $dev['id'];
        if (!is_dir($dir)) {
            mkdir($dir, 0775, true);
        }
        $ts   = isset($_POST['ts']) ? (int) $_POST['ts'] : time();
        $name = date('Ymd_His', $ts) . '_' . substr(token(3), 0, 6) . '.jpg';
        $full = $dir . '/' . $name;
        if (!move_uploaded_file($_FILES['shot']['tmp_name'], $full)) {
            fail('Could not store image.', 500);
        }
        x('INSERT INTO screenshots (device_id, ts, path, w, h) VALUES (?,?,?,?,?)',
            [$dev['id'], date('Y-m-d H:i:s', $ts), $dev['id'] . '/' . $name,
             (int) ($_POST['w'] ?? 0), (int) ($_POST['h'] ?? 0)]);
        x('UPDATE devices SET last_seen = NOW() WHERE id = ?', [$dev['id']]);
        json_out(['ok' => true]);
    }

    // ============ ADMIN API (the dashboard) ============
    if ($path === '/api/admin/login' && $method === 'POST') {
        $b    = body();
        $user = trim($b['username'] ?? '');
        $a    = q1('SELECT * FROM admins WHERE username = ?', [$user]);
        if (!$a || !password_verify($b['password'] ?? '', $a['password_hash'])) {
            log_event(null, 'admin_login_fail', $user);
            fail('Wrong username or password.', 401);
        }
        $tok = token();
        x('INSERT INTO admin_sessions (admin_id, token) VALUES (?,?)', [$a['id'], $tok]);
        $secure = (!empty($_SERVER['HTTPS']) && $_SERVER['HTTPS'] !== 'off');
        setcookie('em_admin', $tok, [
            'expires'  => time() + 86400 * 30,
            'path'     => '/',
            'httponly' => true,
            'secure'   => $secure,
            'samesite' => 'Lax',
        ]);
        log_event(null, 'admin_login', $user);
        json_out(['ok' => true, 'name' => $a['name'] ?: $a['username'], 'role' => $a['role']]);
    }

    if ($path === '/api/admin/logout' && $method === 'POST') {
        if (!empty($_COOKIE['em_admin'])) {
            x('DELETE FROM admin_sessions WHERE token = ?', [$_COOKIE['em_admin']]);
            setcookie('em_admin', '', ['expires' => time() - 3600, 'path' => '/']);
        }
        json_out(['ok' => true]);
    }

    if ($path === '/api/admin/me' && $method === 'GET') {
        $a = require_admin();
        json_out(['ok' => true, 'name' => $a['name'] ?: $a['username'], 'role' => $a['role']]);
    }

    if ($path === '/api/admin/devices' && $method === 'GET') {
        require_admin();
        $rows = q(
            "SELECT d.id, d.hostname, d.employee_name, d.os, d.agent_version,
                    d.consent_ack, d.last_seen,
                    (UNIX_TIMESTAMP() - UNIX_TIMESTAMP(d.last_seen)) AS ago,
                    (SELECT path FROM screenshots s WHERE s.device_id = d.id ORDER BY s.ts DESC LIMIT 1) AS last_shot,
                    (SELECT CONCAT(active_app,'|',window_title,'|',is_idle) FROM activity a
                        WHERE a.device_id = d.id ORDER BY a.ts DESC LIMIT 1) AS last_act
             FROM devices d WHERE d.active = 1 ORDER BY d.last_seen DESC"
        );
        foreach ($rows as &$r) {
            $r['online'] = ($r['ago'] !== null && $r['ago'] < 180);
            [$app, $title, $idle] = array_pad(explode('|', (string) $r['last_act'], 3), 3, '');
            $r['active_app']   = $app;
            $r['window_title'] = $title;
            $r['is_idle']      = $idle === '1';
            unset($r['last_act']);
        }
        json_out(['ok' => true, 'devices' => $rows, 'sample_seconds' => $cfg['sample_seconds']]);
    }

    if ($path === '/api/admin/device' && $method === 'GET') {
        require_admin();
        $id  = (int) ($_GET['id'] ?? 0);
        $dev = q1('SELECT id, hostname, employee_name, os, agent_version, consent_ack, last_seen FROM devices WHERE id = ?', [$id]);
        if (!$dev) {
            fail('Not found.', 404);
        }
        $dev['activity']    = q('SELECT ts, active_app, window_title, website, is_idle FROM activity WHERE device_id = ? ORDER BY ts DESC LIMIT 100', [$id]);
        $dev['screenshots'] = q('SELECT id, ts, path FROM screenshots WHERE device_id = ? ORDER BY ts DESC LIMIT 40', [$id]);
        json_out(['ok' => true, 'device' => $dev]);
    }

    if ($path === '/api/admin/report' && $method === 'GET') {
        require_admin();
        $id  = (int) ($_GET['id'] ?? 0);
        $day = preg_match('/^\d{4}-\d{2}-\d{2}$/', $_GET['day'] ?? '') ? $_GET['day'] : date('Y-m-d');
        $apps = q(
            "SELECT active_app, COUNT(*) AS samples, SUM(is_idle) AS idle
             FROM activity WHERE device_id = ? AND DATE(ts) = ? AND active_app <> ''
             GROUP BY active_app ORDER BY samples DESC LIMIT 30",
            [$id, $day]
        );
        $sites = q(
            "SELECT website, COUNT(*) AS samples FROM activity
             WHERE device_id = ? AND DATE(ts) = ? AND website <> ''
             GROUP BY website ORDER BY samples DESC LIMIT 30",
            [$id, $day]
        );
        json_out(['ok' => true, 'day' => $day, 'apps' => $apps, 'sites' => $sites, 'sample_seconds' => $cfg['sample_seconds']]);
    }

    if ($path === '/api/admin/enroll-key' && $method === 'POST') {
        require_admin();
        $b    = body();
        $code = enroll_code();
        x('INSERT INTO enroll_keys (enroll_key, label) VALUES (?,?)', [$code, mb_substr($b['label'] ?? '', 0, 128)]);
        json_out(['ok' => true, 'enroll_key' => $code]);
    }

    // Send an action to a computer (the agent runs it on its next ping).
    if ($path === '/api/admin/command' && $method === 'POST') {
        $a    = require_admin();
        $b    = body();
        $id   = (int) ($b['device_id'] ?? 0);
        $type = $b['type'] ?? '';
        $allowed = ['message', 'lock', 'logoff', 'restart', 'shutdown', 'live'];
        if (!in_array($type, $allowed, true)) {
            fail('Unknown action.', 400);
        }
        if (!q1('SELECT id FROM devices WHERE id = ?', [$id])) {
            fail('Not found.', 404);
        }
        $cid = x('INSERT INTO commands (device_id, type, payload, created_by) VALUES (?,?,?,?)',
            [$id, $type, mb_substr($b['payload'] ?? '', 0, 1000), $a['id']]);
        log_event($id, 'command', $type . ' ' . ($b['payload'] ?? ''));
        json_out(['ok' => true, 'command_id' => $cid]);
    }

    // The recent actions sent to a computer, with their outcome.
    if ($path === '/api/admin/commands' && $method === 'GET') {
        require_admin();
        $id = (int) ($_GET['id'] ?? 0);
        json_out(['ok' => true, 'commands' => q(
            'SELECT id, type, payload, status, result, created_at, done_at FROM commands WHERE device_id = ? ORDER BY id DESC LIMIT 30', [$id])]);
    }

    // Read or set what a computer may not run/open.
    if ($path === '/api/admin/policy' && $method === 'GET') {
        require_admin();
        $id  = (int) ($_GET['id'] ?? 0);
        $pol = q1('SELECT blocked_sites, blocked_apps FROM policies WHERE device_id = ?', [$id]);
        json_out(['ok' => true, 'blocked_sites' => $pol['blocked_sites'] ?? '', 'blocked_apps' => $pol['blocked_apps'] ?? '']);
    }
    if ($path === '/api/admin/policy' && $method === 'POST') {
        require_admin();
        $b  = body();
        $id = (int) ($b['device_id'] ?? 0);
        if (!q1('SELECT id FROM devices WHERE id = ?', [$id])) {
            fail('Not found.', 404);
        }
        x('INSERT INTO policies (device_id, blocked_sites, blocked_apps) VALUES (?,?,?)
           ON DUPLICATE KEY UPDATE blocked_sites = VALUES(blocked_sites), blocked_apps = VALUES(blocked_apps)',
            [$id, mb_substr($b['blocked_sites'] ?? '', 0, 8000), mb_substr($b['blocked_apps'] ?? '', 0, 8000)]);
        log_event($id, 'policy', 'updated');
        json_out(['ok' => true]);
    }

    // A screenshot image, admin only.
    if ($path === '/api/admin/shot' && $method === 'GET') {
        require_admin();
        $rel  = $_GET['p'] ?? '';
        if (!preg_match('#^\d+/[\w.]+\.jpg$#', $rel)) {
            fail('Bad path.', 400);
        }
        $full = em_config()['data_dir'] . '/shots/' . $rel;
        if (!is_file($full)) {
            fail('Not found.', 404);
        }
        header('Content-Type: image/jpeg');
        header('Cache-Control: private, max-age=86400');
        readfile($full);
        exit;
    }

    // ============ Dashboard (everything else) ============
    if ($path === '/' || $path === '/index.html') {
        header('Content-Type: text/html; charset=utf-8');
        readfile(__DIR__ . '/dashboard.html');
        exit;
    }

    fail('Not found.', 404);
}
