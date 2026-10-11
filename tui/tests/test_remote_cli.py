"""Remote mode and the entry points, against fake servers on 127.0.0.1.

Remote mode had only ever met the real gateway, so the ways it went wrong were
found by users. What these tests pin down (each module's header has the why):

- **Ctrl-C could not stop a streaming answer.** The `interrupt` frame went out
  only when the stream paused for half a second, so a long reply ran to the
  end; and a server that never confirmed left the turn open for good.
- **A refused resume broke every later turn.** Once the resume window had
  passed the server closed every hello with 4410, and the client kept offering
  the dead token until /reset. Now it opens a fresh session and says the turn
  in flight was lost.
- **Seats were held after quitting.** /reset and quit dropped the socket
  without `bye`, so the seat stayed taken for the whole resume window.
- **The server decided what needed approval.** `destructive: false` on a
  run_command meant no prompt; [a]lways was ignored the other way round. An
  approval given after the server stopped waiting still ran the command the
  model had already been told did not run. Non-dict `args` crashed the turn.
- **Silent stops, stale replays.** A step-limit stop ended with no word to the
  user, and the replay journal outlived its turn, so a reused tool id was
  answered with an earlier turn's result.
- **wsjson** read a masked frame's key as payload (desyncing the stream), could
  not go through a proxy, and hung forever on a half-received frame.
- **auth** turned a captive-portal page or a reset into a traceback, and
  rewrote the credentials file in place (an interrupted save lost every token).
- **__main__**: `--remote ai.jojocode.in` dialled 127.0.0.1:80, and a wss://
  URL broke login, which is plain HTTPS.
- **headless** printed model text escapes and all (a reply could retitle the
  terminal), and a script could not tell a failed one-shot turn from a good one.

The websocket server here is a minimal RFC 6455 peer written for the test: it
answers the upgrade, reads masked client frames, writes unmasked server frames,
and speaks just enough of server/shared/src/protocol.ts to script a scenario.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import select
import socket
import stat
import struct
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tui"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_ollama import FakeModel, FakeOllama, pieces, saw_tool_result  # noqa: E402
from jojocode_ai import __main__ as cli  # noqa: E402
from jojocode_ai import auth, headless, remote, wsjson  # noqa: E402
from jojocode_ai.config import Config  # noqa: E402
from jojocode_ai.remote import RemoteAgent  # noqa: E402
from jojocode_ai.wsjson import WSClient, WSClosed, WSError  # noqa: E402

POSIX = os.name == "posix"
_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_PROXY_VARS = ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy",
               "ALL_PROXY", "no_proxy", "NO_PROXY")


# =========================================================================== #
# A minimal websocket server
# =========================================================================== #

def _ref_mask(data: bytes, key: bytes) -> bytes:
    """RFC 6455 masking, byte by byte: the reference the fast version must match."""
    return bytes(b ^ key[i % 4] for i, b in enumerate(data))


def _frame(opcode: int, payload: bytes, fin: bool = True, mask: bytes | None = None) -> bytes:
    b0 = (0x80 if fin else 0) | opcode
    mbit = 0x80 if mask else 0
    n = len(payload)
    if n < 126:
        hdr = struct.pack(">BB", b0, mbit | n)
    elif n < (1 << 16):
        hdr = struct.pack(">BBH", b0, mbit | 126, n)
    else:
        hdr = struct.pack(">BBQ", b0, mbit | 127, n)
    if mask:
        return hdr + mask + _ref_mask(payload, mask)
    return hdr + payload


def _text(obj) -> bytes:
    return _frame(0x1, json.dumps(obj).encode())


def _parse(buf: bytes):
    """((fin, opcode, masked, payload), consumed) or None if incomplete."""
    if len(buf) < 2:
        return None
    b0, b1 = buf[0], buf[1]
    n, i = b1 & 0x7F, 2
    if n == 126:
        if len(buf) < 4:
            return None
        n, i = struct.unpack(">H", buf[2:4])[0], 4
    elif n == 127:
        if len(buf) < 10:
            return None
        n, i = struct.unpack(">Q", buf[2:10])[0], 10
    masked = bool(b1 & 0x80)
    key = b""
    if masked:
        if len(buf) < i + 4:
            return None
        key, i = buf[i:i + 4], i + 4
    if len(buf) < i + n:
        return None
    payload = buf[i:i + n]
    if masked:
        payload = _ref_mask(payload, key)
    return (bool(b0 & 0x80), b0 & 0x0F, masked, payload), i + n


class Peer:
    """One accepted connection, seen from the server's side."""

    def __init__(self, sock: socket.socket, index: int, server: "FakeWSServer | None"):
        self.sock = sock
        self.index = index
        self.server = server
        self.buf = b""
        self.request_line = ""
        self.headers: dict[str, str] = {}
        self.frames: list[dict] = []          # every JSON frame the client sent
        self.raw: list[tuple] = []            # (fin, opcode, masked, payload) of every frame
        self.gone = False

    # -- handshake ------------------------------------------------------- #
    def read_head(self, timeout: float = 5.0) -> tuple[str, dict[str, str]]:
        self.sock.settimeout(timeout)
        while b"\r\n\r\n" not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise EOFError("closed before the request was complete")
            self.buf += chunk
        head, _, self.buf = self.buf.partition(b"\r\n\r\n")
        lines = head.decode("latin1").split("\r\n")
        hdrs = {}
        for ln in lines[1:]:
            k, _, v = ln.partition(":")
            hdrs[k.strip().lower()] = v.strip()
        return lines[0], hdrs

    def handshake(self, accept: str | None = None, status: str = "101 Switching Protocols"):
        self.request_line, self.headers = self.read_head()
        key = self.headers.get("sec-websocket-key", "")
        good = base64.b64encode(hashlib.sha1((key + _GUID).encode()).digest()).decode()
        self.send_raw((f"HTTP/1.1 {status}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                       f"Sec-WebSocket-Accept: {accept or good}\r\n\r\n").encode())

    # -- frames ---------------------------------------------------------- #
    def send_raw(self, data: bytes) -> bool:
        try:
            self.sock.sendall(data)
            return True
        except OSError:
            self.gone = True
            return False

    def send(self, obj: dict) -> bool:
        return self.send_raw(_text(obj))

    def close_with(self, code: int, reason: str = "") -> None:
        self.send_raw(_frame(0x8, struct.pack(">H", code) + reason.encode()))

    def drop(self) -> None:
        """Hang up without a close frame — a dead network, a killed server."""
        self.gone = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass

    def recv(self, timeout: float = 10.0) -> dict | None:
        """The next JSON frame from the client; None on timeout; EOFError once
        the client has gone (EOF or a close frame)."""
        deadline = time.monotonic() + timeout
        while True:
            got = _parse(self.buf)
            if got:
                (fin, op, masked, payload), used = got
                self.buf = self.buf[used:]
                self.raw.append((fin, op, masked, payload))
                if op == 0x8:
                    self.gone = True
                    raise EOFError("client sent close")
                if op in (0x9, 0xA):          # ping / pong: logged, not returned
                    continue
                obj = json.loads(payload.decode("utf-8"))
                self.frames.append(obj)
                if self.server is not None:
                    self.server.record(self.index, obj)
                return obj
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            try:
                r, _, _ = select.select([self.sock], [], [], left)
                if not r:
                    return None
                chunk = self.sock.recv(65536)
            except (OSError, ValueError):
                chunk = b""
            if not chunk:
                self.gone = True
                raise EOFError("client hung up")
            self.buf += chunk

    def expect(self, t: str, timeout: float = 10.0) -> dict:
        """The next frame of type `t`, skipping (but logging) any others."""
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise EOFError(f"no {t!r} frame within {timeout}s")
            f = self.recv(timeout=left)
            if f is not None and f.get("t") == t:
                return f

    def drain(self, timeout: float = 15.0) -> None:
        """Keep reading (and logging) until the client goes away."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.recv(timeout=0.2)


class FakeWSServer:
    """Accepts websocket connections; `handler(peer)` scripts each one."""

    def __init__(self, handler):
        self.handler = handler
        self.peers: list[Peer] = []
        self.log: list[tuple[int, dict]] = []
        self.cond = threading.Condition()
        self._stop = False
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.sock.settimeout(0.05)
        threading.Thread(target=self._accept_loop, daemon=True).start()

    @property
    def port(self) -> int:
        return self.sock.getsockname()[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _accept_loop(self) -> None:
        while not self._stop:
            try:
                s, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            peer = Peer(s, len(self.peers), self)
            self.peers.append(peer)
            threading.Thread(target=self._serve, args=(peer,), daemon=True).start()

    def _serve(self, peer: Peer) -> None:
        try:
            peer.handshake()
            self.handler(peer)
        except (EOFError, OSError, ValueError):
            pass
        finally:
            peer.drop()

    def record(self, index: int, frame: dict) -> None:
        with self.cond:
            self.log.append((index, frame))
            self.cond.notify_all()

    def wait_for(self, pred, timeout: float = 5.0) -> bool:
        with self.cond:
            return self.cond.wait_for(lambda: any(pred(i, f) for i, f in self.log), timeout)

    def frames(self, t: str, peer: int | None = None) -> list[dict]:
        with self.cond:
            return [f for i, f in self.log if f.get("t") == t and (peer is None or i == peer)]

    def stop(self) -> None:
        self._stop = True
        try:
            self.sock.close()
        except OSError:
            pass
        for p in self.peers:
            p.drop()


# -- protocol helpers -------------------------------------------------------- #

def ready(peer: Peer, sid: str | None = None, rt: str | None = None) -> dict:
    hello = peer.expect("hello")
    peer.send({"t": "session_ready", "protocol": 1,
               "sessionId": sid or f"s-{peer.index}", "seatNo": 1,
               "resumeToken": rt or f"rt-{peer.index}",
               "resumed": bool(hello.get("resumeToken"))})
    return hello


def turn_done(stop: str = "model_done") -> dict:
    return {"t": "turn_done", "stopReason": stop,
            "usage": {"tokensIn": 11, "tokensOut": 7, "modelMs": 120, "toolCalls": 0}}


def tool_request(tid: str, name: str, args, destructive: bool = False,
                 timeout_ms: int = 30_000) -> dict:
    return {"t": "tool_request", "id": tid, "name": name, "args": args,
            "destructive": destructive, "timeoutMs": timeout_ms}


def kinds(events, kind):
    return [e for e in events if e[0] == kind]


def errors(events):
    return [e[1] for e in events if e[0] == "error"]


def said(events):
    return "".join(e[1] for e in events if e[0] == "assistant")


# -- fixtures ---------------------------------------------------------------- #

@pytest.fixture
def clean_env(tmp_path, monkeypatch):
    for v in _PROXY_VARS:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("JOJO_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("JOJO_CONFIG", str(tmp_path / "config"))
    return tmp_path


@pytest.fixture
def project(clean_env, monkeypatch):
    # A failing scenario must fail in seconds, not sit out the 75 s liveness.
    monkeypatch.setattr(remote, "LIVENESS", 8.0)
    proj = clean_env / "proj"
    proj.mkdir()
    return proj


@pytest.fixture
def ws():
    """ws(handler) -> a running FakeWSServer, stopped at teardown."""
    made: list[FakeWSServer] = []

    def make(handler):
        srv = FakeWSServer(handler)
        made.append(srv)
        return srv
    yield make
    for s in made:
        s.stop()


@pytest.fixture
def agents():
    """agents(srv, project, approver=None, **cfg) -> RemoteAgent, closed at teardown."""
    made: list[RemoteAgent] = []

    def make(srv, project, approver=None, **cfg_kw):
        cfg = Config(cwd=str(project), remote_endpoint=srv.url, project_label="proj", **cfg_kw)
        a = RemoteAgent(cfg, "test-token", approver)
        made.append(a)
        return a
    yield make
    for a in made:
        a.close()


class Approver:
    def __init__(self, answer=True, delay: float = 0.0):
        self.answer = answer
        self.delay = delay
        self.calls: list[tuple[str, object]] = []

    def __call__(self, name, args):
        self.calls.append((name, args))
        if self.delay:
            time.sleep(self.delay)
        return self.answer


def one_tool_server(ws, *requests):
    """Each user_turn gets the next tool request; the result is kept."""
    results: list[dict] = []
    queue = list(requests)

    def handler(peer):
        ready(peer)
        while queue:
            peer.expect("user_turn")
            peer.send(queue.pop(0))
            results.append(peer.expect("tool_result"))
            peer.send(turn_done())
        peer.drain()
    return ws(handler), results


# =========================================================================== #
# RemoteAgent: interrupting a turn
# =========================================================================== #

def test_ctrl_c_mid_stream_sends_interrupt_at_once(project, ws, agents):
    got = {}

    def handler(peer):
        ready(peer)
        peer.expect("user_turn")
        for i in range(400):                    # ~8 s of tokens with no pause in them
            if not peer.send({"t": "assistant_delta", "text": f"tok{i} "}):
                return
            f = peer.recv(timeout=0.02)
            if f and f.get("t") == "interrupt":
                got["at"] = time.monotonic()
                peer.send(turn_done("interrupted"))
                break
        peer.drain()

    srv = ws(handler)
    agent = agents(srv, project)
    events, pressed = [], None
    for ev in agent.run("write me a novel"):
        events.append(ev)
        if ev[0] == "assistant" and pressed is None and len(kinds(events, "assistant")) >= 3:
            pressed = time.monotonic()
            agent.interrupt()
    ended = time.monotonic()

    assert pressed is not None, events
    assert "at" in got, "the interrupt frame never reached the server while tokens streamed"
    assert got["at"] - pressed < 1.0
    assert ended - pressed < 3.0
    assert "interrupted" in errors(events)
    assert events[-1] == ("turn_end",)


def test_unconfirmed_interrupt_ends_turn_within_grace_and_next_turn_recovers(
        project, ws, agents, monkeypatch):
    monkeypatch.setattr(remote, "INTERRUPT_GRACE", 0.6)

    def handler(peer):
        if peer.index == 0:
            ready(peer)
            peer.expect("user_turn")
            for i in range(300):                # streams on; never confirms
                if not peer.send({"t": "assistant_delta", "text": f"t{i} "}):
                    return
                try:
                    peer.recv(timeout=0.03)
                except EOFError:
                    return
            return
        if peer.index == 1:
            # The old session's resume window is over: refuse with close 4410
            # (the close frame alone, no `closing` message first).
            peer.expect("hello")
            peer.close_with(remote.RESUME_EXPIRED, "resume window elapsed")
            time.sleep(0.2)
            return
        ready(peer)
        peer.expect("user_turn")
        peer.send({"t": "assistant_delta", "text": "back again"})
        peer.send(turn_done())
        peer.drain()

    srv = ws(handler)
    agent = agents(srv, project)
    events, pressed = [], None
    for ev in agent.run("go"):
        events.append(ev)
        if ev[0] == "assistant" and pressed is None:
            pressed = time.monotonic()
            agent.interrupt()
    took = time.monotonic() - pressed

    assert took < remote.INTERRUPT_GRACE + 2.0, f"turn ran {took:.1f}s past Ctrl-C"
    assert any("did not confirm" in e for e in errors(events)), events
    assert events[-1] == ("turn_end",)
    assert srv.frames("interrupt", peer=0), "Ctrl-C was never sent to the server"
    assert agent._ws is None                  # the next message starts on a new socket

    # The next turn: the resume is refused, so a fresh session carries the turn.
    events2 = list(agent.run("are you there?"))
    assert errors(events2) == [], events2
    assert said(events2) == "back again"
    hellos = srv.frames("hello")
    assert hellos[1].get("resumeToken") == "rt-0"
    assert "resumeToken" not in hellos[2]
    assert agent.resume_token == "rt-2"


# =========================================================================== #
# RemoteAgent: sessions
# =========================================================================== #

def test_refused_resume_opens_fresh_session_and_reports_turn_lost(project, ws, agents):
    def handler(peer):
        if peer.index == 0:
            ready(peer)
            peer.expect("user_turn")
            peer.send({"t": "assistant_delta", "text": "partial"})
            time.sleep(0.1)
            peer.drop()                         # the network dies mid-turn
            return
        if peer.index == 1:
            peer.expect("hello")
            peer.send({"t": "closing", "code": 4410, "resumable": False,
                       "reason": "resume window elapsed; start a fresh session"})
            peer.close_with(4410)
            time.sleep(0.2)
            return
        ready(peer, sid="fresh", rt="rt-fresh")
        peer.expect("user_turn")
        peer.send({"t": "assistant_delta", "text": "fresh answer"})
        peer.send(turn_done())
        peer.drain()

    srv = ws(handler)
    agent = agents(srv, project)
    events = list(agent.run("first"))
    assert any("lost" in e for e in errors(events)), events
    assert events[-1] == ("turn_end",)
    assert agent.session_id == "fresh"
    assert agent.resume_token == "rt-fresh"

    hellos = srv.frames("hello")
    assert hellos[1].get("resumeToken") == "rt-0"      # the resume was tried...
    assert "resumeToken" not in hellos[2]              # ...then a fresh session

    # and the session works: the dead token is not offered again
    events2 = list(agent.run("second"))
    assert errors(events2) == [], events2
    assert said(events2) == "fresh answer"
    assert len(srv.peers) == 3
    assert srv.frames("user_turn", peer=2) == [{"t": "user_turn", "text": "second"}]


def test_silent_socket_mid_turn_is_resumed(project, ws, agents, monkeypatch):
    monkeypatch.setattr(remote, "LIVENESS", 0.4)

    def handler(peer):
        if peer.index == 0:
            ready(peer)
            peer.expect("user_turn")
            peer.send({"t": "assistant_delta", "text": "a"})
            peer.drain()                        # ...and then nothing, socket still open
            return
        hello = ready(peer, rt="rt-0")
        assert hello.get("resumeToken") == "rt-0"
        peer.send({"t": "assistant_delta", "text": "b"})
        peer.send(turn_done())
        peer.drain()

    srv = ws(handler)
    agent = agents(srv, project)
    t0 = time.monotonic()
    events = list(agent.run("go"))
    assert time.monotonic() - t0 < 5
    assert errors(events) == [], events
    assert said(events) == "ab"
    assert any("resumed" in e[1] for e in kinds(events, "info"))


def test_reset_and_close_say_bye(project, ws, agents):
    def handler(peer):
        ready(peer)
        while True:
            peer.expect("user_turn", timeout=15)
            peer.send({"t": "assistant_delta", "text": "ok"})
            peer.send(turn_done())

    srv = ws(handler)
    agent = agents(srv, project)
    assert errors(list(agent.run("one"))) == []

    agent.reset()
    assert srv.wait_for(lambda i, f: i == 0 and f.get("t") == "bye"), \
        "/reset dropped the socket without giving the seat back"
    assert agent.resume_token is None
    assert agent.messages == []

    assert errors(list(agent.run("two"))) == []
    assert "resumeToken" not in srv.frames("hello", peer=1)[0]   # a new session, not a resume
    agent.close()
    assert srv.wait_for(lambda i, f: i == 1 and f.get("t") == "bye"), \
        "quitting dropped the socket without giving the seat back"
    assert all(f.get("reason") == "user_quit" for f in srv.frames("bye"))


def test_idle_client_gives_its_seat_back(project, ws, agents, monkeypatch):
    monkeypatch.setattr(remote, "HEARTBEAT", 0.05)
    monkeypatch.setattr(remote, "IDLE_RELEASE", 0.3)

    def handler(peer):
        ready(peer)
        peer.expect("user_turn")
        peer.send(turn_done())
        peer.drain()

    srv = ws(handler)
    agent = agents(srv, project)
    list(agent.run("hi"))
    assert srv.wait_for(lambda i, f: f.get("t") == "bye", timeout=4)
    assert srv.frames("heartbeat", peer=0)
    deadline = time.monotonic() + 2              # the release runs on the heartbeat thread
    while (agent._ws is not None or agent.resume_token is not None) \
            and time.monotonic() < deadline:
        time.sleep(0.02)
    assert agent._ws is None and agent.resume_token is None
    # and the next turn opens a new session rather than resuming the released one
    list(agent.run("again"))
    assert "resumeToken" not in srv.frames("hello", peer=1)[0]


# =========================================================================== #
# RemoteAgent: tools and approval
# =========================================================================== #

def test_non_destructive_run_command_still_asks(project, ws, agents):
    srv, results = one_tool_server(
        ws, tool_request("c1", "run_command", {"cmd": "echo ran > marker.txt"}, destructive=False))
    ask = Approver(answer=False)
    agent = agents(srv, project, approver=ask)
    events = list(agent.run("run it"))

    assert [c[0] for c in ask.calls] == ["run_command"], \
        "destructive:false from the server skipped the approval prompt"
    assert not (project / "marker.txt").exists()
    assert results[0]["ok"] is False and results[0]["refused"] == "user_rejected"
    assert kinds(events, "tool_denied") == [("tool_denied", "run_command")]


def test_always_approved_tool_runs_without_asking(project, ws, agents):
    srv, results = one_tool_server(
        ws, tool_request("c1", "run_command",
                         {"cmd": "echo ran > marker.txt", "timeout_ms": 600_000},
                         destructive=True, timeout_ms=30_000))
    ask = Approver(answer=False)
    agent = agents(srv, project, approver=ask)
    agent.cfg.auto_approve.add("run_command")           # what [a]lways does
    events = list(agent.run("run it"))

    assert ask.calls == [], "[a]lways was ignored in remote mode"
    assert (project / "marker.txt").exists()
    assert results[0]["ok"] is True and results[0]["exitCode"] == 0
    # the command must finish (or be killed) inside the server's window
    (start,) = kinds(events, "tool_start")
    assert start[2]["timeout_ms"] <= 30_000 - 1000


@pytest.mark.parametrize("name,args,destructive,asked", [
    ("list_dir", {"path": "."}, False, False),            # read-only: never asks
    ("list_dir", {"path": "."}, True, True),              # the flag may ADD a prompt
    ("write_file", {"path": "a.txt", "content": "x"}, False, False),   # auto-edit, in project
    ("write_file", {"path": ".envrc", "content": "x"}, False, True),   # a dot-file is code
    ("write_file", {"path": "../out.txt", "content": "x"}, False, True),
    ("apply_patch", {"patch": "..."}, False, True),       # unknown to us: not read-only
])
def test_who_decides_what_needs_approval(project, ws, agents, name, args, destructive, asked):
    srv, results = one_tool_server(ws, tool_request("t1", name, args, destructive=destructive))
    ask = Approver(answer=False)
    agent = agents(srv, project, approver=ask)
    list(agent.run("go"))
    assert bool(ask.calls) is asked, ask.calls
    assert len(results) == 1


def test_approval_after_the_server_stopped_waiting_is_not_run(project, ws, agents):
    # timeoutMs 1500: an approval later than ~0.5 s cannot finish inside it
    srv, results = one_tool_server(
        ws, tool_request("late", "run_command", {"cmd": "echo ran > marker.txt"},
                         destructive=True, timeout_ms=1500))
    ask = Approver(answer=True, delay=0.8)
    agent = agents(srv, project, approver=ask)
    events = list(agent.run("run it"))

    assert ask.calls, "the user was never asked"
    assert not (project / "marker.txt").exists(), "a late approval ran the command anyway"
    assert results[0]["ok"] is False and results[0]["refused"] == "timeout"
    assert kinds(events, "tool_start") == []
    assert any("too late" in e[1] for e in kinds(events, "info"))


def test_a_late_approval_replayed_in_the_same_turn_is_not_run(project, ws, agents):
    # The refusal is journaled: the same request replayed before turn_done
    # (a reconnect) is answered from the journal, never run or asked again.
    results: list[dict] = []
    req = tool_request("late", "run_command", {"cmd": "echo ran > marker.txt"},
                       destructive=True, timeout_ms=1500)

    def handler(peer):
        ready(peer)
        peer.expect("user_turn")
        peer.send(req)
        results.append(peer.expect("tool_result"))
        peer.send(req)                                  # replayed
        results.append(peer.expect("tool_result"))
        peer.send(turn_done())
        peer.drain()

    ask = Approver(answer=True, delay=0.8)
    agent = agents(ws(handler), project, approver=ask)
    list(agent.run("run it"))
    assert len(ask.calls) == 1
    assert [r["refused"] for r in results] == ["timeout", "timeout"]
    assert not (project / "marker.txt").exists()


@pytest.mark.parametrize("args", ["path=.", ["."], None, 42])
def test_non_dict_args_do_not_crash(project, ws, agents, args):
    srv, results = one_tool_server(ws, tool_request("t1", "list_dir", args))
    agent = agents(srv, project)
    events = list(agent.run("look"))
    assert events[-1] == ("turn_end",)
    assert results and results[0]["ok"] is True, results


def test_non_dict_args_reach_the_approver_as_a_dict(project, ws, agents):
    srv, results = one_tool_server(ws, tool_request("t1", "run_command", ["rm", "-rf", "/"]))
    ask = Approver(answer=True)
    agent = agents(srv, project, approver=ask)
    events = list(agent.run("go"))
    assert ask.calls == [("run_command", {})]
    assert events[-1] == ("turn_end",)
    assert results[0]["ok"] is False and "no command" in results[0]["error"]


@pytest.mark.parametrize("timeout_ms", [None, "30000", {"ms": 1}])
def test_malformed_timeout_does_not_crash(project, ws, agents, timeout_ms):
    # Same class as non-dict args: one bad field must not kill the turn with a
    # traceback and leave the server waiting for a tool_result that never comes.
    req = tool_request("t1", "list_dir", {"path": "."})
    req["timeoutMs"] = timeout_ms
    srv, results = one_tool_server(ws, req)
    events = list(agents(srv, project).run("look"))
    assert events[-1] == ("turn_end",)
    assert results and results[0]["ok"] is True, results


@pytest.mark.parametrize("payload", [b"[1, 2]", b'"hello"', b"42"])
@pytest.mark.parametrize("when", ["mid_turn", "first_frame"])
def test_non_object_frame_does_not_crash(project, ws, agents, payload, when):
    def handler(peer):
        if when == "first_frame":
            peer.expect("hello")
            peer.send_raw(_frame(0x1, payload))
            peer.drain(timeout=5)
            return
        ready(peer)
        peer.expect("user_turn")
        peer.send_raw(_frame(0x1, payload))
        peer.send({"t": "assistant_delta", "text": "after"})
        peer.send(turn_done())
        peer.drain()

    srv = ws(handler)
    events = list(agents(srv, project).run("x"))        # must not raise
    assert events[-1] == ("turn_end",)
    if when == "first_frame":
        assert errors(events), events


# =========================================================================== #
# RemoteAgent: how a turn ends, and the journal
# =========================================================================== #

@pytest.mark.parametrize("reason,phrase", [
    ("max_turns", "step limit"),
    ("max_wallclock", "time limit"),
    ("max_tokens", "length limit"),
])
def test_server_stop_reason_is_told_to_the_user(project, ws, agents, reason, phrase):
    def handler(peer):
        ready(peer)
        peer.expect("user_turn")
        peer.send({"t": "assistant_delta", "text": "partial answer"})
        peer.send(turn_done(reason))
        peer.drain()

    srv = ws(handler)
    agent = agents(srv, project)
    events = list(agent.run("big task"))
    finals = kinds(events, "final")
    assert len(finals) == 1, events
    _, note, shown = finals[0]
    assert phrase in note and "continue" in note
    assert shown is False                  # it was not streamed, so it must be printed
    assert errors(events) == []


def test_error_stop_reason_is_an_error(project, ws, agents):
    def handler(peer):
        ready(peer)
        peer.expect("user_turn")
        peer.send(turn_done("error"))
        peer.drain()

    srv = ws(handler)
    events = list(agents(srv, project).run("x"))
    assert errors(events) and kinds(events, "final") == []


def test_journal_is_per_turn(project, ws, agents):
    (project / "a.txt").write_text("AAA", encoding="utf-8")
    (project / "b.txt").write_text("BBB", encoding="utf-8")
    # A server that numbers tool ids per turn reuses "1"; it is a new request.
    srv, results = one_tool_server(ws, tool_request("1", "read_file", {"path": "a.txt"}),
                                   tool_request("1", "read_file", {"path": "b.txt"}))
    agent = agents(srv, project)
    list(agent.run("read a"))
    list(agent.run("read b"))
    assert [r.get("content") for r in results] == ["AAA", "BBB"], \
        "turn 2 was answered from turn 1's journal"
    assert agent.journal == {}      # a finished turn's requests can no longer be replayed


def test_replayed_request_is_answered_from_journal_not_rerun(project, ws, agents):
    results: list[dict] = []
    req = tool_request("w1", "run_command", {"cmd": "echo once >> count.txt"}, destructive=True)

    def handler(peer):
        if peer.index == 0:
            ready(peer)
            peer.expect("user_turn")
            peer.send(req)
            results.append(peer.expect("tool_result"))
            peer.drop()                         # dies before turn_done
            return
        hello = ready(peer, rt="rt-0")
        assert hello.get("resumeToken") == "rt-0"
        peer.send({"t": "replay_begin", "count": 1})
        peer.send(req)
        results.append(peer.expect("tool_result"))
        peer.send({"t": "replay_end"})
        peer.send(turn_done())
        peer.drain()

    srv = ws(handler)
    ask = Approver(answer=True)
    agent = agents(srv, project, approver=ask)
    events = list(agent.run("count"))

    assert errors(events) == [], events
    assert len(results) == 2 and results[0] == results[1]
    lines = (project / "count.txt").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1, "the replay re-ran the command"
    assert len(ask.calls) == 1, "the replay asked the user again"


def test_every_client_frame_is_masked(project, ws, agents):
    def handler(peer):
        ready(peer)
        peer.expect("user_turn")
        peer.send(turn_done())
        peer.drain()

    srv = ws(handler)
    agent = agents(srv, project)
    list(agent.run("x"))
    agent.close()
    srv.wait_for(lambda i, f: f.get("t") == "bye")
    raw = srv.peers[0].raw
    assert raw and all(masked for _, _, masked, _ in raw)
    # the upgrade names us: an unnamed client is refused at the edge (net.py)
    assert srv.peers[0].headers["user-agent"].startswith("JojoAI/")
    assert srv.peers[0].request_line.startswith("GET /agent ")


# =========================================================================== #
# wsjson
# =========================================================================== #

@pytest.mark.parametrize("n", [0, 1, 2, 3, 4, 5, 7, 8, 9, 125, 126, 1000, 65537])
def test_mask_matches_rfc_and_round_trips(n):
    rnd = random.Random(n)
    data = bytes(rnd.randrange(256) for _ in range(n))
    key = bytes(rnd.randrange(256) for _ in range(4))
    assert wsjson._mask(data, key) == _ref_mask(data, key)
    assert wsjson._mask(wsjson._mask(data, key), key) == data


def _client_for(srv) -> WSClient:
    c = WSClient(f"ws://127.0.0.1:{srv.port}/agent", connect_timeout=5)
    c.connect()
    return c


def test_server_frames_masked_fragmented_large_and_ping(clean_env, ws):
    big = {"t": "assistant_delta", "text": "x" * 70_000}       # 64-bit length
    mid = {"t": "assistant_delta", "text": "y" * 300}          # 16-bit length

    def handler(peer):
        # Servers must not mask, but one that does must not desync the stream.
        peer.send_raw(_frame(0x1, b'{"t":"masked","n":1}', mask=b"\x01\x02\x03\x04"))
        peer.send_raw(_frame(0x9, b"are-you-there"))           # ping, between messages
        peer.send_raw(_frame(0x1, b'{"t":"frag",', fin=False))
        peer.send_raw(_frame(0x0, b'"n":2}', fin=True))
        peer.send_raw(_text(mid))
        peer.send_raw(_text(big))
        peer.close_with(4410, "bye now")
        peer.drain(timeout=3)

    srv = ws(handler)
    c = _client_for(srv)
    try:
        assert c.recv_json(timeout=5) == {"t": "masked", "n": 1}
        assert c.recv_json(timeout=5) == {"t": "frag", "n": 2}
        assert c.recv_json(timeout=5) == mid
        assert c.recv_json(timeout=5) == big
        with pytest.raises(WSClosed) as ei:
            c.recv_json(timeout=5)
        assert ei.value.code == 4410 and ei.value.reason == "bye now"
    finally:
        c.close()
    time.sleep(0.1)
    pongs = [r for r in srv.peers[0].raw if r[1] == 0xA]
    assert pongs and pongs[0][3] == b"are-you-there" and pongs[0][2] is True


def test_idle_socket_is_none_not_an_error(clean_env, ws):
    srv = ws(lambda peer: peer.drain(timeout=3))
    c = _client_for(srv)
    try:
        assert c.recv_json(timeout=0.2) is None
    finally:
        c.close()


def test_half_received_frame_times_out_as_closed(clean_env, ws, monkeypatch):
    monkeypatch.setattr(wsjson, "READ_TIMEOUT", 0.3)

    def handler(peer):
        peer.send_raw(b"\x81\x0a" + b'{"t"')      # says 10 bytes, sends 4, then nothing
        peer.drain(timeout=5)

    srv = ws(handler)
    c = _client_for(srv)
    t0 = time.monotonic()
    try:
        with pytest.raises(WSClosed):
            c.recv_json(timeout=2)
    finally:
        c.close()
    assert time.monotonic() - t0 < 2.5


@pytest.mark.parametrize("accept,status,match", [
    ("bm90LXRoZS1yaWdodC1hY2NlcHQ=", "101 Switching Protocols", "Sec-WebSocket-Accept"),
    (None, "403 Forbidden", "403"),
])
def test_bad_handshake_is_wserror(clean_env, accept, status, match):
    lst = socket.socket()
    lst.bind(("127.0.0.1", 0))
    lst.listen(1)

    def serve():
        s, _ = lst.accept()
        p = Peer(s, 0, None)
        try:
            p.handshake(accept=accept, status=status)
            time.sleep(0.5)
        finally:
            p.drop()
    threading.Thread(target=serve, daemon=True).start()
    try:
        c = WSClient(f"ws://127.0.0.1:{lst.getsockname()[1]}/agent", connect_timeout=5)
        with pytest.raises(WSError, match=match):
            c.connect()
    finally:
        lst.close()


class FakeProxy:
    """An HTTP CONNECT proxy that, once the tunnel is up, *is* the far end:
    it answers the websocket upgrade itself, so no real host is involved."""

    def __init__(self, answer: str = "200 Connection established"):
        self.answer = answer
        self.connect_line = ""
        self.headers: dict[str, str] = {}
        self.peer: Peer | None = None
        self.got: list[dict] = []
        self.done = threading.Event()
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        threading.Thread(target=self._serve, daemon=True).start()

    @property
    def port(self) -> int:
        return self.sock.getsockname()[1]

    def _serve(self) -> None:
        try:
            s, _ = self.sock.accept()
        except OSError:
            return
        p = Peer(s, 0, None)
        try:
            self.connect_line, self.headers = p.read_head()
            p.send_raw(f"HTTP/1.1 {self.answer}\r\n\r\n".encode())
            if not self.answer.startswith("200"):
                return
            self.peer = p
            p.handshake()
            p.send({"t": "through", "via": "proxy"})
            self.got.append(p.expect("hello", timeout=5))
        except (EOFError, OSError):
            pass
        finally:
            self.done.set()
            time.sleep(0.1)
            p.drop()

    def close(self) -> None:
        self.sock.close()


def _use_proxy(monkeypatch, proxy_url: str) -> None:
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {"http": proxy_url,
                                                               "https": proxy_url})
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: False)


def test_websocket_tunnels_through_connect_proxy(clean_env, monkeypatch):
    proxy = FakeProxy()
    _use_proxy(monkeypatch, f"http://jo%40jo:s3cr%3At@127.0.0.1:{proxy.port}")
    try:
        c = WSClient("ws://jojo.example.test/agent", connect_timeout=5)
        c.connect()
        try:
            assert c.recv_json(timeout=5) == {"t": "through", "via": "proxy"}
            c.send_json({"t": "hello"})
            assert proxy.done.wait(5)
        finally:
            c.close()
    finally:
        proxy.close()
    assert proxy.connect_line == "CONNECT jojo.example.test:80 HTTP/1.1"
    want = base64.b64encode(b"jo@jo:s3cr:t").decode()
    assert proxy.headers.get("proxy-authorization") == f"Basic {want}"
    assert proxy.peer is not None and proxy.peer.headers["host"] == "jojo.example.test"
    assert proxy.got == [{"t": "hello"}]


def test_proxy_refusing_the_tunnel_is_wserror(clean_env, monkeypatch):
    proxy = FakeProxy(answer="403 Forbidden")
    _use_proxy(monkeypatch, f"http://127.0.0.1:{proxy.port}")
    try:
        c = WSClient("ws://jojo.example.test/agent", connect_timeout=5)
        with pytest.raises(WSError, match="403"):
            c.connect()
    finally:
        proxy.close()


def test_loopback_never_goes_through_the_proxy(clean_env, ws, monkeypatch):
    dead = socket.socket()
    dead.bind(("127.0.0.1", 0))
    port = dead.getsockname()[1]
    dead.close()                                    # nothing listens here
    _use_proxy(monkeypatch, f"http://127.0.0.1:{port}")
    srv = ws(lambda peer: (peer.send({"t": "direct"}), peer.drain(timeout=3)))
    c = _client_for(srv)
    try:
        assert c.recv_json(timeout=5) == {"t": "direct"}
    finally:
        c.close()


# =========================================================================== #
# auth
# =========================================================================== #

class FakeAuth:
    """Answers per path; `seen` keeps (path, headers, body) of every request."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.seen: list[tuple[str, dict, dict]] = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                fake.seen.append((self.path, dict(self.headers), body))
                route = fake.routes.get(self.path)
                if route == "reset":
                    self.close_connection = True
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return
                if route == "hang":
                    time.sleep(3)
                    return
                code, ctype, payload = route or (404, "text/plain", "nope")
                data = payload.encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05},
                         daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.srv.server_address[1]}"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.srv.shutdown()
        self.srv.server_close()


