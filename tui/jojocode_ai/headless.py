"""Plain-text REPL / one-shot runner for JojoAI (no curses).

Useful for scripting, piping, and testing the agent without a full terminal UI.
"""

from __future__ import annotations

import os
import sys

from .agent import Agent
from .cols import clean, clean_delta, visible
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


def _approver(cfg: Config, interactive: bool = False):
    def ask(name, args):
        if cfg.approves(name):
            return True
        # `interactive`: the Git Bash / mintty fallback, where stdin is a pipe
        # but a person is typing — auto-denying there meant no command could
        # ever run. Real pipes and one-shot scripts still deny.
        if not (interactive or sys.stdin.isatty()):
            print(_c("yellow", f"[auto-deny {name} - no TTY to confirm]"))
            return False
        # The whole request, never a 200-character preview: what is cut off
        # is exactly where a prompt-injected command would put its payload.
        print(_c("yellow", f"\nAllow {name}?"))
        for k, v in (args or {}).items():
            body = visible(v if isinstance(v, str) else repr(v))
            lines = body.split("\n")          # all of it: what is cut is what goes unseen
            print(_c("yellow", f"  {k}:"))
            for ln in lines:
                print("    " + ln)
        try:
            ans = input(_c("yellow", "[y/N/a] ")).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        if ans == "a":
            cfg.auto_approve.add(name)
            return True
        return ans == "y"
    return ask


def _render(agent: Agent, text: str) -> bool:
    """Print one turn. True if it ended with an answer rather than an error."""
    in_thinking = False
    streamed = False
    failed = answered = False
    for ev in agent.run(text):
        kind = ev[0]
        if kind == "thinking":
            if not in_thinking:
                sys.stdout.write(_c("dim", "\n· thinking: "))
                in_thinking = True
            sys.stdout.write(_c("dim", clean_delta(ev[1])))
            sys.stdout.flush()
        elif kind == "assistant":
            streamed = answered = True
            if in_thinking:
                sys.stdout.write("\n")
                in_thinking = False
            # model text is printed, so it must not carry escape sequences: a
            # page could otherwise set the window title or the clipboard (OSC 52)
            sys.stdout.write(clean_delta(ev[1]))
            sys.stdout.flush()
        elif kind == "tool_start":
            in_thinking = False
            print(_c("cyan", f"\n→ {ev[1]}({_short(ev[2])})"))
        elif kind == "tool_end":
            out = clean(ev[2])
            print(_c("dim", out if len(out) < 1500 else out[:1500] + "\n…"))
        elif kind == "recall":
            print(_c("dim", f"· recalled {ev[1]} snippet(s) from earlier chats"))
        elif kind == "info":
            print(_c("dim", f"· {clean(ev[1])}"))
            if len(ev) > 2 and ev[2] == "truncated":
                failed = True               # half an answer is not a successful run
        elif kind == "tool_denied":
            print(_c("red", f"✗ denied: {ev[1]}"))
        elif kind == "final":
            shown = ev[2] if len(ev) > 2 else streamed
            if ev[1] and not shown:
                print(_c("green", "\n" + clean(ev[1])))
            if ev[1].startswith("(stopped"):
                failed = True
            elif ev[1]:
                answered = True
        elif kind == "error":
            failed = True
            print(_c("red", f"\n[error] {clean(ev[1])}"))
        elif kind == "stats":
            s = ev[1]
            ec, ed = s.get("eval_count"), s.get("eval_duration")
            if ec and ed:
                print(_c("dim", f"  [{ec} tok, {ec / (ed / 1e9):.1f} tok/s]"))
    return answered and not failed


def _short(args):
    if not isinstance(args, dict):
        return ""
    s = ", ".join(f"{k}={v!r}" for k, v in list(args.items())[:3])
    s = clean(s)
    return s if len(s) < 120 else s[:120] + "…"


def run(cfg: Config, one_shot: str | None = None, interactive: bool = False):
    # Piped or redirected on Windows, stdout is cp1252 and the first "→" this
    # prints raised UnicodeEncodeError — in the mode meant for scripting.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    agent = Agent(cfg, approver=_approver(cfg, interactive and one_shot is None))
    try:
        from .store import Store
        agent.store = Store(cfg.host, cfg.embed_model, cfg.rag,
                            api=getattr(agent.client, "api", "ollama"))
    except Exception as e:  # noqa: BLE001
        print(_c("yellow", f"history store off: {e}"))
    try:
        v = agent.client.version()
        label = f"Ollama {v}" if getattr(agent.client, "api", "ollama") == "ollama" else v
        print(_c("dim", f"connected to {label} · model {cfg.model} · cwd {cfg.cwd}"))
        if not agent.client.has_model(cfg.model):
            print(_c("yellow", f"warning: model {cfg.model!r} not found. "
                               f"Run: ollama pull {cfg.model}"))
    except Exception as e:  # noqa: BLE001
        print(_c("red", f"[error] {e}"))
        return 1

    if one_shot is not None:
        try:
            ok = _render(agent, one_shot)
        except KeyboardInterrupt:
            agent.close()
            print(_c("yellow", "\n(interrupted)"))
            return 130
        print()
        return 0 if ok else 1            # scripts need to know a turn failed

    print(_c("dim", "Type a task. Ctrl-D or /quit to exit. /reset clears history."))
    last_ctrl_c = 0.0
    while True:
        try:
            line = input(_c("bold", "\njojo> "))
        except EOFError:
            print()
            break
        except KeyboardInterrupt:
            import time
            if time.time() - last_ctrl_c < 2:
                print()
                break
            last_ctrl_c = time.time()
            print(_c("dim", "\n(Ctrl-C again, Ctrl-D or /quit to exit)"))
            continue
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
    agent.close()
    return 0
