"""Kiwi core: parses and runs commands. No UI code here, so it can be tested anywhere.

Create (free, no protection):
  +nf name          create a file            +nd name          create a folder
  +gf link [name]   download a file in the background (https only); "cancel" stops it
  +zf name [zip]    zip a file                +zd name [zip]    zip a folder
                    (the source is protected like cut: nothing from Android/, DCIM/ ... or outside /sdcard)
  -uzf archive      unzip into the current folder (existing names get three digits, after a y/n)
  -uzd archive      unzip into ONE new folder (a single top folder inside the archive is used as is)
Delete / rename / edit (always pass the protection: root zone, protected folders like Android/,
  and the folder Download itself; files inside Download are free):
  -df name          delete a file            -dd name          delete a folder
  -rf old new       rename a file            -rd old new       rename a folder
  +af name "text"   append a line to a text file
  ~of name "text"   overwrite a text file (creates it if missing); +of is the same
  ~of name "old" replace to "new"   change every "old" inside a text file into "new"
Buffer (working names, easy to rename in the command table):
  copy name ...     put files/folders into the buffer (nothing changes on disk)
  cut name ...      same, but they MOVE when you paste (protected like -df; nothing is removed until paste)
  paste             put the buffer into the current folder; names already taken get three digits
  ?buf              show what is in the buffer
Read / walk / print (free):
  ?afid [path]      list everything in a folder ("." or no path = current folder)
  ?vf path          show the contents of a text file
  ?vf path h N      first N lines (default 20)    ?vf path t N   last N lines
  ?vf path A B      lines A to B, numbered (numbers match ?g)
  >gt [path]        go to a folder (anywhere; no path = back to hegd)
  <l                go up one folder (anywhere)
  echo text         print text
  py script.py [args]  run a Python script inside the app (asks every time; the script ignores the protection)
  clear             clear the screen
  ?md               show the current folder (my directory)
  ?hist [N]         the last N commands (default 15)
  redo [N]          run a past command again: no N = the last one, N = the number shown by ?hist
  ?help [command]   list of commands (or one command)
  ?g "text" [path]  grep: find text inside files (folder = recursive); case-insensitive
  ?if path          info about a file         ?id [path]        info about a folder
  ?ff pattern [path] find files by name       ?fd pattern [path] find folders by name
                    (pattern with * or ? = wildcard match; plain word = name contains it)

Text with spaces or | goes in quotes: +af notes.txt "a | b". A quote opens only at the start of a word.

Chains:  cmd1 | cmd2 | cmd3
  Without !ma an ERROR stops everything after it. A y/n question pauses the chain
  until you answer; "n" counts as a stop. "!ma" works on its command and all after it:
  errors (and "n") no longer stop the chain.

Modifiers (at the start of a command; they can be stacked, e.g.  !hl !dry -dd folder):
  !ma cmd           this command and everything after it goes on after errors
  !hl cmd           hide the logs of cmd (questions and problems still show)
  !dry cmd          show what cmd would do, change nothing (reads and echo run as usual)
  !y cmd            answer "yes" to the question cmd asks (not for deleting a non-empty folder,
                    overwriting a file that has content, or running a script: those still ask)
  !chk A [| B] to C    run C only if A (and B) worked; with nto: only if they did NOT work
                    (the first plain word "to" or "nto" ends the condition; the check itself is silent;
                    a search ?g ?ff ?fd that finds nothing counts as "not passed")

Every command returns a list of (kind, text) where kind is INFO, WARNING, ERROR, DEBUG,
SUCCESS, CRITICAL, TEXT (plain output without a tag, used by echo and ?help) or CLEAR
(the screen wipes itself).
"""
import fnmatch
import hashlib
import io
import json
import os
import queue
import random
import runpy
import shutil
import socket
import stat
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import zipfile

INFO, WARNING, ERROR, TEXT = "INFO", "WARNING", "ERROR", "TEXT"
# Extra levels, ready for later (downloads etc.):
#   DEBUG    technical details, only shown after "debug on"
#   SUCCESS  a finished job (green)
#   CRITICAL worse than ERROR; stops a chain just like ERROR does
DEBUG, SUCCESS, CRITICAL = "DEBUG", "SUCCESS", "CRITICAL"
ERROR_KINDS = (ERROR, CRITICAL)
VERSION = "2026-10-29.2"                   # printed at startup: tells you which file version is really running
MODIFIERS = ("!ma", "!hl", "!dry", "!chk", "!y")
YES_NEVER = ("delete_dir", "overwrite", "pyrun", "trust")    # questions !y never answers: they would destroy data or open the door
TRUST_FILE = "trusted_scripts.json"        # path -> sha256 of the scripts you allowed to run without asking
WAKE_DEFAULT_MINUTES = 30                  # +wl without a number
WAKE_MAX_MINUTES = 480                     # the longest wakelock: 8 hours
MAX_TRUST_BYTES = 5 * 1024 * 1024          # a bigger script cannot be trusted (it would be slow to check)
FAST_WAIT = 0.2                            # a background job that is done within this time answers on the spot
HIDE_KINDS = (INFO, SUCCESS, DEBUG, TEXT)  # what !hl hides; questions and problems always stay visible
JOB_COMMANDS = ("+gf", "+zf", "+zd", "-uzf", "-uzd", "paste", "py", "pip", "tul", "redo")   # not allowed inside a !chk condition
MAX_PY_LINES = 1000                        # a script's output is cut after this many lines
MAX_HISTORY = 200                          # ?hist remembers this many commands (until the app is closed)

ROOT_DIR = "/storage/emulated/0"           # same as /sdcard
MAX_VIEW_LINES = 500                       # ?vf shows at most this many lines
MAX_TAIL_BYTES = 2000000                   # ?vf t never reads more than this from the end
MAX_RANGE_FILE_BYTES = 50000000            # ?vf A B refuses bigger files
MAX_VIEW_CHARS = 100000                    # ...and reads at most this many characters
MAX_GREP_RESULTS = 200                     # ?g stops after this many matching lines
MAX_GREP_FILES = 5000                      # ...or after scanning this many files
MAX_GREP_FILE_BYTES = 5000000              # bigger files are skipped by ?g and not line-counted by ?if
MAX_FIND_RESULTS = 300                     # ?ff / ?fd stop after this many results
MAX_WALK = 50000                           # ...or after looking at this many entries (also ?id)
MAX_LINE_SHOW = 160                        # long matching lines are cut
DOWNLOAD_TIMEOUT = 20                      # seconds without data before a download gives up
MAX_DOWNLOAD_BYTES = 200000000             # bigger downloads are refused / stopped
MAX_ZIP_SOURCE_BYTES = 2000000000          # +zf / +zd refuse to pack more than this
MAX_UNZIP_BYTES = 2000000000               # -uzf / -uzd refuse archives that unpack to more than this
MAX_UNZIP_FILES = 100000                   # ...or with more entries than this
PROJECT_NAME = "hegd"                      # project folder -> /sdcard/hegd
PROTECTED_NAMES = (                        # the folder AND everything inside is protected
    "Android", "DCIM", "Pictures", "Movies", "Music",
    "Documents", "Alarms", "Notifications", "Podcasts", "Ringtones",
)
FOLDER_ONLY_NAMES = ("Download",)          # only the folder itself is protected, files inside are free
HELP = [
    ("Make", [
        ("+nf name", "new file"),
        ("+nd name", "new folder"),
        ("+gf link [name]", "download a file (https)"),
        ("+zf name [zip]", "zip a file"),
        ("+zd name [zip]", "zip a folder"),
        ("-uzf archive", "unzip here"),
        ("-uzd archive", "unzip into a new folder"),
    ]),
    ("Change", [
        ('+af name "text"', "append a line to a file"),
        ('~of name "text"', "overwrite a text file"),
        ('~of name "a" replace to "b"', "replace text inside a file"),
        ("-rf old new", "rename a file"),
        ("-rd old new", "rename a folder"),
        ("-df name", "delete a file"),
        ("-dd name", "delete a folder"),
    ]),
    ("Buffer", [
        ("copy name ...", "copy to the buffer"),
        ("cut name ...", "cut (moves on paste)"),
        ("paste", "paste here"),
        ("?buf", "show the buffer"),
    ]),
    ("Look", [
        ("?afid [path]", "list a folder"),
        ("?vf path", "view a text file"),
        ("?vf path h 20", "first 20 lines"),
        ("?vf path t 20", "last 20 lines"),
        ("?vf path 120 140", "lines 120 to 140"),
        ("?if path", "file info"),
        ("?id [path]", "folder info"),
        ('?g "text" [path]', "search text in files"),
        ("?ff pattern [path]", "find files by name"),
        ("?fd pattern [path]", "find folders by name"),
    ]),
    ("Walk", [
        (">gt [path]", "go to a folder"),
        ("<l", "go up one folder"),
        ("?md", "where am I"),
    ]),
    ("Modifiers", [
        ("!ma cmd", "keep going after errors"),
        ("!hl cmd", "hide the logs of cmd"),
        ("!dry cmd", "show, do not do"),
        ("!y cmd", "say yes to its question"),
        ("!chk A to B", "run B if A works"),
        ("!chk A nto B", "run B if A fails"),
    ]),
    ("Other", [
        ("echo text", "print text"),
        ("py file [args]", "run a Python script"),
        ("pip command", "install libraries: pip install rich, pip list ... (runs hpip.py)"),
        ("tul command", "Hegd tools: tul install network, tul updates, tul update --all (runs tul.py)"),
        ("+wl [min]", "keep the processor awake (30 min by default, 480 at most)"),
        ("-wl", "release the wakelock"),
        ("?wl", "wakelock and battery-limit status"),
        ("bat", "ask Android to lift battery limits for Hegd"),
        ("+tr file", "trust a script: it runs without asking while its content stays the same"),
        ("-tr file", "stop trusting a script"),
        ("?tr", "list trusted scripts"),
        ("?hist [N]", "last commands"),
        ("redo [N]", "run a past command again"),
        ("clear", "clear the screen"),
        ("debug [on|off]", "show technical details"),
        ("cancel", "stop a running job"),
        ("?help [command]", "this list"),
    ]),
]
HELP_LEGEND = [
    "a | b     run a, then b",
    '"text"    quotes keep spaces and |',
]
LONE_MA = [("!ma", False)]                 # a segment that is only "!ma"


def clean_line(line):
    """Tabs -> spaces, drop control characters so the screen stays tidy."""
    return "".join(ch for ch in line.replace("\t", "    ") if ch >= " ")


def human_size(n):
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return ("%d %s" % (size, unit)) if unit == "B" else ("%.2f %s" % (size, unit))
        size /= 1024


def input_lines(text):
    """A pasted block runs line by line, like in a terminal. Blank lines inside a block are skipped."""
    lines = text.splitlines()
    if len(lines) <= 1:
        return [text]
    return [l for l in lines if l.strip()]


def parse_line(line):
    """Split a line into chain segments (by |), each a list of (word, was_quoted).

    Spaces and | inside quotes are kept. A quote (" or ') opens only at the start of a word.
    Raises ValueError for an unclosed quote.
    """
    segments, words = [], []
    buf, quoted, in_word = "", False, False
    i, n = 0, len(line)
    while i < n:
        ch = line[i]
        if ch in ('"', "'") and not in_word:
            end = line.find(ch, i + 1)
            if end == -1:
                raise ValueError("Unclosed quote.")
            buf += line[i + 1:end]
            quoted = in_word = True
            i = end + 1
            continue
        if ch == "|" or ch.isspace():
            if in_word:
                words.append((buf, quoted))
                buf, quoted, in_word = "", False, False
            if ch == "|":
                segments.append(words)
                words = []
        else:
            buf += ch
            in_word = True
        i += 1
    if in_word:
        words.append((buf, quoted))
    segments.append(words)
    return segments


class Cancelled(Exception):
    """Raised inside a background job when the user typed cancel."""


class LineSink:
    """A file-like object that sends what a script prints to the screen, line by line."""

    def __init__(self, q, kind, budget):
        self.q, self.kind, self.buf, self.budget = q, kind, "", budget

    def write(self, text):
        self.buf += text
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            self.emit(line)
        return len(text)

    def emit(self, line):
        if self.budget["left"] > 0:
            self.budget["left"] -= 1
            self.q.put((self.kind, clean_line(line)))
            if self.budget["left"] == 0:
                self.q.put((WARNING, "... the output is cut after %d lines" % MAX_PY_LINES))

    def flush(self):
        if self.buf:
            self.emit(self.buf)
            self.buf = ""

    def isatty(self):
        return False