_PORTAL = (200, "text/html", "<html><body>Sign in to the cafe wifi</body></html>")


@pytest.mark.parametrize("route,expect", [
    (_PORTAL, (200, {})),
    ((200, "application/json", "[1, 2, 3]"), (200, {})),
    ((200, "application/json", ""), (200, {})),
    ((502, "text/html", "<h1>Bad gateway</h1>"), (502, {})),
    ((429, "application/json", '{"error": "slow down"}'), (429, {"error": "slow down"})),
    ((200, "application/json", '{"token": "t0k"}'), (200, {"token": "t0k"})),
])
def test_post_reads_any_body_without_raising(clean_env, route, expect):
    with FakeAuth({"/x": route}) as srv:
        assert auth._post(srv.url + "/x", {"email": "a@b.c"}) == expect
        path, headers, body = srv.seen[0]
    assert body == {"email": "a@b.c"}
    ua = {k.lower(): v for k, v in headers.items()}["user-agent"]
    assert ua.startswith("JojoAI/"), "the default urllib agent is banned at the edge"


def test_post_connection_errors_are_runtime_errors(clean_env):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                                       # refused
    with pytest.raises(RuntimeError, match="cannot reach"):
        auth._post(f"http://127.0.0.1:{port}/api/auth/otp/request", {})
    with FakeAuth({"/reset": "reset", "/hang": "hang"}) as srv:
        with pytest.raises(RuntimeError, match="cannot reach"):
            auth._post(srv.url + "/reset", {})
        t0 = time.monotonic()
        with pytest.raises(RuntimeError, match="cannot reach"):
            auth._post(srv.url + "/hang", {}, timeout=0.5)
        assert time.monotonic() - t0 < 2.5


