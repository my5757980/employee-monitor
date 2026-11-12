<?php
// Small HTTP helpers: JSON in/out, random tokens, and the two kinds of caller
// this server has - a signed-in admin (dashboard) and an enrolled device (agent).

function json_out($data, int $status = 200): void
{
    if (!headers_sent()) {           // a held ping has already started its reply
        http_response_code($status);
        header('Content-Type: application/json; charset=utf-8');
    }
    echo json_encode($data, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE);
    exit;
}

function fail(string $message, int $status = 400): void
{
    json_out(['ok' => false, 'error' => $message], $status);
}

/** The JSON body of the request as an array. */
function body(): array
{
    $raw = file_get_contents('php://input');
    if ($raw === '' || $raw === false) {
        return [];
    }
    $data = json_decode($raw, true);
    return is_array($data) ? $data : [];
}

function token(int $bytes = 32): string
{
    return bin2hex(random_bytes($bytes));
}

/** A short, readable enrollment key (no look-alike characters). */
function enroll_code(): string
{
    $alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789';
    $out = '';
    for ($i = 0; $i < 20; $i++) {
        $out .= $alphabet[random_int(0, strlen($alphabet) - 1)];
    }
    return $out;
}

/** The signed-in admin, from the session cookie - or 401. */
function require_admin(): array
{
    $tok = $_COOKIE['em_admin'] ?? '';
    if ($tok === '') {
        fail('Please sign in.', 401);
    }
    // A session that has not been used for 14 days is treated as expired, so an old
    // cookie left on a machine cannot sign in forever.
    $row = q1(
        'SELECT a.*, UNIX_TIMESTAMP(s.last_seen) AS sess_seen
         FROM admin_sessions s JOIN admins a ON a.id = s.admin_id
         WHERE s.token = ? AND s.last_seen > (NOW() - INTERVAL 14 DAY)',
        [$tok]
    );
    if (!$row) {
        fail('Please sign in.', 401);
    }
    // The dashboard fires many small requests (every screenshot, the 1s live poll).
    // Only touch the session clock once a minute, so images and polls don't each
    // cost a database write.
    if ((time() - (int) $row['sess_seen']) > 60) {
        x('UPDATE admin_sessions SET last_seen = NOW() WHERE token = ?', [$tok]);
    }
    unset($row['sess_seen']);
    return $row;
}

/** Like require_admin, but only an 'owner' may pass. A 'manager' account is
 * read-only: it can watch the dashboard but cannot send actions, change
 * restrictions, enrol or remove computers, or change settings. */
function require_owner(): array
{
    $a = require_admin();
    if (($a['role'] ?? '') !== 'owner') {
        fail('This action is only available to the owner account.', 403);
    }
    same_origin_or_fail();
    return $a;
}

/** The caller's IP, trusting the one reverse proxy in front (Caddy/Apache) when it
 * forwards it, so the login throttle counts real callers, not the proxy. */
function client_ip(): string
{
    $fwd = $_SERVER['HTTP_X_FORWARDED_FOR'] ?? '';
    if ($fwd !== '') {
        $first = trim(explode(',', $fwd)[0]);
        if ($first !== '') {
            return mb_substr($first, 0, 64);
        }
    }
    return mb_substr($_SERVER['REMOTE_ADDR'] ?? '', 0, 64);
}

/** Refuse a state-changing request that another web site made in the owner's signed-in
 * browser: its Origin (or Referer) host is not ours. Same-origin requests and non-browser
 * callers (the agent, curl - no Origin/Referer) are unaffected. A second line of defence
 * behind the SameSite=Lax cookie. */
function same_origin_or_fail(): void
{
    if (($_SERVER['REQUEST_METHOD'] ?? 'GET') === 'GET') {
        return;
    }
    $src = $_SERVER['HTTP_ORIGIN'] ?? '';
    if ($src === '' && !empty($_SERVER['HTTP_REFERER'])) {
        $src = $_SERVER['HTTP_REFERER'];
    }
    if ($src === '') {
        return;                       // no browser Origin/Referer to check
    }
    $host = parse_url($src, PHP_URL_HOST);
    $me   = preg_replace('/:\d+$/', '', $_SERVER['HTTP_HOST'] ?? '');
    if ($host === null || strcasecmp((string) $host, (string) $me) !== 0) {
        fail('This request did not come from the dashboard.', 403);
    }
}

/** Read the raw Authorization header no matter how the web server exposes it.
 * Apache/mod_php often drops it from $_SERVER; a .htaccess rewrite re-adds it as
 * REDIRECT_HTTP_AUTHORIZATION, and apache_request_headers() has it directly. */
