"""
Employee Monitor - the agent that runs on a company computer.

What it does, openly (no hidden/stealth operation):
  - On first run it shows the employee a notice that the computer is monitored
    for work, and remembers that the notice was shown (consent_ack).
  - Every few seconds it records which application/window is in front and whether
    the computer is idle, and takes one screenshot, and sends them to the server.
  - It never records passwords or key-by-key text, and it only runs on the
    company computer it was installed on.

First run (enrol this PC once):
    python agent.py --server https://monitor.example.com --key ABCD1234...

After that just run:  python agent.py
(When packaged as an .exe the company sets --server/--key in the installer.)

Needs: pip install -r requirements.txt   (mss, Pillow, requests, psutil)
Windows is the main target; it also runs on macOS/Linux with reduced detail.
"""
import argparse
import ctypes
import io
import json
import os
import platform
import re
import socket
import subprocess
import sys
import time

import requests

try:
    import mss
    from PIL import Image
except Exception as e:  # pragma: no cover
    print("Missing libraries. Run: pip install -r requirements.txt")
    raise

AGENT_VERSION = "1.0.0"
IS_WIN = sys.platform.startswith("win")
NO_DIALOG = False  # set by --no-dialog; prints notices instead of pop-ups (tests/services)

# ----- where the agent keeps its config (server URL + this device's token) -----
def config_path():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "EmployeeMonitor")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "agent.json")


def log_path():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "EmployeeMonitor")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "agent.log")