@pytest.mark.parametrize("code", [200, 502])
def test_post_truncated_body_is_a_runtime_error(clean_env, code):
    # A proxy or the edge that cuts its own error page short: Content-Length
    # promises 100 bytes, 6 arrive. Either status must end in a sentence.
    lst = socket.socket()
    lst.bind(("127.0.0.1", 0))
    lst.listen(1)

    def serve():
        s, _ = lst.accept()
        p = Peer(s, 0, None)
        # Read the whole request, body too: unread bytes would turn close()
        # into a reset, which is a different failure from the one under test.
        _, hdrs = p.read_head()
        while len(p.buf) < int(hdrs.get("content-length") or 0):
            p.buf += s.recv(65536)
        s.sendall(f"HTTP/1.1 {code} X\r\nContent-Type: text/html\r\n"
                  f"Content-Length: 100\r\n\r\n<html>".encode())
        s.shutdown(socket.SHUT_WR)              # a clean EOF, six bytes in
        time.sleep(0.5)
        s.close()
    threading.Thread(target=serve, daemon=True).start()
    try:
        try:
            got = auth._post(f"http://127.0.0.1:{lst.getsockname()[1]}/x", {})
        except RuntimeError:
            return
        assert got == (code, {})
    finally:
        lst.close()


def test_login_against_a_portal_page_is_a_sentence_not_a_traceback(clean_env):
    routes = {"/api/auth/otp/request": (200, "application/json", "{}"),
              "/api/auth/otp/verify": _PORTAL}
    with FakeAuth(routes) as srv:
        with pytest.raises(RuntimeError, match="verification failed"):
            auth.otp_login(srv.url, "me@example.com", code="123456")
        assert auth.load_token(srv.url) is None


