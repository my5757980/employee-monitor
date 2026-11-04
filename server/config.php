<?php
// Employee Monitor - server configuration. Values come from the environment
// (set on the host / cPanel), with local-dev defaults so it runs out of the box.
// Its OWN database - not shared with any other project.

return [
    'db' => [
        'host' => getenv('EM_DB_HOST') ?: '127.0.0.1',
        'port' => (int) (getenv('EM_DB_PORT') ?: 3307),      // local MariaDB test instance
        'name' => getenv('EM_DB_NAME') ?: 'empmon',
        'user' => getenv('EM_DB_USER') ?: 'root',
        'pass' => getenv('EM_DB_PASS') ?: '',
    ],
    // Where screenshot image files are kept (outside public when deployed).
    'data_dir' => getenv('EM_DATA_DIR') ?: __DIR__ . '/data',
    // The monitoring notice the agent must show on the employee's screen.
    'consent_notice' => getenv('EM_CONSENT_NOTICE')
        ?: 'This is a company computer. Activity on it is monitored for work purposes.',
    // How often the agent samples activity / takes a screenshot (seconds).
    'sample_seconds' => (int) (getenv('EM_SAMPLE_SECONDS') ?: 60),
    // Keep screenshots this many days (a cleanup task deletes older ones).
    'retention_days' => (int) (getenv('EM_RETENTION_DAYS') ?: 14),
    // How long a computer's ping may wait for a new action, in seconds, so the
    // action reaches it at once. The agent's own timeout is 30 s. 0 turns this off.
    'ping_hold' => getenv('EM_PING_HOLD') !== false ? (int) getenv('EM_PING_HOLD') : 15,
];
