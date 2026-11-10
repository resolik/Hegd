"""Kiwi, step 2: Termux-style screen (this file) + command core (kiwi_core.py).

Scrolling is done by hand: we keep all lines, work out how many fit on the
screen, and draw the right slice into a single Label.

- black background, blinking `_` on the first line
- tap the screen to open the keyboard
- text flows top to bottom; once the screen is full, the top lines scroll away
- drag up/down (or mouse wheel) to browse history
- commands are run by KiwiCore (see kiwi_core.py for the list)
"""
import os
import re
import sys
import threading
import time
import traceback

from kivy.config import Config
Config.set("kivy", "exit_on_escape", "0")   # by default Escape / the Android Back button CLOSES a Kivy app

from kivy.core.window import Window
Window.softinput_mode = "resize"          # shrink the window when the keyboard is shown
Window.clearcolor = (0, 0, 0, 1)

from kivy.app import App
from kivy.clock import Clock
from kivy.metrics import dp, sp
from kivy.uix.floatlayout import FloatLayout
from kivy.uix.label import Label
from kivy.uix.textinput import TextInput
from kivy.utils import escape_markup

from kiwi_core import KiwiCore, input_lines, CONTACTS, VERSION as CORE_VERSION

# ---- settings you can tweak ----
VISIBLE_ROWS = 28                         # max lines on screen (lower it if text hides behind the keyboard)
FONT_SIZE = sp(13)
REQUEST_PERMISSIONS = True                # asks for storage access on Android; does nothing on desktop / Pydroid (no android module)
SCREEN_VERSION = "2026-11-09.2"             # printed at startup next to the core version
BANNER = [                                 # shown at the top when the app opens (colour: COLORS["BANNER"])
    r" ___       _   _",
    r"| _ ) _  _| |_| |_  ___  _ _",
    r"| _ \| || |  _|  _|/ -_)| '_|",
    r"|___/ \_,_|\__|\__|\___||_|",
    "",
]
MAX_ENTRIES = 1000                        # history limit
# --------------------------------
FONT = "RobotoMono-Regular"               # monospace font that ships with Kivy
COLORS = {"BANNER": "f5d062", "INFO": "ffffff", "WARNING": "ffd400", "ERROR": "ff4040",
          "DEBUG": "8a8a8a", "SUCCESS": "3ddc84", "CRITICAL": "ff2d95"}


def request_storage_permissions():
    """Ask Android for storage access. Does nothing on desktop / Pydroid (no 'android' module)."""
    try:
        from android.permissions import request_permissions, Permission
        request_permissions([Permission.READ_EXTERNAL_STORAGE, Permission.WRITE_EXTERNAL_STORAGE])
    except Exception:
        return
    # Android 11+ also needs the separate "all files access" switch in system settings
    try:
        from jnius import autoclass
        if autoclass("android.os.Build$VERSION").SDK_INT >= 30:
            Environment = autoclass("android.os.Environment")
            if not Environment.isExternalStorageManager():
                Intent = autoclass("android.content.Intent")
                Settings = autoclass("android.provider.Settings")
                Uri = autoclass("android.net.Uri")
                activity = autoclass("org.kivy.android.PythonActivity").mActivity
                intent = Intent(Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION)
                intent.setData(Uri.parse("package:" + activity.getPackageName()))
                activity.startActivity(intent)
    except Exception:
        pass


def wrap(text, cols):
    """Hard wrap at screen width, like a terminal."""
    cols = max(1, cols)
    rows = []
    for part in text.split("\n"):
        rows += [part[i:i + cols] for i in range(0, len(part), cols)] or [""]
    return rows


CONTACT_COLORS = {"Telegram": "2aabee",     # Telegram light blue
                  "Discord": "5865f2"}      # Discord blue


def paint_contacts(safe):
    """The words Telegram / Discord and the user names from CONTACTS get their own colours. Each is replaced on
    its own (a name has no spaces), so a line that wraps between 'Telegram' and the name still looks right."""
    for name, value in CONTACTS:
        color = CONTACT_COLORS.get(name)
        if not color or not value:
            continue
        for word in (value, name):
            safe = re.sub(r"(?<![\w@])%s(?!\w)" % re.escape(escape_markup(word)),
                          lambda mo: "[color=%s]%s[/color]" % (color, mo.group(0)), safe)
    return safe


def fmt(kind, chunk):
    safe = paint_contacts(escape_markup(chunk))
    if kind is None:
        return safe
    return "[color=%s]%s[/color]" % (COLORS.get(kind, "ffffff"), safe)