def test_login_saves_the_token(clean_env):
    routes = {"/api/auth/otp/request": (200, "application/json", "{}"),
              "/api/auth/otp/verify": (200, "application/json", '{"token": "tok-1"}')}
    with FakeAuth(routes) as srv:
        assert auth.otp_login(srv.url + "/", "me@example.com", code="123456") == "tok-1"
        assert auth.load_token(srv.url) == "tok-1"
        assert srv.seen[1][2] == {"email": "me@example.com", "code": "123456"}


@pytest.mark.skipif(not POSIX, reason="file modes are POSIX")
def test_save_token_is_private_from_the_first_byte(clean_env, monkeypatch):
    path = Path(auth.creds_path())
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"https://old": {"token": "keep"}}), encoding="utf-8")
    os.chmod(path, 0o644)                       # an older client left it readable
    modes = []
    real_replace = os.replace

    def spy(src, dst, *a, **kw):
        modes.append(stat.S_IMODE(os.stat(src).st_mode))
        return real_replace(src, dst, *a, **kw)
    monkeypatch.setattr(auth.os, "replace", spy)
    old = os.umask(0)                           # the file must not depend on the umask
    try:
        auth.save_token("https://ai.jojocode.in/", "secret", "me@example.com")
    finally:
        os.umask(old)

    assert modes == [0o600], "the temporary file was readable before it was renamed"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert not Path(str(path) + ".tmp").exists()
    assert auth.load_token("https://ai.jojocode.in") == "secret"
    assert auth.load_token("https://old") == "keep"


