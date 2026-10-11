"""Plain-text REPL / one-shot runner for JojoAI (no curses).

Useful for scripting, piping, and testing the agent without a full terminal UI.
"""

from __future__ import annotations

import os
import sys

from .agent import Agent
from .config import Config

C = {
    "dim": "\033[2m", "cyan": "\033[36m", "green": "\033[32m",
    "yellow": "\033[33m", "red": "\033[31m", "bold": "\033[1m", "reset": "\033[0m",
}


def _enable_ansi() -> bool:
    """Whether colour escapes will render rather than print as `←[2m`.

    A Windows console only interprets them once the program asks for it
    (ENABLE_VIRTUAL_TERMINAL_PROCESSING); Windows Terminal does by default,
    conhost — what cmd.exe and PowerShell open in on many machines — does not.
    """
    if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        return False
    if os.name != "nt":
        return True
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.windll.kernel32
        h = k32.GetStdHandle(-11)                      # STD_OUTPUT_HANDLE
        mode = wintypes.DWORD()
        if not k32.GetConsoleMode(h, ctypes.byref(mode)):
            return False
        return bool(k32.SetConsoleMode(h, mode.value | 0x0004))
    except (AttributeError, OSError, ValueError):
        return False


_COLOR = None


def _c(key, text):
    global _COLOR
    if _COLOR is None:
        _COLOR = _enable_ansi()
    if not _COLOR:
        return text
    return f"{C[key]}{text}{C['reset']}"


def _approver(cfg: Config):
    def ask(name, args):
        if cfg.approves(name):
            return True
        if not sys.stdin.isatty():
            print(_c("yellow", f"[auto-deny {name} - no TTY to confirm]"))
            return False
        preview = str(args)
        if len(preview) > 200:
            preview = preview[:200] + "..."
        ans = input(_c("yellow", f"Allow {name} {preview} ? [y/N/a] ")).strip().lower()
        if ans == "a":
            cfg.auto_approve.add(name)
            return True
        return ans == "y"
    return ask


def _render(agent: Agent, text: str):
    in_thinking = False
    streamed = False
    for ev in agent.run(text):
        kind = ev[0]
        if kind == "assistant":
            streamed = True
        if kind == "thinking":
            if not in_thinking:
                sys.stdout.write(_c("dim", "\n· thinking: "))
                in_thinking = True
            sys.stdout.write(_c("dim", ev[1]))
            sys.stdout.flush()
        elif kind == "assistant":
            if in_thinking:
                sys.stdout.write("\n")
                in_thinking = False
            sys.stdout.write(ev[1])
            sys.stdout.flush()
        elif kind == "tool_start":
            in_thinking = False
            print(_c("cyan", f"\n→ {ev[1]}({_short(ev[2])})"))
        elif kind == "tool_end":
            out = ev[2]
            print(_c("dim", out if len(out) < 1500 else out[:1500] + "\n…"))
        elif kind == "recall":
            print(_c("dim", f"· recalled {ev[1]} snippet(s) from earlier chats"))
        elif kind == "info":
            print(_c("dim", f"· {ev[1]}"))
        elif kind == "tool_denied":
            print(_c("red", f"✗ denied: {ev[1]}"))
        elif kind == "final":
            shown = ev[2] if len(ev) > 2 else streamed
            if ev[1] and not shown:
                print(_c("green", "\n" + ev[1]))
        elif kind == "error":
            print(_c("red", f"\n[error] {ev[1]}"))
        elif kind == "stats":
            s = ev[1]
            ec, ed = s.get("eval_count"), s.get("eval_duration")
            if ec and ed:
                print(_c("dim", f"  [{ec} tok, {ec / (ed / 1e9):.1f} tok/s]"))


def _short(args):
    s = ", ".join(f"{k}={v!r}" for k, v in list(args.items())[:3])
    return s if len(s) < 120 else s[:120] + "…"


def run(cfg: Config, one_shot: str | None = None):
    # Piped or redirected on Windows, stdout is cp1252 and the first "→" this
    # prints raised UnicodeEncodeError — in the mode meant for scripting.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    agent = Agent(cfg, approver=_approver(cfg))
    try:
        from .store import Store
        agent.store = Store(cfg.host, cfg.embed_model, cfg.rag)
    except Exception as e:  # noqa: BLE001
        print(_c("yellow", f"history store off: {e}"))
    try:
        v = agent.client.version()
        print(_c("dim", f"connected to Ollama {v} · model {cfg.model} · cwd {cfg.cwd}"))
        if not agent.client.has_model(cfg.model):
            print(_c("yellow", f"warning: model {cfg.model!r} not found. "
                               f"Run: ollama pull {cfg.model}"))
    except Exception as e:  # noqa: BLE001
        print(_c("red", f"[error] {e}"))
        return 1

    if one_shot is not None:
        _render(agent, one_shot)
        print()
        return 0

    print(_c("dim", "Type a task. Ctrl-D or /quit to exit. /reset clears history."))
    while True:
        try:
            line = input(_c("bold", "\njojo> "))
        except EOFError:
            print()
            break
        line = line.strip()
        if not line:
            continue
        if line in ("/quit", "/exit"):
            break
        if line == "/reset":
            agent.reset()
            print(_c("dim", "history cleared"))
            continue
        try:
            _render(agent, line)
        except KeyboardInterrupt:
            agent.interrupt()
            print(_c("yellow", "\n(interrupted)"))
    return 0
