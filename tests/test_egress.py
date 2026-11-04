"""egress.py: the allow-list, the CONNECT proxy on its unix socket (a local TCP server stands for an allowed host), and the forwarder
inside a real sandbox without a network."""
import os
import socket
import threading

import pytest

from dtree import egress, sandbox


class Echo:
    """A TCP server on the host's loopback that answers ``ECHO:<what it got>`` and closes."""

    def __init__(self):
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(8)
        self.port = self.srv.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            data = c.recv(1000)
            c.sendall(b"ECHO:" + data)
            c.close()


@pytest.fixture
def echo():
    e = Echo()
    yield e
    e.srv.close()


def _ask(sock_path, request, send=b"hello"):
    """Send ``request`` (the head) over the proxy's unix socket; return (status line, what came back through the tunnel)."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(10)
    s.connect(sock_path)
    s.sendall(request)
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = s.recv(4096)
        if not chunk:
            break
        head += chunk
    status = head.split(b"\r\n", 1)[0].decode()
    body = b""
    if " 200 " in status:
        s.sendall(send)
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            body += chunk
    s.close()
    return status, body


def test_the_allow_list_takes_hosts_suffixes_ports_and_addresses():
    rules = egress.parse(["api.anthropic.com", ".example.org", "pypi.org:8443", "127.0.0.1:5000", "[::1]:8080", "  ", "Platform.Claude.com."])
    assert egress.match(rules, "api.anthropic.com", 443) and not egress.match(rules, "api.anthropic.com", 80)
    assert not egress.match(rules, "evil-api.anthropic.com", 443) and not egress.match(rules, "anthropic.com", 443)
    assert egress.match(rules, "example.org", 443) and egress.match(rules, "a.b.example.org", 443) and not egress.match(rules, "badexample.org", 443)
    assert egress.match(rules, "pypi.org", 8443) and not egress.match(rules, "pypi.org", 443)
    assert egress.match(rules, "127.0.0.1", 5000) and not egress.match(rules, "127.0.0.1", 5077)
    assert egress.match(rules, "::1", 8080) and egress.match(rules, "0:0:0:0:0:0:0:1", 8080)            # the same address, written longer
    assert egress.match(rules, "PLATFORM.claude.com.", 443)
    for bad in ["http://x.com", "a b", ".1.2.3.4", "host:99999", "host:0", "::1", "-x.com", "a..b"]:
        with pytest.raises(ValueError):
            egress.parse([bad])


def test_the_proxy_tunnels_to_an_allowed_host_and_refuses_the_rest(echo):
    lines = []
    with egress.Proxy(["127.0.0.1:%d" % echo.port], lines.append) as p:
        assert os.path.exists(p.sock) and os.path.exists(p.fwd) and len(p.sock) < 100
        assert _ask(p.sock, b"CONNECT 127.0.0.1:%d HTTP/1.1\r\nHost: x\r\n\r\n" % echo.port) == ("HTTP/1.1 200 Connection established", b"ECHO:hello")
        for _ in range(2):                                                    # counted twice, logged once
            assert _ask(p.sock, b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")[0] == "HTTP/1.1 403 Forbidden"
        assert _ask(p.sock, b"GET http://example.com/x HTTP/1.1\r\nHost: example.com\r\n\r\n")[0] == "HTTP/1.1 403 Forbidden"
        assert _ask(p.sock, b"CONNECT 127.0.0.1:%d HTTP/1.1\r\n\r\n" % (echo.port + 1 if echo.port < 65535 else 1))[0] == "HTTP/1.1 403 Forbidden"
        assert _ask(p.sock, b"nonsense\r\n\r\n")[0] == "HTTP/1.1 400 Bad Request"
        s = p.summary()
        d = p.dir
    assert s["allowed"] == {"127.0.0.1:%d" % echo.port: 1}
    assert s["denied"]["example.com:443"] == 2 and s["denied"]["example.com:80"] == 1 and len(s["denied"]) == 3
    assert sum("example.com:443" in l for l in lines) == 1 and any(l.startswith("🌐 egress allowed: 127.0.0.1:") for l in lines)
    assert any("only https (CONNECT)" in l for l in lines)
    assert not os.path.exists(d)                                              # stop() removes the socket and the forwarder


def test_a_name_that_resolves_to_a_local_address_is_refused(echo, monkeypatch):
    real = socket.getaddrinfo
    monkeypatch.setattr(egress.socket, "getaddrinfo", lambda host, *a, **k: real("127.0.0.1", *a, **k) if host == "sneaky.example.org" else real(host, *a, **k))
    with egress.Proxy([".example.org:%d" % echo.port, "localhost:%d" % echo.port]) as p:
        status, _ = _ask(p.sock, b"CONNECT sneaky.example.org:%d HTTP/1.1\r\n\r\n" % echo.port)
        assert status == "HTTP/1.1 403 Forbidden" and "sneaky.example.org:%d" % echo.port in p.summary()["denied"]
        assert _ask(p.sock, b"CONNECT localhost:%d HTTP/1.1\r\n\r\n" % echo.port)[1] == b"ECHO:hello"      # named on the list itself


def test_an_allowed_host_that_cannot_be_reached_is_a_failure_not_a_refusal():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                                                                 # nothing listens there now
    with egress.Proxy(["127.0.0.1:%d" % port]) as p:
        assert _ask(p.sock, b"CONNECT 127.0.0.1:%d HTTP/1.1\r\n\r\n" % port)[0] == "HTTP/1.1 502 Bad Gateway"
        assert "127.0.0.1:%d" % port in p.summary()["failed"]


def test_stop_ends_an_open_tunnel():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    held = []
    threading.Thread(target=lambda: held.append(srv.accept()), daemon=True).start()     # accepts and never answers
    p = egress.Proxy(["127.0.0.1:%d" % port]).start()
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.settimeout(10)
    c.connect(p.sock)
    c.sendall(b"CONNECT 127.0.0.1:%d HTTP/1.1\r\n\r\n" % port)
    assert c.recv(100).startswith(b"HTTP/1.1 200")
    p.stop()
    assert c.recv(100) == b""                                                 # the tunnel is closed, not left hanging
    c.close()
    srv.close()


@pytest.mark.skipif(not sandbox.check()[0], reason="bwrap cannot run here")
def test_a_command_that_cannot_start_is_reported_as_itself_not_as_the_forwarder(tmp_path):
    """Review of 2026-11-01: the wrapper passed the forwarder's path as the shell's $0, so a binary the sandbox could not run read
    '…/fwd.py: 1: exec: …: not found' and the job's reason pointed at the proxy."""
    missing = str(tmp_path / "no-such-claude")
    with egress.Proxy([]) as p:
        r = sandbox.run(egress.wrap([missing, "-p"], p), tmp_path, tmp_path / "out.log", 30,
                        sandbox={"mounts": [("rw", str(tmp_path)), ("ro", p.dir, p.dir)], "network": "none"})
    text = (r["head"] + r["tail"]).strip()
    assert r["state"] == "failed" and r["exit_code"] == 127, r
    assert "fwd.py" not in text and text.startswith("dtree-sandbox:") and missing in text, text