def test_interrupted_save_keeps_every_saved_token(clean_env, monkeypatch):
    auth.save_token("https://a", "token-a")
    auth.save_token("https://b", "token-b")

    def full_disk(fd):
        raise OSError(28, "No space left on device")
    with monkeypatch.context() as m:
        m.setattr(auth.os, "fsync", full_disk)
        with pytest.raises(OSError):
            auth.save_token("https://c", "token-c")
    assert auth.load_token("https://a") == "token-a"
    assert auth.load_token("https://b") == "token-b"


def test_forget_keeps_other_endpoints(clean_env):
    auth.save_token("https://a.example", "token-a")
    auth.save_token("https://b.example", "token-b")
    assert auth.forget("https://a.example/") is True
    assert auth.load_token("https://a.example") is None
    assert auth.load_token("https://b.example") == "token-b"
    assert auth.forget("https://a.example") is False


# =========================================================================== #
# __main__
# =========================================================================== #

@pytest.mark.parametrize("raw,url", [
    ("ai.jojocode.in", "https://ai.jojocode.in"),
    ("wss://X/", "https://x"),
    ("wss://AI.JojoCode.in/", "https://ai.jojocode.in"),
    ("ws://localhost:8080/", "http://localhost:8080"),
    ("https://ai.jojocode.in/", "https://ai.jojocode.in"),
    ("  http://127.0.0.1:7450  ", "http://127.0.0.1:7450"),
    ("ftp://ai.jojocode.in", None),
    ("https://", None),
    ("wss://", None),
])
def test_remote_url_normalisation(raw, url):
    assert cli._remote_url(raw) == url


