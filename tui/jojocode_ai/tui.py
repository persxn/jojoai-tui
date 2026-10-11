"""Curses UI for JojoAI.

green theme + gradients · an ASCII cat · keyboard shortcuts · slash commands ·
you on the right / jojo on the left · syntax-highlighted code · persistent
history with cross-chat recall (RAG).

── How the loop stays cheap and responsive ─────────────────────────────────

- **Draw only what changed.** Each block caches its wrapped rows, keyed by
  its version and the width; only the block that is streaming is re-wrapped.
  The whole transcript used to be re-wrapped 30 times a second — 260 ms a
  frame at 1,000 blocks, and two-thirds of a core while idle.
- **Wait on the keyboard, not a sleep.** get_wch() blocks with a timeout
  (short while something animates, long when idle), so a key is handled the
  moment it arrives, and an idle TUI goes quiet instead of repainting the
  header colours 30 times a second down an SSH link.
- **Read input in batches.** Everything the terminal has buffered is read
  before anything is drawn. A paste arrives as one batch, so its newlines and
  tabs are text, not Enter and "cycle think" — the old 12 ms timing test
  failed whenever a frame took longer than that. Bracketed paste is used
  where the terminal offers it.

── The approval prompt ─────────────────────────────────────────────────────

It is the product's main safety property, so: the whole command or edit is
shown (it used to be cut at 77 characters, so a `; curl … | sh` tail was
invisible); type-ahead is discarded and keys are ignored for a moment after it
appears (an `a` typed into a note used to answer it — "always allow
run_bash"); and "always" takes a second press.

── Width ─────────────────────────────────────────────────────────────────────

Text is measured in terminal columns (cols.py), not code points: CJK and emoji
take two, combining marks none.
"""

from __future__ import annotations

import curses
import datetime as _dt
import math
import os
import queue
import random
import re
import sys
import threading
import time

from .agent import Agent
from .cols import clean as _clean
from .cols import clean_delta as _clean_delta
from .cols import clip as _clip_cols
from .cols import visible as _visible
from .cols import width as _slen
from .cols import wrap as _wrap
from .config import Config
from .syntax import highlight_line, inline_segments, norm_lang, split_fences
from .theme import BARS, SPINNER, Theme

THINK_LEVELS = ["off", "low", "medium", "high"]

# ASCII wordmark for the boot splash (drawn with a gradient + moving glint)
LOGO = [
    r"     ██╗ ██████╗      ██╗ ██████╗      █████╗ ██╗",
    r"     ██║██╔═══██╗     ██║██╔═══██╗    ██╔══██╗██║",
    r"     ██║██║   ██║     ██║██║   ██║    ███████║██║",
    r"██   ██║██║   ██║██   ██║██║   ██║    ██╔══██║██║",
    r"╚█████╔╝╚██████╔╝╚█████╔╝╚██████╔╝    ██║  ██║██║",
    r" ╚════╝  ╚═════╝  ╚════╝  ╚═════╝     ╚═╝  ╚═╝╚═╝",
]
LOGO_SMALL = [
    r" _  _  _  _  __   _ ",
    r"|_||  |_||  |__|  | ",
    r"|  |_ |  |_ |  |  | ",
]
SPLASH_SECS = 1.9

HELP = """commands
  /help                 this screen
  /reset                fresh chat — nothing carried over (new saved session)
  /retry                run the last prompt again
  /tools                list the tools the model can call
  /think off|low|medium|high     set reasoning effort
  /model                what's running, and what's pulled
  /model <name> [--save]  switch model; --save makes it this machine's default
  /cwd [path]           show, or change, the project directory tools act on
  /steps <n>            max tool iterations per turn (1–500)
  /yolo                 toggle auto-approve for EVERY tool (incl. run_bash)
  /trust                run shell commands without asking (keeps file edits on)
  /btw <text>           drop a side note; folded into your next message
  /recall [--use] <q>   search your OTHER chats; --use folds a match in
  /rag on|off|status    auto-recall from past chats (default: OFF — chats isolated)
  /history              open the chat-history browser (same as Ctrl-R)
  /save [file]          write the transcript to a file
  /stats                model + conversation + index counters
  /cat  /cats  /meow    the cat: toggle · restyle · wisdom
  /mouse [on|off]       wheel-scroll capture (hold Shift to select text)
  /quit                 exit
  //text                send a message that itself starts with "/"

keyboard
  Enter          send (pasted newlines are kept) · while busy, queue the message
  mouse wheel    scroll the conversation
  PgUp / PgDn    scroll a page      Shift-↑ / Shift-↓  scroll a line
  Home / End     top / newest (when the input box is empty)
  Shift-Tab/Tab  cycle think effort
  Ctrl-T         show/hide reasoning      Ctrl-L  redraw
  ← → ↑ ↓        move the cursor; ↑ ↓ on the first/last line walk input history
  Ctrl-A/E       line start/end           Ctrl-W  delete word
  Ctrl-U / K     delete to start / end    Ctrl-Y  put back what was deleted
  Ctrl-R         chat-history browser
  Ctrl-C         interrupt · clear input · quit

approval prompt
  y  allow once    n / Esc  deny    a then a  always allow this tool (this session)
  PgUp / PgDn    scroll a long command or edit      Ctrl-C  deny and stop the turn
"""

MEOWS = [
    "name things well and the bugs get lonely.",
    "the fastest code is the code you delete.",
    "read the error message. all of it.",
    "if it isn't tested, it's just a rumour.",
    "on this box it's python3, never python.",
    "commit small, commit often.",
    "the model sounds confident and is sometimes wrong. verify.",
    "a tool call you didn't read is a tool call you didn't make.",
    "green on black: the programmer's savannah.",
]

CATS = {
    "tux": (
        [(r" /\_/\ ", r"( -.- ) z"), (r" /\_/\ ", r"( -.- ) z"),
         (r" /\_/\ ", r"( u.u ) Z"), (r" /\_/\ ", r"( -.- )  ")],
        [(r" /\_/\ ", r"( o.o ) ~"), (r" /\_/\ ", r"( -.o )~ "),
         (r" /\_/\ ", r"( o.o ) ~"), (r" /\_/\ ", r"( o.- ) ~~")],
    ),
    "loaf": (
        [(r"  ____  ", r" (=^.^=)z "), (r"  ____  ", r" (=^.^=)  "),
         (r"  ____  ", r" (=-.-=)  "), (r"  ____  ", r" (=^.^=)  ")],
        [(r"  ____  ", r" (=^.^=)/ "), (r"  ____  ", r" (=^.^=)  "),
         (r"  ____  ", r" (=^-^=)~ "), (r"  ____  ", r" (=^o^=)  ")],
    ),
    "long": (
        [(r"=^._.^=", r"  ~~~ zzz"), (r"=^-_-^=", r"  ~~~ zzz")],
        [(r"=^._.^=", r"  ~~~>   "), (r"=^o_o^=", r"   ~~~>  "),
         (r"=^._.^=", r"    ~~~> "), (r"=^-_-^=", r"     ~~~>")],
    ),
    "curl": (
        [(r" (  )~  ", r" (=^.^=) "), (r" (  )~  ", r" (=^.^=) "),
         (r" (  )~  ", r" (=-.-=) "), (r" (  )~  ", r" (=^.^=) ")],
        [(r" (  )~  ", r" (>^.^)> "), (r" (  )~  ", r" (=^.^=) "),
         (r" (  )~  ", r" <(^.^<) "), (r" (  )~  ", r" (=^o^=) ")],
    ),
}
CAT_ORDER = list(CATS)


def _env_seconds(name: str, default: float) -> float:
    # a typo in the variable must not stop the app from starting
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


IDLE_ANIMATION = _env_seconds("JOJO_IDLE_ANIMATION", 20.0)  # quiet seconds before the header stops
APPROVAL_GRACE = 0.35      # seconds an approval prompt ignores keys after it appears
ARM_SECS = 3.0             # window for the second press of a two-key action
IS_WINDOWS = sys.platform == "win32"


class Block:
    """One transcript entry. Streamed text is appended as parts and joined on
    demand (a += on the attribute copied the whole block every token), and
    the wrapped rows are cached against `version`."""

    __slots__ = ("kind", "_parts", "_text", "ts", "version", "cache")

    def __init__(self, kind, text=""):
        self.kind = kind
        self._parts = [text] if text else []
        self._text = text
        self.ts = time.time()
        self.version = 0
        self.cache = None

    @property
    def text(self) -> str:
        if self._text is None:
            self._text = "".join(self._parts)
            self._parts = [self._text] if self._text else []
        return self._text

    @text.setter
    def text(self, value: str):
        self._parts = [value] if value else []
        self._text = value
        self.version += 1

    def add(self, s: str):
        self._parts.append(s)
        self._text = None
        self.version += 1


