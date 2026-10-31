"""gitkit v1.0 - GitHub from the phone, without the git program: clone, status, pull, push, log, ls.

    py gitkit.py token                     check the token (github_token.txt next to this file)
    py gitkit.py clone OWNER/NAME [FOLDER] [--ref BRANCH]     download the current state into repos/FOLDER
    py gitkit.py list                      cloned repositories
    py gitkit.py status [REPO]             what you changed (no network)
    py gitkit.py pull [REPO] [--force]     bring in what others changed
    py gitkit.py push [REPO] -m "text" [--dry] [--allow-secrets]      send your changes as ONE commit
    py gitkit.py log OWNER/NAME|REPO [-n 10] [--ref BRANCH]           last commits
    py gitkit.py ls OWNER/NAME [PATH] [--ref BRANCH]                  files of a repository

How it works: GitHub's web API is used instead of git. A clone is a snapshot (no history): the commit it
was made from and the git hash of every file are remembered in REPO/.gitkit/state.json. So  status  can tell
what you changed without the network, and  push  makes one commit on top of exactly that commit; if the
branch moved in the meantime, push refuses and you run  pull  first (no merges, no surprises).

Safety:
  - the token lives only in github_token.txt (or the GITHUB_TOKEN variable); it is never printed, never put
    into a link, never sent to any address except api.github.com;
  - push refuses files that look like secrets (token/secret/password in the name, .env, keys, or a GitHub/Telegram/
    AWS token inside the text) unless you say --allow-secrets;
  - everything downloaded is checked against the git hashes GitHub lists; unsafe paths are refused;
  - pull changes nothing when anything fails and keeps your versions of files that changed on both sides.
Make the token on GitHub: Settings -> Developer settings -> Fine-grained tokens: ONE repository,
Permissions -> Contents: Read and write.
"""
import base64
import fnmatch
import hashlib
import io
import json
import os
import re
import shutil
import socket
import ssl
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import zipfile

VERSION = "1.0"
UA = "hegd-gitkit/" + VERSION
API = "https://api.github.com"
API_VERSION = "2022-11-28"
MAX_JSON = 20 * 1024 * 1024
MAX_ZIP = 50 * 1024 * 1024
MAX_REPO_FILES = 3000
MAX_PULL_FILE = 20 * 1024 * 1024
MAX_SCAN_FILE = 50 * 1024 * 1024
MAX_PUSH_FILE = 5 * 1024 * 1024
MAX_PUSH_TOTAL = 25 * 1024 * 1024
MAX_PUSH_FILES = 200
ALWAYS_IGNORE_DIRS = {".gitkit", ".git", "__pycache__"}
IGNORE_SUFFIXES = (".pyc", ".tul-tmp", ".gk-tmp", ".mine")
TOKEN_FILE = "github_token.txt"

try:
    BASE = os.path.dirname(os.path.abspath(__file__))
except NameError:
    BASE = os.getcwd()
REPOS = os.path.join(BASE, "repos")


class GkError(Exception):
    pass


class NoNetwork(GkError):
    pass


class RateLimited(GkError):
    pass


def say(text):
    print(text)


# ------------------------------------------------------------------- small helpers

