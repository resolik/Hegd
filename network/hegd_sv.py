"""hegd_sv - a tiny file web server for Hegd (used by the +sv command and by sv.py).

It shows ONE folder in a browser on the same network:
  - read-only by default; uploads only when asked for ("up"),
  - the address contains a short secret code; a wrong code looks like a missing page,
    and an address that tries a wrong code too often is blocked for a while,
  - it stops by itself after a number of minutes,
  - names starting with "." are never shown or served, symlinks cannot lead outside the folder,
  - HTML/SVG/XML files are sent as plain text, so a shared page cannot run scripts.
The traffic is plain HTTP (not encrypted): use it at home, not on a stranger's Wi-Fi.
"""
import hmac
import html
import http.server
import mimetypes
import os
import re
import secrets
import socket
import threading
import time
import urllib.parse

DEFAULT_MINUTES = 15
MAX_MINUTES = 120
MAX_UPLOAD = 200 * 1024 * 1024
MAX_LISTED = 5000
MAX_CONNECTIONS = 16
CHUNK = 64 * 1024
PORTS = range(8080, 8100)
TOKEN_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"       # no look-alike characters (i l o 0 1)
TOKEN_LENGTH = 8
BAD_LIMIT = 10                                           # wrong codes from one address ...
BLOCK_SECONDS = 300                                      # ... block it for 5 minutes
AS_TEXT = (".html", ".htm", ".xhtml", ".svg", ".xml")


class SvError(Exception):
    pass


def make_token(length=TOKEN_LENGTH):
    return "".join(secrets.choice(TOKEN_ALPHABET) for _ in range(length))


def lan_ips():
    """Addresses of this phone on the local network (best effort)."""
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.2)
        s.connect(("10.255.255.255", 1))          # nothing is sent: it only shows which address would be used
        ips.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    return [ip for ip in ips if ip and not ip.startswith("127.") and ip != "0.0.0.0"]


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0


def clean(text, limit=80):
    """Safe to print: no control characters, not too long."""
    return re.sub(r"[^\w\-. /()+,@]", "?", text, flags=re.UNICODE)[:limit]


def safe_name(name):
    if not name or len(name) > 120 or name in (".", ".."):
        return False
    if name.startswith(".") or "/" in name or "\\" in name:
        return False
    return not any(ord(c) < 32 or ord(c) == 127 for c in name)


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True
    request_queue_size = 16


PAGE_CSS = ("body{font-family:sans-serif;margin:0;padding:12px;background:#fff;color:#111}"
            "h1{font-size:18px;word-break:break-all}a{color:#0645ad}"
            "li{list-style:none;padding:10px 4px;border-bottom:1px solid #ddd;word-break:break-all}"
            "ul{padding:0;margin:0}.s{color:#666;float:right;margin-left:8px}"
            "button,input{font-size:16px;margin:6px 0}pre{white-space:pre-wrap}")
UPLOAD_JS = ("async function up(){const fs=document.getElementById('f').files,o=document.getElementById('o');"
             "for(const f of fs){o.textContent+=f.name+' ... ';try{const r=await fetch(encodeURIComponent(f.name),"
             "{method:'PUT',body:f});o.textContent+=r.status+' '+(await r.text())+'\\n';}catch(e){o.textContent+='failed\\n';}}"
             "setTimeout(()=>location.reload(),800);}")