class Terminal(FloatLayout):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.entries = []          # (kind, text); kind None = typed command
        self.hist_rows = []        # the same entries, split into screen rows
        self.offset = 0            # how many rows we are scrolled back from the end
        self.cursor_on = True
        self.cols, self.vis = 40, VISIBLE_ROWS

        self.label = Label(markup=True, font_name=FONT, font_size=FONT_SIZE,
                           halign="left", valign="top", size_hint=(None, None),
                           color=(1, 1, 1, 1))
        self.add_widget(self.label)

        # invisible field that captures keyboard input
        self.ti = TextInput(multiline=False, text_validate_unfocus=False,
                            write_tab=False, size_hint=(None, None),
                            size=(dp(2), dp(2)), pos=(0, 0), opacity=0)
        self.ti.bind(text=self.on_text, on_text_validate=self.submit)
        self.add_widget(self.ti)

        self.core = KiwiCore()
        self.core.screen_version = SCREEN_VERSION
        self.enable_crash_log()
        Window.bind(on_keyboard=self.on_key, on_request_close=self.on_close_request)
        for line in BANNER:
            self.add_entry("BANNER", line)
        for kind, msg in self.core.startup():
            self.show(kind, msg)
        self.show("INFO", "Versions: screen %s, core %s" % (SCREEN_VERSION, CORE_VERSION))
        self._last_poll_error = None

        self.measure()
        self.bind(size=self.on_resize)
        Clock.schedule_interval(self.blink, 0.5)
        Clock.schedule_interval(self.poll_jobs, 0.25)      # messages from a background download
        Clock.schedule_once(lambda dt: self.open_keyboard(), 0.3)
        self.on_resize()

    def enable_crash_log(self):
        """If the app dies, the reason is written to crash.log in the project folder:
        a native crash (faulthandler) and any uncaught Python error, also from background threads."""
        try:
            import faulthandler
            self._crash_log = open(os.path.join(self.core.project, "crash.log"), "a", buffering=1)
            self._crash_log.write("\n--- start %s (screen %s, core %s) ---\n"
                                  % (time.strftime("%Y-%m-%d %H:%M:%S"), SCREEN_VERSION, CORE_VERSION))
            faulthandler.enable(file=self._crash_log, all_threads=True)
            log = self._crash_log

            def write_error(kind, etype, value, tb):
                try:
                    log.write("[%s] %s\n" % (kind, time.strftime("%H:%M:%S")))
                    log.write("".join(traceback.format_exception(etype, value, tb)))
                except Exception:
                    pass

            def note(text):
                try:
                    log.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), text))
                except Exception:
                    pass
            self.note = note

            import builtins
            note("sys.exit = %r from %r; exit = %r; python %s"
                 % (sys.exit, getattr(sys.exit, "__module__", None), getattr(builtins, "exit", None), sys.version.split()[0]))

            import atexit
            atexit.register(lambda: note("atexit: Python is exiting normally"))

            real_exit = os._exit                           # who calls a hard exit? write the stack first
            def logged_exit(code=0):
                note("os._exit(%r) called from:\n%s" % (code, "".join(traceback.format_stack(limit=8))))
                real_exit(code)
            os._exit = logged_exit

            import signal
            for name in ("SIGTERM", "SIGHUP", "SIGINT", "SIGPIPE"):
                try:
                    num = getattr(signal, name)
                    def handler(signum, frame, name=name):
                        note("signal %s received" % name)
                        raise SystemExit(128 + signum) if name != "SIGPIPE" else BrokenPipeError()
                    signal.signal(num, handler)
                except Exception:
                    pass                                   # only the main thread may set handlers, some do not exist

            old_hook = sys.excepthook
            sys.excepthook = lambda t, v, tb: (write_error("main thread", t, v, tb), old_hook(t, v, tb))
            threading.excepthook = lambda a: write_error("thread %s" % getattr(a.thread, "name", "?"),
                                                         a.exc_type, a.exc_value, a.exc_traceback)
        except Exception:
            pass                                           # a crash log must never stop the app from starting

    def on_key(self, window, key, *rest):
        """Back (27) must not close the app: it only hides the keyboard. Other keys are not logged (privacy)."""
        if key == 27:
            if getattr(self, "note", None):
                self.note("Back/Escape key pressed (ignored, the app stays open)")
            Window.release_all_keyboards()
            return True
        return False

    def on_close_request(self, *args):
        if getattr(self, "note", None):
            self.note("Window close was requested (on_request_close)")
        return False                                       # False = let it close

    # --- sizes ---
    def measure(self):
        probe = Label(text="\n".join(["M" * 20] * 5), font_name=FONT, font_size=FONT_SIZE)
        probe.texture_update()
        self.cw = max(1.0, probe.texture_size[0] / 20.0)
        self.lh = max(1.0, probe.texture_size[1] / 5.0)

    def on_resize(self, *_):
        pad = dp(8)
        w, h = max(1, self.width - 2 * pad), max(1, self.height - 2 * pad)
        self.label.pos = (pad, pad)
        self.label.size = (w, h)
        self.label.text_size = (w, h)
        self.cols = max(1, int(w / self.cw))
        by_height = max(1, int(h / self.lh) - 1)   # spare row so the bottom is never cut off
        self.vis = min(VISIBLE_ROWS, by_height)
        self.rebuild()
        self.refresh()

    # --- history ---
    def rebuild(self):
        self.hist_rows = []
        for kind, text in self.entries:
            self.hist_rows += [fmt(kind, c) for c in wrap(text, self.cols)]

    def poll_jobs(self, dt=0):
        """Collect messages from a background job (download, zip...). It runs from several places on
        purpose, and an error in it is shown on the screen instead of disappearing."""
        try:
            res = self.core.poll()
            for kind, msg in res:
                if kind in ("SUCCESS", "ERROR") and getattr(self, "note", None):
                    self.note("job message: %s %s" % (kind, str(msg)[:80]))
                self.show(kind, msg)
            if res:
                self.refresh()
        except Exception as e:
            text = "Screen could not read the background job: %r" % (e,)
            if text != self._last_poll_error:                  # say it once, not every 0.25 s
                self._last_poll_error = text
                self.add_entry("CRITICAL", "[CRITICAL] " + text)
                self.refresh()

    def show(self, kind, msg):
        if kind == "CLEAR":                    # clear: wipe the screen
            self.entries, self.hist_rows, self.offset = [], [], 0
        elif kind == "TEXT":                   # plain output (echo, ?help): no tag
            self.add_entry(None, msg)
        else:
            self.add_entry(kind, "[%s] %s" % (kind, msg))

    def add_entry(self, kind, text):
        self.entries.append((kind, text))
        new = [fmt(kind, c) for c in wrap(text, self.cols)]
        self.hist_rows += new
        if self.offset > 0:
            self.offset += len(new)            # don't yank the view while the user reads above
        if len(self.entries) > MAX_ENTRIES:
            del self.entries[:len(self.entries) - MAX_ENTRIES]
            self.rebuild()

    # --- input ---
    def open_keyboard(self):
        self.ti.focus = False
        Clock.schedule_once(lambda dt: setattr(self.ti, "focus", True), 0.05)

    def on_text(self, *_):
        self.offset = 0
        self.poll_jobs()
        self.refresh()

    def submit(self, *_):
        cmd = self.ti.text
        self.ti.text = ""
        for line in input_lines(cmd):              # a pasted block runs line by line
            self.add_entry(None, line)
            for kind, msg in self.core.execute(line):
                self.show(kind, msg)
        self.offset = 0
        self.refresh()

    # --- drawing ---
    def blink(self, dt):
        self.cursor_on = not self.cursor_on
        self.poll_jobs()
        self.refresh()

    def refresh(self):
        cur = wrap(self.ti.text + ("_" if self.cursor_on else " "), self.cols)
        rows = self.hist_rows + [escape_markup(r) for r in cur]
        n = len(rows)
        self.offset = min(max(self.offset, 0), max(0, n - self.vis))
        end = n - self.offset
        start = max(0, end - self.vis)
        self.label.text = "\n".join(rows[start:end])

    # --- touch: scrolling and tapping ---
    def on_touch_down(self, touch):
        if getattr(touch, "is_mouse_scrolling", False):
            if touch.button == "scrollup":
                self.offset += 3
            elif touch.button == "scrolldown":
                self.offset -= 3
            self.refresh()
            return True
        touch.grab(self)
        touch.ud["acc"] = 0.0
        touch.ud["moved"] = 0.0
        return True

    def on_touch_move(self, touch):
        if touch.grab_current is not self:
            return False
        touch.ud["acc"] += touch.dy
        touch.ud["moved"] += abs(touch.dx) + abs(touch.dy)
        steps = int(touch.ud["acc"] / self.lh)
        if steps:
            self.offset -= steps               # finger down: show earlier lines
            touch.ud["acc"] -= steps * self.lh
            self.refresh()
        return True

    def on_touch_up(self, touch):
        if touch.grab_current is not self:
            return False
        touch.ungrab(self)
        if touch.ud["moved"] < dp(10):
            self.open_keyboard()
        return True


class KiwiApp(App):
    def on_stop(self):
        term = getattr(self, "term", None)
        note = getattr(term, "note", None)
        if note:
            note("Kivy on_stop: the app is closing")

    def on_resume(self):
        note = getattr(getattr(self, "term", None), "note", None)
        if note:
            note("Kivy on_resume")

    def on_pause(self):
        note = getattr(getattr(self, "term", None), "note", None)
        if note:
            note("Kivy on_pause (the app went to the background)")
        return True

    def build(self):
        if REQUEST_PERMISSIONS:
            request_storage_permissions()
        self.title = "Butter"
        self.term = Terminal()
        return self.term


if __name__ == "__main__":
    KiwiApp().run()
