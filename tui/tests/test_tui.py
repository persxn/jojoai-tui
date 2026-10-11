"""The curses UI: what reaches the screen, and what Ctrl-C does.

- Ctrl-C mid-turn quit the whole app. curses.wrapper leaves the terminal in
  cbreak mode, where Ctrl-C is still SIGINT, so it arrived as a
  KeyboardInterrupt in the main loop instead of as the key the loop handles —
  the session was gone instead of the turn being interrupted. Driven here
  through a real pty against the real TUI.
- A `finish` summary vanished when any earlier step of the same turn had
  streamed prose: "was anything streamed?" stood in for "was *this* streamed?".
- Tool output went to curses raw: colour codes drew as ^[[32m, "\\r" progress
  bars jumped the cursor to column 0, tabs ran past the counted width.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tui"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

curses = pytest.importorskip("curses")

from jojocode_ai import tui  # noqa: E402
from jojocode_ai.config import Config  # noqa: E402


def make_tui():
    agent = SimpleNamespace(store=None, messages=[], ctx=SimpleNamespace(cwd="."),
                            client=SimpleNamespace(model="m"), interrupt=lambda: None)
    return tui.TUI(Config(model="m"), lambda approver: agent)


def feed(ui, *events):
    for ev in events:
        ui.events.put(ev)
    ui._drain()
    return [(b.kind, b.text) for b in ui.blocks]


def test_finish_summary_appears_after_streamed_prose():
    ui = make_tui()
    blocks = feed(ui, ("assistant", "Looking."), ("tool_start", "list_dir", {}),
                  ("tool_end", "list_dir", "a.py"), ("final", "Listed it.", False))
    assert blocks[-1] == ("assistant", "Listed it.")


def test_a_streamed_answer_is_not_printed_twice():
    ui = make_tui()
    blocks = feed(ui, ("assistant", "Hello."), ("final", "Hello.", True))
    assert [b for b in blocks if b[0] == "assistant"] == [("assistant", "Hello.")]


def test_remote_final_without_the_flag_keeps_the_old_rule():
    ui = make_tui()
    ui._streamed_turn = False
    assert feed(ui, ("final", "x"))[-1] == ("assistant", "x")


def test_tool_output_is_made_safe_to_draw():
    raw = "\x1b[32mPASSED\x1b[0m\ttest_a\r\n 10%\r 55%\r100%\nend\x07\x1b]0;title\x07"
    assert tui._clean(raw) == "PASSED  test_a\n100%\nend"     # tabs go to tab stops
    assert tui._clean_delta("a\r\n\tb\x00") == "a\n    b"


# --------------------------------------------------------------------------- #
# the real thing, through a pty
# --------------------------------------------------------------------------- #

@pytest.mark.skipif(os.name != "posix", reason="needs a pty")
def test_ctrl_c_interrupts_the_turn_and_only_quits_when_idle(tmp_path):
    import fcntl
    import pty
    import select
    import struct
    import subprocess
    import termios

    from fake_ollama import FakeModel, FakeOllama

    def slow(body):
        for i in range(400):
            time.sleep(0.05)
            yield {"content": f"word{i} "}

    def read(fd, secs):
        end, buf = time.time() + secs, b""
        while time.time() < end:
            r, _, _ = select.select([fd], [], [], 0.05)
            if r:
                try:
                    buf += os.read(fd, 65536)
                except OSError:
                    break
        return buf

    def exited(pid, secs):
        end = time.time() + secs
        while time.time() < end:
            done, status = os.waitpid(pid, os.WNOHANG)
            if done:
                return status
            time.sleep(0.05)
        return None

    with FakeOllama(FakeModel("slow:1b", ["completion", "tools"], slow)) as srv:
        env = dict(os.environ, TERM="xterm-256color", JOJO_DATA=str(tmp_path / "d"),
                   JOJO_CONFIG=str(tmp_path / "c"), PYTHONPATH=str(ROOT / "tui"))
        fd, slave = pty.openpty()
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
        # The pty must be the child's *controlling* terminal, or Ctrl-C would
        # never become SIGINT and the old bug could not show itself. The
        # child claims it (TIOCSCTTY) in a session of its own, then becomes
        # the TUI — no fork() from this threaded test process.
        claim = ("import os, sys, fcntl, termios;"
                 "fd = os.open(os.ttyname(0), os.O_RDWR); fcntl.ioctl(fd, termios.TIOCSCTTY, 0);"
                 "os.execv(sys.executable, [sys.executable] + sys.argv[1:])")
        proc = subprocess.Popen(
            [sys.executable, "-c", claim, "-m", "jojocode_ai", "--host", srv.url,
             "--model", "slow:1b", "--think", "off", "--no-mouse"],
            stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True)
        os.close(slave)
        pid = proc.pid
        try:
            read(fd, 1.5)
            os.write(fd, b" ")                         # skip the splash
            read(fd, 0.5)
            for ch in b"say hi":
                os.write(fd, bytes([ch]))
                time.sleep(0.03)
            time.sleep(0.2)
            os.write(fd, b"\r")
            read(fd, 1.5)                              # the reply is streaming now

            os.write(fd, b"\x03")
            screen = read(fd, 2.0)
            assert exited(pid, 0.1) is None, "Ctrl-C mid-turn quit the app"
            assert b"interrupt" in screen

            os.write(fd, b"\x03")                      # idle, empty input: quit
            read(fd, 0.5)
            status = exited(pid, 10)
            assert status is not None, "Ctrl-C at an idle, empty prompt should quit"
            assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
        finally:
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass
            os.close(fd)