def blob_sha_bytes(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def blob_sha_file(path, size):
    h = hashlib.sha1(b"blob %d\0" % size)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def write_atomic(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".gk-tmp"
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


def load_json(path, what):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        raise GkError("%s is damaged: delete it and clone again" % what)


def save_json(path, data):
    write_atomic(path, (json.dumps(data, indent=1, sort_keys=True) + "\n").encode("utf-8"))


def parse_repo(text):
    m = re.match(r"^(?:https://github\.com/)?([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9._-]{1,100}?)(?:\.git)?/?$", text.strip())
    if not m or set(m.group(2)) <= {"."}:
        raise GkError("use OWNER/NAME, for example  resolik/Hegd")
    return m.group(1), m.group(2)


def valid_ref(ref):
    return (isinstance(ref, str) and re.match(r"^[A-Za-z0-9._/-]{1,100}$", ref) is not None and ".." not in ref
            and not ref.startswith(("/", "-")) and not ref.endswith(("/", ".lock")))


def safe_rel(path):
    if not isinstance(path, str) or not path or len(path) > 400 or "\\" in path or "\x00" in path or path.startswith("/"):
        return False
    parts = path.split("/")
    return all(p and p not in (".", "..", ".git", ".gitkit") for p in parts)


def q(ref):
    return urllib.parse.quote(ref, safe="/")


def short(sha):
    return str(sha)[:7]


# ---------------------------------------------------------------------------- token

_TOKEN = [None]


def get_token():
    if _TOKEN[0] is not None:
        return _TOKEN[0]
    tok = os.environ.get("GITHUB_TOKEN", "").strip()
    if not tok:
        try:
            with open(os.path.join(BASE, TOKEN_FILE), encoding="utf-8") as f:
                tok = f.read().strip()
        except OSError:
            tok = ""
    if tok and not re.match(r"^[A-Za-z0-9_-]{20,255}$", tok):
        raise GkError("%s does not look like a GitHub token (one line, no spaces)" % TOKEN_FILE)
    _TOKEN[0] = tok
    return tok


def need_token():
    tok = get_token()
    if not tok:
        raise GkError("this needs a token: put it into %s next to gitkit.py (run: gitkit token)" % TOKEN_FILE)
    return tok


# ----------------------------------------------------------------------------- HTTP

def _ctx():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is never followed by itself: urllib would resend the Authorization header to the new address."""

    def redirect_request(self, *args, **kwargs):
        return None


def _open(url, headers, data=None, method=None):
    opener = urllib.request.build_opener(_NoRedirect(), urllib.request.HTTPSHandler(context=_ctx()))
    return opener.open(urllib.request.Request(url, data=data, headers=headers, method=method), timeout=30)


def _map_http_error(e, has_token):
    code = e.code
    try:
        msg = str(json.loads(e.read(65536).decode("utf-8", "replace")).get("message", ""))
    except Exception:
        msg = ""
    if code == 401:
        return GkError("GitHub does not accept the token (401): it is wrong, expired or revoked. Make a new one (gitkit token).")
    if code in (403, 429):
        remaining = e.headers.get("X-RateLimit-Remaining") if e.headers else None
        if remaining == "0" or code == 429 or "rate limit" in msg.lower():
            wait = ""
            try:
                wait = " It resets in about %d min." % max(1, (int(e.headers.get("X-RateLimit-Reset")) - int(time.time()) + 59) // 60)
            except (TypeError, ValueError):
                pass
            return RateLimited("GitHub request limit reached.%s%s" % (wait, "" if has_token else " With a token the limit is 80 times higher."))
        return GkError("GitHub refused (403): the token has no right for this. It needs 'Contents: Read and write' on THIS repository. (%s)" % msg[:80])
    if code == 404:
        return GkError("not found (404). Check the name, and that the token can see this repository.")
    if code == 409:
        return GkError("conflict (409): %s" % (msg[:120] or "the repository is empty or the branch changed"))
    if code == 422:
        return GkError("GitHub rejected the data (422): %s" % (msg[:120] or "not accepted"))
    return GkError("GitHub answered HTTP %d" % code)


def _headers():
    h = {"User-Agent": UA, "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": API_VERSION}
    tok = get_token()
    if tok:
        h["Authorization"] = "Bearer " + tok
    return h


def api(method, path, body=None):
    headers = _headers()
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    try:
        with _open(API + path, headers, data, method) as r:
            raw = r.read(MAX_JSON + 1)
    except urllib.error.HTTPError as e:
        raise _map_http_error(e, bool(get_token()))
    except urllib.error.URLError as e:
        raise NoNetwork("no connection to GitHub (%s). Check Wi-Fi or mobile data." % (e.reason,))
    except (socket.timeout, TimeoutError):
        raise NoNetwork("GitHub did not answer in time. Check the connection and try again.")
    if len(raw) > MAX_JSON:
        raise GkError("an answer of GitHub is too big")
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        raise GkError("GitHub answered something that is not JSON")


def fetch_zip(owner, repo, sha):
    """The archive of one commit. GitHub redirects to codeload.github.com: the redirect is followed here,
    by hand, and WITHOUT the token."""
    url = "%s/repos/%s/%s/zipball/%s" % (API, owner, repo, sha)
    has_token = bool(get_token())
    data, location = None, None
    try:
        with _open(url, _headers()) as r:
            data = r.read(MAX_ZIP + 1)
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308):
            location = e.headers.get("Location")
        else:
            raise _map_http_error(e, has_token)
    except urllib.error.URLError as e:
        raise NoNetwork("no connection to GitHub (%s). Check Wi-Fi or mobile data." % (e.reason,))
    except (socket.timeout, TimeoutError):
        raise NoNetwork("GitHub did not answer in time.")
    if location:
        p = urllib.parse.urlparse(location)
        if p.scheme != "https" or p.hostname != "codeload.github.com":
            raise GkError("GitHub sent the archive from an unexpected address: refused")
        try:
            with _open(location, {"User-Agent": UA}) as r:
                data = r.read(MAX_ZIP + 1)
        except urllib.error.HTTPError as e:
            raise _map_http_error(e, has_token)
        except urllib.error.URLError as e:
            raise NoNetwork("no connection (%s). Check Wi-Fi or mobile data." % (e.reason,))
        except (socket.timeout, TimeoutError):
            raise NoNetwork("the download did not finish in time.")
    if data is None or len(data) > MAX_ZIP:
        raise GkError("the archive is missing or bigger than %d MB" % (MAX_ZIP // 1048576))
    return data


# ------------------------------------------------------------------ remote snapshot

def remote_head(owner, repo, ref):
    c = api("GET", "/repos/%s/%s/commits/%s" % (owner, repo, q(ref)))
    try:
        return c["sha"], c["commit"]["tree"]["sha"]
    except (KeyError, TypeError):
        raise GkError("GitHub's answer about the commit is not understood")


def remote_tree(owner, repo, tree_sha):
    t = api("GET", "/repos/%s/%s/git/trees/%s?recursive=1" % (owner, repo, tree_sha))
    if not isinstance(t, dict) or t.get("truncated"):
        raise GkError("this repository is too big for gitkit")
    files, lower = {}, set()
    for e in t.get("tree", []):
        if e.get("type") != "blob" or e.get("mode") not in ("100644", "100755"):
            continue                                              # folders, symlinks and submodules are left alone
        path = e.get("path")
        if not safe_rel(path):
            raise GkError("the repository has a path that is not safe here: %s" % str(path)[:60])
        if path.lower() in lower:
            raise GkError("two files differ only by letter case (%s): a phone cannot hold both" % path[:60])
        lower.add(path.lower())
        files[path] = {"sha": e["sha"], "mode": e["mode"]}
    if len(files) > MAX_REPO_FILES:
        raise GkError("this repository has more than %d files: too big for gitkit" % MAX_REPO_FILES)
    return files


def snapshot(owner, repo, sha, wanted):
    """Download the archive of commit `sha`; return {path: bytes} for the files in `wanted`,
    each checked against the git hash GitHub listed."""
    if not wanted:
        return {}
    data = fetch_zip(owner, repo, sha)
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise GkError("the archive is not a valid zip")
    out = {}
    with z:
        for info in z.infolist():
            if info.filename.endswith("/"):
                continue
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                continue
            parts = info.filename.split("/", 1)
            rel = parts[1] if len(parts) == 2 else ""
            if rel not in wanted:
                continue
            if info.file_size > MAX_PULL_FILE:
                raise GkError("%s is bigger than %d MB" % (rel[:60], MAX_PULL_FILE // 1048576))
            content = z.read(info)
            if blob_sha_bytes(content) != wanted[rel]["sha"]:
                raise GkError("integrity check failed for %s: the download does not match GitHub's hash" % rel[:60])
            out[rel] = content
    missing = sorted(set(wanted) - set(out))
    if missing:
        raise GkError("the archive misses files: %s" % ", ".join(missing[:3]))
    return out


# ---------------------------------------------------------------- local repository

def state_path(folder):
    return os.path.join(folder, ".gitkit", "state.json")


def load_state(folder):
    st = load_json(state_path(folder), "the state of this repository (.gitkit/state.json)")
    if not isinstance(st, dict) or not all(k in st for k in ("repo", "ref", "commit", "tree", "files")) or not isinstance(st["files"], dict):
        raise GkError("the state of this repository is damaged: delete the folder and clone again")
    return st


def cloned_repos():
    out = []
    if os.path.isdir(REPOS):
        for n in sorted(os.listdir(REPOS)):
            if os.path.isfile(state_path(os.path.join(REPOS, n))):
                out.append(n)
    return out


def find_repo(arg):
    if arg:
        for cand in (os.path.join(REPOS, arg), arg if os.path.isabs(arg) else os.path.join(BASE, arg)):
            if os.path.isfile(state_path(cand)):
                return os.path.realpath(cand)
        raise GkError("no cloned repository '%s'. Cloned: %s" % (arg[:40], ", ".join(cloned_repos()) or "none"))
    here = os.getcwd()
    if os.path.isfile(state_path(here)):
        return here
    names = cloned_repos()
    if len(names) == 1:
        return os.path.join(REPOS, names[0])
    if not names:
        raise GkError("nothing is cloned yet. Start with:  gitkit clone OWNER/NAME")
    raise GkError("several repositories are cloned, say which: %s" % ", ".join(names))


def load_ignore(folder):
    try:
        with open(os.path.join(folder, ".gitignore"), encoding="utf-8", errors="replace") as f:
            return [ln.strip() for ln in f.read().splitlines() if ln.strip() and not ln.startswith(("#", "!"))]
    except OSError:
        return []


def ignored(rel, patterns):
    """A simple subset of .gitignore: names, *, folders with a trailing /, paths with a /. Tracked files never count."""
    parts = rel.split("/")
    if any(p in ALWAYS_IGNORE_DIRS for p in parts[:-1]) or parts[-1].endswith(IGNORE_SUFFIXES):
        return True
    for pat in patterns:
        dir_only = pat.endswith("/")
        pat = pat.rstrip("/")
        if not pat:
            continue
        if "/" in pat.lstrip("/"):
            pat = pat.lstrip("/")
            if fnmatch.fnmatch(rel, pat) or rel.startswith(pat + "/"):
                return True
        else:
            pat = pat.lstrip("/")
            names = parts[:-1] if dir_only else parts
            if any(fnmatch.fnmatch(n, pat) for n in names):
                return True
    return False


def scan(folder, tracked, patterns):
    """-> ({path: git hash}, number of files skipped because they are too big)"""
    out, big = {}, 0
    for dp, dn, fn in os.walk(folder):
        rel_dir = os.path.relpath(dp, folder).replace(os.sep, "/")
        rel_dir = "" if rel_dir == "." else rel_dir
        dn[:] = sorted(d for d in dn if d not in ALWAYS_IGNORE_DIRS and not os.path.islink(os.path.join(dp, d)))
        for f in sorted(fn):
            path = os.path.join(dp, f)
            if os.path.islink(path) or not os.path.isfile(path):
                continue
            rel = (rel_dir + "/" + f) if rel_dir else f
            if rel not in tracked and ignored(rel, patterns):
                continue
            try:
                size = os.path.getsize(path)
                if size > MAX_SCAN_FILE:
                    big += 1
                    continue
                out[rel] = blob_sha_file(path, size)
            except OSError:
                continue
    return out, big


def changes(base_files, local):
    modified = sorted(p for p in local if p in base_files and local[p] != base_files[p]["sha"])
    added = sorted(p for p in local if p not in base_files)
    deleted = sorted(p for p in base_files if p not in local)
    return modified, added, deleted


SECRET_NAME = re.compile(r"(^|[._-])(tokens?|secrets?|passwords?|passwd|credentials?)([._-]|$)|^\.env|\.(pem|key|p12|pfx)$|^id_(rsa|ed25519|ecdsa)", re.I)
SECRET_TEXT = [re.compile(p) for p in (r"gh[pousr]_[A-Za-z0-9]{36,}", r"github_pat_[A-Za-z0-9_]{50,}",
                                       r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b", r"AKIA[0-9A-Z]{16}",
                                       r"-----BEGIN [A-Z ]*PRIVATE KEY-----")]


def find_secrets(folder, paths):
    """-> list of (path, why). The VALUE is never reported."""
    found = []
    for p in paths:
        if SECRET_NAME.search(os.path.basename(p)):
            found.append((p, "the name looks like a secret"))
            continue
        try:
            with open(os.path.join(folder, *p.split("/")), "rb") as f:
                text = f.read(MAX_PUSH_FILE).decode("latin-1")
        except OSError:
            continue
        if any(rx.search(text) for rx in SECRET_TEXT):
            found.append((p, "the text contains something that looks like a token or a private key"))
    return found


def prune_empty(folder):
    for dp, dn, fn in os.walk(folder, topdown=False):
        if dp != folder and ".gitkit" not in dp.split(os.sep) and not os.listdir(dp):
            try:
                os.rmdir(dp)
            except OSError:
                pass


def parse_args(args, valued=(), flags=()):
    pos, opts, flg = [], {}, set()
    i = 0
    while i < len(args):
        a = args[i]
        if a in valued:
            if i + 1 >= len(args):
                raise GkError("%s needs a value" % a)
            opts[a] = args[i + 1]
            i += 2
            continue
        if a in flags:
            flg.add(a)
        elif a.startswith("-") and len(a) > 1:
            raise GkError("unknown option %s" % a[:20])
        else:
            pos.append(a)
        i += 1
    return pos, opts, flg


# ------------------------------------------------------------------------- commands

def cmd_token(args):
    tok = get_token()
    if not tok:
        say("[INFO] No token yet. Public repositories can be read without one (60 requests per hour).")
        say("[INFO] To push: GitHub -> Settings -> Developer settings -> Fine-grained tokens -> Generate.")
        say("[INFO] Choose ONE repository, Permissions -> Contents: Read and write. Put the token into %s "
            "(one line) next to gitkit.py, then run  gitkit token  again." % TOKEN_FILE)
        return
    me = api("GET", "/user")
    say("[SUCCESS] The token works: logged in as %s." % str(me.get("login", "?"))[:40])


def cmd_clone(args):
    pos, opts, _ = parse_args(args, valued=("--ref",))
    if not 1 <= len(pos) <= 2:
        say("usage: gitkit clone OWNER/NAME [FOLDER] [--ref BRANCH]")
        return
    owner, name = parse_repo(pos[0])
    folder_name = pos[1] if len(pos) == 2 else name
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", folder_name) or set(folder_name) <= {"."}:
        raise GkError("the folder name must be a plain name (letters, digits, . _ -)")
    target = os.path.join(REPOS, folder_name)
    if os.path.exists(target) and (not os.path.isdir(target) or os.listdir(target)):
        raise GkError("%s already exists and is not empty" % folder_name)
    ref = opts.get("--ref")
    if ref is not None and not valid_ref(ref):
        raise GkError("bad branch name")
    info = api("GET", "/repos/%s/%s" % (owner, name))
    is_branch = True
    if ref is None:
        ref = str(info.get("default_branch") or "main")
        if not valid_ref(ref):
            raise GkError("GitHub reports a branch name that is not accepted here")
    else:
        try:
            api("GET", "/repos/%s/%s/git/ref/heads/%s" % (owner, name, q(ref)))
        except RateLimited:
            raise
        except NoNetwork:
            raise
        except GkError:
            is_branch = False                                     # a tag or a commit: readable, not pushable
    say("[INFO] Asking GitHub about %s/%s (%s)..." % (owner, name, ref))
    sha, tree = remote_head(owner, name, ref)
    files = remote_tree(owner, name, tree)
    say("[INFO] Downloading %d files..." % len(files))
    snap = snapshot(owner, name, sha, files)
    os.makedirs(REPOS, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix=".gk-clone-", dir=REPOS)
    try:
        for p, content in snap.items():
            write_atomic(os.path.join(tmp, *p.split("/")), content)
        save_json(state_path(tmp), {"repo": "%s/%s" % (owner, name), "ref": ref, "branch": is_branch, "commit": sha,
                                    "tree": tree, "files": files, "cloned": time.strftime("%Y-%m-%d %H:%M")})
        if os.path.isdir(target):
            os.rmdir(target)
        os.replace(tmp, target)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    say("[SUCCESS] Cloned %s/%s (%s @ %s): %d files into repos/%s" % (owner, name, ref, short(sha), len(files), folder_name))
    say("[INFO] This is a snapshot without history. Edit the files, then:  gitkit status  and  gitkit push -m \"text\"")


def cmd_list(args):
    names = cloned_repos()
    if not names:
        say("[INFO] Nothing is cloned yet.")
        return
    for n in names:
        folder = os.path.join(REPOS, n)
        try:
            st = load_state(folder)
            local, _ = scan(folder, st["files"], load_ignore(folder))
            m, a, d = changes(st["files"], local)
            note = "%d change(s)" % (len(m) + len(a) + len(d)) if (m or a or d) else "clean"
            say("%-14s %-24s %s @ %s  %s" % (n, st["repo"], st["ref"], short(st["commit"]), note))
        except GkError as e:
            say("%-14s [ERROR] %s" % (n, e))


def cmd_status(args):
    pos, _, _ = parse_args(args)
    if len(pos) > 1:
        say("usage: gitkit status [REPO]")
        return
    folder = find_repo(pos[0] if pos else None)
    st = load_state(folder)
    local, big = scan(folder, st["files"], load_ignore(folder))
    m, a, d = changes(st["files"], local)
    say("%s: %s, %s @ %s" % (os.path.basename(folder), st["repo"], st["ref"], short(st["commit"])))
    for p in m:
        say("  M %s" % p)
    for p in a:
        say("  A %s" % p)
    for p in d:
        say("  D %s" % p)
    if not (m or a or d):
        say("[INFO] Nothing changed.")
    else:
        say("[INFO] %d modified, %d added, %d deleted. Send them: gitkit push -m \"text\"" % (len(m), len(a), len(d)))
    if big:
        say("[WARNING] %d file(s) over %d MB are not tracked." % (big, MAX_SCAN_FILE // 1048576))


def cmd_pull(args):
    pos, _, flags = parse_args(args, flags=("--force",))
    if len(pos) > 1:
        say("usage: gitkit pull [REPO] [--force]")
        return
    force = "--force" in flags
    folder = find_repo(pos[0] if pos else None)
    st = load_state(folder)
    owner, name = st["repo"].split("/", 1)
    sha, tree = remote_head(owner, name, st["ref"])
    local, _ = scan(folder, st["files"], load_ignore(folder))
    if sha == st["commit"]:
        say("[INFO] Already up to date (%s @ %s)." % (st["ref"], short(sha)))
        return
    new = remote_tree(owner, name, tree)
    base = st["files"]
    to_write, deletions, conflicts = [], [], []
    for p in sorted(new):
        if p in base and base[p]["sha"] == new[p]["sha"]:
            continue                                              # the same on GitHub as at the clone
        lsha = local.get(p)
        if lsha == new[p]["sha"]:
            continue                                              # you already have exactly this
        if lsha == (base[p]["sha"] if p in base else None):
            to_write.append(p)                                    # you did not touch it
        else:
            conflicts.append(p)                                   # changed on both sides
    for p in sorted(set(base) - set(new)):
        lsha = local.get(p)
        if lsha is None:
            continue
        (deletions if lsha == base[p]["sha"] else conflicts).append(p)
    if conflicts and not force:
        say("[ERROR] %d file(s) changed both on GitHub and here: %s%s" % (len(conflicts), ", ".join(conflicts[:5]), " ..." if len(conflicts) > 5 else ""))
        say("[INFO] Nothing was changed. Options: copy your edits somewhere and run  gitkit pull --force  "
            "(GitHub's version wins, yours is kept as NAME.mine).")
        return
    take = sorted(set(to_write) | {p for p in conflicts if p in new})
    kill = sorted(set(deletions) | {p for p in conflicts if p not in new})
    snap = snapshot(owner, name, sha, {p: new[p] for p in take})

    bdir = os.path.join(folder, ".gitkit", "backup")
    shutil.rmtree(bdir, ignore_errors=True)
    os.makedirs(bdir)
    had, made = [], []
    try:
        for p in sorted(set(take) | set(kill)):
            target = os.path.join(folder, *p.split("/"))
            if os.path.isfile(target):
                bp = os.path.join(bdir, str(len(had)))
                shutil.copyfile(target, bp)
                had.append((p, bp))
        for p in conflicts:                                       # your version stays next to it
            target = os.path.join(folder, *p.split("/"))
            if os.path.isfile(target):
                shutil.copyfile(target, target + ".mine")
                made.append(target + ".mine")
        for p in take:
            target = os.path.join(folder, *p.split("/"))
            if not os.path.exists(target):
                made.append(target)
            write_atomic(target, snap[p])
        for p in kill:
            try:
                os.remove(os.path.join(folder, *p.split("/")))
            except FileNotFoundError:
                pass
        st2 = dict(st)
        st2.update({"commit": sha, "tree": tree, "files": new})
        save_json(state_path(folder), st2)
    except BaseException:
        for target in made:
            try:
                os.remove(target)
            except OSError:
                pass
        for p, bp in had:
            try:
                write_atomic(os.path.join(folder, *p.split("/")), open(bp, "rb").read())
            except OSError:
                pass
        shutil.rmtree(bdir, ignore_errors=True)
        raise
    shutil.rmtree(bdir, ignore_errors=True)
    prune_empty(folder)
    say("[SUCCESS] Pulled %s @ %s: %d updated, %d removed." % (st["ref"], short(sha), len(take), len(kill)))
    if conflicts:
        say("[WARNING] %d file(s) took GitHub's version; yours are saved as NAME.mine (they are not pushed)." % len(conflicts))


def cmd_push(args):
    pos, opts, flags = parse_args(args, valued=("-m",), flags=("--dry", "--allow-secrets"))
    if len(pos) > 1:
        say('usage: gitkit push [REPO] -m "text" [--dry] [--allow-secrets]')
        return
    folder = find_repo(pos[0] if pos else None)
    st = load_state(folder)
    if not st.get("branch", True):
        raise GkError("%s is a tag or a commit, not a branch: it cannot be pushed to" % st["ref"])
    owner, name = st["repo"].split("/", 1)
    local, _ = scan(folder, st["files"], load_ignore(folder))
    m, a, d = changes(st["files"], local)
    if not (m or a or d):
        say("[INFO] Nothing to push.")
        return
    send = m + a
    if "--allow-secrets" not in flags:
        bad = find_secrets(folder, send)
        if bad:
            for p, why in bad[:5]:
                say("[ERROR] %s: %s" % (p, why))
            say("[INFO] Nothing was sent. Remove it from the folder, or list it in .gitignore, or, if you are sure, add --allow-secrets.")
            return
    if len(send) + len(d) > MAX_PUSH_FILES:
        raise GkError("more than %d files in one push: split it" % MAX_PUSH_FILES)
    total = 0
    for p in send:
        size = os.path.getsize(os.path.join(folder, *p.split("/")))
        if size > MAX_PUSH_FILE:
            raise GkError("%s is bigger than %d MB: too big to push from here" % (p[:60], MAX_PUSH_FILE // 1048576))
        total += size
    if total > MAX_PUSH_TOTAL:
        raise GkError("more than %d MB in one push: split it" % (MAX_PUSH_TOTAL // 1048576))
    message = (opts.get("-m") or "").strip() or "Update from Hegd (%d files)" % (len(send) + len(d))
    if len(message) > 500:
        raise GkError("the commit message is too long")
    if "--dry" in flags:
        for p in m:
            say("  M %s" % p)
        for p in a:
            say("  A %s" % p)
        for p in d:
            say("  D %s" % p)
        say("[INFO] Dry run: would push %d change(s) to %s (%s) with the message: %s" % (len(m) + len(a) + len(d), st["repo"], st["ref"], message[:60]))
        return
    need_token()

    head = api("GET", "/repos/%s/%s/git/ref/heads/%s" % (owner, name, q(st["ref"])))
    head_sha = (head.get("object") or {}).get("sha")
    if head_sha != st["commit"]:
        say("[ERROR] The branch moved on GitHub since your clone (%s -> %s)." % (short(st["commit"]), short(head_sha)))
        say("[INFO] Nothing was sent. Run  gitkit pull  first, then push again.")
        return
    entries, new_sha = [], {}
    for p in send:
        with open(os.path.join(folder, *p.split("/")), "rb") as f:
            content = f.read()
        blob = api("POST", "/repos/%s/%s/git/blobs" % (owner, name), {"content": base64.b64encode(content).decode("ascii"), "encoding": "base64"})
        if blob.get("sha") != local[p]:
            raise GkError("GitHub stored %s with another hash: the file may have changed while pushing. Nothing was pushed." % p[:60])
        mode = st["files"][p]["mode"] if p in st["files"] else "100644"
        entries.append({"path": p, "mode": mode, "type": "blob", "sha": blob["sha"]})
        new_sha[p] = {"sha": blob["sha"], "mode": mode}
    for p in d:
        entries.append({"path": p, "mode": "100644", "type": "blob", "sha": None})
    t = api("POST", "/repos/%s/%s/git/trees" % (owner, name), {"base_tree": st["tree"], "tree": entries})
    c = api("POST", "/repos/%s/%s/git/commits" % (owner, name), {"message": message, "tree": t["sha"], "parents": [st["commit"]]})
    api("PATCH", "/repos/%s/%s/git/refs/heads/%s" % (owner, name, q(st["ref"])), {"sha": c["sha"], "force": False})
    files = dict(st["files"])
    for p in d:
        files.pop(p, None)
    files.update(new_sha)
    st2 = dict(st)
    st2.update({"commit": c["sha"], "tree": t["sha"], "files": files})
    try:
        save_json(state_path(folder), st2)
    except OSError:
        say("[WARNING] Pushed, but the local state could not be saved: run  gitkit pull  before the next push.")
    say("[SUCCESS] Pushed %d change(s) to %s (%s): %s \"%s\"" % (len(send) + len(d), st["repo"], st["ref"], short(c["sha"]), message[:50]))
    say("[INFO] https://github.com/%s/commit/%s" % (st["repo"], c["sha"]))


def cmd_log(args):
    pos, opts, _ = parse_args(args, valued=("-n", "--ref"))
    if len(pos) > 1:
        say("usage: gitkit log [OWNER/NAME|REPO] [-n 10] [--ref BRANCH]")
        return
    count = opts.get("-n", "10")
    if not count.isdigit() or not 1 <= int(count) <= 30:
        raise GkError("-n: a number from 1 to 30")
    ref = opts.get("--ref")
    if ref is not None and not valid_ref(ref):
        raise GkError("bad branch name")
    target = pos[0] if pos else None
    if target and "/" in target:
        owner, name = parse_repo(target)
    else:
        st = load_state(find_repo(target))
        owner, name = st["repo"].split("/", 1)
        ref = ref or st["ref"]
    path = "/repos/%s/%s/commits?per_page=%s" % (owner, name, count) + (("&sha=" + urllib.parse.quote(ref, safe="")) if ref else "")
    items = api("GET", path)
    if not isinstance(items, list) or not items:
        say("[INFO] No commits.")
        return
    for it in items:
        c = it.get("commit", {})
        who = str((c.get("author") or {}).get("name", "?"))[:16]
        day = str((c.get("author") or {}).get("date", ""))[:10]
        first = str(c.get("message", "")).splitlines()[0][:60] if c.get("message") else ""
        say("%s  %s  %-16s %s" % (short(it.get("sha", "")), day, who, first))


def cmd_ls(args):
    pos, opts, _ = parse_args(args, valued=("--ref",))
    if not 1 <= len(pos) <= 2:
        say("usage: gitkit ls OWNER/NAME [PATH] [--ref BRANCH]")
        return
    owner, name = parse_repo(pos[0])
    path = pos[1].strip("/") if len(pos) == 2 else ""
    if path and not safe_rel(path):
        raise GkError("bad path")
    ref = opts.get("--ref")
    if ref is not None and not valid_ref(ref):
        raise GkError("bad branch name")
    url = "/repos/%s/%s/contents/%s" % (owner, name, urllib.parse.quote(path, safe="/")) + (("?ref=" + urllib.parse.quote(ref, safe="")) if ref else "")
    items = api("GET", url)
    if isinstance(items, dict):
        items = [items]
    items = sorted(items, key=lambda e: (e.get("type") != "dir", str(e.get("name", "")).lower()))
    for e in items:
        if e.get("type") == "dir":
            say("%s/" % e.get("name", "?"))
        else:
            say("%-30s %d B" % (str(e.get("name", "?"))[:30], int(e.get("size", 0) or 0)))
    if not items:
        say("[INFO] Empty.")


COMMANDS = {"token": cmd_token, "clone": cmd_clone, "list": cmd_list, "status": cmd_status, "pull": cmd_pull,
            "push": cmd_push, "log": cmd_log, "ls": cmd_ls}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in COMMANDS:
        say("gitkit %s - GitHub from the phone" % VERSION)
        say("commands: token | clone OWNER/NAME [FOLDER] | list | status | pull | push -m \"text\" | log | ls OWNER/NAME")
        return
    _TOKEN[0] = None
    try:
        COMMANDS[argv[0]](argv[1:])
    except GkError as e:
        say("[ERROR] " + str(e))
    except Exception:
        say("[CRITICAL] gitkit crashed:")
        say(traceback.format_exc()[-800:])


if __name__ != "gitkit":
    main()