@pytest.mark.skipif(not sandbox.check()[0], reason="bwrap cannot run here")
def test_inside_a_sandbox_without_a_network_only_the_forwarder_gets_out(tmp_path, echo):
    """The path Claude Code's requests take: a program in the sandbox → 127.0.0.1:3128 (the forwarder) → the unix socket → the proxy
    in this process → the allowed host. A direct connection, to the host's loopback or out, finds nothing."""
    probe = r'''
import os, socket, sys
port = int(sys.argv[1])
def via_proxy(target):
    s = socket.create_connection(("127.0.0.1", 3128), 5)
    s.sendall(("CONNECT %s HTTP/1.1\r\n\r\n" % target).encode())
    head = s.recv(1000).decode()
    if " 200 " not in head:
        return head.split("\r\n")[0]
    s.sendall(b"hi")
    return s.recv(100).decode()
def direct(addr):
    try:
        socket.create_connection(addr, 3)
        return "connected"
    except OSError as exc:
        return type(exc).__name__
print("ALLOWED", via_proxy("127.0.0.1:%d" % port))
print("DENIED", via_proxy("example.com:443"))
print("DIRECT-HOST", direct(("127.0.0.1", port)))
print("DIRECT-NET", direct(("1.1.1.1", 443)))
print("PROXY-ENV", os.environ.get("HTTPS_PROXY"))
'''
    lines = []
    with egress.Proxy(["127.0.0.1:%d" % echo.port], lines.append) as p:
        cmd = egress.wrap(["/usr/bin/python3", "-I", "-c", probe, str(echo.port)], p)
        r = sandbox.run(cmd, tmp_path, tmp_path / "out.log", 60,
                        sandbox={"mounts": [("rw", str(tmp_path)), ("ro", p.dir, p.dir)], "network": "none", "env": egress.proxy_env()})
        summary = p.summary()
    out = dict(l.split(" ", 1) for l in r["tail"].splitlines() if " " in l)
    assert r["state"] == "ok", r
    assert out["ALLOWED"] == "ECHO:hi" and out["DENIED"] == "HTTP/1.1 403 Forbidden"
    assert out["DIRECT-HOST"] == "ConnectionRefusedError" and out["DIRECT-NET"] != "connected"
    assert out["PROXY-ENV"] == "http://127.0.0.1:%d" % egress.PORT
    assert summary["allowed"] == {"127.0.0.1:%d" % echo.port: 1} and summary["denied"] == {"example.com:443": 1}
    assert r["seconds"] < 20                                                   # the forwarder left running does not hold the sandbox open
