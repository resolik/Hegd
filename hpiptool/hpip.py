"""hpip v0.4.1 - tiny installer for pure-Python wheels, made to run inside Hegd.

Usage (inside Hegd, script lies in the project folder):
    py hpip.py install requests          (installs dependencies too)
    py hpip.py install six==1.16.0
    py hpip.py install six --force       (reinstall)
    py hpip.py install six --pypi        (ignore the registry for this command)
    py hpip.py install requests[socks]   (with an optional group of dependencies)
    py hpip.py install -r frozen.txt     (install exactly what a freeze file lists)
    py hpip.py freeze [file.txt]         (write the list of installed packages with versions and hashes)
    py hpip.py verify [name]             (check that installed files were not changed)
    py hpip.py registry add six https://.../six-1.17.0-py2.py3-none-any.whl [sha256]
    py hpip.py registry list
    py hpip.py registry remove six
    py hpip.py remove six [--force]
    py hpip.py outdated [--refresh]      (which packages have a newer version)
    py hpip.py upgrade six [--refresh]   (one package; the old version is kept if it fails)
    py hpip.py upgrade --all
    py hpip.py info six
    py hpip.py list
    py hpip.py path

Libraries go to  <script folder>/site-packages,  the list of installed
packages is  <script folder>/installed.json,  the registry (name -> wheel link
+ SHA-256 you pinned) is  <script folder>/registry.json.
Only wheels tagged  *-none-any  (pure Python) are accepted.
All wheels are downloaded and checked first; files are written only if every
package passed, and any failure while writing rolls everything back.
"""
import base64
import csv
import difflib
import hashlib
import importlib
import importlib.util
import json
import os
import platform
import re
import shutil
import socket
import ssl
import stat
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from email.parser import Parser

VERSION = "0.4.1"
UA = "hegd-hpip/" + VERSION
PYPI = "https://pypi.org/pypi/"
ALLOWED_HOSTS = ("pypi.org", "files.pythonhosted.org")

MAX_JSON = 15 * 1024 * 1024          # PyPI metadata answer
MAX_WHEEL = 25 * 1024 * 1024         # one wheel download
MAX_TOTAL = 150 * 1024 * 1024        # all unpacked files of one wheel
MAX_ONE = 60 * 1024 * 1024           # one unpacked file
MAX_FILES = 10000                    # files in one wheel
MAX_PACKAGES = 25                    # packages in one install command
MAX_PACKAGES_FILE = 60               # packages in one install -r
NATIVE_EXT = (".so", ".pyd", ".dll", ".dylib")
CHECK_TTL = 3600                     # an answer to "what is the newest version" is reused for an hour
CHECK_COOLDOWN = 600                 # one package is never asked about more than once per 10 min, even with --refresh
MIN_GAP = 0.4                        # seconds between two requests to PyPI
MAX_UPGRADE_ALL = 20                 # packages upgraded by one "upgrade --all"

# clock functions live here so tests can replace them
_now, _sleep, _mono = time.time, time.sleep, time.monotonic

# distribution name -> import name, when they differ (used to spot libraries the app already has)
IMPORT_NAMES = {"pyyaml": "yaml", "pillow": "PIL", "beautifulsoup4": "bs4",
                "python-dateutil": "dateutil", "pyjwt": "jwt", "attrs": "attr",
                "pysocks": "socks", "typing-extensions": "typing_extensions"}

try:
    BASE = os.path.dirname(os.path.abspath(__file__))
except NameError:
    BASE = os.getcwd()
SITE = os.path.join(BASE, "site-packages")
DB = os.path.join(BASE, "installed.json")
REG = os.path.join(BASE, "registry.json")
CACHE = os.path.join(BASE, "pypi_cache.json")


class HpipError(Exception):
    pass


class NotFound(HpipError):
    pass


class RateLimited(HpipError):
    pass


class NoNetwork(HpipError):
    pass


# ------------------------------------------------------- names and versions

def normalize(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_spec(text):
    if "==" in text:
        name, ver = text.split("==", 1)
    elif any(c in text for c in "<>=!~"):
        raise HpipError("use only name or name==version")
    else:
        name, ver = text, ""
    name, ver = name.strip(), ver.strip()
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", name):
        raise HpipError("bad package name: " + name[:40])
    if ver and not re.match(r"^[A-Za-z0-9][A-Za-z0-9.+!_-]*$", ver):
        raise HpipError("bad version: " + ver[:40])
    return name, ver


def parse_target(text):
    """'name', 'name==1.0', 'name[extra1,extra2]==1.0' -> (name, version, [extras])."""
    m = re.match(r"^\s*([^\[\]=<>!~\s]+)\s*(?:\[([^\]]*)\])?\s*(.*)$", text)
    if not m:
        raise HpipError("bad package name: " + text[:40])
    name, ver = parse_spec(m.group(1) + m.group(3).strip())
    extras = []
    for e in (m.group(2) or "").split(","):
        e = e.strip()
        if not e:
            continue
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", e):
            raise HpipError("bad extra name: " + e[:30])
        extras.append(normalize(e))
    return name, ver, sorted(set(extras))


def vkey(s):
    m = re.match(r"\s*v?(\d+(?:\.\d+)*)", s or "")
    return tuple(int(x) for x in m.group(1).split(".")) if m else (0,)


def vcmp(a, b):
    a, b = list(a), list(b)
    n = max(len(a), len(b))
    a += [0] * (n - len(a))
    b += [0] * (n - len(b))
    return (a > b) - (a < b)


def is_final(v):
    return bool(re.match(r"^\d+(\.\d+)*$", v or ""))


_SPEC_RE = re.compile(r"^\s*(===|~=|==|!=|<=|>=|<|>)\s*([0-9][0-9A-Za-z.*+!_-]*)\s*$")


def spec_ok(version, spec):
    """Does `version` satisfy specifier text like '>=1.21.1,<3'? Unknown parts are ignored."""
    if not version:
        return True
    cur = vkey(version)
    for clause in (spec or "").split(","):
        if not clause.strip():
            continue
        m = _SPEC_RE.match(clause)
        if not m:
            continue
        op, v = m.group(1), m.group(2)
        if v.endswith(".*"):
            pre = vkey(v[:-2])
            same = vcmp(cur[:len(pre)], pre) == 0
            if op in ("==", "===") and not same:
                return False
            if op == "!=" and same:
                return False
            continue
        c = vcmp(cur, vkey(v))
        if op == "~=":
            pre = vkey(v)[:-1]
            if c < 0 or (pre and vcmp(cur[:len(pre)], pre) != 0):
                return False
        elif op in ("==", "==="):
            if c != 0:
                return False
        elif op == "!=":
            if c == 0:
                return False
        elif op == ">=":
            if c < 0:
                return False
        elif op == "<=":
            if c > 0:
                return False
        elif op == ">":
            if c <= 0:
                return False
        elif op == "<":
            if c >= 0:
                return False
    return True


def python_ok(spec):
    return spec_ok("%d.%d.%d" % tuple(sys.version_info[:3]), spec)


def wheel_ok(filename):
    """True for pure-Python wheels this Python can use (abi 'none', platform 'any')."""
    if not filename.endswith(".whl"):
        return False
    parts = filename[:-4].split("-")
    if len(parts) < 5:
        return False
    py, abi, plat = parts[-3], parts[-2], parts[-1]
    if abi != "none" or plat != "any":
        return False
    for tag in py.split("."):
        m = re.match(r"^py3(\d*)$", tag)
        if m:
            minor = int(m.group(1)) if m.group(1) else 0
            if sys.version_info[1] >= minor:
                return True
    return False


# ----------------------------------------------------- requirements/markers

_REQ_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[([^\]]*)\])?\s*(.*?)\s*$")


