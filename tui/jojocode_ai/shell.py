"""Running one shell command for a tool call. Shared by tools.py (local mode)
and remote_exec.py (remote mode) so the two cannot drift apart on this.

── Why this is not just subprocess.run(cmd, shell=True) ────────────────────

Each of these hung the agent, lost output or broke the screen:

- **stdin was the user's terminal.** A command that reads input (`npm init`,
  `git commit` opening an editor, a bare `python`) fought curses for the
  keyboard until the timeout. Commands get an empty stdin; on POSIX a session
  of their own, so they cannot open /dev/tty behind our back either; on
  Windows no console at all, so they cannot read keystrokes from, or draw
  over, the one the TUI is using.
- **Output went through pipes.** A command that starts a server in the
  background (`npm run dev &`) left it holding the pipe, so the call waited
  out the whole timeout and then killed the server it was meant to start; a
  chatty command buffered every byte in memory (gigabytes, for a runaway
  loop). Output now goes to temporary files, the call returns when the shell
  exits, background jobs keep running, and only the head and tail are read.
- **Nothing could stop it.** A timeout or the user's Ctrl-C (`cancel`) now
  kills the whole process tree (POSIX: killpg; Windows: taskkill /T).
- **Windows specifics.** cmd.exe runs only the first line of a multi-line
  command, and silently runs in C:\\Windows when started in a UNC directory;
  both are refused up front (precheck) with what to do instead. Its built-ins
  write in the console (OEM) code page, not the ANSI one.
"""

from __future__ import annotations

import atexit
import codecs
import locale
import os
import re
import shlex
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass

IS_WINDOWS = os.name == "nt"
HEAD = 48_000            # bytes kept from the start of each stream
TAIL = 16_000            # ...and from the end
_CREATE_NO_WINDOW = 0x08000000
_LIVE: set = set()             # commands still running: quitting kills their trees
_LIVE_LOCK = threading.Lock()


@dataclass
class Result:
    out: str
    err: str
    code: int | None
    timed_out: bool = False
    interrupted: bool = False


def _oem_codepage() -> str | None:
    if not IS_WINDOWS:
        return None
    try:
        import ctypes
        return f"cp{ctypes.windll.kernel32.GetOEMCP()}"
    except (AttributeError, OSError):
        return None


def _decode(data: bytes, cut_start: bool = False, cut_end: bool = False) -> str:
    """Bytes a command wrote, as text. `cut_start` / `cut_end`: the bytes were
    cut out of a longer stream there, so a character may be split in half —
    which must not send the whole kept output to the legacy code page."""
    if not data:
        return ""
    wide = _wide_encoding(data)
    if wide:
        # wsl.exe and wmic write UTF-16 when redirected: read as UTF-8 it
        # reached the model NUL-interleaved and drew as blank lines
        return data.decode(wide, errors="replace").lstrip("\ufeff").replace("\r\n", "\n")
    utf8 = data
    if cut_start:
        i = 0
        while i < min(3, len(utf8)) and 0x80 <= utf8[i] <= 0xBF:
            i += 1                       # the tail end of a split character
        utf8 = utf8[i:]
    try:
        if cut_end:
            text = codecs.getincrementaldecoder("utf-8")().decode(utf8, final=False)
        else:
            text = utf8.decode("utf-8")
    except UnicodeDecodeError:
        # cmd.exe built-ins write the OEM code page; most else, the ANSI one
        enc = _oem_codepage() or locale.getpreferredencoding(False) or "utf-8"
        try:
            text = data.decode(enc, errors="replace")
        except LookupError:
            text = data.decode("utf-8", errors="replace")
    return text.replace("\r\n", "\n")


def _wide_encoding(data: bytes) -> str | None:
    from .textfiles import sniff
    head = data[:8192]
    found = sniff(head)
    # The NULs too, not a BOM alone: two stray bytes FF FE at the start of
    # ordinary output are not a reason to read all of it as UTF-16.
    if found and found[0].startswith(("utf-16", "utf-32")) and b"\x00" in head:
        return found[0]
    return None


def _read_bounded(f) -> str:
    """The head and tail of a captured stream, with what was dropped counted."""
    f.flush()
    size = f.seek(0, os.SEEK_END)
    f.seek(0)
    if size <= HEAD + TAIL:
        return _decode(f.read())
    head = f.read(HEAD)
    f.seek(size - TAIL)
    tail = f.read(TAIL)
    return (_decode(head, cut_end=True) + f"\n... [{size - HEAD - TAIL} bytes omitted] ...\n"
            + _decode(tail, cut_start=True))


_OMITTED = re.compile(r"\[(\d+) bytes omitted\]")


def omitted_bytes(text: str) -> int:
    """Bytes _read_bounded already dropped inside `text` — so a later clip that
    cuts its marker out can still say how much is missing."""
    return sum(int(n) for n in _OMITTED.findall(text))