class AndroidPower:
    """Android's PowerManager through pyjnius. It works only inside the APK; elsewhere every call raises RuntimeError."""

    def __init__(self):
        self.lock = None

    def _ctx(self):
        try:
            from jnius import autoclass
        except ImportError:
            raise RuntimeError("this works only inside the Android app")
        return autoclass, autoclass("org.kivy.android.PythonActivity").mActivity

    def acquire(self, minutes):
        autoclass, activity = self._ctx()
        pm = activity.getSystemService(autoclass("android.content.Context").POWER_SERVICE)
        if self.lock is None:
            self.lock = pm.newWakeLock(autoclass("android.os.PowerManager").PARTIAL_WAKE_LOCK, "hegd:keepawake")
            self.lock.setReferenceCounted(False)
        self.lock.acquire(int(minutes) * 60 * 1000)        # Android lets it go by itself when the time is up

    def release(self):
        if self.lock is not None and self.lock.isHeld():
            self.lock.release()

    def held(self):
        try:
            return bool(self.lock is not None and self.lock.isHeld())
        except Exception:
            return False

    def battery_ignored(self):
        autoclass, activity = self._ctx()
        pm = activity.getSystemService(autoclass("android.content.Context").POWER_SERVICE)
        return bool(pm.isIgnoringBatteryOptimizations(activity.getPackageName()))

    def open_battery_settings(self):
        autoclass, activity = self._ctx()
        Intent, Settings = autoclass("android.content.Intent"), autoclass("android.provider.Settings")
        try:                                               # one tap: "allow Hegd to run without battery limits?"
            intent = Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS)
            intent.setData(autoclass("android.net.Uri").parse("package:" + activity.getPackageName()))
            activity.startActivity(intent)
        except Exception:                                  # otherwise the list of apps
            activity.startActivity(Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS))


