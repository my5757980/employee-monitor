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

Needs: pip install -r requirements.txt   (mss, Pillow, requests, psutil, pynput)
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

# Optional: remote control (mouse + keyboard). If pynput is missing the agent
# still runs normally; only remote_start / mouse_* / key commands will fail.
try:
    from pynput.mouse import Controller as MouseController, Button
    from pynput.keyboard import Controller as KeyboardController, Key
    HAS_REMOTE = True
except Exception:
    HAS_REMOTE = False

AGENT_VERSION = "1.2.0"
IS_WIN = sys.platform.startswith("win")
NO_DIALOG = False  # set by --no-dialog; prints notices instead of pop-ups (tests/services)

# Remote control (mouse + keyboard) is only ever carried out while the owner is also
# watching live, and while it is on the employee sees a clear banner on their screen.
# The main loop keeps these up to date; run_command reads REMOTE_ACTIVE as the gate.
remote_requested = False   # the owner pressed "Take Control" (cleared by Stop / closing live)
REMOTE_ACTIVE = False      # remote_requested AND the owner is watching live right now

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


_last_log = {"msg": None, "n": 0}


def log(*parts):
    """Print and append to the agent's log file (the packaged exe has no console).
    Keeps the file from growing without bound (rotates past ~2 MB), and collapses a
    flood of identical lines - e.g. the same network error every couple of seconds -
    so the log stays small and readable even over days offline."""
    msg = " ".join(str(p) for p in parts)
    try:
        print(msg)
    except Exception:
        pass
    # Collapse repeats: write the first few, then only every 100th, noting the count.
    if msg == _last_log["msg"]:
        _last_log["n"] += 1
        if _last_log["n"] > 3 and _last_log["n"] % 100 != 0:
            return
        if _last_log["n"] % 100 == 0:
            msg = msg + "  (x%d)" % _last_log["n"]
    else:
        _last_log["msg"] = msg
        _last_log["n"] = 1
    try:
        path = log_path()
        try:
            if os.path.getsize(path) > 2 * 1024 * 1024:
                old = path + ".old"
                if os.path.exists(old):
                    os.remove(old)
                os.replace(path, old)
        except OSError:
            pass
        with open(path, "a", encoding="utf-8") as f:
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
                    # Tie the image to THIS window's interpreter. Without a master it binds
                    # to a stale default root, so a second notice's picture fails to load
                    # and the whole agent can crash (Tcl_AsyncDelete) when the first closes.
                    photo = ImageTk.PhotoImage(im, master=win)
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
        finally:
            # The picture and voice were downloaded to TEMP only to show this one notice;
            # remove them now so they don't pile up on the computer for good.
            for _p in (image_path, voice_path):
                if _p:
                    try:
                        os.remove(_p)
                    except OSError:
                        pass

    threading.Thread(target=_run, daemon=True).start()
    return True


# ----- remote control: screen coordinates, and the "being controlled" banner -----
_banner = {"thread": None, "stop": None}


def _screen_size():
    if IS_WIN:
        try:
            u = ctypes.windll.user32
            w, h = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
            if w and h:
                return w, h
        except Exception:
            pass
    return 1920, 1080


def _to_xy(payload):
    """'fx,fy' as fractions 0..1 of the screen -> absolute primary-screen pixels, so a
    click lands in the right place whatever the real screen size is. An old absolute
    'x,y' (values above 1) is still accepted."""
    parts = payload.replace(" ", "").split(",")
    a, b = float(parts[0]), float(parts[1])
    if 0.0 <= a <= 1.0 and 0.0 <= b <= 1.0:
        w, h = _screen_size()
        return int(round(a * (w - 1))), int(round(b * (h - 1)))
    return int(a), int(b)


