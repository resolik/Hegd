"""net.py v1.0 - small network tools for Hegd: ip, dns, port, ping, trace.

    py net.py ip [public]                 addresses of this phone ("public" asks api.ipify.org for the outside one)
    py net.py dns NAME [TYPE] [-s google] what NAME is: without TYPE the phone's own resolver answers (A and AAAA);
                                          with TYPE (A AAAA MX TXT NS CNAME SOA PTR) a DNS-over-HTTPS server answers
    py net.py dns 8.8.8.8                 reverse lookup of an address
    py net.py port HOST 80,443,8000-8010 [-t 2] [--yes]     which TCP ports answer
    py net.py ping HOST [-c 4] [-t 2] [--tcp] [-p 443]      round-trip time (the system ping program if there is one)
    py net.py trace HOST [-m 20] [--raw]                    the route to HOST, hop by hop

Limits of a phone without root: raw network packets cannot be made from Python, so  ping  and  trace  use the
system "ping" program (Android has one); if it does not work, ping falls back to timing a TCP connection
(and says so), trace cannot work without it.
Safe by design: names are checked before they reach any program, programs are started without a shell,
a port scan of a PUBLIC address is limited (20 ports; more needs --yes; never more than 100).
"""
import concurrent.futures
import ipaddress
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

VERSION = "1.0"
UA = "hegd-net/" + VERSION
PUBLIC_IP_URL = "https://api.ipify.org"
DOH = {"cloudflare": "https://cloudflare-dns.com/dns-query", "google": "https://dns.google/resolve"}
DNS_TYPES = ("A", "AAAA", "CNAME", "MX", "NS", "PTR", "SOA", "TXT")
STATUS = {1: "FORMERR (the question was malformed)", 2: "SERVFAIL (the server could not answer)",
          3: "NXDOMAIN (the name does not exist)", 5: "REFUSED"}
MAX_PORTS = 100
FREE_PUBLIC_PORTS = 20
MAX_PING = 20
MAX_HOPS = 30


class NetError(Exception):
    pass


def say(text):
    print(text)


# ---------------------------------------------------------------------- checks

def valid_host(host):
    """A name or an address that is safe to hand to a program, or None."""
    if not isinstance(host, str) or not 1 <= len(host) <= 253 or host[0] in "-.":
        return None
    try:
        return str(ipaddress.ip_address(host.strip("[]")))
    except ValueError:
        pass
    try:
        name = host.encode("idna").decode("ascii").rstrip(".")
    except UnicodeError:
        return None
    if not name or any(not re.match(r"^[A-Za-z0-9_]([A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?$", part) for part in name.split(".")):
        return None
    return name


def need_host(text):
    host = valid_host(text)
    if host is None:
        raise NetError("'%s' is not a valid name or address" % str(text)[:40])
    return host


def parse_ports(spec):
    ports = set()
    for part in spec.split(","):
        m = re.match(r"^(\d{1,5})(?:-(\d{1,5}))?$", part.strip())
        if not m:
            raise NetError("bad port list '%s'. Example: 80,443,8000-8010" % spec[:40])
        a = int(m.group(1))
        b = int(m.group(2)) if m.group(2) else a
        if not (1 <= a <= 65535 and 1 <= b <= 65535 and a <= b):
            raise NetError("ports must be 1-65535, and a range must go up")
        if b - a + 1 > MAX_PORTS:
            raise NetError("at most %d ports at a time" % MAX_PORTS)
        ports.update(range(a, b + 1))
    if len(ports) > MAX_PORTS:
        raise NetError("at most %d ports at a time" % MAX_PORTS)
    return sorted(ports)


def is_private(ip):
    a = ipaddress.ip_address(ip)
    return a.is_private or a.is_loopback or a.is_link_local


def parse_opts(args, valued=(), flags=()):
    pos, opts, flg = [], {}, set()
    i = 0
    while i < len(args):
        a = args[i]
        if a in valued:
            if i + 1 >= len(args):
                raise NetError("%s needs a value" % a)
            opts[a] = args[i + 1]
            i += 2
            continue
        if a in flags:
            flg.add(a)
        elif a.startswith("-") and len(a) > 1 and not a[1:].isdigit():
            raise NetError("unknown option %s" % a[:20])
        else:
            pos.append(a)
        i += 1
    return pos, opts, flg


def num(opts, key, default, low, high, what):
    try:
        value = float(opts.get(key, default))
    except ValueError:
        raise NetError("%s: a number from %s to %s" % (what, low, high))
    if not low <= value <= high:
        raise NetError("%s: a number from %s to %s" % (what, low, high))
    return value