def parse_req_ex(text):
    """'urllib3[x] (>=1.21,<3) ; python_version > "3"' -> (name, extras, spec, marker) or None."""
    body, _, marker = text.partition(";")
    body = body.strip()
    if "@" in body:
        return None
    m = _REQ_RE.match(body)
    if not m:
        return None
    rest = m.group(3).strip()
    if rest.startswith("(") and rest.endswith(")"):
        rest = rest[1:-1]
    spec = ",".join(c.strip() for c in rest.split(",") if c.strip())
    extras = sorted({normalize(e.strip()) for e in (m.group(2) or "").split(",") if e.strip()})
    return normalize(m.group(1)), extras, spec, marker.strip()


def parse_req(text):
    """Same without the extras: (name, spec, marker) or None."""
    pr = parse_req_ex(text)
    return None if pr is None else (pr[0], pr[2], pr[3])


def marker_env():
    return {
        "python_version": "%d.%d" % tuple(sys.version_info[:2]),
        "python_full_version": "%d.%d.%d" % tuple(sys.version_info[:3]),
        "sys_platform": sys.platform,
        "platform_system": platform.system(),
        "os_name": os.name,
        "implementation_name": sys.implementation.name,
        "platform_python_implementation": platform.python_implementation(),
        "platform_machine": platform.machine(),
        "extra": "",
    }


_FLIP = {"<": ">", ">": "<", "<=": ">=", ">=": "<="}


def _compare(left, op, right):
    (a, av), (b, bv) = left, right
    if op in ("in", "not in"):
        res = a in b
        return res if op == "in" else not res
    if av or bv:
        if bv and not av:
            a, b = b, a
            op = _FLIP.get(op, op)
        return spec_ok(a, op + b)
    if op in ("==", "==="):
        return a == b
    if op == "!=":
        return a != b
    if op == "~=":
        return a == b
    return {"<": a < b, ">": a > b, "<=": a <= b, ">=": a >= b}[op]


class _Marker:
    TOKENS = re.compile(r"""\(|\)|'[^']*'|"[^"]*"|===|==|!=|~=|<=|>=|<|>|[A-Za-z_][A-Za-z0-9_.]*""")

    def __init__(self, text, env):
        self.t = self.TOKENS.findall(text)
        self.i = 0
        self.env = env

    def peek(self):
        return self.t[self.i] if self.i < len(self.t) else None

    def next(self):
        tok = self.peek()
        self.i += 1
        return tok

    def parse(self):
        v = self.or_expr()
        if self.peek() is not None:
            raise ValueError("junk after marker")
        return v

    def or_expr(self):
        v = self.and_expr()
        while self.peek() == "or":
            self.next()
            r = self.and_expr()
            v = v or r
        return v

    def and_expr(self):
        v = self.atom()
        while self.peek() == "and":
            self.next()
            r = self.atom()
            v = v and r
        return v

    def atom(self):
        if self.peek() == "(":
            self.next()
            v = self.or_expr()
            if self.next() != ")":
                raise ValueError("missing )")
            return v
        left = self.value()
        op = self.next()
        if op == "not":
            if self.next() != "in":
                raise ValueError("bad 'not'")
            op = "not in"
        if op is None:
            raise ValueError("incomplete marker")
        return _compare(left, op, self.value())

    def value(self):
        tok = self.next()
        if tok is None:
            raise ValueError("incomplete marker")
        if tok[0] in "'\"":
            return (tok[1:-1], False)
        if tok in ("and", "or", "in", "not", "(", ")"):
            raise ValueError("unexpected " + tok)
        return (self.env.get(tok, ""), tok in ("python_version", "python_full_version"))


def eval_marker(text, env=None):
    return _Marker(text, env or marker_env()).parse()


def deps_from_meta(meta, extras=()):
    """-> ({name: spec}, [warnings], {name: [extras of that dependency]}) for what applies here and for these extras."""
    deps, warns, dep_extras = {}, [], {}
    wanted = {""} | {normalize(e) for e in extras}
    reqs = (meta.get_all("Requires-Dist") or []) if meta is not None else []
    for req in reqs:
        pr = parse_req_ex(req)
        if pr is None:
            warns.append("unsupported requirement skipped: " + req[:60])
            continue
        name, rextras, spec, marker = pr
        if marker:
            applies = False
            try:
                for e in wanted:
                    env = marker_env()
                    env["extra"] = e
                    if eval_marker(marker, env):
                        applies = True
                        break
            except (ValueError, KeyError):
                applies = True
                warns.append("could not read marker, assuming it applies: " + req[:60])
            if not applies:
                continue
        if name in deps and spec:
            deps[name] = (deps[name] + "," + spec).strip(",")
        elif name not in deps:
            deps[name] = spec
        dep_extras.setdefault(name, set()).update(rextras)
    return deps, warns, {k: sorted(v) for k, v in dep_extras.items()}


# ------------------------------------------------------------------- network

def check_url(url, any_host=False):
    """Only https. PyPI addresses are limited to PyPI's own hosts; a registry link may point anywhere."""
    p = urllib.parse.urlparse(url)
    if p.scheme != "https" or not p.hostname or (not any_host and p.hostname not in ALLOWED_HOSTS):
        raise HpipError("blocked address: " + url[:80])


