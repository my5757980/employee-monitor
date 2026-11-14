<?php
// Employee Monitor - server configuration. Values come from the environment (set
// on the host), with defaults so it runs out of the box.
// Its OWN database - not shared with any other project.
//
// On a host where environment variables cannot be set (shared cPanel hosting), put
// the same names in a file called config.local.php beside this one. It is not kept
// in git, and it wins over the environment:
//     <?php return ['EM_DB_NAME' => 'cpuser_empmon', 'EM_DB_USER' => '...', 'EM_DB_PASS' => '...'];

$local = is_file(__DIR__ . '/config.local.php') ? (array) require __DIR__ . '/config.local.php' : [];

// One setting, or the default when it is not set at all. '0' and '' are real answers.
$get = function (string $key, $default) use ($local) {
    if (array_key_exists($key, $local)) {
        return $local[$key];
    }
    $v = getenv($key);
    return $v === false ? $default : $v;
};

return [
    'db' => [
        'host' => $get('EM_DB_HOST', '127.0.0.1') ?: '127.0.0.1',
        'port' => (int) ($get('EM_DB_PORT', 3306) ?: 3306),
        'name' => $get('EM_DB_NAME', 'empmon') ?: 'empmon',
        'user' => $get('EM_DB_USER', 'root') ?: 'root',
        'pass' => (string) $get('EM_DB_PASS', ''),
    ],
    // Where screenshot image files are kept (outside public when deployed).
    'data_dir' => $get('EM_DATA_DIR', '') ?: __DIR__ . '/data',
    // The zone times are stored and shown in, e.g. Asia/Karachi. Empty = PHP's own
    // setting (date.timezone). The database connection is always set to match, so
    // a host whose PHP and MySQL disagree cannot store times hours apart.
    'timezone' => (string) $get('EM_TIMEZONE', ''),
    // The monitoring notice the agent must show on the employee's screen.
    'consent_notice' => $get('EM_CONSENT_NOTICE', '')
        ?: 'This is a company computer. Activity on it is monitored for work purposes.',
    // How often the agent samples activity / takes a screenshot (seconds).
    'sample_seconds' => (int) ($get('EM_SAMPLE_SECONDS', 60) ?: 60),
    // No keyboard or mouse for this long counts as idle (seconds).
    'idle_seconds' => (int) ($get('EM_IDLE_SECONDS', 180) ?: 180),
    // Keep screenshots this many days (a cleanup task deletes older ones).
    'retention_days' => (int) ($get('EM_RETENTION_DAYS', 14) ?: 14),
    // While the owner watches live, every frame is shown but only one in this many
    // seconds is kept as history, so an hour of watching cannot fill the disk.
    // 0 keeps every live frame.
    'live_keep_seconds' => (int) $get('EM_LIVE_KEEP_SECONDS', 10),
    // A recording (the Record button) is kept this many days, then deleted with its
    // pictures. The owner can download one as a video file to keep it longer.
    'recording_days' => (int) ($get('EM_RECORDING_DAYS', 30) ?: 30),
    // A notice sent to a computer that is switched off waits this many hours for it.
    'notice_hours' => (int) ($get('EM_NOTICE_HOURS', 72) ?: 72),
    // How long a computer's ping may wait for a new action, in seconds, so the
    // action reaches it at once. The agent's own timeout is 30 s. 0 turns this off.
    'ping_hold' => (int) $get('EM_PING_HOLD', 15),
    // Each waiting ping keeps one web-server worker busy. With more computers
    // online than this, pings stop waiting (actions then take up to 2 s), so the
    // console can never be starved of workers.
    'ping_hold_max' => (int) $get('EM_PING_HOLD_MAX', 30),
    // Whose X-Forwarded-For header to believe when finding a caller's address:
    // 'private' = a reverse proxy on this machine or its private network (the
    // usual set-up), 'none' = nobody, or a comma list of addresses / CIDR ranges.
    'trusted_proxies' => (string) $get('EM_TRUSTED_PROXIES', 'private'),
];