# ------------------------------------------------------------------------ network

def _ctx():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def https_get(url, headers=None, max_bytes=1024 * 1024):
    req = urllib.request.Request(url, headers=dict({"User-Agent": UA}, **(headers or {})))
    try:
        with urllib.request.urlopen(req, timeout=10, context=_ctx()) as r:
            data = r.read(max_bytes + 1)
    except urllib.error.HTTPError as e:
        raise NetError("the server answered HTTP %s" % e.code)
    except urllib.error.URLError as e:
        raise NetError("no connection (%s). Check Wi-Fi or mobile data." % (e.reason,))
    except (socket.timeout, TimeoutError):
        raise NetError("the server did not answer in time")
    if len(data) > max_bytes:
        raise NetError("the answer is too big")
    return data


def local_ips():
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.2)
        s.connect(("10.255.255.255", 1))             # nothing is sent: it only shows which address would be used
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


def public_ip():
    text = https_get(PUBLIC_IP_URL, max_bytes=100).decode("utf-8", "replace").strip()
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        raise NetError("api.ipify.org answered something that is not an address")


def resolve_ip(host):
    """-> one address of the host (IPv4 preferred)."""
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise NetError("cannot resolve '%s' (no such name, or no connection)" % host[:60])
    infos.sort(key=lambda i: i[0] != socket.AF_INET)
    return infos[0][4][0]


def tcp_check(ip, port, timeout):
    """-> ('open' | 'closed' | 'filtered' | 'unreachable', milliseconds or None)"""
    start = time.time()
    try:
        s = socket.create_connection((ip, port), timeout)
        s.close()
        return "open", (time.time() - start) * 1000
    except ConnectionRefusedError:
        return "closed", (time.time() - start) * 1000
    except (socket.timeout, TimeoutError):
        return "filtered", None
    except OSError:
        return "unreachable", None


def find_ping():
    return shutil.which("ping") or ("/system/bin/ping" if os.path.exists("/system/bin/ping") else None)


# -------------------------------------------------------------------------- ip, dns

def cmd_ip(args):
    pos, _, _ = parse_opts(args)
    if len(pos) > 1 or (pos and pos[0] != "public"):
        say("usage: net ip [public]")
        return
    ips = local_ips()
    say("local   " + (", ".join(ips) if ips else "none found (no network?)"))
    if pos:
        say("public  %s   (asked api.ipify.org)" % public_ip())


def doh_query(name, rtype, server):
    url = DOH[server] + "?" + urllib.parse.urlencode({"name": name, "type": rtype})
    raw = https_get(url, {"Accept": "application/dns-json"})
    try:
        data = json.loads(raw.decode("utf-8"))
        return int(data.get("Status", 0)), list(data.get("Answer") or [])
    except (ValueError, TypeError, AttributeError):
        raise NetError("the DNS server answered something that is not understood")


def cmd_dns(args):
    pos, opts, _ = parse_opts(args, valued=("-s",))
    if not 1 <= len(pos) <= 2 or opts.get("-s", "cloudflare") not in DOH:
        say("usage: net dns NAME [TYPE] [-s cloudflare|google]     types: %s" % " ".join(DNS_TYPES))
        return
    name = need_host(pos[0])
    rtype = pos[1].upper() if len(pos) == 2 else None
    if rtype is not None and rtype not in DNS_TYPES:
        raise NetError("type: one of %s" % " ".join(DNS_TYPES))
    is_ip = False
    try:
        ipaddress.ip_address(name)
        is_ip = True
    except ValueError:
        pass
    if rtype is None:
        if is_ip:
            try:
                host = socket.gethostbyaddr(name)[0]
            except (socket.herror, socket.gaierror, OSError):
                raise NetError("no name is known for %s (the phone's resolver has none)" % name)
            say("PTR   %s   (the phone's resolver)" % host)
            return
        try:
            infos = socket.getaddrinfo(name, None, type=socket.SOCK_STREAM)
        except socket.gaierror:
            raise NetError("cannot resolve '%s' (no such name, or no connection)" % name[:60])
        seen = []
        for fam, _, _, _, addr in infos:
            if addr[0] not in seen:
                seen.append(addr[0])
                say("%-5s %s" % ("A" if fam == socket.AF_INET else "AAAA", addr[0]))
        say("[INFO] This is what the phone's own resolver says. For other record types: net dns %s MX" % name)
        return
    query = ipaddress.ip_address(name).reverse_pointer if (is_ip and rtype == "PTR") else name
    first = opts.get("-s", "cloudflare")
    order = [first] + ([s for s in DOH if s != first] if "-s" not in opts else [])
    last = None
    for server in order:
        try:
            status, answers = doh_query(query, rtype, server)
            break
        except NetError as e:
            last = e
    else:
        raise last
    if status != 0:
        raise NetError("%s" % STATUS.get(status, "the server answered with status %d" % status))
    if not answers:
        say("[INFO] No %s records for %s (asked %s)." % (rtype, name, server))
        return
    for a in answers:
        say("%-6s ttl %-6s %s" % (rtype if a.get("type") else "?", a.get("TTL", "?"), str(a.get("data", ""))[:200]))
    say("[INFO] Asked %s over HTTPS." % server)


