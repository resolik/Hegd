"""tul v1.0 - the toolchain manager of Hegd: installs and updates Hegd's own tools from a Git repository.

Usage in Hegd (or with the command  tul ...  once it is built into the app):
    py tul.py repo [https://raw.githubusercontent.com/USER/REPO/main]   show / set the repository
    py tul.py list                    tools in the repository and what is installed
    py tul.py install hpip            tool "hpip":    hpip.py
    py tul.py install network         tool "network": hegd_sv.py, sv.py
    py tul.py install NAME --force    take over files that already exist (the old ones are kept in the backup)
    py tul.py updates                 which installed tools have a newer version (asks the server once per 10 min)
    py tul.py update NAME             update one tool
    py tul.py update --all            update every installed tool
    py tul.py rollback NAME           go back to the version before the last install/update

How it stays safe:
  - the list of tools is tools.json in the repository: tool name -> files, version, SHA-256 of every file;
  - everything is downloaded, checked (SHA-256, the Python code must compile) BEFORE any file is replaced;
  - the files that are replaced are copied to a backup first; if anything fails, everything is put back;
  - one backup generation is kept per tool, so  rollback  brings the previous version back.
Files land in the folder where tul.py lies (the Hegd project folder). The updated code is NEW code:
if you had trusted a script (+tr), Hegd asks again, and you decide whether to trust the new content.
"""
import hashlib
import json
import os
import re
import shutil
import socket
import ssl
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

VERSION = "1.0"
UA = "hegd-tul/" + VERSION
MANIFEST = "tools.json"
MAX_MANIFEST = 256 * 1024
MAX_FILE = 2 * 1024 * 1024
MAX_FILES = 20
CHECK_COOLDOWN = 600                       # the server is asked about updates at most once per 10 minutes
ALLOWED_EXT = (".py", ".txt")
FORBIDDEN_NAMES = {"tul.py", "main.py", "kiwi_core.py", "installed.json", "registry.json", "pypi_cache.json",
                   "tul_config.json", "tools_installed.json", "tools_updates.json"}

try:
    BASE = os.path.dirname(os.path.abspath(__file__))
except NameError:
    BASE = os.getcwd()
CONFIG = os.path.join(BASE, "tul_config.json")
STATE = os.path.join(BASE, "tools_installed.json")
UPDATES = os.path.join(BASE, "tools_updates.json")
BACKUPS = os.path.join(BASE, "tools_backup")

_now = time.time                           # a hook so tests can move the clock


class TulError(Exception):
    pass


class NoNetwork(TulError):
    pass


# --------------------------------------------------------------------- small helpers

def say(text):
    print(text)


def short(url):
    return url if len(url) <= 70 else url[:67] + "..."


def sha_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def write_atomic(path, data):
    tmp = path + ".tul-tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def copy_atomic(src, dst):
    with open(src, "rb") as f:
        write_atomic(dst, f.read())