function auth_header(): string
{
    foreach (['HTTP_AUTHORIZATION', 'REDIRECT_HTTP_AUTHORIZATION'] as $k) {
        if (!empty($_SERVER[$k])) {
            return $_SERVER[$k];
        }
    }
    if (function_exists('apache_request_headers')) {
        foreach (apache_request_headers() as $name => $val) {
            if (strcasecmp($name, 'Authorization') === 0 && $val !== '') {
                return $val;
            }
        }
    }
    return '';
}

/** The device behind an agent request, from its Bearer token - or 401. */
function require_device(): array
{
    $hdr = auth_header();
    if (stripos($hdr, 'Bearer ') === 0) {
        $tok = trim(substr($hdr, 7));
    } else {
        $tok = $_SERVER['HTTP_X_DEVICE_TOKEN'] ?? '';
    }
    if ($tok === '') {
        fail('No device token.', 401);
    }
    $dev = q1('SELECT * FROM devices WHERE device_token = ? AND active = 1', [$tok]);
    if (!$dev) {
        fail('Unknown device.', 401);
    }
    return $dev;
}

/** Split a textarea value into a clean list of non-empty, trimmed lines. */
function lines(?string $text): array
{
    if (!$text) {
        return [];
    }
    $out = [];
    foreach (preg_split('/[\r\n,]+/', $text) as $line) {
        $line = trim($line);
        if ($line !== '') {
            $out[] = $line;
        }
    }
    return array_values(array_unique($out));
}

function log_event(?int $deviceId, string $type, string $detail = ''): void
{
    x('INSERT INTO events (device_id, type, detail) VALUES (?,?,?)', [$deviceId, $type, mb_substr($detail, 0, 500)]);
}

/** A small file per computer and purpose, by which one request tells another that
 * something new is there, instantly and with no database. The folder is outside
 * the web root. */
function sig_file(int $deviceId, string $kind): string
{
    $dir = em_config()['data_dir'] . '/signal';
    if (!is_dir($dir)) {
        @mkdir($dir, 0775, true);
    }
    return $dir . '/' . $deviceId . '.' . $kind;
}

function sig_put(int $deviceId, string $kind, string $value): void
{
    @file_put_contents(sig_file($deviceId, $kind), $value, LOCK_EX);
}

function sig_get(int $deviceId, string $kind): string
{
    $v = @file_get_contents(sig_file($deviceId, $kind));
    return $v === false ? '' : $v;
}

/** Wake a computer's waiting ping, because an action or a rule is now there. */
function sig_wake(int $deviceId): void
{
    sig_put($deviceId, 'cmd', uniqid('', true));
}

/** How many seconds this computer's ping may wait for a new action. 0 = answer now.
 * The agent does its other work between pings, so a wait must never cost it:
 *  - frames are flowing (live, record): never stall them;
 *  - it has blocked applications: they are enforced on each ping, so keep that beat;
 *  - its next activity sample is near: end before it is due, so reports stay right;
 *  - its network cut an earlier wait: leave it alone for an hour. */
function ping_hold(int $id, array $blockedApps, int $sampleSeconds): int
{
    $max = (int) em_config()['ping_hold'];
    if ($max <= 0 || $blockedApps) {
        return 0;
    }
    $now = time();
    if ((int) sig_get($id, 'live') > $now || (int) sig_get($id, 'rec') > $now || (int) sig_get($id, 'nohold') > $now) {
        return 0;
    }
    $held = (int) sig_get($id, 'hold');
    if ($held > 0) {                     // its last wait never reached its end
        sig_put($id, 'hold', '0');
        if ($now <= $held + 5) {         // and it is already back, so the network cut it (a PC that was shut down takes longer)
            sig_put($id, 'nohold', (string) ($now + 3600));
            log_event($id, 'ping_hold_off', 'a waiting ping was cut short');
            return 0;
        }
    }
    $sample = (int) sig_get($id, 'sample');
    if ($sample === 0) {
        return 0;
    }
    return max(0, min($max, $sample + $sampleSeconds - $now - 4));
}

/** The grid's rows. One short indexed look-up per computer: the same answer as
 * correlated sub-queries, which sort a computer's whole history each time (39 ms
 * against 4 ms on the live data). */
function device_rows(): array
{
    $rows = q(
        'SELECT id, hostname, employee_name, group_name, os, agent_version, consent_ack, last_seen,
                (UNIX_TIMESTAMP() - UNIX_TIMESTAMP(last_seen)) AS ago
         FROM devices WHERE active = 1 ORDER BY last_seen DESC'
    );
    // Order by id (the server's own receipt order), not ts (the computer's clock), so a
    // computer with a wrong clock cannot freeze its tile on a stale or future frame.
    $shot = db()->prepare('SELECT path FROM screenshots WHERE device_id = ? ORDER BY id DESC LIMIT 1');
    $act  = db()->prepare('SELECT active_app, window_title, is_idle FROM activity WHERE device_id = ? ORDER BY id DESC LIMIT 1');
    foreach ($rows as &$r) {
        $shot->execute([$r['id']]);
        $s = $shot->fetch();
        $shot->closeCursor();
        $act->execute([$r['id']]);
        $a = $act->fetch();
        $act->closeCursor();
        $r['last_shot']    = $s ? $s['path'] : null;
        $r['online']       = ($r['ago'] !== null && $r['ago'] < 180);
        $r['active_app']   = $a ? $a['active_app'] : '';
        $r['window_title'] = $a ? $a['window_title'] : '';
        $r['is_idle']      = $a ? ((int) $a['is_idle'] === 1) : false;
    }
    unset($r);
    return $rows;
}

