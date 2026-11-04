"""ข (sandbox) — the one way out of a Claude Code job's sandbox: a CONNECT proxy for an allow-list of hosts.

Why a proxy (the probes of 2026-11-01): Claude Code needs its API, so its sandbox cannot simply have no network; but sharing the host's
network (``network: host``) also opens every service on the host's localhost — the dtree server and ultimateTruth's dashboard take
localhost writes without a token — and the whole internet. No user-mode network stack (slirp4netns, pasta) is installed here. So the
job keeps a network namespace of its own (only loopback) and gets one door:

- in the runner, outside the sandbox, a thread listens on a unix socket in a per-job directory and tunnels ``CONNECT host:port`` to the
  hosts on the allow-list only: ``host`` exact or ``.suffix`` (the domain and every name under it), port 443 unless the entry names
  one (``host:8443``). Anything else is answered 403. A name that resolves to a loopback, link-local or unspecified address is refused
  too, unless the entry is that address or ``localhost`` itself;
- inside, a tiny forwarder (``/usr/bin/python3 -I fwd.py <socket> <port>``, started by the shell before claude) listens on
  127.0.0.1:<port> and pipes each connection to the socket, whose directory is bound into the sandbox;
- Claude Code gets ``HTTPS_PROXY`` / ``HTTP_PROXY`` = ``http://127.0.0.1:<port>``, and so do the commands it runs: they reach the same
  hosts and nothing else (a project whose jobs ``pip install`` adds pypi.org and files.pythonhosted.org).

Only CONNECT goes out (https, or any TLS); a plain-http request through the proxy is refused. Every host asked for is counted
(allowed, denied, failed) and logged once per job; the counts go into the job's result (``egress``).
"""
from __future__ import annotations

import ipaddress
import os
import re
import shutil
import socket
import tempfile
import threading
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlsplit

#: what Claude Code itself needs: the API, and the OAuth token endpoint (TOKEN_URL in the 2.1.314 binary) for a token refresh
CLAUDE_HOSTS = ("api.anthropic.com", "platform.claude.com")
#: the forwarder's port inside the sandbox (the sandbox's own loopback: no clash with the host or another job)
PORT = 3128
MAX_HEAD = 16384
_NAME = re.compile(r"^[a-z0-9_]([a-z0-9_-]{0,62})(\.[a-z0-9_]([a-z0-9_-]{0,62}))*$")
_ENTRY = re.compile(r"^(\.?)(\[[0-9a-f:.]+\]|[^:\[\]]+)(?::(\d{1,5}))?$")
_CONNECT = re.compile(r"^(\[[0-9A-Fa-f:.]+\]|[^:/\[\]\s]+):(\d{1,5})$")

#: (name, suffix, port): ``suffix`` matches the name itself and every name under it
Rule = Tuple[str, bool, int]


def _ip(name: str) -> Optional[str]:
    try:
        return str(ipaddress.ip_address(name))
    except ValueError:
        return None


def parse(entries: Iterable[Any]) -> List[Rule]:
    """``network_allow`` entries → rules: ``host``, ``.suffix``, an IP address (``[::1]`` for IPv6), each with ``:port`` or 443."""
    out: List[Rule] = []
    for e in entries or []:
        s = str(e).strip().lower()
        if not s:
            continue
        m = _ENTRY.match(s)
        if not m:
            raise ValueError("network_allow: %r is not host, .suffix or either with :port" % (e,))
        suffix, name, port = bool(m.group(1)), m.group(2).strip("[]").rstrip("."), int(m.group(3) or 443)
        ip = _ip(name)
        if not 0 < port < 65536 or (ip is None and not _NAME.match(name)) or (suffix and ip is not None):
            raise ValueError("network_allow: %r is not a host name or an address" % (e,))
        out.append((ip or name, suffix, port))
    return out


def match(rules: List[Rule], host: str, port: int) -> Optional[Rule]:
    """The rule that lets ``host:port`` through, or None."""
    h = host.lower().rstrip(".")
    h = _ip(h) or h
    for r in rules:
        name, suffix, p = r
        if p == port and (h == name or (suffix and h.endswith("." + name))):
            return r
    return None