def log(*parts):
    """Print and append to the agent's log file (the packaged exe has no console)."""
    msg = " ".join(str(p) for p in parts)
    try:
        print(msg)
    except Exception:
        pass
    try:
        with open(log_path(), "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except Exception:
        pass


def load_config():
    try:
        with open(config_path(), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg):
    with open(config_path(), "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


# ----- the visible monitoring notice (transparency) -----
def show_notice(text, dialog=True):
    if not dialog:
        print("\n*** " + text + " ***\n")
        return True
    if IS_WIN:
        try:
            ctypes.windll.user32.MessageBoxW(
                0, text, "Company Computer - Monitoring Notice", 0x40)  # MB_ICONINFORMATION
            return True
        except Exception:
            pass
    print("\n*** " + text + " ***\n")
    return True


# ----- what is the user doing right now -----
def active_window():
    """(app, title) of the foreground window."""
    if IS_WIN:
        try:
            import psutil
            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()
            length = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value or ""
            pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            app = ""
            try:
                app = psutil.Process(pid.value).name()
            except Exception:
                pass
            return app, title
        except Exception:
            return "", ""
    # macOS / Linux: best effort
    try:
        import psutil  # noqa
    except Exception:
        pass
    return "", ""


def idle_seconds():
    """Seconds since the last keyboard/mouse input (Windows)."""
    if IS_WIN:
        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            millis = ctypes.windll.kernel32.GetTickCount() - info.dwTime
            return millis / 1000.0
    return 0.0


# A browser's address isn't in its window title, so we only keep the title; a
# later browser-extension phase can add the real URL. This stays honest about
# what it knows.
BROWSERS = {"chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe", "Safari", "firefox"}


def website_from(app, title):
    return ""  # reserved for the browser-extension phase


def take_screenshot(max_width=1280, quality=55):
    with mss.mss() as sct:
        mon = sct.monitors[1]  # primary screen
        raw = sct.grab(mon)
        img = Image.frombytes("RGB", raw.size, raw.rgb)
    w, h = img.size
    if w > max_width:
        img = img.resize((max_width, int(h * max_width / w)))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue(), img.size


# ----- talking to the server -----
class Server:
    def __init__(self, base, token=None):
        self.base = base.rstrip("/")
        self.token = token
        self.s = requests.Session()

    def _headers(self):
        return {"Authorization": "Bearer " + self.token} if self.token else {}

    def enroll(self, key):
        r = self.s.post(self.base + "/api/agent/enroll", timeout=30, json={
            "enroll_key": key,
            "hostname": socket.gethostname(),
            "os": platform.platform(),
            "agent_version": AGENT_VERSION,
        })
        r.raise_for_status()
        return r.json()

    def ping(self, consent_ack):
        r = self.s.post(self.base + "/api/agent/ping", timeout=30,
                        headers=self._headers(), json={"consent_ack": consent_ack})
        r.raise_for_status()
        return r.json()

    def command_result(self, cmd_id, ok, result=""):
        try:
            self.s.post(self.base + "/api/agent/command-result", timeout=30,
                        headers=self._headers(), json={"id": cmd_id, "ok": ok, "result": result})
        except requests.RequestException:
            pass

    def activity(self, sample):
        r = self.s.post(self.base + "/api/agent/activity", timeout=30,
                        headers=self._headers(), json=sample)
        r.raise_for_status()
        return r.json()

    def screenshot(self, data, size, ts):
        r = self.s.post(self.base + "/api/agent/screenshot", timeout=60,
                        headers=self._headers(),
                        files={"shot": ("s.jpg", data, "image/jpeg")},
                        data={"ts": int(ts), "w": size[0], "h": size[1]})
        r.raise_for_status()
        return r.json()


# ----- actions the admin can send -----
def run_command(cmd):
    """Returns (ok, result_text). 'live' is handled by the caller (timing)."""
    t = cmd.get("type")
    payload = cmd.get("payload") or ""
    try:
        if t == "message":
            show_notice("Message from your administrator:\n\n" + payload, dialog=not NO_DIALOG)
            return True, "shown"
        if t == "lock":
            if IS_WIN:
                ctypes.windll.user32.LockWorkStation()
                return True, "locked"
            return False, "windows only"
        if t in ("logoff", "restart", "shutdown"):
            if not IS_WIN:
                return False, "windows only"
            flag = {"logoff": "/l", "restart": "/r", "shutdown": "/s"}[t]
            args = ["shutdown", flag]
            if t != "logoff":
                args += ["/t", "15", "/c", (payload or "Your administrator scheduled this.")[:120]]
            subprocess.run(args, capture_output=True)
            return True, t + " scheduled"
        if t == "live":
            return True, "live"
    except Exception as e:
        return False, str(e)[:120]
    return False, "unknown action"


# ----- policy enforcement (block apps and websites) -----
HOSTS = r"C:\Windows\System32\drivers\etc\hosts"
MARK_BEGIN = "# >>> EmployeeMonitor blocked sites >>>"
MARK_END = "# <<< EmployeeMonitor blocked sites <<<"
_applied_sites = None
_site_block_warned = False


def enforce_blocked_apps(apps):
    if not apps:
        return
    want = {a.lower() for a in apps}
    try:
        import psutil
    except Exception:
        return
    for p in psutil.process_iter(["name"]):
        try:
            name = (p.info["name"] or "").lower()
            if name and name in want:
                p.kill()
        except Exception:
            pass


def enforce_blocked_sites(sites):
    """Point blocked domains at 0.0.0.0 in the hosts file (needs admin)."""
    global _applied_sites, _site_block_warned
    if not IS_WIN:
        return
    key = tuple(sorted(s.lower() for s in sites))
    if key == _applied_sites:
        return
    try:
        with open(HOSTS, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except Exception:
        return
    stripped = re.sub(re.escape(MARK_BEGIN) + r".*?" + re.escape(MARK_END) + r"\n?", "", content, flags=re.S)
    block = ""
    if sites:
        rows = "".join("0.0.0.0 %s\n0.0.0.0 www.%s\n" % (d, d) for d in sites)
        block = MARK_BEGIN + "\n" + rows + MARK_END + "\n"
    new = stripped.rstrip() + "\n" + block
    if new == content:
        _applied_sites = key
        return
    try:
        with open(HOSTS, "w", encoding="utf-8") as f:
            f.write(new)
        subprocess.run(["ipconfig", "/flushdns"], capture_output=True)
        _applied_sites = key
    except PermissionError:
        if not _site_block_warned:
            print("note: website blocking needs the agent to run as administrator.")
            _site_block_warned = True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", help="server base URL, e.g. https://monitor.example.com")
    ap.add_argument("--key", help="enrollment key (first run only)")
    ap.add_argument("--once", action="store_true", help="send one sample and exit (for testing)")
    ap.add_argument("--no-dialog", action="store_true", help="print notices instead of pop-ups (tests/services)")
    args = ap.parse_args()
    global NO_DIALOG
    NO_DIALOG = args.no_dialog

    cfg = load_config()
    base = args.server or cfg.get("server")
    if not base:
        sys.exit("No server set. First run: python agent.py --server <url> --key <key>")

    srv = Server(base, cfg.get("device_token"))

    # Enrol once.
    if not cfg.get("device_token"):
        if not args.key:
            sys.exit("This PC is not enrolled. Run with --key <enrollment key>.")
        log("Enrolling this computer...")
        res = srv.enroll(args.key)
        cfg.update(server=base, device_token=res["device_token"], device_id=res.get("device_id"),
                   sample_seconds=res.get("sample_seconds", 60),
                   consent_notice=res.get("consent_notice", ""), consent_ack=0)
        save_config(cfg)
        srv.token = cfg["device_token"]
        log("Enrolled as device #%s." % cfg.get("device_id"))

    # Show the monitoring notice once, then tell the server it was shown.
    if not cfg.get("consent_ack"):
        show_notice(cfg.get("consent_notice") or
                    "This is a company computer. Activity on it is monitored for work purposes.",
                    dialog=not args.no_dialog)
        cfg["consent_ack"] = 1
        save_config(cfg)

    interval   = int(cfg.get("sample_seconds") or 60)
    srv.token  = cfg["device_token"]
    next_sample = 0.0
    live_until  = 0.0
    log("Monitoring agent running. Sampling every %ss. (This computer is monitored for work.)" % interval)

    while True:
        now = time.time()
        try:
            info = srv.ping(cfg.get("consent_ack", 1))
            if info.get("sample_seconds"):
                interval = int(info["sample_seconds"])

            # Actions the admin sent.
            for cmd in info.get("commands", []):
                ok, result = run_command(cmd)
                if cmd.get("type") == "live" and ok:
                    live_until = now + int(cmd.get("payload") or 60)
                srv.command_result(cmd["id"], ok, result)

            # What this computer may not run / open.
            pol = info.get("policy", {})
            enforce_blocked_apps(pol.get("blocked_apps", []))
            enforce_blocked_sites(pol.get("blocked_sites", []))

            live = now < live_until
            due  = now >= next_sample
            if due or live:
                app, title = active_window()
                idle = idle_seconds() > min(interval, 90)
                if due:
                    srv.activity({
                        "ts": int(now), "active_app": app, "window_title": title,
                        "website": website_from(app, title), "is_idle": 1 if idle else 0,
                    })
                    next_sample = now + interval
                if live or (due and not idle):   # skip a still screen on the slow beat
                    data, size = take_screenshot()
                    srv.screenshot(data, size, now)
        except requests.RequestException as e:
            log("network:", str(e)[:120])
        except Exception as e:
            log("error:", str(e)[:160])

        if args.once:
            break
        # Pick up actions quickly (<=10s), or almost live while the admin is watching.
        time.sleep(2 if now < live_until else max(3, min(interval, 10)))


if __name__ == "__main__":
    main()
