# Employee Monitor

A workplace computer‑monitoring tool for a company to see what is happening on
its **own** office computers, from anywhere over the internet.

**It is transparent by design.** The agent shows the employee a notice that the
computer is monitored for work, and it does not hide. There is **no stealth mode
and no keystroke logging.** Install it only on company‑owned computers, and tell
your staff (a written policy) that these computers are monitored — that is the
law in most places.

It is a standalone project with **its own code and its own MySQL database**,
separate from anything else.

## The three parts

| Part | What it is | Where it runs |
|---|---|---|
| **agent** (`agent/`) | A small Python program (built to one `.exe`). Sends the active app/window, idle state and a screenshot every so often; runs admin actions (lock, message, log off, restart, shut down, live view) and enforces blocked apps/sites. | each company computer |
| **server** (`server/`) | Plain PHP 8 + MySQL. Stores everything; one API for the agents, one for the dashboard. | your web host (PHP + MySQL) |
| **dashboard** (`server/public/dashboard.html`) | The web page the owner/managers sign in to: live grid of computers, detail (screenshots, activity, today's report), controls and restrictions. | any browser |

## Run it locally (for testing)

1. **Database** — point at a MySQL/MariaDB and create the schema + a login:
   ```
   cd server
   EM_DB_HOST=127.0.0.1 EM_DB_PORT=3306 EM_DB_USER=root EM_DB_PASS=... \
     php tool.php migrate
   php tool.php admin owner "a-good-password" "Office Owner"
   ```
2. **Server** — serve `server/public` with PHP (docroot = `public`):
   ```
   cd server/public
   EM_DB_HOST=127.0.0.1 EM_DB_PORT=3306 EM_DB_USER=root EM_DB_PASS=... \
     EM_DATA_DIR="../data" php -d extension=pdo_mysql -S 127.0.0.1:8092 index.php
   ```
   Open <http://127.0.0.1:8092> and sign in.
3. **Agent** — install the Python libraries and run it (first run needs an
   enrollment key, which the dashboard’s “Add a computer” button creates):
   ```
   cd agent
   pip install -r requirements.txt
   python agent.py --server http://127.0.0.1:8092 --key <ENROLL-KEY>
   ```

## Deploy (owner reaches it from anywhere)

- Put `server/` on a host that runs **PHP 8 + MySQL** (e.g. the company’s cPanel,
  or a small VPS). Docroot = `server/public`. Create a database and set the
  `EM_DB_*` and `EM_DATA_DIR` environment variables (keep `data/` **outside** the
  public folder). Serve it over **HTTPS**.
- Run `php tool.php migrate` and `php tool.php admin <user> <pass>` once on the host.
- Build the agent once (`agent/build.bat`, needs Python + PyInstaller on Windows) →
  `agent/dist/EmployeeMonitorAgent.exe`.
- On each company computer (as Administrator):
  `install.bat https://your-monitor-domain  <ENROLL-KEY>` — it starts the agent at
  every logon and runs it now. `uninstall.bat` removes it.
- Add a daily `php tool.php cleanup` (cron/Task Scheduler) to delete old
  screenshots per the retention setting.

## Configuration (environment variables)

`EM_DB_HOST`, `EM_DB_PORT`, `EM_DB_NAME`, `EM_DB_USER`, `EM_DB_PASS`,
`EM_DATA_DIR` (where screenshots are stored), `EM_CONSENT_NOTICE` (the notice
text the agent shows), `EM_SAMPLE_SECONDS` (how often it samples),
`EM_RETENTION_DAYS` (how long screenshots are kept).