def set_control_banner(on):
    """Show or hide a visible, always-on-top bar telling the employee that their computer
    is being controlled. This product is transparent by design, so control is never
    silent. Runs in its own thread; a plain text window, closed from that same thread."""
    if on and not _banner["thread"]:
        stop = threading.Event()

        def _run():
            try:
                import tkinter as tk
                win = tk.Tk()
                win.overrideredirect(True)
                win.attributes("-topmost", True)
                try:
                    win.attributes("-alpha", 0.92)
                except Exception:
                    pass
                win.geometry("%dx34+0+0" % win.winfo_screenwidth())
                win.configure(bg="#b91c1c")
                tk.Label(win, text="●  Your administrator is viewing and controlling this computer.",
                         bg="#b91c1c", fg="white", font=("Segoe UI", 11, "bold")).pack(fill="both", expand=True)

                def check():
                    if stop.is_set():
                        try:
                            win.destroy()
                        except Exception:
                            pass
                        return
                    win.after(200, check)

                win.after(200, check)
                win.mainloop()
            except Exception:
                pass

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        _banner["thread"] = t
        _banner["stop"] = stop
    elif not on and _banner["thread"]:
        try:
            _banner["stop"].set()
        except Exception:
            pass
        _banner["thread"] = None
        _banner["stop"] = None


# ----- the control session: hold back local input, apply the owner's input -----
# While the owner is in control, the employee's own keyboard and mouse are held back so
# the two don't fight over the cursor. This can never trap anyone:
#   - the red banner stays up the whole time (control is never silent);
#   - input is handed back the instant the owner presses Stop or closes the live view;
#   - if the agent loses contact with the server, input is returned within a few seconds;
#   - if the agent is closed or crashes, Windows restores input by itself;
#   - Ctrl+Alt+Del always works, so there is always a way out.
def block_input(on):
    """Hold back (or restore) this computer's own keyboard + mouse. Returns True if it
    took effect. Must be called from the same thread that injects the owner's input."""
    if not IS_WIN:
        return False
    try:
        return bool(ctypes.windll.user32.BlockInput(bool(on)))
    except Exception:
        return False


_SPECIAL_KEYS = {}


def _frac_xy(fx, fy):
    """A fraction 0..1 of the screen -> absolute primary-screen pixels (or absolute if a
    value is already above 1)."""
    try:
        a, b = float(fx), float(fy)
    except (TypeError, ValueError):
        return None
    if a > 1.0 or b > 1.0:
        return int(a), int(b)
    w, h = _screen_size()
    return int(round(a * (w - 1))), int(round(b * (h - 1)))


def _apply_key(name):
    if not HAS_REMOTE:
        return
    global _SPECIAL_KEYS
    if not _SPECIAL_KEYS:
        _SPECIAL_KEYS = {
            "enter": Key.enter, "tab": Key.tab, "esc": Key.esc, "escape": Key.esc,
            "backspace": Key.backspace, "delete": Key.delete, "up": Key.up, "down": Key.down,
            "left": Key.left, "right": Key.right, "ctrl": Key.ctrl, "alt": Key.alt,
            "shift": Key.shift, "space": Key.space, "home": Key.home, "end": Key.end,
            "pageup": Key.page_up, "pagedown": Key.page_down,
        }
    k = _SPECIAL_KEYS.get(str(name).strip().lower(), name)
    kb = KeyboardController()
    kb.press(k)
    kb.release(k)


def _apply_events(events):
    """Carry out a batch of the owner's input. Consecutive mouse moves are coalesced (only
    the last is applied) so a fast drag never backs up. Returns 'stop' if a stop was sent."""
    n = len(events)
    for i, e in enumerate(events):
        if not isinstance(e, dict):
            continue
        t = e.get("t")
        try:
            if t == "m":
                if i + 1 < n and isinstance(events[i + 1], dict) and events[i + 1].get("t") == "m":
                    continue                       # a newer move is right behind: skip this one
                xy = _frac_xy(e.get("x"), e.get("y"))
                if xy:
                    MouseController().position = xy
            elif t == "c":
                m = MouseController()
                if e.get("x") is not None:
                    xy = _frac_xy(e.get("x"), e.get("y"))
                    if xy:
                        m.position = xy
                m.click(Button.right if str(e.get("b")).lower() == "right" else Button.left)
            elif t == "s":
                MouseController().scroll(0, int(e.get("d") or 0))
            elif t == "k":
                _apply_key(e.get("k"))
            elif t == "stop":
                return "stop"
        except Exception as ex:
            log("control: could not apply input:", str(ex)[:80])
    return None


