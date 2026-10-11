"""Running one shell command for a tool call. Shared by tools.py (local mode)
and remote_exec.py (remote mode) so the two cannot drift apart on this.

── Why this is not just subprocess.run(cmd, shell=True) ────────────────────

That is what both used to call, and it failed three ways on every platform:

- **stdin was the user's terminal.** A command that reads input — `npm init`,
  `git commit` without -m opening an editor, a bare `python`, a credential
  prompt — competed with curses for the keyboard and sat there until the
  timeout, with the screen torn up. Commands now get an empty stdin, and on
  POSIX a session of their own, so they cannot open /dev/tty behind our back
  either (sudo, git's credential helper).
- **The timeout did not stop anything.** `subprocess.run` kills the shell and
  then waits for the pipes to close — which a server the command started
  (`npm run dev`, `python -m http.server`) never does. The agent waited
  forever. The whole process group goes now (POSIX: killpg; Windows:
  taskkill /T, which takes the tree).
- **Undecodable output was an exception.** `text=True` decodes strictly, so a
  single stray byte (`cat` on a binary, a Windows console code page) turned
  the result into `error: UnicodeDecodeError` and the output was lost.
"""

from __future__ import annotations

import locale
import os
import signal
import subprocess

IS_WINDOWS = os.name == "nt"


def _decode(data: bytes | None) -> str:
    if not data:
        return ""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        # cmd.exe built-ins and older tools write in the ANSI code page
        text = data.decode(locale.getpreferredencoding(False) or "utf-8", errors="replace")
    return text.replace("\r\n", "\n")


def _kill_tree(p: subprocess.Popen) -> None:
    if IS_WINDOWS:
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=15)
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            os.killpg(p.pid, signal.SIGKILL)     # start_new_session: pgid == pid
        except OSError:
            pass
    try:
        p.kill()
    except OSError:
        pass


def run(cmd: str, cwd: str, timeout: float) -> tuple[str, str, int | None, bool]:
    """Run `cmd` through the platform shell. Returns (stdout, stderr, exit code, timed_out)."""
    kw: dict = {}
    if IS_WINDOWS:
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    env = dict(os.environ)
    env.setdefault("GIT_TERMINAL_PROMPT", "0")   # fail, rather than wait for a password
    p = subprocess.Popen(cmd, shell=True, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kw)
    try:
        out, err = p.communicate(timeout=timeout)
        return _decode(out), _decode(err), p.returncode, False
    except subprocess.TimeoutExpired:
        _kill_tree(p)
        try:
            out, err = p.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            # Something that left the group still holds the pipes. Stop waiting.
            out = err = b""
            for pipe in (p.stdout, p.stderr):
                try:
                    pipe.close()
                except (OSError, AttributeError):
                    pass
        return _decode(out), _decode(err), p.returncode, True
