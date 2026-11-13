"""
Employee Monitor - the agent that runs on a company computer.

What it does, openly (no hidden/stealth operation):
  - On first run it shows each Windows user of the computer a notice that the
    computer is monitored for work, and remembers that the notice was shown.
  - Every minute it records which application/window is in front and whether the
    computer is idle, and takes one screenshot, and sends them to the server.
  - When the owner takes control of the mouse and keyboard, a red bar on the
    screen says so for as long as it lasts. No bar, no control.
  - It never records passwords or key-by-key text, and it only runs on the
    company computer it was installed on.

First run (enrol this PC once):
    python agent.py --server https://monitor.example.com --key ABCD1234...

After that just run:  python agent.py
(Packaged as an .exe, install.bat does this: see --install below.)

Needs: pip install -r requirements.txt   (mss, Pillow, requests, psutil, pynput)
Windows is the main target; it also runs on macOS/Linux with reduced detail.
"""
import argparse
import ctypes
import io
import json
import os
import platform
import queue
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import zlib

# Packaged, the program runs as two (see supervise): a small supervisor that only starts
# the agent proper and starts it again if it ever stops. The supervisor needs none of
# the heavy parts below, so it does not load them and stays a few megabytes small.
_MODES = ("--worker", "--once", "--install", "--selftest")
LIGHT = ((getattr(sys, "frozen", False) or "--supervise" in sys.argv[1:])
         and not any(a.split("=")[0] in _MODES for a in sys.argv[1:]))
HAS_REMOTE = False

if not LIGHT:
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
    except Exception:  # pragma: no cover
        print("Missing libraries. Run: pip install -r requirements.txt")
        raise

    # Optional: remote control (mouse + keyboard). If pynput is missing the agent
    # still runs normally; a request for control is then refused with the reason.
    try:
        from pynput.mouse import Controller as MouseController, Button
        from pynput.keyboard import Controller as KeyboardController, Key, KeyCode
        HAS_REMOTE = True
    except Exception:
        HAS_REMOTE = False

AGENT_VERSION = "1.3.0"
APP = "EmployeeMonitor"
TASK_NAME = "EmployeeMonitorAgent"
IS_WIN = sys.platform.startswith("win")
NO_DIALOG = False  # set by --no-dialog; prints notices instead of pop-ups (tests/services)
NO_WINDOW = 0x08000000 if IS_WIN else 0   # start a helper program without a black console window flashing up

# What the supervisor (see supervise) reads from the way the agent ended.
EXIT_BUSY = 75     # another copy already runs in this Windows session
EXIT_CONFIG = 78   # nothing to retry: no server, not enrolled, or the install key was refused

# Remote control (mouse + keyboard) is only ever carried out while the owner is also
# watching live, and while it is on the employee sees a clear bar on their screen.
remote_requested = False   # the owner pressed "Take Control" (cleared by Stop, by the end of live, or when the owner's page is gone)
REMOTE_ACTIVE = False      # a control session is running right now


# ----- where the agent keeps things -----
# Two places. The computer's identity (which server, its device token) is kept once for
# the whole computer, so every Windows user who logs on is the same computer on the
# dashboard. What belongs to one person (their log, whether they have seen the notice)
# is kept in that person's own folder. Installs made before this kept everything in the
# user's folder; that still works, and the installer copies it across.
MACHINE_KEYS = ("server", "device_token", "device_id", "enrolled_at", "sample_seconds", "idle_seconds", "consent_notice")


def user_dir():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, APP)
    os.makedirs(d, exist_ok=True)
    return d


def machine_dir(create=False):
    base = os.environ.get("PROGRAMDATA") or os.environ.get("ALLUSERSPROFILE")
    if not base:
        return None
    d = os.path.join(base, APP)
    if create:
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            return None
    return d


def config_path():
    return os.path.join(user_dir(), "agent.json")


def machine_config_path():
    d = machine_dir()
    return os.path.join(d, "agent.json") if d else None


def log_path():
    return os.path.join(user_dir(), "agent.log")


_last_log = {"msg": None, "n": 0}
_log_lock = threading.Lock()


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
    with _log_lock:
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


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_json(path, data):
    """Whole file or nothing: written beside the real one, then swapped in."""
    tmp = "%s.%d.tmp" % (path, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def load_config():
    cfg = _read_json(config_path())
    mp = machine_config_path()
    machine = _read_json(mp) if mp else {}
    # The computer's own identity wins over a user's copy - unless that user's agent
    # enrolled again more recently and could not write the shared file (an ordinary
    # user may not replace a file an administrator made).
    if machine.get("device_token") and _whole(machine.get("enrolled_at"), 0) >= _whole(cfg.get("enrolled_at"), 0):
        for k in MACHINE_KEYS:
            if k in machine:
                cfg[k] = machine[k]
    return cfg


def save_config(cfg):
    try:
        _write_json(config_path(), cfg)
    except OSError as e:
        log("could not save the agent's settings:", str(e)[:120])
    d = machine_dir(create=True)
    if d and cfg.get("device_token"):
        try:
            _write_json(os.path.join(d, "agent.json"), {k: cfg[k] for k in MACHINE_KEYS if k in cfg})
        except OSError:
            pass      # an ordinary user may not write there; the installer (an administrator) already has


def _whole(value, default):
    """A whole number from whatever came (a setting, a server reply), or the default."""
    try:
        return int(value if value not in (None, "") else default)
    except (TypeError, ValueError):
        return default


# ----- small Windows helpers -----
def make_dpi_aware():
    """Work in real pixels on a screen set to 125% or 150%, so the size of the screen,
    the screenshots and the owner's mouse positions all mean the same thing."""
    if not IS_WIN:
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def session_id():
    """The number of the Windows log-on session this program runs in."""
    if not IS_WIN:
        return 0
    try:
        k = ctypes.windll.kernel32
        sid = ctypes.c_ulong(0)
        if k.ProcessIdToSessionId(k.GetCurrentProcessId(), ctypes.byref(sid)):
            return int(sid.value)
    except Exception:
        pass
    return 0


def session_active():
    """Is this log-on session the one at the screen right now? With fast user switching
    several users stay logged on, each with their own copy of the agent; only the one
    whose desktop is showing has anything to report. (A locked screen is still active.)"""
    if not IS_WIN:
        return True
    try:
        wts = ctypes.windll.wtsapi32
        wts.WTSQuerySessionInformationW.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int,
                                                    ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_ulong)]
        wts.WTSFreeMemory.argtypes = [ctypes.c_void_p]
        buf = ctypes.c_void_p()
        size = ctypes.c_ulong(0)
        # 8 = WTSConnectState; the answer is a number, 0 = active.
        if not wts.WTSQuerySessionInformationW(None, session_id(), 8, ctypes.byref(buf), ctypes.byref(size)):
            return True
        try:
            state = ctypes.cast(buf, ctypes.POINTER(ctypes.c_int)).contents.value
        finally:
            wts.WTSFreeMemory(buf)
        return state == 0
    except Exception:
        return True


_mutexes = []     # kept for the life of the program: Windows frees them when it ends


def take_mutex(name):
    """True if this program is now the only holder of the name in this log-on session.
    Used so that a second start (the hourly safety start, a double click) does nothing.
    EM_INSTANCE lets a test run beside the real agent."""
    if not IS_WIN:
        return True
    try:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateMutexW.restype = ctypes.c_void_p
        k.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        k.CloseHandle.argtypes = [ctypes.c_void_p]
        full = "Local\\%s-%s%s" % (TASK_NAME, name, os.environ.get("EM_INSTANCE", ""))
        ctypes.set_last_error(0)
        h = k.CreateMutexW(None, 0, full)
        if not h:
            return True                      # cannot tell: do not stand in the agent's way
        if ctypes.get_last_error() == 183:   # ERROR_ALREADY_EXISTS
            k.CloseHandle(h)
            return False
        _mutexes.append(h)
        return True
    except Exception:
        return True


