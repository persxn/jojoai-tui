"""Second review round, pinned: remote mode, the web tools, the screen, Windows.

Each test is the reviewer's reproduced case (several were first reproduced
against the real backend). The ones that mattered most:

- **Remote mode could read JojoAI's credentials** without a prompt when the
  project was the home directory; local mode always refused.
- **A remote tool could run twice**: the journal was cleared at the start of
  every turn, so a request the server replayed from an unfinished turn ran
  again. And the user's next message was dropped ("a turn is already in
  progress") while the old turn's ending was shown as its answer.
- **A turn could wait forever** after a disconnect: its turn_done had gone
  into the dead socket. session_ready now says whether a turn is running.
- **/reset crashed the TUI** on a dead socket (close() raised).
- **"dd" in the history browser deleted a chat**; Ctrl-C at an approval still
  sent the queued message; a write past line 60 was approved unseen; /trust
  off silently dropped --safe.
- **On Windows**, cmd.exe ran a git.cmd the model had written into the
  project when the user approved "git status"; an argv list's "&" ran a
  second program; UTF-16 command output was NUL-interleaved.
- **fetch_url** could be stalled for as long as a server liked.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tui"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from jojocode_ai import (cols, headless, policy, remote, remote_exec, shell,  # noqa: E402
                         textfiles, tools, websearch, wsjson)
from jojocode_ai.config import Config  # noqa: E402
from jojocode_ai.tools import Ctx, run_tool  # noqa: E402
from test_remote_cli import (Approver, agents, clean_env, errors, kinds,  # noqa: E402,F401
                             project, ready, said, tool_request, turn_done, ws)

POSIX = os.name == "posix"


# --------------------------------------------------------------------------- #
# remote mode
# --------------------------------------------------------------------------- #

def test_remote_never_reads_jojos_own_data(clean_env, monkeypatch):
    home = clean_env / "home"
    (home / ".config" / "jojocode-ai").mkdir(parents=True)
    (home / ".config" / "jojocode-ai" / "credentials").write_text('{"t": "SECRET"}', encoding="utf-8")
    monkeypatch.setenv("JOJO_CONFIG", str(home / ".config" / "jojocode-ai"))
    out = remote_exec.execute("read_file", {"path": ".config/jojocode-ai/credentials"}, str(home))
    assert out["ok"] is False and "SECRET" not in str(out)
    out = remote_exec.execute("list_dir", {"path": ".config/jojocode-ai"}, str(home))
    assert out["ok"] is False


def test_remote_paths_with_nul_or_tilde_are_refused_not_raised(clean_env):
    assert remote_exec._jail(str(clean_env), "a\x00b") is None
    assert remote_exec._jail(str(clean_env), "~/x") is None
    assert remote_exec._jail(str(clean_env), ' "notes.txt" ') == os.path.realpath(clean_env / "notes.txt")


def test_remote_write_keeps_crlf_written_on_purpose(clean_env):
    remote_exec.execute("write_file", {"path": "x.bat", "content": "@echo off\r\necho hi\r\n"},
                        str(clean_env))
    assert (clean_env / "x.bat").read_bytes() == b"@echo off\r\necho hi\r\n"


@pytest.mark.skipif(not POSIX, reason="symlinks")
def test_remote_auto_edit_asks_for_a_symlinked_dot_file(project, ws, agents):
    (project / "config").mkdir()
    (project / "config" / "envrc").write_text("ok\n", encoding="utf-8")
    os.symlink(project / "config" / "envrc", project / ".envrc")
    srv = ws(lambda peer: None)
    agent = agents(srv, project)
    assert agent._pre_approved("write_file", {"path": ".envrc"}) is False
    assert agent._pre_approved("write_file", {"path": "config/envrc"}) is True


def test_close_on_a_dead_socket_does_not_raise(clean_env):
    c = wsjson.WSClient("ws://127.0.0.1:9/agent")
    a, b = socket.socketpair()
    b.close()
    a.close()
    c._sock = a
    c.close()                                          # used to raise WSClosed


def test_a_replay_from_a_turn_given_up_on_is_not_run_again(project, ws, agents):
    count = project / "count.txt"
    req = tool_request("w1", "run_command", {"cmd": "echo once >> count.txt"})

    def handler(peer):
        if peer.index == 0:
            ready(peer)
            peer.expect("user_turn")
            peer.send(req)
            peer.expect("tool_result")
            peer.send({"t": "closing", "reason": "going away", "resumable": True})
            peer.drain()
            return
        ready(peer, rt="rt-0")
        peer.expect("user_turn")
        peer.send({"t": "error", "code": "bad_message", "message": "a turn is already in progress"})
        peer.send(req)                                  # the server replays it
        peer.expect("tool_result")
        peer.send({"t": "assistant_delta", "text": "old ending. "})
        peer.send(turn_done())
        peer.expect("user_turn")                       # ours, sent again
        peer.send({"t": "assistant_delta", "text": "new answer"})
        peer.send(turn_done())
        peer.drain()

    agent = agents(ws(handler), project, auto_bash=True)
    list(agent.run("first"))
    events = list(agent.run("second"))
    assert count.read_text(encoding="utf-8").count("once") == 1
    assert said(events).endswith("new answer")
    assert any("sent when it ends" in e[1] for e in kinds(events, "info"))


def test_a_turn_that_ended_while_disconnected_ends_here_too(project, ws, agents, monkeypatch):
    monkeypatch.setattr(remote, "LIVENESS", 0.4)

    def handler(peer):
        if peer.index == 0:
            ready(peer)
            peer.expect("user_turn")
            peer.send({"t": "assistant_delta", "text": "Here is "})
            peer.drain()
            return
        hello = peer.expect("hello")
        peer.send({"t": "session_ready", "protocol": 1, "sessionId": "s-0", "seatNo": 1,
                   "resumeToken": hello.get("resumeToken"), "resumed": True,
                   "turnActive": False})
        peer.drain()

    agent = agents(ws(handler), project)
    t0 = time.monotonic()
    events = list(agent.run("plan it"))
    assert time.monotonic() - t0 < 5
    assert any("was lost" in e for e in errors(events)), events


def test_two_requests_sent_together_share_the_servers_clock(project, ws, agents):
    results = []

    def handler(peer):
        ready(peer)
        peer.expect("user_turn")
        peer.send(tool_request("A", "run_command", {"cmd": "echo a"}, timeout_ms=1500))
        peer.send(tool_request("B", "run_command", {"cmd": "echo b > b.txt"}, timeout_ms=1500))
        results.append(peer.expect("tool_result"))
        results.append(peer.expect("tool_result"))
        peer.send(turn_done())
        peer.drain()

    agent = agents(ws(handler), project, approver=Approver(answer=True, delay=0.7))
    list(agent.run("both"))
    by_id = {r["id"]: r for r in results}
    assert by_id["B"].get("refused") == "timeout"      # the second approval came too late
    assert not (project / "b.txt").exists()


def test_a_new_server_session_is_announced(project, ws, agents):
    def handler(peer):
        ready(peer, sid=f"sess-{peer.index}", rt=f"rt-{peer.index}")
        peer.expect("user_turn")
        peer.send(turn_done())
        peer.drain()

    agent = agents(ws(handler), project)
    list(agent.run("one"))
    agent._hard_close()
    agent.resume_token = None                          # as after an idle release
    events = list(agent.run("two"))
    assert any("new conversation" in e[1] for e in kinds(events, "info"))


def test_websocket_proxy_without_a_port_is_port_80(clean_env, monkeypatch):
    for k in ("HTTP_PROXY", "http_proxy"):
        monkeypatch.setenv(k, "http://proxy.example")
    seen = []

    def fake(addr, timeout=None, *a, **k):
        seen.append(addr)
        raise OSError("stop here")
    monkeypatch.setattr(socket, "create_connection", fake)
    with pytest.raises(Exception):
        wsjson.WSClient("ws://gpu.example:7420/agent").connect()
    assert seen and seen[0] == ("proxy.example", 80)


def test_headless_one_shot_that_was_cut_off_fails():
    events = [("user", "x"), ("assistant", "half an ans"),
              ("info", "the reply was cut off: it reached the context limit", "truncated"),
              ("final", "half an ans", True), ("turn_end",)]
    class FakeAgent:
        def run(self, text):
            return iter(events)
    assert headless._render(FakeAgent(), "x") is False


def test_embeddings_follow_the_chat_base_url():
    from jojocode_ai.store import _openai_base
    assert _openai_base("http://127.0.0.1:1234") == "http://127.0.0.1:1234/v1"
    assert _openai_base("https://x.example/v1beta/openai") == "https://x.example/v1beta/openai"


# --------------------------------------------------------------------------- #
# the web tools
# --------------------------------------------------------------------------- #

def test_table_cells_are_kept_apart_and_an_unclosed_head_is_not_the_page():
    assert "timeout | 30" in websearch.html_to_text(
        "<table><tr><td>timeout</td><td>30</td></tr></table>")
    assert websearch.html_to_text(
        "<html><head><meta charset=utf-8><title>T</title><h1>Install</h1><p>Run it.</p>"
    ) == "Install\n\nRun it."


def test_latin1_labels_decode_as_windows_1252_and_utf16_is_not_binary():
    assert websearch._decode("It\x92s".encode("latin-1"), "text/plain; charset=iso-8859-1") == "It’s"
    assert websearch._decode("hi there".encode("utf-16"), "text/plain; charset=utf-16") == "hi there"


def test_hosts_idna_2003_would_change_are_refused():
    with pytest.raises(websearch.WebError):
        websearch._normalize("https://faß.de/")


def test_behind_a_proxy_a_name_this_machine_cannot_resolve_is_left_to_it():
    assert websearch._vet("https://nowhere.invalid/", proxied=True)[2] == []
    with pytest.raises(websearch.WebError):
        websearch._vet("https://nowhere.invalid/")
    with pytest.raises(websearch.WebError):
        websearch._vet("http://127.0.0.1/", proxied=True)


def _serve(handler):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(8)

    def run():
        while True:
            try:
                c, _ = s.accept()
            except OSError:
                return
            threading.Thread(target=handler, args=(c,), daemon=True).start()
    threading.Thread(target=run, daemon=True).start()
    return s, s.getsockname()[1]


@pytest.fixture
def pinned(monkeypatch):
    monkeypatch.setattr(websearch, "_vet", lambda url, proxied=False: (
        "t.test", int(url.split(":")[2].split("/")[0]), ["127.0.0.1"]))
    servers = []
    yield lambda h: servers.append(_serve(h)) or servers[-1][1]
    for s, _ in servers:
        s.close()


def test_an_early_hints_response_is_skipped(pinned):
    def h(c):
        c.recv(65536)
        body = b"<html><body><p>real page</p></body></html>"
        c.sendall(b"HTTP/1.1 103 Early Hints\r\nLink: </s.css>\r\n\r\n"
                  b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: %d\r\n\r\n"
                  % len(body) + body)
        c.close()
    port = pinned(h)
    assert "real page" in websearch.fetch(f"http://t.test:{port}/")


def test_a_trickling_server_cannot_hold_the_fetch_past_the_deadline(pinned, monkeypatch):
    monkeypatch.setattr(websearch, "DEADLINE", 1.5)
    monkeypatch.setattr(websearch, "TIMEOUT", 1.0)

    def h(c):
        c.recv(65536)
        try:
            c.sendall(b"HTTP/1.1 200 OK\r\n")
            for _ in range(60):
                c.sendall(b"X-A: b\r\n")
                time.sleep(0.4)
        except OSError:
            pass
    port = pinned(h)
    t0 = time.monotonic()
    with pytest.raises(websearch.WebError, match="timed out"):
        websearch.fetch(f"http://t.test:{port}/")
    assert time.monotonic() - t0 < 4


def test_a_raw_utf8_redirect_lands_where_it_points(pinned):
    seen = []

    def h(c):
        req = c.recv(65536).split(b"\r\n", 1)[0]
        seen.append(req)
        if b"/start" in req:
            c.sendall(b"HTTP/1.1 301 Moved\r\nLocation: /wiki/Caf\xc3\xa9\r\nContent-Length: 0\r\n\r\n")
        else:
            c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\n\r\nok")
        c.close()
    port = pinned(h)
    websearch.fetch(f"http://t.test:{port}/start")
    assert seen[-1].startswith(b"GET /wiki/Caf%C3%A9 ")


def test_a_dead_first_address_falls_back_to_the_next_vetted_one():
    s, port = _serve(lambda c: c.close())
    try:
        dead = socket.socket()
        dead.bind(("127.0.0.1", 0))
        dead_port_ip = "127.0.0.2" if POSIX else "127.0.0.1"
        sock = websearch._dial([dead_port_ip, "127.0.0.1"], port, 2)
        sock.close()
        dead.close()
    finally:
        s.close()


# --------------------------------------------------------------------------- #
# the screen
# --------------------------------------------------------------------------- #

def test_visible_names_bidi_and_zero_width_characters():
    shown = cols.visible("ls ‮txt.exe​")
    assert "⟨U+202E⟩" in shown and "⟨U+200B⟩" in shown and "‮" not in shown


def test_store_close_stops_its_thread(clean_env):
    from jojocode_ai.store import Store
    s = Store("http://127.0.0.1:9", "nomic-embed-text", False)
    s.close()
    assert not s._worker.is_alive()


# --------------------------------------------------------------------------- #
# Windows
# --------------------------------------------------------------------------- #

def test_argv_lists_are_quoted_for_cmd_exe(monkeypatch):
    monkeypatch.setattr(shell, "IS_WINDOWS", True)
    assert shell.as_command(["echo", "hi&calc"]) == 'echo "hi&calc"'
    assert shell.as_command(["python", "-c", 'print("a & b")']) == 'python -c "print(""a & b"")"'
    assert shell.as_command(["git", "status"]) == "git status"


def test_utf16_command_output_is_decoded():
    assert shell._decode("Ubuntu  Running\r\n".encode("utf-16-le")) == "Ubuntu  Running\n"
    assert shell._decode(b"\xff\xfe ok") .endswith("ok")          # two stray bytes are not UTF-16


def test_windows_runnable_files_are_never_auto_written(tmp_path, monkeypatch):
    monkeypatch.setattr(policy, "_SHORT_NAMES", True)
    root = tmp_path / "proj"
    root.mkdir()
    for name in ("git.cmd", "python.bat", "npm.ps1", "node.js"):
        assert policy.auto_write_ok(str(root / name), str(root)) is False, name
    assert policy.auto_write_ok(str(root / "app.py"), str(root)) is True


def test_the_python_search_reads_utf16_files(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "_which_rg", lambda: None)
    monkeypatch.setenv("JOJO_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("JOJO_CONFIG", str(tmp_path / "cfg"))
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "build.log").write_bytes("﻿Program.cs(3,1): error CS1002\r\n".encode("utf-16-le"))
    assert "CS1002" in run_tool("search", {"pattern": "CS1002"}, Ctx(cwd=str(proj)))


def test_the_mintty_fallback_repl_asks_instead_of_denying(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    cfg = Config(host="http://127.0.0.1:9", model="m")
    assert headless._approver(cfg, interactive=True)("run_bash", {"command": "ls"}) is True
    assert headless._approver(cfg)("run_bash", {"command": "ls"}) is False


def test_login_code_prompt_works_without_a_console(monkeypatch):
    from jojocode_ai import auth
    monkeypatch.setattr(auth.os, "name", "nt")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    monkeypatch.setattr("builtins.input", lambda prompt="": " 123456 ")
    monkeypatch.setattr(auth.getpass, "getpass", lambda *a, **k: pytest.fail("getpass used"))
    assert auth._read_code() == "123456"


def test_a_remote_command_cmd_exe_cannot_run_is_refused_before_asking(project, ws, agents,
                                                                         monkeypatch):
    monkeypatch.setattr(shell, "IS_WINDOWS", True)
    results = []

    def handler(peer):
        ready(peer)
        peer.expect("user_turn")
        peer.send(tool_request("m", "run_command", {"cmd": "npm install\nnpm test"}))
        results.append(peer.expect("tool_result"))
        peer.send(turn_done())
        peer.drain()

    ask = Approver(answer=True)
    list(agents(ws(handler), project, approver=ask).run("go"))
    assert ask.calls == [] and results[0]["ok"] is False and "first line" in results[0]["error"]