def _taskkill() -> str:
    # by full path: the bare name is looked up in the current directory first
    return os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "taskkill.exe")


def _kill_tree(p: subprocess.Popen) -> None:
    if IS_WINDOWS:
        try:
            subprocess.run([_taskkill(), "/F", "/T", "/PID", str(p.pid)],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=15,
                           creationflags=_CREATE_NO_WINDOW)
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


def kill_all() -> None:
    """Stop every command still running (the user quit, or the process exits)."""
    with _LIVE_LOCK:
        live = list(_LIVE)
    for p in live:
        if p.poll() is None:
            _kill_tree(p)


atexit.register(kill_all)


def as_command(cmd) -> str:
    """Models sometimes send argv lists (["bash", "-lc", "ls"]); join them properly."""
    if isinstance(cmd, (list, tuple)):
        parts = [str(c) for c in cmd]
        return " ".join(_cmd_quote(p) for p in parts) if IS_WINDOWS else shlex.join(parts)
    return str(cmd)


_CMD_SPECIAL = set(' \t&|<>^()%!"')


def _cmd_quote(arg: str) -> str:
    """One argument for cmd.exe. list2cmdline quotes for the C runtime only:
    in ["echo", "hi&calc"] the & stayed bare and cmd.exe ran calc. Inside
    double quotes cmd.exe's operators are text; a doubled "" is a quote to
    both cmd.exe and the C runtime."""
    if arg and not (set(arg) & _CMD_SPECIAL):
        return arg
    return '"' + arg.replace('"', '""') + '"'


def precheck(cmd, cwd: str) -> str | None:
    """Why this command cannot run here, said before anyone is asked to approve it.

    Two cmd.exe limits, refused plainly rather than worked around: it runs
    only the first line of a multi-line command (a batch file would change
    what `%` means), and it will not start in a UNC directory — it silently
    runs in C:\\Windows instead, and pushd would leak a drive letter a call.
    """
    if not IS_WINDOWS:
        return None
    text = as_command(cmd)
    if "\n" in text.strip():
        return ("cmd.exe runs only the first line of a multi-line command; join the "
                "commands with && (or write a .cmd file and run that)")
    if (cwd or "").startswith("\\\\"):
        return (f"cmd.exe cannot run in a network (UNC) directory ({cwd}); map it to a drive "
                "letter first (net use), or run jojo inside WSL for \\\\wsl$ paths")
    return None


def run(cmd, cwd: str, timeout: float, cancel=None) -> Result:
    """Run `cmd` through the platform shell (sh / cmd.exe)."""
    cmd = as_command(cmd)
    why = precheck(cmd, cwd)
    if why:
        return Result("", why, None)
    env = dict(os.environ)
    env.setdefault("GIT_TERMINAL_PROMPT", "0")   # fail, rather than wait for a password
    env.setdefault("PYTHONIOENCODING", "utf-8")  # a Python child on Windows would write cp1252
    env.setdefault("GCM_INTERACTIVE", "never")    # Git Credential Manager: no pop-up prompts
    if IS_WINDOWS:
        # cmd.exe looks in the current directory before PATH: a git.cmd the
        # model wrote into the project (auto-edit, no prompt) ran when the
        # user approved "git status".
        env["NoDefaultCurrentDirectoryInExePath"] = "1"
    # Overridden, not defaulted: with core.editor "code --wait" (or Notepad),
    # `git commit` without -m opened a window and blocked until the timeout.
    # `true` exits at once: the commit aborts for an empty message, and
    # rebase --continue / --amend keep the message already prepared.
    env["GIT_EDITOR"] = env["GIT_SEQUENCE_EDITOR"] = "true"
    kw: dict = {}
    if IS_WINDOWS:
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW
    else:
        kw["start_new_session"] = True
    timed_out = interrupted = False
    with tempfile.TemporaryFile() as out_f, tempfile.TemporaryFile() as err_f:
        p = subprocess.Popen(cmd, shell=True, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                             stdout=out_f, stderr=err_f, **kw)
        with _LIVE_LOCK:
            _LIVE.add(p)
        deadline = time.monotonic() + timeout
        try:
            while True:
                try:
                    p.wait(timeout=0.1)
                    break
                except subprocess.TimeoutExpired:
                    if cancel is not None and cancel():
                        interrupted = True
                    elif time.monotonic() >= deadline:
                        timed_out = True
                    else:
                        continue
                    _kill_tree(p)
                    try:
                        p.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
                    break
        except BaseException:
            # KeyboardInterrupt in headless mode: never leave it running
            _kill_tree(p)
            raise
        finally:
            with _LIVE_LOCK:
                _LIVE.discard(p)
        return Result(_read_bounded(out_f), _read_bounded(err_f), p.returncode,
                      timed_out, interrupted)
