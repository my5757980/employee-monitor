<?php
// Small HTTP helpers: JSON in/out, random tokens, and the two kinds of caller
// this server has - a signed-in admin (dashboard) and an enrolled device (agent).

function json_out($data, int $status = 200): void
{
    http_response_code($status);
    header('Content-Type: application/json; charset=utf-8');
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
    $row = q1(
        'SELECT a.*, UNIX_TIMESTAMP(s.last_seen) AS sess_seen
         FROM admin_sessions s JOIN admins a ON a.id = s.admin_id WHERE s.token = ?',
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

/** Make a width-capped JPEG copy of a screenshot for fast thumbnails and live
 * frames (needs GD). The copy is cached next to the original, so each size is
 * built once. Returns the file written, the original if it is already small
 * enough, or '' if GD is missing or the source is unreadable (caller then serves
 * the full image). */
function em_make_thumb(string $src, string $dst, int $w): string
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
    $ok = @imagejpeg($t, $dst, 72);
    imagedestroy($t);
    imagedestroy($im);
    return $ok ? $dst : '';
}