def _ctx():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


_last_request = [0.0]


def _throttle():
    """Keep a small pause between two requests to PyPI."""
    gap = MIN_GAP - (_mono() - _last_request[0])
    if gap > 0:
        _sleep(gap)
    _last_request[0] = _mono()


def _retry_after(err):
    try:
        return max(0, int(err.headers.get("Retry-After")))
    except (TypeError, ValueError, AttributeError):
        return None


def http_get_json(url):
    check_url(url)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    data = None
    for attempt in (1, 2):
        _throttle()
        try:
            with urllib.request.urlopen(req, timeout=30, context=_ctx()) as r:
                check_url(r.geturl())
                data = r.read(MAX_JSON + 1)
            break
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise NotFound("not found on PyPI (HTTP 404): check the spelling of the name or version")
            if e.code == 429:
                wait = _retry_after(e)
                if attempt == 1 and wait is not None and wait <= 30:
                    _sleep(wait + 1)                      # PyPI asked for a short pause: wait once and retry
                    continue
                raise RateLimited("PyPI asks to slow down (HTTP 429). Try again in %s."
                                  % (("%d s" % wait) if wait else "a few minutes"))
            raise HpipError("PyPI answered HTTP %s" % e.code)
        except urllib.error.URLError as e:
            raise NoNetwork("no connection to PyPI (%s). Check Wi-Fi or mobile data." % (e.reason,))
        except (socket.timeout, TimeoutError):
            raise NoNetwork("PyPI did not answer in time. Check the connection and try again.")
    if data is None:
        raise HpipError("no answer from PyPI")
    if len(data) > MAX_JSON:
        raise HpipError("metadata answer is too big")
    try:
        return json.loads(data.decode("utf-8"))
    except ValueError:
        raise HpipError("PyPI answer is not valid JSON")


def http_download(url, dest, max_bytes, any_host=False):
    """Download to dest, return (sha256 hex, size)."""
    check_url(url, any_host)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    h = hashlib.sha256()
    n = 0
    try:
        with urllib.request.urlopen(req, timeout=30, context=_ctx()) as r, open(dest, "wb") as f:
            check_url(r.geturl(), any_host)
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                n += len(chunk)
                if n > max_bytes:
                    raise HpipError("download is bigger than the limit")
                h.update(chunk)
                f.write(chunk)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise HpipError("link not found (HTTP 404): check the address")
        raise HpipError("the server answered HTTP %s" % e.code)
    except urllib.error.URLError as e:
        raise NoNetwork("no connection (%s). Check Wi-Fi or mobile data." % (getattr(e, "reason", e),))
    except (socket.timeout, TimeoutError):
        raise NoNetwork("the server did not answer in time")
    return h.hexdigest(), n


# ------------------------------------------------------------------ database

def load_db():
    if not os.path.isfile(DB):
        return {}
    try:
        with open(DB, "r", encoding="utf-8") as f:
            db = json.load(f)
        if not isinstance(db, dict):
            raise ValueError
        return db
    except ValueError:
        raise HpipError("installed.json is damaged: fix it or delete it")


def save_db(db):
    tmp = DB + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=1, sort_keys=True)
    os.replace(tmp, DB)


def load_reg():
    if not os.path.isfile(REG):
        return {}
    try:
        with open(REG, "r", encoding="utf-8") as f:
            reg = json.load(f)
        if not isinstance(reg, dict):
            raise ValueError
        for k, v in reg.items():
            if not (isinstance(v, dict) and isinstance(v.get("url"), str) and isinstance(v.get("sha256"), str)):
                raise ValueError
        return reg
    except ValueError:
        raise HpipError("registry.json is damaged: fix it or delete it")


def save_reg(reg):
    tmp = REG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(reg, f, indent=1, sort_keys=True)
    os.replace(tmp, REG)


def required_by(norm, db):
    return sorted(p for p, rec in db.items() if p != norm and norm in rec.get("deps", []))


# ----------------------------------------------------------------- file work

def safe_rel(name):
    """Split a zip member name into safe parts, or None if it looks dangerous."""
    if "\\" in name or name.startswith("/") or "\x00" in name:
        return None
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    if re.match(r"^[A-Za-z]:", parts[0]):
        return None
    return parts


def select_file(files):
    """Pick the best pure-Python wheel from a release's file list."""
    wheels = [f for f in files if f.get("packagetype") == "bdist_wheel" and not f.get("yanked")]
    pure = [f for f in wheels if wheel_ok(f.get("filename", ""))]
    good = [f for f in pure if python_ok(f.get("requires_python"))]
    if good:
        good.sort(key=lambda f: ("py3-none-any" not in f["filename"], f["filename"]))
        return good[0]
    if pure:
        raise HpipError("needs a newer Python than %s" % sys.version.split()[0])
    if wheels:
        tags = sorted({"-".join(f["filename"][:-4].split("-")[-3:]) for f in wheels})
        raise HpipError("no pure-Python wheel (found: %s). It has compiled code, "
                        "so it cannot work here." % ", ".join(tags[:4]))
    raise HpipError("this release has no wheel (source only). hpip cannot build packages.")


def pick_release(norm, spec):
    """Return (version, file dict) of the newest release fitting `spec`."""
    m = re.match(r"^==\s*([A-Za-z0-9][A-Za-z0-9.+!_-]*)$", spec or "")
    if m:
        data = http_get_json(PYPI + norm + "/" + m.group(1) + "/json")
        info = data.get("info") or {}
        return info.get("version") or m.group(1), select_file(data.get("urls") or [])

    data = http_get_json(PYPI + norm + "/json")
    info = data.get("info") or {}
    latest = info.get("version")
    first_err = None
    if latest and spec_ok(latest, spec):
        try:
            return latest, select_file(data.get("urls") or [])
        except HpipError as e:
            first_err = e
    releases = data.get("releases")
    if not releases:
        raise first_err or HpipError("no release fits %s%s" % (norm, spec))
    cands = []
    for v, files in releases.items():
        if not is_final(v) or not spec_ok(v, spec):
            continue
        try:
            cands.append((vkey(v), v, select_file(files)))
        except HpipError:
            continue
    if not cands:
        raise first_err or HpipError("no release of %s fits '%s' with a pure-Python wheel for this Python"
                                     % (norm, spec))
    cands.sort(key=lambda c: c[0], reverse=True)
    return cands[0][1], cands[0][2]


