"""Kiwi core: parses and runs commands. No UI code here, so it can be tested anywhere.

Create (free, no protection):
  new file name     create a file             new dir name      create a folder
  download link [name]  download a file in the background (https only); "cancel" stops it
  zip file name [zip]   zip a file            zip dir name [zip]  zip a folder
                    (the source is protected like cut: nothing from Android/, DCIM/ ... or outside /sdcard)
  unzip archive     unzip into ONE new folder named like the archive (a single top folder inside is used as is)
  unzip archive to FOLDER   unzip straight into FOLDER (to here = this folder; existing names get three digits, after a y/n)
Delete / rename / edit (always pass the protection: root zone, protected folders like Android/,
  and the folder Download itself; files inside Download are free):
  del file name     delete a file             del dir name      delete a folder
  rename file old new   rename a file         rename dir old new  rename a folder
  append name "text"    append a line to a text file
  overwrite name "text"     overwrite a text file (creates it if missing)
  overwrite name "old" replace to "new"   change every "old" inside a text file into "new"
  edit name "old" "new" [all] [-e]    change ONE exact piece of text (refuses when it is not unique; all = every place)
Buffer (working names, easy to rename in the command table):
  copy name ...     put files/folders into the buffer (nothing changes on disk)
  cut name ...      same, but they MOVE when you paste (protected like del file; nothing is removed until paste)
  paste             put the buffer into the current folder; names already taken get three digits
  buffer            show what is in the buffer
Read / walk / print (free):
  list [path]       list everything in a folder ("." or no path = current folder)
  view path         show the contents of a text file
  view path h N     first N lines (default 20)    view path t N   last N lines
  view path A B     lines A to B, numbered (numbers match grep)
  go to [path]      go to a folder (anywhere; no path = back to butter)
  go up             go up one folder (anywhere)
  echo text         print text
  py script.py [args]  run a Python script inside the app (asks every time; the script ignores the protection)
  clear             clear the screen
  here              show the current folder
  history [N]       the last N commands (default 15)
  redo [N]          run a past command again: no N = the last one, N = the number shown by history
  help [command]    list of commands (or one command)
  grep "text" [path]    find text inside files (folder = recursive); case-insensitive
  info file path    info about a file         info dir [path]   info about a folder
  find file pattern [path]  find files by name    find dir pattern [path]  find folders by name
                    (pattern with * or ? = wildcard match; plain word = name contains it)
Tools (installed with tul, no "py" needed):
  git ...           GitHub from the phone (gitkit.py)     net ...    ip, dns, port, ping, trace (net.py)
  sv [folder] [min] share a folder in a browser (sv.py)   asm prog.s run ARM64 assembly (asm.py)
Trust and power:
  trust add file | trust remove file | trust list      scripts that may run without asking
  wakelock on [min] | wakelock off | wakelock status    keep the processor awake
  battery           ask Android to lift battery limits

One name for each action (the old short names like +nf or ?afid are gone).

Text with spaces or | goes in quotes: +af notes.txt "a | b". A quote opens only at the start of a word.

Chains:  cmd1 | cmd2 | cmd3
  Without !ma an ERROR stops everything after it. A y/n question pauses the chain
  until you answer; "n" counts as a stop. "!ma" works on its command and all after it:
  errors (and "n") no longer stop the chain.

Modifiers (at the start of a command; they can be stacked, e.g.  !hl !dry -dd folder):
  !ma cmd           this command and everything after it goes on after errors
  !hl cmd           hide the logs of cmd (questions and problems still show)
  !dry cmd          show what cmd would do, change nothing (reads and echo run as usual)
  !prl cmd          run cmd in the background and go on with the chain at once (parallel jobs, max 4;
                    only download zip unzip (also unzip to) paste grep find info dir;
                    not for commands that ask a question; their messages carry a [prl N] label; cancel stops all)
  !y cmd            answer "yes" to the question cmd asks (not for deleting a non-empty folder,
                    overwriting a file that has content, or running a script: those still ask)
  !chk A [| B] then C   run C only if A (and B) worked; with else: only if they did NOT work
                    (the first plain word "then" or "else" ends the condition; the check itself is silent;
                    a search grep / find that finds nothing counts as "not passed")
  macro NAME = A | B    save commands as a macro; $1..$9 and $* are the arguments
  macro NAME            type the commands one per line, "end" saves, "cancel" throws it away
  macro list | macro show NAME | macro del NAME      run NAME [arguments]    run it (like typing the commands)
  CMD to FOLDER     do it in another folder (new file, new dir, download, paste, copy, cut, unzip, zip):
                    new file a.txt to Pictures, or to here for this folder.  It is like go to FOLDER, the command, and back, but names the
                    command READS (zip, unzip, copy, cut) still come from the folder you are in

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
import re
import builtins
import platform
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
VERSION = "2026-11-09.3"                   # printed at startup: tells you which file version is really running
# Where people can reach the author: shown at start and by "report". Put your names in; an empty one is hidden.
CONTACTS = (("Telegram", "@ResolixVozmozen"), ("Discord", "resolikforever"))
ISSUES_URL = "https://github.com/resolik/Hegd/issues"      # change it when the repository is renamed
MODIFIERS = ("!ma", "!hl", "!dry", "!chk", "!y", "!prl")
YES_NEVER = ("delete_dir", "overwrite", "pyrun", "trust")    # questions !y never answers: they would destroy data or open the door
TRUST_FILE = "trusted_scripts.json"        # path -> sha256 of the scripts you allowed to run without asking
ENV_FILE = "env.json"                      # variables of the env command, in the app's private folder
ENV_MAX_VARS = 100
ENV_MAX_VALUE = 4096
ENV_BLOCKED_PREFIXES = ("PYTHON", "LD_", "ANDROID_", "KIVY_", "SDL_", "P4A_")   # these could break or hijack the app
ENV_BLOCKED_NAMES = ("PATH", "HOME", "TMPDIR", "SHELL", "USER", "LOGNAME", "LANG", "TZ")
WAKE_DEFAULT_MINUTES = 30                  # wakelock on without a number
WAKE_MAX_MINUTES = 480                     # the longest wakelock: 8 hours
MAX_TRUST_BYTES = 5 * 1024 * 1024          # a bigger script cannot be trusted (it would be slow to check)
FAST_WAIT = 0.2                            # a background job that is done within this time answers on the spot
HIDE_KINDS = (INFO, SUCCESS, DEBUG, TEXT)  # what !hl hides; questions and problems always stay visible
JOB_COMMANDS = ("download", "zip file", "zip dir", "unzip", "paste", "py", "pip", "asm", "git", "net", "sv", "run", "tul", "redo")   # not allowed inside a !chk condition
MACRO_FILE = "macros.json"                  # name -> {"lines": [...]}, lies in the project folder
MACRO_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,19}$")
MACRO_RESERVED = ("to", "then", "else", "end", "run", "macro", "here", "list", "show", "del", "cancel")
MACRO_DEF = re.compile(r"^\s*(macro)\s+([^\s=|]+)\s*=\s*(.+?)\s*$")
MACRO_FORBIDDEN = ("macro", "redo")  # not inside a macro
MAX_MACROS = 100
MAX_MACRO_STEPS = 200                       # commands one input may expand to (stops a macro that calls itself)
TO_COMMANDS = ("new file", "new dir", "download", "paste", "copy", "cut", "unzip", "zip file", "zip dir")
PRL_COMMANDS = ("download", "zip file", "zip dir", "unzip", "paste", "grep", "find file", "find dir", "info dir")
MAX_PRL_JOBS = 4                           # parallel jobs at the same time (a phone is not a server)
MAX_PY_LINES = 1000                        # a script's output is cut after this many lines
MAX_HISTORY = 200                          # history remembers this many commands (until the app is closed)

ROOT_DIR = "/storage/emulated/0"           # same as /sdcard
MAX_VIEW_LINES = 500                       # view shows at most this many lines
MAX_TAIL_BYTES = 2000000                   # view t never reads more than this from the end
MAX_RANGE_FILE_BYTES = 50000000            # view A B refuses bigger files
MAX_VIEW_CHARS = 100000                    # ...and reads at most this many characters
MAX_GREP_RESULTS = 200                     # grep stops after this many matching lines
MAX_GREP_FILES = 5000                      # ...or after scanning this many files
MAX_GREP_FILE_BYTES = 5000000              # bigger files are skipped by grep and not line-counted by fileinfo
MAX_FIND_RESULTS = 300                     # findfile / finddir stop after this many results
MAX_WALK = 50000                           # ...or after looking at this many entries (also dirinfo)
MAX_LINE_SHOW = 160                        # long matching lines are cut
DOWNLOAD_TIMEOUT = 20                      # seconds without data before a download gives up
MAX_DOWNLOAD_BYTES = 200000000             # bigger downloads are refused / stopped
MAX_ZIP_SOURCE_BYTES = 2000000000          # zipfile / zipdir refuse to pack more than this
MAX_UNZIP_BYTES = 2000000000               # unzip refuses archives that unpack to more than this
MAX_UNZIP_FILES = 100000                   # ...or with more entries than this
PROJECT_NAME = "butter"                    # project folder -> /sdcard/butter
LEGACY_PROJECT_NAME = "hegd"               # the old folder name; renamed to butter on first start
PROTECTED_NAMES = (                        # the folder AND everything inside is protected
    "Android", "DCIM", "Pictures", "Movies", "Music",
    "Documents", "Alarms", "Notifications", "Podcasts", "Ringtones",
)
FOLDER_ONLY_NAMES = ("Download",)          # only the folder itself is protected, files inside are free
HELP = [
    ("Make", [
        ("new file|dir name", "create a file or a folder"),
        ("download link [name]", "download a file (https)"),
        ("zip file|dir name [zip]", "zip a file or a folder"),
        ("unzip archive", "unzip into a new folder named like the archive"),
        ("unzip archive to FOLDER", "unzip straight into FOLDER (to here = the folder you are in)"),
    ]),
    ("Change", [
        ('append name "text"', "append a line to a file"),
        ('overwrite name "text"', "overwrite a text file"),
        ('overwrite name "a" replace to "b"', "replace text inside a file"),
        ("rename file|dir old new", "rename a file or a folder"),
        ("del file|dir name", "delete a file or a folder"),
    ]),
    ("Buffer", [
        ("copy name ...", "copy to the buffer"),
        ("cut name ...", "cut (moves on paste)"),
        ("paste", "paste here"),
        ("buffer", "show the buffer"),
    ]),
    ("Look", [
        ("list [path]", "list a folder"),
        ("view path", "view a text file"),
        ("view path h 20", "first 20 lines"),
        ("view path t 20", "last 20 lines"),
        ("view path 120 140", "lines 120 to 140"),
        ("info file|dir [path]", "info about a file or a folder"),
        ('grep "text" [path]', "search text in files"),
        ("find file|dir pattern [path]", "find files or folders by name"),
    ]),
    ("Walk", [
        ("go to [path]", "go to a folder (no path = back to butter)"),
        ("go up", "go up one folder"),
        ("here", "where am I (the current folder)"),
    ]),
    ("Modifiers", [
        ("!ma cmd", "keep going after errors"),
        ("!hl cmd", "hide the logs of cmd"),
        ("!dry cmd", "show, do not do"),
        ("!y cmd", "say yes to its question"),
        ("!prl cmd", "run cmd in the background, go on at once (max 4)"),
        ("!chk A then B", "run B if A works"),
        ("!chk A else B", "run B if A fails"),
        ("macro NAME = cmds", "save commands under a name (use | between them)"),
        ("macro NAME", "type the commands one per line, finish with end (cancel throws it away)"),
        ("macro list|show|del", "list your macros / show one / delete one (macro show NAME)"),
        ("run NAME [args]", "run a macro; $1 .. $9 and $* inside it are your arguments"),
        ("to FOLDER", "after a command: do it in that folder (new file a.txt to Pictures; download URL to Music; paste to Docs; copy a.txt to Docs; unzip a.zip to Games)"),
    ]),
    ("Other", [
        ("report", "how to report a problem: contacts and one line of info to copy"),
        ("echo text", "print text"),
        ("py file [args]", "run a Python script"),
        ("env [NAME [value]]", "variables for scripts (tokens): env, env NAME value, env -d NAME"),
        ('edit file "old" "new"', "edit one exact piece of text; refuses if it is not unique (all, -e: \\n)"),
        ("pip command", "install libraries: pip install rich, pip list ... (runs hpip.py)"),
        ("git command", "GitHub from the phone: git clone OWNER/NAME, status, pull, push, log, ls (runs gitkit.py; tul install gitkit)"),
        ("net command", "ip, dns, port, ping, trace: net ip, net dns NAME ... (runs net.py; tul install network)"),
        ("sv [folder] [min]", "share a folder in a browser; cancel stops it (runs sv.py; tul install network)"),
        ("asm prog.s", "run an ARM64 assembly program in a safe emulator (runs asm.py; tul install asm)"),
        ("tul command", "Butter tools: tul install network, tul updates, tul update --all (runs tul.py)"),
        ("wakelock on [min]", "keep the processor awake (30 min by default, 480 at most)"),
        ("wakelock off", "release the wakelock"),
        ("wakelock status", "wakelock and battery-limit status"),
        ("battery", "ask Android to lift battery limits for Butter"),
        ("trust add file", "trust a script: it runs without asking while its content stays the same"),
        ("trust remove file", "stop trusting a script"),
        ("trust list", "list trusted scripts"),
        ("history [N]", "last commands"),
        ("redo [N]", "run a past command again"),
        ("clear", "clear the screen"),
        ("debug [on|off]", "show technical details"),
        ("cancel", "stop a running job"),
        ("help [command]", "this list"),
    ]),
]
HELP_LEGEND = [
    "a | b     run a, then b",
    '"text"    quotes keep spaces and |',
]
# One word + file|dir: "new file a.txt" is the command whose table name is "new file" (a name nobody can type
# as one word, so there is exactly one way to write each command).
PAIRS = {"new": ("new file", "new dir"), "del": ("del file", "del dir"), "rename": ("rename file", "rename dir"),
         "info": ("info file", "info dir"), "find": ("find file", "find dir"), "zip": ("zip file", "zip dir")}
PAIR_USAGE = {"new": "new file name   |   new dir name", "del": "del file name   |   del dir name",
              "rename": "rename file old new   |   rename dir old new", "info": "info file path   |   info dir [path]",
              "find": 'find file "pattern" [folder]   |   find dir "pattern" [folder]',
              "zip": "zip file name [archive]   |   zip dir name [archive]"}


def resolve_command(words):
    """['new', 'file', 'a'] -> ['new file', 'a'];  ['go', 'to', 'x'] -> ['go to', 'x'];  ['go', 'up'] -> ['go up']."""
    if len(words) > 1 and words[0] in PAIRS and words[1] in ("file", "dir"):
        return [PAIRS[words[0]][0 if words[1] == "file" else 1]] + words[2:]
    if len(words) > 1 and words[0] == "go" and words[1] == "to":
        return ["go to"] + words[2:]
    if len(words) == 2 and words[0] == "go" and words[1] == "up":
        return ["go up"]
    return words


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
            self.lock = pm.newWakeLock(autoclass("android.os.PowerManager").PARTIAL_WAKE_LOCK, "butter:keepawake")
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
        try:                                               # one tap: "allow Butter to run without battery limits?"
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
        self.cwd = self.project            # current folder (start in butter)
        # the trust list lives in the app's private folder (other apps cannot edit it); without it, next to the project
        self.state_dir = os.environ.get("ANDROID_PRIVATE") or self.project
        self.power = AndroidPower()                    # wakelock and battery settings (Android only)
        self.wake_until = 0.0
        self.pending = None                # a question waiting for y/n
        self.chain = None                  # rest of a chain paused by that question
        self.cancelled = False
        self.debug = False                 # "debug on" shows DEBUG lines
        self.history = []                  # commands typed so far (for history)
        self.dry = False                   # True only while a command runs under !dry
        self.py_thread = None              # the thread of a running script
        self.sync_jobs = False             # True while a !chk condition runs: background jobs run right there
        self.found_nothing = False         # a search (grep findfile finddir) that found nothing: a !chk condition counts it as "not passed"
        self.job = None                    # a running download (see download)
        self.bg_jobs = []                  # parallel jobs started with !prl
        self.prl = None                    # set only while a command runs under !prl
        self.prl_count = 0                 # numbers the [prl N] labels
        self.screen_version = ""           # the screen (main.py) tells its version here, for "report"
        self.to_dir = None                 # set while "zip ... to FOLDER" runs: where the archive goes
        self.unzip_files = False           # set while "unzip ... to FOLDER" runs: files go straight into the folder
        self.recording = None              # {"name": ..., "lines": [...]} while "macro NAME" collects lines
        self.macro_steps = 0               # commands macros have added to the current input
        self.allow_insecure = False        # only for tests: lets download use http://
        self.buffer = {"mode": None, "items": []}   # copy/cut buffer: absolute paths
        # name -> (handler, usage, min_args, max_args); handlers get a list of words
        self.commands = {
            "to": (self.cmd_to_alone, "CMD ... to FOLDER", 0, None),
            "macro": (self.cmd_macro, "macro NAME = commands   |   macro NAME   |   macro list   |   macro show NAME   |   macro del NAME", 0, None),
            "run": (self.cmd_run_alone, "run NAME [arguments]", 0, None),
            "new": (self.pair_usage("new"), PAIR_USAGE["new"], 0, None),
            "del": (self.pair_usage("del"), PAIR_USAGE["del"], 0, None),
            "rename": (self.pair_usage("rename"), PAIR_USAGE["rename"], 0, None),
            "info": (self.pair_usage("info"), PAIR_USAGE["info"], 0, None),
            "find": (self.pair_usage("find"), PAIR_USAGE["find"], 0, None),
            "zip": (self.pair_usage("zip"), PAIR_USAGE["zip"], 0, None),
            "new file": (self.cmd_new_file, "new file test.txt", 1, 1),
            "new dir": (self.cmd_new_dir, "new dir myfolder", 1, 1),
            "del file": (self.cmd_del_file, "del file test.txt", 1, 1),
            "del dir": (self.cmd_del_dir, "del dir myfolder", 1, 1),
            "rename file": (self.cmd_rename_file, "rename file old.txt new", 2, 2),
            "rename dir": (self.cmd_rename_dir, "rename dir oldfolder newfolder", 2, 2),
            "append": (self.cmd_append, 'append test.txt "some text"', 2, None),
            "overwrite": (self.cmd_overwrite, 'overwrite test.txt "new text"   or   overwrite test.txt "old" replace to "new"', 2, None),
            "copy": (self.cmd_copy, 'copy a.txt "my folder"', 1, None),
            "cut": (self.cmd_cut, "cut a.txt", 1, None),
            "paste": (self.cmd_paste, "paste", 0, 0),
            "buffer": (self.cmd_buffer, "buffer", 0, 0),
            "grep": (self.cmd_grep, 'grep "text" [folder]', 1, None),
            "info file": (self.cmd_info_file, "info file test.txt", 1, None),
            "info dir": (self.cmd_info_dir, "info dir .", 0, None),
            "find file": (self.cmd_find_file, 'find file "*.txt" [folder]', 1, None),
            "find dir": (self.cmd_find_dir, 'find dir "photo" [folder]', 1, None),
            "download": (self.cmd_get_file, "download https://site/file.zip [name]", 1, 2),
            "zip file": (self.cmd_zip_file, "zip file test.txt [archive]", 1, 2),
            "zip dir": (self.cmd_zip_dir, "zip dir myfolder [archive]", 1, 2),
            "unzip": (self.cmd_unzip, "unzip archive.zip   |   unzip archive.zip to FOLDER", 1, None),
            "report": (self.cmd_report, "report", 0, 0),
            "cancel": (self.cmd_cancel, "cancel", 0, 0),
            "clear": (self.cmd_clear, "clear", 0, 0),
            "debug": (self.cmd_debug, "debug on", 0, 1),
            "here": (self.cmd_md, "here", 0, 0),
            "history": (self.cmd_hist, "history [N]", 0, 1),
            "redo": (self.cmd_redo_inline, "redo [N]", 0, 1),
            "help": (self.cmd_help, "help [command]", 0, 1),
            "py": (self.cmd_py, "py script.py [args]", 1, None),
            "env": (self.cmd_env, "env [NAME [value]]   or   env -d NAME", 0, None),
            "edit": (self.cmd_replace, 'edit file "old" "new" [all] [-e]', 3, None),
            "pip": (self.cmd_pip, "pip command [args]", 0, None),
            "asm": (self.cmd_asm, "asm prog.s [--trace] [--regs] [--steps N] [--input text]", 0, None),
            "git": (self.cmd_git, "git clone|status|pull|push|log|ls|list|token ...", 0, None),
            "net": (self.cmd_net, "net ip|dns|port|ping|trace ...", 0, None),
            "sv": (self.cmd_sv, "sv [folder] [minutes] [up] [local]", 0, None),
            "tul": (self.cmd_tul, "tul command [args]", 0, None),
            "wakelock": (self.cmd_wakelock, "wakelock on [minutes]   |   wakelock off   |   wakelock status", 1, 2),
            "battery": (self.cmd_battery, "battery", 0, 0),
            "trust": (self.cmd_trust_main, "trust add script.py   |   trust remove script.py   |   trust list", 1, 2),
            "echo": (self.cmd_echo, 'echo "hello"', 0, None),
            "list": (self.cmd_list, "list .", 0, None),
            "view": (self.cmd_view_file, "view test.txt [h N | t N | A B]", 1, None),
            "go": (self.cmd_go_alone, "go to FOLDER   |   go up", 0, None),
            "go to": (self.cmd_go, "go to Download", 0, None),
            "go up": (self.cmd_up, "go up", 0, 0),
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
            return None, (ERROR, "Invalid name '%s'. Names cannot contain '/' (use go to FOLDER to change folder)." % name)
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
            return None, (ERROR, "File '%s' not found. Create it first with new file." % name)
        if os.path.isdir(path):
            return None, (ERROR, "'%s' is a folder." % name)
        if not self.looks_like_text(path):
            return None, (ERROR, "'%s' is not a text file." % name)
        return path, None

    def startup(self):
        ok, reason = self.check_path(self.project)
        if not ok:
            return [(ERROR, "Project folder is not allowed: " + reason)]
        moved = []
        legacy = os.path.join(os.path.dirname(self.project), LEGACY_PROJECT_NAME)
        if (os.path.basename(self.project) == PROJECT_NAME and not os.path.exists(self.project)
                and os.path.isdir(legacy)):
            try:
                os.rename(legacy, self.project)        # keep all the user's files: only the folder name changes
                moved = [(INFO, "Your folder %s was renamed to %s." % (legacy, self.project))]
            except OSError as e:
                return [(ERROR, "Cannot rename %s to %s: %s" % (legacy, self.project, e.strerror or e))]
        try:
            os.makedirs(self.project, exist_ok=True)
        except OSError as e:
            return [(ERROR, "Cannot open project folder %s: %s" % (self.project, e.strerror or e))]
        return moved + [(INFO, "Butter ready. Project folder: " + self.project),
                (INFO, "Type help for the list of commands."),
                (INFO, self.contact_hint())] + self.tools_hint()

    # ---------- entry point ----------
    def execute(self, line):
        """Run one line; commands can be chained with |"""
        if self.recording:
            return self.record_line(line)
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
        self.macro_steps = 0
        self.remember(line)
        m = MACRO_DEF.match(line)
        if m:                                                # macro NAME = A | B   (the | belongs to the macro)
            return self.define_macro(m.group(2), [m.group(3)])
        try:
            segments = parse_line(line)
        except ValueError as e:
            return [(ERROR, str(e))]
        if len(segments) == 1 and not segments[0]:
            return []                      # blank line
        if len(segments) > 1 and any(not seg for seg in segments):
            return [(ERROR, "Empty command next to '|'.")]
        if all(seg == LONE_MA for seg in segments):
            return [(ERROR, "'!ma' needs a command after it, e.g. !ma new file test.txt")]
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
                    return out + [(ERROR, "A modifier needs a command after it, e.g. %s new file test.txt" % mods[0])] \
                        + self.stopped_note(segments[i + 1:])
                i += 1
                continue                   # a lone "!ma" only switches it on
            if words[0][0] == "run" and not words[0][1]:       # a macro takes this place in the chain
                new_segments, err = self.expand_macro(segments, i, [m for m in mods if m != "!ma"], words)
                if new_segments is None:
                    return out + [err] + self.stopped_note(segments[i + 1:])
                segments = new_segments
                continue
            pre = [(DEBUG, "run: %s  (in %s)" % (" ".join(w for w, _ in words), self.cwd))] if self.debug else []
            self.dry = "!dry" in mods
            prl = "!prl" in mods
            self.prl = {"hide": "!hl" in mods, "tag": None} if prl else None
            cmd = resolve_command([w for w, _ in words])[0]
            typed = words[0][0]                                                                   # what the user wrote
            if len(words) > 1 and words[0][0] in PAIRS:
                typed += " " + words[1][0]
            try:
                if prl and cmd not in PRL_COMMANDS:
                    res = [(ERROR, "'%s' cannot run with !prl. Allowed: download, zip, unzip, paste, grep, find, info dir" % typed)]
                elif prl and len(self.bg_jobs) >= MAX_PRL_JOBS:
                    res = [(ERROR, "Too many parallel jobs (max %d). Wait for one, or type: cancel" % MAX_PRL_JOBS)]
                else:
                    res = self.run_pairs(words)
            finally:
                prl_info, self.prl, self.dry = self.prl, None, False
            if prl and self.pending:                               # a question needs the answer now: no background
                self.pending = None
                res = [(ERROR, "'%s' asks a question, so it cannot run with !prl. Run it without !prl." % typed)]
            if prl and prl_info["tag"]:
                res = [(k, prl_info["tag"] + " " + t) for k, t in res]
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

    def find_then(self, words):
        """Index of the plain word 'then' / 'else' that ends the condition of !chk, or None."""
        for k, (w, quoted) in enumerate(words):
            if not quoted and w in ("then", "else"):
                return k
        return None

    def expand_check(self, segments, i, other_mods, words):
        """!chk A [| B ...] then|else C : decide now and put C (or nothing) in place of the whole group.
        Returns (new segments, notes), or (None, error) when the check cannot be used."""
        usage = (ERROR, "Usage: !chk COMMAND [| COMMAND] then COMMAND   (else = run it when the check fails)")
        cond, cur, j = [], list(words), i
        while True:
            k = self.find_then(cur)
            if k is not None:
                break
            cond.append(cur)
            j += 1
            if j >= len(segments):
                old_words = any(w in ("to", "nto") and not q for seg in cond for w, q in seg)
                if old_words:
                    usage = (ERROR, usage[1] + "   (the words to / nto were renamed: then / else; to now means a folder)")
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
        run_it = passed if keyword == "then" else not passed
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
            if resolve_command(words)[0] in JOB_COMMANDS:
                return None, "'%s' cannot be used inside a check condition." % words[0]
            self.dry, self.sync_jobs, self.found_nothing = dry, True, False
            try:
                res = self.run_pairs(seg)
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
            return [(ERROR, "Usage: redo  (the last command)  or  redo N  (number N from history)")]
        if not self.history:
            return [(ERROR, "No commands yet.")]
        n = int(args[0]) if args else len(self.history)
        if not 1 <= n <= len(self.history):
            return [(ERROR, "There is no command number %d (see history)." % n)]
        text = self.history[n - 1]
        return [(TEXT, "-> " + text)] + self.execute(text)           # runs exactly as if it was typed now

    def cmd_redo_inline(self, args):
        return [(ERROR, "redo works only as a whole line: type just  redo  or  redo N")]

    def mask_secrets(self, text):
        """The history must never hold the value of a variable set with env."""
        parts, masked = text.split("|"), False
        for i, seg in enumerate(parts):
            w = seg.split()
            if w and w[0] == "env" and w[1:2] != ["-d"] and (len(w) >= 3 or (len(w) == 2 and "=" in w[1])):
                parts[i] = "env %s ***" % w[1].split("=", 1)[0]
                masked = True
        return " | ".join(x.strip() for x in parts) if masked else text

    def remember(self, line):
        text = self.mask_secrets(line.strip())
        if not text or text.split()[0] in ("history", "redo"):
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

    def drain(self, job):
        """Take the messages a job has put in its queue -> (messages, finished, failed)."""
        out, finished, failed = [], False, False
        while True:
            try:
                kind, msg = job["queue"].get_nowait()
            except queue.Empty:
                break
            if kind == "_done":
                finished, failed = True, msg
                break
            out.append((kind, msg))
        if not finished and not job["thread"].is_alive() and job["queue"].empty():
            out.append((ERROR, "The background job ended unexpectedly."))    # the worker died without a word
            finished, failed = True, True
        return out, finished, failed

    def poll_bg(self):
        """Messages of the parallel (!prl) jobs, each with its [prl N] label."""
        out = []
        for job in list(self.bg_jobs):
            msgs, finished, _ = self.drain(job)
            if job.get("hide"):
                msgs = [m for m in msgs if m[0] not in HIDE_KINDS]
            out += [(k, job["tag"] + " " + t) for k, t in msgs]
            if finished:
                self.bg_jobs.remove(job)
        return out

    def cancel_bg(self):
        """Stop all parallel jobs (the workers notice, stop and remove their leftovers). Returns how many."""
        n = len(self.bg_jobs)
        for job in self.bg_jobs:
            job["cancel"].set()
        self.bg_jobs = []
        return n

    def poll(self):
        """The screen calls this several times a second: messages from a running download,
        and, when it ends, the rest of the paused chain."""
        bg = self.poll_bg()
        if not self.job:
            return bg
        out, finished, failed = self.drain(self.job)
        if not finished:
            return bg + out
        self.job = None
        return bg + self.after_step(out, failed)

    def during_job(self, line):
        if line.strip().lower() == "cancel":
            job = self.job
            job["cancel"].set()
            self.job = None                      # we do not wait: the worker notices, stops and removes its leftovers
            if job["label"] == "python":
                self.interrupt(job["thread"])
            extra = self.cancel_bg()
            note = "Cancelled." if not extra else "Cancelled (and %d parallel job(s))." % extra
            return self.after_step([(INFO, note)], True)
        return [(WARNING, "A %s is running. Wait for it, or type: cancel" % self.job["label"])]

    def find_dest(self, words):
        """Index of the plain word 'to' that introduces a destination folder (CMD ARGS to FOLDER), or None.
        'replace to' (overwrite) and 'unzip to ARCHIVE' are something else; only some commands take a folder."""
        plain = [w for w, _ in words]
        for k in range(1, len(words)):
            if words[k][1] or plain[k] != "to":
                continue
            if plain[k - 1] == "replace":
                continue
            return k if resolve_command(plain[:k])[0] in TO_COMMANDS else None
        return None

    def run_pairs(self, words):
        """Run one command given as (word, was_quoted) pairs, with the 'to FOLDER' ending understood."""
        plain = [w for w, _ in words]
        k = self.find_dest(words)
        if k is None:
            return self.run_one(plain)
        if len(words) != k + 2:
            return [(ERROR, "'to' needs exactly one folder after it, e.g. %s to Pictures" % " ".join(plain[:k]))]
        return self.run_with_dest(plain[:k], plain[k + 1])

    def run_with_dest(self, words, dest):
        """CMD ARGS to FOLDER: things are created / saved / pasted / extracted in FOLDER (any path goto accepts).
        What the command READS (zip and unzip sources, copy and cut) is taken from the folder you are in."""
        folder = self.cwd if dest == "here" else self.where(dest)        # "here" = the folder you are in
        if not os.path.lexists(folder):
            return [(ERROR, "Folder '%s' not found." % dest)]
        if not os.path.isdir(folder):
            return [(ERROR, "'%s' is a file, not a folder." % dest)]
        if not os.access(folder, os.R_OK | os.X_OK):
            return [(ERROR, "Permission denied: %s" % folder)]
        words = resolve_command(words)
        name, home = words[0], self.cwd
        try:
            if name in ("copy", "cut"):                       # put in the buffer from here, paste there at once
                res = self.run_one(words)
                if self.dry:
                    return res + [(INFO, "...and it would be pasted into %s." % folder)]
                if any(kind in ERROR_KINDS for kind, _ in res) or self.pending:
                    return res
                self.cwd = folder
                return self.run_one(["paste"])
            if name in ("zip file", "zip dir"):               # the source stays here, only the archive goes there
                self.to_dir = folder
            else:
                if name == "unzip":
                    self.unzip_files = True                    # to FOLDER: straight into it (without: a new folder)
                    if len(words) > 1:
                        words = [name, self.where(" ".join(words[1:]))]    # the archive is read from here
                self.cwd = folder
            return self.run_one(words)
        finally:
            self.cwd, self.to_dir, self.unzip_files = home, None, False

    # ---------- macros ----------
    def macro_path(self):
        return os.path.join(self.project, MACRO_FILE)

    def load_macros(self):
        try:
            with open(self.macro_path(), encoding="utf-8") as f:
                data = json.load(f)
            return {n: v for n, v in data.items() if isinstance(v, dict) and isinstance(v.get("lines"), list)}
        except OSError:
            return {}                                          # no file yet: no macros
        except (ValueError, AttributeError):
            try:                                               # a damaged file: keep it aside instead of overwriting it
                os.replace(self.macro_path(), self.macro_path() + ".broken")
            except OSError:
                pass
            return {}

    def write_macros(self, data):
        path = self.macro_path()
        try:
            with open(path + ".tmp", "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            os.replace(path + ".tmp", path)
        except OSError as e:
            return str(e.strerror or e)
        return None

    def check_macro(self, name, lines):
        """-> an error text, or None when the name and the lines are fine."""
        if not MACRO_NAME.match(name):
            return "Macro names: a letter first, then letters, digits, _ or -, up to 20 characters."
        if name in MACRO_RESERVED:
            return "'%s' is reserved. Pick another name." % name
        if not lines:
            return "The macro is empty."
        stars = False
        for line in lines:
            try:
                segments = parse_line(line)
            except ValueError as e:
                return "In '%s': %s" % (line, e)
            if not segments or any(not seg for seg in segments):
                return "Empty command in '%s'." % line
            for seg in segments:
                try:
                    _, words = self.take_modifiers(list(seg))
                except ValueError as e:
                    return str(e)
                if words and not words[0][1] and words[0][0] in MACRO_FORBIDDEN:
                    return "'%s' cannot be used inside a macro." % words[0][0]
                if words and not words[0][1] and words[0][0] == "run" and len(words) > 1 and words[1][0] == name:
                    return "A macro cannot call itself."
                for w, q in seg:
                    if "$*" in w and w != "$*":
                        return "$* must stand alone as a word, not inside '%s'." % w
        return None

    def define_macro(self, name, lines):
        err = self.check_macro(name, lines)
        if err:
            return [(ERROR, err)]
        data = self.load_macros()
        if name not in data and len(data) >= MAX_MACROS:
            return [(ERROR, "Too many macros (max %d). Delete one: macro del NAME" % MAX_MACROS)]
        if name in data:
            return self.ask({"type": "macro_save", "name": name, "lines": lines, "dir": self.cwd},
                            "Macro '%s' already exists. Replace it? (y/n)" % name)
        return self.save_macro(name, lines)

    def save_macro(self, name, lines):
        data = self.load_macros()
        data[name] = {"lines": lines}
        err = self.write_macros(data)
        if err:
            return [(ERROR, "Cannot save the macros: " + err)]
        return [(SUCCESS, "Macro '%s' saved (%d line%s). Run it with: run %s" % (name, len(lines), "" if len(lines) == 1 else "s", name))]

    def record_line(self, line):
        """While 'macro NAME' is collecting: one command per line, end saves, cancel throws it away."""
        text = line.strip()
        rec = self.recording
        if text == "cancel":
            self.recording = None
            return [(INFO, "Macro '%s' thrown away." % rec["name"])]
        if text == "end":
            self.recording = None
            if not rec["lines"]:
                return [(ERROR, "The macro is empty: nothing saved.")]
            return self.define_macro(rec["name"], rec["lines"])
        if not text:
            return []
        try:
            segments = parse_line(text)
        except ValueError as e:
            return [(ERROR, "%s  (this line was not added)" % e)]
        if any(not seg for seg in segments):
            return [(ERROR, "Empty command next to '|'  (this line was not added)")]
        rec["lines"].append(text)
        return [(INFO, "  %d: %s" % (len(rec["lines"]), text))]

    def cmd_macro(self, args):
        if not args:
            return [(ERROR, "Usage: macro NAME = commands   |   macro NAME   |   macro list   |   macro show NAME   |   macro del NAME")]
        sub = args[0]
        data = self.load_macros()
        if sub == "list":
            if not data:
                return [(INFO, "No macros yet. Make one: macro clean = del file a.txt | del dir tmp")]
            return [(TEXT, "%s: %s%s" % (n, data[n]["lines"][0], "  (+%d more)" % (len(data[n]["lines"]) - 1) if len(data[n]["lines"]) > 1 else ""))
                    for n in sorted(data)]
        if sub in ("show", "del"):
            if len(args) != 2:
                return [(ERROR, "Usage: macro %s NAME" % sub)]
            if args[1] not in data:
                return [(ERROR, "There is no macro '%s'. See: macro list" % args[1])]
            if sub == "show":
                return [(TEXT, "%d: %s" % (n, line)) for n, line in enumerate(data[args[1]]["lines"], 1)]
            return self.ask({"type": "macro_del", "name": args[1], "dir": self.cwd}, "Delete macro '%s'? (y/n)" % args[1])
        if len(args) >= 2 and args[1] == "=":
            return [(ERROR, "'macro NAME = commands' must be typed as a line of its own (the | belongs to the macro).")]
        if len(args) != 1:
            return [(ERROR, "Usage: macro NAME   (then type the commands; or: macro NAME = commands)")]
        err = self.check_macro(sub, ["echo x"])
        if err:
            return [(ERROR, err)]
        self.recording = {"name": sub, "lines": []}
        return [(INFO, "Recording macro '%s': one command per line, end saves, cancel throws it away." % sub)]

    def substitute(self, words, args):
        """$1..$9 -> the arguments, $* -> all of them as separate words. Done on the parsed words, so an argument
        with spaces, | or the word 'to' stays plain text and cannot add commands (it is marked as quoted)."""
        out = []
        for w, q in words:
            if w == "$*" and not q:
                out += [(a, True) for a in args]
                continue
            if "$" in w:
                new = re.sub(r"\$([1-9])", lambda m: args[int(m.group(1)) - 1] if int(m.group(1)) <= len(args) else m.group(0), w)
                if new != w:
                    q = True
                w = new
            out.append((w, q))
        return out

    def expand_macro(self, segments, i, mods, words):
        """'run NAME args' standing at segments[i] -> the macro's commands in its place (with the same modifiers)."""
        if "!prl" in mods:
            return None, (ERROR, "'run' cannot be used with !prl (a macro is not one background job).")
        if len(words) < 2:
            return None, (ERROR, "Usage: run NAME [arguments]")
        name, args = words[1][0], [w for w, _ in words[2:]]
        lines = self.load_macros().get(name, {}).get("lines")
        if not lines:
            return None, (ERROR, "There is no macro '%s'. See: macro list" % name)
        body = []
        for line in lines:
            try:
                body += parse_line(line)
            except ValueError as e:
                return None, (ERROR, "Macro '%s' is damaged: %s" % (name, e))
        need, star = 0, False
        for seg in body:
            for w, q in seg:
                if w == "$*" and not q:
                    star = True
                for d in re.findall(r"\$([1-9])", w):
                    need = max(need, int(d))
        if (star and len(args) < need) or (not star and len(args) != need):
            return None, (ERROR, "Macro '%s' needs %s%d argument%s, got %d. Nothing was run." %
                          (name, "at least " if star else "", need, "" if need == 1 else "s", len(args)))
        self.macro_steps += len(body)
        if self.macro_steps > MAX_MACRO_STEPS:
            return None, (ERROR, "Too many macro steps (max %d for one input): does a macro call itself? Nothing more was run." % MAX_MACRO_STEPS)
        prefix = [(m, False) for m in mods]
        replacement = [prefix + self.substitute(seg, args) for seg in body]
        return segments[:i] + replacement + segments[i + 1:], None

    def contact_hint(self):
        names = " | ".join("%s %s" % (n, v) for n, v in CONTACTS if v)
        return ("Problem or idea? %s (or type: report)" % names) if names else "Problem or idea? Type: report"

    def diagnostics(self):
        """One line to paste into a bug report. No file names, commands or tokens in it."""
        try:
            android = getattr(sys, "getandroidapilevel", None)
            system = "Android API %s" % android() if callable(android) else platform.system()
            return "Butter core %s, screen %s, Python %s, %s, %s" % (
                VERSION, self.screen_version or "?", sys.version.split()[0], system, platform.machine() or "?")
        except Exception:
            return "Butter core %s" % VERSION

    def cmd_report(self, args):
        lines = ["Found a bug or have an idea? Tell me:"]
        lines += ["  %s: %s" % (n, v) for n, v in CONTACTS if v]
        lines += ["  GitHub issues: " + ISSUES_URL, "",
                  "Copy this line into your message:", "  " + self.diagnostics(),
                  "(It has no file names, commands or tokens. Say what you typed and what you expected.)"]
        return [(TEXT, "\n".join(lines))]

    def cmd_run_alone(self, args):
        return [(ERROR, "'run' starts a macro and must stand at the beginning of a command, e.g.  run clean")]

    def cmd_go_alone(self, args):
        return [(ERROR, "Say where: go to FOLDER   |   go up   |   go to  (alone = back to butter)")]

    def cmd_to_alone(self, args):
        return [(ERROR, "'to' goes after a command, e.g.  new file a.txt to Pictures   or   paste to Docs")]

    def run_one(self, words):
        try:
            words = resolve_command(words)
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
                return [(ERROR, "Usage: history [N]  (N = how many commands, at least 1)")]
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
            word = resolve_command([args[0]])[0]
            found = [(u, d) for _, pairs in HELP for u, d in pairs if u.split()[0] == word]
            if not found:
                return [(ERROR, "No help for '%s'. Try help" % word)]
            return [(TEXT, "\n".join(lines(found)))]
        out = ["Butter commands"]
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
            target = self.project                  # "go to" alone = back to butter
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
        """view path | view path h [N] | view path t [N] | view path A B  ->  (path, mode, a, b)"""
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
            return [(ERROR, "'%s' is a folder. Use list to list it." % raw)]
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
            out.append("... cut: showing the first %d line(s). Use view path h/t N or A B for more." % len(shown))
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
            return [(ERROR, "'%s' is a folder. Use info dir for folders." % raw)]
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
            return [(ERROR, "'%s' is a file. Use info file for files." % raw)]
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
            return [(ERROR, "'%s' is a folder. Use del dir to delete folders." % name)]
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
            return [(ERROR, "'%s' is a file. Use del file to delete files." % name)]
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
            return [(ERROR, "'%s' is a file. Use rename file to rename files." % old)]
        if not is_dir and old_is_dir:
            return [(ERROR, "'%s' is a folder. Use rename dir to rename folders." % old)]
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
        for i in range(len(rest) - 1):                # overwrite name "old" replace to "new"
            if rest[i] == "replace" and rest[i + 1] == "to":
                if len(rest) == i + 2:
                    return [(ERROR, 'Usage: overwrite test.txt "old" replace to "new"')]
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
        path, err = self.existing_text_file(name)                  # protected like overwrite and append
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
            # copying only reads (free); cutting removes the source, so it is protected like del file
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
        if self.prl is not None:                 # !prl: a parallel job, the chain does not wait for it
            self.prl_count += 1
            job["tag"] = "[prl %d]" % self.prl_count
            job["hide"] = self.prl["hide"]
            self.prl["tag"] = job["tag"]
            self.bg_jobs.append(job)
        else:
            self.job = job
        thread = threading.Thread(target=worker, args=(job,) + args, daemon=True)
        job["thread"] = thread
        thread.start()
        return job

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
        job = self.start_job(label, worker, *args)
        job["thread"].join(FAST_WAIT)
        if not job["thread"].is_alive():
            if job is self.job:
                return self.poll()
            out, _, _ = self.drain(job)                # a parallel job that is already done
            if job in self.bg_jobs:
                self.bg_jobs.remove(job)
            return [m for m in out if not (job.get("hide") and m[0] in HIDE_KINDS)]
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
        n = self.cancel_bg()
        if n:
            return [(INFO, "Cancelled %d parallel job(s)." % n)]
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
            req = urllib.request.Request(url, headers={"User-Agent": "Butter/1.0"})
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
            return [(ERROR, "'%s' is a file. Use zip file for files." % name)]
        if not is_dir and src_is_dir:
            return [(ERROR, "'%s' is a folder. Use zip dir for folders." % name)]
        if out_name is None:
            out_name = (name if is_dir else os.path.splitext(name)[0]) + ".zip"
        elif not out_name.lower().endswith(".zip"):
            out_name += ".zip"
        out_dir = self.to_dir or self.cwd                             # "zip ... to FOLDER": the archive goes there
        out_path, err = self.resolve(out_name, out_dir, protect=False)    # the archive is a new file: free
        if err:
            return [err]
        if os.path.lexists(out_path):
            suggested = self.unique_name(out_name, False, out_dir)
            return self.ask({"type": "zip", "name": name, "out": suggested, "is_dir": is_dir, "dir": self.cwd,
                             "out_dir": out_dir},
                            "File '%s' already exists. Zip it as '%s' instead? (y/n)" % (out_name, suggested))
        return self.start_zip(name, out_name, is_dir, self.cwd, out_dir)

    def start_zip(self, name, out_name, is_dir, folder, out_dir=None):
        ok, reason = self.check_path(os.path.join(folder, name), deep_only=True)
        if not ok:
            return [(ERROR, reason)]
        if self.dry:
            return [(INFO, "Would zip '%s' into '%s'." % (name, out_name))]
        self.start_job("zip", self.zip_worker, os.path.join(folder, name), os.path.join(out_dir or folder, out_name), is_dir)
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

    def cmd_unzip(self, args):
        """unzip a.zip -> into a new folder 'a';  unzip a.zip to FOLDER (or to here) -> straight into that folder."""
        return self.unzip_command(" ".join(args), "files" if self.unzip_files else "dir")

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
            same = os.path.realpath(dest) == os.path.realpath(self.cwd)       # not the folder you are in: "unzip ... to FOLDER"
            where = "'%s'" % os.path.basename(dest) if (created_dirs and created_dirs[0] == dest) or not same else "the current folder"
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

    def pair_usage(self, name):
        """'new' without file|dir (or with something else): say how to write it."""
        def handler(args):
            return [(ERROR, "Say what it is: " + PAIR_USAGE[name])]
        return handler

    def run_tool(self, command, filename, tul_id, args):
        """A tool without 'py': 'git status' = 'py gitkit.py status'. The script lies in the project folder (tul puts it there)."""
        script = os.path.join(self.project, filename)
        if not os.path.isfile(script):
            return [(ERROR, "%s is not in the project folder (%s). Install it with: tul install %s"
                     % (filename, self.project, tul_id))]
        return self.cmd_py([script] + list(args))

    def cmd_asm(self, args):
        return self.run_tool("asm", "asm.py", "asm", args)

    def cmd_git(self, args):
        return self.run_tool("git", "gitkit.py", "gitkit", args)

    def cmd_net(self, args):
        return self.run_tool("net", "net.py", "network", args)

    def cmd_sv(self, args):
        return self.run_tool("sv", "sv.py", "network", args)

    # ---------- wakelock and battery ----------
    def cmd_trust_main(self, args):
        sub, rest = args[0], args[1:]
        if sub == "add" and len(rest) == 1:
            return self.cmd_trust(rest)
        if sub == "remove" and len(rest) == 1:
            return self.cmd_untrust(rest)
        if sub == "list" and not rest:
            return self.cmd_trust_list(rest)
        return [(ERROR, "Usage: trust add script.py   |   trust remove script.py   |   trust list")]

    def cmd_wakelock(self, args):
        sub, rest = args[0], args[1:]
        if sub == "on" and len(rest) <= 1:
            return self.cmd_wake_on(rest)
        if sub == "off" and not rest:
            return self.cmd_wake_off(rest)
        if sub == "status" and not rest:
            return self.cmd_wake_status(rest)
        return [(ERROR, "Usage: wakelock on [minutes]   |   wakelock off   |   wakelock status")]

    def cmd_wake_on(self, args):
        minutes = WAKE_DEFAULT_MINUTES
        if args:
            if not args[0].isdigit() or not 1 <= int(args[0]) <= WAKE_MAX_MINUTES:
                return [(ERROR, "Minutes: 1 to %d. Example: wakelock on 30" % WAKE_MAX_MINUTES)]
            minutes = int(args[0])
        if self.dry:
            return [(INFO, "Would keep the processor awake for %d min." % minutes)]
        try:
            self.power.acquire(minutes)
        except Exception as e:
            return [(ERROR, "Cannot hold the wakelock: %s" % e)]
        self.wake_until = time.time() + minutes * 60
        return [(SUCCESS, "The processor stays awake for %d min (the screen can still turn off). Release it: wakelock off" % minutes)]

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
                lines.append("battery limits: none (Android will not stop Butter to save power)")
            else:
                lines.append("battery limits: ON (Android may stop Butter in the background; run: battery)")
        except Exception as e:
            lines.append("battery limits: unknown (%s)" % e)
        return [(TEXT, "\n".join(lines))]

    def cmd_battery(self, args):
        if self.dry:
            return [(INFO, "Would ask Android to lift the battery limits for Butter.")]
        try:
            self.power.open_battery_settings()
        except Exception as e:
            return [(ERROR, "Cannot open the battery setting: %s" % e)]
        return [(INFO, "Android asks whether Butter may run without battery limits. Choose Allow.")]

    # ---------- env: variables for scripts, kept in the app's private folder ----------
    def env_path(self):
        return os.path.join(self.state_dir, ENV_FILE)

    @staticmethod
    def env_name_ok(name):
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$", name):
            return False
        up = name.upper()
        return not (up.startswith(ENV_BLOCKED_PREFIXES) or up in ENV_BLOCKED_NAMES)

    def load_env(self):
        try:
            with open(self.env_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str) and self.env_name_ok(k)}

    def save_env(self, data):
        tmp = self.env_path() + ".tmp"
        try:
            os.makedirs(self.state_dir, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=1, sort_keys=True)
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, self.env_path())
        except OSError as e:
            return e.strerror or str(e)
        return None

    def cmd_env(self, args):
        data = self.load_env()
        if not args:
            if not data:
                return [(INFO, "No variables. Set one:  env NAME value   (scripts read it with os.environ)")]
            return [(TEXT, "\n".join("%s = ****  (%d chars)" % (k, len(data[k])) for k in sorted(data)))]
        if args[0] == "-d":
            if len(args) != 2:
                return [(ERROR, "Usage: env -d NAME")]
            if args[1] not in data:
                return [(ERROR, "'%s' is not set." % args[1][:40])]
            if self.dry:
                return [(INFO, "Would remove '%s'." % args[1])]
            del data[args[1]]
            err = self.save_env(data)
            return [(ERROR, "Cannot save: " + err)] if err else [(INFO, "'%s' is removed." % args[1])]
        if len(args) == 1 and "=" not in args[0]:
            if args[0] not in data:
                return [(ERROR, "'%s' is not set." % args[0][:40])]
            return [(TEXT, "%s = %s" % (args[0], data[args[0]]))]
        if len(args) == 1:
            name, value = args[0].split("=", 1)
        else:
            name, value = args[0], " ".join(args[1:])
        if not self.env_name_ok(name):
            return [(ERROR, "Bad name '%s'. Use letters, digits and _ (not first a digit); names such as PATH, "
                            "PYTHON*, LD_*, ANDROID_* are reserved." % name[:40])]
        if not value or len(value) > ENV_MAX_VALUE or any(c in value for c in "\n\r\x00"):
            return [(ERROR, "The value must be one line, 1 to %d characters." % ENV_MAX_VALUE)]
        if name not in data and len(data) >= ENV_MAX_VARS:
            return [(ERROR, "Too many variables (limit %d)." % ENV_MAX_VARS)]
        if self.dry:
            return [(INFO, "Would set '%s' (%d chars)." % (name, len(value)))]
        data[name] = value
        err = self.save_env(data)
        if err:
            return [(ERROR, "Cannot save: " + err)]
        return [(SUCCESS, "'%s' is set (%d chars): scripts see it in os.environ. The screen still shows what you "
                          "typed: run  clear." % (name, len(value)))]

    # ---------- ~rp: one exact edit ----------
    @staticmethod
    def unescape(text):
        return re.sub(r"\\(n|t|r|\\)", lambda m: {"n": "\n", "t": "\t", "r": "\r", "\\": "\\"}[m.group(1)], text)

    @staticmethod
    def edit_preview(old_text, new_text, line, old, new):
        k_old, k_new = old.count("\n") + 1, new.count("\n") + 1
        a, b = old_text.split("\n"), new_text.split("\n")
        rows = ["- %4d: %s" % (line + i, x.rstrip("\r")[:100]) for i, x in enumerate(a[line - 1:line - 1 + k_old][:3])]
        rows += ["+ %4d: %s" % (line + i, x.rstrip("\r")[:100]) for i, x in enumerate(b[line - 1:line - 1 + k_new][:3])]
        return "\n".join(rows)

    def cmd_replace(self, args):
        name, old, new, rest = args[0], args[1], args[2], args[3:]
        bad = [x for x in rest if x not in ("all", "-e")]
        if bad:
            return [(ERROR, "Unknown option '%s'. Options: all, -e" % bad[0][:20])]
        if "-e" in rest:
            old, new = self.unescape(old), self.unescape(new)
        if not old:
            return [(ERROR, "Nothing to replace: the old text is empty.")]
        if old == new:
            return [(INFO, "The old and the new text are the same: nothing to change.")]
        path, err = self.existing_text_file(name)
        if err:
            return [err]
        if os.path.getsize(path) > MAX_GREP_FILE_BYTES:
            return [(ERROR, "The file is too big to edit (limit %d MB)." % (MAX_GREP_FILE_BYTES // 1000000))]
        try:
            with open(path, "r", encoding="utf-8", newline="") as f:
                text = f.read()
        except UnicodeDecodeError:
            return [(ERROR, "'%s' is not a UTF-8 text file." % name)]
        spots, i = [], 0
        while True:
            i = text.find(old, i)
            if i < 0:
                break
            spots.append(i)
            i += len(old)
        shown = old if len(old) <= 40 else old[:40] + "..."
        if not spots:
            return [(ERROR, "'%s' not found in '%s'. Nothing changed." % (shown, name))]
        lines = [text.count("\n", 0, x) + 1 for x in spots]
        if len(spots) > 1 and "all" not in rest:
            where = ", ".join(str(n) for n in lines[:8]) + (", ..." if len(lines) > 8 else "")
            return [(ERROR, "'%s' occurs %d times (lines %s). Add more text around it to make it unique, "
                            "or add the word: all" % (shown, len(spots), where))]
        new_text = text.replace(old, new) if "all" in rest else text[:spots[0]] + new + text[spots[0] + len(old):]
        view = self.edit_preview(text, new_text, lines[0], old, new)
        more = " (+%d more)" % (len(spots) - 1) if len(spots) > 1 else ""
        if self.dry:
            return [(INFO, "Would replace %d occurrence(s) in '%s', first at line %d%s:" % (len(spots) if "all" in rest else 1, name, lines[0], more)),
                    (TEXT, view)]
        tmp = path + ".part"
        try:
            with open(tmp, "w", encoding="utf-8", newline="") as f:
                f.write(new_text)
            try:
                shutil.copymode(path, tmp)
            except OSError:
                pass
            os.replace(tmp, path)
        except OSError as e:
            try:
                os.remove(tmp)
            except OSError:
                pass
            return [(ERROR, "Cannot write '%s': %s. Nothing changed." % (name, e.strerror or e))]
        return [(SUCCESS, "Replaced %d occurrence(s) in '%s', first at line %d%s." % (len(spots) if "all" in rest else 1, name, lines[0], more)),
                (TEXT, view)]

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
        env_prev = {}
        real_exits = None

        def plain_exit(code=None):                                 # only raises SystemExit, nothing else
            raise SystemExit(code)
        try:
            # Some hosts (IDEs such as Pydroid) replace sys.exit/exit/quit with something that closes the
            # whole app. A script must only end itself, so it gets the plain version while it runs.
            real_exits = (sys.exit, getattr(builtins, "exit", None), getattr(builtins, "quit", None))
            sys.exit = plain_exit
            builtins.exit = builtins.quit = plain_exit
            os.chdir(folder)                       # the script starts in the folder you were in
            sys.stdout, sys.stderr, sys.stdin = out, err, io.StringIO("")      # input() ends at once instead of hanging
            sys.argv = [path] + list(args)
            for k, v in self.load_env().items():                              # variables from the env command
                env_prev[k] = os.environ.get(k)
                os.environ[k] = v
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
            if real_exits:
                sys.exit = real_exits[0]
                for name, old in (("exit", real_exits[1]), ("quit", real_exits[2])):
                    if old is not None:
                        setattr(builtins, name, old)
            for k, old in env_prev.items():
                if old is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = old
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
            out, out_dir = action["out"], action.get("out_dir") or folder
            if os.path.lexists(os.path.join(out_dir, out)):               # taken in the meantime
                out = self.unique_name(out, False, out_dir)
            return self.start_zip(action["name"], out, action["is_dir"], folder, out_dir)
        if action["type"] == "unzip":
            return self.start_unzip(action["archive"], action["dest"], action["plan"], True)
        if action["type"] == "macro_save":
            return self.save_macro(action["name"], action["lines"])
        if action["type"] == "macro_del":
            data = self.load_macros()
            if data.pop(action["name"], None) is None:
                return [(ERROR, "There is no macro '%s'." % action["name"])]
            err = self.write_macros(data)
            return [(ERROR, "Cannot save the macros: " + err)] if err else [(SUCCESS, "Macro '%s' deleted." % action["name"])]
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
