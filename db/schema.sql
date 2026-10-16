-- Employee Monitor - its own MySQL database (empmon), separate from every other project.
-- Workplace monitoring of company-owned computers, with the agent visible to the
-- user (consent_ack) - no hidden/stealth operation.

CREATE DATABASE IF NOT EXISTS empmon CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE empmon;

-- The owner / managers who sign in to the dashboard.
CREATE TABLE IF NOT EXISTS admins (
  id            INT AUTO_INCREMENT PRIMARY KEY,
  username      VARCHAR(64)  NOT NULL UNIQUE,
  password_hash VARCHAR(255) NOT NULL,
  name          VARCHAR(128) NOT NULL DEFAULT '',
  role          ENUM('owner','manager') NOT NULL DEFAULT 'owner',
  created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- A signed-in dashboard session (cookie token).
CREATE TABLE IF NOT EXISTS admin_sessions (
  id         INT AUTO_INCREMENT PRIMARY KEY,
  admin_id   INT NOT NULL,
  token      CHAR(64) NOT NULL UNIQUE,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  last_seen  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY (admin_id)
);

-- A one-time key an admin makes, typed once when installing the agent on a PC.
CREATE TABLE IF NOT EXISTS enroll_keys (
  id         INT AUTO_INCREMENT PRIMARY KEY,
  enroll_key CHAR(32) NOT NULL UNIQUE,
  label      VARCHAR(128) NOT NULL DEFAULT '',
  created_by INT NULL,
  used_by    INT NULL,                       -- the device that used it
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- One row per monitored company computer (one agent install).
CREATE TABLE IF NOT EXISTS devices (
  id            INT AUTO_INCREMENT PRIMARY KEY,
  device_token  CHAR(64) NOT NULL UNIQUE,     -- the agent authenticates with this
  hostname      VARCHAR(190) NOT NULL DEFAULT '',
  employee_name VARCHAR(190) NOT NULL DEFAULT '',
  group_name    VARCHAR(190) NOT NULL DEFAULT '',
  os            VARCHAR(190) NOT NULL DEFAULT '',
  agent_version VARCHAR(32)  NOT NULL DEFAULT '',
  consent_ack   TINYINT NOT NULL DEFAULT 0,   -- the on-screen "this PC is monitored" notice was shown
  active        TINYINT NOT NULL DEFAULT 1,   -- an admin can disable a device
  created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  last_seen     TIMESTAMP NULL,
  KEY (last_seen)
);

-- Periodic activity samples (what app/window was in front, idle or not).
CREATE TABLE IF NOT EXISTS activity (
  id           BIGINT AUTO_INCREMENT PRIMARY KEY,
  device_id    INT NOT NULL,
  ts           TIMESTAMP NOT NULL,
  active_app   VARCHAR(190) NOT NULL DEFAULT '',
  window_title VARCHAR(512) NOT NULL DEFAULT '',
  website      VARCHAR(255) NOT NULL DEFAULT '',
  is_idle      TINYINT NOT NULL DEFAULT 0,
  KEY (device_id, ts)
);

-- Screenshot metadata; the image files live under server/data/shots/<device>/.
CREATE TABLE IF NOT EXISTS screenshots (
  id        BIGINT AUTO_INCREMENT PRIMARY KEY,
  device_id INT NOT NULL,
  ts        TIMESTAMP NOT NULL,
  path      VARCHAR(255) NOT NULL,
  w         INT NOT NULL DEFAULT 0,
  h         INT NOT NULL DEFAULT 0,
  KEY (device_id, ts)
);

-- Actions the admin sends to a computer; the agent picks them up on its next ping.
CREATE TABLE IF NOT EXISTS commands (
  id         BIGINT AUTO_INCREMENT PRIMARY KEY,
  device_id  INT NOT NULL,
  type       VARCHAR(32)  NOT NULL,            -- message | lock | logoff | restart | shutdown | live
  payload    VARCHAR(1000) NOT NULL DEFAULT '',
  status     ENUM('pending','sent','done','failed') NOT NULL DEFAULT 'pending',
  result     VARCHAR(255) NOT NULL DEFAULT '',
  created_by INT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  done_at    TIMESTAMP NULL,
  KEY (device_id, status)
);

-- What a computer is not allowed to run or open (one row per device).
CREATE TABLE IF NOT EXISTS policies (
  device_id     INT PRIMARY KEY,
  blocked_sites TEXT NULL,                     -- domains, one per line
  blocked_apps  TEXT NULL,                     -- executable names, one per line
  updated_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

-- The latest list of running applications on a computer (one row per device,
-- replaced each time; only sent while an admin is watching live).
CREATE TABLE IF NOT EXISTS device_procs (
  device_id INT PRIMARY KEY,
  ts        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  apps      TEXT NULL
);

-- Notable events: agent start/stop, admin login, device enrolled...
CREATE TABLE IF NOT EXISTS events (
  id        BIGINT AUTO_INCREMENT PRIMARY KEY,
  device_id INT NULL,
  ts        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  type      VARCHAR(48) NOT NULL,
  detail    VARCHAR(512) NOT NULL DEFAULT '',
  KEY (device_id, ts)
);
