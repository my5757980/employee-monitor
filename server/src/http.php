<?php
// Small HTTP helpers: JSON in/out, random tokens, and the two kinds of caller
// this server has - a signed-in admin (dashboard) and an enrolled device (agent).

function json_out($data, int $status = 200): void
{
    if (!headers_sent()) {           // a held ping has already started its reply
        http_response_code($status);
        header('Content-Type: application/json; charset=utf-8');
    }
    // A window title is whatever the employee's programs set: a broken character in
    // one must not turn the whole reply into nothing.
    echo json_encode($data, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE | JSON_INVALID_UTF8_SUBSTITUTE);
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
    if ($data === null && json_last_error() === JSON_ERROR_UTF16) {
        // Windows lets a window title hold half of a two-part character, and JSON
        // refuses the whole body for it. Swap each stray half for the "unknown
        // character" mark and read it again, so that one title does not cost a sample.
        $raw = preg_replace(
            ['/\\\\u[dD][89abAB][0-9a-fA-F]{2}(?!\\\\u[dD][c-fC-F][0-9a-fA-F]{2})/',
             '/(?<!\\\\u[dD][89abAB][0-9a-fA-F]{2})\\\\u[dD][c-fC-F][0-9a-fA-F]{2}/'],
            '\\\\ufffd',
            $raw
        );
        $data = json_decode((string) $raw, true);
    }
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

/** One address as plain text, or '' if it is not an address. Takes the forms a proxy
 * may write: 1.2.3.4, 1.2.3.4:5678, [2001:db8::1]:443, ::ffff:1.2.3.4 */
function ip_clean(string $raw): string
{
    $ip = trim($raw);
    if (preg_match('/^\[(.+)\](:\d+)?$/', $ip, $m)) {
        $ip = $m[1];
    } elseif (preg_match('/^(\d{1,3}(\.\d{1,3}){3}):\d+$/', $ip, $m)) {
        $ip = $m[1];
    }
    if (stripos($ip, '::ffff:') === 0 && filter_var(substr($ip, 7), FILTER_VALIDATE_IP, FILTER_FLAG_IPV4)) {
        $ip = substr($ip, 7);
    }
    return filter_var($ip, FILTER_VALIDATE_IP) ? $ip : '';
}

/** Is this address inside the range ("10.0.0.0/8", "fc00::/7", or one address)? */
function ip_in(string $ip, string $range): bool
{
    $bits = null;
    if (strpos($range, '/') !== false) {
        [$range, $bits] = explode('/', $range, 2);
    }
    $a = @inet_pton($ip);
    $b = @inet_pton(trim($range));
    if ($a === false || $b === false || strlen($a) !== strlen($b)) {
        return false;
    }
    $bits  = $bits === null ? strlen($a) * 8 : max(0, min(strlen($a) * 8, (int) $bits));
    $whole = intdiv($bits, 8);
    if ($whole > 0 && substr($a, 0, $whole) !== substr($b, 0, $whole)) {
        return false;
    }
    $rest = $bits % 8;
    if ($rest === 0) {
        return true;
    }
    $mask = (0xFF << (8 - $rest)) & 0xFF;
    return (ord($a[$whole]) & $mask) === (ord($b[$whole]) & $mask);
}

/** May this address be believed when it names the caller behind it (it is our own
 * reverse proxy)? Set by EM_TRUSTED_PROXIES. */
function proxy_trusted(string $ip): bool
{
    foreach (preg_split('/[\s,]+/', strtolower(trim(em_config()['trusted_proxies']))) as $r) {
        if ($r === '' || $r === 'none') {
            continue;
        }
        $ranges = $r === 'private'
            ? ['127.0.0.0/8', '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '::1', 'fc00::/7']
            : [$r];
        foreach ($ranges as $range) {
            if (ip_in($ip, $range)) {
                return true;
            }
        }
    }
    return false;
}

/** The caller's address, for the sign-in throttle and the log. X-Forwarded-For is a
 * header anyone can type, so it is believed only when the request reached us from
 * our own reverse proxy, and then only its last hop that is not one of ours - the
 * part the proxy itself wrote. Whatever a caller puts in front of that is ignored,
 * so nobody can dodge the throttle, or lock out someone else's address, by lying. */
function client_ip(): string
{
    $peer = ip_clean($_SERVER['REMOTE_ADDR'] ?? '');
    if ($peer === '' || !proxy_trusted($peer)) {
        return $peer;
    }
    foreach (array_reverse(explode(',', $_SERVER['HTTP_X_FORWARDED_FOR'] ?? '')) as $hop) {
        $ip = ip_clean($hop);
        if ($ip === '') {
            break;                    // not an address: stop believing the list here
        }
        if (!proxy_trusted($ip)) {
            return $ip;
        }
        $peer = $ip;
    }
    return $peer;
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
    foreach (['HTTP_AUTHORIZATION', 'REDIRECT_HTTP_AUTHORIZATION', 'REDIRECT_REDIRECT_HTTP_AUTHORIZATION'] as $k) {
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

/** The blocked-programs list as the computer needs it. A program runs under its file
 * name, so an owner who typed "vlc" means vlc.exe: add that form too. */
function blocked_apps_for_agent(?string $text): array
{
    $out = [];
    foreach (lines($text) as $name) {
        $out[] = $name;
        if (strpos($name, '.') === false) {
            $out[] = $name . '.exe';
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
 * the web root. Computer 0 is the server itself. */
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

/** ---- Fast input channel for remote control ----
 * The owner's mouse/keyboard events ride their own tiny file queue, NOT the commands
 * table, so they never queue up, never need a per-event reply, and reach the computer
 * in about one round trip. The agent drains this queue on a long-poll.
 *
 * Input is only good for a moment. Each event is stamped as it arrives and dropped if
 * the computer has not taken it within INPUT_MAX_AGE seconds, so a click sent when
 * nobody was in control (or during a network gap) can never land later, in another
 * session, on whatever happens to be on screen by then. */
const INPUT_MAX_AGE = 3.0;

function input_push(int $deviceId, array $events): void
{
    $lines = '';
    $at    = round(microtime(true), 3);
    foreach ($events as $e) {
        if (is_array($e)) {
            $e['_at'] = $at;
            $lines .= json_encode($e, JSON_UNESCAPED_SLASHES) . "\n";
        }
    }
    if ($lines === '') {
        return;
    }
    $file = sig_file($deviceId, 'input');
    // If the computer isn't draining it (agent offline), drop the stale backlog first.
    if ((int) @filesize($file) > 65536) {
        @file_put_contents($file, '', LOCK_EX);
    }
    @file_put_contents($file, $lines, FILE_APPEND | LOCK_EX);
    sig_put($deviceId, 'insig', uniqid('', true));   // wake the agent's waiting input poll
}

/** Empty the queue: a new control session starts with nothing left over. */
function input_clear(int $deviceId): void
{
    @file_put_contents(sig_file($deviceId, 'input'), '', LOCK_EX);
}

/** Read and clear the device's input queue in one locked step (so a push and a drain
 * never tread on each other). Returns the events still fresh enough to act on, oldest
 * first. */
function input_drain(int $deviceId): array
{
    $fp = @fopen(sig_file($deviceId, 'input'), 'c+');
    if (!$fp) {
        return [];
    }
    $out = [];
    if (flock($fp, LOCK_EX)) {
        $raw = stream_get_contents($fp);
        ftruncate($fp, 0);
        fflush($fp);
        flock($fp, LOCK_UN);
        $oldest = microtime(true) - INPUT_MAX_AGE;
        foreach (explode("\n", (string) $raw) as $line) {
            if ($line !== '') {
                $e = json_decode($line, true);
                if (is_array($e) && (float) ($e['_at'] ?? 0) >= $oldest) {
                    unset($e['_at']);
                    $out[] = $e;
                }
            }
        }
    }
    fclose($fp);
    return $out;
}

/** Is one process serving every request (PHP's built-in server, as in the README's
 * local run)? Then nothing may wait: a held ping or an open live stream would make
 * the console, and every other computer, stand in line behind it.
 * EM_SINGLE_WORKER=0 or 1 overrides the guess (a test rig that puts several
 * built-in servers behind one address sets 0). */
function single_worker(): bool
{
    $forced = getenv('EM_SINGLE_WORKER');
    if ($forced !== false && $forced !== '') {
        return (bool) (int) $forced;
    }
    return PHP_SAPI === 'cli-server' && (int) getenv('PHP_CLI_SERVER_WORKERS') < 2;
}

/** How many computers are online now. Asked on every ping, so the answer is kept
 * for half a minute. */
function online_count(): int
{
    $p = explode(' ', sig_get(0, 'online'));
    if (count($p) === 2 && (int) $p[0] > time() - 30) {
        return (int) $p[1];
    }
    $n = (int) (q1('SELECT COUNT(*) c FROM devices WHERE active = 1 AND last_seen > NOW() - INTERVAL 3 MINUTE')['c'] ?? 0);
    sig_put(0, 'online', time() . ' ' . $n);
    return $n;
}

/** How many seconds this computer's ping may wait for a new action. 0 = answer now.
 * The agent does its other work between pings, so a wait must never cost it:
 *  - frames are flowing (live, record): never stall them;
 *  - it has blocked applications: they are enforced on each ping, so keep that beat;
 *  - its next activity sample is near: end before it is due, so reports stay right;
 *  - its network cut an earlier wait: leave it alone for an hour;
 *  - there are more computers online than workers to spare: nobody waits. */
function ping_hold(int $id, array $blockedApps, int $sampleSeconds): int
{
    $max = (int) em_config()['ping_hold'];
    if ($max <= 0 || $blockedApps || single_worker()) {
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
    $hold = max(0, min($max, $sample + $sampleSeconds - $now - 4));
    if ($hold > 0 && online_count() > (int) em_config()['ping_hold_max']) {
        return 0;
    }
    return $hold;
}

/** The web site a browser window is on, as far as its title tells. The agent sends
 * the program and the window title, not the address (reading the address needs a
 * browser add-on), so this recognises well-known sites by the name they put in
 * the title, and any title part that is itself a domain. Anything else stays
 * unknown ('') rather than guessed. */
function site_from_title(string $app, string $title): string
{
    static $browsers = ['chrome.exe', 'msedge.exe', 'firefox.exe', 'brave.exe', 'opera.exe', 'vivaldi.exe',
                        'iexplore.exe', 'chromium.exe', 'safari', 'firefox', 'chrome'];
    static $known = [
        'youtube' => 'youtube.com', 'youtube music' => 'music.youtube.com', 'facebook' => 'facebook.com',
        'messenger' => 'messenger.com', 'instagram' => 'instagram.com',
        'instagram photos and videos' => 'instagram.com', 'whatsapp' => 'web.whatsapp.com',
        'whatsapp web' => 'web.whatsapp.com', 'tiktok' => 'tiktok.com', 'x' => 'x.com', 'twitter' => 'x.com',
        'linkedin' => 'linkedin.com', 'gmail' => 'mail.google.com', 'google' => 'google.com',
        'google search' => 'google.com', 'google docs' => 'docs.google.com', 'google sheets' => 'docs.google.com',
        'google slides' => 'docs.google.com', 'google drive' => 'drive.google.com', 'google maps' => 'maps.google.com',
        'google meet' => 'meet.google.com', 'google translate' => 'translate.google.com',
        'google calendar' => 'calendar.google.com', 'netflix' => 'netflix.com', 'prime video' => 'primevideo.com',
        'chatgpt' => 'chatgpt.com', 'claude' => 'claude.ai', 'gemini' => 'gemini.google.com',
        'wikipedia' => 'wikipedia.org', 'github' => 'github.com', 'stack overflow' => 'stackoverflow.com',
        'reddit' => 'reddit.com', 'pinterest' => 'pinterest.com', 'twitch' => 'twitch.tv', 'discord' => 'discord.com',
        'telegram web' => 'web.telegram.org', 'outlook' => 'outlook.com', 'microsoft teams' => 'teams.microsoft.com',
        'zoom' => 'zoom.us', 'canva' => 'canva.com', 'olx pakistan' => 'olx.com.pk',
        'espncricinfo' => 'espncricinfo.com', 'cricbuzz' => 'cricbuzz.com', 'dailymotion' => 'dailymotion.com',
        'spotify' => 'open.spotify.com', 'aliexpress' => 'aliexpress.com', 'fiverr' => 'fiverr.com',
        'upwork' => 'upwork.com', 'bing' => 'bing.com', 'yahoo' => 'yahoo.com', 'quora' => 'quora.com',
    ];
    if (!in_array(strtolower(trim($app)), $browsers, true)) {
        return '';
    }
    $t = trim(str_replace("\u{200B}", '', $title));       // Edge hides a zero-width space in its name
    $t = preg_replace('/\s[-–—]\s(Google Chrome|Microsoft Edge|Mozilla Firefox( Private Browsing)?|Brave|Opera|Vivaldi|Chromium|Internet Explorer)\s*$/iu', '', $t);
    foreach (array_reverse(preg_split('/\s+[-–—|·•\/]\s+/u', (string) $t)) as $part) {
        $part = preg_replace('/^\(\d+\+?\)\s*/', '', trim($part));                // "(3) YouTube"
        $part = strtolower(preg_replace('/\s+and \d+ more pages?$/i', '', $part)); // Edge: "YouTube and 2 more pages"
        if (isset($known[$part])) {
            return $known[$part];
        }
        $first = explode(':', $part, 2)[0];                                       // "Amazon.com: Online Shopping"
        foreach ([$part, $first] as $word) {
            if (preg_match('/^([a-z0-9-]+\.)+[a-z]{2,}$/', $word)) {
                return preg_replace('/^www\./', '', $word);
            }
        }
    }
    return '';
}

/** A statement for one computer's newest rows in a table, newest first (bind the
 * computer's id). Newest means by id - the order the server received them - not by
 * ts, the computer's own clock, so a computer whose clock is wrong can neither
 * hide its newest rows nor freeze on a future-dated one.
 *
 * It names the (device_id, id) index, which answers at once. Left to choose, MySQL
 * reads the whole table backwards by id looking for this computer's rows: fine for
 * one that is on, 0.3 s for one that has been off for a while, on every refresh.
 * A database that "tool.php migrate" has not reached yet has no such index: the
 * plain form then gives the same rows, at the old speed. */
function latest_stmt(string $table, string $cols, int $limit): PDOStatement
{
    $sql = "SELECT $cols FROM $table %s WHERE device_id = ? ORDER BY id DESC LIMIT " . max(1, $limit);
    try {
        return db()->prepare(sprintf($sql, 'FORCE INDEX (dev_latest)'));
    } catch (PDOException $e) {
        return db()->prepare(sprintf($sql, ''));
    }
}

/** The same, run for one computer. */
function latest_rows(string $table, string $cols, int $deviceId, int $limit): array
{
    $st = latest_stmt($table, $cols, $limit);
    $st->execute([$deviceId]);
    return $st->fetchAll();
}

/** The grid's rows: one short indexed look-up per computer. */
function device_rows(): array
{
    $rows = q(
        'SELECT id, hostname, employee_name, group_name, os, agent_version, consent_ack, last_seen,
                (UNIX_TIMESTAMP() - UNIX_TIMESTAMP(last_seen)) AS ago
         FROM devices WHERE active = 1 ORDER BY last_seen DESC'
    );
    $shot = latest_stmt('screenshots', 'path', 1);
    $act  = latest_stmt('activity', 'active_app, window_title, is_idle', 1);
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
    // Taking and handing back control are listed like any other action. Only the old
    // per-event input types are left out (input has its own channel now).
    // 'age' = seconds since it was sent, by the server's own clock, so the page can tell
    // how much of a recording is left without trusting its own clock or time zone.
    return q("SELECT id, type, payload, status, result, created_at, done_at,
                     (UNIX_TIMESTAMP() - UNIX_TIMESTAMP(created_at)) AS age FROM commands
              WHERE device_id = ?
                AND type NOT IN ('mouse_move','mouse_click','mouse_scroll','key')
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

/** Delete one stored screenshot file and the smaller copies made from it. */
function shot_unlink(string $full): void
{
    @unlink($full);
    @unlink($full . '.360.jpg');
    @unlink($full . '.1280.jpg');
    @unlink($full . '.960.jpg');     // an old size, in case any are left
}

/** Never let the screenshots disk fill up, whatever the retention setting. While
 * free space is under $minFree bytes, delete the oldest screenshots (across all
 * computers, in the order the server received them) in batches. Returns how many
 * were removed. */
function disk_guard(int $minFree, int $maxBatches = 400): int
{
    $dir   = em_config()['data_dir'];
    $base  = $dir . '/shots/';
    $freed = 0;
    for ($i = 0; $i < $maxBatches; $i++) {
        clearstatcache();
        $free = @disk_free_space($dir);
        if ($free === false || $free >= $minFree) {
            break;
        }
        $batch = q('SELECT id, path FROM screenshots ORDER BY id ASC LIMIT 500');
        if (!$batch) {
            break;
        }
        foreach ($batch as $row) {
            shot_unlink($base . $row['path']);
        }
        $ids = implode(',', array_map(fn ($r) => (int) $r['id'], $batch));
        db()->exec("DELETE FROM screenshots WHERE id IN ($ids)");
        $freed += count($batch);
    }
    return $freed;
}

/** A live frame that is shown but not kept. Its file has its own name (browsers keep
 * a picture by its name for a month, so a name is never reused) and no database row.
 * The last twenty are left on disk, long enough for a slow reader to fetch them,
 * and the one before that is deleted as each new one arrives. */
function live_frame_remember(int $deviceId, string $dir, string $name): void
{
    $names   = array_values(array_filter(explode("\n", sig_get($deviceId, 'livetmp'))));
    $names[] = $name;
    while (count($names) > 20) {
        $old = array_shift($names);
        if (preg_match('/^t_[\w.]+\.jpg$/', $old)) {
            @unlink($dir . '/' . $old);
        }
    }
    sig_put($deviceId, 'livetmp', implode("\n", $names));
}
