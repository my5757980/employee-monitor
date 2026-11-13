# Employee Monitor

A workplace computer‑monitoring tool for a company to see what is happening on
its **own** office computers, from anywhere over the internet.

**It is transparent by design.** The agent shows every user of the computer a
notice that the computer is monitored for work, and it does not hide. There is
**no stealth mode and no keystroke logging.** When the owner takes control of a
computer's mouse and keyboard, a red bar on that computer's screen says so for
as long as it lasts. Install it only on company‑owned computers, and tell your
staff (a written policy) that these computers are monitored — that is the law
in most places.

It is a standalone project with **its own code and its own MySQL database**,
separate from anything else.

## The three parts

| Part | What it is | Where it runs |
|---|---|---|
| **agent** (`agent/`) | A small Python program (built to one `.exe`). Sends the active app/window, idle state and a screenshot every minute; runs the owner's actions (notice, lock, log off, restart, shut down, live view, record, remote control) and enforces blocked apps/sites. | each company computer |
| **server** (`server/`) | Plain PHP 8 + MySQL. Stores everything; one API for the agents, one for the dashboard. | your web host (PHP + MySQL) |
| **dashboard** (`server/public/dashboard.html`) | The web page the owner/managers sign in to: live grid of computers, detail (screenshots, playback, activity, the day's report), controls and restrictions. | any browser |

## What the owner can do

- **See**: a grid of all computers, each one's recent screenshots, playback of a
  day, the activity list, and a report of the day's applications and web sites
  (also as CSV). A web site is named from the browser's window title, so
  well‑known sites and titles that contain an address are recognised; the exact
  address is not known.
- **Live view**: a new frame whenever the screen changes (up to about ten a
  second; how many really arrive depends on that computer's upload speed, and
  a slow line gets lighter frames). All of them are shown, one every 10 seconds
  is kept as history. **Fullscreen** is the browser's real full screen, with
  the picture filling it.
- **Take control** of the mouse and keyboard, while the live view is on:
  clicks, dragging, the wheel, typing, shortcuts (Ctrl+C, Alt+F4, F5 …). In
  full screen, Chrome and Edge also pass on the keys Windows otherwise keeps
  (Esc, Alt+Tab, the Windows key); hold Esc, or press *Leave full screen*, to
  get out. The employee sees a red bar the whole time and their own keyboard
  and mouse — built in or plugged in — are held. Control ends at once on Stop,
  when the live view or the owner's page is closed, and by itself within 10
  seconds if the owner's page stops answering. Ctrl+Alt+Del always reaches
  Windows itself and gives the employee their input back; the hold is taken
  again only while the owner is actually working the computer (a click, key or
  mouse move in the last 20 seconds). Trying control out on your **own**
  computer freezes your own mouse: press Ctrl+Alt+Del, then Esc, then *Stop
  Control*.
- **Notice**: a message with an optional picture and voice clip, to one,
  several or all computers. A computer that is switched off shows it when it is
  next on (for up to 3 days).
- **Lock, log off, restart, shut down**, and **record**: a frame every 4
  seconds for five minutes, kept for Playback. The button counts down while it
  records and the same button stops it; the new frames appear in Playback by
  themselves.
- **Restrictions** per computer: programs that are closed as soon as they
  start (`vlc` or `vlc.exe`), and web sites that do not open. A site is blocked
  by name: `youtube.com` also blocks `www.` and `m.youtube.com`; any other name
  of the same site (`music.youtube.com`) goes on its own line.
- **Accounts**: an *owner* can do everything; a *manager* can only watch. The
  owner has a log of sign‑ins and of every action sent.

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
   Open <http://127.0.0.1:8092> and sign in. PHP's built‑in server answers one
   request at a time, so on it nothing is kept waiting: an action reaches a
   computer within 2 seconds instead of at once, and the live view asks for
   frames instead of receiving them as a stream. Everything else is the same.
3. **Agent** — install the Python libraries and run it (first run needs an
   install key, which the dashboard’s “Add computer” button creates):
   ```
   cd agent
   pip install -r requirements.txt
   python agent.py --server http://127.0.0.1:8092 --key <INSTALL-KEY>
   ```

## Deploy the server (owner reaches it from anywhere)

- Put `server/` and `db/` on a host that runs **PHP 8 + MySQL** (the company’s
  cPanel, or a small VPS). **Docroot = `server/public`**, so that `config.php`,
  `tool.php` and the pictures are not reachable from the web. Serve it over
  **HTTPS**.
- **Apache**: `server/public/.htaccess` sends every address to `index.php`
  (needs `mod_rewrite`, and `AllowOverride All` for that folder).
  **nginx**: `try_files $uri /index.php$is_args$args;`
- **Settings**: set the `EM_*` variables below in the host's environment. Where
  that is not possible (shared hosting), put them in `server/config.local.php`:
  ```php
  <?php return ['EM_DB_NAME' => 'cpuser_empmon', 'EM_DB_USER' => '...', 'EM_DB_PASS' => '...'];
  ```
  Keep the pictures folder (`EM_DATA_DIR`) **outside** the public folder.
- Run `php tool.php migrate` and `php tool.php admin <user> <pass>` once on the
  host. Run `migrate` again after every update of the code: it adds what a new
  version needs and changes nothing else.
- Add a daily `php tool.php cleanup` (cron / Task Scheduler). It deletes
  screenshots and activity older than the retention setting, old notices, and
  makes room when the disk is nearly full.
- Each computer's waiting ping keeps one web‑server worker busy for up to 15
  seconds. Give the server more workers than computers, or set
  `EM_PING_HOLD_MAX` to the number it can spare; above that number of computers
  online, pings stop waiting and nothing else changes.

## Put the agent on a computer

1. **Build it once** (on a Windows machine with Python):
   `agent\build.bat` — or `agent\build.bat C:\path\to\python.exe` to say which
   Python. One Python does the whole build, and the fresh `.exe` tests itself;
   a build that lacks a part fails here instead of on an office computer.
   Result: `agent\dist\EmployeeMonitorAgent.exe`.
2. **On the dashboard**, press *Add computer* and create an install key. The box
   shows the line to run. A key adds one computer, once.
3. **On the company computer**, with `EmployeeMonitorAgent.exe`, `install.bat`
   and `uninstall.bat` in one folder, open a Command Prompt **as
   Administrator** and run:
   ```
   install.bat https://your-monitor-domain <INSTALL-KEY>
   ```
   It copies the agent to Program Files, enrols the computer, starts the agent
   and makes it start at every log‑on — for **every** Windows user of that
   computer, on mains or battery. If the agent ever stops, it starts itself
   again. Each user sees the monitoring notice once.

**Update the agent** later with the same command without a key:
`install.bat https://your-monitor-domain`. The computer keeps its place on the
dashboard.

**Remove** a computer on the dashboard and its agent stops reporting. To add it
back, create a new key and run `install.bat` with it. `uninstall.bat` (as
Administrator) removes the agent from the computer.

Good to know:

- Blocking web sites and holding the employee's keyboard and mouse during
  remote control need Windows administrator rights. For a user without them,
  everything else works and the agent's log says what it could not do.
- The agent's log is `%APPDATA%\EmployeeMonitor\agent.log` (per user); the
  installer's is `%ProgramData%\EmployeeMonitor\install.log`.
- An agent from before version 1.3.0 keeps working with a newer server, but
  only version 1.3.0 has the fixes on the computer's side, so update them.

## Configuration (environment variables, or `server/config.local.php`)

| Name | What it sets | Default |
|---|---|---|
| `EM_DB_HOST`, `EM_DB_PORT`, `EM_DB_NAME`, `EM_DB_USER`, `EM_DB_PASS` | the database | `127.0.0.1`, `3306`, `empmon`, `root`, empty |
| `EM_DATA_DIR` | where screenshots and notice files are stored | `server/data` |
| `EM_TIMEZONE` | the zone times are stored and shown in, e.g. `Asia/Karachi` | PHP's own setting |
| `EM_CONSENT_NOTICE` | the notice text the agent shows | a short English notice |
| `EM_SAMPLE_SECONDS` | how often the agent samples | `60` |
| `EM_IDLE_SECONDS` | no keyboard or mouse for this long counts as idle | `180` |
| `EM_RETENTION_DAYS` | how long screenshots and activity are kept | `14` |
| `EM_LIVE_KEEP_SECONDS` | during live view, keep one frame in this many seconds (`0` = every frame) | `10` |
| `EM_NOTICE_HOURS` | how long a notice waits for a computer that is off | `72` |
| `EM_PING_HOLD` | seconds a computer's ping may wait so a new action reaches it at once (`0` = off) | `15` |
| `EM_PING_HOLD_MAX` | with more computers online than this, pings stop waiting | `30` |
| `EM_TRUSTED_PROXIES` | whose `X-Forwarded-For` to believe: `private` (a reverse proxy on this machine or its private network), `none`, or a list of addresses / CIDR ranges | `private` |

The time zone matters: the dashboard shows times as stored, and "today" in the
report is the server's day. The database connection is always set to the same
zone as PHP, so the two cannot disagree.

## Changing the dashboard

The dashboard's styles are compiled into the page. After adding or changing a
Tailwind class in `server/public/dashboard.html`, run
`python server/tools/build_css.py`. See `specs/002-console-speed/` for how the
page stays fast and what the numbers were.