class SvServer:
    def __init__(self, root, allow=None, token=None, upload=False, local_only=False,
                 minutes=DEFAULT_MINUTES, port=None, log=None, max_upload=MAX_UPLOAD):
        self.root = os.path.realpath(root)
        if not os.path.isdir(self.root):
            raise SvError("'%s' is not a folder" % root)
        self.allow = allow or (lambda path: True)
        self.token = token or make_token()
        self.upload = bool(upload)
        self.local_only = bool(local_only)
        self.minutes = max(1, min(int(minutes), MAX_MINUTES))
        self.port_wanted = port
        self.log = log or (lambda text: None)
        self.max_upload = max_upload
        self.httpd = None
        self.port = None
        self.started = None
        self.timer = None
        self.lock = threading.Lock()
        self.sem = threading.BoundedSemaphore(MAX_CONNECTIONS)
        self.bad = {}                                     # address -> [count, blocked until]
        self.stats = {"requests": 0, "sent": 0, "uploads": 0, "uploaded": 0, "wrong": 0}

    # ---------------------------------------------------------------- control
    def start(self):
        if self.httpd is not None:
            raise SvError("already running")
        handler = self._make_handler()
        host = "127.0.0.1" if self.local_only else "0.0.0.0"
        ports = [self.port_wanted] if self.port_wanted else list(PORTS)
        httpd, last = None, None
        for p in ports:
            try:
                httpd = _Server((host, p), handler)
                break
            except OSError as e:
                last = e
        if httpd is None:
            raise SvError("no free port (%s)" % (last.strerror if last and last.strerror else "busy"))
        self.httpd = httpd
        self.port = httpd.server_address[1]
        self.started = time.time()
        threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.3}, daemon=True).start()
        self.timer = threading.Timer(self.minutes * 60, self._time_up)
        self.timer.daemon = True
        self.timer.start()
        return self.urls()

    def _time_up(self):
        if self.stop():
            self.log("time is up, sharing stopped")

    def stop(self):
        with self.lock:
            httpd, self.httpd = self.httpd, None
        if httpd is None:
            return False
        if self.timer:
            self.timer.cancel()
        httpd.shutdown()
        httpd.server_close()
        return True

    def running(self):
        return self.httpd is not None

    def urls(self):
        hosts = ["127.0.0.1"] if self.local_only else (lan_ips() or ["127.0.0.1"])
        return ["http://%s:%d/%s/" % (h, self.port, self.token) for h in hosts]

    def status(self):
        left = max(0, int(self.started + self.minutes * 60 - time.time())) if self.started and self.running() else 0
        with self.lock:
            stats = dict(self.stats)
        return {"running": self.running(), "urls": self.urls() if self.port else [], "remaining": left,
                "root": self.root, "upload": self.upload, **stats}

    def _count(self, **kw):
        with self.lock:
            for k, v in kw.items():
                self.stats[k] += v

    # ---------------------------------------------------------------- inside root?
    def inside(self, real):
        return real == self.root or real.startswith(self.root + os.sep)

    def permitted(self, real):
        if not self.inside(real):
            return False
        return real == self.root or bool(self.allow(real))

    # ---------------------------------------------------------------- handler
    def _make_handler(self):
        srv = self

        class Handler(http.server.BaseHTTPRequestHandler):
            server_version = "hegd-sv"
            sys_version = ""
            protocol_version = "HTTP/1.0"                 # one request per connection: simple and safe
            timeout = 30

            def log_message(self, fmt, *args):
                pass

            def handle(self):
                if not srv.sem.acquire(blocking=False):
                    try:
                        self.wfile.write(b"HTTP/1.0 503 Busy\r\nContent-Length: 0\r\n\r\n")
                    except OSError:
                        pass
                    return
                try:
                    super().handle()
                except OSError:
                    pass
                finally:
                    srv.sem.release()

            # ---- small helpers
            def _head(self, code, ctype, length, extra=()):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(length))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                for k, v in extra:
                    self.send_header(k, v)
                self.end_headers()

            def _text(self, code, text, extra=()):
                body = text.encode("utf-8")
                self._head(code, "text/plain; charset=utf-8", len(body), extra)
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _note(self, code, parts):
                srv._count(requests=1)
                srv.log("%s /%s %d (%s)" % (self.command, clean("/".join(parts)), code, self.client_address[0]))

            def _wrong(self):
                """A request with a wrong code (or no code): it looks like a missing page."""
                ip = self.client_address[0]
                with srv.lock:
                    rec = srv.bad.setdefault(ip, [0, 0.0])
                    rec[0] += 1
                    srv.stats["wrong"] += 1
                    n = rec[0]
                    if n >= BAD_LIMIT and rec[1] < time.time():
                        rec[1] = time.time() + BLOCK_SECONDS
                        blocked = True
                    else:
                        blocked = False
                if blocked:
                    srv.log("%s tried a wrong code %d times: blocked for %d min" % (ip, n, BLOCK_SECONDS // 60))
                elif n <= 3:
                    srv.log("wrong address from %s" % ip)
                self._text(404, "Not found")

            def _is_blocked(self):
                with srv.lock:
                    rec = srv.bad.get(self.client_address[0])
                    return bool(rec and rec[1] > time.time())

            def _route(self):
                """-> (parts, ends_with_slash, query) or None (an answer was sent)."""
                if self._is_blocked():
                    self._text(429, "Too many wrong tries. Wait a few minutes.")
                    return None
                u = urllib.parse.urlsplit(self.path)
                segs = u.path.split("/")
                if len(segs) < 2 or segs[0] != "":
                    self._wrong()
                    return None
                try:
                    given = urllib.parse.unquote(segs[1], errors="strict").encode("utf-8")
                except (UnicodeDecodeError, ValueError):
                    self._wrong()
                    return None
                if not hmac.compare_digest(given, srv.token.encode("utf-8")):
                    self._wrong()
                    return None
                parts = []
                for raw in segs[2:]:
                    if raw == "":
                        continue
                    p = urllib.parse.unquote(raw)
                    if p in (".", "..") or "/" in p or "\\" in p or "\x00" in p or p.startswith("."):
                        self._text(404, "Not found")
                        return None
                    parts.append(p)
                return parts, u.path.endswith("/"), urllib.parse.parse_qs(u.query)

            def _resolve(self, parts):
                real = os.path.realpath(os.path.join(srv.root, *parts))
                return real if srv.permitted(real) else None

            # ---- GET / HEAD
            def do_HEAD(self):
                self._get()

            def do_GET(self):
                self._get()

            def _get(self):
                try:
                    routed = self._route()
                    if routed is None:
                        return
                    parts, slash, query = routed
                    real = self._resolve(parts)
                    if real is None or not os.path.exists(real):
                        self._note(404, parts)
                        return self._text(404, "Not found")
                    if os.path.isdir(real):
                        if not slash:
                            self._note(301, parts)
                            return self._text(301, "Moved", [("Location", "/%s/%s/" % (
                                srv.token, "/".join(urllib.parse.quote(p) for p in parts)))])
                        self._note(200, parts)
                        return self._listing(parts, real)
                    return self._file(parts, real, "dl" in query)
                except OSError:
                    pass
                except Exception:
                    try:
                        self._text(500, "Server error")
                    except OSError:
                        pass

            def _listing(self, parts, directory):
                rows, shown, hidden_more = [], 0, False
                try:
                    entries = sorted(os.scandir(directory), key=lambda e: (not e.is_dir(), e.name.lower()))
                except OSError:
                    return self._text(403, "Cannot read this folder")
                for e in entries:
                    if e.name.startswith("."):
                        continue
                    real = os.path.realpath(e.path)
                    if not srv.permitted(real):
                        continue
                    if shown >= MAX_LISTED:
                        hidden_more = True
                        break
                    shown += 1
                    isdir = os.path.isdir(real)
                    try:
                        size = "" if isdir else human(os.path.getsize(real))
                    except OSError:
                        size = "?"
                    href = urllib.parse.quote(e.name) + ("/" if isdir else "")
                    label = html.escape(e.name) + ("/" if isdir else "")
                    rows.append('<li><span class="s">%s</span><a href="%s">%s</a></li>' % (size, href, label))
                up_link = '<li><a href="../">../ (up)</a></li>' if parts else ""
                more = "<li>... more files are not listed</li>" if hidden_more else ""
                form = ""
                if srv.upload:
                    form = ('<p><input type="file" id="f" multiple><br><button onclick="up()">Upload here</button>'
                            '<pre id="o"></pre></p><script>%s</script>' % UPLOAD_JS)
                body = ('<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" '
                        'content="width=device-width,initial-scale=1"><title>%s</title><style>%s</style></head><body>'
                        '<h1>/%s</h1>%s<ul>%s%s%s</ul></body></html>'
                        % (html.escape("/" + "/".join(parts)), PAGE_CSS, html.escape("/".join(parts)), form,
                           up_link, "".join(rows), more)).encode("utf-8")
                csp = ("default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; "
                       "script-src %s; connect-src 'self'; form-action 'none'; base-uri 'none'"
                       % ("'unsafe-inline'" if srv.upload else "'none'"))
                self._head(200, "text/html; charset=utf-8", len(body), [("Content-Security-Policy", csp)])
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _file(self, parts, real, download):
                try:
                    size = os.path.getsize(real)
                    f = open(real, "rb")
                except OSError:
                    self._note(403, parts)
                    return self._text(403, "Cannot read this file")
                with f:
                    start, end, code = 0, size - 1, 200
                    extra = [("Accept-Ranges", "bytes")]
                    m = re.match(r"^bytes=(\d*)-(\d*)$", (self.headers.get("Range") or "").strip())
                    if m and (m.group(1) or m.group(2)) and size > 0:
                        if m.group(1):
                            start = int(m.group(1))
                            end = int(m.group(2)) if m.group(2) else size - 1
                        else:
                            start = max(0, size - int(m.group(2)))
                        end = min(end, size - 1)
                        if start >= size or start > end:
                            self._note(416, parts)
                            return self._text(416, "Bad range", [("Content-Range", "bytes */%d" % size)])
                        code = 206
                        extra.append(("Content-Range", "bytes %d-%d/%d" % (start, end, size)))
                    length = max(0, end - start + 1) if size else 0
                    ext = os.path.splitext(real)[1].lower()
                    ctype = "text/plain; charset=utf-8" if ext in AS_TEXT else (mimetypes.guess_type(real)[0] or "application/octet-stream")
                    name = os.path.basename(real)
                    extra.append(("Content-Disposition", "%s; filename*=UTF-8''%s" % (
                        "attachment" if download else "inline", urllib.parse.quote(name))))
                    self._note(code, parts)
                    self._head(code, ctype, length, extra)
                    if self.command == "HEAD" or not length:
                        return
                    f.seek(start)
                    left = length
                    while left > 0:
                        chunk = f.read(min(CHUNK, left))
                        if not chunk:
                            break
                        try:
                            self.wfile.write(chunk)
                        except OSError:
                            break                           # the browser stopped the download
                        left -= len(chunk)
                        srv._count(sent=len(chunk))

            # ---- PUT (upload)
            def do_PUT(self):
                tmp = None
                try:
                    routed = self._route()
                    if routed is None:
                        return
                    parts, slash, _q = routed
                    if not srv.upload:
                        self._note(405, parts)
                        return self._text(405, "Uploads are off")
                    if not parts or slash or not safe_name(parts[-1]):
                        self._note(400, parts)
                        return self._text(400, "Bad file name")
                    folder = self._resolve(parts[:-1])
                    if folder is None or not os.path.isdir(folder):
                        self._note(404, parts)
                        return self._text(404, "Folder not found")
                    target = os.path.join(folder, parts[-1])
                    if os.path.lexists(target):
                        self._note(409, parts)
                        return self._text(409, "A file with this name already exists")
                    try:
                        length = int(self.headers.get("Content-Length", ""))
                    except ValueError:
                        self._note(411, parts)
                        return self._text(411, "Content-Length is required")
                    if length < 0 or length > srv.max_upload:
                        self._note(413, parts)
                        return self._text(413, "Too big (limit %s)" % human(srv.max_upload))
                    tmp = os.path.join(folder, ".part-%s" % secrets.token_hex(6))
                    got = 0
                    with open(tmp, "wb") as out:
                        while got < length:
                            chunk = self.rfile.read(min(CHUNK, length - got))
                            if not chunk:
                                break
                            out.write(chunk)
                            got += len(chunk)
                    if got != length or os.path.lexists(target):
                        code, why = (400, "The upload was cut off") if got != length else (409, "A file with this name already exists")
                        os.remove(tmp)                    # remove the temporary file BEFORE answering
                        tmp = None
                        self._note(code, parts)
                        return self._text(code, why)
                    os.replace(tmp, target)
                    tmp = None
                    srv._count(uploads=1, uploaded=length)
                    self._note(201, parts)
                    self._text(201, "saved (%s)" % human(length))
                except OSError as e:
                    try:
                        self._text(507 if getattr(e, "errno", 0) == 28 else 500, "Cannot save the file")
                    except OSError:
                        pass
                except Exception:
                    try:
                        self._text(500, "Server error")
                    except OSError:
                        pass
                finally:
                    if tmp:
                        try:
                            os.remove(tmp)
                        except OSError:
                            pass

            def _not_allowed(self):
                routed = self._route()
                if routed is not None:
                    self._note(405, routed[0])
                    self._text(405, "Method not allowed", [("Allow", "GET, HEAD" + (", PUT" if srv.upload else ""))])

            do_POST = do_DELETE = do_PATCH = do_OPTIONS = _not_allowed

        return Handler