function setting(string $key): string
{
    try {
        $row = q1('SELECT sval FROM settings WHERE skey = ?', [$key]);
    } catch (Throwable $e) {
        return '';                   // the table comes with the next migrate
    }
    return (string) ($row['sval'] ?? '');
}

/** Everything the page needs to start, in one reply. */
function boot_payload(array $admin): array
{
    return [
        'ok'             => true,
        'name'           => $admin['name'] ?: $admin['username'],
        'role'           => $admin['role'],
        'devices'        => device_rows(),
        'sample_seconds' => em_config()['sample_seconds'],
        'cameras_url'    => setting('cameras_url'),
    ];
}

/** A day's application and website totals for one computer. */
function report_data(int $id, string $day): array
{
    // Range on ts (not DATE(ts)) so the (device_id, ts) index is used.
    $from = $day . ' 00:00:00';
    $to   = date('Y-m-d', strtotime($day . ' +1 day')) . ' 00:00:00';
    return [
        'ok'  => true,
        'day' => $day,
        'apps' => q(
            "SELECT active_app, COUNT(*) AS samples, SUM(is_idle) AS idle
             FROM activity WHERE device_id = ? AND ts >= ? AND ts < ? AND active_app <> ''
             GROUP BY active_app ORDER BY samples DESC LIMIT 30",
            [$id, $from, $to]
        ),
        'sites' => q(
            "SELECT website, COUNT(*) AS samples FROM activity
             WHERE device_id = ? AND ts >= ? AND ts < ? AND website <> ''
             GROUP BY website ORDER BY samples DESC LIMIT 30",
            [$id, $from, $to]
        ),
        'sample_seconds' => em_config()['sample_seconds'],
    ];
}

function procs_data(int $id): array
{
    $row  = q1('SELECT ts, apps FROM device_procs WHERE device_id = ?', [$id]);
    $apps = $row && $row['apps'] ? json_decode($row['apps'], true) : [];
    return ['ok' => true, 'ts' => $row['ts'] ?? null, 'apps' => is_array($apps) ? $apps : []];
}

function policy_data(int $id): array
{
    $pol = q1('SELECT blocked_sites, blocked_apps FROM policies WHERE device_id = ?', [$id]);
    return ['ok' => true, 'blocked_sites' => $pol['blocked_sites'] ?? '', 'blocked_apps' => $pol['blocked_apps'] ?? ''];
}

function commands_data(int $id): array
{
    // Leave out the high-rate remote-control input, so it never floods the recent-actions list.
    return q("SELECT id, type, payload, status, result, created_at, done_at FROM commands
              WHERE device_id = ?
                AND type NOT IN ('mouse_move','mouse_click','mouse_scroll','key','remote_start','remote_stop')
              ORDER BY id DESC LIMIT 30", [$id]);
}

/** Make a width-capped JPEG copy of a screenshot for fast thumbnails and live
 * frames (needs GD). The copy is cached next to the original, so each size is
 * built once, and is written to a temporary name first, because browsers keep it
 * for a long time and must never keep half a file. Returns the file written, the
 * original if it is already small enough, or '' if GD is missing or the source is
 * unreadable (caller then serves the full image). */
function em_make_thumb(string $src, string $dst, int $w, int $quality = 72): string
{
    if (!function_exists('imagecreatefromjpeg')) {
        return '';
    }
    $info = @getimagesize($src);
    if (!$info || empty($info[0]) || empty($info[1])) {
        return '';
    }
    [$sw, $sh] = $info;
    if ($sw <= $w) {                 // already small enough - reuse the original
        return $src;
    }
    $nw = $w;
    $nh = max(1, (int) round($sh * $w / $sw));
    $im = @imagecreatefromjpeg($src);
    if (!$im) {
        return '';
    }
    $t = imagecreatetruecolor($nw, $nh);
    imagecopyresampled($t, $im, 0, 0, 0, 0, $nw, $nh, $sw, $sh);
    imageinterlace($t, true);        // progressive: about 8% smaller here
    $tmp = $dst . '.' . getmypid() . '.tmp';
    $ok  = @imagejpeg($t, $tmp, $quality) && @rename($tmp, $dst);
    imagedestroy($t);
    imagedestroy($im);
    if (!$ok) {
        @unlink($tmp);
    }
    return $ok ? $dst : '';
}
