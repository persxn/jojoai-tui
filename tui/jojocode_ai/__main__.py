"""Entry point:  jojo  [options]  ["one-shot task"]   (also: python -m jojocode_ai)"""

from __future__ import annotations

import argparse
import os
import sys

from . import __version__
from .config import Config


def _crash_log_path() -> str:
    base = os.environ.get("JOJO_DATA") or os.path.expanduser("~/.local/share/jojocode-ai")
    return os.path.join(base, "crash.log")


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="jojo",
        description="JojoCode AI TUI - a curses agentic coding assistant (local Ollama, or a remote server)",
    )
    ap.add_argument("task", nargs="*", help="run this task once and exit (headless)")
    ap.add_argument("--model", help="Ollama model tag (default: settings.json, else gpt-oss:120b)")
    ap.add_argument("--host", help="Ollama host URL")
    ap.add_argument("--cwd", help="working directory for tools")
    ap.add_argument("--ctx", type=int, help="context window (num_ctx)")
    ap.add_argument("--think", type=str.lower, choices=["off", "low", "medium", "high"],
                    help="reasoning effort; levels apply to gpt-oss, other thinking "
                         "models just think or don't")
    ap.add_argument("--max-steps", type=int, help="max tool iterations per turn")
    ap.add_argument("--yolo", action="store_true", help="auto-approve every tool")
    ap.add_argument("--trust", action="store_true",
                    help="run shell commands without asking (plus file edits)")
    ap.add_argument("--safe", action="store_true",
                    help="also ask before creating/editing files")
    ap.add_argument("--rag", action="store_true",
                    help="opt in to recalling context from your past chats")
    ap.add_argument("--no-mouse", action="store_true",
                    help="don't capture the mouse wheel (leave scrolling/selection to the terminal)")
    ap.add_argument("--headless", action="store_true", help="plain REPL, no curses")

    rg = ap.add_argument_group("remote mode (agent loop runs on a server)")
    rg.add_argument("--remote", metavar="URL",
                    help="server base URL, e.g. https://ai.jojocode.in")
    rg.add_argument("--project", metavar="LABEL",
                    help="human label for this project (never a path)")
    rg.add_argument("--login", action="store_true",
                    help="log in to --remote by email + OTP, save the token, then start")
    rg.add_argument("--email", help="email for --login (otherwise prompted)")
    rg.add_argument("--code", metavar="ACCESS_CODE",
                    help="save a demo access code as the token for --remote, then start")
    rg.add_argument("--logout", action="store_true",
                    help="forget the saved token for --remote and exit")

    ap.add_argument("--version", action="version", version=f"JojoCode AI TUI {__version__}")
    args = ap.parse_args(argv)

    cfg = Config()
    if args.model:
        cfg.model = args.model
    if args.host:
        cfg.host = args.host
    if args.cwd:
        cfg.cwd = args.cwd
    if args.ctx:
        cfg.num_ctx = args.ctx
    if args.think is not None:
        cfg.think = False if args.think == "off" else args.think
    if args.max_steps:
        cfg.max_steps = args.max_steps
    if args.yolo:
        cfg.auto_approve.add("*")
    if args.trust:
        cfg.auto_edit = cfg.auto_bash = True
    if args.safe:
        cfg.auto_edit = False
    if args.rag:
        cfg.rag = True
    if args.no_mouse:
        cfg.mouse = False
    if args.remote:
        cfg.remote_endpoint = args.remote.rstrip("/")
    if args.project:
        cfg.project_label = args.project

    one_shot = " ".join(args.task) if args.task else None

    # ---- remote mode -------------------------------------------------- #
    if cfg.remote_endpoint:
        from . import auth

        ep = cfg.remote_endpoint
        if args.logout:
            print("forgotten" if auth.forget(ep) else f"no saved credentials for {ep}")
            return 0
        if args.code:
            auth.stash_code(ep, args.code)
            print(f"saved access code for {ep}", file=sys.stderr)
        elif args.login:
            try:
                auth.otp_login(ep, args.email)
            except RuntimeError as e:
                print(f"login failed: {e}", file=sys.stderr)
                return 2

        token = auth.load_token(ep)
        if not token:
            print(f"not logged in to {ep}.\n  jojo --remote {ep} --login", file=sys.stderr)
            return 2
        if one_shot is not None or args.headless:
            print("remote mode needs the interactive TUI for tool approvals "
                  "— headless remote isn't supported yet.", file=sys.stderr)
            return 2

        try:
            import curses  # noqa: F401
        except ImportError:
            print("jojo: this terminal has no `curses` (Windows: pip install windows-curses)",
                  file=sys.stderr)
            return 2

        from .remote import RemoteAgent
        from .tui import run as run_tui
        return _run_tui_guarded(
            lambda: run_tui(cfg, lambda approver: RemoteAgent(cfg, token, approver)))

    # ---- local mode ------------------------------------------------- #
    if one_shot is not None or args.headless:
        from .headless import run as run_headless
        return run_headless(cfg, one_shot)

    try:
        import curses  # noqa: F401
    except ImportError:
        sys.stderr.write(
            "jojo: this terminal has no `curses` support.\n"
            "  Windows:  pip install windows-curses   (or use --headless, or WSL)\n"
            "  Otherwise, run with --headless for the plain REPL.\n"
        )
        return 2

    from .tui import run as run_tui
    return _run_tui_guarded(lambda: run_tui(cfg))


def _run_tui_guarded(launch) -> int:
    try:
        launch()
    except KeyboardInterrupt:
        pass
    except Exception:  # noqa: BLE001 — curses.wrapper has already restored the terminal
        import traceback

        path = _crash_log_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8", errors="replace") as f:
                f.write(traceback.format_exc() + "\n")
            where = path
        except OSError:
            where = "(could not write crash log)"
        sys.stderr.write(
            f"jojo crashed. The terminal has been restored.\nDetails: {where}\n"
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
