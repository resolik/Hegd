"""sshkit v0.1 - SSH for Butter: run a command on a server and copy files, with keys (no passwords).

Inside Butter:
    ssh add NAME user@host[:port]      remember a server               ssh list      ssh remove NAME
    ssh key new [rsa|ecdsa] [--force]  make this phone's key           ssh key show  print its public line
    ssh trust NAME [FINGERPRINT]       check and remember the server's host key (see below)
    ssh NAME command                   run one command (one word in quotes = a whole command line)
    ssh NAME ls -la /tmp               several words are passed as separate, safely quoted arguments
    ssh upload NAME local remote       copy a file to the server       ssh download NAME remote local
    options after the command words:   --timeout N   (seconds, default 30)

First time with a server (done once):
  1. ssh key new                       then put the printed line into ~/.ssh/authorized_keys on the server
  2. ssh add pi pi@192.168.1.20
  3. ssh trust pi                      shows the server's fingerprint; on the server run
                                       ssh-keygen -lf /etc/ssh/ssh_host_*_key.pub   and compare
  4. ssh trust pi SHA256:...           (the same fingerprint) - only now it is remembered
  5. ssh pi uptime

Security: key login only, no passwords are stored or typed. A server is used only after its host key was
accepted in step 4; if the key ever changes, the connection is refused (possible attack or a reinstalled server).
The private key lives in the app's private folder, never in /sdcard.

This tool talks to the Java library JSch through pyjnius, so it works only in the Android build that has it
(buildozer.spec: android.gradle_dependencies = com.github.mwiede:jsch:0.2.22).
"""
import base64
import hashlib
import json
import os
import re
import shlex
import sys
import time

VERSION = "0.1.1"

NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,19}$")
HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.\-]{0,251}[A-Za-z0-9])?$|^\[[0-9A-Fa-f:]+\]$")
USER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-]{0,31}$")
RESERVED = ("add", "list", "remove", "key", "trust", "upload", "download", "help")
MAX_LINES = 2000
DEFAULT_TIMEOUT = 30


def say(text):
    print(text)


class SshError(Exception):
    pass