def test_login_without_remote_is_an_argparse_error(clean_env, capsys):
    with pytest.raises(SystemExit) as ei:
        cli.main(["--login"])
    assert ei.value.code == 2
    assert "--remote" in capsys.readouterr().err


def test_bad_remote_url_is_an_argparse_error(clean_env, capsys):
    with pytest.raises(SystemExit) as ei:
        cli.main(["--remote", "ftp://ai.jojocode.in", "--logout"])
    assert ei.value.code == 2
    assert "not an http(s) URL" in capsys.readouterr().err


def test_cwd_that_does_not_exist_is_an_error(clean_env, capsys):
    with pytest.raises(SystemExit) as ei:
        cli.main(["--cwd", str(clean_env / "no-such-dir"), "--headless"])
    assert ei.value.code == 2
    assert "not a directory" in capsys.readouterr().err


@pytest.fixture
def captured_cfg(monkeypatch):
    """main() into local headless mode, with the run replaced by a recorder."""
    seen = {}

    def fake_run(cfg, one_shot=None):
        seen["cfg"], seen["one_shot"] = cfg, one_shot
        return 0
    monkeypatch.setattr(headless, "run", fake_run)
    return seen


@pytest.mark.parametrize("given,host", [
    ("localhost:11434", "http://localhost:11434"),
    (":11434", "http://127.0.0.1:11434"),
    ("0.0.0.0", "http://127.0.0.1:11434"),
    ("gpu-box", "http://gpu-box:11434"),
    ("http://gpu-box:8080/v1/", "http://gpu-box:8080/v1"),
])
def test_host_is_normalised(clean_env, captured_cfg, given, host):
    assert cli.main(["--host", given, "--headless"]) == 0
    assert captured_cfg["cfg"].host == host