def load_json(path, default, what):
    if not os.path.isfile(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        raise TulError("%s is damaged: fix it or delete it" % what)
    return data


def save_json(path, data):
    write_atomic(path, (json.dumps(data, indent=1, sort_keys=True) + "\n").encode("utf-8"))


def load_state():
    st = load_json(STATE, {}, "tools_installed.json")
    if not isinstance(st, dict) or not all(isinstance(r, dict) and isinstance(r.get("files"), dict) for r in st.values()):
        raise TulError("tools_installed.json is damaged: fix it or delete it")
    return st


def valid_tool_name(name):
    return isinstance(name, str) and re.match(r"^[a-z0-9][a-z0-9_-]{0,30}$", name) is not None


def safe_repo_path(path):
    if not isinstance(path, str) or not path or path.startswith("/") or "\\" in path or "\x00" in path:
        return False
    parts = path.split("/")
    return all(p and p not in (".", "..") and re.match(r"^[A-Za-z0-9._-]+$", p) for p in parts)


def safe_dest_name(name):
    if not isinstance(name, str) or not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", name):
        return False
    return name.endswith(ALLOWED_EXT) and name not in FORBIDDEN_NAMES


def valid_repo(url):
    p = urllib.parse.urlparse(url)
    if p.scheme != "https" or not p.hostname or p.query or p.fragment or re.search(r"\s", url):
        raise TulError("the repository address must be a plain https link, "
                       "e.g. https://raw.githubusercontent.com/USER/REPO/main")
    return url.rstrip("/")


# ------------------------------------------------------------------------ network

def _ctx():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def http_get(url, max_bytes):
    p = urllib.parse.urlparse(url)
    if p.scheme != "https":
        raise TulError("only https addresses are used")
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(req, timeout=30, context=_ctx()) as r:
            if urllib.parse.urlparse(r.geturl()).scheme != "https":
                raise TulError("the server tried to leave https: refused")
            data = r.read(max_bytes + 1)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise TulError("not found (HTTP 404): %s" % short(url))
        if e.code == 429:
            raise TulError("the server asks to slow down (HTTP 429): try again in a few minutes")
        raise TulError("the server answered HTTP %s" % e.code)
    except urllib.error.URLError as e:
        raise NoNetwork("no connection (%s). Check Wi-Fi or mobile data." % (e.reason,))
    except (socket.timeout, TimeoutError):
        raise NoNetwork("the server did not answer in time. Check the connection and try again.")
    if len(data) > max_bytes:
        raise TulError("a download is bigger than the limit (%d KB)" % (max_bytes // 1024))
    return data


# ------------------------------------------------------------------- repository

def get_repo():
    cfg = load_json(CONFIG, {}, "tul_config.json")
    repo = cfg.get("repo") if isinstance(cfg, dict) else None
    if not repo:
        raise TulError("no repository is set. Run:  tul repo https://raw.githubusercontent.com/USER/REPO/main")
    return repo


def fetch_manifest(repo):
    raw = http_get(repo + "/" + MANIFEST, MAX_MANIFEST)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise TulError("tools.json in the repository is not valid JSON")
    if not isinstance(data, dict) or data.get("format") != 1 or not isinstance(data.get("tools"), dict):
        raise TulError("tools.json has an unknown format (this tul understands format 1)")
    tools = {}
    for name, t in data["tools"].items():
        if not valid_tool_name(name) or not isinstance(t, dict):
            raise TulError("tools.json: bad tool name %r" % (str(name)[:30],))
        files = t.get("files")
        if not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES:
            raise TulError("tools.json: tool %s needs 1 to %d files" % (name, MAX_FILES))
        clean, seen = [], set()
        for f in files:
            if not isinstance(f, dict):
                raise TulError("tools.json: bad file entry in %s" % name)
            path, dname, sha = f.get("path"), f.get("name") or os.path.basename(str(f.get("path"))), str(f.get("sha256", "")).lower()
            if not safe_repo_path(path):
                raise TulError("tools.json: unsafe path in %s: %s" % (name, str(path)[:50]))
            if not safe_dest_name(dname):
                raise TulError("tools.json: file name not allowed in %s: %s" % (name, str(dname)[:50]))
            if not re.match(r"^[0-9a-f]{64}$", sha):
                raise TulError("tools.json: %s has no valid SHA-256" % dname)
            if dname in seen:
                raise TulError("tools.json: %s is listed twice in %s" % (dname, name))
            seen.add(dname)
            clean.append({"path": path, "name": dname, "sha256": sha})
        tools[name] = {"version": str(t.get("version", "?"))[:30], "description": str(t.get("description", ""))[:120],
                       "files": clean}
    return tools


def needs_update(rec, entry):
    """True when the repository has another version, or the same version with other file contents."""
    if rec.get("version") != entry["version"]:
        return True
    want = {f["name"]: f["sha256"] for f in entry["files"]}
    return want != rec.get("files", {})


def suggest(name, names):
    import difflib
    close = difflib.get_close_matches(name, list(names), n=2, cutoff=0.5)
    return (" Did you mean: %s?" % ", ".join(close)) if close else ""


def forget_update(tool):
    cache = load_json(UPDATES, {}, "tools_updates.json") if os.path.isfile(UPDATES) else {}
    if isinstance(cache, dict) and isinstance(cache.get("available"), dict) and tool in cache["available"]:
        del cache["available"][tool]
        try:
            save_json(UPDATES, cache)
        except OSError:
            pass


# ----------------------------------------------------- the transaction (install/update)

def compile_check(name, data):
    if name.endswith(".py"):
        try:
            compile(data, name, "exec")
        except (SyntaxError, ValueError) as e:
            raise TulError("%s does not compile (%s): the file is refused" % (name, getattr(e, "msg", e)))


def apply_tool(tool, entry, state, repo, force=False):
    """Download and check every file, then swap them in with a backup. Returns (old record, new record, notes)."""
    old = state.get(tool)
    # 1. everything is downloaded and checked before anything is touched
    new_files = {}
    for f in entry["files"]:
        data = http_get(repo + "/" + f["path"], MAX_FILE)
        if sha_bytes(data) != f["sha256"]:
            raise TulError("%s: SHA-256 differs from tools.json (the file changed after the list was made, "
                           "or the download is damaged)" % f["name"])
        compile_check(f["name"], data)
        new_files[f["name"]] = data

    # 2. conflicts
    others = {dn: t for t, r in state.items() if t != tool for dn in r.get("files", {})}
    old_names = set(old["files"]) if old else set()
    clash, notes = [], []
    for dn in new_files:
        target = os.path.join(BASE, dn)
        if dn in others:
            clash.append("%s belongs to the tool %s" % (dn, others[dn]))
        elif os.path.isdir(target):
            clash.append("%s is a folder" % dn)
        elif dn not in old_names and os.path.lexists(target) and not force:
            clash.append("%s already exists (use --force to take it over; the old file is kept in the backup)" % dn)
        elif dn in old_names and os.path.isfile(target) and sha_file(target) != old["files"].get(dn):
            notes.append("local changes in %s will be replaced (they stay in the backup)" % dn)
    if clash:
        raise TulError("conflict, nothing was changed: " + "; ".join(clash))
    obsolete = sorted(dn for dn in old_names if dn not in new_files)
    touched = sorted(set(new_files) | set(obsolete))

    # 3. a backup of everything that exists now
    bnew = os.path.join(BACKUPS, tool + ".new")
    shutil.rmtree(bnew, ignore_errors=True)
    os.makedirs(os.path.join(bnew, "files"))
    had = {}
    try:
        for dn in touched:
            target = os.path.join(BASE, dn)
            if os.path.isfile(target):
                shutil.copyfile(target, os.path.join(bnew, "files", dn))
                had[dn] = sha_file(target)
        meta = {"version": old.get("version") if old else None, "record": old, "had": had,
                "new": [dn for dn in touched if dn not in had], "when": time.strftime("%Y-%m-%d %H:%M", time.localtime(_now()))}
        save_json(os.path.join(bnew, "meta.json"), meta)

        # 4. the swap; any failure puts everything back
        try:
            for dn, data in new_files.items():
                write_atomic(os.path.join(BASE, dn), data)
                if sha_file(os.path.join(BASE, dn)) != sha_bytes(data):
                    raise TulError("%s was not written correctly" % dn)
            for dn in obsolete:
                try:
                    os.remove(os.path.join(BASE, dn))
                except FileNotFoundError:
                    pass
            record = {"version": entry["version"], "repo": repo, "files": {f["name"]: f["sha256"] for f in entry["files"]},
                      "installed": time.strftime("%Y-%m-%d %H:%M", time.localtime(_now()))}
            state2 = dict(state)
            state2[tool] = record
            save_json(STATE, state2)
        except BaseException:
            for dn in touched:
                target = os.path.join(BASE, dn)
                try:
                    if dn in had:
                        copy_atomic(os.path.join(bnew, "files", dn), target)
                    elif os.path.isfile(target):
                        os.remove(target)
                except OSError:
                    pass
            raise
    except BaseException:
        shutil.rmtree(bnew, ignore_errors=True)
        raise

    # 5. success: the new backup replaces the older one
    bold = os.path.join(BACKUPS, tool)
    shutil.rmtree(bold, ignore_errors=True)
    os.replace(bnew, bold)
    state[tool] = record
    forget_update(tool)
    return old, record, notes


# ------------------------------------------------------------------------ commands

def cmd_repo(args):
    if not args:
        cfg = load_json(CONFIG, {}, "tul_config.json")
        repo = cfg.get("repo") if isinstance(cfg, dict) else None
        say("[INFO] Repository: %s" % repo if repo else "[INFO] No repository is set. Run:  tul repo https://raw.githubusercontent.com/USER/REPO/main")
        return
    if len(args) != 1:
        say("usage: tul repo [https://raw.githubusercontent.com/USER/REPO/main]")
        return
    repo = valid_repo(args[0])
    save_json(CONFIG, {"repo": repo})
    try:
        os.remove(UPDATES)                                  # a cached answer belongs to the old repository
    except OSError:
        pass
    say("[SUCCESS] Repository set: %s" % repo)


def cmd_list(args):
    state = load_state()
    tools = None
    try:
        tools = fetch_manifest(get_repo())
    except NoNetwork as e:
        say("[WARNING] %s Showing only what is installed." % e)
    names = sorted(set(state) | set(tools or {}))
    if not names:
        say("[INFO] Nothing is installed and the repository has no tools.")
        return
    for n in names:
        rec, entry = state.get(n), (tools or {}).get(n)
        if rec and entry:
            status = "installed" + (" - update to %s available" % entry["version"] if needs_update(rec, entry) else ", up to date")
            ver = rec["version"]
        elif rec:
            status, ver = "installed" + ("" if tools is None else " (no longer in the repository)"), rec["version"]
        else:
            status, ver = "not installed", entry["version"]
        desc = ("  " + entry["description"]) if entry and entry["description"] else ""
        say("%-10s %-8s %s%s" % (n, ver, status, desc))


def cmd_install(args):
    force = "--force" in args
    names = [a for a in args if not a.startswith("--")]
    if len(names) != 1:
        say("usage: tul install NAME [--force]")
        return
    tool = names[0].lower()
    repo = get_repo()
    tools = fetch_manifest(repo)
    if tool not in tools:
        raise TulError("no tool '%s' in the repository.%s Available: %s" % (tool, suggest(tool, tools), ", ".join(sorted(tools)) or "-"))
    state = load_state()
    if tool in state and not force:
        say("[INFO] %s %s is already installed (update: tul update %s)." % (tool, state[tool]["version"], tool))
        return
    old, rec, notes = apply_tool(tool, tools[tool], state, repo, force)
    for n in notes:
        say("[WARNING] " + n)
    say("[SUCCESS] Installed %s %s (%s)." % (tool, rec["version"], ", ".join(sorted(rec["files"]))))
    say("[INFO] Trust the scripts you want to run without questions, e.g.  +tr %s" % sorted(rec["files"])[0])


def cmd_update(args):
    every = "--all" in args
    names = [a for a in args if not a.startswith("--")]
    if every == bool(names) or len(names) > 1:
        say("usage: tul update NAME   |   tul update --all")
        return
    state = load_state()
    if every and not state:
        say("[INFO] No tools are installed.")
        return
    targets = sorted(state) if every else [names[0].lower()]
    if not every and targets[0] not in state:
        raise TulError("%s is not installed.%s Install it with: tul install %s" % (targets[0], suggest(targets[0], state), targets[0]))
    repo = get_repo()
    tools = fetch_manifest(repo)
    done, failed = 0, []
    for tool in targets:
        entry = tools.get(tool)
        if entry is None:
            say("[WARNING] %s is no longer in the repository: nothing to update." % tool)
            continue
        if not needs_update(state[tool], entry):
            say("[INFO] %s %s is up to date." % (tool, state[tool]["version"]))
            continue
        try:
            old, rec, notes = apply_tool(tool, entry, state, repo)
        except NoNetwork:
            raise
        except TulError as e:
            say("[ERROR] %s: %s. Nothing was changed." % (tool, e))
            failed.append(tool)
            continue
        for n in notes:
            say("[WARNING] " + n)
        same = old["version"] == rec["version"]
        say("[SUCCESS] Updated %s %s -> %s%s." % (tool, old["version"], rec["version"], " (files changed)" if same else ""))
        say("[INFO] Something wrong with it?  tul rollback %s    Trust the new content again:  +tr <file>" % tool)
        done += 1
    if every:
        say("[INFO] Updated %d, failed %d%s." % (done, len(failed), (": " + ", ".join(failed)) if failed else ""))


def cmd_updates(args):
    state = load_state()
    repo = get_repo()
    cache = load_json(UPDATES, {}, "tools_updates.json") if os.path.isfile(UPDATES) else {}
    if not isinstance(cache, dict):
        cache = {}
    age = _now() - cache.get("checked", 0) if cache.get("repo") == repo and isinstance(cache.get("checked"), (int, float)) else None
    if age is not None and 0 <= age < CHECK_COOLDOWN:
        available = cache.get("available", {})
        note = " (checked %d s ago; the server is asked at most once per %d min)" % (int(age), CHECK_COOLDOWN // 60)
    else:
        tools = fetch_manifest(repo)
        available = {}
        for tool, rec in state.items():
            entry = tools.get(tool)
            if entry and needs_update(rec, entry):
                changed = sorted(f["name"] for f in entry["files"] if rec["files"].get(f["name"]) != f["sha256"])
                available[tool] = {"from": rec["version"], "to": entry["version"], "files": changed}
        save_json(UPDATES, {"checked": _now(), "repo": repo, "available": available})
        note = ""
    if not state:
        say("[INFO] No tools are installed.%s" % note)
        return
    if not available:
        say("[INFO] All installed tools are up to date.%s" % note)
        return
    for tool in sorted(available):
        a = available[tool]
        extra = " (files changed)" if a["from"] == a["to"] else ""
        say("%s %s -> %s%s  [%s]" % (tool, a["from"], a["to"], extra, ", ".join(a.get("files", []))))
    say("[INFO] Update: tul update NAME   or all: tul update --all%s" % note)


def cmd_rollback(args):
    if len(args) != 1:
        say("usage: tul rollback NAME")
        return
    tool = args[0].lower()
    bdir = os.path.join(BACKUPS, tool)
    meta = load_json(os.path.join(bdir, "meta.json"), None, "the backup of %s" % tool)
    if meta is None:
        raise TulError("no earlier version of %s is saved." % tool)
    state = load_state()
    had, new = meta.get("had", {}), meta.get("new", [])
    if not isinstance(had, dict) or not isinstance(new, list):
        raise TulError("the backup of %s is damaged" % tool)
    for dn, sha in had.items():                               # check the backup BEFORE touching anything
        src = os.path.join(bdir, "files", dn)
        if not safe_dest_name(dn) or not os.path.isfile(src) or sha_file(src) != sha:
            raise TulError("the backup of %s is damaged (%s): nothing was changed" % (tool, dn))
    current = state.get(tool, {}).get("files", {})
    for dn, sha in had.items():
        copy_atomic(os.path.join(bdir, "files", dn), os.path.join(BASE, dn))
    for dn in new:
        if dn in current and safe_dest_name(dn):
            try:
                os.remove(os.path.join(BASE, dn))
            except OSError:
                pass
    if meta.get("record") is None:
        state.pop(tool, None)
    else:
        state[tool] = meta["record"]
    save_json(STATE, state)
    shutil.rmtree(bdir, ignore_errors=True)
    try:
        os.remove(UPDATES)
    except OSError:
        pass
    if meta.get("record") is None:
        say("[SUCCESS] Rolled back: %s was not installed before, so it is removed." % tool)
    else:
        say("[SUCCESS] Rolled back %s to version %s." % (tool, meta.get("version")))
    say("[INFO] The older scripts have other content: trust them again with +tr <file> if you want.")


COMMANDS = {"repo": cmd_repo, "list": cmd_list, "install": cmd_install, "update": cmd_update,
            "updates": cmd_updates, "rollback": cmd_rollback}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in COMMANDS:
        say("tul %s - the tools of Hegd" % VERSION)
        say("commands: repo [url] | list | install NAME [--force] | updates | update NAME|--all | rollback NAME")
        return
    try:
        COMMANDS[argv[0]](argv[1:])
    except TulError as e:
        say("[ERROR] " + str(e))
    except Exception:
        say("[CRITICAL] tul crashed:")
        say(traceback.format_exc()[-800:])


if __name__ != "tul":
    main()