def _target(line: str) -> Tuple[str, str, int]:
    """The request line → (method, host, port)."""
    parts = line.split()
    if len(parts) != 3 or not parts[2].startswith("HTTP/"):
        raise ValueError("not an HTTP request")
    method, target = parts[0].upper(), parts[1]
    if method == "CONNECT":
        m = _CONNECT.match(target)
        if not m:
            raise ValueError("CONNECT needs host:port")
        return method, m.group(1).strip("[]").lower(), int(m.group(2))
    u = urlsplit(target)
    try:
        port = u.port
    except ValueError:
        port = None
    return method, (u.hostname or "?").lower(), port or (443 if u.scheme == "https" else 80)


def _copy(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


_REASONS = {400: "Bad Request", 403: "Forbidden", 431: "Request Header Fields Too Large", 502: "Bad Gateway"}


class Proxy:
    """The runner's side: ``start()`` makes the per-job directory (socket + forwarder) and serves in a thread; ``stop()`` ends every
    tunnel and removes the directory. ``summary()`` is what the job keeps: {allowed, denied, failed}: {"host:port": connections}."""

    def __init__(self, allow: Iterable[Any], say: Callable[[str], None] = lambda line: None) -> None:
        self.rules = parse(allow)
        self.say = say
        self.counts: Dict[str, Dict[str, int]] = {"allowed": {}, "denied": {}, "failed": {}}
        self.dir: Optional[str] = None
        self.sock: Optional[str] = None
        self.fwd: Optional[str] = None
        self._srv: Optional[socket.socket] = None
        self._open: set = set()
        self._lock = threading.Lock()
        self._closed = threading.Event()

    def __enter__(self) -> "Proxy":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    def start(self) -> "Proxy":
        base = tempfile.gettempdir()
        if len(base) > 60:                                       # a unix socket's path has room for ~107 bytes
            base = "/tmp"
        self.dir = tempfile.mkdtemp(prefix="dtree-egress-", dir=base)
        self.sock = os.path.join(self.dir, "proxy.sock")
        self.fwd = os.path.join(self.dir, "fwd.py")
        with open(self.fwd, "w", encoding="utf-8") as f:
            f.write(FORWARDER)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(self.sock)
        srv.listen(64)
        srv.settimeout(0.5)                                      # close() does not wake accept(): look at the flag twice a second
        self._srv = srv
        threading.Thread(target=self._serve, name="dtree-egress", daemon=True).start()
        return self

    def stop(self) -> None:
        self._closed.set()
        if self._srv is not None:
            try:
                self._srv.close()
            except OSError:
                pass
        with self._lock:
            socks = list(self._open)
        for s in socks:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if self.dir:
            shutil.rmtree(self.dir, ignore_errors=True)

    def summary(self) -> Dict[str, Dict[str, int]]:
        with self._lock:
            return {k: dict(sorted(v.items())) for k, v in self.counts.items()}

    # ------------------------------------------------------------------------------------------------------------- serving
    def _serve(self) -> None:
        while not self._closed.is_set():
            try:
                c, _ = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._handle, args=(c,), daemon=True).start()

    def _count(self, kind: str, host: str, port: int, why: str = "") -> None:
        key = "%s:%d" % (host, port)
        with self._lock:
            d = self.counts[kind]
            new = key not in d
            d[key] = d.get(key, 0) + 1
        if new:
            self.say({"allowed": "🌐 egress allowed: %s", "denied": "⛔ egress denied: %s (%s)",
                      "failed": "⚠ egress could not reach %s (%s)"}[kind] % ((key, why) if why else key))

    def _reply(self, c: socket.socket, code: int, text: str) -> None:
        body = (text + "\n").encode("utf-8")
        c.sendall(b"HTTP/1.1 %d %s\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Length: %d\r\nConnection: close\r\n\r\n"
                  % (code, _REASONS[code].encode(), len(body)) + body)

    def _connect(self, host: str, port: int, local_ok: bool) -> socket.socket:
        last = "no address"
        for family, kind, proto, _, addr in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
            ip = ipaddress.ip_address(str(addr[0]).split("%")[0])
            if not local_ok and (ip.is_loopback or ip.is_link_local or ip.is_unspecified):
                last = "it resolves to the local address %s" % ip
                continue
            s = socket.socket(family, kind, proto)
            s.settimeout(15)
            try:
                s.connect(addr)
                return s
            except OSError as exc:
                last = str(exc)
                s.close()
        raise OSError(last)

    def _handle(self, c: socket.socket) -> None:
        up: Optional[socket.socket] = None
        with self._lock:
            self._open.add(c)
        try:
            c.settimeout(30)
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = c.recv(4096)
                if not chunk:
                    return
                head += chunk
                if len(head) > MAX_HEAD:
                    return self._reply(c, 431, "dtree: the request head is too large")
            raw, rest = head.split(b"\r\n\r\n", 1)
            try:
                method, host, port = _target(raw.split(b"\r\n", 1)[0].decode("latin-1"))
            except ValueError as exc:
                return self._reply(c, 400, "dtree: %s" % exc)
            if method != "CONNECT":
                self._count("denied", host, port, "plain http: only https (CONNECT) goes out")
                return self._reply(c, 403, "dtree: only https (CONNECT) leaves this sandbox")
            rule = match(self.rules, host, port)
            if rule is None:
                self._count("denied", host, port, "not on the job's network allow-list")
                return self._reply(c, 403, "dtree: %s:%d is not on this job's network allow-list" % (host, port))
            try:
                up = self._connect(host, port, local_ok=_ip(rule[0]) is not None or rule[0] == "localhost")
            except OSError as exc:
                kind = "denied" if "local address" in str(exc) else "failed"
                self._count(kind, host, port, str(exc))
                return self._reply(c, 403 if kind == "denied" else 502, "dtree: %s:%d: %s" % (host, port, exc))
            with self._lock:
                self._open.add(up)
            self._count("allowed", host, port)
            c.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            if rest:
                up.sendall(rest)
            c.settimeout(None)
            up.settimeout(None)
            back = threading.Thread(target=_copy, args=(up, c), daemon=True)
            back.start()
            _copy(c, up)
            back.join()
        except OSError:
            pass
        finally:
            for s in (c, up):
                if s is not None:
                    with self._lock:
                        self._open.discard(s)
                    try:
                        s.close()
                    except OSError:
                        pass


