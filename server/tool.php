<?php
// Command-line setup for Employee Monitor. Run with the host's PHP.
//
//   php tool.php migrate                 create the database + tables (db/schema.sql)
//   php tool.php admin <user> <pass> [name] [owner|manager]   create / reset a dashboard login
//   php tool.php enroll [label]          make an enrollment key for one PC
//   php tool.php cleanup                  delete screenshots older than retention_days, and old recordings
//   php tool.php status                   quick counts
//
// Deploy note: set EM_DB_* / EM_DATA_DIR in the environment on the host (or in
// config.local.php, see config.php).

if (PHP_SAPI !== 'cli') {            // a set-up tool, never a web page
    http_response_code(404);
    exit;
}

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
        // so this is optional - ignore the failure. The name is the configured one.
        try {
            connect_server()->exec('CREATE DATABASE IF NOT EXISTS `' . str_replace('`', '', $cfg['db']['name'])
                . '` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci');
        } catch (Throwable $e) { /* already exists, or not ours to create */ }
        // Create the tables inside the database itself. Only whole statements that
        // start a line are dropped, so a comment that happens to hold the word
        // "use" can never take SQL away with it.
        $ddl = preg_replace('/^\s*(CREATE DATABASE|USE)\b[^;]*;/mi', '', $sql);
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
        // Indexes an older database does not have yet. (device_id, id) answers "this
        // computer's newest row" at once; without it the grid slows down as soon as
        // one computer has been off for a while.
        $addkey = function (string $table, string $name, string $cols): void {
            $has = q1('SELECT COUNT(*) c FROM information_schema.STATISTICS
                       WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = ? AND INDEX_NAME = ?', [$table, $name]);
            if ((int) ($has['c'] ?? 0) === 0) {
                db()->exec("ALTER TABLE `$table` ADD KEY `$name` ($cols)");
            }
        };
        $addkey('screenshots', 'dev_latest', 'device_id, id');
        $addkey('activity', 'dev_latest', 'device_id, id');
        $addkey('events', 'type_ts', 'type, ts');
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
            // A new password must end every sign-in made with the old one: whoever
            // had the old password (or a browser left signed in) is out now.
            x('DELETE FROM admin_sessions WHERE admin_id = ?', [$exists['id']]);
            echo "Updated admin '$user' ($role). Everyone signed in as '$user' has been signed out.\n";
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
        $days = (int) em_config()['retention_days'];
        // Delete by recv_at (when the server received it), not ts (the computer's clock),
        // so a screenshot from a computer whose clock is behind is not wiped immediately.
        // In batches, so the first cleanup after a long time cannot run out of memory.
        $base  = em_config()['data_dir'] . '/shots/';
        $gone  = 0;
        do {
            $old = q('SELECT id, path FROM screenshots WHERE recv_at < (NOW() - INTERVAL ? DAY) LIMIT 2000', [$days]);
            foreach ($old as $row) {
                shot_unlink($base . $row['path']);
            }
            if ($old) {
                $ids = implode(',', array_map(fn ($r) => (int) $r['id'], $old));
                db()->exec("DELETE FROM screenshots WHERE id IN ($ids)");
                $gone += count($old);
            }
        } while (count($old) === 2000);
        db()->exec('DELETE FROM activity WHERE recv_at < (NOW() - INTERVAL ' . $days . ' DAY)');
        // Old events and long-idle sessions are cleaned too, so those tables don't grow forever.
        db()->exec('DELETE FROM events WHERE ts < (NOW() - INTERVAL 30 DAY)');
        db()->exec('DELETE FROM admin_sessions WHERE last_seen < (NOW() - INTERVAL 14 DAY)');
        // Old actions are cleared too, so the commands table cannot grow without bound.
        db()->exec('DELETE FROM commands WHERE created_at < (NOW() - INTERVAL 30 DAY)');

        // Notices older than that have reached every computer they ever will: remove
        // their picture and voice (up to 18 MB each) and the notice itself.
        $ndir  = em_config()['data_dir'] . '/notices/';
        $ngone = 0;
        foreach (q('SELECT id FROM notices WHERE created_at < (NOW() - INTERVAL 30 DAY)') as $row) {
            $dir = $ndir . (int) $row['id'];
            foreach ((array) @glob($dir . '/*') as $f) {
                @unlink($f);
            }
            @rmdir($dir);
            $ngone++;
        }
        db()->exec('DELETE FROM notices WHERE created_at < (NOW() - INTERVAL 30 DAY)');

        // Recordings: finish the ones whose time is up, then delete those older than the
        // setting (EM_RECORDING_DAYS) with their pictures. One in which nothing was
        // recorded is only a line on the dashboard: it goes after two days.
        $rgone = 0;
        try {
            foreach (q("SELECT DISTINCT device_id FROM recordings WHERE status IN ('waiting','recording')") as $row) {
                rec_settle((int) $row['device_id']);
            }
            $rdays = max(1, (int) em_config()['recording_days']);
            foreach (q("SELECT id FROM recordings WHERE status IN ('done','failed') AND
                        (created_at < (NOW() - INTERVAL ? DAY) OR (status = 'failed' AND created_at < (NOW() - INTERVAL 2 DAY)))",
                    [$rdays]) as $row) {
                rec_delete((int) $row['id']);
                $rgone++;
            }
            // A folder of pictures whose recording is no longer listed: nothing shows it.
            foreach ((array) @glob(em_config()['data_dir'] . '/recs/*', GLOB_ONLYDIR) as $dir) {
                $rid = basename((string) $dir);
                if (ctype_digit($rid) && !q1('SELECT id FROM recordings WHERE id = ?', [(int) $rid])) {
                    foreach ((array) @glob($dir . '/*') as $f) {
                        @unlink($f);
                    }
                    @rmdir($dir);
                }
            }
        } catch (PDOException $e) { /* no recordings on this database yet: run migrate */ }

        // Passing live frames (shown, not kept) that were still on disk when a live
        // view ended: nothing reads them any more.
        $tgone = 0;
        foreach ((array) @glob($base . '*/t_*.jpg') as $f) {
            if (@filemtime($f) < time() - 3600 && @unlink($f)) {
                $tgone++;
            }
        }

        // Disk guard: whatever the retention settings, never let the pictures disk
        // fill up. If free space is low, delete the oldest pictures (screenshots across
        // all computers, and finished recordings) until there is room again (~600 MB free).
        $freed = disk_guard(600 * 1024 * 1024);
        echo "Removed $gone old screenshots and old activity.\n";
        if ($ngone || $tgone || $rgone) {
            echo "Also removed $ngone old notices, $rgone old recordings and $tgone left-over live frames.\n";
        }
        if ($freed) {
            echo "Disk guard: removed $freed more of the oldest pictures to keep the disk from filling.\n";
        }
        break;

    case 'status':
        echo 'Admins:      ' . (q1('SELECT COUNT(*) c FROM admins')['c'] ?? 0) . "\n";
        echo 'Devices:     ' . (q1('SELECT COUNT(*) c FROM devices')['c'] ?? 0) . "\n";
        echo 'Activity:    ' . (q1('SELECT COUNT(*) c FROM activity')['c'] ?? 0) . "\n";
        echo 'Screenshots: ' . (q1('SELECT COUNT(*) c FROM screenshots')['c'] ?? 0) . "\n";
        try {
            $rec = q1("SELECT COUNT(*) c, COALESCE(SUM(bytes), 0) b FROM recordings WHERE status = 'done'");
            echo 'Recordings:  ' . ($rec['c'] ?? 0) . ' (' . round(($rec['b'] ?? 0) / 1048576) . " MB)\n";
        } catch (PDOException $e) {
            echo "Recordings:  none yet (run: php tool.php migrate)\n";
        }
        $free = @disk_free_space(em_config()['data_dir']);
        if ($free !== false) {
            echo 'Disk free:   ' . round($free / 1048576) . " MB (pictures folder)\n";
        }
        echo 'Time zone:   ' . date_default_timezone_get() . ' (' . date('P') . ")\n";
        break;

    default:
        echo "Commands: migrate | admin <user> <pass> [name] [owner|manager] | enroll [label] | cleanup | status\n";
}