class TUI:
    def __init__(self, cfg: Config, agent_factory=None):
        self.cfg = cfg
        self.theme = Theme()
        self.blocks: list[Block] = []
        self.scroll = 0                   # rows the viewport bottom sits ABOVE the newest row
        self._max_scroll = 0              # set every frame by _draw
        self._body_h = 10                # visible transcript rows, set every frame by _draw
        self._input_w = 40               # input box width, set every frame by _draw
        self._last_row_count = 0         # to hold the view still while output streams in
        self.mouse_on = False
        self.input = ""
        self.cur = 0                      # cursor index within self.input
        self.events: queue.Queue = queue.Queue()
        self.approve_reply: queue.Queue = queue.Queue()
        self.pending_approval = None
        self._approval_at = 0.0
        self._approval_scroll = 0
        self._approval_rows = 0
        self.worker: threading.Thread | None = None
        self.busy = False
        self.spin = 0
        self.stats_line = ""
        self.agent = (agent_factory or (lambda ap: Agent(cfg, approver=ap)))(self._approver)
        self.store = None
        self.show_thinking = True
        self._streamed_turn = False
        self.show_cat = True
        self.cat_style = "tux"
        self.cat_frame = 0
        self._last_cat = 0.0
        self._phase = 0.0               # global animation clock
        self._boot = 0.0               # set when the curses loop starts
        self._splash_done = False
        self.history: list[str] = []
        self.hist_idx: int | None = None
        self._draft = ""                # what was typed before walking history
        self.last_user: str | None = None
        self._btw: list[str] = []
        self._queued: list[str] = []    # messages sent while busy
        self._kill = ""                 # Ctrl-U / W / K buffer, for Ctrl-Y
        self._armed: tuple[str, float] = ("", 0.0)   # a two-press action awaiting its second
        self._surrogate = ""            # half of a non-BMP character (Windows)
        self._in_paste = False          # between bracketed-paste markers
        self._toast = ""
        self._toast_until = 0.0
        self._redraw = False
        self._dirty = True
        self._last_activity = time.time()
        self.picker = None              # {"rows": [...], "sel": int} when history browser is open

    # -- agent side ------------------------------------------------- #
    def _approver(self, name, args):
        self.events.put(("__approval__", name, args))
        return self.approve_reply.get()

    def _run_turn(self, text):
        try:
            for ev in self.agent.run(text):
                self.events.put(ev)
        except Exception as e:  # noqa: BLE001
            self.events.put(("error", f"{type(e).__name__}: {e}"))
        finally:
            self.events.put(("__idle__",))

    def _bg(self, fn, *args):
        """Run slow work (network, embeddings) off the UI thread; it reports
        back through self.events. Startup, /model, /stats, /rag and /recall
        all used to freeze the screen — ten seconds of blank terminal when the
        Ollama host was down."""
        def run():
            try:
                fn(*args)
            except Exception as e:  # noqa: BLE001
                self.events.put(("error", f"{type(e).__name__}: {e}"))
        threading.Thread(target=run, daemon=True).start()

    # -- blocks -------------------------------------------------- #
    def _append(self, kind, text=""):
        self.blocks.append(Block(kind, text))

    def _extend_last(self, kind, text):
        if self.blocks and self.blocks[-1].kind == kind:
            self.blocks[-1].add(text)
        else:
            self._append(kind, text)

    def _toast_msg(self, msg, secs=2.2):
        self._toast = msg
        self._toast_until = time.time() + secs

    def _busy_refusal(self, what: str) -> bool:
        if self.busy:
            self._toast_msg(f"busy — Ctrl-C first, then {what}")
            return True
        return False

    def _arm(self, key: str) -> bool:
        """True on the second press of `key` within ARM_SECS."""
        k, t = self._armed
        if k == key and time.time() - t < ARM_SECS:
            self._armed = ("", 0.0)
            return True
        self._armed = (key, time.time())
        return False

    # -- model ------------------------------------------------ #
    def _cmd_model(self, arg: str):
        """`/model` — what is running; `/model <tag> [--save]` — switch, and
        optionally make it this machine's default.

        The --save half is the point: without somewhere to write the answer,
        every session started on whatever the built-in default was and the
        model you actually pulled had to be re-typed forever.
        """
        from .config import load_settings, settings_path

        words = arg.split()
        save = False
        for flag in ("--save", "-s"):
            if flag in words:
                words.remove(flag)
                save = True
        name = " ".join(words).strip()

        if not name:
            if save:                      # `/model --save` == keep what's running
                return self._save_model(self.cfg.model)
            saved = load_settings().get("model")
            head = [f"running   {self.cfg.model}",
                    f"default   {saved or '(none saved — using the built-in default)'}"]

            def ask():
                lines = list(head)
                try:
                    pulled = self.agent.client.list_models()
                    lines.append("pulled    " + (", ".join(pulled) if pulled else "nothing yet"))
                except Exception as e:  # noqa: BLE001
                    lines.append(f"pulled    (cannot ask the model server: {e})")
                lines.append(f"settings  {settings_path()}")
                self.events.put(("info", "\n".join(lines)))
            return self._bg(ask)

        if self._busy_refusal("switch models"):
            return None
        self.cfg.model = name
        self.agent.client.model = name
        if save:
            self._save_model(name)
        else:
            self._toast_msg(f"model → {name}")
        if not self.cfg.remote_endpoint:
            def check():
                if not self.agent.client.has_model(name):
                    self.events.put(("error", f"heads up: {name!r} is not available — "
                                              f"run: ollama pull {name}"))
            self._bg(check)
        return None

    def _save_model(self, name: str):
        from .config import save_settings
        try:
            path = save_settings(model=name)
        except OSError as e:
            return self._append("error", f"could not save the model: {e}")
        self._toast_msg(f"model → {name} (saved)")
        self._append("info", f"{name} is now this machine's default · {path}")

    def _model_missing(self, name: str, pulled: list):
        """The model this session wants is not on the machine.

        The old message said `ollama pull <it>` and stopped — which, when the
        wanted model was the 65 GB default and the machine had a 7B one sitting
        right there, was advice to download something that would not run. So
        say what is actually available, and how to make the good one stick.
        """
        from .config import load_settings

        lines = [f"model {name!r} is not pulled."]
        if pulled:
            lines.append("  on this machine: " + ", ".join(pulled))
            lines.append(f"  use one now:     /model {pulled[0]}")
            lines.append(f"  or for good:     /model {pulled[0]} --save")
        else:
            lines.append(f"  get it:          ollama pull {name}")
            lines.append("  or let the installer pick one that fits this machine:")
            lines.append("                   python3 recommend-model.py --pull")
        if not load_settings().get("model") and pulled:
            lines.append("  (nothing is saved as this machine's default yet — that is why"
                         " it opened on the built-in one.)")
        self._append("error", "\n".join(lines))

    def _startup(self):
        """Who are we talking to — on a thread, so the splash shows at once."""
        try:
            v = self.agent.client.version()
        except Exception as e:  # noqa: BLE001
            self.events.put(("error", str(e)))
            return
        if self.cfg.remote_endpoint:
            self.events.put(("info", f"connected · {v} · tools run in {self.cfg.cwd}"))
            return
        api = getattr(self.agent.client, "api", "ollama")
        label = f"Ollama {v}" if api == "ollama" else v
        self.events.put(("info", f"connected · {label} · {self.cfg.cwd}"))
        if not self.agent.client.has_model(self.cfg.model):
            try:
                pulled = self.agent.client.list_models()
            except Exception:  # noqa: BLE001
                pulled = []
            self.events.put(("__model_missing__", self.cfg.model, pulled))
        if self.store is not None and self.cfg.rag and not self.store.embed_available():
            self.events.put(("info", "recall: embed model not pulled yet — "
                                     "run: ollama pull nomic-embed-text"))

    # -- transcript scrolling -------------------------------- #
    def _enable_mouse(self, on: bool):
        """Turn wheel capture on/off. With it on the terminal reports the wheel
        instead of translating it to arrow keys, so we can scroll the chat."""
        try:
            if on:
                avail, _ = curses.mousemask(curses.ALL_MOUSE_EVENTS)
                self.mouse_on = avail != 0
                if self.mouse_on:
                    curses.mouseinterval(0)
            else:
                curses.mousemask(0)
                self.mouse_on = False
        except curses.error:
            self.mouse_on = False

    def _scroll_by(self, delta: int):
        """delta > 0 scrolls UP toward older messages; < 0 toward the newest."""
        self.scroll = max(0, min(self.scroll + delta, self._max_scroll))

    def _scroll_page(self, direction: int):
        self._scroll_by(direction * max(1, self._body_h - 2))

    # -- event pump ------------------------------------------- #
    def _drain(self) -> bool:
        """Apply queued events. True if anything changed on screen."""
        changed = False
        while True:
            try:
                ev = self.events.get_nowait()
            except queue.Empty:
                return changed
            changed = True
            self._last_activity = time.time()
            k = ev[0]
            if k == "thinking":
                # always kept: Ctrl-T hides it on screen, it does not throw it away
                self._extend_last("thinking", _clean_delta(ev[1]))
            elif k == "assistant":
                self._streamed_turn = True
                self._extend_last("assistant", _clean_delta(ev[1]))
            elif k == "recall":
                self._append("info", f"recalled {ev[1]} snippet(s) from earlier chats")
                self._toast_msg(f"recalled {ev[1]} snippet(s)")
            elif k == "info":
                self._append("info", _clean(ev[1]))
            elif k == "tool_start":
                self._append("tool", _clean(f"{ev[1]}  {_fmt_args(ev[2])}"))
            elif k == "tool_end":
                out = _clean(ev[2])
                self._append("tool_out", out if len(out) < 4000 else out[:4000] + "\n…[clipped]")
            elif k == "tool_denied":
                self._append("error", f"denied: {ev[1]}")
            elif k == "final":
                # The agent says whether this text was streamed. Guessing from
                # "anything streamed this turn" lost every `finish` summary that
                # followed a step with prose in it.
                shown = ev[2] if len(ev) > 2 else self._streamed_turn
                if ev[1] and not shown:
                    self._append("assistant", _clean(ev[1]))
            elif k == "error":
                self._append("error", _clean(ev[1]))
            elif k == "stats":
                s = ev[1]
                ec, ed = s.get("eval_count"), s.get("eval_duration")
                pc = s.get("prompt_eval_count")
                if ec and ed:
                    self.stats_line = f"{pc or '?'} in · {ec} out · {ec / (ed / 1e9):.0f} tok/s"
            elif k == "__model_missing__":
                self._model_missing(ev[1], ev[2])
            elif k == "__approval__":
                self.pending_approval = (ev[1], ev[2] if isinstance(ev[2], dict) else {})
                self.picker = None              # an approval is never hidden behind the browser
                self._approval_at = time.time()
                self._approval_scroll = 0
                self._armed = ("", 0.0)
                try:
                    curses.flushinp()           # type-ahead must not answer it
                except curses.error:
                    pass
            elif k == "__idle__":
                self.busy = False
                if self._queued:
                    self._submit(self._queued.pop(0))

    # -- cat --------------------------------------------------- #
    def _cat_lines(self):
        idle_f, busy_f = CATS[self.cat_style]
        frames = busy_f if self.busy else idle_f
        return frames[self.cat_frame % len(frames)]

    # -- little animations ------------------------------------ #
    def _meter(self, n=4):
        """A tiny bouncing equaliser rendered from BARS."""
        now = time.time()
        top = len(BARS) - 1
        return "".join(
            BARS[int((0.5 + 0.5 * math.sin(now * 7 + k * 1.7)) * top)]
            for k in range(n)
        )

    def _draw_splash(self, stdscr, h, w):
        A = self.theme.A
        art = LOGO if w >= len(LOGO[0]) + 4 else LOGO_SMALL
        aw = max(len(s) for s in art)
        x0 = max(0, (w - aw) // 2)
        y0 = max(1, h // 2 - len(art) // 2 - 2)

        self.theme.wave_rule(stdscr, y0 - 2, x0, aw, self._phase, "─")
        self.theme.block_art(stdscr, y0, x0, art, self._phase)
        self.theme.wave_rule(stdscr, y0 + len(art) + 1, x0, aw, -self._phase, "─")

        tag = "offline · agentic · curses"
        self._safe(stdscr, y0 + len(art) + 3, max(0, (w - len(tag)) // 2),
                   tag, A["hint"], w)

        # a cat trots in from the left while the logo shimmers
        idle_f, busy_f = CATS["long"]
        fr = busy_f[(self.spin // 2) % len(busy_f)]
        span = max(1, w - 12)
        prog = min(1.0, max(0.0, (time.time() - self._boot) / SPLASH_SECS))
        cx = int(prog * span)
        self._safe(stdscr, y0 + len(art) + 5, cx, fr[0], A["assist"], w)
        self._safe(stdscr, y0 + len(art) + 6, cx, fr[1], A["tool"], w)

        hint = "press any key"
        self._safe(stdscr, h - 2, max(0, (w - len(hint)) // 2), hint, A["hint"], w)
        if self._redraw:
            stdscr.clearok(True)
            self._redraw = False
        stdscr.refresh()

    # -- transcript rows: (align, [(text, attr), ...]) -------- #
    def _block_rows(self, b: Block, width: int, live: bool):
        """Rows for one block, from its cache unless it changed."""
        dots = (self.spin // 3) % 3 if (live and b.kind == "thinking") else -1
        key = (b.version, width, self.show_thinking, live, dots)
        if b.cache is not None and b.cache[0] == key:
            return b.cache[1]
        rows = self._render_block(b, width, live, dots)
        b.cache = (key, rows)
        return rows

    def _render_block(self, b: Block, width: int, live: bool, dots: int):
        A = self.theme.A
        rows: list[tuple[str, list]] = []
        bw = max(20, min(int(width * 0.72), 76))       # bubble wrap width (right side)

        def L(segs):
            rows.append(("l", segs))

        def R(segs):
            rows.append(("r", segs))

        hhmm = _dt.datetime.fromtimestamp(b.ts).strftime("%H:%M")
        if b.kind == "user":
            R([(f"you · {hhmm} ▐", A["user"])])
            for para in b.text.split("\n"):
                for ln in _wrap(para, bw):
                    R([(ln, A["user"])])
            rows.append(("l", [("", 0)]))
        elif b.kind == "assistant":
            L([(f"▌ jojo · {hhmm}", A["assist_label"])])
            for chunk in split_fences(b.text):
                if chunk[0] == "text":
                    for para in chunk[1].split("\n"):
                        if not para.strip():
                            L([("", 0)])
                            continue
                        for ln in _wrap(para, width - 3):
                            L([("  ", 0)] + inline_segments(ln, A))
                else:
                    _, lang, code = chunk
                    lg = norm_lang(lang)
                    label = lang.strip() or ("code" if lg == "generic" else lg)
                    L([("  ╭─ ", A["rule"]), (label + " ", A["hint"]),
                       ("─" * max(0, width - 8 - _slen(label)), A["rule"])])
                    cw = max(8, width - 6)
                    for cl in code:
                        # long lines wrap with a continuation gutter instead of
                        # vanishing silently off the right edge
                        pieces = _wrap(cl, cw) if _slen(cl) > cw else [cl]
                        for i, piece in enumerate(pieces):
                            gutter = "  │ " if i == 0 else "  ┊ "
                            L([(gutter, A["rule"])] + highlight_line(piece, lg, A))
                    L([("  ╰" + "─" * max(0, width - 6), A["rule"])])
            rows.append(("l", [("", 0)]))
        elif b.kind == "thinking":
            if not self.show_thinking:
                L([(f"   ╭╴ reasoning hidden ({len(b.text)} chars) · Ctrl-T to show",
                    A["think_hdr"])])
                rows.append(("l", [("", 0)]))
                return rows
            hdr = f"   ╭╴ thinking{'.' * (1 + dots) if dots >= 0 else ''} "
            L([(hdr, A["think_hdr"]),
               ("┄" * max(0, width - _slen(hdr) - 1), A["rule"])])
            body = b.text.strip("\n") or "…"
            for para in body.split("\n"):
                for ln in _wrap(para, max(10, width - 6)):
                    L([("   ┆ ", A["think_gut"]), (ln, A["think"])])
            if not live:
                L([("   ╰" + "┄" * 5, A["rule"])])
            rows.append(("l", [("", 0)]))
        elif b.kind == "tool":
            first = True
            for para in b.text.split("\n"):
                for ln in _wrap(para, max(10, width - 6)):
                    L([("  ▸ " if first else "    ", A["tool"]), (ln, A["tool"])])
                    first = False
        elif b.kind == "tool_out":
            for para in b.text.split("\n"):
                for ln in _wrap(para, max(10, width - 6)):
                    L([("   ╎ ", A["rule"]), (ln, A["out"])])
            rows.append(("l", [("", 0)]))
        else:
            pref = {"info": "  ", "btw": "  ~ btw · ", "error": "  ✗ "}.get(b.kind, "  ")
            attr = {"info": A["info"], "btw": A["hint"],
                    "error": A["error"]}.get(b.kind, A["assist"])
            first = True
            for para in b.text.split("\n"):
                for ln in _wrap(para, max(10, width - len(pref))):
                    L([((pref if first else " " * len(pref)) + ln, attr)])
                    first = False
            rows.append(("l", [("", 0)]))
        return rows

    def _rows(self, width):
        A = self.theme.A
        rows: list[tuple[str, list]] = []
        last = self.blocks[-1] if self.blocks else None
        for b in self.blocks:
            rows.extend(self._block_rows(b, width, live=(b is last and self.busy)))

        # a soft blinking caret on whatever Jojo is streaming right now
        if self.busy and last is not None and last.kind in ("assistant", "thinking") \
                and (self.spin // 4) % 2 == 0:
            for j in range(len(rows) - 1, -1, -1):
                align, segs = rows[j]
                if segs and any(t.strip() for t, _ in segs):
                    rows[j] = (align, segs + [("▋", A["caret"])])
                    break
        return rows

    def _put_segs(self, win, y, x0, segs, w):
        x = x0
        for text, attr in segs:
            if x >= w - 1:
                break
            text = _clip_cols(text, w - 1 - x)
            try:
                win.addstr(y, x, text, attr)
            except curses.error:
                pass
            x += _slen(text)

    def _wrap_input(self, text, width):
        """List of (start, end) index pairs, one per visual input line, by columns."""
        width = max(4, width)
        segs = []
        off = 0
        for part in text.split("\n"):
            if part == "":
                segs.append((off, off))
            else:
                s = 0
                while s < len(part):
                    used, e = 0, s
                    while e < len(part) and used + _slen(part[e]) <= width:
                        used += _slen(part[e])
                        e += 1
                    if e == s:
                        e = s + 1                # a character wider than the box
                    if e < len(part):
                        sp = part.rfind(" ", s, e)
                        if sp > s:
                            e = sp + 1
                    segs.append((off + s, off + e))
                    s = e
            off += len(part) + 1
        return segs or [(0, 0)]

    def _input_line(self, segs):
        """Index of the visual line the cursor is on."""
        ci = 0
        for i, (s, e) in enumerate(segs):
            if s <= self.cur <= e:
                ci = i
        return ci

    def _input_first(self, segs, rows):
        ci = self._input_line(segs)
        return 0 if ci < rows else ci - rows + 1

    def _approval_lines(self, width):
        """The whole request being approved, wrapped — nothing cut off."""
        name, args = self.pending_approval
        out = [f"{name}"]
        w = max(10, width - 6)
        for k, v in args.items():
            if isinstance(v, str):
                body = v
            else:
                try:
                    import json as _json
                    body = _json.dumps(v, ensure_ascii=False, indent=1)
                except (TypeError, ValueError):
                    body = repr(v)
            body = _visible(body)
            if k in ("path", "url", "cwd"):
                body = body.replace("\n", "␊")      # a path has no line breaks to show
            lines = body.split("\n")
            if k == "content" and len(lines) > 60:
                extra = len(lines) - 60
                lines = lines[:60] + [f"… {extra} more lines ({len(body)} chars in all)"]
            out.append(f"{k}:")
            for ln in lines:
                out.extend("  " + piece for piece in _wrap(ln, w - 2))
        return out

    def _draw(self, stdscr):
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        A = self.theme.A

        if h < 4 or w < 12:                       # too small to lay anything out
            self._safe(stdscr, 0, 0, "terminal too small", A.get("hint", 0), w)
            stdscr.refresh()
            return

        if not self._splash_done:
            if time.time() - self._boot > SPLASH_SECS or h < 18 or w < 40:
                self._splash_done = True
                try:
                    curses.curs_set(1)
                except curses.error:
                    pass
            else:
                self._draw_splash(stdscr, h, w)
                return

        top = 4
        iw = max(8, w - 4)
        self._input_w = iw
        in_segs = self._wrap_input(self.input, iw)
        in_rows = min(max(1, len(in_segs)), 6)
        bot = 2 + in_rows + 1                      # rule + status + input + hint
        body_h = max(1, h - top - bot)

        # header
        self.theme.wave_rule(stdscr, 0, 0, w - 1, self._phase * 0.5, "━")
        self.theme.shimmer_text(stdscr, 1, 1, "❯❯ JojoAI", self._phase)
        loc = f"remote · {self.cfg.remote_endpoint}" if self.cfg.remote_endpoint else "offline"
        sub = f"{loc} · {self.cfg.model} · think={_tname(self.cfg.think)}"
        if "*" in self.cfg.auto_approve:
            sub += " · YOLO"
        elif self.cfg.auto_bash:
            sub += " · edits+runs cmds"
        elif self.cfg.auto_edit:
            sub += " · edits files"
        else:
            sub += " · asks first"
        if self.store is not None and getattr(self.store, "rag_enabled", False):
            sub += " · recall ON"
        else:
            sub += " · isolated chat"
        if not self.show_thinking:
            sub += " · reasoning hidden"
        if self._queued:
            sub += f" · {len(self._queued)} queued"
        self._safe(stdscr, 2, 1, sub, A["hint"], w)
        if self.show_cat:
            c1, c2 = self._cat_lines()
            cw = max(len(c1), len(c2)) + 1
            self._safe(stdscr, 1, max(12, w - 1 - cw), c1, A["assist"], w)
            self._safe(stdscr, 2, max(12, w - 1 - cw), c2, A["tool"], w)
        self.theme.wave_rule(stdscr, 3, 0, w - 1, -self._phase * 0.5, "━")

        # history browser takes over the body
        if self.picker is not None:
            self._draw_picker(stdscr, top, w, h)
            if self._redraw:
                stdscr.clearok(True)
                self._redraw = False
            stdscr.refresh()
            return

        # body
        rows = self._rows(w - 1)
        max_scroll = max(0, len(rows) - body_h)
        self._body_h = body_h
        self._max_scroll = max_scroll
        # while scrolled up, keep the SAME lines in view as new output streams in
        # (don't let the conversation slide out from under the reader)
        if self.scroll > 0 and len(rows) > self._last_row_count:
            self.scroll = min(self.scroll + (len(rows) - self._last_row_count), max_scroll)
        self._last_row_count = len(rows)
        self.scroll = min(self.scroll, max_scroll)
        start = max(0, len(rows) - body_h - self.scroll)
        for i, (align, segs) in enumerate(rows[start:start + body_h]):
            if align == "r":
                rowlen = sum(_slen(t) for t, _ in segs)
                x0 = max(0, (w - 1) - rowlen)
            else:
                x0 = 0
            self._put_segs(stdscr, top + i, x0, segs, w)

        # an approval takes over the bottom of the body: the full request
        if self.pending_approval:
            lines = self._approval_lines(w - 1)
            panel_h = max(3, min(len(lines) + 2, body_h - 1, max(6, body_h * 2 // 3)))
            inner = panel_h - 2
            self._approval_rows = max(0, len(lines) - inner)
            self._approval_scroll = min(self._approval_scroll, self._approval_rows)
            y0 = top + body_h - panel_h
            for r in range(panel_h):
                self._safe(stdscr, y0 + r, 0, " " * (w - 1), 0, w)
            title = f"╭─ approve? {lines[0]} "
            self._safe(stdscr, y0, 0, title + "─" * max(0, w - 2 - _slen(title)), A["error"], w)
            for r, ln in enumerate(lines[1 + self._approval_scroll:1 + self._approval_scroll + inner]):
                self._safe(stdscr, y0 + 1 + r, 0, "│ " + ln, A["assist"], w)
            more = len(lines) - 1 - self._approval_scroll - inner
            tail = f"╰─ {more} more lines · PgDn " if more > 0 else "╰─"
            self._safe(stdscr, y0 + panel_h - 1, 0, tail + "─" * max(0, w - 2 - _slen(tail)),
                       A["error"], w)

        # footer
        y = top + body_h
        if self.busy:
            self.theme.wave_rule(stdscr, y, 0, w - 1, self._phase, "━")
        else:
            self.theme.rule(stdscr, y, 0, w - 1, "━")
        if self.busy:
            cat = SPINNER[self.spin % len(SPINNER)]
            seg = f" {cat} working {self._meter()}  ·  {self.cfg.model}  ·  think {_tname(self.cfg.think)} "
        else:
            cat = "●"
            seg = f" {cat} ready  ·  {self.cfg.model}  ·  think {_tname(self.cfg.think)} "
        if self.stats_line:
            seg += f" ·  {self.stats_line} "
        if self.scroll:
            seg += f" ·  ↑{self.scroll} scrolled — End for latest "
        if time.time() < self._toast_until:
            seg = f" {cat} {self._toast} "
        self._safe(stdscr, y + 1, 0, seg + " " * max(0, w - 1 - _slen(seg)), A["status"], w)

        if self.pending_approval:
            name = self.pending_approval[0]
            k, t = self._armed
            if k == "always" and time.time() - t < ARM_SECS:
                legend = f" press a again to ALWAYS allow {name} this session · any other key cancels "
            else:
                legend = " [y] allow   [n] deny   [a] always…   PgUp/PgDn scroll   Ctrl-C stop "
            self._safe(stdscr, y + 2, 0, legend, A["error"], w)
            for r in range(1, in_rows + 1):
                self._safe(stdscr, y + 2 + r, 0, "", A["hint"], w)
        else:
            # multi-line, word-wrapped input box with a visible cursor
            self.cur = max(0, min(self.cur, len(self.input)))
            total = len(in_segs)
            first = max(0, min(total - in_rows, self._input_first(in_segs, in_rows)))
            cur_row = cur_col = 0
            for vr in range(in_rows):
                si = first + vr
                if si >= total:
                    break
                s, e = in_segs[si]
                pre = "❯ " if si == 0 else "  "
                self._safe(stdscr, y + 2 + vr, 0, pre + self.input[s:e], A["prompt"], w)
                if s <= self.cur <= e:
                    cur_row, cur_col = vr, _slen(pre) + _slen(self.input[s:self.cur])
            self._safe(stdscr, y + 2 + in_rows, 0,
                       "  Enter send · wheel / PgUp / PgDn scroll · Shift-Tab think · Ctrl-R history · /help",
                       A["hint"], w)
            try:
                stdscr.move(y + 2 + cur_row, min(w - 1, cur_col))
            except curses.error:
                pass

        if self._redraw:
            stdscr.clearok(True)
            self._redraw = False
        stdscr.refresh()

    def _safe(self, win, y, x, text, attr, w):
        try:
            win.addstr(y, x, _clip_cols(text, max(0, w - 1 - x)), attr)
        except curses.error:
            pass

    # -- options ------------------------------------------- #
    def _cycle_think(self, step=1):
        cur = _tname(self.cfg.think)
        i = (THINK_LEVELS.index(cur) + step) % len(THINK_LEVELS)
        lvl = THINK_LEVELS[i]
        self.cfg.think = False if lvl == "off" else lvl
        self._toast_msg(f"think → {lvl}")

    def _toggle_yolo(self):
        if "*" in self.cfg.auto_approve:
            self.cfg.auto_approve.discard("*")
            self._toast_msg("YOLO off — tools will ask")
        else:
            self.cfg.auto_approve.add("*")
            self._toast_msg("YOLO on — tools run unattended")

    # -- commands ----------------------------------------- #
    COMMANDS = {"/quit", "/exit", "/help", "/reset", "/retry", "/tools", "/think", "/model",
                "/cwd", "/steps", "/yolo", "/trust", "/btw", "/rag", "/recall", "/history",
                "/save", "/stats", "/cat", "/cats", "/meow", "/mouse"}

    def _command(self, line: str) -> bool:
        parts = line.split(None, 1)
        cmd = parts[0].lower()
        raw = parts[1].strip() if len(parts) > 1 else ""      # spaces inside paths survive
        arg = raw
        if cmd in ("/quit", "/exit"):
            return True
        elif cmd == "/help":
            self._append("info", HELP)
        elif cmd == "/reset":
            if self._busy_refusal("/reset"):
                return False
            self.agent.reset()                      # wipes conversation messages
            self.blocks.clear()
            self.stats_line = ""
            self._btw.clear()
            self._clear_input()
            self.scroll = 0
            if self.store is not None:
                old = self.store
                try:
                    from .store import Store
                    # keep the user's /rag choice: a reset used to turn recall
                    # back on after "/rag off"
                    self.store = Store(self.cfg.host, self.cfg.embed_model, old.rag_enabled,
                                       api=getattr(self.agent.client, "api", "ollama"))
                    self.agent.store = self.store
                except Exception:  # noqa: BLE001
                    pass
            self._append("info", "fresh chat — nothing carried over")
        elif cmd == "/retry":
            if self._busy_refusal("/retry"):
                return False
            if self.last_user:
                self._submit(self.last_user)
            else:
                self._append("error", "nothing to retry")
        elif cmd == "/tools":
            if self.cfg.remote_endpoint:
                self._append("info", "remote mode: the server decides the tools. They run here:\n"
                                     "list_dir  read_file  write_file  run_command  web_search  fetch_url")
            else:
                from .tools import TOOLS
                self._append("info", "\n".join(
                    f"{t['function']['name']:11} {t['function']['description']}" for t in TOOLS))
        elif cmd == "/think":
            v = (arg.strip() or "medium").lower()
            if v not in THINK_LEVELS:
                self._append("error", f"think must be one of {THINK_LEVELS}")
            else:
                self.cfg.think = False if v == "off" else v
                self._toast_msg(f"think → {v}")
        elif cmd == "/model":
            self._cmd_model(arg)
        elif cmd == "/cwd":
            self._cwd_cmd(raw)
        elif cmd == "/steps":
            if arg.isdigit() and 1 <= int(arg) <= 500:
                self.cfg.max_steps = int(arg)
                self._toast_msg(f"max steps → {arg}")
            else:
                self._append("error", f"usage: /steps <1–500>   (now {self.cfg.max_steps})")
        elif cmd == "/yolo":
            self._toggle_yolo()
        elif cmd == "/trust":
            self.cfg.auto_bash = not self.cfg.auto_bash
            self.cfg.auto_edit = True
            self._toast_msg("shell commands run without asking"
                            if self.cfg.auto_bash else "shell commands will ask again")
        elif cmd == "/btw":
            if arg:
                self._btw.append(arg)
                self._append("btw", arg)
            else:
                self._append("error", "usage: /btw <side note>")
        elif cmd == "/rag":
            self._rag_cmd(arg.strip().lower())
        elif cmd == "/recall":
            self._recall_cmd(arg)
        elif cmd == "/history":
            self._history_cmd()
        elif cmd == "/save":
            self._save_transcript(_unquote(raw) or None)
        elif cmd == "/stats":
            self._stats_cmd()
        elif cmd == "/cat":
            self.show_cat = not self.show_cat
            self._toast_msg("cat " + ("on" if self.show_cat else "off"))
        elif cmd == "/cats":
            i = (CAT_ORDER.index(self.cat_style) + 1) % len(CAT_ORDER)
            self.cat_style = CAT_ORDER[i]
            self._toast_msg(f"cat style → {self.cat_style}")
        elif cmd == "/meow":
            self._append("info", f"  =^..^=  {random.choice(MEOWS)}")
        elif cmd == "/mouse":
            want = arg.strip().lower()
            on = (want != "off") if want in ("", "on", "off") else not self.mouse_on
            self._enable_mouse(on)
            self._toast_msg("mouse wheel scroll " + ("on" if self.mouse_on else "off — hold Shift to select text")
                            if on else "mouse off — terminal handles selection/scroll")
        else:
            self._append("error", f"unknown command: {cmd}  (/help · start with // to send "
                                  "a message that begins with /)")
        self.scroll = 0                   # a command's output should be visible
        return False

    def _cwd_cmd(self, raw: str):
        if not raw:
            here = getattr(self.agent.ctx, "cwd", self.cfg.cwd)
            self._append("info", f"project  {self.cfg.cwd}\ntools in {here}")
            return
        if self._busy_refusal("/cwd"):
            return
        p = os.path.expanduser(_unquote(raw))
        if not os.path.isabs(p):
            p = os.path.join(getattr(self.agent.ctx, "cwd", self.cfg.cwd), p)
        p = os.path.abspath(p)
        if not os.path.isdir(p):
            self._append("error", f"not a directory: {p}")
            return
        if hasattr(self.agent, "set_cwd"):
            self.agent.set_cwd(p)
        else:
            self.cfg.cwd = p
            self.agent.ctx.cwd = p
        self._toast_msg(f"cwd → {p}")

    def _rag_cmd(self, arg):
        if self.store is None:
            self._append("error", "history store unavailable")
            return
        if arg == "on":
            self.store.rag_enabled = self.cfg.rag = True
            self._toast_msg("recall on")
        elif arg == "off":
            self.store.rag_enabled = self.cfg.rag = False
            self._toast_msg("recall off")
        else:
            store = self.store

            def status():
                s = store.stats()
                embed = "ready" if s["embed_available"] else \
                    "NOT pulled — run: ollama pull nomic-embed-text"
                state = "on" if store.rag_enabled else "off"
                self.events.put(("info",
                                 f"recall {state} · embed {s['embed_model']} ({embed}) · "
                                 f"{s['chunks']} chunks from {s['sessions_indexed']} past sessions · "
                                 f"{s['data_dir']}"))
            self._bg(status)

    def _recall_cmd(self, arg):
        if self.store is None:
            self._append("error", "history store unavailable")
            return
        words = arg.split()
        use = bool(words) and words[0] == "--use"
        q = " ".join(words[1:] if use else words).strip()
        if not q:
            self._append("error", "usage: /recall [--use] <query>")
            return
        store = self.store

        def search():
            hits = store.retrieve(q, k=5, min_score=0.25)
            if not hits:
                self.events.put(("info", "no matching context found in your other chats"))
                return
            out = []
            for score, sess, ts, title, text in hits:
                when = _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
                out.append(f"[{when} · {title or sess} · {score:.2f}]\n{text}")
            self.events.put(("info", "recalled (reference only):\n\n" + "\n\n".join(out)))
            if use:
                self._btw.append("From an earlier chat: " + " ".join(h[4][:300] for h in hits))
                self.events.put(("info", "folded into your next message"))
        self._toast_msg("searching your other chats…")
        self._bg(search)

    def _history_cmd(self):
        # /history opens the same interactive browser as Ctrl-R
        self._open_picker()

    # -- history browser (Ctrl-R) ----------------------- #
    def _open_picker(self):
        if self.pending_approval:
            return
        if self.store is None:
            self._toast_msg("history unavailable")
            return
        rows = self.store.recent_sessions(50)
        if not rows:
            self._toast_msg("no past chats yet")
            return
        self.picker = {"rows": rows, "sel": 0}

    def _draw_picker(self, stdscr, top, w, h):
        A = self.theme.A
        rows = self.picker["rows"]
        sel = self.picker["sel"]
        k, t = self._armed
        if k.startswith("del:") and time.time() - t < ARM_SECS:
            head = f"press d again to DELETE {k[4:]} · any other key cancels"
        else:
            head = "chat history   ↑↓ move · Enter view · Ctrl-O resume · d delete · Esc close"
        self._safe(stdscr, top, 1, head, A["hint"], w)
        view_h = max(1, h - top - 3)
        first = max(0, min(sel - view_h // 2, max(0, len(rows) - view_h)))
        for i in range(view_h):
            idx = first + i
            if idx >= len(rows):
                break
            sid, n, first_user = rows[idx]
            when = f"{sid[4:6]}-{sid[6:8]} {sid[9:11]}:{sid[11:13]}"
            marker = "❯ " if idx == sel else "  "
            line = f"{marker}{when}   {n:3d} msg   {first_user or '—'}"
            attr = A["status"] if idx == sel else A["assist"]
            self._safe(stdscr, top + 2 + i, 0, line + " " * max(0, w - 1 - _slen(line)), attr, w)
        self._safe(stdscr, h - 1, 0,
                   f"  {len(rows)} past chats · this one stays isolated unless you resume ",
                   A["hint"], w)

    def _view_session(self, sid):
        msgs = self.store.load_session(sid)
        self.picker = None
        self._append("info", f"─── history {sid} · {len(msgs)} msgs · read-only, "
                             f"NOT added to this chat's context ───")
        for m in msgs:
            self._append("user" if m["role"] == "user" else "assistant", m["content"])
        self._append("info", "─── end of history · /reset for a clean slate ───")
        self.scroll = 0
        self._toast_msg(f"viewing {sid}")

    def _delete_session(self, sid):
        n = self.store.delete_session(sid)
        self.picker["rows"] = [r for r in self.picker["rows"] if r[0] != sid]
        if not self.picker["rows"]:
            self.picker = None
        else:
            self.picker["sel"] = min(self.picker["sel"], len(self.picker["rows"]) - 1)
        self._toast_msg(f"deleted {sid} ({n} indexed chunks removed)")

    def _resume_session(self, sid):
        if self._busy_refusal("resume"):
            return
        msgs = self.store.load_session(sid)
        self.picker = None
        self.agent.reset()
        self.blocks.clear()
        self._clear_input()
        for m in msgs:
            self.agent.messages.append({"role": m["role"], "content": m["content"]})
            self._append("user" if m["role"] == "user" else "assistant", m["content"])
        self.last_user = next((m["content"] for m in reversed(msgs)
                               if m["role"] == "user"), None)
        self.scroll = 0
        self._append("info", f"─── resumed {sid} — keep going where it left off ───")
        self._toast_msg(f"resumed {sid}")

    def _stats_cmd(self):
        msgs = len(self.agent.messages)
        head = (f"model {self.cfg.model} · {msgs} messages in context · "
                f"ctx {self.cfg.num_ctx}")
        if self.store is None:
            self._append("info", head)
            return
        store = self.store

        def stats():
            s = store.stats()
            self.events.put(("info", head + f" · index {s['chunks']} chunks / "
                                            f"{s['sessions_indexed']} sessions · {s['data_dir']}"))
        self._bg(stats)

    def _save_transcript(self, path):
        if not path:
            ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            path = os.path.expanduser(f"~/jojoai-transcript-{ts}.txt")
        try:
            # UTF-8: with the Windows locale codec the first emoji in the chat
            # raised UnicodeEncodeError, which nothing caught — the TUI died.
            with open(os.path.expanduser(path), "w", encoding="utf-8") as f:
                for b in self.blocks:
                    f.write(f"[{b.kind}] {b.text}\n\n")
            self._toast_msg(f"saved → {path}")
        except (OSError, ValueError) as e:
            self._append("error", f"save failed: {e}")

    # -- input editing --------------------------------- #
    def _ins(self, s):
        self.input = self.input[:self.cur] + s + self.input[self.cur:]
        self.cur += len(s)
        self.hist_idx = None

    def _clear_input(self):
        self.input = ""
        self.cur = 0
        self.hist_idx = None

    def _history_step(self, step: int):
        """Walk input history; the unsent draft comes back at the end of it."""
        if not self.history:
            return
        if self.hist_idx is None:
            if step > 0:
                return
            self._draft = self.input
            self.hist_idx = len(self.history) - 1
        else:
            self.hist_idx += step
            if self.hist_idx < 0:
                self.hist_idx = 0
            elif self.hist_idx >= len(self.history):
                self.hist_idx = None
                self.input, self.cur = self._draft, len(self._draft)
                return
        self.input = self.history[self.hist_idx]
        self.cur = len(self.input)

    def _vertical(self, step: int) -> bool:
        """Move the cursor a visual line up/down inside a multi-line input.
        False when already on the first/last line (then arrows walk history)."""
        segs = self._wrap_input(self.input, self._input_w)
        ci = self._input_line(segs)
        ni = ci + step
        if not (0 <= ni < len(segs)):
            return False
        col = _slen(self.input[segs[ci][0]:self.cur])
        s, e = segs[ni]
        pos, used = s, 0
        while pos < e and used + _slen(self.input[pos]) <= col and self.input[pos] != "\n":
            used += _slen(self.input[pos])
            pos += 1
        self.cur = pos
        return True

    # -- submit ------------------------------------------ #
    def _submit(self, text):
        self.history.append(text)
        self.hist_idx = None
        self.last_user = text
        shown = text
        if self._btw:
            notes = "\n".join(f"- {n}" for n in self._btw)
            text = f"(side notes to keep in mind:\n{notes}\n)\n\n{text}"
            self._btw.clear()
        self._append("user", shown)
        self.busy = True
        self._streamed_turn = False
        self.stats_line = ""
        self.scroll = 0
        self.worker = threading.Thread(target=self._run_turn, args=(text,), daemon=True)
        self.worker.start()

    # -- keys ------------------------------------------- #
    def _read_keys(self, stdscr, timeout_ms: int) -> list:
        """Everything the terminal has buffered: wait up to timeout_ms for the
        first key, then take the rest without waiting."""
        stdscr.timeout(timeout_ms)
        try:
            keys = [stdscr.get_wch()]
        except curses.error:
            return []
        except KeyboardInterrupt:                   # a SIGINT raw mode did not stop
            return ["\x03"]
        stdscr.timeout(0)
        while len(keys) < 200_000:
            try:
                keys.append(stdscr.get_wch())
            except curses.error:
                break
            except KeyboardInterrupt:
                keys.append("\x03")
                break
        return keys

    def _tokens(self, keys: list) -> list:
        """Keys → tokens: surrogate pairs joined, escape sequences curses did
        not know decoded or dropped (they used to be typed into the input)."""
        out: list = []
        i = 0
        while i < len(keys):
            ch = keys[i]
            i += 1
            if isinstance(ch, str) and "\ud800" <= ch <= "\udbff":
                self._surrogate = ch                # PDCurses: half of an emoji
                continue
            if isinstance(ch, str) and "\udc00" <= ch <= "\udfff":
                if self._surrogate:
                    ch = (self._surrogate + ch).encode("utf-16", "surrogatepass").decode("utf-16")
                    self._surrogate = ""
                else:
                    continue
            if ch == "\x1b" and i < len(keys) and keys[i] in ("[", "O"):
                seq = keys[i]
                j = i + 1
                while j < len(keys) and isinstance(keys[j], str) and len(seq) < 16:
                    seq += keys[j]
                    j += 1
                    if "@" <= seq[-1] <= "~" and len(seq) > 1:
                        break
                i = j
                tok = _ESCAPES.get(seq)
                if tok is not None:
                    out.append(tok)
                continue                            # unknown sequences are dropped
            out.append(ch)
        return out

    def _handle_keys(self, keys: list) -> bool:
        """Apply one batch of input. True means quit."""
        tokens = self._tokens(keys)
        printable = sum(1 for t in tokens if isinstance(t, str) and len(t) == 1
                        and t.isprintable())
        pasted = printable >= 3                     # a burst that size is a paste, not typing
        ignored_paste = False
        i = 0
        while i < len(tokens):
            t = tokens[i]
            i += 1
            if t == "<paste>":
                self._in_paste = True
                continue
            if t == "</paste>":
                self._in_paste = False
                continue
            if self.pending_approval and (self._in_paste or pasted) and isinstance(t, str) \
                    and (t.isprintable() or t in ("\r", "\n", "\t")):
                # A paste is never an answer: "yes" pasted here allowed the
                # command, and any "aa" in it granted "always". Ctrl-C and the
                # scroll keys still reach the prompt.
                if not ignored_paste:
                    ignored_paste = True
                    self._toast_msg("paste ignored — answer the approval with y / n / a")
                continue
            literal = (self._in_paste or pasted) and not self.pending_approval \
                and self.picker is None and self._splash_done
            if literal and t in ("\r", "\n", "\t"):
                if t == "\r" and i < len(tokens) and tokens[i] == "\n":
                    i += 1                          # CRLF from a Windows clipboard
                self._ins("    " if t == "\t" else "\n")
                continue
            if self._in_paste and isinstance(t, str) and t.isprintable() \
                    and not self.pending_approval:
                self._ins(t)
                continue
            if self._key(t):
                return True
        return False

    def _key(self, ch) -> bool:
        """One key. True means quit."""
        if not self._splash_done:      # any key skips the splash
            self._splash_done = True
            try:
                curses.curs_set(1)
            except curses.error:
                pass
            return False

        if ch == "\x1b" + "ESC":
            ch = "\x1b"
        # decoded escape sequences arrive as "<home>"/"<end>" strings; as
        # strings they fell into the text branch and were typed into the input
        ch = _TOKEN_KEYS.get(ch, ch) if isinstance(ch, str) else ch

        if self.pending_approval:
            return self._approval_key(ch)

        if self.picker is not None:
            return self._picker_key(ch)

        # mouse wheel -> scroll the transcript (works only with mouse capture on)
        if ch == curses.KEY_MOUSE:
            delta = self._wheel()
            if delta:
                self._scroll_by(delta)
            return False

        if isinstance(ch, str):
            if ch in ("\n", "\r"):
                return self._enter()
            if ch == "\x03":                                # Ctrl-C
                if self.busy:
                    self.agent.interrupt()
                    self._queued.clear()
                    self._toast_msg("interrupt requested")
                elif self.input:
                    self._clear_input()
                else:
                    return True
            elif ch == "\x14":                                # Ctrl-T
                self.show_thinking = not self.show_thinking
                self._toast_msg("reasoning " + ("shown" if self.show_thinking else "hidden"))
            elif ch == "\x0c":                                # Ctrl-L
                self._redraw = True
            elif ch == "\x12":                                # Ctrl-R  history browser
                self._open_picker()
            elif ch == "\x15":                                # Ctrl-U  delete to start
                self._kill = self.input[:self.cur]
                self.input = self.input[self.cur:]
                self.cur = 0
            elif ch == "\x0b":                                # Ctrl-K  delete to end
                self._kill = self.input[self.cur:]
                self.input = self.input[:self.cur]
            elif ch == "\x19":                                # Ctrl-Y  put it back
                # It used to switch YOLO on — the readline key for "yank"
                # silently auto-approved every tool. /yolo does that now.
                if self._kill:
                    self._ins(self._kill)
            elif ch == "\x17":                                # Ctrl-W  delete word
                j = self.cur
                while j > 0 and self.input[j - 1] == " ":
                    j -= 1
                while j > 0 and self.input[j - 1] not in " \n":
                    j -= 1
                self._kill = self.input[j:self.cur]
                self.input = self.input[:j] + self.input[self.cur:]
                self.cur = j
            elif ch == "\x01":                                # Ctrl-A  line start
                self.cur = 0
            elif ch == "\x05":                                # Ctrl-E  line end
                self.cur = len(self.input)
            elif ch in ("\x7f", "\b", "\x08"):                # backspace
                if self.cur > 0:
                    self.input = self.input[:self.cur - 1] + self.input[self.cur:]
                    self.cur -= 1
            elif ch == "\t":
                self._cycle_think(1)
            elif ch.isprintable():
                self._ins(ch)
            return False

        if ch == curses.KEY_RESIZE:
            if IS_WINDOWS:
                try:
                    curses.resize_term(0, 0)     # PDCurses needs telling the new size
                except curses.error:
                    pass
            self._redraw = True
        elif ch in _ENTER_KEYS:
            return self._enter()
        elif ch == curses.KEY_BACKSPACE:
            if self.cur > 0:
                self.input = self.input[:self.cur - 1] + self.input[self.cur:]
                self.cur -= 1
        elif ch == curses.KEY_DC:                         # Delete
            if self.cur < len(self.input):
                self.input = self.input[:self.cur] + self.input[self.cur + 1:]
        elif ch == curses.KEY_LEFT:
            self.cur = max(0, self.cur - 1)
        elif ch == curses.KEY_RIGHT:
            self.cur = min(len(self.input), self.cur + 1)
        elif ch == curses.KEY_BTAB:
            self._cycle_think(1)
        elif ch == curses.KEY_PPAGE:
            self._scroll_page(1)
        elif ch == curses.KEY_NPAGE:
            self._scroll_page(-1)
        elif ch in _SHIFT_UP:
            self._scroll_by(1)
        elif ch in _SHIFT_DOWN:
            self._scroll_by(-1)
        elif ch in _HOME_KEYS:
            if self.input:
                self.cur = 0
            else:
                self.scroll = self._max_scroll           # jump to the top
        elif ch in _END_KEYS:
            if self.input:
                self.cur = len(self.input)
            else:
                self.scroll = 0                          # jump to the newest
        elif ch == curses.KEY_UP:
            if not self._vertical(-1):
                self._history_step(-1)
        elif ch == curses.KEY_DOWN:
            if not self._vertical(1):
                self._history_step(1)
        return False

    def _wheel(self) -> int:
        try:
            _id, _mx, _my, _mz, bstate = curses.getmouse()
        except curses.error:
            return 0
        if bstate & curses.BUTTON4_PRESSED:
            return 3
        # ncurses mouse version 1 (macOS's system library) has no BUTTON5 and
        # reports the wheel going down as button 2
        down = getattr(curses, "BUTTON5_PRESSED", 0) or curses.BUTTON2_PRESSED
        if bstate & down:
            return -3
        return 0

    def _enter(self) -> bool:
        line = self.input.strip()
        if not line:
            self._clear_input()
            return False
        first = line.split(None, 1)[0].lower()
        if line.startswith("//"):
            line = line[1:]                    # an escaped leading slash: a message
        elif first in self.COMMANDS:
            self._clear_input()
            return self._command(line)
        elif re.fullmatch(r"/[a-z][a-z-]*", first):
            # looks like a command, is not one: a typo should not go to the model
            self._toast_msg(f"unknown command {first} — /help, or // to send it as a message")
            return False
        # anything else that starts with "/" is a path: "/usr/bin/foo is broken"
        if self.busy:
            # It used to be thrown away with a toast; now it waits its turn.
            self._queued.append(line)
            self._clear_input()
            self._toast_msg("queued — sends when this turn ends (Ctrl-C drops it)")
            return False
        self._clear_input()
        self._submit(line)
        return False

    def _approval_key(self, ch) -> bool:
        if ch == "\x03":                                # Ctrl-C: deny, and stop
            self.agent.interrupt()
            self._answer(False)
            self._toast_msg("interrupt requested")
            return False
        if ch in (curses.KEY_PPAGE, curses.KEY_UP) or ch in _SHIFT_UP:
            self._approval_scroll = max(0, self._approval_scroll - (5 if ch == curses.KEY_PPAGE else 1))
            return False
        if ch in (curses.KEY_NPAGE, curses.KEY_DOWN) or ch in _SHIFT_DOWN:
            self._approval_scroll = min(self._approval_rows,
                                        self._approval_scroll + (5 if ch == curses.KEY_NPAGE else 1))
            return False
        if ch == curses.KEY_MOUSE:
            d = self._wheel()
            self._approval_scroll = max(0, min(self._approval_rows, self._approval_scroll - d))
            return False
        if time.time() - self._approval_at < APPROVAL_GRACE:
            return False                                # too soon to be an answer
        if ch in ("y", "Y"):
            self._answer(True)
        elif ch in ("a", "A"):
            if self._arm("always"):
                self.cfg.auto_approve.add(self.pending_approval[0])
                self._answer(True)
        elif ch in ("n", "N", "\x1b"):
            self._answer(False)
        else:
            self._armed = ("", 0.0)
        return False

    def _answer(self, ok: bool):
        self.pending_approval = None
        self._armed = ("", 0.0)
        self.approve_reply.put(ok)

    def _picker_key(self, ch) -> bool:
        pk = self.picker
        sid = pk["rows"][pk["sel"]][0]
        if ch in ("d", "D", curses.KEY_DC):
            # one keypress used to delete a chat for good
            if self._arm("del:" + sid):
                self._delete_session(sid)
            return False
        self._armed = ("", 0.0)
        if ch in ("\x1b", "q", "Q", "\x12", "\x03"):  # Esc / q / Ctrl-R / Ctrl-C
            self.picker = None
        elif ch in ("\n", "\r") or ch in _ENTER_KEYS:
            self._view_session(sid)
        elif ch == "\x0f":                          # Ctrl-O resume
            self._resume_session(sid)
        elif ch == curses.KEY_UP:
            pk["sel"] = max(0, pk["sel"] - 1)
        elif ch == curses.KEY_DOWN:
            pk["sel"] = min(len(pk["rows"]) - 1, pk["sel"] + 1)
        elif ch == curses.KEY_PPAGE:
            pk["sel"] = max(0, pk["sel"] - 10)
        elif ch == curses.KEY_NPAGE:
            pk["sel"] = min(len(pk["rows"]) - 1, pk["sel"] + 10)
        elif ch in _HOME_KEYS:
            pk["sel"] = 0
        elif ch in _END_KEYS:
            pk["sel"] = len(pk["rows"]) - 1
        return False

    # -- main loop ------------------------------------- #
    def loop(self, stdscr):
        try:
            curses.curs_set(0)              # hidden during the boot splash
        except curses.error:
            pass
        self._boot = time.time()
        stdscr.keypad(True)
        try:
            # curses.wrapper leaves the terminal in cbreak mode, where Ctrl-C is
            # still SIGINT: it surfaced as KeyboardInterrupt in this loop and
            # quit the whole app mid-turn instead of interrupting the agent.
            # Raw mode delivers it as the key "\x03" (on Windows, PDCurses'
            # raw() likewise turns off the console's own Ctrl-C handling).
            curses.raw()
        except curses.error:
            pass
        if hasattr(curses, "set_escdelay"):
            try:
                curses.set_escdelay(25)     # a lone Esc used to freeze input for a second
            except curses.error:
                pass
        _bracketed_paste(True)
        self._enable_mouse(getattr(self.cfg, "mouse", True))
        self.theme.init()

        if not self.cfg.remote_endpoint:
            try:
                from .store import Store
                self.store = Store(self.cfg.host, self.cfg.embed_model, self.cfg.rag,
                                   api=getattr(self.agent.client, "api", "ollama"))
                self.agent.store = self.store
            except Exception as e:  # noqa: BLE001
                self._append("error", f"history store off: {e}")
        self._bg(self._startup)
        self._append("info", f"=^..^=  {random.choice(MEOWS)}")

        last = time.time()
        while True:
            dirty = self._drain() or self._dirty
            self._dirty = False
            now = time.time()
            toast = now < self._toast_until
            animating = (self.busy or not self._splash_done or toast
                         or now - self._last_activity < IDLE_ANIMATION)
            if animating:
                dt = min(0.25, now - last)
                self._phase += dt * 5.4          # the old 0.18 per 33 ms frame
                if int(now / 0.09) != int(last / 0.09):
                    self.spin += 1
                if now - self._last_cat > (0.18 if (self.busy or not self._splash_done) else 0.9):
                    self.cat_frame += 1
                    self._last_cat = now
                dirty = True
            last = now
            if dirty:
                self._draw(stdscr)

            keys = self._read_keys(stdscr, 33 if animating else 500)
            if not keys:
                continue
            self._last_activity = time.time()
            self._dirty = True
            if self._handle_keys(keys):
                break


_ENTER_KEYS = {k for k in (getattr(curses, "KEY_ENTER", None), getattr(curses, "PADENTER", None))
               if k is not None}
_SHIFT_UP = {k for k in (getattr(curses, "KEY_SR", None), getattr(curses, "KEY_SUP", None))
             if k is not None}
_SHIFT_DOWN = {k for k in (getattr(curses, "KEY_SF", None), getattr(curses, "KEY_SDOWN", None))
               if k is not None}
_HOME_KEYS = {k for k in (curses.KEY_HOME, getattr(curses, "KEY_FIND", None),
                          getattr(curses, "KEY_A1", None)) if k is not None} | {"<home>"}
_END_KEYS = {k for k in (curses.KEY_END, getattr(curses, "KEY_SELECT", None),
                         getattr(curses, "KEY_C1", None)) if k is not None} | {"<end>"}
_TOKEN_KEYS = {"<home>": curses.KEY_HOME, "<end>": curses.KEY_END}
# Escape sequences ncurses' terminfo may not decode; anything else is dropped.
_ESCAPES = {
    "[200~": "<paste>", "[201~": "</paste>",
    "[H": "<home>", "[1~": "<home>", "[7~": "<home>", "OH": "<home>",
    "[F": "<end>", "[4~": "<end>", "[8~": "<end>", "OF": "<end>",
    "[A": curses.KEY_UP, "[B": curses.KEY_DOWN, "[C": curses.KEY_RIGHT, "[D": curses.KEY_LEFT,
    "OA": curses.KEY_UP, "OB": curses.KEY_DOWN, "OC": curses.KEY_RIGHT, "OD": curses.KEY_LEFT,
    "[1;2A": getattr(curses, "KEY_SR", curses.KEY_UP), "[1;2B": getattr(curses, "KEY_SF", curses.KEY_DOWN),
    "[3~": curses.KEY_DC, "[5~": curses.KEY_PPAGE, "[6~": curses.KEY_NPAGE, "[Z": curses.KEY_BTAB,
}


def _bracketed_paste(on: bool) -> None:
    """Ask an xterm-compatible terminal to mark pastes. Windows consoles feed
    PDCurses key events instead; there the batch rule alone does the job."""
    if IS_WINDOWS or os.environ.get("TERM", "") in ("", "dumb", "linux"):
        return
    try:
        sys.stdout.write("\x1b[?2004h" if on else "\x1b[?2004l")
        sys.stdout.flush()
    except (OSError, ValueError):
        pass


def _unquote(s: str) -> str:
    s = (s or "").strip()
    if len(s) > 1 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    return s


def _tname(think):
    return "off" if think in (False, None, "off") else str(think)


def _fmt_args(args) -> str:
    if not isinstance(args, dict) or not args:
        return "()"
    bits = []
    for k, v in args.items():
        sv = repr(v)
        if len(sv) > 80:
            sv = sv[:77] + "…'"
        bits.append(f"{k}={sv}")
    s = "(" + ", ".join(bits) + ")"
    return s if len(s) < 200 else s[:200] + "…)"


def run(cfg: Config, agent_factory=None):
    os.environ.setdefault("ESCDELAY", "25")       # before initscr; set_escdelay is 3.9+
    ui = TUI(cfg, agent_factory)
    try:
        curses.wrapper(ui.loop)
    finally:
        _bracketed_paste(False)
        # Quitting used to leave the agent's command running and, in remote
        # mode, the server seat held for ten minutes (no `bye`).
        close = getattr(ui.agent, "close", None)
        if close is not None:
            try:
                close()
            except Exception:  # noqa: BLE001
                pass