_control = {"thread": None, "stop": None}


def _control_loop(srv, stop_event):
    """Runs while the owner is in control: holds back local input, long-polls the owner's
    events, and applies them. All injection happens on THIS thread - the one that blocked
    input - so the owner's input gets through while the employee's is held back."""
    global remote_requested
    blocked = block_input(True)
    log("control: started" + (", local input held." if blocked else " (could not hold local input - agent may not be elevated)."))
    last_ok = time.time()
    try:
        while not stop_event.is_set():
            try:
                events = srv.input_poll()
                last_ok = time.time()
                if (not blocked) and IS_WIN:          # a network gap had released it; re-hold
                    blocked = block_input(True)
                if _apply_events(events) == "stop":
                    remote_requested = False          # tell the main loop control is over
                    break
            except requests.RequestException:
                # Failsafe: lost contact -> give the employee their input back after a few
                # seconds, so a dropped network can never leave them locked out.
                if blocked and time.time() - last_ok > 4:
                    block_input(False)
                    blocked = False
                    log("control: lost contact, handed input back (failsafe).")
                time.sleep(0.3)
            except Exception as e:
                log("control loop error:", str(e)[:120])
                time.sleep(0.3)
    finally:
        if blocked:
            block_input(False)
        log("control: ended, local input restored.")


def start_control(srv):
    if _control["thread"] and _control["thread"].is_alive():
        return
    ev = threading.Event()
    _control["stop"] = ev
    _control["thread"] = threading.Thread(target=_control_loop, args=(srv, ev), daemon=True)
    _control["thread"].start()


def stop_control():
    if _control["thread"]:
        try:
            _control["stop"].set()
        except Exception:
            pass
        _control["thread"] = None


def apply_control(active, srv):
    """Keep the banner and the control session in step with whether control is active."""
    set_control_banner(active)
    if active:
        start_control(srv)
    else:
        stop_control()


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
            # GetTickCount and dwTime are 32-bit unsigned millisecond counters.
            # Read GetTickCount unsigned and wrap the difference to 32 bits, so the
            # idle time stays correct after ~24.9 days (signed overflow) and across
            # the ~49.7-day counter wrap.
            k = ctypes.windll.kernel32
            k.GetTickCount.restype = ctypes.c_uint
            millis = (k.GetTickCount() - info.dwTime) & 0xFFFFFFFF
            return millis / 1000.0
    return 0.0


# A browser's address isn't in its window title, so we only keep the title; a
# later browser-extension phase can add the real URL. This stays honest about
# what it knows.
BROWSERS = {"chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe", "Safari", "firefox"}


def website_from(app, title):
    return ""  # reserved for the browser-extension phase