# ---------------------------------------------------------------- where things are kept
def data_dir():
    """The private folder of the app (SSHKIT_HOME for tests); created with owner-only rights."""
    base = os.environ.get("SSHKIT_HOME") or os.environ.get("ANDROID_PRIVATE") or os.path.expanduser("~")
    path = os.path.join(base, "sshkit" if os.environ.get("SSHKIT_HOME") or os.environ.get("ANDROID_PRIVATE") else ".butter_sshkit")
    os.makedirs(path, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def p(name):
    return os.path.join(data_dir(), name)


def load_hosts():
    try:
        with open(p("hosts.json"), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_hosts(hosts):
    tmp = p("hosts.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(hosts, f, indent=1)
    os.replace(tmp, p("hosts.json"))


def known_hosts_name(host, port):
    return host if port == 22 else "[%s]:%d" % (host, port)


def fingerprint(key_b64):
    """OpenSSH style SHA256 fingerprint of a base64 public host key."""
    digest = hashlib.sha256(base64.b64decode(key_b64)).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


# ---------------------------------------------------------------- the Java side (all pyjnius calls live here)
class JschBackend:
    """The only place that touches Java. Tests replace it with a fake that has the same methods."""

    def __init__(self):
        try:
            from jnius import autoclass
        except ImportError:
            raise SshError("SSH needs the Android build with the JSch library (pyjnius is not available here). "
                           "See buildozer.spec: android.gradle_dependencies = com.github.mwiede:jsch:0.2.22")
        self.autoclass = autoclass
        try:
            self.JSch = autoclass("com.jcraft.jsch.JSch")
        except Exception as e:
            raise SshError("The JSch library is not in this build (%s: %s). Add to buildozer.spec: "
                           "android.gradle_dependencies = com.github.mwiede:jsch:0.2.22  and build again."
                           % (type(e).__name__, e))
    def _session(self, user, host, port, key_path, known_hosts, strict, timeout):
        jsch = self.JSch()
        if key_path:
            jsch.addIdentity(key_path)
        if known_hosts:
            jsch.setKnownHosts(known_hosts)
        session = jsch.getSession(user, host, port)
        session.setConfig("StrictHostKeyChecking", "yes" if strict else "no")
        session.setConfig("PreferredAuthentications", "publickey")
        session.setTimeout(int(timeout * 1000))
        return jsch, session

    def keygen(self, kind, private_path, public_path, comment):
        KeyPair = self.autoclass("com.jcraft.jsch.KeyPair")
        jsch = self.JSch()
        pair = KeyPair.genKeyPair(jsch, KeyPair.RSA, 3072) if kind == "rsa" else KeyPair.genKeyPair(jsch, KeyPair.ECDSA, 256)
        try:
            pair.writePrivateKey(private_path)
            pair.writePublicKey(public_path, comment)
        finally:
            pair.dispose()

    def fetch_host_key(self, user, host, port, timeout):
        """Connect only far enough to learn the host key -> (type, base64 key). Nothing is sent that could log in."""
        jsch, session = self._session(user, host, port, None, None, False, timeout)
        try:
            try:
                session.connect(int(timeout * 1000))
            except Exception:
                pass                                         # authentication fails on purpose: we only wanted the key
            hk = session.getHostKey()
            if hk is None:
                raise SshError("Could not reach %s:%d (no host key received)." % (host, port))
            return str(hk.getType()), str(hk.getKey())
        finally:
            session.disconnect()

    def run(self, user, host, port, key_path, known_hosts, command, timeout, emit):
        """Run one command; emit(line, is_error) gets the output lines as they arrive. -> exit status"""
        jsch, session = self._session(user, host, port, key_path, known_hosts, True, timeout)
        channel = None
        try:
            session.connect(int(timeout * 1000))
            channel = session.openChannel("exec")
            channel.setCommand(command)
            err_stream = self.autoclass("java.io.ByteArrayOutputStream")()
            channel.setErrStream(err_stream)
            reader = self.autoclass("java.io.BufferedReader")(
                self.autoclass("java.io.InputStreamReader")(channel.getInputStream(), "UTF-8"))
            channel.connect(int(timeout * 1000))
            deadline = time.time() + timeout
            while True:
                if reader.ready():
                    emit(str(reader.readLine()), False)
                    deadline = time.time() + timeout           # output keeps the command alive
                elif channel.isClosed():
                    while reader.ready():
                        emit(str(reader.readLine()), False)
                    break
                elif time.time() > deadline:
                    raise SshError("The command did not finish in %d s (raise it with --timeout N)." % timeout)
                else:
                    time.sleep(0.05)                           # python code runs here, so 'cancel' can stop us
            for line in str(err_stream.toString("UTF-8")).splitlines():
                emit(line, True)
            return int(channel.getExitStatus())
        finally:
            if channel is not None:
                channel.disconnect()
            session.disconnect()

    def sftp(self, user, host, port, key_path, known_hosts, action, local, remote, timeout):
        jsch, session = self._session(user, host, port, key_path, known_hosts, True, timeout)
        channel = None
        try:
            session.connect(int(timeout * 1000))
            channel = session.openChannel("sftp")
            channel.connect(int(timeout * 1000))
            if action == "put":
                channel.put(local, remote)
            else:
                channel.get(remote, local)
        finally:
            if channel is not None:
                channel.disconnect()
            session.disconnect()


BACKEND = None


def backend():
    global BACKEND
    if BACKEND is None:
        BACKEND = JschBackend()
    return BACKEND


# ---------------------------------------------------------------- commands
def parse_target(text):
    m = re.match(r"^([^@\s]+)@(.+?)(?::(\d{1,5}))?$", text)
    if not m or (":" in m.group(2) and not m.group(2).startswith("[")):
        raise SshError("Write it as user@host or user@host:port  (got '%s')." % text)
    user, host, port = m.group(1), m.group(2).lower(), int(m.group(3) or 22)
    if not USER_RE.match(user):
        raise SshError("'%s' is not a valid user name." % user)
    if not HOST_RE.match(host):
        raise SshError("'%s' is not a valid host name or address." % host)
    if not 1 <= port <= 65535:
        raise SshError("Port must be 1..65535.")
    return user, host, port


def get_host(name):
    hosts = load_hosts()
    if name not in hosts:
        raise SshError("There is no server '%s'. Add it: ssh add %s user@host   (see: ssh list)" % (name, name))
    return hosts[name]


def need_key():
    if not os.path.isfile(p("id_butter")):
        raise SshError("This phone has no SSH key yet. Make one: ssh key new")
    return p("id_butter")


def need_trusted(name, entry):
    if not entry.get("trusted"):
        raise SshError("The host key of '%s' is not accepted yet. Run: ssh trust %s" % (name, name))
    return p("known_hosts")


def cmd_add(args):
    if len(args) != 2:
        raise SshError("Usage: ssh add NAME user@host[:port]")
    name = args[0]
    if not NAME_RE.match(name) or name in RESERVED:
        raise SshError("Server names: a letter first, then letters, digits, _ or -, up to 20; not one of: " + " ".join(RESERVED))
    user, host, port = parse_target(args[1])
    hosts = load_hosts()
    replaced = name in hosts
    hosts[name] = {"user": user, "host": host, "port": port, "trusted": False}
    save_hosts(hosts)
    say("[SUCCESS] %s '%s' = %s@%s:%d.%s" % ("Updated" if replaced else "Saved", name, user, host, port,
                                           " Its host key must be accepted again: ssh trust %s" % name if replaced else
                                           " Next: ssh trust %s" % name))


def cmd_list(args):
    hosts = load_hosts()
    if not hosts:
        say("[INFO] No servers yet. Add one: ssh add pi pi@192.168.1.20")
        return
    for name in sorted(hosts):
        h = hosts[name]
        say("%s: %s@%s:%d  %s" % (name, h["user"], h["host"], h["port"], "trusted" if h.get("trusted") else "NOT trusted yet"))


def cmd_remove(args):
    if len(args) != 1:
        raise SshError("Usage: ssh remove NAME")
    hosts = load_hosts()
    if args[0] not in hosts:
        raise SshError("There is no server '%s'." % args[0])
    entry = hosts.pop(args[0])
    save_hosts(hosts)
    try:                                                      # forget its host key line too
        kh = known_hosts_name(entry["host"], entry["port"])
        with open(p("known_hosts"), encoding="utf-8") as f:
            lines = [ln for ln in f if ln.split(" ", 1)[0] != kh]
        with open(p("known_hosts"), "w", encoding="utf-8") as f:
            f.writelines(lines)
    except OSError:
        pass
    say("[SUCCESS] Server '%s' removed." % args[0])


def cmd_key(args):
    sub = args[0] if args else ""
    if sub == "show":
        try:
            with open(p("id_butter.pub"), encoding="utf-8") as f:
                say("[INFO] Put this line into ~/.ssh/authorized_keys on the server:")
                say(f.read().strip())
        except OSError:
            raise SshError("This phone has no SSH key yet. Make one: ssh key new")
        return
    if sub != "new":
        raise SshError("Usage: ssh key new [rsa|ecdsa] [--force]   |   ssh key show")
    rest = args[1:]
    force = "--force" in rest
    kinds = [a for a in rest if a != "--force"]
    kind = kinds[0] if kinds else "ecdsa"
    if kind not in ("rsa", "ecdsa") or len(kinds) > 1:
        raise SshError("Key type: ecdsa (fast, default) or rsa (3072 bits, takes longer).")
    if os.path.exists(p("id_butter")) and not force:
        raise SshError("This phone already has a key. Show it: ssh key show   (a new one replaces it: ssh key new --force)")
    backend().keygen(kind, p("id_butter"), p("id_butter.pub"), "butter@phone")
    try:
        os.chmod(p("id_butter"), 0o600)
    except OSError:
        pass
    say("[SUCCESS] Key made (%s). The private part never leaves this app." % kind)
    cmd_key(["show"])


def cmd_trust(args):
    if len(args) not in (1, 2):
        raise SshError("Usage: ssh trust NAME   (shows the fingerprint)   |   ssh trust NAME SHA256:...   (accepts it)")
    name = args[0]
    hosts = load_hosts()
    entry = get_host(name)
    key_type, key = backend().fetch_host_key(entry["user"], entry["host"], entry["port"], DEFAULT_TIMEOUT)
    fp = fingerprint(key)
    if len(args) == 1:
        say("[INFO] %s presents a %s key:" % (name, key_type))
        say(fp)
        say("[INFO] Compare with the server: ssh-keygen -lf /etc/ssh/ssh_host_*_key.pub")
        say("[INFO] If one line matches, run:  ssh trust %s %s" % (name, fp))
        return
    if args[1] != fp:
        raise SshError("The fingerprint does NOT match what the server presents (%s). Nothing was saved. "
                       "Is this the right server?" % fp)
    line = "%s %s %s\n" % (known_hosts_name(entry["host"], entry["port"]), key_type, key)
    try:
        with open(p("known_hosts"), encoding="utf-8") as f:
            others = [ln for ln in f if ln.split(" ", 1)[0] != known_hosts_name(entry["host"], entry["port"])]
    except OSError:
        others = []
    with open(p("known_hosts"), "w", encoding="utf-8") as f:
        f.writelines(others + [line])
    hosts[name]["trusted"] = True
    save_hosts(hosts)
    say("[SUCCESS] Host key of '%s' accepted. Try: ssh %s uptime" % (name, name))


def split_timeout(args):
    args = list(args)
    timeout = DEFAULT_TIMEOUT
    if "--timeout" in args:
        i = args.index("--timeout")
        try:
            timeout = int(args[i + 1])
            if not 1 <= timeout <= 3600:
                raise ValueError
        except (IndexError, ValueError):
            raise SshError("--timeout needs a number of seconds (1..3600).")
        del args[i:i + 2]
    return args, timeout


def cmd_exec(name, words):
    words, timeout = split_timeout(words)
    if not words:
        raise SshError("Usage: ssh %s command   (for example: ssh %s uptime)" % (name, name))
    entry = get_host(name)
    key, known = need_key(), need_trusted(name, entry)
    command = words[0] if len(words) == 1 else " ".join(shlex.quote(w) for w in words)
    count = [0]

    def emit(line, is_error):
        count[0] += 1
        if count[0] <= MAX_LINES:
            say(("[ERROR] " + line) if is_error else line)
        elif count[0] == MAX_LINES + 1:
            say("[WARNING] Output cut after %d lines." % MAX_LINES)

    status = backend().run(entry["user"], entry["host"], entry["port"], key, known, command, timeout, emit)
    say("[INFO] exit status %d" % status)
    if status != 0:
        raise SshError("The command failed on the server (exit status %d)." % status)


def cmd_transfer(action, args):
    args, timeout = split_timeout(args)
    usage = "ssh upload NAME local remote" if action == "put" else "ssh download NAME remote local"
    if len(args) != 3:
        raise SshError("Usage: " + usage)
    name, first, second = args
    local, remote = (first, second) if action == "put" else (second, first)
    if action == "put" and not os.path.isfile(local):
        raise SshError("'%s' is not a file here." % local)
    if action == "get" and os.path.exists(local) and os.path.isdir(local):
        raise SshError("'%s' is a folder; give the new file name." % local)
    entry = get_host(name)
    key, known = need_key(), need_trusted(name, entry)
    backend().sftp(entry["user"], entry["host"], entry["port"], key, known, action, local, remote, timeout)
    say("[SUCCESS] %s %s." % ("Uploaded" if action == "put" else "Downloaded", local if action == "put" else local))


def run_command(args):
    if not args or args[0] in ("help", "-h", "--help"):
        say("sshkit %s - see the top of sshkit.py. Start: ssh key new, ssh add NAME user@host, ssh trust NAME" % VERSION)
        return
    sub, rest = args[0], args[1:]
    if sub == "add":
        cmd_add(rest)
    elif sub == "list":
        cmd_list(rest)
    elif sub == "remove":
        cmd_remove(rest)
    elif sub == "key":
        cmd_key(rest)
    elif sub == "trust":
        cmd_trust(rest)
    elif sub == "upload":
        cmd_transfer("put", rest)
    elif sub == "download":
        cmd_transfer("get", rest)
    else:
        cmd_exec(sub, rest)


def main(argv):
    try:
        run_command(argv[1:])
    except SshError as e:
        say("[ERROR] %s" % e)
        return 1
    except Exception as e:                                    # Java errors arrive here too
        text = str(e)
        if "UnknownHostKey" in text or "HostKey has been changed" in text or "reject HostKey" in text:
            text = "The server's host key does not match the one you accepted. Possible attack, or the server was " \
                   "reinstalled. If you are sure: ssh remove NAME, add it again and run ssh trust."
        elif "Auth fail" in text or "Auth cancel" in text:
            text = "The server refused the key. Is this phone's key in ~/.ssh/authorized_keys there? (ssh key show)"
        elif "timeout" in text.lower() or "UnknownHost" in text or "Connection refused" in text:
            text = "Cannot reach the server: " + text
        say("[ERROR] %s" % text)
        return 1
    return 0


if __name__ == "__main__":
    try:
        rc = main(sys.argv)
    except KeyboardInterrupt:
        say("[ERROR] stopped")
        rc = 1
    try:
        sys.stdout.flush()
    except Exception:
        pass
    if rc:                                                    # exit only on failure (see asm.py for why)
        sys.exit(rc)