def test_cwd_is_cleaned_and_names_the_project(clean_env, captured_cfg):
    proj = clean_env / "my proj"
    proj.mkdir()
    assert cli.main(["--cwd", f'"{proj}"', "do", "it"]) == 0
    cfg = captured_cfg["cfg"]
    assert cfg.cwd == str(proj)
    assert cfg.project_label == "my proj"
    assert captured_cfg["one_shot"] == "do it"


def test_remote_bare_host_and_ws_scheme_reach_the_same_credentials(clean_env, capsys):
    assert cli.main(["--remote", "wss://AI.JojoCode.in/", "--code", "demo", "one", "shot"]) == 2
    assert "headless remote" in capsys.readouterr().err
    assert auth.load_token("https://ai.jojocode.in") == "demo"
    assert cli.main(["--remote", "ai.jojocode.in", "--logout"]) == 0
    assert capsys.readouterr().out.strip() == "forgotten"
    assert auth.load_token("https://ai.jojocode.in") is None


# =========================================================================== #
# headless
# =========================================================================== #

@pytest.fixture
def hl(project, monkeypatch):
    monkeypatch.setattr(headless, "_COLOR", False)
    return project


def _cfg(srv, model, project):
    return Config(host=srv.url, model=model, cwd=str(project), think=False)