def _forget(*paths):
    for p in paths:
        if p:
            try:
                os.remove(p)
            except OSError:
                pass


# ----- every window the agent shows, on one thread -----
class _Ui:
    """The control bar and an administrator's message are small Tk windows. They all
    live on ONE thread, which owns ONE Tk interpreter for the whole life of the agent.

    It was a window thread of its own per window before, each with its own interpreter.
    When such a thread ended, its interpreter was freed later, by Python's garbage
    collector, on whichever thread happened to be running - and Tcl ends the whole
    program when an interpreter is freed from a thread other than its own
    ("Tcl_AsyncDelete: async handler deleted by the wrong thread"). That is what
    stopped the agent some time after remote control was used. Here the interpreter
    is never freed at all: its root window is kept, hidden, until the agent ends."""

    def __init__(self):
        self._jobs = queue.Queue()
        self._lock = threading.Lock()
        self._thread = None
        self._up = threading.Event()
        self.started = False
        self.ok = False
        self.error = ""
        self.root = None

    def start(self):
        """Start the window thread if it is not running. True if windows can be shown."""
        with self._lock:
            if self._thread is None:
                self.started = True
                self._thread = threading.Thread(target=self._run, name="em-ui", daemon=True)
                self._thread.start()
        self._up.wait(20)
        return self.ok

    def run(self, fn, *args):
        """Have the window thread call fn(root, *args). False if there are no windows here."""
        if not self.start():
            return False
        self._jobs.put((fn, args))
        return True

    def _run(self):
        try:
            import tkinter as tk
            root = tk.Tk()
            root.withdraw()
        except Exception as e:
            self.error = str(e)[:160]
            self._up.set()
            return
        self.root = root

        def pump():
            try:
                while True:
                    fn, args = self._jobs.get_nowait()
                    try:
                        fn(root, *args)
                    except Exception as e:
                        log("window error:", str(e)[:160])
            except queue.Empty:
                pass
            root.after(120, pump)

        self._pump = pump
        root.after(0, pump)
        self.ok = True
        self._up.set()
        try:
            root.mainloop()
        except Exception as e:
            log("window thread ended:", str(e)[:160])
        self.ok = False


ui = _Ui()


# ----- the visible monitoring notice (transparency) -----
def show_notice(text, dialog=True):
    """The monitoring notice itself: a plain box the employee closes with OK. Waits for it."""
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


