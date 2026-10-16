<?php
// Command-line setup for Employee Monitor. Run with the host's PHP.
//
//   php tool.php migrate                 create the database + tables (db/schema.sql)
//   php tool.php admin <user> <pass> [name]   create / reset a dashboard login
//   php tool.php enroll [label]          make an enrollment key for one PC
//   php tool.php cleanup                  delete screenshots older than retention_days
//   php tool.php status                   quick counts
//
// Deploy note: set EM_DB_* / EM_DATA_DIR in the environment on the host.

require __DIR__ . '/src/db.php';
require __DIR__ . '/src/http.php';

$cmd = $argv[1] ?? '';
$cfg = em_config();

function connect_server(): PDO
{
    $c = em_config()['db'];
    return new PDO(sprintf('mysql:host=%s;port=%d;charset=utf8mb4', $c['host'], $c['port']),
        $c['user'], $c['pass'], [PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION]);
}

switch ($cmd) {
    case 'migrate':
        $sql = file_get_contents(__DIR__ . '/../db/schema.sql');
        connect_server()->exec($sql);
        echo "Database and tables ready.\n";
        break;

    case 'admin':
        $user = $argv[2] ?? '';
        $pass = $argv[3] ?? '';
        $name = $argv[4] ?? $user;
        if ($user === '' || $pass === '') {
            exit("Usage: php tool.php admin <user> <pass> [name]\n");
        }
        $hash = password_hash($pass, PASSWORD_DEFAULT);
        $exists = q1('SELECT id FROM admins WHERE username = ?', [$user]);
        if ($exists) {
            x('UPDATE admins SET password_hash = ?, name = ? WHERE id = ?', [$hash, $name, $exists['id']]);
            echo "Updated admin '$user'.\n";
        } else {
            x('INSERT INTO admins (username, password_hash, name, role) VALUES (?,?,?,?)', [$user, $hash, $name, 'owner']);
            echo "Created admin '$user'.\n";
        }
        break;

    case 'enroll':
        $code = enroll_code();
        x('INSERT INTO enroll_keys (enroll_key, label) VALUES (?,?)', [$code, $argv[2] ?? '']);
        echo "Enrollment key: $code\n";
        break;

    case 'cleanup':
        $days = em_config()['retention_days'];
        $old  = q('SELECT id, path FROM screenshots WHERE ts < (NOW() - INTERVAL ? DAY)', [$days]);
        $base = em_config()['data_dir'] . '/shots/';
        foreach ($old as $row) {
            @unlink($base . $row['path']);
        }
        if ($old) {
            $ids = implode(',', array_map(fn ($r) => (int) $r['id'], $old));
            db()->exec("DELETE FROM screenshots WHERE id IN ($ids)");
        }
        db()->exec('DELETE FROM activity WHERE ts < (NOW() - INTERVAL ' . (int) $days . ' DAY)');
        echo 'Removed ' . count($old) . " old screenshots and old activity.\n";
        break;

    case 'status':
        echo 'Admins:      ' . (q1('SELECT COUNT(*) c FROM admins')['c'] ?? 0) . "\n";
        echo 'Devices:     ' . (q1('SELECT COUNT(*) c FROM devices')['c'] ?? 0) . "\n";
        echo 'Activity:    ' . (q1('SELECT COUNT(*) c FROM activity')['c'] ?? 0) . "\n";
        echo 'Screenshots: ' . (q1('SELECT COUNT(*) c FROM screenshots')['c'] ?? 0) . "\n";
        break;

    default:
        echo "Commands: migrate | admin <user> <pass> [name] | enroll [label] | cleanup | status\n";
}
