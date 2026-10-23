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
import tempfile
import threading
import time
import urllib.request

import requests

# Trust the operating system's certificate store (the same roots the browser
# uses) so the agent works behind corporate TLS-inspection proxies / firewalls
# and with custom company root CAs - otherwise HTTPS fails with a cert error
# even though the browser opens the site fine.
try:
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass

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


# ----- a richer notice from the owner: text + picture on screen, voice plays -----
def show_notice_rich(body, image_path=None, voice_path=None):
    """A visible, dismissable announcement from the owner - the same disclosed
    notice as the Message button, now with an optional picture shown and a voice
    clip played. Runs in its own thread so monitoring keeps going; falls back to
    a plain message box where there is no screen (a service/test session)."""
    def _run():
        if voice_path:
            try:
                import winsound
                winsound.PlaySound(voice_path, winsound.SND_FILENAME | winsound.SND_ASYNC)
            except Exception:
                try:
                    os.startfile(voice_path)       # no WAV support -> the default player
                except Exception:
                    pass
        try:
            import tkinter as tk
            win = tk.Tk()
            win.title("Message from your administrator")
            win.attributes("-topmost", True)
            frm = tk.Frame(win, padx=20, pady=18)
            frm.pack(fill="both", expand=True)
            tk.Label(frm, text="Message from your administrator",
                     font=("Segoe UI", 13, "bold"), fg="#1d4ed8").pack(anchor="w")
            if body:
                tk.Label(frm, text=body, font=("Segoe UI", 11), wraplength=540,
                         justify="left").pack(anchor="w", pady=(10, 0))
            if image_path:
                try:
                    from PIL import Image, ImageTk
                    im = Image.open(image_path)
                    im.thumbnail((560, 420))
                    photo = ImageTk.PhotoImage(im)
                    lbl = tk.Label(frm, image=photo)
                    lbl.image = photo                # keep a reference so it is not garbage-collected
                    lbl.pack(pady=(12, 0))
                except Exception:
                    pass

            def close():
                try:
                    import winsound
                    winsound.PlaySound(None, winsound.SND_PURGE)   # stop the voice
                except Exception:
                    pass
                win.destroy()

            tk.Button(frm, text="Close", command=close, padx=18, pady=5,
                      font=("Segoe UI", 10)).pack(pady=(16, 0))
            win.update_idletasks()
            w, h = win.winfo_width(), win.winfo_height()
            sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
            win.geometry("+%d+%d" % (max(0, (sw - w) // 2), max(0, (sh - h) // 3)))
            win.mainloop()
        except Exception:
            show_notice("Message from your administrator:\n\n"
                        + (body or "(a picture or voice was sent)"), dialog=not NO_DIALOG)

    threading.Thread(target=_run, daemon=True).start()
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
        # Use the computer's own proxy settings (same as the browser). Many
        # office networks only reach the internet through a proxy; without this
        # the agent can't connect even though the browser can.
        try:
            proxies = urllib.request.getproxies()
            if proxies:
                self.s.proxies.update(proxies)
        except Exception:
            pass

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

    def procs(self, apps):
        try:
            self.s.post(self.base + "/api/agent/procs", timeout=30,
                        headers=self._headers(), json={"apps": apps})
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

    def get_json(self, path):
        r = self.s.get(self.base + path, timeout=30, headers=self._headers())
        r.raise_for_status()
        return r.json()

    def download(self, path, suffix=""):
        """Fetch a file (a notice's picture or voice) to a temp file; return its path."""
        r = self.s.get(self.base + path, timeout=60, headers=self._headers())
        r.raise_for_status()
        fd, dest = tempfile.mkstemp(prefix="emnotice_", suffix=suffix)
        with os.fdopen(fd, "wb") as f:
            f.write(r.content)
        return dest


# ----- actions the admin can send -----
def run_command(cmd, srv=None):
    """Returns (ok, result_text). 'live' is handled by the caller (timing)."""
    t = cmd.get("type")
    payload = cmd.get("payload") or ""
    try:
        if t == "message":
            show_notice("Message from your administrator:\n\n" + payload, dialog=not NO_DIALOG)
            return True, "shown"
        if t == "notice":
            if srv is None:
                return False, "no server"
            try:
                nid  = int(payload or 0)
                meta = srv.get_json("/api/agent/notice?id=%d" % nid)
            except Exception as e:
                return False, "fetch failed: " + str(e)[:80]
            image = srv.download("/api/agent/notice-media?id=%d&kind=image" % nid, ".img") if meta.get("image") else None
            voice = srv.download("/api/agent/notice-media?id=%d&kind=voice" % nid, ".wav") if meta.get("voice") else None
            show_notice_rich(meta.get("body") or "", image, voice)
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


def running_apps():
    """Distinct program names currently running (for the admin's 'running apps' list)."""
    try:
        import psutil
    except Exception:
        return []
    seen = {}
    for p in psutil.process_iter(["name"]):
        try:
            n = (p.info["name"] or "").strip()
            if n:
                seen[n.lower()] = n
        except Exception:
            pass
    return sorted(seen.values(), key=lambda s: s.lower())[:80]


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


def enroll_and_save(srv, cfg, base, key):
    """Enroll with the server and persist the new device token. Needs a key."""
    log("Enrolling this computer...")
    res = srv.enroll(key)
    cfg.update(server=base, device_token=res["device_token"], device_id=res.get("device_id"),
               sample_seconds=res.get("sample_seconds", 60),
               consent_notice=res.get("consent_notice", ""), consent_ack=0)
    save_config(cfg)
    srv.token = cfg["device_token"]
    log("Enrolled as device #%s." % cfg.get("device_id"))
    return res


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

    # Enrol once - and re-enrol if this PC was last pointed at a different server
    # (a saved device token only works on the server that issued it).
    saved_server = (cfg.get("server") or "").rstrip("/")
    server_changed = bool(cfg.get("device_token")) and saved_server != base.rstrip("/")
    if server_changed:
        log("Server changed (%s -> %s); re-enrolling." % (saved_server, base.rstrip("/")))
        cfg["device_token"] = None
        srv.token = None
    # Enroll, retrying on any failure so a network blip or proxy hiccup is logged
    # and recovered from instead of silently killing the agent.
    while not cfg.get("device_token"):
        if not args.key:
            sys.exit("This PC is not enrolled. Run with --key <enrollment key>.")
        try:
            enroll_and_save(srv, cfg, base, args.key)
        except Exception as e:
            log("enroll failed:", str(e)[:180])
            if args.once:
                sys.exit("Enroll failed (see log).")
            time.sleep(30)

    # Show the monitoring notice once, then tell the server it was shown.
    if not cfg.get("consent_ack"):
        show_notice(cfg.get("consent_notice") or
                    "This is a company computer. Activity on it is monitored for work purposes.",
                    dialog=not args.no_dialog)
        cfg["consent_ack"] = 1
        save_config(cfg)

    interval   = int(cfg.get("sample_seconds") or 60)
    srv.token  = cfg["device_token"]
    next_sample  = 0.0
    live_until   = 0.0
    record_until = 0.0
    last_procs   = 0.0
    last_ping    = 0.0
    log("Monitoring agent running. Sampling every %ss. (This computer is monitored for work.)" % interval)

    while True:
        now = time.time()
        try:
            live      = now < live_until
            recording = now < record_until
            # Pick up commands/policy on a ping. While live we keep sending frames
            # every loop, but only ping about once a second, so the frame rate isn't
            # tied to the ping round-trip - this keeps the live view smooth.
            if (not live) or (now - last_ping >= 1.0):
                info = srv.ping(cfg.get("consent_ack", 1))
                last_ping = now
                if info.get("sample_seconds"):
                    interval = int(info["sample_seconds"])

                # Actions the admin sent.
                for cmd in info.get("commands", []):
                    ok, result = run_command(cmd, srv)
                    if ok and cmd.get("type") == "live":
                        live_until = now + int(cmd.get("payload") or 60)
                    if ok and cmd.get("type") == "record":
                        record_until = now + int(cmd.get("payload") or 5) * 60
                    srv.command_result(cmd["id"], ok, result)

                # What this computer may not run / open.
                pol = info.get("policy", {})
                enforce_blocked_apps(pol.get("blocked_apps", []))
                enforce_blocked_sites(pol.get("blocked_sites", []))
                live      = now < live_until
                recording = now < record_until

            due = now >= next_sample
            if due or live or recording:
                app, title = active_window()
                idle = idle_seconds() > min(interval, 90)
                if due:
                    srv.activity({
                        "ts": int(now), "active_app": app, "window_title": title,
                        "website": website_from(app, title), "is_idle": 1 if idle else 0,
                    })
                    next_sample = now + interval
                # Live frames are small so they arrive fast; a recording keeps full
                # frames; the slow beat skips a still screen.
                if live:
                    data, size = take_screenshot(760, 32)
                    srv.screenshot(data, size, now)
                    if now - last_procs > 4:          # the running-apps list, while watching
                        srv.procs(running_apps())
                        last_procs = now
                elif recording or (due and not idle):
                    data, size = take_screenshot()
                    srv.screenshot(data, size, now)
        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else 0
            if code == 401 and args.key:
                # Our token was rejected (server reset, device removed, re-pointed).
                # Heal automatically by enrolling again with the key.
                log("Session rejected (401); re-enrolling.")
                try:
                    cfg["device_token"] = None
                    srv.token = None
                    enroll_and_save(srv, cfg, base, args.key)
                    cfg["consent_ack"] = 1   # notice was already shown on this PC
                    save_config(cfg)
                except Exception as e2:
                    log("re-enroll failed:", str(e2)[:120])
            else:
                log("network:", str(e)[:120])
        except requests.RequestException as e:
            log("network:", str(e)[:120])
        except Exception as e:
            log("error:", str(e)[:160])

        if args.once:
            break
        # Stream frames as fast as they upload while the admin watches, dense while
        # recording (4s), otherwise just often enough to pick up actions (2s).
        time.sleep(0.25 if now < live_until else (4 if now < record_until else 2))


if __name__ == "__main__":
    main()