<?php
// One PDO connection to the employee-monitor database, shared per request.

function em_config(): array
{
    static $cfg = null;
    if ($cfg === null) {
        $cfg = require __DIR__ . '/../config.php';
    }
    return $cfg;
}

function db(): PDO
{
    if (empty($GLOBALS['em_pdo'])) {
        $c = em_config()['db'];
        $dsn = sprintf('mysql:host=%s;port=%d;dbname=%s;charset=utf8mb4', $c['host'], $c['port'], $c['name']);
        $GLOBALS['em_pdo'] = new PDO($dsn, $c['user'], $c['pass'], [
            PDO::ATTR_ERRMODE            => PDO::ERRMODE_EXCEPTION,
            PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
            PDO::ATTR_EMULATE_PREPARES   => false,
        ]);
    }
    return $GLOBALS['em_pdo'];
}

/** Give the connection back while a request only waits (held ping, live stream).
 * The next db() call opens a new one. */
function db_close(): void
{
    $GLOBALS['em_pdo'] = null;
}

/** Run a query and return all rows. */
function q(string $sql, array $args = []): array
{
    $st = db()->prepare($sql);
    $st->execute($args);
    return $st->fetchAll();
}

/** Run a query and return the first row (or null). */
function q1(string $sql, array $args = []): ?array
{
    $st = db()->prepare($sql);
    $st->execute($args);
    $row = $st->fetch();
    return $row === false ? null : $row;
}

/** Run an insert/update/delete; return the new id for inserts. */
function x(string $sql, array $args = []): int
{
    $st = db()->prepare($sql);
    $st->execute($args);
    return (int) db()->lastInsertId();
}