class KiwiCore:
    def __init__(self, root=ROOT_DIR, project=None):
        self.root = os.path.realpath(root)
        self.project = os.path.realpath(project or os.path.join(root, PROJECT_NAME))
        self.protected = {os.path.join(self.root, n) for n in PROTECTED_NAMES}
        self.folder_only = {os.path.join(self.root, n) for n in FOLDER_ONLY_NAMES}
        self.cwd = self.project            # current folder (start in hegd)
        # the trust list lives in the app's private folder (other apps cannot edit it); without it, next to the project
        self.state_dir = os.environ.get("ANDROID_PRIVATE") or self.project
        self.power = AndroidPower()                    # wakelock and battery settings (Android only)
        self.wake_until = 0.0
        self.pending = None                # a question waiting for y/n
        self.chain = None                  # rest of a chain paused by that question
        self.cancelled = False
        self.debug = False                 # "debug on" shows DEBUG lines
        self.history = []                  # commands typed so far (for ?hist)
        self.dry = False                   # True only while a command runs under !dry
        self.py_thread = None              # the thread of a running script
        self.sync_jobs = False             # True while a !chk condition runs: background jobs run right there
        self.found_nothing = False         # a search (?g ?ff ?fd) that found nothing: a !chk condition counts it as "not passed"
        self.job = None                    # a running download (see +gf)
        self.allow_insecure = False        # only for tests: lets +gf use http://
        self.buffer = {"mode": None, "items": []}   # copy/cut buffer: absolute paths
        # name -> (handler, usage, min_args, max_args); handlers get a list of words
        self.commands = {
            "+nf": (self.cmd_new_file, "+nf test.txt", 1, 1),
            "+nd": (self.cmd_new_dir, "+nd myfolder", 1, 1),
            "-df": (self.cmd_del_file, "-df test.txt", 1, 1),
            "-dd": (self.cmd_del_dir, "-dd myfolder", 1, 1),
            "-rf": (self.cmd_rename_file, "-rf old.txt new", 2, 2),
            "-rd": (self.cmd_rename_dir, "-rd oldfolder newfolder", 2, 2),
            "+af": (self.cmd_append, '+af test.txt "some text"', 2, None),
            "~of": (self.cmd_overwrite, '~of test.txt "new text"   or   ~of test.txt "old" replace to "new"', 2, None),
            "+of": (self.cmd_overwrite, '+of test.txt "new text"', 2, None),
            "copy": (self.cmd_copy, 'copy a.txt "my folder"', 1, None),
            "cut": (self.cmd_cut, "cut a.txt", 1, None),
            "paste": (self.cmd_paste, "paste", 0, 0),
            "?buf": (self.cmd_buffer, "?buf", 0, 0),
            "?g": (self.cmd_grep, '?g "text" [folder]', 1, None),
            "?if": (self.cmd_info_file, "?if test.txt", 1, None),
            "?id": (self.cmd_info_dir, "?id .", 0, None),
            "?ff": (self.cmd_find_file, '?ff "*.txt" [folder]', 1, None),
            "?fd": (self.cmd_find_dir, '?fd "photo" [folder]', 1, None),
            "+gf": (self.cmd_get_file, "+gf https://site/file.zip [name]", 1, 2),
            "+zf": (self.cmd_zip_file, "+zf test.txt [archive]", 1, 2),
            "+zd": (self.cmd_zip_dir, "+zd myfolder [archive]", 1, 2),
            "-uzf": (self.cmd_unzip_here, "-uzf archive.zip", 1, None),
            "-uzd": (self.cmd_unzip_dir, "-uzd archive.zip", 1, None),
            "cancel": (self.cmd_cancel, "cancel", 0, 0),
            "clear": (self.cmd_clear, "clear", 0, 0),
            "debug": (self.cmd_debug, "debug on", 0, 1),
            "?md": (self.cmd_md, "?md", 0, 0),
            "?hist": (self.cmd_hist, "?hist [N]", 0, 1),
            "redo": (self.cmd_redo_inline, "redo [N]", 0, 1),
            "?help": (self.cmd_help, "?help [command]", 0, 1),
            "py": (self.cmd_py, "py script.py [args]", 1, None),
            "pip": (self.cmd_pip, "pip command [args]", 0, None),
            "tul": (self.cmd_tul, "tul command [args]", 0, None),
            "+wl": (self.cmd_wake_on, "+wl [minutes]", 0, 1),
            "-wl": (self.cmd_wake_off, "-wl", 0, 0),
            "?wl": (self.cmd_wake_status, "?wl", 0, 0),
            "bat": (self.cmd_battery, "bat", 0, 0),
            "+tr": (self.cmd_trust, "+tr script.py", 1, 1),
            "-tr": (self.cmd_untrust, "-tr script.py", 1, 1),
            "?tr": (self.cmd_trust_list, "?tr", 0, 0),
            "echo": (self.cmd_echo, 'echo "hello"', 0, None),
            "?afid": (self.cmd_list, "?afid .", 0, None),
            "?vf": (self.cmd_view_file, "?vf test.txt [h N | t N | A B]", 1, None),
            ">gt": (self.cmd_go, ">gt Download", 0, None),
            "<l": (self.cmd_up, "<l", 0, 0),
        }

    # ---------- protection ----------
    def check_path(self, path, deep_only=False):
        """Two stages: (1) root zone, (2) protected system folders (Android/ etc.)."""
        real = os.path.realpath(path)
        if real == self.root:
            return False, "The root folder itself cannot be touched."
        if not real.startswith(self.root + os.sep):
            return False, "'%s' is outside the allowed zone (%s)." % (real, self.root)
        for blocked in self.protected:
            if real == blocked or real.startswith(blocked + os.sep):
                return False, "'%s' is a protected system folder." % real
        if real in self.folder_only and not deep_only:
            return False, "'%s' is a protected folder (the folder itself cannot be changed, its contents can)." % real
        return True, ""

    def resolve(self, name, base=None, protect=True):
        """Turn a name into a path inside the current folder. Returns (path, error).
        protect=False skips the protection (only creating files and folders does that)."""
        if not name or name in (".", "..") or "/" in name or "\\" in name or "\x00" in name:
            return None, (ERROR, "Invalid name '%s'. Names cannot contain '/' (use >gt to change folder)." % name)
        path = os.path.join(base or self.cwd, name)
        if protect:
            ok, reason = self.check_path(path)
            if not ok:
                return None, (ERROR, reason)
        return path, None

    # ---------- helpers ----------
    def unique_name(self, name, is_dir, folder=None):
        """test.txt -> test468.txt (folders: name468). Retries until the name is free."""
        stem, ext = (name, "") if is_dir else os.path.splitext(name)
        while True:
            candidate = "%s%s%s" % (stem, str(random.randint(0, 999)).zfill(3), ext)
            if not os.path.lexists(os.path.join(folder or self.cwd, candidate)):
                return candidate

    def looks_like_text(self, path):
        with open(path, "rb") as f:
            return b"\x00" not in f.read(4096)

    def existing_text_file(self, name, folder=None):
        """A protected, existing, text file inside the folder. Returns (path, error)."""
        path, err = self.resolve(name, folder)
        if err:
            return None, err
        if not os.path.lexists(path):
            return None, (ERROR, "File '%s' not found. Create it first with +nf." % name)
        if os.path.isdir(path):
            return None, (ERROR, "'%s' is a folder." % name)
        if not self.looks_like_text(path):
            return None, (ERROR, "'%s' is not a text file." % name)
        return path, None

    def startup(self):
        ok, reason = self.check_path(self.project)
        if not ok:
            return [(ERROR, "Project folder is not allowed: " + reason)]
        try:
            os.makedirs(self.project, exist_ok=True)
        except OSError as e:
            return [(ERROR, "Cannot open project folder %s: %s" % (self.project, e.strerror or e))]
        return [(INFO, "Hegd ready. Project folder: " + self.project),
                (INFO, "Type ?help for the list of commands.")] + self.tools_hint()

    # ---------- entry point ----------
    def execute(self, line):
        """Run one line; commands can be chained with |"""
        if self.job:
            out = self.poll()                    # the job may have finished since the screen last looked
            if self.job:
                return out + self.during_job(line)
            return out + self.execute(line)
        if self.pending:
            return self.resume(line)
        words = line.split()
        if words and words[0] == "redo":
            return self.redo(words[1:])
        self.remember(line)
        try:
            segments = parse_line(line)
        except ValueError as e:
            return [(ERROR, str(e))]
        if len(segments) == 1 and not segments[0]:
            return []                      # blank line
        if len(segments) > 1 and any(not seg for seg in segments):
            return [(ERROR, "Empty command next to '|'.")]
        if all(seg == LONE_MA for seg in segments):
            return [(ERROR, "'!ma' needs a command after it, e.g. !ma +nf test.txt")]
        return self.run_chain(segments, False)

    def take_modifiers(self, words):
        """The leading !words of a command -> (list of modifiers, the rest). ValueError if one is unknown."""
        mods = []
        while words and not words[0][1] and words[0][0].startswith("!") and len(words[0][0]) > 1:
            if words[0][0] not in MODIFIERS:
                raise ValueError("Unknown modifier '%s'. Known: %s" % (words[0][0], " ".join(MODIFIERS)))
            mods.append(words[0][0])
            words = words[1:]
        return mods, words

    def run_chain(self, segments, ma):
        """Run in order. Without !ma an ERROR stops the rest; a y/n question or a download pauses the chain."""
        segments = list(segments)
        out = []
        i = 0
        while i < len(segments):
            try:
                mods, words = self.take_modifiers(list(segments[i]))
            except ValueError as e:
                return out + [(ERROR, str(e))] + self.stopped_note(segments[i + 1:])
            if "!ma" in mods:
                ma = True                  # !ma works on this command and everything after it
            if "!chk" in mods:
                new_segments, notes = self.expand_check(segments, i, [m for m in mods if m != "!chk"], words)
                if new_segments is None:                           # a usage error: "notes" is the error
                    return out + [notes] + self.stopped_note(segments[i + 1:])
                segments = new_segments
                out += notes
                continue                   # the chosen action (if any) now stands at this very place
            if not words:
                if any(m != "!ma" for m in mods):
                    return out + [(ERROR, "A modifier needs a command after it, e.g. %s +nf test.txt" % mods[0])] \
                        + self.stopped_note(segments[i + 1:])
                i += 1
                continue                   # a lone "!ma" only switches it on
            pre = [(DEBUG, "run: %s  (in %s)" % (" ".join(w for w, _ in words), self.cwd))] if self.debug else []
            self.dry = "!dry" in mods
            try:
                res = self.run_one([w for w, _ in words])
            finally:
                self.dry = False
            if "!y" in mods and self.pending and self.pending["type"] not in YES_NEVER:
                question = [t for k, t in res if k == WARNING]
                res = [r for r in res if r[0] != WARNING]
                if question:                                       # say what was answered, it is not silent
                    res.append((INFO, "Auto-yes: " + question[-1].replace(" (y/n)", "")))
                try:
                    res += self.answer("y")
                except OSError as e:
                    self.pending = None
                    res.append((ERROR, str(e.strerror or e)))
            shown = pre + res
            if "!hl" in mods:
                shown = [r for r in shown if r[0] not in HIDE_KINDS]
            out += shown
            if self.pending or self.job:   # a question or a download: pause here
                self.chain = {"rest": segments[i + 1:], "ma": ma}
                return out
            if not ma and any(kind in ERROR_KINDS for kind, _ in res):
                return out + self.stopped_note(segments[i + 1:])
            i += 1
        return out

    def find_to(self, words):
        """Index of the plain word 'to' / 'nto' ('replace to' of ~of does not count), or None."""
        for k, (w, quoted) in enumerate(words):
            if not quoted and w in ("to", "nto") and not (k > 0 and words[k - 1][0] == "replace"):
                return k
        return None

    def expand_check(self, segments, i, other_mods, words):
        """!chk A [| B ...] to|nto C : decide now and put C (or nothing) in place of the whole group.
        Returns (new segments, notes), or (None, error) when the check cannot be used."""
        usage = (ERROR, "Usage: !chk COMMAND [| COMMAND] to COMMAND   (nto = run it when the check fails)")
        cond, cur, j = [], list(words), i
        while True:
            k = self.find_to(cur)
            if k is not None:
                break
            cond.append(cur)
            j += 1
            if j >= len(segments):
                return None, usage
            cur = list(segments[j])
        before, keyword, after = cur[:k], cur[k][0], cur[k + 1:]
        if before:
            cond.append(before)
        if not cond or any(not c for c in cond) or not after:
            return None, usage
        passed, why = self.check_passes(cond, "!dry" in other_mods)
        if passed is None:
            return None, (ERROR, why)
        run_it = passed if keyword == "to" else not passed
        shown = " ".join(w for w, _ in after)
        notes = []
        if run_it:
            replacement = [[(m, False) for m in other_mods] + after]
            if self.debug and "!hl" not in other_mods:
                notes = [(DEBUG, "check %s, so '%s' runs" % ("passed" if passed else "failed", shown))]
        else:
            replacement = []
            if "!hl" not in other_mods:
                state = "passed" if passed else "failed (%s)" % why
                notes = [(INFO, "Check %s, so '%s' was skipped." % (state, shown))]
        return segments[:i] + replacement + segments[j + 1:], notes

    def check_passes(self, cond, dry):
        """Run the condition commands silently. Returns (True/False, first error) or (None, why it cannot run)."""
        for seg in cond:
            words = [w for w, _ in seg]
            if words[0].startswith("!"):
                return None, "Modifiers are not allowed inside a check condition."
            if words[0] in JOB_COMMANDS:
                return None, "'%s' cannot be used inside a check condition." % words[0]
            self.dry, self.sync_jobs, self.found_nothing = dry, True, False
            try:
                res = self.run_one(words)
            finally:
                self.dry, self.sync_jobs = False, False
            if self.pending:
                self.pending = None
                return None, "'%s' asks a question; a check condition cannot answer it." % words[0]
            bad = [text for kind, text in res if kind in ERROR_KINDS]
            if bad:
                return False, bad[0]
            if self.found_nothing:                  # a search that found nothing is "not passed"
                return False, "nothing found"
        return True, ""

    def redo(self, args):
        if len(args) > 1 or (args and not args[0].isdigit()):
            return [(ERROR, "Usage: redo  (the last command)  or  redo N  (number N from ?hist)")]
        if not self.history:
            return [(ERROR, "No commands yet.")]
        n = int(args[0]) if args else len(self.history)
        if not 1 <= n <= len(self.history):
            return [(ERROR, "There is no command number %d (see ?hist)." % n)]
        text = self.history[n - 1]
        return [(TEXT, "-> " + text)] + self.execute(text)           # runs exactly as if it was typed now

    def cmd_redo_inline(self, args):
        return [(ERROR, "redo works only as a whole line: type just  redo  or  redo N")]

    def remember(self, line):
        text = line.strip()
        if not text or text.split()[0] in ("?hist", "redo"):
            return
        if not self.history or self.history[-1] != text:      # a repeated command is stored once
            self.history.append(text)
            del self.history[:-MAX_HISTORY]

    def stopped_note(self, rest):
        n = sum(1 for seg in rest if seg and seg != LONE_MA)
        return [(INFO, "Stopped: %d command(s) not run." % n)] if n else []

    def resume(self, line):
        """The answer to a y/n question; then the paused chain continues or stops."""
        self.cancelled = False
        try:
            res = self.answer(line)
        except OSError as e:
            self.pending = None
            res = [(ERROR, str(e.strerror or e))]
        except Exception as e:
            self.pending = None
            res = [(CRITICAL, "Internal error: %r" % e)]
        if self.pending or self.job:       # still waiting: a wrong answer, or a download has started
            return res
        failed = self.cancelled or any(kind in ERROR_KINDS for kind, _ in res)
        return self.after_step(res, failed)

    def after_step(self, res, failed):
        """A paused chain goes on (or stops) once a question or a download is settled."""
        chain, self.chain = self.chain, None
        if not chain:
            return res
        if failed and not chain["ma"]:
            return res + self.stopped_note(chain["rest"])
        if not chain["rest"]:
            return res
        return res + self.run_chain(chain["rest"], chain["ma"])

    def poll(self):
        """The screen calls this several times a second: messages from a running download,
        and, when it ends, the rest of the paused chain."""
        if not self.job:
            return []
        out, finished, failed = [], False, False
        while True:
            try:
                kind, msg = self.job["queue"].get_nowait()
            except queue.Empty:
                break
            if kind == "_done":
                finished, failed = True, msg
                break
            out.append((kind, msg))
        if not finished and not self.job["thread"].is_alive() and self.job["queue"].empty():
            out.append((ERROR, "The background job ended unexpectedly."))    # the worker died without a word
            finished, failed = True, True
        if not finished:
            return out
        self.job = None
        return self.after_step(out, failed)

    def during_job(self, line):
        if line.strip().lower() == "cancel":
            job = self.job
            job["cancel"].set()
            self.job = None                      # we do not wait: the worker notices, stops and removes its leftovers
            if job["label"] == "python":
                self.interrupt(job["thread"])
            return self.after_step([(INFO, "Cancelled.")], True)
        return [(WARNING, "A %s is running. Wait for it, or type: cancel" % self.job["label"])]

    def run_one(self, words):
        try:
            entry = self.commands.get(words[0])
            if entry is None:
                return [(ERROR, "Unknown command '%s'." % words[0])]
            handler, usage, min_args, max_args = entry
            args = words[1:]
            if len(args) < min_args:
                return [(ERROR, "Usage: " + usage)]
            if max_args == 0 and args:
                return [(ERROR, "'%s' takes no arguments." % words[0])]
            if max_args is not None and len(args) > max_args:
                return [(ERROR, "Too many arguments (put names with spaces in quotes). Usage: " + usage)]
            return handler(args)
        except OSError as e:
            self.pending = None
            return [(ERROR, str(e.strerror or e))]
        except Exception as e:                    # a bug must show up as a message, not close the app
            self.pending = None
            return [(CRITICAL, "Internal error in '%s': %r" % (words[0], e))]

    # ---------- print ----------
    def cmd_echo(self, args):
        return [(TEXT, " ".join(args))]

    def cmd_clear(self, args):
        if self.dry:
            return [(INFO, "Would clear the screen.")]
        return [("CLEAR", "")]

    def cmd_debug(self, args):
        if args and args[0] not in ("on", "off"):
            return [(ERROR, "Usage: debug on | debug off")]
        if args and self.dry:
            return [(INFO, "Would turn debug %s." % args[0])]
        if args:
            self.debug = args[0] == "on"
        return [(INFO, "Debug is %s." % ("on" if self.debug else "off"))]

    def cmd_hist(self, args):
        n = 15
        if args:
            if not args[0].isdigit() or int(args[0]) < 1:
                return [(ERROR, "Usage: ?hist [N]  (N = how many commands, at least 1)")]
            n = int(args[0])
        if not self.history:
            return [(INFO, "No commands yet.")]
        first = max(0, len(self.history) - n)
        rows = ["%3d  %s" % (i + 1, self.history[i]) for i in range(first, len(self.history))]
        return [(TEXT, "\n".join(rows))]

    def cmd_md(self, args):
        return [(INFO, self.cwd)]

    def cmd_help(self, args):
        def row(u, d):
            return ["  %-19s%s" % (u, d)] if len(u) <= 18 else ["  " + u, "      " + d]
        lines = lambda pairs: [r for u, d in pairs for r in row(u, d)]
        if args:
            word = args[0]
            found = [(u, d) for _, pairs in HELP for u, d in pairs if u.split()[0] == word]
            if not found:
                return [(ERROR, "No help for '%s'. Try ?help" % word)]
            return [(TEXT, "\n".join(lines(found)))]
        out = ["Kiwi commands"]
        for title, pairs in HELP:
            out.append(title + ":")
            out += lines(pairs)
        out.append("")
        out += HELP_LEGEND
        return [(TEXT, "\n".join(out))]

    # ---------- navigation (free: you can go anywhere) ----------
    def cmd_go(self, args):
        raw = " ".join(args).strip()
        if not raw:
            target = self.project                  # ">gt" alone = back to hegd
        elif os.path.isabs(raw):
            target = raw
        else:
            target = os.path.join(self.cwd, raw)   # relative to the current folder
        real = os.path.realpath(target)
        if not os.path.lexists(real):
            return [(ERROR, "Path '%s' not found." % raw)]
        if not os.path.isdir(real):
            return [(ERROR, "'%s' is a file, not a folder." % raw)]
        if not os.access(real, os.R_OK | os.X_OK):
            return [(ERROR, "Permission denied: %s" % real)]
        if self.dry:
            return [(INFO, "Would go to %s." % real)]
        self.cwd = real
        return [(INFO, "Now in " + real)]

    def cmd_up(self, args):
        parent = os.path.dirname(self.cwd)
        if parent == self.cwd:
            return [(ERROR, "Already at the top.")]
        if self.dry:
            return [(INFO, "Would go to %s." % parent)]
        self.cwd = parent
        return [(INFO, "Now in " + parent)]

    # ---------- list (read-only) ----------
    def cmd_list(self, args):
        raw = " ".join(args).strip()
        if raw in ("", "."):
            path = self.cwd
        elif os.path.isabs(raw):
            path = raw
        else:
            path = os.path.join(self.cwd, raw)           # relative to the current folder
        real = os.path.realpath(path)
        if not os.path.lexists(real):
            return [(ERROR, "Path '%s' not found." % (raw or "."))]
        if not os.path.isdir(real):
            return [(ERROR, "'%s' is a file, not a folder." % raw)]
        try:
            names = sorted(os.listdir(real), key=str.lower)
        except PermissionError:
            return [(ERROR, "Permission denied: %s" % real)]
        folders = [n + "/" for n in names if os.path.isdir(os.path.join(real, n))]
        files = [n for n in names if not os.path.isdir(os.path.join(real, n))]
        head = "%s (%d folder(s), %d file(s))" % (real, len(folders), len(files))
        if not names:
            return [(INFO, head + "\n(empty)")]
        return [(INFO, head + "\n" + "\n".join(folders + files))]

    # ---------- view file (read-only, can read anywhere) ----------
    def parse_view(self, args):
        """?vf path | ?vf path h [N] | ?vf path t [N] | ?vf path A B  ->  (path, mode, a, b)"""
        n = len(args)
        if n >= 3 and args[-1].isdigit() and args[-2].isdigit():
            return " ".join(args[:-2]), "range", int(args[-2]), int(args[-1])
        if n >= 3 and args[-2] in ("h", "t") and args[-1].isdigit():
            return " ".join(args[:-2]), args[-2], int(args[-1]), None
        if n >= 2 and args[-1] in ("h", "t"):
            return " ".join(args[:-1]), args[-1], 20, None
        return " ".join(args), "all", None, None

    def cmd_view_file(self, args):
        raw, mode, a, b = self.parse_view(args)
        raw = raw.strip()
        path = raw if os.path.isabs(raw) else os.path.join(self.cwd, raw)
        real = os.path.realpath(path)
        if not os.path.lexists(real):
            return [(ERROR, "File '%s' not found." % raw)]
        if os.path.isdir(real):
            return [(ERROR, "'%s' is a folder. Use ?afid to list it." % raw)]
        try:
            if mode == "all":
                return self.view_all(real, raw)
            if not self.looks_like_text(real):
                return [(ERROR, "'%s' is not a text file." % raw)]
            if mode == "h":
                return self.view_head(real, a)
            if mode == "t":
                return self.view_tail(real, a)
            return self.view_range(real, a, b)
        except UnicodeDecodeError:
            return [(ERROR, "'%s' is not a UTF-8 text file." % raw)]
        except PermissionError:
            return [(ERROR, "Permission denied: %s" % real)]

    def view_all(self, real, raw):
        with open(real, "r", encoding="utf-8") as f:
            text = f.read(MAX_VIEW_CHARS + 1)
        if "\x00" in text:
            return [(ERROR, "'%s' is not a text file." % raw)]
        cut = len(text) > MAX_VIEW_CHARS
        lines = text[:MAX_VIEW_CHARS].splitlines()
        shown = lines[:MAX_VIEW_LINES]
        cut = cut or len(lines) > MAX_VIEW_LINES
        out = [real] + ([clean_line(l) for l in shown] or ["(empty file)"])
        if cut:
            out.append("... cut: showing the first %d line(s). Use ?vf path h/t N or A B for more." % len(shown))
        return [(INFO, "\n".join(out))]

    def view_head(self, real, n):
        if n < 1:
            return [(ERROR, "N must be at least 1.")]
        capped = n > MAX_VIEW_LINES
        n = min(n, MAX_VIEW_LINES)
        shown, more = [], False
        with open(real, "r", encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i >= n:
                    more = True
                    break
                shown.append(clean_line(line.rstrip("\r\n")))
        out = ["%s (first %d line(s))" % (real, n)] + (shown or ["(empty file)"])
        if capped:
            out.append("... limit is %d lines per view" % MAX_VIEW_LINES)
        elif more:
            out.append("... more below")
        return [(INFO, "\n".join(out))]

    def view_tail(self, real, n):
        if n < 1:
            return [(ERROR, "N must be at least 1.")]
        capped = n > MAX_VIEW_LINES
        n = min(n, MAX_VIEW_LINES)
        size = os.path.getsize(real)
        data, pos, newlines = b"", size, 0
        with open(real, "rb") as f:                  # read from the end, never the whole file
            while pos > 0 and newlines <= n and len(data) < MAX_TAIL_BYTES:
                step = min(8192, pos)
                pos -= step
                f.seek(pos)
                chunk = f.read(step)
                newlines += chunk.count(b"\n")
                data = chunk + data
        if pos > 0:                                  # we started in the middle of a line: drop it
            cut = data.find(b"\n")
            data = data[cut + 1:] if cut >= 0 else b""
        if b"\x00" in data:
            return [(ERROR, "'%s' is not a text file." % real)]
        lines = data.decode("utf-8").splitlines()[-n:]
        out = ["%s (last %d line(s))" % (real, n)] + ([clean_line(l) for l in lines] or ["(empty file)"])
        if capped:
            out.append("... limit is %d lines per view" % MAX_VIEW_LINES)
        elif pos > 0 and len(lines) < n:
            out.append("... lines are too long, showing only what fits")
        return [(INFO, "\n".join(out))]

    def view_range(self, real, a, b):
        if a < 1 or b < a:
            return [(ERROR, "Bad range %d-%d: lines start at 1 and the second number cannot be smaller." % (a, b))]
        if os.path.getsize(real) > MAX_RANGE_FILE_BYTES:
            return [(ERROR, "The file is too big for line ranges (limit %d MB)." % (MAX_RANGE_FILE_BYTES // 1000000))]
        shown, total, cut = [], 0, False
        with open(real, "r", encoding="utf-8") as f:
            for i, line in enumerate(f, 1):
                total = i
                if i < a:
                    continue
                if i > b:
                    break
                if i - a >= MAX_VIEW_LINES:
                    cut = True
                    break
                shown.append("%d: %s" % (i, clean_line(line.rstrip("\r\n"))))
        if not shown:
            return [(INFO, "The file has only %d line(s)." % total)]
        out = ["%s (lines %d-%d)" % (real, a, int(shown[-1].split(":")[0]))] + shown
        if cut:
            out.append("... limit is %d lines per view" % MAX_VIEW_LINES)
        return [(INFO, "\n".join(out))]

    # ---------- search and info (read-only, can read anywhere) ----------
    def where(self, raw):
        """"" or "." = current folder; absolute path as is; otherwise relative to the current folder."""
        raw = raw.strip()
        if raw in ("", "."):
            return os.path.realpath(self.cwd)
        return os.path.realpath(raw if os.path.isabs(raw) else os.path.join(self.cwd, raw))

    def walk_files(self, root):
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
            for fn in sorted(filenames):
                path = os.path.join(dirpath, fn)
                if os.path.isfile(path):
                    yield path

    def cmd_grep(self, args):
        needle, raw = args[0], " ".join(args[1:])
        target = self.where(raw)
        if not os.path.lexists(target):
            return [(ERROR, "Path '%s' not found." % (raw.strip() or "."))]
        return self.run_bg("search", self.grep_run, target, needle)

    def grep_run(self, job, target, needle):
        if os.path.isdir(target):
            files, base = self.walk_files(target), target
        else:
            files, base = [target], os.path.dirname(target)
        low = needle.lower()
        hits, hit_files, scanned, limit = [], set(), 0, False
        for path in files:
            if job["cancel"].is_set():
                return [(INFO, "Cancelled.")]
            if scanned >= MAX_GREP_FILES:
                limit = True
                break
            scanned += 1
            try:
                if os.path.getsize(path) > MAX_GREP_FILE_BYTES or not self.looks_like_text(path):
                    continue
                rel = os.path.relpath(path, base) if path != target else os.path.basename(path)
                with open(path, "r", encoding="utf-8") as f:
                    for n, line in enumerate(f, 1):
                        if low in line.lower():
                            hits.append("%s:%d: %s" % (rel, n, clean_line(line.rstrip("\r\n"))[:MAX_LINE_SHOW]))
                            hit_files.add(path)
                            if len(hits) >= MAX_GREP_RESULTS:
                                limit = True
                                break
            except (UnicodeDecodeError, OSError):
                continue                          # not UTF-8 text or unreadable: skip the file
            if limit:
                break
        where = target if os.path.isdir(target) else base
        if not hits:
            self.found_nothing = True
            return [(INFO, "No matches for '%s' in %s (looked at %d file(s))." % (needle, where, scanned))]
        head = "%d match(es) in %d file(s) for '%s' in %s" % (len(hits), len(hit_files), needle, where)
        tail = ["... stopped: limit reached, narrow the search"] if limit else []
        return [(INFO, "\n".join([head] + hits + tail))]

    def find(self, pattern, raw, want_dirs):
        root = self.where(raw)
        if not os.path.lexists(root):
            return [(ERROR, "Path '%s' not found." % (raw.strip() or "."))]
        if not os.path.isdir(root):
            return [(ERROR, "'%s' is a file, not a folder." % raw.strip())]
        return self.run_bg("search", self.find_run, root, pattern, want_dirs)

    def find_run(self, job, root, pattern, want_dirs):
        low = pattern.lower()
        wild = any(ch in pattern for ch in "*?[")
        results, seen, limit = [], 0, False
        for dirpath, dirnames, filenames in os.walk(root):
            if job["cancel"].is_set():
                return [(INFO, "Cancelled.")]
            dirnames.sort()
            for name in (dirnames if want_dirs else sorted(filenames)):
                lname = name.lower()
                if (fnmatch.fnmatchcase(lname, low) if wild else low in lname):
                    rel = os.path.relpath(os.path.join(dirpath, name), root)
                    results.append(rel + ("/" if want_dirs else ""))
            seen += len(dirnames) + len(filenames)
            if len(results) >= MAX_FIND_RESULTS or seen > MAX_WALK:
                limit = True
                break
        label = "folder" if want_dirs else "file"
        if not results:
            self.found_nothing = True
            return [(INFO, "No %ss match '%s' in %s." % (label, pattern, root))]
        results = results[:MAX_FIND_RESULTS]
        head = "%d %s(s) match '%s' in %s" % (len(results), label, pattern, root)
        tail = ["... stopped: limit reached, narrow the search"] if limit else []
        return [(INFO, "\n".join([head] + results + tail))]

    def cmd_find_file(self, args):
        return self.find(args[0], " ".join(args[1:]), False)

    def cmd_find_dir(self, args):
        return self.find(args[0], " ".join(args[1:]), True)

    def when(self, st):
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))

    def cmd_info_file(self, args):
        raw = " ".join(args).strip()
        real = self.where(raw)
        if not os.path.lexists(real):
            return [(ERROR, "File '%s' not found." % raw)]
        if os.path.isdir(real):
            return [(ERROR, "'%s' is a folder. Use ?id for folders." % raw)]
        st = os.stat(real)
        ext = os.path.splitext(real)[1] or "(none)"
        try:
            is_text = self.looks_like_text(real)
        except OSError:
            is_text = None
        if st.st_size == 0:
            kind = "empty file"
        elif is_text is None:
            kind = "unknown (no permission to read)"
        elif not is_text:
            kind = "binary"
        elif st.st_size > MAX_GREP_FILE_BYTES:
            kind = "text (too big to count lines)"
        else:
            with open(real, "rb") as f:
                data = f.read()
            lines = data.count(b"\n") + (0 if data.endswith(b"\n") else 1)
            kind = "text, %d line(s)" % lines
        return [(INFO, "\n".join([
            "File: " + real,
            "Size: %s (%d bytes)" % (human_size(st.st_size), st.st_size),
            "Type: " + kind,
            "Extension: " + ext,
            "Modified: " + self.when(st),
            "Permissions: " + stat.filemode(st.st_mode),
        ]))]

    def cmd_info_dir(self, args):
        raw = " ".join(args).strip()
        real = self.where(raw)
        if not os.path.lexists(real):
            return [(ERROR, "Folder '%s' not found." % (raw or "."))]
        if not os.path.isdir(real):
            return [(ERROR, "'%s' is a file. Use ?if for files." % raw)]
        st = os.stat(real)
        try:
            direct = os.listdir(real)
        except PermissionError:
            return [(ERROR, "Permission denied: %s" % real)]
        d_dirs = sum(1 for n in direct if os.path.isdir(os.path.join(real, n)))
        d_files = len(direct) - d_dirs
        return self.run_bg("scan", self.info_dir_run, real, d_dirs, d_files, st)

    def info_dir_run(self, job, real, d_dirs, d_files, st):
        n_dirs = n_files = total = seen = 0
        limit = False
        for dirpath, dirnames, filenames in os.walk(real):
            if job["cancel"].is_set():
                return [(INFO, "Cancelled.")]
            n_dirs += len(dirnames)
            n_files += len(filenames)
            for fn in filenames:
                try:
                    total += os.lstat(os.path.join(dirpath, fn)).st_size
                except OSError:
                    pass
            seen += len(dirnames) + len(filenames)
            if seen > MAX_WALK:
                limit = True
                break
        more = " (at least, stopped counting)" if limit else ""
        return [(INFO, "\n".join([
            "Folder: " + real,
            "Directly inside: %d folder(s), %d file(s)" % (d_dirs, d_files),
            "Everything inside: %d folder(s), %d file(s), %s%s" % (n_dirs, n_files, human_size(total), more),
            "Modified: " + self.when(st),
            "Permissions: " + stat.filemode(st.st_mode),
        ]))]

    def ask(self, pending, text):
        """Ask a y/n question. Under !dry it only says that the question would be asked."""
        if self.dry:
            return [(INFO, "Would ask: " + text.replace(" (y/n)", ""))]
        self.pending = pending
        return [(WARNING, text)]

    # ---------- create (free: no protection) ----------
    def cmd_new_file(self, args):
        return self.new(args[0], False)

    def cmd_new_dir(self, args):
        return self.new(args[0], True)

    def new(self, name, is_dir):
        path, err = self.resolve(name, protect=False)
        if err:
            return [err]
        label = "Folder" if is_dir else "File"
        if os.path.lexists(path):
            suggested = self.unique_name(name, is_dir, self.cwd)
            return self.ask({"type": "create", "is_dir": is_dir, "suggested": suggested, "dir": self.cwd},
                            "%s '%s' already exists. Create it as '%s' instead? (y/n)" % (label, name, suggested))
        if self.dry:
            return [(INFO, "Would create %s '%s'." % (label.lower(), name))]
        return self.create(name, is_dir)

    def create(self, name, is_dir, folder=None):
        path, err = self.resolve(name, folder, protect=False)
        if err:
            return [err]
        if is_dir:
            os.mkdir(path)
        else:
            open(path, "x").close()          # "x" = never overwrite
        return [(INFO, "%s '%s' created." % ("Folder" if is_dir else "File", name))]

    # ---------- delete ----------
    def cmd_del_file(self, args):
        name = args[0]
        path, err = self.resolve(name)
        if err:
            return [err]
        if not os.path.lexists(path):
            return [(ERROR, "File '%s' not found." % name)]
        if os.path.isdir(path) and not os.path.islink(path):
            return [(ERROR, "'%s' is a folder. Use -dd to delete folders." % name)]
        if self.dry:
            return [(INFO, "Would delete file '%s'." % name)]
        os.remove(path)
        return [(INFO, "File '%s' deleted." % name)]

    def cmd_del_dir(self, args):
        name = args[0]
        path, err = self.resolve(name)
        if err:
            return [err]
        if not os.path.lexists(path):
            return [(ERROR, "Folder '%s' not found." % name)]
        if os.path.islink(path) or not os.path.isdir(path):
            return [(ERROR, "'%s' is a file. Use -df to delete files." % name)]
        count = len(os.listdir(path))
        if count:
            return self.ask({"type": "delete_dir", "name": name, "dir": self.cwd},
                            "Folder '%s' is not empty (%d item(s)). Delete it with everything inside? (y/n)"
                            % (name, count))
        if self.dry:
            return [(INFO, "Would delete folder '%s'." % name)]
        os.rmdir(path)
        return [(INFO, "Folder '%s' deleted." % name)]

    # ---------- rename ----------
    def cmd_rename_file(self, args):
        return self.rename(args[0], args[1], False)

    def cmd_rename_dir(self, args):
        return self.rename(args[0], args[1], True)

    def rename(self, old, new, is_dir):
        old_path, err = self.resolve(old)
        if err:
            return [err]
        new_path, err = self.resolve(new)
        if err:
            return [err]
        label = "Folder" if is_dir else "File"
        if not os.path.lexists(old_path):
            return [(ERROR, "%s '%s' not found." % (label, old))]
        old_is_dir = os.path.isdir(old_path) and not os.path.islink(old_path)
        if is_dir and not old_is_dir:
            return [(ERROR, "'%s' is a file. Use -rf to rename files." % old)]
        if not is_dir and old_is_dir:
            return [(ERROR, "'%s' is a folder. Use -rd to rename folders." % old)]
        if old == new:
            return [(ERROR, "The new name is the same as the old one.")]
        if os.path.lexists(new_path) and not self.same_file(old_path, new_path):
            suggested = self.unique_name(new, is_dir, self.cwd)
            return self.ask({"type": "rename", "is_dir": is_dir, "old": old, "suggested": suggested, "dir": self.cwd},
                            "%s '%s' already exists. Rename '%s' to '%s' instead? (y/n)"
                            % (label, new, old, suggested))
        if self.dry:
            return [(INFO, "Would rename %s '%s' to '%s'." % (label.lower(), old, new))]
        return self.do_rename(old, new, is_dir, self.cwd)

    def same_file(self, a, b):
        try:
            return os.path.samefile(a, b)        # e.g. only the letter case changes
        except OSError:
            return False

    def do_rename(self, old, new, is_dir, folder):
        old_path, err = self.resolve(old, folder)
        if err:
            return [err]
        new_path, err = self.resolve(new, folder)
        if err:
            return [err]
        os.rename(old_path, new_path)
        return [(INFO, "%s '%s' renamed to '%s'." % ("Folder" if is_dir else "File", old, new))]

    # ---------- write text ----------
    def cmd_append(self, args):
        name, text = args[0], " ".join(args[1:])
        path, err = self.existing_text_file(name)
        if err:
            return [err]
        if self.dry:
            return [(INFO, "Would add a line to '%s'." % name)]
        prefix = ""
        if os.path.getsize(path):
            with open(path, "rb") as f:
                f.seek(-1, os.SEEK_END)
                if f.read(1) != b"\n":
                    prefix = "\n"                # keep the new text on its own line
        with open(path, "a", encoding="utf-8") as f:
            f.write(prefix + text + "\n")
        return [(INFO, "Added a line to '%s'." % name)]

    def cmd_overwrite(self, args):
        rest = args[1:]
        for i in range(len(rest) - 1):                # ~of name "old" replace to "new"
            if rest[i] == "replace" and rest[i + 1] == "to":
                if len(rest) == i + 2:
                    return [(ERROR, 'Usage: ~of test.txt "old" replace to "new"')]
                return self.replace_text(args[0], " ".join(rest[:i]), " ".join(rest[i + 2:]))
        name, text = args[0], " ".join(args[1:])
        path, err = self.resolve(name)
        if err:
            return [err]
        if not os.path.lexists(path):
            if self.dry:
                return [(INFO, "Would create file '%s' with that text." % name)]
            return self.write_text(path, name, text, "created")
        path, err = self.existing_text_file(name)
        if err:
            return [err]
        if os.path.getsize(path):
            return self.ask({"type": "overwrite", "name": name, "text": text, "dir": self.cwd},
                            "File '%s' has content. Overwrite it? (y/n)" % name)
        if self.dry:
            return [(INFO, "Would overwrite the empty file '%s'." % name)]
        return self.write_text(path, name, text, "overwritten")

    def replace_text(self, name, old, new):
        if not old:
            return [(ERROR, "Nothing to replace: the text before 'replace to' is empty.")]
        path, err = self.existing_text_file(name)                  # protected like ~of and +af
        if err:
            return [err]
        if os.path.getsize(path) > MAX_GREP_FILE_BYTES:
            return [(ERROR, "The file is too big to edit (limit %d MB)." % (MAX_GREP_FILE_BYTES // 1000000))]
        try:
            with open(path, "r", encoding="utf-8", newline="") as f:   # newline="": keep the line endings
                text = f.read()
        except UnicodeDecodeError:
            return [(ERROR, "'%s' is not a UTF-8 text file." % name)]
        count = text.count(old)
        if count == 0:
            shown = old if len(old) <= 40 else old[:40] + "..."
            return [(ERROR, "'%s' not found in '%s'. Nothing changed." % (shown, name))]
        if self.dry:
            return [(INFO, "Would replace %d occurrence(s) in '%s'." % (count, name))]
        tmp = path + ".part"                                       # write aside, then swap: no half-written file
        try:
            with open(tmp, "w", encoding="utf-8", newline="") as f:
                f.write(text.replace(old, new))
            try:
                shutil.copymode(path, tmp)
            except OSError:
                pass
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        return [(INFO, "Replaced %d occurrence(s) in '%s'." % (count, name))]

    def write_text(self, path, name, text, verb):
        with open(path, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        return [(INFO, "File '%s' %s." % (name, verb))]

    # ---------- buffer: copy / cut / paste ----------
    def cmd_copy(self, args):
        return self.fill_buffer(args, "copy")

    def cmd_cut(self, args):
        return self.fill_buffer(args, "cut")

    def fill_buffer(self, names, mode):
        items = []
        for name in dict.fromkeys(names):          # no duplicates, keep the order
            # copying only reads (free); cutting removes the source, so it is protected like -df
            path, err = self.resolve(name, protect=(mode == "cut"))
            if err:
                return [err]
            if not os.path.lexists(path):
                return [(ERROR, "'%s' not found." % name)]
            items.append(path)
        if self.dry:
            return [(INFO, "Would put %d item(s) into the buffer (%s)." % (len(items), mode))]
        self.buffer = {"mode": mode, "items": items}
        shown = ", ".join(os.path.basename(i) for i in items)
        if mode == "copy":
            return [(INFO, "Copied to buffer: %s (%d item(s))." % (shown, len(items)))]
        return [(INFO, "Cut to buffer: %s (%d item(s)). They move when you paste." % (shown, len(items)))]

    def cmd_buffer(self, args):
        mode, items = self.buffer["mode"], self.buffer["items"]
        if not items:
            return [(INFO, "The buffer is empty.")]
        return [(INFO, "Buffer: %s, %d item(s)\n%s" % (mode, len(items), "\n".join(items)))]

    def cmd_paste(self, args):
        items = self.buffer["items"]
        if not items:
            return [(ERROR, "The buffer is empty. Use copy or cut first.")]
        folder = self.cwd
        taken = []
        for src in items:
            dst = os.path.join(folder, os.path.basename(src))
            if os.path.lexists(dst) and not (self.buffer["mode"] == "cut" and self.same_file(src, dst)):
                taken.append(os.path.basename(src))
        if taken:
            return self.ask({"type": "paste", "dir": folder},
                            "%d item(s) already exist here: %s. Paste them with new names? (y/n)"
                            % (len(taken), ", ".join(taken)))
        if self.dry:
            if self.buffer["mode"] == "cut":                    # a cut is protected: say so in the preview too
                for src in items:
                    ok, reason = self.check_path(src)
                    if not ok:
                        return [(ERROR, reason)]
            verb = "move" if self.buffer["mode"] == "cut" else "copy"
            return [(INFO, "Would %s %d item(s) into %s." % (verb, len(items), folder))]
        return self.start_paste(folder)

    def inside(self, folder, src):
        a, b = os.path.realpath(folder), os.path.realpath(src)
        return a == b or a.startswith(b + os.sep)

    def progress(self, job, prog, nbytes=0, nfiles=0):
        prog["bytes"] += nbytes
        prog["files"] += nfiles
        if job["debug"] and time.time() - prog["last"] >= 1.0:
            prog["last"] = time.time()
            job["queue"].put((DEBUG, "%d file(s), %s so far" % (prog["files"], human_size(prog["bytes"]))))

    def copy_file_chunked(self, job, src, dst, prog):
        try:
            with open(src, "rb") as fi, open(dst, "wb") as fo:
                while True:
                    if job["cancel"].is_set():
                        raise Cancelled()
                    chunk = fi.read(1024 * 1024)
                    if not chunk:
                        break
                    fo.write(chunk)
                    self.progress(job, prog, len(chunk))
        except BaseException:                       # cancelled or failed: no half-written file stays
            try:
                os.remove(dst)
            except OSError:
                pass
            raise
        self.progress(job, prog, 0, 1)
        try:
            shutil.copystat(src, dst)
        except OSError:
            pass

    def copy_item(self, job, src, dst, prog):
        if os.path.islink(src):
            os.symlink(os.readlink(src), dst)
            return
        if not os.path.isdir(src):
            self.copy_file_chunked(job, src, dst, prog)
            return
        try:
            os.mkdir(dst)
            for dirpath, dirnames, filenames in os.walk(src):
                rel = os.path.relpath(dirpath, src)
                target_dir = dst if rel == "." else os.path.join(dst, rel)
                os.makedirs(target_dir, exist_ok=True)
                for dn in dirnames:                  # links to folders are copied as links, not followed
                    p = os.path.join(dirpath, dn)
                    if os.path.islink(p):
                        os.symlink(os.readlink(p), os.path.join(target_dir, dn))
                for fn in filenames:
                    p, t = os.path.join(dirpath, fn), os.path.join(target_dir, fn)
                    if os.path.islink(p):
                        os.symlink(os.readlink(p), t)
                    else:
                        self.copy_file_chunked(job, p, t, prog)
        except BaseException:                       # cancelled or failed: remove the half copy
            shutil.rmtree(dst, ignore_errors=True)
            raise

    def move_item(self, job, src, dst, prog):
        try:
            os.rename(src, dst)                      # same storage: instant
            return
        except OSError:
            pass                                     # another storage, or not allowed: copy, then remove the original
        self.copy_item(job, src, dst, prog)
        try:
            if os.path.isdir(src) and not os.path.islink(src):
                shutil.rmtree(src)
            else:
                os.remove(src)
        except OSError:
            if os.path.isdir(dst) and not os.path.islink(dst):    # could not remove the original: no duplicate
                shutil.rmtree(dst, ignore_errors=True)
            else:
                try:
                    os.remove(dst)
                except OSError:
                    pass
            raise

    def start_paste(self, folder):
        items, mode = list(self.buffer["items"]), self.buffer["mode"]
        return self.launch("paste", self.paste_worker, (mode, items, folder),
                           "Pasting %d item(s) ... (type cancel to stop)" % len(items))

    def paste_worker(self, job, mode, items, folder):
        q, started = job["queue"], time.time()
        prog = {"bytes": 0, "files": 0, "last": time.time()}
        done, ok, problems, stopped, failed = [], 0, 0, False, True
        try:
            for src in items:
                if job["cancel"].is_set():
                    stopped = True
                    break
                name = os.path.basename(src)
                try:
                    if not os.path.lexists(src):
                        problems += 1
                        q.put((ERROR, "'%s' not found any more." % name))
                        continue
                    is_dir = os.path.isdir(src) and not os.path.islink(src)
                    if is_dir and self.inside(folder, src):
                        problems += 1
                        q.put((ERROR, "Cannot paste folder '%s' into itself." % name))
                        continue
                    dst = os.path.join(folder, name)
                    if mode == "cut":
                        allowed, reason = self.check_path(src)
                        if not allowed:
                            problems += 1
                            q.put((ERROR, reason))
                            continue
                        if os.path.lexists(dst) and self.same_file(src, dst):
                            q.put((INFO, "'%s' is already here." % name))
                            done.append(src)
                            continue
                    final = name
                    if os.path.lexists(dst):
                        final = self.unique_name(name, is_dir, folder)
                        dst = os.path.join(folder, final)
                    if mode == "cut":
                        self.move_item(job, src, dst, prog)
                        done.append(src)
                    else:
                        self.copy_item(job, src, dst, prog)
                    ok += 1
                    verb = "Moved" if mode == "cut" else "Copied"
                    q.put((INFO, "%s '%s'%s." % (verb, name, "" if final == name else " as '%s'" % final)))
                except Cancelled:
                    stopped = True
                    break
                except OSError as e:
                    problems += 1
                    q.put((ERROR, "'%s': %s" % (name, e.strerror or e)))
            if mode == "cut":                        # a cut is used once; keep only what is left
                left = [i for i in items if i not in done]
                self.buffer = {"mode": "cut" if left else None, "items": left}
            if stopped:
                q.put((INFO, "Stopped: %d of %d item(s) done." % (ok, len(items))))
            elif not problems and len(items) > 1:
                q.put((SUCCESS, "Pasted %d item(s) in %.1f s." % (ok, time.time() - started)))
            failed = stopped or problems > 0
        except Exception as e:
            q.put((CRITICAL, "Unexpected paste error: %r" % e))
        finally:
            q.put(("_done", failed))

    def start_delete(self, path, name):
        return self.launch("delete", self.delete_worker, (path, name),
                           "Deleting folder '%s' ... (type cancel to stop)" % name)

    def delete_worker(self, job, path, name):
        q, started = job["queue"], time.time()
        prog = {"bytes": 0, "files": 0, "last": time.time()}
        removed, failed = 0, True
        try:
            for dirpath, dirnames, filenames in os.walk(path, topdown=False):
                for fn in filenames:
                    if job["cancel"].is_set():
                        q.put((INFO, "Stopped: %d file(s) were already deleted." % removed))
                        return
                    os.remove(os.path.join(dirpath, fn))
                    removed += 1
                    self.progress(job, prog, 0, 1)
                for dn in dirnames:
                    p = os.path.join(dirpath, dn)
                    if os.path.islink(p):
                        os.unlink(p)
                    else:
                        os.rmdir(p)
            os.rmdir(path)
            failed = False
            q.put((SUCCESS, "Folder '%s' deleted (%d file(s) in %.1f s)." % (name, removed, time.time() - started)))
        except OSError as e:
            q.put((ERROR, "'%s': %s" % (name, e.strerror or e)))
        except Exception as e:
            q.put((CRITICAL, "Unexpected delete error: %r" % e))
        finally:
            q.put(("_done", failed))

    # ---------- download (runs in a background thread) ----------
    def url_allowed(self, url):
        return url.startswith("https://") or (self.allow_insecure and url.startswith("http://"))

    def name_from_url(self, url):
        path = urllib.parse.urlparse(url).path.rstrip("/")
        name = urllib.parse.unquote(os.path.basename(path))
        name = name.replace("/", "").replace("\\", "").replace("\x00", "").strip()
        return name if name not in ("", ".", "..") else "download"

    def cmd_get_file(self, args):
        url = args[0]
        if not self.url_allowed(url):
            return [(ERROR, "Only https:// links are allowed.")]
        name = args[1] if len(args) > 1 else self.name_from_url(url)
        path, err = self.resolve(name, protect=False)            # downloading creates a file: free
        if err:
            return [err]
        if os.path.lexists(path):
            suggested = self.unique_name(name, False, self.cwd)
            return self.ask({"type": "download", "url": url, "name": suggested, "dir": self.cwd},
                            "File '%s' already exists. Download it as '%s' instead? (y/n)" % (name, suggested))
        return self.start_download(url, name, self.cwd)

    def start_job(self, label, worker, *args):
        """Run worker(job, *args) in a background thread; the screen collects its messages through poll()."""
        job = {"queue": queue.Queue(), "cancel": threading.Event(), "debug": self.debug, "label": label}
        self.job = job
        thread = threading.Thread(target=worker, args=(job,) + args, daemon=True)
        job["thread"] = thread
        thread.start()

    def generic_worker(self, job, fn, args):
        """Runs fn(job, *args) -> [(kind, text), ...] in a thread and hands the result over."""
        q, failed = job["queue"], True
        try:
            res = fn(job, *args)
            for item in res:
                q.put(item)
            failed = any(kind in ERROR_KINDS for kind, _ in res)
        except OSError as e:
            q.put((ERROR, str(e.strerror or e)))
        except Exception as e:
            q.put((CRITICAL, "Unexpected error: %r" % e))
        finally:
            q.put(("_done", failed))

    def launch(self, label, worker, args, start_msg=None):
        """Start a background job. If it is finished within a moment the answer comes right here,
        as if nothing ran in the background; otherwise the screen collects it later."""
        self.start_job(label, worker, *args)
        thread = self.job["thread"]
        thread.join(FAST_WAIT)
        if not thread.is_alive():
            return self.poll()
        return [(INFO, start_msg)] if start_msg else []

    def run_bg(self, label, fn, *args):
        """Long reads (search, folder size): in the background, or right now inside a !chk condition."""
        if self.sync_jobs:
            return fn({"cancel": threading.Event(), "debug": self.debug}, *args)
        return self.launch(label, self.generic_worker, (fn, args))

    def start_download(self, url, name, folder):
        if self.dry:
            return [(INFO, "Would download '%s' from %s into %s." % (name, url, folder))]
        self.start_job("download", self.download_worker, url, name, folder)
        return [(INFO, "Downloading '%s' ... (type cancel to stop)" % name)]

    def cmd_cancel(self, args):
        return [(ERROR, "Nothing is running.")]

    def download_worker(self, job, url, name, folder):
        """Runs in its own thread: it only talks to the rest of the program through job["queue"]."""
        q, started = job["queue"], time.time()
        final = os.path.join(folder, name)
        part = "%s.%06x.part" % (final, random.randrange(16 ** 6))   # written here first, renamed when complete
        failed, total = True, 0

        def say(kind, msg):
            q.put((kind, msg))

        def dbg(msg):
            if job["debug"]:
                q.put((DEBUG, msg))

        try:
            ctx = None
            try:
                import certifi
                import ssl
                ctx = ssl.create_default_context(cafile=certifi.where())
            except Exception:
                ctx = None                       # fall back to the system certificates
            req = urllib.request.Request(url, headers={"User-Agent": "Kiwi/1.0"})
            with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT, context=ctx) as resp:
                if not self.url_allowed(resp.geturl()):
                    say(ERROR, "Redirected to a link that is not allowed: " + resp.geturl())
                    return
                length = resp.headers.get("Content-Length")
                length = int(length) if length and length.isdigit() else None
                dbg("HTTP %s, %s, from %s" % (getattr(resp, "status", "?"),
                                              human_size(length) if length is not None else "unknown size",
                                              resp.geturl()))
                if length is not None and length > MAX_DOWNLOAD_BYTES:
                    say(ERROR, "The file is too big (%s, the limit is %s)." % (
                        human_size(length), human_size(MAX_DOWNLOAD_BYTES)))
                    return
                last = time.time()
                read = getattr(resp, "read1", None) or resp.read   # read1: returns as soon as some data is there
                with open(part, "wb") as f:
                    while True:
                        if job["cancel"].is_set():
                            return
                        chunk = read(65536)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > MAX_DOWNLOAD_BYTES:
                            say(ERROR, "The download is bigger than the limit (%s). Stopped." % human_size(MAX_DOWNLOAD_BYTES))
                            return
                        f.write(chunk)
                        if time.time() - last >= 1.0:
                            last = time.time()
                            dbg("downloaded %s%s" % (human_size(total),
                                                     " of " + human_size(length) if length is not None else ""))
            if length is not None and total != length:
                say(ERROR, "The connection broke: got %s of %s." % (human_size(total), human_size(length)))
                return
            target = final
            if os.path.lexists(target):          # the name got taken while we were downloading
                target = os.path.join(folder, self.unique_name(name, False, folder))
            os.rename(part, target)
            failed = False
            say(SUCCESS, "Downloaded '%s' (%s in %.1f s)." % (os.path.basename(target), human_size(total),
                                                             time.time() - started))
        except urllib.error.HTTPError as e:
            say(ERROR, "The server answered %s %s." % (e.code, e.reason))
        except urllib.error.URLError as e:
            text = str(e.reason)
            hint = " (on Android the APK needs the 'certifi' package)" if "CERTIFICATE" in text.upper() else ""
            say(ERROR, "Cannot download: %s%s" % (text, hint))
        except (socket.timeout, TimeoutError):
            say(ERROR, "Timed out: no data for %d seconds." % DOWNLOAD_TIMEOUT)
        except ValueError as e:
            say(ERROR, "Bad link: %s" % e)
        except OSError as e:
            say(ERROR, str(e.strerror or e))
        except Exception as e:                   # nothing should kill the thread silently
            say(CRITICAL, "Unexpected download error: %r" % e)
        finally:
            if failed:
                try:
                    os.remove(part)              # never leave a half-downloaded file behind
                except OSError:
                    pass
            q.put(("_done", failed))

    # ---------- zip / unzip (background jobs) ----------
    def cmd_zip_file(self, args):
        return self.zip_command(args[0], args[1] if len(args) > 1 else None, False)

    def cmd_zip_dir(self, args):
        return self.zip_command(args[0], args[1] if len(args) > 1 else None, True)

    def zip_command(self, name, out_name, is_dir):
        src, err = self.resolve(name, protect=False)
        if err:
            return [err]
        # the protection: the source must not be inside Android/, DCIM/ ... or outside /sdcard
        # (the folder Download itself may be zipped: zipping changes nothing)
        ok, reason = self.check_path(src, deep_only=True)
        if not ok:
            return [(ERROR, reason)]
        label = "Folder" if is_dir else "File"
        if not os.path.lexists(src):
            return [(ERROR, "%s '%s' not found." % (label, name))]
        src_is_dir = os.path.isdir(src) and not os.path.islink(src)
        if is_dir and not src_is_dir:
            return [(ERROR, "'%s' is a file. Use +zf for files." % name)]
        if not is_dir and src_is_dir:
            return [(ERROR, "'%s' is a folder. Use +zd for folders." % name)]
        if out_name is None:
            out_name = (name if is_dir else os.path.splitext(name)[0]) + ".zip"
        elif not out_name.lower().endswith(".zip"):
            out_name += ".zip"
        out_path, err = self.resolve(out_name, protect=False)         # the archive is a new file: free
        if err:
            return [err]
        if os.path.lexists(out_path):
            suggested = self.unique_name(out_name, False, self.cwd)
            return self.ask({"type": "zip", "name": name, "out": suggested, "is_dir": is_dir, "dir": self.cwd},
                            "File '%s' already exists. Zip it as '%s' instead? (y/n)" % (out_name, suggested))
        return self.start_zip(name, out_name, is_dir, self.cwd)

    def start_zip(self, name, out_name, is_dir, folder):
        ok, reason = self.check_path(os.path.join(folder, name), deep_only=True)
        if not ok:
            return [(ERROR, reason)]
        if self.dry:
            return [(INFO, "Would zip '%s' into '%s'." % (name, out_name))]
        self.start_job("zip", self.zip_worker, os.path.join(folder, name), os.path.join(folder, out_name), is_dir)
        return [(INFO, "Zipping '%s' ... (type cancel to stop)" % name)]

    def zip_worker(self, job, src, archive, is_dir):
        q, started = job["queue"], time.time()
        part = "%s.%06x.part" % (archive, random.randrange(16 ** 6))   # written here first, renamed when complete
        failed = True

        def say(kind, msg):
            q.put((kind, msg))

        def dbg(msg):
            if job["debug"]:
                q.put((DEBUG, msg))

        try:
            entries, total = [], 0
            if is_dir:
                top = os.path.basename(src)
                for dirpath, dirnames, filenames in os.walk(src):
                    dirnames.sort()
                    rel = os.path.relpath(dirpath, src)
                    arcdir = top if rel == "." else top + "/" + rel.replace(os.sep, "/")
                    if not dirnames and not filenames:
                        entries.append((dirpath, arcdir + "/"))         # keep empty folders
                    for fn in sorted(filenames):
                        path = os.path.join(dirpath, fn)
                        if os.path.islink(path) or not os.path.isfile(path):
                            continue
                        total += os.path.getsize(path)
                        if total > MAX_ZIP_SOURCE_BYTES:
                            say(ERROR, "Too big to zip (the limit is %s)." % human_size(MAX_ZIP_SOURCE_BYTES))
                            return
                        entries.append((path, arcdir + "/" + fn))
                    if job["cancel"].is_set():
                        say(INFO, "Cancelled.")
                        return
            else:
                total = os.path.getsize(src)
                if total > MAX_ZIP_SOURCE_BYTES:
                    say(ERROR, "Too big to zip (the limit is %s)." % human_size(MAX_ZIP_SOURCE_BYTES))
                    return
                entries.append((src, os.path.basename(src)))
            n_files = sum(1 for _, arc in entries if not arc.endswith("/"))
            dbg("%d file(s), %s to pack" % (n_files, human_size(total)))
            with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED) as zf:
                last = time.time()
                for i, (path, arc) in enumerate(entries, 1):
                    if job["cancel"].is_set():
                        say(INFO, "Cancelled.")
                        return
                    zf.write(path, arc)
                    if time.time() - last >= 1.0:
                        last = time.time()
                        dbg("packed %d of %d" % (i, len(entries)))
            size = os.path.getsize(part)
            target = archive
            if os.path.lexists(target):          # the name got taken while we were working
                target = os.path.join(os.path.dirname(archive), self.unique_name(
                    os.path.basename(archive), False, os.path.dirname(archive)))
            os.rename(part, target)
            failed = False
            say(SUCCESS, "Zipped to '%s' (%d file(s), %s -> %s in %.1f s)." % (
                os.path.basename(target), n_files, human_size(total), human_size(size), time.time() - started))
        except OSError as e:
            say(ERROR, str(e.strerror or e))
        except Exception as e:
            say(CRITICAL, "Unexpected zip error: %r" % e)
        finally:
            if failed:
                try:
                    os.remove(part)              # never leave a half-written archive behind
                except OSError:
                    pass
            q.put(("_done", failed))

    def cmd_unzip_here(self, args):
        return self.unzip_command(" ".join(args), "files")

    def cmd_unzip_dir(self, args):
        return self.unzip_command(" ".join(args), "dir")

    def plan_unzip(self, archive, mode, folder):
        """Check the archive and decide where everything goes. Returns a dict, or an (kind, text) error."""
        try:
            zf = zipfile.ZipFile(archive)
        except zipfile.BadZipFile:
            return (ERROR, "'%s' is not a valid zip file." % os.path.basename(archive))
        with zf:
            infos = zf.infolist()
            if len(infos) > MAX_UNZIP_FILES:
                return (ERROR, "The archive has too many entries (%d, the limit is %d)." % (len(infos), MAX_UNZIP_FILES))
            if sum(i.file_size for i in infos) > MAX_UNZIP_BYTES:
                return (ERROR, "The archive unpacks to more than the limit (%s)." % human_size(MAX_UNZIP_BYTES))
            plan = []
            for info in infos:
                if info.flag_bits & 0x1:
                    return (ERROR, "Encrypted archives are not supported.")
                name = info.filename.replace("\\", "/")
                parts = [p for p in name.split("/") if p not in ("", ".")]
                if (name.startswith("/") or ".." in parts or "\x00" in name
                        or (parts and len(parts[0]) == 2 and parts[0][1] == ":")):
                    return (ERROR, "The archive contains an unsafe path ('%s'). Nothing was extracted." % info.filename)
                if parts:
                    plan.append((info.filename, parts, name.endswith("/")))
        if not plan:
            return (INFO, "The archive is empty.")
        if mode == "dir":
            tops = {parts[0] for _, parts, _ in plan}
            single_top = len(tops) == 1 and any(len(parts) > 1 or is_dir for _, parts, is_dir in plan)
            if single_top:
                name = plan[0][1][0]
                plan = [(n, parts[1:], d) for n, parts, d in plan if len(parts) > 1]
            else:
                name = os.path.splitext(os.path.basename(archive))[0] or "unzipped"
            if os.path.lexists(os.path.join(folder, name)):
                name = self.unique_name(name, True, folder)
            dest = os.path.join(folder, name)
        else:
            dest = folder
        collisions = []
        for _, parts, is_dir in plan:
            for k in range(1, len(parts)):
                if os.path.isfile(os.path.join(dest, *parts[:k])):
                    return (ERROR, "'%s' is a file, but the archive needs a folder there." % "/".join(parts[:k]))
            target = os.path.join(dest, *parts)
            if not is_dir and os.path.lexists(target):
                if os.path.isdir(target):
                    return (ERROR, "'%s' is a folder, but the archive has a file with that name." % "/".join(parts))
                collisions.append("/".join(parts))
        return {"plan": plan, "dest": dest, "collisions": collisions}

    def unzip_command(self, raw, mode):
        raw = raw.strip()
        archive = self.where(raw)
        if not os.path.lexists(archive):
            return [(ERROR, "Archive '%s' not found." % raw)]
        if os.path.isdir(archive):
            return [(ERROR, "'%s' is a folder, not a zip file." % raw)]
        res = self.plan_unzip(archive, mode, self.cwd)
        if isinstance(res, tuple):
            return [res]
        if res["collisions"]:
            shown = ", ".join(res["collisions"][:5]) + (" ..." if len(res["collisions"]) > 5 else "")
            return self.ask({"type": "unzip", "archive": archive, "plan": res["plan"], "dest": res["dest"],
                             "dir": self.cwd},
                            "%d file(s) already exist here: %s. Extract them with new names? (y/n)"
                            % (len(res["collisions"]), shown))
        return self.start_unzip(archive, res["dest"], res["plan"], False)

    def start_unzip(self, archive, dest, plan, rename):
        if self.dry:
            files = sum(1 for _, _, is_dir in plan if not is_dir)
            return [(INFO, "Would unzip '%s' (%d file(s)) into %s." % (os.path.basename(archive), files, dest))]
        self.start_job("unzip", self.unzip_worker, archive, dest, plan, rename)
        return [(INFO, "Unzipping '%s' ... (type cancel to stop)" % os.path.basename(archive))]

    def unzip_worker(self, job, archive, dest, plan, rename):
        q, started = job["queue"], time.time()
        failed = True
        created_files, created_dirs = [], []

        def say(kind, msg):
            q.put((kind, msg))

        def dbg(msg):
            if job["debug"]:
                q.put((DEBUG, msg))

        def make_dirs(path):
            missing = []
            while path and not os.path.isdir(path) and path != os.path.dirname(path):
                missing.append(path)
                path = os.path.dirname(path)
            for d in reversed(missing):
                os.mkdir(d)
                created_dirs.append(d)

        try:
            real_dest = os.path.realpath(dest)
            make_dirs(dest)
            written, n_files, last = 0, 0, time.time()
            with zipfile.ZipFile(archive) as zf:
                for member, parts, is_dir in plan:
                    if job["cancel"].is_set():
                        say(INFO, "Cancelled.")
                        return
                    target = os.path.join(dest, *parts)
                    real_target = os.path.realpath(target)
                    if real_target != real_dest and not real_target.startswith(real_dest + os.sep):
                        say(ERROR, "Unsafe path in the archive: %s. Stopped." % member)
                        return
                    if is_dir:
                        make_dirs(target)
                        continue
                    make_dirs(os.path.dirname(target))
                    if os.path.lexists(target):                       # never overwrite
                        folder = os.path.dirname(target)
                        target = os.path.join(folder, self.unique_name(os.path.basename(target), False, folder))
                    with zf.open(member) as src_f, open(target, "wb") as out_f:
                        created_files.append(target)
                        while True:
                            chunk = src_f.read(65536)
                            if not chunk:
                                break
                            written += len(chunk)
                            if written > MAX_UNZIP_BYTES:
                                say(ERROR, "The archive unpacks to more than the limit. Stopped.")
                                return
                            out_f.write(chunk)
                            if job["cancel"].is_set():
                                say(INFO, "Cancelled.")
                                return
                    n_files += 1
                    if time.time() - last >= 1.0:
                        last = time.time()
                        dbg("unpacked %d file(s), %s" % (n_files, human_size(written)))
            failed = False
            where = "'%s'" % os.path.basename(dest) if created_dirs and created_dirs[0] == dest else "the current folder"
            say(SUCCESS, "Unzipped '%s' into %s (%d file(s), %s in %.1f s)." % (
                os.path.basename(archive), where, n_files, human_size(written), time.time() - started))
        except zipfile.BadZipFile:
            say(ERROR, "'%s' is not a valid zip file." % os.path.basename(archive))
        except RuntimeError as e:                # e.g. a password-protected entry
            say(ERROR, "Cannot unpack: %s" % e)
        except OSError as e:
            say(ERROR, str(e.strerror or e))
        except Exception as e:
            say(CRITICAL, "Unexpected unzip error: %r" % e)
        finally:
            if failed:                           # an unpack that did not finish leaves nothing behind
                for f in created_files:
                    try:
                        os.remove(f)
                    except OSError:
                        pass
                for d in reversed(created_dirs):
                    try:
                        os.rmdir(d)
                    except OSError:
                        pass
            q.put(("_done", failed))

    # ---------- python scripts (run inside the app) ----------
    def cmd_py(self, args):
        raw, extra = args[0], args[1:]
        path = self.where(raw)
        if not os.path.lexists(path):
            return [(ERROR, "Script '%s' not found." % raw)]
        if os.path.isdir(path):
            return [(ERROR, "'%s' is a folder, not a script." % raw)]
        if not path.lower().endswith(".py"):
            return [(ERROR, "Only .py files can be run.")]
        name = os.path.basename(path)
        entry = self.load_trust().get(os.path.realpath(path))
        if entry:
            digest = self.script_hash(path)
            if digest and entry.get("sha256") == digest:           # trusted and not changed: no question
                if self.dry:
                    return [(INFO, "Would run '%s' without asking (trusted script)." % name)]
                return self.start_py(path, extra, self.cwd, digest)
            return self.ask({"type": "pyrun", "path": path, "args": extra, "dir": self.cwd},
                            "'%s' was trusted, but its content has changed since. Run it? "
                            "A script ignores the protection. (y/n)" % name)
        return self.ask({"type": "pyrun", "path": path, "args": extra, "dir": self.cwd},
                        "Run '%s'? A script ignores the protection: it can change or delete anything this app can. (y/n)"
                        % name)

    def start_py(self, path, args, folder, expected=None):
        if self.py_thread is not None and self.py_thread.is_alive():
            return [(ERROR, "The previous script is still running (it has not stopped yet).")]
        return self.launch("python", self.py_worker, (path, args, folder, expected),
                           "Running '%s' ... (type cancel to stop)" % os.path.basename(path))

    # ---------- trusted scripts: they run without the question while their content is exactly the same ----------
    def script_hash(self, path):
        try:
            if os.path.getsize(path) > MAX_TRUST_BYTES:
                return None
            with open(path, "rb") as f:
                return hashlib.sha256(f.read()).hexdigest()
        except OSError:
            return None

    def trust_path(self):
        return os.path.join(self.state_dir, TRUST_FILE)

    def load_trust(self):
        """{real path: {"sha256": ..., "added": ...}}. A missing or damaged file means: nothing is trusted."""
        try:
            with open(self.trust_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {k: v for k, v in data.items() if isinstance(v, dict)}

    def save_trust(self, data):
        """Returns None, or a text saying why it could not be saved."""
        tmp = self.trust_path() + ".tmp"
        try:
            os.makedirs(self.state_dir, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=1, sort_keys=True)
            os.replace(tmp, self.trust_path())
        except OSError as e:
            return e.strerror or str(e)
        return None

    def tools_hint(self):
        """One line at startup when the last "tul updates" found newer tool versions. It reads a small file: no network here."""
        try:
            with open(os.path.join(self.project, "tools_updates.json"), "r", encoding="utf-8") as f:
                data = json.load(f)
            available, checked = data.get("available"), data.get("checked")
            if not isinstance(available, dict) or not available or not isinstance(checked, (int, float)):
                return []
            if time.time() - checked > 30 * 86400:                  # an old check says nothing about today
                return []
            parts = ["%s %s -> %s" % (n, str(a.get("from", "?"))[:12], str(a.get("to", "?"))[:12])
                     for n, a in sorted(available.items()) if isinstance(a, dict)]
        except (OSError, ValueError, AttributeError):
            return []
        if not parts:
            return []
        more = ", ..." if len(parts) > 4 else ""
        return [(INFO, "Tool updates are available: %s%s. Run: tul update --all" % (", ".join(parts[:4]), more))]

    def cmd_tul(self, args):
        """tul install network  =  py tul.py install network  (tul.py lies in the project folder)."""
        script = os.path.join(self.project, "tul.py")
        if not os.path.isfile(script):
            return [(ERROR, "tul.py is not in the project folder (%s)." % self.project)]
        return self.cmd_py([script] + list(args))

    def cmd_pip(self, args):
        """pip install rich  =  py hpip.py install rich  (hpip.py lies in the project folder)."""
        script = os.path.join(self.project, "hpip.py")
        if not os.path.isfile(script):
            return [(ERROR, "hpip.py is not in the project folder (%s)." % self.project)]
        return self.cmd_py([script] + list(args))

    # ---------- wakelock and battery ----------
    def cmd_wake_on(self, args):
        minutes = WAKE_DEFAULT_MINUTES
        if args:
            if not args[0].isdigit() or not 1 <= int(args[0]) <= WAKE_MAX_MINUTES:
                return [(ERROR, "Minutes: 1 to %d. Example: +wl 30" % WAKE_MAX_MINUTES)]
            minutes = int(args[0])
        if self.dry:
            return [(INFO, "Would keep the processor awake for %d min." % minutes)]
        try:
            self.power.acquire(minutes)
        except Exception as e:
            return [(ERROR, "Cannot hold the wakelock: %s" % e)]
        self.wake_until = time.time() + minutes * 60
        return [(SUCCESS, "The processor stays awake for %d min (the screen can still turn off). Release it: -wl" % minutes)]

    def cmd_wake_off(self, args):
        if self.dry:
            return [(INFO, "Would release the wakelock.")]
        try:
            held = self.power.held()
            self.power.release()
        except Exception as e:
            return [(ERROR, "Cannot release the wakelock: %s" % e)]
        self.wake_until = 0.0
        return [(INFO, "The wakelock is released." if held else "The wakelock was not held.")]

    def cmd_wake_status(self, args):
        try:
            held = self.power.held()
        except Exception:
            held = False
        if held and self.wake_until > time.time():
            left = int(self.wake_until - time.time())
            lines = ["wakelock:       held, %d min %02d s left" % (left // 60, left % 60)]
        else:
            lines = ["wakelock:       off"]
        try:
            if self.power.battery_ignored():
                lines.append("battery limits: none (Android will not stop Hegd to save power)")
            else:
                lines.append("battery limits: ON (Android may stop Hegd in the background; run: bat)")
        except Exception as e:
            lines.append("battery limits: unknown (%s)" % e)
        return [(TEXT, "\n".join(lines))]

    def cmd_battery(self, args):
        if self.dry:
            return [(INFO, "Would ask Android to lift the battery limits for Hegd.")]
        try:
            self.power.open_battery_settings()
        except Exception as e:
            return [(ERROR, "Cannot open the battery setting: %s" % e)]
        return [(INFO, "Android asks whether Hegd may run without battery limits. Choose Allow.")]

    def cmd_trust(self, args):
        path = self.where(args[0])
        if not os.path.lexists(path):
            return [(ERROR, "Script '%s' not found." % args[0])]
        if not os.path.isfile(path) or not path.lower().endswith(".py"):
            return [(ERROR, "Only a .py file can be trusted.")]
        ok, reason = self.check_path(path)
        if not ok:
            return [(ERROR, reason)]
        digest = self.script_hash(path)
        if digest is None:
            return [(ERROR, "Cannot read '%s' or it is bigger than %d MB." % (args[0], MAX_TRUST_BYTES // 1048576))]
        entry = self.load_trust().get(path)
        if entry and entry.get("sha256") == digest:
            return [(INFO, "'%s' is already trusted." % os.path.basename(path))]
        return self.ask({"type": "trust", "path": path, "hash": digest, "dir": self.cwd},
                        "Trust '%s'? From now on it runs without asking, until its content changes. (y/n)"
                        % os.path.basename(path))

    def cmd_untrust(self, args):
        path = self.where(args[0])
        data = self.load_trust()
        if path not in data:
            return [(ERROR, "'%s' is not in the trusted list." % args[0])]
        if self.dry:
            return [(INFO, "Would stop trusting '%s'." % os.path.basename(path))]
        del data[path]
        err = self.save_trust(data)
        if err:
            return [(ERROR, "Cannot save the trusted list: " + err)]
        return [(INFO, "'%s' is not trusted any more." % os.path.basename(path))]

    def cmd_trust_list(self, args):
        data = self.load_trust()
        if not data:
            return [(INFO, "No trusted scripts.")]
        lines = []
        for path in sorted(data):
            if not os.path.isfile(path):
                state = "missing"
            elif self.script_hash(path) == data[path].get("sha256"):
                state = "ok"
            else:
                state = "CHANGED"
            lines.append("%-8s %s" % (state, path))
        return [(TEXT, "\n".join(lines))]

    def interrupt(self, thread):
        """Best effort: raise Cancelled inside the script's thread. A script that sleeps or waits inside a
        long C call only notices it afterwards, and a bare 'except:' in the script can swallow it."""
        try:
            import ctypes
            ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(thread.ident), ctypes.py_object(Cancelled))
        except Exception:
            pass

    def py_worker(self, job, path, args, folder, expected=None):
        q, started, failed = job["queue"], time.time(), True
        self.py_thread = threading.current_thread()
        if expected and self.script_hash(path) != expected:        # swapped between the check and the start
            q.put((ERROR, "The script changed just before it started, so it was not run. Run it again."))
            self.py_thread = None
            q.put(("_done", True))
            return
        budget = {"left": MAX_PY_LINES}
        out, err = LineSink(q, TEXT, budget), LineSink(q, WARNING, budget)
        saved = (sys.stdout, sys.stderr, sys.stdin, sys.argv, list(sys.path), os.getcwd())
        try:
            os.chdir(folder)                       # the script starts in the folder you were in
            sys.stdout, sys.stderr, sys.stdin = out, err, io.StringIO("")      # input() ends at once instead of hanging
            sys.argv = [path] + list(args)
            sys.path.insert(0, os.path.dirname(path))                          # "import neighbour" works
            site = os.path.join(self.project, "site-packages")                 # libraries put there by hpip
            if os.path.isdir(site) and site not in sys.path:
                sys.path.insert(1, site)
            runpy.run_path(path, run_name="__main__")
            failed = False
        except SystemExit as e:
            if e.code in (None, 0):
                failed = False
            else:
                q.put((ERROR, "The script exited with code %s." % (e.code,)))
        except Cancelled:
            q.put((INFO, "Stopped."))
        except BaseException:
            etype, evalue, tb = sys.exc_info()                  # show the script's own lines, not ours
            frames = [f for f in traceback.extract_tb(tb) if f.filename != __file__ and "runpy" not in f.filename]
            text = "".join(traceback.format_list(frames) + traceback.format_exception_only(etype, evalue))
            head = "Traceback (most recent call last):\n" if frames else ""
            q.put((ERROR, "\n".join((head + text).rstrip().split("\n")[-10:])))
        finally:
            out.flush()
            err.flush()
            sys.stdout, sys.stderr, sys.stdin, sys.argv = saved[:4]
            sys.path[:] = saved[4]
            try:
                os.chdir(saved[5])
            except OSError:
                pass
            self.py_thread = None
            if not failed:
                q.put((SUCCESS, "'%s' finished in %.1f s." % (os.path.basename(path), time.time() - started)))
            q.put(("_done", failed))

    # ---------- answering a y/n question ----------
    def answer(self, line):
        ans = line.strip().lower()
        action = self.pending
        if ans in ("n", "no"):
            self.pending = None
            self.cancelled = True
            return [(INFO, "Cancelled.")]
        if ans not in ("y", "yes"):
            return [(WARNING, "Please answer y or n.")]
        self.pending = None
        folder = action["dir"]
        if action["type"] == "create":
            name = action["suggested"]
            if os.path.lexists(os.path.join(folder, name)):          # taken in the meantime
                name = self.unique_name(name, action["is_dir"], folder)
            return self.create(name, action["is_dir"], folder)
        if action["type"] == "rename":
            name = action["suggested"]
            if os.path.lexists(os.path.join(folder, name)):
                name = self.unique_name(name, action["is_dir"], folder)
            return self.do_rename(action["old"], name, action["is_dir"], folder)
        if action["type"] == "delete_dir":
            path, err = self.resolve(action["name"], folder)
            if err:
                return [err]
            if not os.path.isdir(path):
                return [(ERROR, "Folder '%s' not found." % action["name"])]
            return self.start_delete(path, action["name"])
        if action["type"] == "paste":
            return self.start_paste(folder)
        if action["type"] == "zip":
            out = action["out"]
            if os.path.lexists(os.path.join(folder, out)):                # taken in the meantime
                out = self.unique_name(out, False, folder)
            return self.start_zip(action["name"], out, action["is_dir"], folder)
        if action["type"] == "unzip":
            return self.start_unzip(action["archive"], action["dest"], action["plan"], True)
        if action["type"] == "trust":
            if self.script_hash(action["path"]) != action["hash"]:
                return [(ERROR, "The script changed while you were answering: nothing was trusted.")]
            data = self.load_trust()
            data[action["path"]] = {"sha256": action["hash"], "added": time.strftime("%Y-%m-%d %H:%M")}
            err = self.save_trust(data)
            if err:
                return [(ERROR, "Cannot save the trusted list: " + err)]
            return [(SUCCESS, "'%s' is trusted now. Change it and the question comes back." % os.path.basename(action["path"]))]
        if action["type"] == "pyrun":
            if not os.path.isfile(action["path"]):
                return [(ERROR, "The script is not there any more.")]
            return self.start_py(action["path"], action["args"], folder)
        if action["type"] == "download":
            name = action["name"]
            if os.path.lexists(os.path.join(folder, name)):          # taken in the meantime
                name = self.unique_name(name, False, folder)
            return self.start_download(action["url"], name, folder)
        if action["type"] == "overwrite":
            path, err = self.existing_text_file(action["name"], folder)
            if err:
                return [err]
            return self.write_text(path, action["name"], action["text"], "overwritten")
        return []