def plan_extract(z):
    infos = z.infolist()
    if len(infos) > MAX_FILES:
        raise HpipError("wheel has too many files")
    plan, skipped, total = [], 0, 0
    for info in infos:
        if (info.external_attr >> 16) & 0o170000 == stat.S_IFLNK:
            raise HpipError("wheel contains a symlink: refused")
        parts = safe_rel(info.filename)
        if parts is None:
            raise HpipError("unsafe path inside wheel: " + info.filename[:80])
        if parts[0].endswith(".data"):
            if len(parts) >= 3 and parts[1] in ("purelib", "platlib"):
                parts = parts[2:]
            else:
                skipped += 1
                continue
        is_dir = info.filename.endswith("/")
        if not is_dir:
            if info.file_size > MAX_ONE:
                raise HpipError("a file inside the wheel is too big")
            total += info.file_size
            if total > MAX_TOTAL:
                raise HpipError("unpacked size is over the limit")
            if parts[-1].lower().endswith(NATIVE_EXT):
                raise HpipError("wheel has compiled code (%s): refused" % parts[-1])
        plan.append((info.filename, parts, is_dir))
    return plan, skipped


def read_metadata(z):
    for info in z.infolist():
        if re.match(r"^[^/]+\.dist-info/METADATA$", info.filename):
            with z.open(info) as f:
                text = f.read(1024 * 1024).decode("utf-8", "replace")
            return Parser().parsestr(text)
    return None


def remove_files(rels):
    """Delete files of a package, together with the .pyc files Python made for them."""
    for rel in rels:
        path = os.path.join(SITE, *rel.split("/"))
        try:
            os.remove(path)
        except OSError:
            pass
        if rel.endswith(".py"):
            folder, base = os.path.split(path)
            cdir = os.path.join(folder, "__pycache__")
            try:
                for fn in os.listdir(cdir):
                    if fn.startswith(base[:-3] + ".") and fn.endswith(".pyc"):
                        os.remove(os.path.join(cdir, fn))
            except OSError:
                pass


def prune_empty():
    if not os.path.isdir(SITE):
        return
    for root, dirs, files in os.walk(SITE, topdown=False):
        if root != SITE and not os.listdir(root):
            try:
                os.rmdir(root)
            except OSError:
                pass


def extract(z, plan, created):
    root = os.path.realpath(SITE)
    written = 0
    for filename, parts, is_dir in plan:
        dest = os.path.join(SITE, *parts)
        real = os.path.realpath(dest)
        if real != root and not real.startswith(root + os.sep):
            raise HpipError("path escapes site-packages: refused")
        if is_dir:
            os.makedirs(dest, exist_ok=True)
            continue
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        created.append("/".join(parts))
        with z.open(z.getinfo(filename)) as src, open(dest, "wb") as out:
            while True:
                chunk = src.read(65536)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_TOTAL:
                    raise HpipError("unpacked size is over the limit")
                out.write(chunk)


# -------------------------------------------------- libraries the app already has

def provided_by_app(norm):
    """Return the version string ('' if unknown) when the app itself ships this library, else None."""
    mod = IMPORT_NAMES.get(norm, norm.replace("-", "_"))
    try:
        spec = importlib.util.find_spec(mod)
    except (ImportError, ValueError, AttributeError):
        return None
    if spec is None:
        return None
    where = spec.origin if spec.origin not in (None, "namespace") else None
    if where is None and spec.submodule_search_locations:
        where = list(spec.submodule_search_locations)[0]
    root = os.path.realpath(SITE)
    if where and os.path.realpath(where).startswith(root + os.sep):
        return None
    try:
        from importlib import metadata
        return metadata.version(norm)
    except Exception:
        pass
    try:
        m = sys.modules.get(mod) or importlib.import_module(mod)
        return str(getattr(m, "__version__", "") or "")
    except Exception:
        return ""


# ------------------------------------------------------------------- install