# ------------------------------------------------------------------------- port

def cmd_port(args):
    pos, opts, flags = parse_opts(args, valued=("-t",), flags=("--yes",))
    if len(pos) != 2:
        say("usage: net port HOST 80,443,8000-8010 [-t 2] [--yes]")
        return
    host = need_host(pos[0])
    ports = parse_ports(pos[1])
    timeout = num(opts, "-t", 2, 0.2, 10, "-t")
    ip = resolve_ip(host)
    if not is_private(ip) and len(ports) > FREE_PUBLIC_PORTS and "--yes" not in flags:
        say("[ERROR] %s is a public address and you ask for %d ports. Scanning many ports of someone else's server "
            "is not welcome (and gets you blocked). Add --yes if it is yours." % (ip, len(ports)))
        return
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda p: tcp_check(ip, p, timeout), ports))
    say("%s (%s)" % (host, ip))
    counts = {}
    for p, (state, ms) in zip(ports, results):
        counts[state] = counts.get(state, 0) + 1
        if len(ports) <= FREE_PUBLIC_PORTS or state == "open":
            try:
                service = socket.getservbyport(p, "tcp")
            except OSError:
                service = "-"
            say("%5d  %-8s %-12s %s" % (p, service[:8], state, ("%.0f ms" % ms) if ms is not None else ""))
    say("[INFO] %s." % ", ".join("%d %s" % (n, s) for s, n in sorted(counts.items())))
    if counts.get("filtered"):
        say("[INFO] 'filtered' = no answer within %s s: a firewall, or nothing there." % timeout)


# ------------------------------------------------------------------- ping, trace

REPLY = re.compile(r"bytes from (\S+)(?: \(([0-9a-fA-F:.]+)\))?.*?time[=<]\s*([\d.]+)\s*ms")
SUMMARY = re.compile(r"(\d+) packets transmitted, (\d+) (?:packets )?received")


def parse_reply(line):
    """'64 bytes from 8.8.8.8: ... time=12.3 ms' and 'bytes from name (8.8.8.8): ...' -> (address, milliseconds) or None"""
    m = REPLY.search(line)
    return (m.group(2) or m.group(1).rstrip(":"), float(m.group(3))) if m else None


def summarize(times, sent, how):
    got = len(times)
    lost = 100 * (sent - got) // sent if sent else 0
    say("[INFO] sent %d, received %d, lost %d%% (%s)" % (sent, got, lost, how))
    if times:
        say("[INFO] min/avg/max = %.1f/%.1f/%.1f ms" % (min(times), sum(times) / got, max(times)))


