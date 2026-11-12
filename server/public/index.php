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
        // One-time keys: once a key has enrolled a computer it cannot enroll another.
        // This makes "Remove computer" stick - a removed agent that still holds its key
        // can no longer silently re-enroll itself and reappear as a brand-new computer.
        if (!empty($row['used_by'])) {
            fail('This install key was already used. Create a new key to add a computer.', 403);
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
        $id  = (int) $dev['id'];
        // Read the signal BEFORE looking for actions: one queued right after the
        // look still changes the signal, so it wakes the wait below.
        $seen = sig_get($id, 'cmd');
        // Also keep the agent version current: it used to be stored only at enrol, so
        // the console showed the old version for ever after an agent upgrade.
        $ver = mb_substr($b['agent_version'] ?? '', 0, 32);
        x("UPDATE devices SET last_seen = NOW(), consent_ack = GREATEST(consent_ack, ?),
                  agent_version = CASE WHEN ? <> '' THEN ? ELSE agent_version END WHERE id = ?",
            [isset($b['consent_ack']) ? (int) $b['consent_ack'] : 0, $ver, $ver, $id]);
        // Stale queued actions must not fire when a computer comes back much later
        // (a "Shut down" pressed while a PC was off would otherwise run next day).
        // Power actions expire after 5 minutes, anything else after an hour. Marked
        // 'failed' (the status enum has no 'expired') with the reason, so the owner
        // can see it never ran.
        x("UPDATE commands SET status = 'failed', result = 'expired: computer was offline too long', done_at = NOW()
           WHERE device_id = ? AND status = 'pending'
             AND ( (type IN ('logoff','restart','shutdown') AND created_at < NOW() - INTERVAL 5 MINUTE)
                   OR (type IN ('mouse_move','mouse_click','mouse_scroll','key') AND created_at < NOW() - INTERVAL 45 SECOND)
                   OR created_at < NOW() - INTERVAL 60 MINUTE )", [$id]);
        // An action stays on offer until the computer reports on it. The agent runs what it
        // gets and reports before its next ping, so 'sent' with no report means the reply
        // never reached it (it dropped off while its ping was waiting). Recent ones only, and
        // never the three that end a session: a second Restart is worse than a lost one, and
        // the owner can see it never said 'done'.
        $findCmds = fn () => q(
            "SELECT id, type, payload FROM commands WHERE device_id = ? AND (status = 'pending'
               OR (status = 'sent'
                   AND type NOT IN ('logoff', 'restart', 'shutdown', 'mouse_move', 'mouse_click', 'mouse_scroll', 'key')
                   AND created_at > NOW() - INTERVAL 180 SECOND))
             ORDER BY id", [$id]);
        $findPol  = fn () => q1('SELECT blocked_sites, blocked_apps FROM policies WHERE device_id = ?', [$id]);
        $cmds = $findCmds();
        $pol  = $findPol();

        // Nothing to do: keep the answer back for a while, so a new action reaches
        // this computer the moment it is queued, with no change on the computer.
        $hold = $cmds ? 0 : ping_hold($id, lines($pol['blocked_apps'] ?? ''), (int) $cfg['sample_seconds']);
        if ($hold > 0) {
            sig_put($id, 'hold', (string) (time() + $hold));
            db_close();
            header('Content-Type: application/json; charset=utf-8');
            $sigFile = sig_file($id, 'cmd');
            $end  = microtime(true) + $hold;
            $beat = microtime(true);
            while (microtime(true) < $end) {
                usleep(50000);
                if ((string) @file_get_contents($sigFile) !== $seen) {
                    break;
                }
                if (microtime(true) - $beat >= 2) {   // a space is valid before JSON; if the computer has gone, PHP stops here
                    echo ' ';
                    flush();
                    $beat = microtime(true);
                }
            }
            // Same check once more, so an action is never handed to a computer that has gone.
            echo ' ';
            flush();
            usleep(2000);
            echo ' ';
            flush();
            sig_put($id, 'hold', '0');
            x('UPDATE devices SET last_seen = NOW() WHERE id = ?', [$id]);
            $cmds = $findCmds();
            $pol  = $findPol();
        }

        // Hand over any pending actions, once (mark them 'sent' so they don't repeat).
        if ($cmds) {
            $ids = implode(',', array_map(fn ($c) => (int) $c['id'], $cmds));
            db()->exec("UPDATE commands SET status = 'sent' WHERE id IN ($ids)");
            foreach ($cmds as $c) {               // while frames flow, pings must not wait
                if ($c['type'] === 'live') {
                    $secs = $c['payload'] === '' ? 60 : (int) $c['payload'];
                    sig_put($id, 'live', (string) ($secs > 0 ? time() + $secs + 5 : 0));
                } elseif ($c['type'] === 'record') {
                    sig_put($id, 'rec', (string) (time() + ($c['payload'] === '' ? 5 : (int) $c['payload']) * 60 + 5));
                }
            }
        }

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
        sig_put((int) $dev['id'], 'sample', (string) time());
        json_out(['ok' => true, 'saved' => $n]);
    }

    if ($path === '/api/agent/screenshot' && $method === 'POST') {
        $dev = require_device();
        if (empty($_FILES['shot']['tmp_name']) || !is_uploaded_file($_FILES['shot']['tmp_name'])) {
            fail('No image.', 400);
        }
        // Only a real JPEG is stored, so junk or a mislabelled file can never be kept
        // and later handed back to the dashboard as if it were a screenshot.
        $imgInfo = @getimagesize($_FILES['shot']['tmp_name']);
        if (!$imgInfo || ($imgInfo[2] ?? 0) !== IMAGETYPE_JPEG) {
            fail('Not a JPEG image.', 400);
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
        $shotId = x('INSERT INTO screenshots (device_id, ts, path, w, h) VALUES (?,?,?,?,?)',
            [$dev['id'], date('Y-m-d H:i:s', $ts), $dev['id'] . '/' . $name,
             (int) ($_POST['w'] ?? 0), (int) ($_POST['h'] ?? 0)]);
        x('UPDATE devices SET last_seen = NOW() WHERE id = ?', [$dev['id']]);
        sig_put((int) $dev['id'], 'frame', json_encode(
            ['id' => $shotId, 'ts' => date('Y-m-d H:i:s', $ts), 'path' => $dev['id'] . '/' . $name],
            JSON_UNESCAPED_SLASHES));
        json_out(['ok' => true]);
    }

    if ($path === '/api/agent/procs' && $method === 'POST') {
        $dev  = require_device();
        $b    = body();
        $apps = array_slice(array_values(array_filter(array_map(
            fn ($a) => mb_substr(trim((string) $a), 0, 120), $b['apps'] ?? []))), 0, 300);
        x('INSERT INTO device_procs (device_id, ts, apps) VALUES (?, NOW(), ?)
           ON DUPLICATE KEY UPDATE ts = NOW(), apps = VALUES(apps)',
            [$dev['id'], json_encode($apps, JSON_UNESCAPED_UNICODE)]);
        json_out(['ok' => true]);
    }

    // A notice the agent was told to show (command type 'notice', payload = id).
    // Only readable by a computer the notice was actually sent to.
    if ($path === '/api/agent/notice' && $method === 'GET') {
        $dev = require_device();
        $nid = (int) ($_GET['id'] ?? 0);
        if (!q1('SELECT id FROM commands WHERE device_id = ? AND type = ? AND payload = ?',
                [$dev['id'], 'notice', (string) $nid])) {
            fail('Not found.', 404);
        }
        $n = q1('SELECT body, image, voice FROM notices WHERE id = ?', [$nid]);
        if (!$n) {
            fail('Not found.', 404);
        }
        json_out(['ok' => true, 'body' => $n['body'],
                  'image' => $n['image'] !== '', 'voice' => $n['voice'] !== '']);
    }

    // The picture or voice of a notice, streamed to the computer it was sent to.
    if ($path === '/api/agent/notice-media' && $method === 'GET') {
        $dev  = require_device();
        $nid  = (int) ($_GET['id'] ?? 0);
        $kind = $_GET['kind'] ?? '';
        if (!in_array($kind, ['image', 'voice'], true)) {
            fail('Bad kind.', 400);
        }
        if (!q1('SELECT id FROM commands WHERE device_id = ? AND type = ? AND payload = ?',
                [$dev['id'], 'notice', (string) $nid])) {
            fail('Not found.', 404);
        }
        $n = q1('SELECT image, voice FROM notices WHERE id = ?', [$nid]);
        $file = $n ? ($kind === 'image' ? $n['image'] : $n['voice']) : '';
        if ($file === '' || !preg_match('/^[\w.]+$/', $file)) {
            fail('Not found.', 404);
        }
        $full = em_config()['data_dir'] . '/notices/' . $nid . '/' . $file;
        if (!is_file($full)) {
            fail('Not found.', 404);
        }
        $ext   = strtolower(pathinfo($file, PATHINFO_EXTENSION));
        $types = ['jpg' => 'image/jpeg', 'png' => 'image/png', 'gif' => 'image/gif',
                  'webp' => 'image/webp', 'wav' => 'audio/wav'];
        header('Content-Type: ' . ($types[$ext] ?? 'application/octet-stream'));
        header('Cache-Control: private, max-age=3600');
        readfile($full);
        exit;
    }

    // ============ ADMIN API (the dashboard) ============
    if ($path === '/api/admin/login' && $method === 'POST') {
        $b    = body();
        $user = trim($b['username'] ?? '');
        $ip   = client_ip();
        // Rate limit by where the tries come from, not by the username. Keying on the
        // username let anyone lock a real account out just by guessing its name with
        // wrong passwords; keying on the caller's address means a flood only blocks
        // the flooder.
        $fails = (int) (q1("SELECT COUNT(*) c FROM events WHERE type = 'admin_login_fail' AND detail LIKE ? AND ts > NOW() - INTERVAL 10 MINUTE", ['% @ ' . $ip])['c'] ?? 0);
        if ($fails >= 15) {
            fail('Too many failed attempts. Please wait a few minutes and try again.', 429);
        }
        $a    = q1('SELECT * FROM admins WHERE username = ?', [$user]);
        if (!$a || !password_verify($b['password'] ?? '', $a['password_hash'])) {
            log_event(null, 'admin_login_fail', $user . ' @ ' . $ip);
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
        json_out(boot_payload($a));
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

    // Everything the page needs to start, in one reply: one trip instead of three.
    if ($path === '/api/admin/boot' && $method === 'GET') {
        json_out(boot_payload(require_admin()));
    }

    if ($path === '/api/admin/devices' && $method === 'GET') {
        require_admin();
        json_out(['ok' => true, 'devices' => device_rows(), 'sample_seconds' => $cfg['sample_seconds']]);
    }

    if ($path === '/api/admin/device' && $method === 'GET') {
        require_admin();
        $id  = (int) ($_GET['id'] ?? 0);
        $dev = q1('SELECT id, hostname, employee_name, group_name, os, agent_version, consent_ack, last_seen,
                          (UNIX_TIMESTAMP() - UNIX_TIMESTAMP(last_seen)) AS ago FROM devices WHERE id = ?', [$id]);
        if (!$dev) {
            fail('Not found.', 404);
        }
        $dev['online']      = ($dev['ago'] !== null && $dev['ago'] < 180);
        // Order by id (the order the server received them), not by ts (the computer's
        // own clock): a computer whose clock is wrong must not push its newest frames
        // out of view or freeze the grid on a future-dated one.
        $dev['activity']    = q('SELECT ts, active_app, window_title, website, is_idle FROM activity WHERE device_id = ? ORDER BY id DESC LIMIT 100', [$id]);
        $dev['screenshots'] = q('SELECT id, ts, path FROM screenshots WHERE device_id = ? ORDER BY id DESC LIMIT 40', [$id]);
        // All four tabs in this one reply, so moving between tabs makes no trip.
        $dev['report']   = report_data($id, date('Y-m-d'));
        $dev['procs']    = procs_data($id);
        $dev['policy']   = policy_data($id);
        $dev['commands'] = commands_data($id);
        json_out(['ok' => true, 'device' => $dev]);
    }

    // A tiny poll for Live view: only the newest frame + the online flag, so
    // watching costs one small query a second instead of re-reading all history.
    if ($path === '/api/admin/live' && $method === 'GET') {
        require_admin();
        $id   = (int) ($_GET['id'] ?? 0);
        $shot = q1('SELECT id, ts, path FROM screenshots WHERE device_id = ? ORDER BY id DESC LIMIT 1', [$id]);
        $dev  = q1('SELECT (UNIX_TIMESTAMP() - UNIX_TIMESTAMP(last_seen)) AS ago FROM devices WHERE id = ?', [$id]);
        $ago  = $dev['ago'] ?? null;
        json_out(['ok' => true, 'shot' => $shot ?: null, 'ago' => $ago, 'online' => ($ago !== null && $ago < 180)]);
    }

    // Live view without polling: this reply stays open and each new frame is written
    // into it the moment the computer's upload is stored. Each message is
    // [4 bytes: meta length][4 bytes: image length][meta JSON][JPEG]. The browser
    // opens it again after 170 s, and falls back to /api/admin/live if a network on
    // the way holds the stream back.
    if ($path === '/api/admin/live-stream' && $method === 'GET') {
        require_admin();
        $id    = (int) ($_GET['id'] ?? 0);
        $after = (int) ($_GET['after'] ?? 0);
        db_close();                                // only files from here on
        while (ob_get_level() > 0) {
            ob_end_clean();
        }
        // The label makes proxies pass it straight through; the browser reads raw bytes.
        header('Content-Type: text/event-stream');
        header('Cache-Control: no-store, no-transform');
        header('X-Accel-Buffering: no');           // tell our reverse proxy not to hold it
        $send = function (array $meta, string $img = ''): void {
            $m = json_encode($meta, JSON_UNESCAPED_SLASHES);
            echo pack('NN', strlen($m), strlen($img)), $m, $img;
            flush();
        };
        $send(['hello' => 1]);
        $file  = sig_file($id, 'frame');
        $base  = em_config()['data_dir'] . '/shots/';
        $start = time();
        $beat  = $start;
        // A frame stored a while ago is not live: the page already shows the newest shot.
        $last  = (is_file($file) && $start - filemtime($file) > 5) ? (string) @file_get_contents($file) : '';
        while (time() - $start < 170) {
            $raw = (string) @file_get_contents($file);
            if ($raw !== '' && $raw !== $last) {
                $last = $raw;
                $f = json_decode($raw, true);
                if (is_array($f) && (int) ($f['id'] ?? 0) > $after && is_string($f['path'] ?? null)) {
                    $img = @file_get_contents($base . $f['path']);
                    if ($img !== false) {
                        $after = (int) $f['id'];
                        $send($f, $img);
                        $beat = time();
                    }
                }
            }
            if (time() - $beat >= 4) {             // if the browser has gone, PHP stops here
                $send(['beat' => 1]);
                $beat = time();
            }
            usleep(30000);
        }
        exit;
    }

    if ($path === '/api/admin/report' && $method === 'GET') {
        require_admin();
        $id  = (int) ($_GET['id'] ?? 0);
        $day = preg_match('/^\d{4}-\d{2}-\d{2}$/', $_GET['day'] ?? '') ? $_GET['day'] : date('Y-m-d');
        json_out(report_data($id, $day));
    }

    // The day's report as a CSV file (for Excel).
    if ($path === '/api/admin/report-csv' && $method === 'GET') {
        require_admin();
        $id  = (int) ($_GET['id'] ?? 0);
        $day = preg_match('/^\d{4}-\d{2}-\d{2}$/', $_GET['day'] ?? '') ? $_GET['day'] : date('Y-m-d');
        $from = $day . ' 00:00:00';
        $to   = date('Y-m-d', strtotime($day . ' +1 day')) . ' 00:00:00';
        $sec = (int) $cfg['sample_seconds'];
        $dev = q1('SELECT employee_name, hostname FROM devices WHERE id = ?', [$id]);
        $apps = q("SELECT active_app, COUNT(*) s, SUM(is_idle) i FROM activity WHERE device_id=? AND ts>=? AND ts<? AND active_app<>'' GROUP BY active_app ORDER BY s DESC", [$id, $from, $to]);
        $sites = q("SELECT website, COUNT(*) s FROM activity WHERE device_id=? AND ts>=? AND ts<? AND website<>'' GROUP BY website ORDER BY s DESC", [$id, $from, $to]);
        header('Content-Type: text/csv; charset=utf-8');
        header('Content-Disposition: attachment; filename="report-' . $id . '-' . $day . '.csv"');
        $out = fopen('php://output', 'w');
        fwrite($out, "\xEF\xBB\xBF");   // UTF-8 mark, so Excel reads non-English names correctly
        // An app or window title is set by whatever the employee ran, so it must never
        // be treated as a live formula when the file is opened in a spreadsheet.
        $safe = fn ($v) => (is_string($v) && $v !== '' && strpbrk($v[0], "=+-@\t\r") !== false) ? "'" . $v : $v;
        fputcsv($out, ['Computer', $safe(($dev['employee_name'] ?? '') ?: ($dev['hostname'] ?? '')), 'Date', $day]);
        fputcsv($out, []);
        fputcsv($out, ['Application', 'Minutes', 'Idle minutes']);
        foreach ($apps as $a) {
            fputcsv($out, [$safe($a['active_app']), round($a['s'] * $sec / 60), round($a['i'] * $sec / 60)]);
        }
        fputcsv($out, []);
        fputcsv($out, ['Website', 'Minutes']);
        foreach ($sites as $s) {
            fputcsv($out, [$safe($s['website']), round($s['s'] * $sec / 60)]);
        }
        fclose($out);
        exit;
    }

    if ($path === '/api/admin/enroll-key' && $method === 'POST') {
        require_owner();
        $b    = body();
        $code = enroll_code();
        x('INSERT INTO enroll_keys (enroll_key, label) VALUES (?,?)', [$code, mb_substr($b['label'] ?? '', 0, 128)]);
        json_out(['ok' => true, 'enroll_key' => $code]);
    }

    // Rename a computer / set its group.
    if ($path === '/api/admin/device-update' && $method === 'POST') {
        require_owner();
        $b  = body();
        $id = (int) ($b['id'] ?? 0);
        if (!q1('SELECT id FROM devices WHERE id = ?', [$id])) {
            fail('Not found.', 404);
        }
        x('UPDATE devices SET employee_name = ?, group_name = ? WHERE id = ?',
            [mb_substr($b['employee_name'] ?? '', 0, 190), mb_substr($b['group_name'] ?? '', 0, 190), $id]);
        json_out(['ok' => true]);
    }

    // Remove a computer from the dashboard (hides it; data kept).
    if ($path === '/api/admin/device-remove' && $method === 'POST') {
        require_owner();
        $b  = body();
        $id = (int) ($b['id'] ?? 0);
        x('UPDATE devices SET active = 0 WHERE id = ?', [$id]);
        log_event($id, 'device_removed', '');
        json_out(['ok' => true]);
    }

    // Send an action to a computer (the agent runs it on its next ping).
    if ($path === '/api/admin/command' && $method === 'POST') {
        $a    = require_owner();
        $b    = body();
        $id   = (int) ($b['device_id'] ?? 0);
        $type = $b['type'] ?? '';
        // Remote control (mouse + keyboard) rides the same queue. These high-rate input
        // actions are kept out of the audit log and the "recent actions" list below.
        $allowed = ['message', 'lock', 'logoff', 'restart', 'shutdown', 'live', 'record',
                    'remote_start', 'remote_stop', 'mouse_move', 'mouse_click', 'mouse_scroll', 'key'];
        $input   = ['remote_start', 'remote_stop', 'mouse_move', 'mouse_click', 'mouse_scroll', 'key'];
        if (!in_array($type, $allowed, true)) {
            fail('Unknown action.', 400);
        }
        $payload = mb_substr((string) ($b['payload'] ?? ''), 0, 1000);
        // Live / record carry a count of seconds. A non-number used to reach the agent
        // and stall its loop for minutes, so it is refused here.
        if (($type === 'live' || $type === 'record') && !preg_match('/^\d{1,6}$/', trim($payload))) {
            fail('That action needs a number of seconds.', 400);
        }
        if (!q1('SELECT id FROM devices WHERE id = ?', [$id])) {
            fail('Not found.', 404);
        }
        $cid = x('INSERT INTO commands (device_id, type, payload, created_by) VALUES (?,?,?,?)',
            [$id, $type, $payload, $a['id']]);
        if (!in_array($type, $input, true)) {
            log_event($id, 'command', $type . ' ' . $payload . ' · by ' . ($a['name'] ?: $a['username']));
        }
        sig_wake($id);
        json_out(['ok' => true, 'command_id' => $cid]);
    }

    // Send a NOTICE (text + optional picture + optional voice) to one computer,
    // several hand-picked computers, or all of them. The content is stored once
    // and one 'notice' command is queued per computer. Multipart form:
    //   body, scope (one|some|all), device_ids (csv, for one/some), image, voice.
    if ($path === '/api/admin/notice' && $method === 'POST') {
        $a     = require_owner();
        $text  = mb_substr(trim((string) ($_POST['body'] ?? '')), 0, 2000);
        $scope = in_array($_POST['scope'] ?? '', ['one', 'some', 'all'], true) ? $_POST['scope'] : 'one';

        // Who it goes to - only computers that still exist and are active.
        if ($scope === 'all') {
            $targets = array_map(fn ($r) => (int) $r['id'], q('SELECT id FROM devices WHERE active = 1'));
        } else {
            $raw = $_POST['device_ids'] ?? '';
            $raw = is_array($raw) ? $raw : explode(',', (string) $raw);
            $ids = array_values(array_unique(array_filter(array_map('intval', $raw))));
            if (!$ids) {
                fail('Choose at least one computer.', 400);
            }
            $in      = implode(',', array_fill(0, count($ids), '?'));
            $targets = array_map(fn ($r) => (int) $r['id'],
                q("SELECT id FROM devices WHERE active = 1 AND id IN ($in)", $ids));
        }
        if (!$targets) {
            fail('No computers to send to.', 400);
        }
        foreach (['image' => 'picture', 'voice' => 'voice clip'] as $field => $word) {
            if (in_array($_FILES[$field]['error'] ?? 0, [UPLOAD_ERR_INI_SIZE, UPLOAD_ERR_FORM_SIZE], true)) {
                fail("The $word is too large for the server. Please send a smaller one.", 400);
            }
        }
        $hasImg = !empty($_FILES['image']['tmp_name']) && is_uploaded_file($_FILES['image']['tmp_name']);
        $hasVoc = !empty($_FILES['voice']['tmp_name']) && is_uploaded_file($_FILES['voice']['tmp_name']);
        if ($text === '' && !$hasImg && !$hasVoc) {
            fail('Write a message, or add a picture or voice.', 400);
        }

        // Check the files BEFORE anything is written, so a bad picture or voice leaves
        // no half-made notice row and no empty folder behind.
        $imgExt = '';
        if ($hasImg) {
            if ((int) ($_FILES['image']['size'] ?? 0) > 6 * 1024 * 1024) {
                fail('The picture is too large (max 6 MB).', 400);
            }
            $info   = @getimagesize($_FILES['image']['tmp_name']);
            $imgExt = ['image/jpeg' => 'jpg', 'image/png' => 'png', 'image/gif' => 'gif',
                       'image/webp' => 'webp'][$info['mime'] ?? ''] ?? '';
            if (!$imgExt) {
                fail('That picture type is not supported (use JPG, PNG, GIF or WebP).', 400);
            }
        }
        if ($hasVoc) {
            if ((int) ($_FILES['voice']['size'] ?? 0) > 12 * 1024 * 1024) {
                fail('The voice clip is too large (max 12 MB).', 400);
            }
            $head = (string) file_get_contents($_FILES['voice']['tmp_name'], false, null, 0, 12);
            if (substr($head, 0, 4) !== 'RIFF' || substr($head, 8, 4) !== 'WAVE') {
                fail('The voice clip must be a WAV recording.', 400);
            }
        }

        $nid = x('INSERT INTO notices (body, scope, recipients, created_by) VALUES (?,?,?,?)',
            [$text, $scope, count($targets), $a['id']]);
        $image = '';
        $voice = '';
        if ($hasImg || $hasVoc) {
            $dir = em_config()['data_dir'] . '/notices/' . $nid;
            @mkdir($dir, 0775, true);
            if ($hasImg) {
                $image = 'image.' . $imgExt;
                if (!move_uploaded_file($_FILES['image']['tmp_name'], $dir . '/' . $image)) {
                    fail('Could not store the picture.', 500);
                }
            }
            if ($hasVoc) {
                $voice = 'voice.wav';
                if (!move_uploaded_file($_FILES['voice']['tmp_name'], $dir . '/' . $voice)) {
                    fail('Could not store the voice clip.', 500);
                }
            }
            x('UPDATE notices SET image = ?, voice = ? WHERE id = ?', [$image, $voice, $nid]);
        }

        foreach ($targets as $did) {
            x('INSERT INTO commands (device_id, type, payload, created_by) VALUES (?,?,?,?)',
                [$did, 'notice', (string) $nid, $a['id']]);
            sig_wake($did);
        }
        log_event(null, 'notice', 'sent #' . $nid . ' to ' . count($targets) . ' computer(s) [' . $scope . ']');
        json_out(['ok' => true, 'notice_id' => $nid, 'sent' => count($targets)]);
    }

    // The recent actions sent to a computer, with their outcome.
    if ($path === '/api/admin/commands' && $method === 'GET') {
        require_admin();
        $id = (int) ($_GET['id'] ?? 0);
        json_out(['ok' => true, 'commands' => commands_data($id)]);
    }

    // The computer's latest running-applications list (sent while watching live).
    if ($path === '/api/admin/procs' && $method === 'GET') {
        require_admin();
        json_out(procs_data((int) ($_GET['id'] ?? 0)));
    }

    // Stop an application now and keep it blocked (adds it to the block list).
    if ($path === '/api/admin/block-app' && $method === 'POST') {
        require_owner();
        $b   = body();
        $id  = (int) ($b['device_id'] ?? 0);
        $app = mb_substr(trim($b['app'] ?? ''), 0, 120);
        if (!$app || !q1('SELECT id FROM devices WHERE id = ?', [$id])) {
            fail('Not found.', 404);
        }
        $pol  = q1('SELECT blocked_apps FROM policies WHERE device_id = ?', [$id]);
        $list = lines($pol['blocked_apps'] ?? '');
        if (!in_array($app, $list, true)) {
            $list[] = $app;
        }
        $text = implode("\n", $list);
        x('INSERT INTO policies (device_id, blocked_apps) VALUES (?,?)
           ON DUPLICATE KEY UPDATE blocked_apps = VALUES(blocked_apps)', [$id, $text]);
        log_event($id, 'block_app', $app);
        sig_wake($id);
        json_out(['ok' => true, 'blocked_apps' => $text]);
    }

    // Read or set what a computer may not run/open.
    if ($path === '/api/admin/policy' && $method === 'GET') {
        require_admin();
        json_out(policy_data((int) ($_GET['id'] ?? 0)));
    }
    if ($path === '/api/admin/policy' && $method === 'POST') {
        require_owner();
        $b  = body();
        $id = (int) ($b['device_id'] ?? 0);
        if (!q1('SELECT id FROM devices WHERE id = ?', [$id])) {
            fail('Not found.', 404);
        }
        x('INSERT INTO policies (device_id, blocked_sites, blocked_apps) VALUES (?,?,?)
           ON DUPLICATE KEY UPDATE blocked_sites = VALUES(blocked_sites), blocked_apps = VALUES(blocked_apps)',
            [$id, mb_substr($b['blocked_sites'] ?? '', 0, 8000), mb_substr($b['blocked_apps'] ?? '', 0, 8000)]);
        log_event($id, 'policy', 'updated');
        sig_wake($id);
        json_out(['ok' => true]);
    }

    // Frames for playback: a computer's screenshots over a time range, oldest first.
    if ($path === '/api/admin/frames' && $method === 'GET') {
        require_admin();
        $id    = (int) ($_GET['id'] ?? 0);
        $limit = max(1, min(600, (int) ($_GET['limit'] ?? 300)));
        $from  = $_GET['from'] ?? '';
        $to    = $_GET['to'] ?? '';
        $sql   = 'SELECT id, ts, path FROM screenshots WHERE device_id = ?';
        $args  = [$id];
        if (preg_match('/^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?$/', $from)) {
            $sql .= ' AND ts >= ?';
            $args[] = str_replace('T', ' ', $from);
        }
        if (preg_match('/^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?$/', $to)) {
            $sql .= ' AND ts <= ?';
            $args[] = str_replace('T', ' ', $to);
        }
        // newest N, then show oldest-first for playback
        $rows = q($sql . ' ORDER BY ts DESC LIMIT ' . $limit, $args);
        json_out(['ok' => true, 'frames' => array_reverse($rows)]);
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
        // Serve a width-capped copy for the grid (w=360) and the main / live view
        // (w=1280, kept sharp); full resolution only when no width is asked
        // (Fullscreen). Each size is built once and cached beside the original.
        $file = $full;
        $w    = (int) ($_GET['w'] ?? 0);
        if ($w === 360 || $w === 1280) {
            $thumb = $full . '.' . $w . '.jpg';
            if (!is_file($thumb) || filemtime($thumb) < filemtime($full)) {
                em_make_thumb($full, $thumb, $w, $w === 360 ? 62 : 82);
            }
            if (is_file($thumb)) {
                $file = $thumb;
            }
        }
        header('Content-Type: image/jpeg');
        header('Cache-Control: private, max-age=2592000, immutable');   // a name is never reused
        header('Content-Length: ' . filesize($file));
        readfile($file);
        exit;
    }

    // The office cameras' own web page (e.g. Guarding Vision portal or the DVR's
    // web page). We only store/return that address; the dashboard "Cameras" button
    // opens it in a new tab. No camera video is streamed or stored by this server.
    if ($path === '/api/admin/cameras' && $method === 'GET') {
        require_admin();
        json_out(['ok' => true, 'url' => setting('cameras_url')]);
    }
    if ($path === '/api/admin/cameras' && $method === 'POST') {
        require_owner();
        $b   = body();
        $url = trim($b['url'] ?? '');
        if ($url !== '' && !preg_match('#^https?://#i', $url)) {
            fail('Enter a web address that starts with http:// or https://', 400);
        }
        $url = mb_substr($url, 0, 500);
        x("INSERT INTO settings (skey, sval) VALUES ('cameras_url', ?)
           ON DUPLICATE KEY UPDATE sval = VALUES(sval)", [$url]);
        log_event(null, 'cameras', $url === '' ? 'link cleared' : 'link set');
        json_out(['ok' => true, 'url' => $url]);
    }

    // The activity / audit log: sign-ins, actions sent to computers, and settings
    // changes. Owner-only, because it shows every admin's actions on every computer.
    if ($path === '/api/admin/events' && $method === 'GET') {
        require_owner();
        $rows = q(
            "SELECT e.ts, e.type, e.detail, d.hostname, d.employee_name
             FROM events e LEFT JOIN devices d ON d.id = e.device_id
             ORDER BY e.id DESC LIMIT 300"
        );
        json_out(['ok' => true, 'events' => $rows]);
    }

    // ============ Dashboard (everything else) ============
    if ($path === '/' || $path === '/index.html') {
        $file  = __DIR__ . '/dashboard.html';
        $shell = sprintf('%x-%x', filemtime($file), filesize($file));
        header('Content-Type: text/html; charset=utf-8');
        header('Cache-Control: no-cache');
        header('ETag: "' . $shell . '"');
        header('X-Shell: ' . $shell);
        // The reverse proxy and Apache may add W/ or -gzip when they compress.
        if (str_replace(['W/', '"', '-gzip'], '', $_SERVER['HTTP_IF_NONE_MATCH'] ?? '') === $shell) {
            http_response_code(304);
            exit;
        }
        readfile($file);
        exit;
    }

    // With the README's local run (`php -S ... index.php`) every path reaches this
    // router, so serve the page's service worker by name. On a real Apache host the
    // file exists and is served directly, so this never runs there.
    if ($path === '/sw.js' && $method === 'GET') {
        header('Content-Type: text/javascript; charset=utf-8');
        header('Cache-Control: no-cache');
        readfile(__DIR__ . '/sw.js');
        exit;
    }

    fail('Not found.', 404);
}