def take_screenshot(max_width=1920, quality=82):
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
                        headers=self._headers(),
                        json={"consent_ack": consent_ack, "agent_version": AGENT_VERSION})
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

    def input_poll(self):
        """Long-poll the owner's remote-control input. Returns at once when there is
        input, else after the server's brief hold. Its timeout is longer than the hold."""
        r = self.s.get(self.base + "/api/agent/input", timeout=6, headers=self._headers())
        r.raise_for_status()
        return r.json().get("events", [])

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
    global remote_requested
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
            r = subprocess.run(args, capture_output=True, text=True)
            if r.returncode != 0:
                # Don't report success when Windows refused it (e.g. not allowed, or one
                # is already scheduled), or the owner would think it worked.
                return False, (r.stderr or r.stdout or ("exit %d" % r.returncode)).strip()[:150]
            return True, t + " scheduled"
        if t == "live":
            return True, "live"
        if t == "record":
            # The caller (main loop) turns on dense recording when this returns ok.
            return True, "recording"

        # ========== REMOTE CONTROL (mouse + keyboard) ==========
        # The owner asks for control; it only takes effect while the live view is on, and
        # the employee sees a banner the whole time (both decided by the main loop).
        if t == "remote_start":
            if not HAS_REMOTE:
                return False, "pynput not installed"
            remote_requested = True
            log("Remote control requested by admin")
            return True, "remote started"

        if t == "remote_stop":
            remote_requested = False
            log("Remote control stopped by admin")
            return True, "remote stopped"

        if t in ("mouse_move", "mouse_click", "mouse_scroll", "key"):
            # Remote input now rides the fast input channel (see _control_loop), which is
            # smooth and applies on the thread that holds local input. If one of these ever
            # arrives here (from an older dashboard), just acknowledge it without acting, so
            # it is never applied twice or from the wrong thread.
            return True, "ok"

    except Exception as e:
        return False, str(e)[:120]
    return False, "unknown action"


# ----- policy enforcement (block apps and websites) -----
HOSTS = r"C:\Windows\System32\drivers\etc\hosts"
MARK_BEGIN = "# >>> EmployeeMonitor blocked sites >>>"
MARK_END = "# <<< EmployeeMonitor blocked sites <<<"
_applied_sites = None
_site_block_warned = False


def _clean_domains(sites):
    """Turn whatever the owner typed into plain host names the hosts file can use.
    'https://www.youtube.com/watch?v=1' -> 'youtube.com', 'www.tiktok.com' -> 'tiktok.com'.
    Entries that are not a domain are dropped; order is kept and duplicates removed."""
    out, seen = [], set()
    for s in sites:
        d = (s or "").strip().lower()
        if not d:
            continue
        d = re.sub(r"^[a-z]+://", "", d)   # drop a scheme (http:// https://)
        d = d.split("/")[0].split("?")[0]  # drop any path or query
        d = d.split(":")[0]                # drop a port
        if d.startswith("www."):
            d = d[4:]
        if re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", d) and d not in seen:
            seen.add(d)
            out.append(d)
    return out


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
    return sorted(seen.values(), key=lambda s: s.lower())[:400]


# Never kill these, even if an admin lists them: Windows would break, the desktop
# would close, or the agent would stop watching itself.
PROTECTED_APPS = {
    "system", "system idle process", "registry", "memory compression",
    "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe",
    "lsass.exe", "svchost.exe", "explorer.exe", "dwm.exe", "fontdrvhost.exe",
    "ctfmon.exe", "taskhostw.exe", "sihost.exe", "runtimebroker.exe",
    "employeemonitoragent.exe", "python.exe", "pythonw.exe",
}


def enforce_blocked_apps(apps):
    if not apps:
        return
    want = {a.lower() for a in apps} - PROTECTED_APPS
    if not want:
        return
    try:
        import psutil
    except Exception:
        return
    for p in psutil.process_iter(["name"]):
        try:
            name = (p.info["name"] or "").lower()
            if name and name in want and name not in PROTECTED_APPS:
                p.kill()
        except Exception:
            pass


