<?php
// Command-line setup for Employee Monitor. Run with the host's PHP.
//
//   php tool.php migrate                 create the database + tables (db/schema.sql)
//   php tool.php admin <user> <pass> [name] [owner|manager]   create / reset a dashboard login
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
        // Create the database if we have the privilege (local root). On a hosted
        // account the database already exists and the user can't CREATE DATABASE,
        // so this is optional - ignore the failure.
        if (preg_match('/CREATE DATABASE[^;]+;/i', $sql, $m)) {
            try { connect_server()->exec($m[0]); } catch (Throwable $e) { /* already exists */ }
        }
        // Create the tables inside the database itself.
        $ddl = preg_replace('/CREATE DATABASE[^;]+;|USE\s+[^;]+;/i', '', $sql);
        db()->exec($ddl);
        // Add columns an older database does not have yet (safe to run again). recv_at is
        // the server's own receipt time, used for "latest" and for cleanup, so a computer
        // whose clock is wrong can neither hide its newest data nor have it deleted early.
        $addcol = function (string $table, string $col, string $def): void {
            $has = q1('SELECT COUNT(*) c FROM information_schema.COLUMNS
                       WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = ? AND COLUMN_NAME = ?', [$table, $col]);
            if ((int) ($has['c'] ?? 0) === 0) {
                db()->exec("ALTER TABLE `$table` ADD COLUMN `$col` $def");
            }
        };
        $addcol('screenshots', 'recv_at', 'TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP');
        $addcol('activity', 'recv_at', 'TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP');
        echo "Database and tables ready.\n";
        break;

    case 'admin':
        $user = $argv[2] ?? '';
        $pass = $argv[3] ?? '';
        $name = $argv[4] ?? $user;
        $role = strtolower($argv[5] ?? 'owner');
        if ($user === '' || $pass === '') {
            exit("Usage: php tool.php admin <user> <pass> [name] [owner|manager]\n");
        }
        if (!in_array($role, ['owner', 'manager'], true)) {
            exit("Role must be 'owner' (full control) or 'manager' (read-only).\n");
        }
        $hash = password_hash($pass, PASSWORD_DEFAULT);
        $exists = q1('SELECT id FROM admins WHERE username = ?', [$user]);
        if ($exists) {
            x('UPDATE admins SET password_hash = ?, name = ?, role = ? WHERE id = ?', [$hash, $name, $role, $exists['id']]);
            echo "Updated admin '$user' ($role).\n";
        } else {
            x('INSERT INTO admins (username, password_hash, name, role) VALUES (?,?,?,?)', [$user, $hash, $name, $role]);
            echo "Created admin '$user' ($role).\n";
        }
        break;

    case 'enroll':
        $code = enroll_code();
        x('INSERT INTO enroll_keys (enroll_key, label) VALUES (?,?)', [$code, $argv[2] ?? '']);
        echo "Enrollment key: $code\n";
        break;

    case 'cleanup':
        $days = em_config()['retention_days'];
        // Delete by recv_at (when the server received it), not ts (the computer's clock),
        // so a screenshot from a computer whose clock is behind is not wiped immediately.
        $old  = q('SELECT id, path FROM screenshots WHERE recv_at < (NOW() - INTERVAL ? DAY)', [$days]);
        $base = em_config()['data_dir'] . '/shots/';
        foreach ($old as $row) {
            @unlink($base . $row['path']);
            @unlink($base . $row['path'] . '.360.jpg');   // cached thumbnails
            @unlink($base . $row['path'] . '.1280.jpg');
            @unlink($base . $row['path'] . '.960.jpg');   // old size, clean up any left over
        }
        if ($old) {
            $ids = implode(',', array_map(fn ($r) => (int) $r['id'], $old));
            db()->exec("DELETE FROM screenshots WHERE id IN ($ids)");
        }
        db()->exec('DELETE FROM activity WHERE recv_at < (NOW() - INTERVAL ' . (int) $days . ' DAY)');
        // Old events and long-idle sessions are cleaned too, so those tables don't grow forever.
        db()->exec('DELETE FROM events WHERE ts < (NOW() - INTERVAL 30 DAY)');
        db()->exec('DELETE FROM admin_sessions WHERE last_seen < (NOW() - INTERVAL 14 DAY)');
        // Old actions are cleared too, so the commands table cannot grow without bound.
        db()->exec('DELETE FROM commands WHERE created_at < (NOW() - INTERVAL 30 DAY)');

        // Disk guard: whatever the retention setting, never let the screenshots disk
        // fill up. If free space is low, delete the oldest screenshots (across all
        // computers) in batches until there is room again.
        $dir     = em_config()['data_dir'];
        $minFree = 600 * 1024 * 1024;   // keep ~600 MB free
        $freed   = 0;
        clearstatcache();
        while (($free = @disk_free_space($dir)) !== false && $free < $minFree && $freed <= 200000) {
            $batch = q('SELECT id, path FROM screenshots ORDER BY ts ASC LIMIT 500');
            if (!$batch) {
                break;
            }
            foreach ($batch as $row) {
                @unlink($base . $row['path']);
                @unlink($base . $row['path'] . '.360.jpg');
                @unlink($base . $row['path'] . '.1280.jpg');
            }
            $ids = implode(',', array_map(fn ($r) => (int) $r['id'], $batch));
            db()->exec("DELETE FROM screenshots WHERE id IN ($ids)");
            $freed += count($batch);
            clearstatcache();
        }
        echo 'Removed ' . count($old) . " old screenshots and old activity.\n";
        if ($freed) {
            echo "Disk guard: removed $freed more oldest screenshots to keep the disk from filling.\n";
        }
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