def fetch_item(norm, spec, explicit, tmpdir, idx, reg=None, extras=(), expect_sha=""):
    """Find, download and check one package (nothing is written to site-packages yet)."""
    entry = (reg or {}).get(norm)
    if entry:
        pinned = entry.get("version") or ""
        m = re.match(r"^==\s*(.+)$", spec or "")
        if m and pinned and m.group(1).strip() != pinned:
            raise HpipError("%s is pinned in the registry to %s. Use --pypi or 'registry remove %s'."
                            % (norm, pinned, norm))
        if pinned and spec and not m and not spec_ok(pinned, spec):
            print("[WARNING] The registry has %s %s but a package wants '%s'." % (norm, pinned, spec))
        version = pinned or "?"
        f = {"filename": os.path.basename(urllib.parse.urlparse(entry["url"]).path), "url": entry["url"],
             "size": 0, "digests": {"sha256": entry["sha256"]}}
        source, any_host = "registry", True
        print("[INFO] %s comes from your registry." % norm)
    else:
        version, f = pick_release(norm, spec)
        source, any_host = "pypi", False
    size = f.get("size") or 0
    if size > MAX_WHEEL:
        raise HpipError("%s: wheel is too big (%d MB)" % (norm, size // 1048576))
    want = ((f.get("digests") or {}).get("sha256") or "").lower()
    if not want:
        raise HpipError("%s: no SHA-256 for this file: refused" % norm)
    path = os.path.join(tmpdir, "%d.whl" % idx)
    print("[INFO] Downloading %s%s..." % (f["filename"], (" (%d KB)" % (max(size, 1) // 1024)) if size else ""))
    got, _ = http_download(f["url"], path, MAX_WHEEL, any_host)
    if got != want:
        raise HpipError("%s: SHA-256 does not match what is pinned or published, nothing installed" % norm)
    if expect_sha and got != expect_sha:
        raise HpipError("%s: SHA-256 differs from the one in your list, nothing installed" % norm)
    try:
        z = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise HpipError("%s: downloaded file is not a valid wheel" % norm)
    with z:
        meta = read_metadata(z)
        if meta is not None and meta.get("Name") and normalize(meta.get("Name")) != norm:
            print("[WARNING] Wheel says its name is %s." % meta.get("Name"))
        plan, skipped = plan_extract(z)
    if extras:
        provided = {normalize(x) for x in ((meta.get_all("Provides-Extra") or []) if meta is not None else [])}
        for e in extras:
            if e not in provided:
                print("[WARNING] %s has no extra '%s'." % (norm, e))
    if source == "registry" and meta is not None and meta.get("Version"):
        version = meta.get("Version")
    deps, warns, dep_extras = deps_from_meta(meta, extras)
    for w in warns:
        print("[WARNING] " + w)
    raw = []
    if meta is not None:
        raw = [r.strip() for r in (meta.get_all("Requires-Dist") or [])
               if "extra ==" not in r and "extra==" not in r]
    return {"norm": norm, "version": version, "wheel": f["filename"], "sha256": got,
            "path": path, "plan": plan, "skipped": skipped, "deps": deps,
            "requires": raw, "explicit": explicit, "source": source,
            "extras": sorted(extras), "dep_extras": dep_extras}


def check_conflicts(items, db, force):
    owners = {}
    for pkg, rec in db.items():
        for rel in rec.get("files", []):
            owners[rel] = pkg
    planned, clash = {}, []
    for it in items:
        for _fn, parts, is_dir in it["plan"]:
            if is_dir:
                continue
            rel = "/".join(parts)
            other = owners.get(rel)
            if other and other != it["norm"]:
                clash.append("%s (belongs to %s)" % (rel, other))
            elif not other and os.path.exists(os.path.join(SITE, *parts)) and not force:
                clash.append("%s (already exists)" % rel)
            if rel in planned and planned[rel] != it["norm"]:
                clash.append("%s (in both %s and %s)" % (rel, planned[rel], it["norm"]))
            planned[rel] = it["norm"]
    if clash:
        raise HpipError("file conflict, nothing installed: " + "; ".join(clash[:4]))


def install_requests(roots, force=False, reg=None, limit=MAX_PACKAGES):
    """Install packages as ONE transaction.
    roots: [{"norm", "spec", "explicit", "extras", "sha" (expected SHA-256), "skip_same"}].
    Replaced versions are moved aside first and put back if anything fails."""
    reg = reg or {}
    db = load_db()
    os.makedirs(SITE, exist_ok=True)
    tmpdir = tempfile.mkdtemp(prefix="hpip_", dir=BASE)
    items, from_app, have, created, backups = [], [], [], [], []
    try:
        rootinfo = {r["norm"]: r for r in roots}
        queue = [(r["norm"], r.get("spec", ""), r.get("explicit", True), True, r.get("extras", [])) for r in roots]
        seen = set()
        while queue:
            norm, spec, explicit, is_root, extras = queue.pop(0)
            if norm in seen:
                continue
            seen.add(norm)
            if not is_root:
                if norm in db:
                    cur = db[norm].get("version")
                    if not spec_ok(cur, spec):
                        print("[WARNING] %s %s is installed but a package wants '%s'." % (norm, cur, spec))
                    have.append(norm)
                    continue
                app_ver = provided_by_app(norm)
                if app_ver is not None:
                    if app_ver and not spec_ok(app_ver, spec):
                        print("[WARNING] The app has %s %s but a package wants '%s'." % (norm, app_ver, spec))
                    from_app.append(norm)
                    continue
            expect = ""
            if is_root:
                r = rootinfo[norm]
                expect = r.get("sha", "")
                if (r.get("skip_same") and not force and norm in db
                        and spec == "==" + str(db[norm].get("version")) and set(extras) <= set(db[norm].get("extras", []))):
                    have.append(norm)
                    continue
            if len(items) >= limit:
                raise HpipError("too many packages in one install (limit %d)" % limit)
            print("[INFO] Looking up %s%s..." % (norm, (" " + spec) if spec else ""))
            item = fetch_item(norm, spec, explicit, tmpdir, len(items), reg, extras, expect)
            items.append(item)
            for dep, dspec in item["deps"].items():
                queue.append((dep, dspec, False, False, item["dep_extras"].get(dep, [])))

        check_conflicts(items, db, force)

        bdir = os.path.join(tmpdir, "backup")
        replaced = {}
        try:
            for it in items:                              # old versions step aside
                if it["norm"] in db:
                    old = db[it["norm"]].get("files", [])
                    replaced[it["norm"]] = old
                    for rel in old:
                        src = os.path.join(SITE, *rel.split("/"))
                        if os.path.isfile(src):
                            os.makedirs(bdir, exist_ok=True)
                            dst = os.path.join(bdir, str(len(backups)))
                            shutil.move(src, dst)
                            backups.append((rel, dst))
            for it in items:
                start = len(created)
                with zipfile.ZipFile(it["path"]) as z:
                    extract(z, it["plan"], created)
                it["files"] = sorted(set(created[start:]))
        except BaseException:
            remove_files(created)                          # throw the new files away ...
            for rel, dst in backups:                       # ... and bring the old ones back
                target = os.path.join(SITE, *rel.split("/"))
                try:
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    shutil.move(dst, target)
                except OSError:
                    pass
            prune_empty()
            raise

        for it in items:
            db[it["norm"]] = {"version": it["version"], "wheel": it["wheel"], "sha256": it["sha256"],
                              "files": it["files"], "requires": it["requires"],
                              "deps": sorted(it["deps"]), "explicit": it["explicit"],
                              "source": it["source"], "extras": it["extras"]}
        save_db(db)
        for it in items:                                   # stale .pyc of files the new version no longer has
            gone = [r for r in replaced.get(it["norm"], []) if r not in set(it["files"])]
            remove_files(gone)
        prune_empty()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    for it in items:
        print("[SUCCESS] Installed %s %s (%d files)." % (it["norm"], it["version"], len(it["files"])))
    if from_app:
        print("[INFO] Already provided by the app: " + ", ".join(sorted(from_app)))
    if have:
        print("[INFO] Already installed: " + ", ".join(sorted(have)))
    skipped = sum(it["skipped"] for it in items)
    if skipped:
        print("[INFO] Skipped %d non-library entries (scripts/headers)." % skipped)
    if items:
        print("[INFO] Use in a script: import sys; sys.path.insert(0, r'%s')" % SITE)
    return items


def read_freeze(path):
    """Read a freeze file -> (list of root dicts, registry-like entries for the '@ link' lines)."""
    full = path if os.path.isabs(path) else os.path.join(BASE, path)
    if not os.path.isfile(full):
        raise HpipError("file not found: " + path)
    if os.path.getsize(full) > 1024 * 1024:
        raise HpipError("the list file is too big")
    with open(full, "r", encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
    roots, local_reg, names = [], {}, set()
    for no, line in enumerate(lines, 1):
        body, _, comment = line.partition("#")
        body = body.strip()
        if not body:
            continue
        m = re.match(r"^(\S+)(?:\s+@\s+(\S+))?(?:\s+--sha256=([0-9A-Fa-f]{64}))?$", body)
        if not m:
            raise HpipError("line %d is not understood: %s" % (no, body[:50]))
        try:
            name, ver, extras = parse_target(m.group(1))
            if not ver:
                raise HpipError("a line needs name==version")
            url, sha = m.group(2), (m.group(3) or "").lower()
            if url:
                if not sha:
                    raise HpipError("a link needs --sha256=")
                check_url(url, True)
        except HpipError as e:
            raise HpipError("line %d: %s" % (no, e))
        norm = normalize(name)
        if norm in names:
            raise HpipError("line %d: %s is listed twice" % (no, norm))
        names.add(norm)
        if url:
            local_reg[norm] = {"url": url, "sha256": sha, "version": ver}
        roots.append({"norm": norm, "spec": "==" + ver, "explicit": "dep" not in comment.split(),
                      "extras": extras, "sha": sha, "skip_same": True, "url": bool(url)})
    if not roots:
        raise HpipError("the list is empty")
    if len(roots) > MAX_PACKAGES_FILE:
        raise HpipError("the list has more than %d packages" % MAX_PACKAGES_FILE)
    return roots, local_reg


def cmd_install(args):
    force = "--force" in args
    rest = [a for a in args if not a.startswith("--")]
    if "-r" in rest:
        i = rest.index("-r")
        if i + 1 >= len(rest) or len(rest) != 2:
            print("usage: py hpip.py install -r file.txt [--force]")
            return
        roots, local_reg = read_freeze(rest[i + 1])
        reg = {} if "--pypi" in args else load_reg()
        for r in roots:                                   # a plain line means PyPI, whatever the registry says
            if not r["url"]:
                reg.pop(r["norm"], None)
        reg.update(local_reg)
        install_requests(roots, force, reg, MAX_PACKAGES_FILE)
        return
    if len(rest) != 1:
        print("usage: py hpip.py install name[extras][==version] [--force] [--pypi]   |   install -r file.txt")
        return
    name, ver, extras = parse_target(rest[0])
    root = normalize(name)
    rec = load_db().get(root)
    if rec and not force:
        missing = sorted(set(extras) - set(rec.get("extras", [])))
        if not missing:
            print("[INFO] %s %s is already installed (use --force to reinstall)." % (root, rec.get("version", "?")))
            return
        print("[INFO] %s is installed without the extra(s) %s: reinstalling with them." % (root, ", ".join(missing)))
        extras = sorted(set(extras) | set(rec.get("extras", [])))
        if not ver:
            ver = str(rec.get("version", ""))
    reg = {} if "--pypi" in args else load_reg()
    install_requests([{"norm": root, "spec": ("==" + ver) if ver else "", "explicit": True, "extras": extras}],
                     force, reg)


def cmd_remove(args):
    force = "--force" in args
    names = [a for a in args if not a.startswith("--")]
    if len(names) != 1:
        print("usage: py hpip.py remove name [--force]")
        return
    norm = normalize(names[0])
    db = load_db()
    if norm not in db:
        print("[ERROR] %s is not installed.%s" % (norm, hint(norm, db)))
        return
    users = required_by(norm, db)
    if users and not force:
        print("[ERROR] Needed by: %s. Remove them first or use --force." % ", ".join(users))
        return
    remove_files(db[norm].get("files", []))
    deps = db[norm].get("deps", [])
    del db[norm]
    save_db(db)
    prune_empty()
    print("[SUCCESS] Removed %s." % norm)
    unused = [d for d in deps if d in db and not db[d].get("explicit", True)
              and not required_by(d, db)]
    if unused:
        print("[INFO] Now unused dependencies: %s (remove with: remove <name>)" % ", ".join(unused))


def cmd_info(args):
    if len(args) != 1:
        print("usage: py hpip.py info name")
        return
    norm = normalize(args[0])
    db = load_db()
    rec = db.get(norm)
    if rec is None:
        print("[ERROR] %s is not installed.%s" % (norm, hint(norm, db)))
        return
    print("%s %s" % (norm, rec.get("version", "?")))
    print("wheel:    %s" % rec.get("wheel", "?"))
    print("sha256:   %s" % rec.get("sha256", "?"))
    print("files:    %d" % len(rec.get("files", [])))
    print("kind:     %s" % ("installed by you" if rec.get("explicit", True) else "dependency"))
    print("source:   %s" % rec.get("source", "pypi"))
    if rec.get("extras"):
        print("extras:   %s" % ", ".join(rec["extras"]))
    print("needs:    %s" % (", ".join(rec.get("deps", [])) or "-"))
    print("used by:  %s" % (", ".join(required_by(norm, db)) or "-"))


def cmd_list(args):
    db = load_db()
    if not db:
        print("[INFO] Nothing installed.")
        return
    for pkg in sorted(db):
        rec = db[pkg]
        mark = ("" if rec.get("explicit", True) else " [dep]") + (" [registry]" if rec.get("source") == "registry" else "")
        print("%s %s (%d files)%s" % (pkg, rec.get("version", "?"), len(rec.get("files", [])), mark))


def hint(norm, names):
    close = difflib.get_close_matches(norm, list(names), n=2, cutoff=0.6)
    return (" Did you mean: %s?" % ", ".join(close)) if close else ""


def _ago(sec):
    if sec < 90:
        return "%d s" % sec
    if sec < 5400:
        return "%d min" % (sec // 60)
    return "%d h" % (sec // 3600)


def load_cache():
    try:
        with open(CACHE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_cache(cache):
    tmp = CACHE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=1, sort_keys=True)
        os.replace(tmp, CACHE)
    except OSError:
        pass                                   # the cache is only a convenience


def latest_installable(norm, refresh=False):
    """Newest version of norm that can be installed here.
    Returns (version, age in seconds, came from the cache, --refresh was refused by the cooldown).
    PyPI is asked at most once per CHECK_COOLDOWN, and once per CHECK_TTL unless --refresh."""
    now = _now()
    cache = load_cache()
    rec = cache.get(norm) if isinstance(cache.get(norm), dict) else None
    age = None
    if rec and isinstance(rec.get("checked"), (int, float)) and now - rec["checked"] >= 0:
        age = now - rec["checked"]
    if age is not None:
        in_cooldown = age < CHECK_COOLDOWN
        if in_cooldown or (age < CHECK_TTL and not refresh):
            if rec.get("error"):
                raise HpipError("%s (checked %s ago)" % (rec["error"], _ago(int(age))))
            if rec.get("latest"):
                return rec["latest"], int(age), True, bool(refresh and in_cooldown)
    try:
        version, _f = pick_release(norm, "")
    except (NoNetwork, RateLimited):
        raise
    except HpipError as e:
        cache[norm] = {"error": str(e), "checked": now}      # remember failures too: no hammering
        save_cache(cache)
        raise
    cache[norm] = {"latest": version, "checked": now}
    save_cache(cache)
    return version, 0, False, False


def check_latest(db, refresh):
    """-> (outdated [(name, have, newest)], notes, packages where --refresh was refused, ages of cached answers)"""
    rows, notes, cooled, ages = [], [], [], []
    for norm in sorted(db):
        rec = db[norm]
        if rec.get("source") == "registry":
            notes.append("%s is pinned in your registry, not checked." % norm)
            continue
        try:
            latest, age, cached, refused = latest_installable(norm, refresh)
        except (NoNetwork, RateLimited):
            raise
        except HpipError as e:
            notes.append("%s: cannot check (%s)" % (norm, e))
            continue
        if cached:
            ages.append(age)
        if refused:
            cooled.append(norm)
        if vcmp(vkey(latest), vkey(rec.get("version", "0"))) > 0:
            rows.append((norm, rec.get("version", "?"), latest))
    return rows, notes, cooled, ages


def cmd_outdated(args):
    db = load_db()
    if not db:
        print("[INFO] Nothing installed.")
        return
    rows, notes, cooled, ages = check_latest(db, "--refresh" in args)
    for n in notes:
        print("[INFO] " + n)
    for norm, cur, new in rows:
        print("%s %s -> %s" % (norm, cur, new))
    if rows:
        print("[INFO] Update one with: upgrade <name>   or all: upgrade --all")
    else:
        print("[INFO] Everything checked is up to date.")
    if ages:
        print("[INFO] Some answers come from the cache (oldest: %s). --refresh asks PyPI again, "
              "but one package is asked at most once per %d min." % (_ago(max(ages)), CHECK_COOLDOWN // 60))
    if cooled:
        print("[INFO] --refresh skipped (asked too recently): " + ", ".join(cooled))


def cmd_upgrade(args):
    refresh = "--refresh" in args
    every = "--all" in args
    names = [a for a in args if not a.startswith("--")]
    if every == bool(names) or len(names) > 1:
        print("usage: py hpip.py upgrade name [--refresh]   |   upgrade --all [--refresh]")
        return
    db = load_db()
    if every:
        rows, notes, cooled, ages = check_latest(db, refresh)
        for n in notes:
            print("[INFO] " + n)
        if not rows:
            print("[INFO] Everything checked is up to date.")
            return
        todo = rows[:MAX_UPGRADE_ALL]
        if len(rows) > len(todo):
            print("[INFO] Upgrading the first %d now; run it again for the rest." % len(todo))
    else:
        norm = normalize(names[0])
        if norm not in db:
            print("[ERROR] %s is not installed.%s" % (norm, hint(norm, db)))
            return
        if db[norm].get("source") == "registry":
            print("[ERROR] %s is pinned in your registry. To change it: registry add %s <new link> --force"
                  % (norm, norm))
            return
        latest, age, cached, refused = latest_installable(norm, refresh)
        cur = db[norm].get("version", "0")
        if vcmp(vkey(latest), vkey(cur)) <= 0:
            print("[INFO] %s %s is up to date%s." % (norm, cur, (" (checked %s ago)" % _ago(age)) if cached else ""))
            if refused:
                print("[INFO] --refresh skipped: asked too recently (cooldown %d min)." % (CHECK_COOLDOWN // 60))
            return
        todo = [(norm, cur, latest)]
    done, failed = 0, []
    for norm, cur, new in todo:
        print("[INFO] Upgrading %s %s -> %s ..." % (norm, cur, new))
        try:
            rec0 = load_db().get(norm, {})
            install_requests([{"norm": norm, "spec": "==" + new, "explicit": rec0.get("explicit", True),
                               "extras": rec0.get("extras", [])}], False, {})
            done += 1
        except (NoNetwork, RateLimited):
            raise
        except HpipError as e:
            print("[ERROR] %s: %s (the old version is kept)" % (norm, e))
            failed.append(norm)
    if every:
        print("[INFO] Upgraded %d, failed %d%s." % (done, len(failed), (": " + ", ".join(failed)) if failed else ""))


def cmd_freeze(args):
    force = "--force" in args
    names = [a for a in args if not a.startswith("--")]
    if len(names) > 1:
        print("usage: py hpip.py freeze [file.txt] [--force]")
        return
    db = load_db()
    if not db:
        print("[INFO] Nothing installed.")
        return
    reg = load_reg()
    lines = ["# hpip %s freeze, made %s" % (VERSION, time.strftime("%Y-%m-%d %H:%M")),
             "# install again with:  py hpip.py install -r <this file>"]
    for norm in sorted(db):
        rec = db[norm]
        ver, sha = rec.get("version"), rec.get("sha256")
        if not ver or not sha:
            lines.append("# %s: no version or hash recorded, not frozen" % norm)
            continue
        ex = rec.get("extras") or []
        head = "%s%s==%s" % (norm, ("[" + ",".join(ex) + "]") if ex else "", ver)
        tail = ""
        if rec.get("source") == "registry":
            entry = reg.get(norm)
            if not entry or entry.get("sha256") != sha:
                lines.append("# %s came from your registry, but the registry no longer has this file: not frozen" % norm)
                continue
            tail = " @ " + entry["url"]
        line = "%s%s --sha256=%s" % (head, tail, sha)
        if not rec.get("explicit", True):
            line += "  # dep"
        lines.append(line)
    if not names:
        for ln in lines:
            print(ln)
        return
    fname = names[0]
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", fname):
        print("[ERROR] Use a plain file name (it is saved next to hpip.py).")
        return
    path = os.path.join(BASE, fname)
    if os.path.exists(path) and not force:
        print("[ERROR] %s already exists (use --force to replace it)." % fname)
        return
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("[SUCCESS] Saved %d packages to %s." % (sum(1 for ln in lines if not ln.startswith("#")), fname))


def _record_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return base64.urlsafe_b64encode(h.digest()).rstrip(b"=").decode("ascii")


def verify_one(rec):
    """-> (files compared by hash, [problems])"""
    files = rec.get("files", [])
    fileset = set(files)
    problems, hashes = [], {}
    rec_rel = next((f for f in files if re.match(r"^[^/]+\.dist-info/RECORD$", f)), None)
    if rec_rel:
        try:
            with open(os.path.join(SITE, *rec_rel.split("/")), "r", encoding="utf-8", newline="") as f:
                for row in csv.reader(f):
                    if len(row) >= 2 and row[1].startswith("sha256=") and row[0] in fileset:
                        hashes[row[0]] = row[1][7:].rstrip("=")
        except (OSError, csv.Error, UnicodeDecodeError):
            problems.append("RECORD cannot be read")
    checked = 0
    for rel in files:
        path = os.path.join(SITE, *rel.split("/"))
        if not os.path.isfile(path):
            problems.append("missing: " + rel)
            continue
        want = hashes.get(rel)
        if want:
            checked += 1
            try:
                if _record_hash(path) != want:
                    problems.append("CHANGED: " + rel)
            except OSError:
                problems.append("unreadable: " + rel)
    return checked, problems


def cmd_verify(args):
    if len(args) > 1:
        print("usage: py hpip.py verify [name]")
        return
    db = load_db()
    if args:
        norm = normalize(args[0])
        if norm not in db:
            print("[ERROR] %s is not installed.%s" % (norm, hint(norm, db)))
            return
        targets = [norm]
    else:
        targets = sorted(db)
    if not targets:
        print("[INFO] Nothing installed.")
        return
    bad = []
    for norm in targets:
        checked, problems = verify_one(db[norm])
        if problems:
            bad.append(norm)
            print("[ERROR] %s %s: %d problem(s)" % (norm, db[norm].get("version", "?"), len(problems)))
            for pr in problems[:5]:
                print("   " + pr)
            if len(problems) > 5:
                print("   ... and %d more" % (len(problems) - 5))
        elif checked:
            print("%s %s: ok (%d files compared with their hashes)" % (norm, db[norm].get("version", "?"), checked))
        else:
            print("%s %s: all files exist (no hashes to compare)" % (norm, db[norm].get("version", "?")))
    if bad:
        print("[INFO] Put a package back with: py hpip.py install <name> --force")
    else:
        print("[SUCCESS] Everything checked is intact.")


def reg_add(args):
    force = "--force" in args
    pos = [a for a in args if not a.startswith("--")]
    if len(pos) not in (2, 3):
        print("usage: py hpip.py registry add name https://link/pkg-1.0-py3-none-any.whl [sha256] [--force]")
        return
    name, url = pos[0], pos[1]
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", name):
        raise HpipError("bad package name: " + name[:40])
    norm = normalize(name)
    given = pos[2].lower() if len(pos) == 3 else ""
    if given and not re.match(r"^[0-9a-f]{64}$", given):
        raise HpipError("the SHA-256 must be 64 hex characters")
    check_url(url, True)
    fname = os.path.basename(urllib.parse.urlparse(url).path)
    if not wheel_ok(fname):
        raise HpipError("the link must end with a pure-Python wheel name (...-py3-none-any.whl)")
    reg = load_reg()
    if norm in reg and not force:
        raise HpipError("%s is already in the registry (use --force to replace it)" % norm)
    tmpdir = tempfile.mkdtemp(prefix="hpip_", dir=BASE)
    try:
        path = os.path.join(tmpdir, "pkg.whl")
        print("[INFO] Downloading %s to check it..." % fname)
        got, _ = http_download(url, path, MAX_WHEEL, True)
        if given and got != given:
            raise HpipError("SHA-256 does not match the one you gave: nothing saved")
        try:
            z = zipfile.ZipFile(path)
        except zipfile.BadZipFile:
            raise HpipError("the link does not give a valid wheel")
        with z:
            meta = read_metadata(z)
            plan_extract(z)                                   # refuses unsafe paths, links and compiled code
        version = (meta.get("Version") if meta is not None else "") or ""
        if meta is not None and meta.get("Name") and normalize(meta.get("Name")) != norm:
            print("[WARNING] The wheel says its name is %s, not %s." % (meta.get("Name"), name))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    reg[norm] = {"url": url, "sha256": got, "version": version}
    save_reg(reg)
    print("[SUCCESS] Registered %s %s." % (norm, version or "(version unknown)"))
    print("[INFO] SHA-256 %s" % got)
    if not given:
        print("[INFO] Pinned from this download: later installs must give the same file.")


def reg_remove(args):
    if len(args) != 1:
        print("usage: py hpip.py registry remove name")
        return
    norm = normalize(args[0])
    reg = load_reg()
    if norm not in reg:
        print("[ERROR] %s is not in the registry.%s" % (norm, hint(norm, reg)))
        return
    del reg[norm]
    save_reg(reg)
    print("[SUCCESS] Removed %s from the registry (installed files are untouched)." % norm)


def reg_list(args):
    reg = load_reg()
    if not reg:
        print("[INFO] The registry is empty.")
        return
    for name in sorted(reg):
        e = reg[name]
        host = urllib.parse.urlparse(e["url"]).hostname or "?"
        print("%s %s  %s  %s" % (name, e.get("version") or "?", e["sha256"][:12], host))


def cmd_registry(args):
    subs = {"add": reg_add, "remove": reg_remove, "list": reg_list}
    if not args or args[0] not in subs:
        print("usage: py hpip.py registry add name url [sha256] | remove name | list")
        return
    subs[args[0]](args[1:])


def cmd_path(args):
    print("import sys; sys.path.insert(0, r'%s')" % SITE)


COMMANDS = {"install": cmd_install, "registry": cmd_registry, "remove": cmd_remove,
            "outdated": cmd_outdated, "upgrade": cmd_upgrade, "freeze": cmd_freeze, "verify": cmd_verify, "info": cmd_info,
            "list": cmd_list, "path": cmd_path}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in COMMANDS:
        print("hpip %s - pure-Python wheels only" % VERSION)
        print("commands: install name[==version] [--force] [--pypi] | remove name [--force] | info name | list | path")
        print("          outdated [--refresh] | upgrade name | upgrade --all")
        print("          freeze [file] | install -r file | verify [name]")
        print("          registry add name url [sha256] | registry remove name | registry list")
        return
    try:
        COMMANDS[argv[0]](argv[1:])
    except HpipError as e:
        print("[ERROR] " + str(e))
    except Exception:
        print("[CRITICAL] hpip crashed:")
        print(traceback.format_exc()[-800:])


if __name__ != "hpip":
    main()