def ping_system(binary, host, count, timeout):
    """Run the system ping, show replies as they come. -> list of times, or None if the program did not work."""
    proc = subprocess.Popen([binary, "-c", str(count), "-W", str(int(max(1, timeout))), host],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    times, text, sent = [], [], None
    try:
        for line in proc.stdout:
            text.append(line)
            r = parse_reply(line)
            if r:
                times.append(r[1])
                say("  reply from %s: %g ms" % r)
            s = SUMMARY.search(line)
            if s:
                sent = int(s.group(1))
        proc.wait(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()
    out = "".join(text).lower()
    if not times and any(w in out for w in ("unknown host", "name or service not known", "bad address", "cannot resolve", "temporary failure")):
        raise NetError("cannot resolve '%s'" % host[:60])
    if not times and any(w in out for w in ("not permitted", "permission denied", "operation not supported", "socket:", "no such file")):
        return None
    return times, (sent if sent is not None else count)


def cmd_ping(args):
    pos, opts, flags = parse_opts(args, valued=("-c", "-t", "-p"), flags=("--tcp",))
    if len(pos) != 1:
        say("usage: net ping HOST [-c 4] [-t 2] [--tcp] [-p 443]")
        return
    host = need_host(pos[0])
    count = int(num(opts, "-c", 4, 1, MAX_PING, "-c"))
    timeout = num(opts, "-t", 2, 0.2, 10, "-t")
    port = int(num(opts, "-p", 443, 1, 65535, "-p"))
    binary = None if "--tcp" in flags else find_ping()
    if binary:
        res = ping_system(binary, host, count, timeout)
        if res is not None:
            times, sent = res
            summarize(times, sent, "system ping")
            return
        say("[WARNING] The system ping program does not work for apps on this phone: timing TCP connections instead.")
    elif "--tcp" not in flags:
        say("[WARNING] No system ping program found: timing TCP connections instead.")
    ip = resolve_ip(host)
    times = []
    for i in range(count):
        state, ms = tcp_check(ip, port, timeout)
        if state in ("open", "closed"):                # a refusal also proves the host answered
            times.append(ms)
            say("  %s:%d %s: %.1f ms" % (ip, port, state, ms))
        else:
            say("  %s:%d no answer (%s)" % (ip, port, state))
        if i + 1 < count:
            time.sleep(0.5)
    summarize(times, count, "TCP connect to port %d, not ICMP" % port)


def ping_once(binary, host, ttl, timeout):
    """One probe with a limited life time. -> (text of the answer, milliseconds)"""
    start = time.time()
    try:
        r = subprocess.run([binary, "-c", "1", "-t", str(ttl), "-W", str(int(max(1, timeout))), host],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, timeout=timeout + 4)
        text = r.stdout or ""
    except subprocess.TimeoutExpired:
        text = ""
    return text, (time.time() - start) * 1000


def parse_hop(text):
    """-> ('reached' | 'hop' | 'none', address). Both 'from 1.2.3.4' and 'from name (1.2.3.4)' are understood."""
    m = re.search(r"bytes from (\S+)(?: \(([0-9a-fA-F:.]+)\))?", text)
    if m:
        return "reached", m.group(2) or m.group(1).rstrip(":")
    if "exceed" in text.lower():
        m = re.search(r"[Ff]rom\s+(\S+)(?: \(([0-9a-fA-F:.]+)\))?", text)
        if m:
            return "hop", m.group(2) or m.group(1).rstrip(":")
    return "none", None


def cmd_trace(args):
    pos, opts, flags = parse_opts(args, valued=("-m",), flags=("--raw",))
    if len(pos) != 1:
        say("usage: net trace HOST [-m 20] [--raw]")
        return
    host = need_host(pos[0])
    maxh = int(num(opts, "-m", 20, 1, MAX_HOPS, "-m"))
    binary = find_ping()
    if not binary:
        raise NetError("trace needs the system ping program, and this phone has none for apps")
    ip = resolve_ip(host)
    say("route to %s (%s), at most %d hops. Times are approximate." % (host, ip, maxh))
    stars = 0
    for ttl in range(1, maxh + 1):
        text, ms = ping_once(binary, host, ttl, 2)
        if "--raw" in flags:
            say("--- ttl %d ---\n%s" % (ttl, text.strip()[:300]))
        kind, addr = parse_hop(text)
        if kind == "none":
            stars += 1
            say("%2d  *" % ttl)
            if stars >= 5:
                say("[INFO] No answer from 5 hops in a row: the rest of the route is hidden or blocked.")
                return
            continue
        stars = 0
        say("%2d  %-39s %6.0f ms" % (ttl, addr, ms))
        if kind == "reached":
            say("[SUCCESS] Reached %s in %d hops." % (host, ttl))
            return
    say("[INFO] Not reached within %d hops (try -m 30)." % maxh)


COMMANDS = {"ip": cmd_ip, "dns": cmd_dns, "port": cmd_port, "ping": cmd_ping, "trace": cmd_trace, "traceroute": cmd_trace}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in COMMANDS:
        say("net %s - network tools" % VERSION)
        say("commands: ip [public] | dns NAME [TYPE] | port HOST PORTS | ping HOST | trace HOST")
        return
    try:
        COMMANDS[argv[0]](argv[1:])
    except NetError as e:
        say("[ERROR] " + str(e))
    except Exception:
        say("[CRITICAL] net crashed:")
        say(traceback.format_exc()[-800:])


if __name__ != "net":
    main()