def enforce_blocked_sites(sites):
    """Point blocked domains at 0.0.0.0 in the hosts file (needs admin)."""
    global _applied_sites, _site_block_warned
    if not IS_WIN:
        return
    domains = _clean_domains(sites)
    key = tuple(domains)
    if key == _applied_sites:
        return
    try:
        with open(HOSTS, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except Exception:
        return
    stripped = re.sub(re.escape(MARK_BEGIN) + r".*?" + re.escape(MARK_END) + r"\n?", "", content, flags=re.S)
    block = ""
    if domains:
        rows = "".join("0.0.0.0 %s\n0.0.0.0 www.%s\n" % (d, d) for d in domains)
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
            # Goes to agent.log (print is invisible in the windowless exe) so the
            # failure is at least recorded on the computer.
            log("note: website blocking could not be applied - the agent is not running as administrator.")
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
    global NO_DIALOG, REMOTE_ACTIVE
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
    removed      = False     # set when this computer has been taken off the dashboard
    net_fail     = 0         # consecutive network failures, for backing off when offline
    log("Monitoring agent running. Sampling every %ss. (This computer is monitored for work.)" % interval)

    while True:
        now = time.time()
        if removed:
            # Taken off the dashboard: stay quiet (a real re-add is a fresh install with a
            # new key, which replaces this process). No samples, no screenshots, no asking.
            time.sleep(300)
            continue
        try:
            live      = now < live_until
            recording = now < record_until
            # Pick up commands/policy on a ping. While live we keep sending frames
            # every loop, but only ping about once a second, so the frame rate isn't
            # tied to the ping round-trip - this keeps the live view smooth.
            if (not live) or (now - last_ping >= 1.0):
                info = srv.ping(cfg.get("consent_ack", 1))
                last_ping = now
                net_fail = 0
                if info.get("sample_seconds"):
                    interval = int(info["sample_seconds"])

                # Actions the admin sent.
                for cmd in info.get("commands", []):
                    # Control counts only while the owner is watching live; recompute per
                    # action so input in this same batch is gated right and the employee's
                    # banner follows. A bad seconds value can no longer crash the loop.
                    REMOTE_ACTIVE = remote_requested and (time.time() < live_until)
                    apply_control(REMOTE_ACTIVE, srv)
                    ok, result = run_command(cmd, srv)
                    if ok and cmd.get("type") == "live":
                        try:
                            live_until = now + int(cmd.get("payload") or 60)
                        except (TypeError, ValueError):
                            live_until = now + 60
                    if ok and cmd.get("type") == "record":
                        try:
                            record_until = now + int(cmd.get("payload") or 5) * 60
                        except (TypeError, ValueError):
                            record_until = now + 5 * 60
                    srv.command_result(cmd["id"], ok, result)

                # Control ends the instant live ends; keep the banner and session in step.
                REMOTE_ACTIVE = remote_requested and (time.time() < live_until)
                apply_control(REMOTE_ACTIVE, srv)

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
                    # While the owner is actually controlling, send smaller, quicker frames
                    # so what they see keeps up with what they do; otherwise keep it sharp.
                    if REMOTE_ACTIVE:
                        data, size = take_screenshot(1024, 42)
                    else:
                        data, size = take_screenshot(1280, 55)
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
                    net_fail = 0
                except requests.HTTPError as e2:
                    c2 = e2.response.status_code if e2.response is not None else 0
                    if c2 == 403:
                        # The key is spent: this computer was removed from the dashboard.
                        # Stop asking for ever, lift any website blocks we applied, drop the
                        # control banner, and go idle.
                        log("This computer was removed from monitoring. Going idle.")
                        try:
                            enforce_blocked_sites([])
                        except Exception:
                            pass
                        apply_control(False, srv)
                        removed = True
                    else:
                        log("re-enroll failed:", str(e2)[:120])
                except Exception as e2:
                    log("re-enroll failed:", str(e2)[:120])
            elif code == 401:
                log("Session rejected (401) and no key to re-enrol; going idle.")
                apply_control(False, srv)
                removed = True
            else:
                net_fail += 1
                log("network:", str(e)[:120])
        except requests.RequestException as e:
            net_fail += 1
            log("network:", str(e)[:120])
        except Exception as e:
            log("error:", str(e)[:160])

        if args.once:
            break
        # Stream frames as fast as they upload while the admin watches, dense while
        # recording (4s), otherwise just often enough to pick up actions (2s). When the
        # server can't be reached, back off so a computer left off all night barely stirs.
        pause = 0.25 if now < live_until else (4 if now < record_until else 2)
        if net_fail > 3:
            pause = min(60, 2 ** min(net_fail, 6))
        time.sleep(pause)


if __name__ == "__main__":
    main()