# ----- a message from the owner: text + picture on screen, voice plays -----
def _message_window(root, body, image_path, voice_path):
    import tkinter as tk
    win = tk.Toplevel(root)
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
            from PIL import ImageTk
            with Image.open(image_path) as im:      # closed again at once, so the file can be deleted
                im.thumbnail((560, 420))
                photo = ImageTk.PhotoImage(im, master=win)
            lbl = tk.Label(frm, image=photo)
            lbl.image = photo                       # keep a reference so it is not garbage-collected
            lbl.pack(pady=(12, 0))
        except Exception:
            pass
    done = []

    def close():
        if done:
            return
        done.append(1)
        if voice_path:
            try:
                import winsound
                winsound.PlaySound(None, winsound.SND_PURGE)   # stop the voice
            except Exception:
                pass
        try:
            win.destroy()
        except Exception:
            pass
        # The picture and voice were downloaded to TEMP only to show this one message.
        _forget(image_path, voice_path)

    tk.Button(frm, text="Close", command=close, padx=18, pady=5,
              font=("Segoe UI", 10)).pack(pady=(16, 0))
    win.protocol("WM_DELETE_WINDOW", close)         # the window's own X closes it the same way
    win.update_idletasks()
    w, h = win.winfo_width(), win.winfo_height()
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    win.geometry("+%d+%d" % (max(0, (sw - w) // 2), max(0, (sh - h) // 3)))
    if voice_path:
        try:
            import winsound
            winsound.PlaySound(voice_path, winsound.SND_FILENAME | winsound.SND_ASYNC)
        except Exception:
            try:
                os.startfile(voice_path)            # no WAV support -> the default player
            except Exception:
                pass


def show_message(body, image_path=None, voice_path=None):
    """A visible, dismissable message from the owner, with an optional picture shown and
    voice clip played. Never waits: monitoring goes on while it is on the screen."""
    text = body or "(a picture or voice was sent)"
    if NO_DIALOG:
        print("\n*** Message from your administrator: " + text + " ***\n")
        _forget(image_path, voice_path)
        return True
    if ui.run(_message_window, body, image_path, voice_path):
        return True

    def _plain():                                   # no window system here: a plain box
        show_notice("Message from your administrator:\n\n" + text)
        _forget(image_path, voice_path)

    threading.Thread(target=_plain, daemon=True).start()
    return True


# ----- remote control: the "being controlled" bar -----
_banner = {"win": None, "shown": threading.Event()}


def _click_through(win):
    """Let clicks fall through the bar to whatever is under it. The bar is there to be
    seen, not to be in the way: it sits on top of every window, and without this the
    owner could not reach a title bar or a browser tab lying under it."""
    if not IS_WIN:
        return
    try:
        u = ctypes.windll.user32
        u.GetParent.restype = ctypes.c_void_p
        u.GetParent.argtypes = [ctypes.c_void_p]
        u.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
        u.SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]
        hwnd = u.GetParent(win.winfo_id()) or win.winfo_id()
        style = u.GetWindowLongW(hwnd, -20)                       # GWL_EXSTYLE
        # layered | transparent to the mouse | no taskbar button | never takes the keyboard
        u.SetWindowLongW(hwnd, -20, style | 0x00080000 | 0x00000020 | 0x00000080 | 0x08000000)
    except Exception:
        pass


def _out_of_pictures(win):
    """Keep the bar out of the pictures taken of the screen (Windows 10 version 2004 and
    later), so the owner sees the whole screen with nothing lying over its top. The
    employee sees the bar all the same: this changes only what a screen capture gets."""
    if not IS_WIN:
        return
    try:
        u = ctypes.windll.user32
        u.GetParent.restype = ctypes.c_void_p
        u.GetParent.argtypes = [ctypes.c_void_p]
        u.SetWindowDisplayAffinity.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        hwnd = u.GetParent(win.winfo_id()) or win.winfo_id()
        u.SetWindowDisplayAffinity(hwnd, 0x11)                    # WDA_EXCLUDEFROMCAPTURE
    except Exception:
        pass


def _banner_show(root):
    if _banner["win"] is None:
        import tkinter as tk
        win = tk.Toplevel(root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        try:
            win.attributes("-alpha", 0.92)
        except Exception:
            pass
        win.configure(bg="#b91c1c")
        lbl = tk.Label(win, text="●  Your administrator is viewing and controlling this computer.",
                       bg="#b91c1c", fg="white", font=("Segoe UI", 11, "bold"), padx=18, pady=6)
        lbl.pack(fill="both", expand=True)
        win.update_idletasks()
        # A bar at the top centre: plain to see, and the corners of the screen (a window's
        # own buttons, the clock) stay uncovered.
        w, h = lbl.winfo_reqwidth(), lbl.winfo_reqheight()
        win.geometry("%dx%d+%d+0" % (w, h, max(0, (win.winfo_screenwidth() - w) // 2)))
        win.update_idletasks()
        _click_through(win)
        _out_of_pictures(win)
        _banner["win"] = win
    _banner["shown"].set()


def _banner_hide(root):
    win = _banner["win"]
    _banner["win"] = None
    _banner["shown"].clear()
    if win is not None:
        try:
            win.destroy()
        except Exception:
            pass


def set_control_banner(on):
    """Show or hide the bar that tells the employee their computer is being controlled.
    Returns True when the bar really is on the screen (or, for hiding, always). The
    caller refuses control when it cannot be shown: control is never silent."""
    if on:
        if _banner["shown"].is_set():
            return True
        if NO_DIALOG or not ui.run(_banner_show):
            return False
        return _banner["shown"].wait(6)
    if ui.ok:
        ui.run(_banner_hide)
    else:
        _banner["shown"].clear()
    return True


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


# ----- the control session: hold back local input, apply the owner's input -----
# While the owner is in control, the employee's own keyboard and mouse - built in or
# plugged in, it makes no difference to Windows - are held back, so the two don't fight
# over the cursor. This can never trap anyone:
#   - the red bar stays up the whole time (control is never silent);
#   - input is handed back the instant the owner presses Stop or closes the live view;
#   - the owner's page sends a heartbeat every 2 s: when those stop for 10 s (the page
#     was closed, the owner's network dropped) control ends by itself;
#   - if the agent loses contact with the server, input is returned within a few seconds;
#   - if the agent is closed or crashes, Windows restores input by itself;
#   - Ctrl+Alt+Del always reaches Windows itself, and Windows lets go of the hold when
#     it is pressed. The agent takes the hold again only while the owner is really
#     working the computer (a click, a key or a mouse move from them in the last 20 s),
#     and again with their next one. So with nobody active at the other end,
#     Ctrl+Alt+Del is a way out - also for an owner who is trying control out on their
#     own computer and so cannot reach their own Stop button.
# Holding input needs administrator rights: for a Windows user without them the owner
# still controls the computer, but the employee's own input is not held (the log says so).
OWNER_GONE_SECONDS = 10
OWNER_ACTIVE_SECONDS = 20
_OWNER_ACTIONS = ("m", "d", "u", "c", "s", "k")      # what the owner does, as opposed to "still here"


def block_input(on):
    """Hold back (or restore) this computer's own keyboard + mouse. Returns True if it
    took effect. Must be called from the same thread that injects the owner's input."""
    if not IS_WIN:
        return False
    try:
        return bool(ctypes.windll.user32.BlockInput(bool(on)))
    except Exception:
        return False


def _frac_xy(fx, fy):
    """A fraction 0..1 of the screen -> absolute primary-screen pixels (or absolute if a
    value is already above 1, as an older console sent)."""
    try:
        a, b = float(fx), float(fy)
    except (TypeError, ValueError):
        return None
    if a != a or b != b:                  # not a number
        return None
    if a > 1.0 or b > 1.0:
        return int(a), int(b)
    w, h = _screen_size()
    return int(round(max(0.0, a) * (w - 1))), int(round(max(0.0, b) * (h - 1)))


_SPECIAL_KEYS = {}
_NAMED = {"enter": "enter", "tab": "tab", "esc": "esc", "escape": "esc", "backspace": "backspace",
          "delete": "delete", "insert": "insert", "up": "up", "down": "down", "left": "left", "right": "right",
          "space": "space", "home": "home", "end": "end", "pageup": "page_up", "page_up": "page_up",
          "pagedown": "page_down", "page_down": "page_down", "menu": "menu",
          "win": "cmd", "capslock": "caps_lock", "printscreen": "print_screen"}
_MOD_NAMES = ("ctrl", "alt", "shift", "win")


def _special_keys():
    """Names the console sends -> the keys they are. Built on first use (pynput is optional)."""
    if not _SPECIAL_KEYS:
        for name, attr in _NAMED.items():
            k = getattr(Key, attr, None)
            if k is not None:
                _SPECIAL_KEYS[name] = k
        for i in range(1, 13):
            k = getattr(Key, "f%d" % i, None)
            if k is not None:
                _SPECIAL_KEYS["f%d" % i] = k
    return _SPECIAL_KEYS


def _key_for(name, mods):
    """What the owner pressed -> (the key to press, the modifier names to hold with it).
    (None, []) for anything that is not a key to type."""
    if not isinstance(name, str) or not name:
        return None, []
    mods = [m for m in _MOD_NAMES if m in (mods or ())]
    if len(name) > 1:
        return _special_keys().get(name.lower()), mods
    if "ctrl" in mods or "alt" in mods or "win" in mods:
        # A shortcut is a key, not a character: Ctrl+C is the C key, whatever Shift or
        # Caps Lock made of the letter on the owner's side.
        if IS_WIN:
            try:
                scan = ctypes.windll.user32.VkKeyScanW
                scan.restype = ctypes.c_short
                scan.argtypes = [ctypes.c_wchar]
                vk = scan(name.lower())
                if vk != -1 and (vk & 0xFF) != 0xFF:
                    return KeyCode.from_vk(vk & 0xFF), mods
            except Exception:
                pass
        return KeyCode.from_char(name.lower()), mods
    # Plain typing: the character already is what Shift made of it ("A", "!"), so Shift
    # is not pressed a second time.
    return KeyCode.from_char(name), [m for m in mods if m != "shift"]


class _Hands:
    """The owner's hands on this computer for one control session: a mouse, a keyboard,
    and what they are holding down - so that everything is let go when it ends, even if
    the "button up" never arrived."""

    def __init__(self):
        self.mouse = MouseController()
        self.kb = KeyboardController()
        self.buttons = set()
        self.mods = []
        self.mod_key = {"ctrl": Key.ctrl, "alt": Key.alt, "shift": Key.shift, "win": Key.cmd}
        self.button = {"left": Button.left, "right": Button.right, "middle": Button.middle}

    def hold_mods(self, mods):
        for m in _MOD_NAMES:
            if m in (mods or ()) and m not in self.mods:
                self.kb.press(self.mod_key[m])
                self.mods.append(m)

    def drop_mods(self):
        while self.mods:
            m = self.mods.pop()
            try:
                self.kb.release(self.mod_key[m])
            except Exception:
                pass

    def key(self, name, mods):
        key, hold = _key_for(name, mods)
        if key is None:
            return
        held = []
        try:
            for m in hold:
                if m not in self.mods:               # not one a held mouse button already keeps down
                    self.kb.press(self.mod_key[m])
                    held.append(m)
            self.kb.press(key)
            self.kb.release(key)
        finally:
            for m in reversed(held):
                try:
                    self.kb.release(self.mod_key[m])
                except Exception:
                    pass

    def release_all(self):
        for b in list(self.buttons):
            try:
                self.mouse.release(b)
            except Exception:
                pass
        self.buttons.clear()
        self.drop_mods()


def _apply_events(events, hands):
    """Carry out a batch of the owner's input, in order. Consecutive mouse moves are
    coalesced (only the last is applied) so a fast drag never backs up. Returns 'stop'
    if the owner handed control back.

    Events: m = move, d / u = a button down / up (so dragging works), c = a whole click
    (older consoles), s = wheel, k = a key with the modifiers held with it, hb = the
    owner's page is still there, stop."""
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
                    hands.mouse.position = xy
            elif t in ("d", "u", "c"):
                btn = hands.button.get(str(e.get("b") or "left").lower())
                if btn is None:
                    continue
                if e.get("x") is not None:
                    xy = _frac_xy(e.get("x"), e.get("y"))
                    if xy:
                        hands.mouse.position = xy
                if t == "d":
                    hands.hold_mods(e.get("mods"))
                    hands.mouse.press(btn)
                    hands.buttons.add(btn)
                elif t == "u":
                    hands.mouse.release(btn)
                    hands.buttons.discard(btn)
                    if not hands.buttons:
                        hands.drop_mods()
                else:
                    hands.mouse.click(btn)
            elif t == "s":
                dy = e.get("dy") if e.get("dy") is not None else e.get("d")
                dx, dy = int(e.get("dx") or 0), int(dy or 0)
                hands.mouse.scroll(max(-10, min(10, dx)), max(-10, min(10, dy)))
            elif t == "k":
                hands.key(e.get("k"), e.get("mods"))
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
    hands = None
    blocked = False
    try:
        link = Server(srv.base, srv.token)   # its own connection, apart from the main loop's
        hands = _Hands()
        blocked = block_input(True)
        log("control: started" + (", local input held." if blocked else
                                  " (could not hold local input - this Windows user is not an administrator)."))
        last_ok = last_owner = time.monotonic()
        last_action = None                   # when the owner last clicked, typed or moved the mouse
        beats = False                        # this console sends heartbeats (older ones do not)
        while not stop_event.is_set():
            asked = time.monotonic()
            try:
                events = link.input_poll()
            except requests.RequestException:
                # Failsafe: lost contact -> give the employee their input back after a few
                # seconds, so a dropped network can never leave them locked out.
                if blocked and time.monotonic() - last_ok > 4:
                    block_input(False)
                    blocked = False
                    log("control: lost contact, handed input back (failsafe).")
                stop_event.wait(0.3)
                continue
            except Exception as e:
                log("control loop error:", str(e)[:120])
                stop_event.wait(0.3)
                continue
            now = time.monotonic()
            last_ok = now
            if stop_event.is_set():
                break                                # ended while waiting: what came is not applied
            if events:
                last_owner = now
                if not beats and any(isinstance(e, dict) and e.get("t") == "hb" for e in events):
                    beats = True
                if any(isinstance(e, dict) and e.get("t") in _OWNER_ACTIONS for e in events):
                    last_action = now
            if IS_WIN and (not blocked or (last_action is not None and now - last_action < OWNER_ACTIVE_SECONDS)):
                # Take the hold (again). A network gap had let it go - or Windows did: it
                # gives input back when Ctrl+Alt+Del is pressed. After that it is taken
                # again only while the owner is working the computer; see the note above.
                # (Asking for a hold that is already there changes nothing.)
                blocked = block_input(True) or blocked
            if _apply_events(events, hands) == "stop":
                remote_requested = False             # tell the main loop control is over
                break
            if beats and now - last_owner > OWNER_GONE_SECONDS:
                remote_requested = False
                log("control: the administrator's page stopped answering; control ended.")
                break
            if not events and now - asked < 0.2:
                stop_event.wait(0.2)                 # a server that answers at once: do not spin
    except Exception as e:                           # could not even begin: no control, and say why
        remote_requested = False
        log("control: stopped by an error:", str(e)[:160])
    finally:
        if hands is not None:
            hands.release_all()
        if blocked:
            block_input(False)
        set_control_banner(False)                    # the bar goes when control has really ended
        log("control: ended, local input restored.")


def control_running():
    t = _control["thread"]
    return bool(t and t.is_alive() and not _control["stop"].is_set())


def start_control(srv):
    t = _control["thread"]
    if t and t.is_alive():
        return                                       # running, or still letting go: the next ping looks again
    ev = threading.Event()
    _control["stop"] = ev
    _control["thread"] = threading.Thread(target=_control_loop, args=(srv, ev), daemon=True, name="em-control")
    _control["thread"].start()


def stop_control():
    if _control["stop"] is not None:
        _control["stop"].set()


def apply_control(active, srv):
    """Keep the control session in step with whether the owner has control. Returns
    whether control is on. With no bar on the employee's screen there is no control."""
    if active and HAS_REMOTE and set_control_banner(True):
        start_control(srv)
        return True
    stop_control()
    t = _control["thread"]
    if not (t and t.is_alive()):
        set_control_banner(False)        # no session to take the bar down (it never began): do it here
    return False


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


def website_from(app, title):
    # A browser's address isn't in its window title, and reading it needs a browser
    # add-on. The server names the site from the title where the title makes it plain.
    return ""


def _primary(mons):
    """The primary screen among the screens mss lists: the one whose corner is the
    desktop's origin. The owner's mouse positions are fractions of that same screen."""
    return next((m for m in mons[1:] if m.get("left") == 0 and m.get("top") == 0),
                mons[1] if len(mons) > 1 else mons[0])


def _picture(raw):
    """What mss grabbed, as a picture (straight from its own bytes: the quick way)."""
    return Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")


def _grab():
    """The primary screen as a picture."""
    maker = getattr(mss, "MSS", None) or mss.mss        # 'mss.mss' is the older name for it
    with maker() as sct:
        return _picture(sct.grab(_primary(sct.monitors)))


def screen_signature(img):
    """A tiny grey copy of the screen (32 x 18 cells) to tell whether it has changed."""
    return img.resize((32, 18), Image.BOX).convert("L").tobytes()


def screen_changed(a, b):
    """Has a visible part of the screen changed between two signatures? The clock ticking
    over or a blinking cursor is not a change; a video frame, a new window or a scrolled
    page is (six or more cells, about 1% of the screen, clearly different)."""
    if a is None or b is None or len(a) != len(b):
        return True
    return sum(1 for x, y in zip(a, b) if abs(x - y) >= 10) >= 6


def take_screenshot(max_width=1920, quality=82, signature=False):
    img = _grab()
    sig = screen_signature(img) if signature else None
    w, h = img.size
    if w > max_width:
        img = img.resize((max_width, int(h * max_width / w)))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return (buf.getvalue(), img.size, sig) if signature else (buf.getvalue(), img.size)


# ----- live frames, on a thread of their own -----
# While the owner watches (or controls), the frames are taken and sent by their own
# thread, over their own connection, so that nothing else the agent does - asking for
# actions, the minute's sample, the list of programs - stands between two frames.
#   - A frame goes out as soon as the screen has changed, so what the owner did shows up
#     with no older frame ahead of it in the line.
#   - While nothing changes nothing is sent, except one frame every two seconds that
#     says "still here".
#   - A slow line gets lighter frames, a quick one sharper ones again.
LIVE_STEPS = ((1280, 55), (1024, 42), (800, 36))      # width, JPEG quality: sharpest first
LIVE_STILL_SECONDS = 2.0
LIVE_MIN_GAP = 0.08                                    # at most about 12 frames a second
_live = {"until": 0.0, "thread": None, "stop": None}


class _Screen:
    """The primary screen, for one thread (an mss object belongs to the thread that made it)."""

    def __init__(self):
        maker = getattr(mss, "MSS", None) or mss.mss
        self.sct = maker()
        self.mon = _primary(self.sct.monitors)

    def grab(self):
        return self.sct.grab(self.mon)

    @staticmethod
    def fingerprint(raw):
        """Tells one screenful from another: the same number means not a pixel has changed."""
        return zlib.crc32(raw.raw)

    @staticmethod
    def jpeg(raw, width, quality):
        img = _picture(raw)
        if img.width > width:
            img = img.resize((width, max(1, int(img.height * width / img.width))), Image.BILINEAR)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        return buf.getvalue(), img.size

    def close(self):
        try:
            self.sct.close()
        except Exception:
            pass


def _frame_loop(srv, stop_event):
    screen = None
    step = None                          # which of LIVE_STEPS is in use
    slow = quick = 0
    last_mark, last_sent, changed_at = None, 0.0, 0.0
    try:
        link = Server(srv.base, srv.token)                       # its own connection
        while not stop_event.is_set() and time.time() < _live["until"]:
            began = time.monotonic()
            try:
                if screen is None:
                    screen = _Screen()
                raw = screen.grab()
                mark = screen.fingerprint(raw)
                now = time.time()
                if mark != last_mark:
                    changed_at = now
                elif now - last_sent < LIVE_STILL_SECONDS:
                    # Nothing new on the screen. Look again soon: at once while the owner is
                    # controlling (their click must show), a little less often when they only
                    # watch a screen that has stood still for some seconds.
                    stop_event.wait(0.06 if REMOTE_ACTIVE else (0.12 if now - changed_at < 3 else 0.25))
                    continue
                floor = 1 if REMOTE_ACTIVE else 0                # in control: lighter frames from the start
                step = floor if step is None else max(step, floor)
                data, size = screen.jpeg(raw, *LIVE_STEPS[step])
                sent = time.monotonic()
                link.screenshot(data, size, now, live=True)
                took = time.monotonic() - sent
                last_mark, last_sent = mark, now
                # A frame that takes half a second to send is too heavy for this line: after
                # two of those, lighter ones. After eight quick ones, a step sharper again.
                if took > 0.5:
                    slow, quick = slow + 1, 0
                elif took < 0.2:
                    slow, quick = 0, quick + 1
                else:
                    slow = quick = 0
                if slow >= 2 and step < len(LIVE_STEPS) - 1:
                    step, slow = step + 1, 0
                elif quick >= 8 and step > floor:
                    step, quick = step - 1, 0
                left = LIVE_MIN_GAP - (time.monotonic() - began)
                if left > 0:
                    stop_event.wait(left)
            except requests.RequestException as e:
                log("live frame:", str(e)[:100])
                stop_event.wait(1.0)
            except Exception as e:
                log("live frame error:", str(e)[:120])
                if screen is not None:
                    screen.close()
                    screen = None
                stop_event.wait(1.0)
    except Exception as e:
        log("live frames stopped by an error:", str(e)[:160])
    finally:
        if screen is not None:
            screen.close()


def start_frames(srv):
    t = _live["thread"]
    if t and t.is_alive():
        return
    ev = threading.Event()
    _live["stop"] = ev
    _live["thread"] = threading.Thread(target=_frame_loop, args=(srv, ev), daemon=True, name="em-live")
    _live["thread"].start()


def stop_frames():
    _live["until"] = 0.0
    if _live["stop"] is not None:
        _live["stop"].set()


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
            import urllib.request
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

    def screenshot(self, data, size, ts, live=False):
        """live: a frame for the owner's live view. The server shows every one of those
        but keeps only one every few seconds as history."""
        fields = {"ts": int(ts), "w": size[0], "h": size[1]}
        if live:
            fields["live"] = 1
        r = self.s.post(self.base + "/api/agent/screenshot", timeout=60,
                        headers=self._headers(),
                        files={"shot": ("s.jpg", data, "image/jpeg")}, data=fields)
        r.raise_for_status()
        return r.json()

    def get_json(self, path):
        r = self.s.get(self.base + path, timeout=30, headers=self._headers())
        r.raise_for_status()
        return r.json()

    def input_poll(self):
        """Long-poll the owner's remote-control input. Returns at once when there is
        input, else after the server's brief hold. Its timeout is longer than the hold."""
        r = self.s.get(self.base + "/api/agent/input", timeout=8, headers=self._headers())
        r.raise_for_status()
        events = r.json().get("events", [])
        return events if isinstance(events, list) else []

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
    """Returns (ok, result_text). The main loop does the timing for 'live', 'record'
    and the start of control."""
    global remote_requested
    t = cmd.get("type")
    payload = cmd.get("payload") or ""
    try:
        if t == "message":
            show_message(payload)
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
            show_message(meta.get("body") or "", image, voice)
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
            r = subprocess.run(args, capture_output=True, text=True, creationflags=NO_WINDOW)
            if r.returncode != 0:
                # Don't report success when Windows refused it (e.g. not allowed, or one
                # is already scheduled), or the owner would think it worked.
                return False, (r.stderr or r.stdout or ("exit %d" % r.returncode)).strip()[:150]
            return True, t + " scheduled"
        if t == "live":
            return True, "live"
        if t == "record":
            return True, "recording"

        # ========== REMOTE CONTROL (mouse + keyboard) ==========
        # The answer is only "started" when it can really happen: the part that moves the
        # mouse is in this build, and the bar that tells the employee is on their screen.
        if t == "remote_start":
            if not HAS_REMOTE:
                return False, "this agent was built without remote control (pynput missing)"
            if not set_control_banner(True):
                return False, "the notice bar could not be shown on the screen, so control was refused"
            remote_requested = True
            log("Remote control requested by admin")
            return True, "control started"

        if t == "remote_stop":
            remote_requested = False
            log("Remote control stopped by admin")
            return True, "control stopped"

        if t in ("mouse_move", "mouse_click", "mouse_scroll", "key"):
            # Remote input rides the fast input channel (see _control_loop). If one of these
            # ever arrives here (from an older dashboard), just acknowledge it without
            # acting, so it is never applied twice or from the wrong thread.
            return True, "ok"

    except Exception as e:
        return False, str(e)[:120]
    return False, "unknown action"


# ----- policy enforcement (block apps and websites) -----
HOSTS = r"C:\Windows\System32\drivers\etc\hosts"
MARK_BEGIN = "# >>> EmployeeMonitor blocked sites >>>"
MARK_END = "# <<< EmployeeMonitor blocked sites <<<"
_applied_sites = None
_applied_at = 0.0
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


def _blocked_names(domains):
    """The host names to block for the owner's list. The hosts file knows whole names
    only, no "everything under youtube.com", so the usual front doors are added: the
    name itself, www. and m. (the phone site, which a desktop browser opens just as
    well). Any other name of a site (music.youtube.com) goes on its own line in the
    owner's list."""
    names = []
    for d in domains:
        for n in (d, "www." + d, "m." + d):
            if n not in names:
                names.append(n)
    return names


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
    want = set()
    for a in apps:
        a = (a or "").strip().lower()
        if a:
            want.add(a)
            if "." not in a:
                want.add(a + ".exe")      # "vlc" means vlc.exe
    want -= PROTECTED_APPS
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
    """Point blocked domains at nowhere in the hosts file (needs admin)."""
    global _applied_sites, _applied_at, _site_block_warned
    if not IS_WIN:
        return
    domains = _clean_domains(sites)
    key = tuple(domains)
    # Nothing new to apply. Look at the file again every five minutes all the same, so a
    # block that was edited out by hand comes back.
    if key == _applied_sites and time.time() - _applied_at < 300:
        return
    try:
        with open(HOSTS, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except Exception:
        return
    stripped = re.sub(re.escape(MARK_BEGIN) + r".*?" + re.escape(MARK_END) + r"\n?", "", content, flags=re.S)
    block = ""
    if domains:
        # Both kinds of address: a name blocked only for IPv4 still opens over IPv6.
        rows = "".join("0.0.0.0 %s\n:: %s\n" % (n, n) for n in _blocked_names(domains))
        block = MARK_BEGIN + "\n" + rows + MARK_END + "\n"
    new = stripped.rstrip() + "\n" + block
    if new == content:
        _applied_sites, _applied_at = key, time.time()
        return
    try:
        with open(HOSTS, "w", encoding="utf-8") as f:
            f.write(new)
        subprocess.run(["ipconfig", "/flushdns"], capture_output=True, creationflags=NO_WINDOW)
        _applied_sites, _applied_at = key, time.time()
    except PermissionError:
        _applied_sites, _applied_at = key, time.time()      # do not try again on every ping
        if not _site_block_warned:
            # Goes to agent.log (print is invisible in the windowless exe) so the
            # failure is at least recorded on the computer.
            log("note: website blocking could not be applied - this Windows user is not an administrator.")
            _site_block_warned = True


def enroll_and_save(srv, cfg, base, key):
    """Enroll with the server and persist the new device token. Needs a key."""
    log("Enrolling this computer...")
    res = srv.enroll(key)
    cfg.update(server=base, device_token=res["device_token"], device_id=res.get("device_id"),
               enrolled_at=int(time.time()),
               sample_seconds=res.get("sample_seconds", 60), idle_seconds=res.get("idle_seconds", 180),
               consent_notice=res.get("consent_notice", ""), consent_ack=0)
    save_config(cfg)
    srv.token = cfg["device_token"]
    log("Enrolled as device #%s." % cfg.get("device_id"))
    return res


# ----- the supervisor: the agent comes back by itself if it ever stops -----
# Packaged, the program runs as two: a small supervisor that does nothing but start the
# agent proper and start it again if it ends or gets stuck. (Windows' own "restart the
# task if it fails" does not restart a program that crashed.) The agent proper writes
# the time into a small file as its loop goes round; the supervisor reads it.
HANG_SECONDS = 600
_beat = {"at": 0.0}


def beat_path():
    return os.path.join(user_dir(), "agent-%d%s.beat" % (session_id(), os.environ.get("EM_INSTANCE", "")))


def beat(state="ok"):
    """The agent's loop is alive. 'wait' = it is waiting for the employee on purpose (the
    first-run notice is on the screen), which is not being stuck."""
    now = time.time()
    if state == "ok" and now - _beat["at"] < 10:
        return
    _beat["at"] = now
    try:
        with open(beat_path(), "w", encoding="utf-8") as f:
            f.write("%d %s" % (now, state))
    except OSError:
        pass


def read_beat():
    try:
        with open(beat_path(), encoding="utf-8") as f:
            at, state = f.read().split(" ", 1)
        return float(at), state.strip()
    except Exception:
        return 0.0, ""


def idle_sleep(seconds):
    """Sleep, and keep saying "alive" while doing it."""
    end = time.time() + seconds
    while True:
        beat()
        left = end - time.time()
        if left <= 0:
            return
        time.sleep(min(10, left))


def _kill_tree(pid):
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=NO_WINDOW)
    except Exception:
        pass


def _watch(p):
    """Wait for the agent proper to end. If its loop has not moved for HANG_SECONDS it is
    stuck: end it, so it is started afresh. Returns its exit code."""
    grace = looked = time.time()
    while True:
        try:
            return p.wait(timeout=15)
        except subprocess.TimeoutExpired:
            pass
        now = time.time()
        if now - looked > 90:             # this program was asleep itself (the computer slept): give it time to wake
            grace = now
        looked = now
        at, state = read_beat()
        if state == "wait":
            grace = now
            continue
        if now - max(at, grace) > HANG_SECONDS:
            log("supervisor: the agent has not moved for %d s; ending it." % HANG_SECONDS)
            if IS_WIN:
                _kill_tree(p.pid)
            else:
                p.kill()
            try:
                return p.wait(timeout=30)
            except subprocess.TimeoutExpired:
                return -1


# A packaged program unpacks itself into a new _MEIxxxxxx folder in TEMP at every start
# and removes it when it ends properly. Windows ends the agent at log-off and shut-down
# without that, so every start left about 76 MB behind for good (1.5 GB after 20 days).
# Each start now removes what earlier ones left.
def _our_unpack(d):
    if os.path.isfile(os.path.join(d, "em_agent.marker")):
        return True
    # A copy from before the marker existed: this agent's own mix of parts.
    return all(os.path.isdir(os.path.join(d, n)) for n in ("_tcl_data", "psutil", "PIL", "certifi"))


def clean_stale_unpacks():
    here = getattr(sys, "_MEIPASS", None)
    if not here:
        return 0
    base = os.path.dirname(os.path.abspath(here))
    cutoff = time.time() - 180            # never one that is being unpacked this minute
    removed = 0
    try:
        names = os.listdir(base)
    except OSError:
        return 0
    for name in names:
        d = os.path.join(base, name)
        if not name.startswith("_MEI") or os.path.normcase(d) == os.path.normcase(os.path.abspath(here)):
            continue
        try:
            if not os.path.isdir(d) or os.path.getmtime(d) > cutoff or not _our_unpack(d):
                continue
            # Is a copy still running out of this folder? Then its Python DLL is loaded,
            # and Windows does not let a loaded DLL be deleted. So that file goes first:
            # if it will not go, the folder is in use and nothing else in it is touched.
            # (Renaming the folder is no test: Windows renames a folder that is in use.)
            for f in os.listdir(d):
                if re.fullmatch(r"python3\d+\.dll", f.lower()):
                    os.remove(os.path.join(d, f))
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
        except OSError:
            continue
    # Pictures and voices of messages that were still open when the computer was switched off.
    try:
        tmp = tempfile.gettempdir()
        for name in os.listdir(tmp):
            if name.startswith("emnotice_"):
                p = os.path.join(tmp, name)
                if os.path.getmtime(p) < time.time() - 86400:
                    _forget(p)
    except OSError:
        pass
    if removed:
        log("Removed %d left-over unpack folder(s) of earlier starts." % removed)
    return removed


def supervise():
    if not take_mutex("Supervisor"):
        return 0                          # this Windows session already has its agent
    log("Agent %s starting." % AGENT_VERSION)
    threading.Thread(target=clean_stale_unpacks, daemon=True).start()
    frozen = getattr(sys, "frozen", False)
    cmd = [sys.executable] + ([] if frozen else [os.path.abspath(__file__)])
    cmd += [a for a in sys.argv[1:] if a != "--supervise"] + ["--worker"]
    env = dict(os.environ)
    # The agent proper is a program of its own: it unpacks its own files and so does not
    # depend on this one staying alive. (Left alone, a packaged program started by itself
    # shares the starter's unpacked files, which go when the starter goes.)
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    wait = 2
    while True:
        started = time.time()
        try:
            p = subprocess.Popen(cmd, env=env, creationflags=NO_WINDOW)
        except OSError as e:
            log("supervisor: could not start the agent:", str(e)[:120])
            time.sleep(60)
            continue
        code = _watch(p)
        ran = time.time() - started
        if code == EXIT_CONFIG:
            log("supervisor: the agent has nothing to do here (see the lines above); stopping.")
            return EXIT_CONFIG
        if code == EXIT_BUSY:
            time.sleep(60)                # an earlier copy is still there; look again in a minute
            continue
        wait = 2 if ran > 300 else min(300, wait * 2)
        log("supervisor: the agent ended (code %s) after %d s; starting it again in %d s." % (code, ran, wait))
        time.sleep(wait)


# ----- installing: enrol once for the whole computer, start at every log-on -----
def task_xml(command, arguments):
    """The Windows start-up task. For EVERY user of the computer (the Users group), not
    only the one who installed it; with full rights where the user has them (blocking
    web sites and holding input need them); on mains or battery; with no time limit.
    Once an hour Windows starts it again: a copy that finds the agent running stops at
    once, so this only matters if the agent was ended by hand."""
    from xml.sax.saxutils import escape as xml_escape
    return """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Employee Monitor agent: visible workplace monitoring of this company computer. Starts at every log-on.</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Repetition>
        <Interval>PT1H</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <Enabled>true</Enabled>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <GroupId>S-1-5-32-545</GroupId>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>Parallel</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>%s</Command>
      <Arguments>%s</Arguments>
    </Exec>
  </Actions>
</Task>
""" % (xml_escape(command), xml_escape(arguments))


def token_state(srv):
    """Does the server still know this computer by its saved token? 'ok', 'gone' (it was
    removed on the dashboard) or 'unknown' (the server could not be asked). Asked with
    the list of running programs: a harmless thing to send, and it takes no action away
    from the agent the way a ping would."""
    try:
        r = srv.s.post(srv.base + "/api/agent/procs", timeout=20,
                       headers=srv._headers(), json={"apps": running_apps()})
    except requests.RequestException:
        return "unknown"
    if r.status_code == 401:
        return "gone"
    return "ok" if r.status_code == 200 else "unknown"


def install(args):
    """Run by install.bat, as Administrator: enrol the computer once and make the task
    that starts the agent at every log-on. What happened is written to install.log in
    the computer's EmployeeMonitor folder, because this program has no console."""
    said = []

    def say(text):
        said.append(text)
        log("install:", text)

    def finish(code):
        try:
            d = machine_dir(create=True) or user_dir()
            with open(os.path.join(d, "install.log"), "w", encoding="utf-8") as f:
                f.write("\n".join(said) + "\n")
        except OSError:
            pass
        return code

    if not IS_WIN:
        say("The installer is for Windows.")
        return finish(1)
    cfg = load_config()
    base = (args.server or cfg.get("server") or "").rstrip("/")
    if not base:
        say("No server address given. Usage: install.bat <server-url> <install-key>")
        return finish(1)
    srv = Server(base, cfg.get("device_token"))
    # Does this computer already have a place on that server? 'ok' = yes, 'gone' = it had
    # one and was removed on the dashboard, 'unknown' = the server cannot be asked now.
    state = "none"
    if cfg.get("device_token") and (cfg.get("server") or "").rstrip("/") == base:
        state = token_state(srv)
    key_later = False             # leave the key with the agent, to enrol by itself when it can
    if state == "ok":
        save_config(cfg)          # an older set-up kept this in one user's folder: now for the whole computer
        say("This computer is already enrolled (computer #%s); kept as it is." % cfg.get("device_id"))
    elif state == "unknown":
        save_config(cfg)
        key_later = bool(args.key)
        say("This computer is already enrolled (computer #%s). The server could not be reached "
            "to check it; the agent will keep trying by itself." % cfg.get("device_id"))
    else:
        if state == "gone":
            say("This computer was removed on the dashboard earlier; adding it again.")
        if not args.key:
            say("An install key is needed. Make one with 'Add computer' on the dashboard, then run:\n"
                "    install.bat %s <install-key>" % base)
            return finish(1)
        cfg["device_token"] = None
        srv.token = None
        try:
            enroll_and_save(srv, cfg, base, args.key)
            say("Enrolled as computer #%s on %s." % (cfg.get("device_id"), base))
        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else 0
            if code == 403:
                say("The server refused this install key: it was already used, or mistyped. "
                    "Make a new key with 'Add computer' on the dashboard and run this again.")
                return finish(1)
            key_later = True
            say("The server answered with an error (%s). The agent will keep trying by itself." % code)
        except Exception as e:
            key_later = True
            say("The server could not be reached now (%s). The agent will keep trying by itself." % str(e)[:120])
    frozen = getattr(sys, "frozen", False)
    command = sys.executable
    arguments = ("" if frozen else '"%s" --supervise ' % os.path.abspath(__file__)) + '--server "%s"' % base
    if key_later:
        arguments += " --key %s" % args.key       # only until it has enrolled; a key works once
    fd, path = tempfile.mkstemp(prefix="emtask_", suffix=".xml")
    try:
        with os.fdopen(fd, "w", encoding="utf-16") as f:
            f.write(task_xml(command, arguments))
        # (EM_INSTANCE is empty in real use. A test sets it, and so can never replace the
        # real agent's task, just as it cannot take the real agent's place in memory.)
        r = subprocess.run(["schtasks", "/Create", "/TN", TASK_NAME + os.environ.get("EM_INSTANCE", ""), "/XML", path, "/F"],
                           capture_output=True, text=True, creationflags=NO_WINDOW)
    finally:
        _forget(path)
    if r.returncode != 0:
        say("Could not create the start-up task: " + (r.stderr or r.stdout or "").strip()[:200])
        return finish(1)
    say("Start-up task created: the agent starts at every log-on, for every user of this computer.")
    return finish(0)


def selftest(out):
    """Which parts of the agent work in THIS build, without showing or sending anything.
    build.bat runs it on the fresh .exe, so a part that dropped out of the build (it has
    happened: remote control) is seen at once and not on an office computer."""
    r = {"version": AGENT_VERSION, "packaged": bool(getattr(sys, "frozen", False)),
         "python": platform.python_version(), "remote_control": HAS_REMOTE}
    for name in ("psutil", "truststore", "winsound"):
        try:
            __import__(name)
            r[name] = True
        except Exception as e:
            r[name] = False if name != "winsound" or IS_WIN else "not on this system"
            r[name + "_error"] = str(e)[:80]
    r["windows"] = ui.start()
    if not r["windows"]:
        r["windows_error"] = ui.error or "the window thread did not start"
    try:
        from PIL import ImageTk  # noqa: F401
        r["pictures_in_messages"] = True
    except Exception as e:
        r["pictures_in_messages"] = False
        r["pictures_error"] = str(e)[:80]
    try:
        data, size = take_screenshot(640, 40)
        r["screenshot"] = "%dx%d, %d KB" % (size[0], size[1], len(data) // 1024)
    except Exception as e:
        r["screenshot"] = "FAILED: " + str(e)[:100]
    try:                                   # the live view's own way of taking frames
        screen = _Screen()
        t0 = time.perf_counter()
        for _ in range(8):
            raw = screen.grab()
            screen.fingerprint(raw)
        look = (time.perf_counter() - t0) / 8
        t0 = time.perf_counter()
        data, size = screen.jpeg(raw, 1024, 42)
        made = time.perf_counter() - t0
        screen.close()
        r["live_frame"] = "%dx%d, %d KB; a look at the screen takes %d ms, making a frame %d ms" % (
            size[0], size[1], len(data) // 1024, look * 1000, made * 1000)
    except Exception as e:
        r["live_frame"] = "FAILED: " + str(e)[:100]
    r["ok"] = bool(r["remote_control"] and r["psutil"] and r["windows"] and r["pictures_in_messages"]
                   and not str(r["screenshot"]).startswith("FAILED") and not str(r["live_frame"]).startswith("FAILED"))
    text = json.dumps(r, indent=2)
    if out and out != "-":
        with open(out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    try:
        print(text)
    except Exception:
        pass
    return 0 if r["ok"] else 1


# ----- the agent proper -----
def run_agent(args):
    global NO_DIALOG, REMOTE_ACTIVE, remote_requested
    NO_DIALOG = args.no_dialog
    if not args.once and not take_mutex("Worker"):
        log("Another copy of the agent is already running in this Windows session; this one stops.")
        return EXIT_BUSY

    cfg = load_config()
    base = args.server or cfg.get("server")
    if not base:
        log("No server set. First run: --server <url> --key <key>")
        return EXIT_CONFIG

    srv = Server(base, cfg.get("device_token"))

    # Enrol once - and re-enrol if this PC was last pointed at a different server
    # (a saved device token only works on the server that issued it).
    saved_server = (cfg.get("server") or "").rstrip("/")
    server_changed = bool(cfg.get("device_token")) and saved_server != base.rstrip("/")
    if server_changed:
        log("Server changed (%s -> %s); re-enrolling." % (saved_server, base.rstrip("/")))
        cfg["device_token"] = None
        srv.token = None
    # Enroll, retrying on a network failure so a blip or proxy hiccup is logged and
    # recovered from. A key the server refuses is not retried: it never will be accepted.
    while not cfg.get("device_token"):
        if not args.key:
            log("This PC is not enrolled. Run the installer with an install key.")
            return EXIT_CONFIG
        try:
            enroll_and_save(srv, cfg, base, args.key)
        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else 0
            if code == 403:
                log("Enroll refused: this install key was already used or is not valid. "
                    "Make a new key on the dashboard and run the installer again.")
                return EXIT_CONFIG
            log("enroll failed:", str(e)[:180])
            if args.once:
                return 1
            idle_sleep(30)
        except Exception as e:
            log("enroll failed:", str(e)[:180])
            if args.once:
                return 1
            idle_sleep(30)

    # Show the monitoring notice once to each Windows user, then tell the server.
    if not cfg.get("consent_ack"):
        beat("wait")
        show_notice(cfg.get("consent_notice") or
                    "This is a company computer. Activity on it is monitored for work purposes.",
                    dialog=not args.no_dialog)
        cfg["consent_ack"] = 1
        save_config(cfg)
        beat("ok")

    interval     = _whole(cfg.get("sample_seconds"), 60)
    idle_limit   = _whole(cfg.get("idle_seconds"), 180)
    srv.token    = cfg["device_token"]
    next_sample  = 0.0
    record_until = 0.0
    _live["until"] = 0.0     # until when the owner is watching live (shared with the frame thread)
    last_procs   = 0.0
    last_ping    = 0.0
    last_sig     = None      # what the screen looked like at the last screenshot sent
    last_shot    = 0.0
    removed      = False     # set when this computer has been taken off the dashboard
    net_fail     = 0         # consecutive network failures, for backing off when offline
    log("Monitoring agent %s running. Sampling every %ss. (This computer is monitored for work.)" % (AGENT_VERSION, interval))

    while True:
        now = time.time()
        beat()
        if removed:
            # Taken off the dashboard: stay quiet (a real re-add is a fresh install with a
            # new key, which replaces this process). No samples, no screenshots, no asking.
            idle_sleep(300)
            continue
        if not session_active():
            # Another Windows user is at this computer now (fast user switching). Their own
            # copy of the agent reports; this one waits, and gives up any control it had.
            if remote_requested or control_running():
                remote_requested = False
                REMOTE_ACTIVE = apply_control(False, srv)
            stop_frames()
            record_until = 0.0
            if args.once:
                break
            time.sleep(5)
            continue
        try:
            live      = now < _live["until"]
            recording = now < record_until
            # Pick up commands/policy on a ping: every 2 s, and about once a second while
            # the owner watches live (the frames themselves go out on their own thread).
            if (not live) or (now - last_ping >= 1.0):
                info = srv.ping(cfg.get("consent_ack", 1))
                last_ping = now
                net_fail = 0
                interval   = _whole(info.get("sample_seconds"), interval) or interval
                idle_limit = _whole(info.get("idle_seconds"), idle_limit) or idle_limit

                # Actions the admin sent, in the order they were sent.
                for cmd in info.get("commands", []):
                    kind = cmd.get("type")
                    ok, result = run_command(cmd, srv)
                    if ok and kind == "live":
                        _live["until"] = now + _whole(cmd.get("payload"), 60)
                    elif ok and kind == "remote_start":
                        # Control includes watching: frames flow from now, even if the
                        # "live" action has not arrived yet (the two are sent together).
                        _live["until"] = max(_live["until"], now + 120)
                    elif ok and kind == "record":
                        record_until = now + _whole(cmd.get("payload"), 5) * 60
                    srv.command_result(cmd["id"], ok, result)

                # Control ends with the live view: the owner must be able to see what they
                # control. It is asked for again each time, never carried over to a later
                # live view.
                if remote_requested and time.time() >= _live["until"]:
                    remote_requested = False
                    log("control: ended with the live view.")
                REMOTE_ACTIVE = apply_control(remote_requested, srv)
                if remote_requested and not REMOTE_ACTIVE:
                    remote_requested = False         # the bar could not be shown: no control

                # What this computer may not run / open.
                pol = info.get("policy", {})
                enforce_blocked_apps(pol.get("blocked_apps", []))
                enforce_blocked_sites(pol.get("blocked_sites", []))
                live      = now < _live["until"]
                recording = now < record_until

            due = now >= next_sample
            if due or live or recording:
                app, title = active_window()
                idle = idle_seconds() > idle_limit
                if due:
                    srv.activity({
                        "ts": int(now), "active_app": app, "window_title": title,
                        "website": website_from(app, title), "is_idle": 1 if idle else 0,
                    })
                    next_sample = now + interval
                if live:
                    start_frames(srv)                 # the frames: on their own thread (see _frame_loop)
                    last_sig = None
                    if now - last_procs > 4:          # the running-apps list, while watching
                        srv.procs(running_apps())
                        last_procs = now
                elif recording or due:
                    data, size, sig = take_screenshot(signature=True)
                    # Somebody at the computer: a screenshot with every sample. Nobody at
                    # the keyboard or mouse: a still screen is not sent again and again,
                    # but one that keeps changing (a video, a game) still is, every five
                    # minutes - "no input" is not the same as "nothing on the screen".
                    if recording or not idle or (screen_changed(sig, last_sig) and now - last_shot >= 300):
                        srv.screenshot(data, size, now)
                        last_sig, last_shot = sig, now
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
                        # Stop asking for ever, lift any website blocks we applied, drop
                        # control, and go idle.
                        log("This computer was removed from monitoring. Going idle.")
                        try:
                            enforce_blocked_sites([])
                        except Exception:
                            pass
                        remote_requested = False
                        REMOTE_ACTIVE = apply_control(False, srv)
                        stop_frames()
                        removed = True
                    else:
                        log("re-enroll failed:", str(e2)[:120])
                except Exception as e2:
                    log("re-enroll failed:", str(e2)[:120])
            elif code == 401:
                log("This computer was removed from monitoring (session rejected, no key). Going idle.")
                try:
                    enforce_blocked_sites([])
                except Exception:
                    pass
                remote_requested = False
                REMOTE_ACTIVE = apply_control(False, srv)
                stop_frames()
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
        # Look for actions often while the admin watches, take a frame every 4 s while
        # recording, otherwise come round just often enough to pick up actions (2 s). When
        # the server can't be reached, back off so a computer left off all night barely stirs.
        pause = 0.25 if now < _live["until"] else (4 if now < record_until else 2)
        if net_fail > 3:
            pause = min(60, 2 ** min(net_fail, 6))
        time.sleep(pause)
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", help="server base URL, e.g. https://monitor.example.com")
    ap.add_argument("--key", help="enrollment key (first run only)")
    ap.add_argument("--once", action="store_true", help="send one sample and exit (for testing)")
    ap.add_argument("--no-dialog", action="store_true", help="print notices instead of pop-ups (tests/services)")
    ap.add_argument("--install", action="store_true",
                    help="as Administrator: enrol this computer and make the task that starts the agent at every log-on")
    ap.add_argument("--selftest", nargs="?", const="-", metavar="FILE",
                    help="report which parts work in this build (to FILE), without showing or sending anything")
    ap.add_argument("--supervise", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    make_dpi_aware()
    if args.selftest is not None:
        return selftest(args.selftest)
    if args.install:
        return install(args)
    # Packaged: a supervisor that keeps the agent proper running (see supervise).
    if (getattr(sys, "frozen", False) or args.supervise) and not (args.worker or args.once):
        return supervise()
    return run_agent(args)


if __name__ == "__main__":
    try:
        code = main()
        code = code if isinstance(code, int) else 0
    except SystemExit as e:                      # argparse, or a plain sys.exit somewhere
        code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        if e.code is not None and not isinstance(e.code, int):
            log(str(e.code))
    except KeyboardInterrupt:
        code = 1
    except BaseException as e:                   # never die without a line in the log
        import traceback
        log("fatal:", repr(e)[:200])
        log(traceback.format_exc()[-1500:])
        code = 1
    if ui.started:
        # End without Python's tidy-up: the window thread's Tcl interpreter must not be
        # taken apart from this thread (see _Ui).
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except Exception:
                pass
        os._exit(code)
    sys.exit(code)