#: the shell's $0 in the wrappers: what its own error messages start with ("dtree-sandbox: 1: exec: …/claude: not found")
SHELL_NAME = "dtree-sandbox"


def wrap(command: List[str], proxy: Proxy, port: int = PORT) -> List[str]:
    """``command`` started after the forwarder listens inside the sandbox (``/bin/sh``; the forwarder forks once it listens, so the
    command never finds the port closed). The shell's $0 is neutral, so a command that cannot start is reported as itself."""
    script = ('/usr/bin/python3 -I "$1" "$2" "$3" </dev/null || { echo "dtree: the egress forwarder did not start" >&2; exit 97; }; '
              'shift 3; exec "$@"')
    return ["/bin/sh", "-c", script, SHELL_NAME, str(proxy.fwd), str(proxy.sock), str(port)] + list(command)


def proxy_env(port: int = PORT) -> Dict[str, str]:
    """The variables that send a sandboxed program's traffic to the forwarder (the sandbox's own loopback stays direct)."""
    url = "http://127.0.0.1:%d" % port
    local = "localhost,127.0.0.1,::1"
    return {"HTTPS_PROXY": url, "HTTP_PROXY": url, "https_proxy": url, "http_proxy": url, "NO_PROXY": local, "no_proxy": local}


#: the forwarder, run inside the sandbox by the system python in isolated mode (it imports nothing from the worktree)
FORWARDER = r'''"""dtree's egress forwarder, inside a job's sandbox: 127.0.0.1:<port> -> the runner's proxy on a unix socket."""
import os
import socket
import sys
import threading


def copy(src, dst):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def serve(conn, path):
    up = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        up.connect(path)
    except OSError:
        conn.close()
        up.close()
        return
    back = threading.Thread(target=copy, args=(up, conn), daemon=True)
    back.start()
    copy(conn, up)
    back.join()
    conn.close()
    up.close()


def main():
    path, port = sys.argv[1], int(sys.argv[2])
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(128)
    if os.fork():
        os._exit(0)
    null = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(null, fd)
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=serve, args=(conn, path), daemon=True).start()


main()
'''