def test_headless_answer_exits_0(hl, capsys):
    m = FakeModel("qwen2.5-coder:7b", ["completion", "tools"], lambda b: pieces("All done here."))
    with FakeOllama(m) as srv:
        code = headless.run(_cfg(srv, m.name, hl), "say something")
    out = capsys.readouterr().out
    assert code == 0
    assert "All done here." in out


def test_headless_failed_turn_exits_1(hl, capsys):
    m = FakeModel("qwen2.5-coder:7b", ["completion", "tools"], lambda b: pieces("unused"))
    with FakeOllama(m) as srv:
        code = headless.run(_cfg(srv, "not-pulled:1b", hl), "say something")
    out = capsys.readouterr().out
    assert code == 1, "a script cannot tell this failed turn from a good one"
    assert "[error]" in out


@pytest.mark.parametrize("reply", [
    lambda b: [{"content": "Hello \x1b]0;pwned\x07world \x1b]52;c;ZXZpbA==\x07!"}],
    lambda b: pieces("Hello \x1b]0;pwned\x07world \x1b[2J\x1b]52;c;ZXZpbA==\x07!", size=2),
], ids=["whole", "split"])
def test_headless_model_text_carries_no_escapes(hl, capsys, reply):
    m = FakeModel("qwen2.5-coder:7b", ["completion", "tools"], reply)
    with FakeOllama(m) as srv:
        code = headless.run(_cfg(srv, m.name, hl), "hi")
    out = capsys.readouterr().out
    assert code == 0
    assert "\x1b" not in out and "\x07" not in out
    assert "Hello" in out and "world" in out


class FakeTTY:
    """A stdin that claims to be a terminal and answers input() from a script."""

    def __init__(self, line: str = ""):
        self.line = line

    def isatty(self):
        return True

    def readline(self, *a):
        line, self.line = self.line, ""
        return line

    def read(self, *a):
        return self.readline()


class _CtrlC(FakeTTY):
    def readline(self, *a):
        raise KeyboardInterrupt


@pytest.mark.parametrize("stdin,ok", [
    (FakeTTY(""), False),            # EOF
    (_CtrlC(), False),
    (FakeTTY("\n"), False),          # Enter: the default is No
    (FakeTTY("y\n"), True),
])
def test_approval_prompt_defaults_to_no(clean_env, monkeypatch, capsys, stdin, ok):
    monkeypatch.setattr(headless, "_COLOR", False)
    monkeypatch.setattr(sys, "stdin", stdin)
    cfg = Config(cwd=str(clean_env))
    assert headless._approver(cfg)("run_bash", {"command": "rm -rf ~"}) is ok
    out = capsys.readouterr().out
    assert "rm -rf ~" in out                     # the whole command was shown
    assert "run_bash" not in cfg.auto_approve


def test_approval_always_is_remembered(clean_env, monkeypatch):
    monkeypatch.setattr(headless, "_COLOR", False)
    monkeypatch.setattr(sys, "stdin", FakeTTY("a\n"))
    cfg = Config(cwd=str(clean_env))
    ask = headless._approver(cfg)
    assert ask("run_bash", {"command": "make"}) is True
    monkeypatch.setattr(sys, "stdin", FakeTTY(""))
    assert ask("run_bash", {"command": "make test"}) is True     # not asked again


def test_headless_eof_at_approval_denies_the_command(hl, monkeypatch, capsys):
    def reply(body):
        if saw_tool_result(body):
            return pieces("I could not run it.")
        return [{"tool_calls": [{"function": {"name": "run_bash",
                                              "arguments": {"command": "echo ran > marker.txt"}}}]}]

    m = FakeModel("qwen2.5-coder:7b", ["completion", "tools"], reply)
    monkeypatch.setattr(sys, "stdin", FakeTTY(""))
    with FakeOllama(m) as srv:
        cfg = _cfg(srv, m.name, hl)
        cfg.auto_bash = False
        headless.run(cfg, "run it")
    out = capsys.readouterr().out
    assert "Allow run_bash?" in out
    assert "denied: run_bash" in out
    assert not (hl / "marker.txt").exists(), "EOF at the prompt ran the command"
