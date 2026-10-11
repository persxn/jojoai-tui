"""Curses UI for JojoAI.

green theme + gradients · an ASCII cat · keyboard shortcuts · slash commands ·
you on the right / jojo on the left · syntax-highlighted code · persistent
history with cross-chat recall (RAG).
"""

from __future__ import annotations

import curses
import datetime as _dt
import math
import os
import queue
import random
import re
import textwrap
import threading
import time

from .agent import Agent
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
  /cwd <path>           change the working directory tools act on
  /steps <n>            max tool iterations per turn
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

keyboard
  Enter          send (pasted newlines are kept, not sent)
  mouse wheel    scroll the conversation
  PgUp / PgDn    scroll a page      Shift-↑ / Shift-↓  scroll a line
  Home / End     top / newest (when the input box is empty)
  Shift-Tab/Tab  cycle think effort      Ctrl-Y  toggle YOLO
  Ctrl-T         show/hide reasoning      Ctrl-L  redraw
  ← →            move cursor              Ctrl-A/E  line start/end
  Ctrl-W  delete word   Ctrl-U  clear input
  Up / Down      input history            Ctrl-R  chat-history browser
  Ctrl-C         interrupt · clear input · quit
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


class Block:
    __slots__ = ("kind", "text", "ts")

    def __init__(self, kind, text=""):
        self.kind = kind
        self.text = text
        self.ts = time.time()


class TUI:
    def __init__(self, cfg: Config, agent_factory=None):
        self.cfg = cfg
        self.theme = Theme()
        self.blocks: list[Block] = []
        self.scroll = 0                   # rows the viewport bottom sits ABOVE the newest row
        self._max_scroll = 0              # set every frame by _draw
        self._body_h = 10                # visible transcript rows, set every frame by _draw
        self._last_row_count = 0         # to hold the view still while output streams in
        self.mouse_on = False
        self.input = ""
        self.cur = 0                      # cursor index within self.input
        self._last_key = 0.0             # for paste-burst detection
        self._burst_len = 0             # consecutive fast keystrokes (paste run)
        self.events: queue.Queue = queue.Queue()
        self.approve_reply: queue.Queue = queue.Queue()
        self.pending_approval = None
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
        self._phase = 0.0               # global animation clock (advances each frame)
        self._boot = 0.0               # set when the curses loop starts
        self._splash_done = False
        self.history: list[str] = []
        self.hist_idx: int | None = None
        self.last_user: str | None = None
        self._btw: list[str] = []
        self._toast = ""
        self._toast_until = 0.0
        self._redraw = False
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

    # -- blocks -------------------------------------------------- #
    def _append(self, kind, text=""):
        self.blocks.append(Block(kind, text))

    def _extend_last(self, kind, text):
        if self.blocks and self.blocks[-1].kind == kind:
            self.blocks[-1].text += text
        else:
            self._append(kind, text)

    def _toast_msg(self, msg, secs=2.2):
        self._toast = msg
        self._toast_until = time.time() + secs

    # -- model ------------------------------------------------ #
    def _cmd_model(self, arg: str):
        """`/model` — what is running; `/model <tag> [--save]` — switch, and
        optionally make it this machine's default.

        The --save half is the point: without somewhere to write the answer,
        every session started on whatever the built-in default was and the
        model you actually pulled had to be re-typed forever.
        """
        from .config import load_settings, save_settings, settings_path

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
            lines = [f"running   {self.cfg.model}"]
            lines.append(f"default   {saved or '(none saved — using the built-in default)'}")
            try:
                pulled = self.agent.client.list_models()
                lines.append("pulled    " + (", ".join(pulled) if pulled else "nothing yet"))
            except Exception as e:  # noqa: BLE001
                lines.append(f"pulled    (cannot ask Ollama: {e})")
            lines.append(f"settings  {settings_path()}")
            return self._append("info", "\n".join(lines))

        self.cfg.model = name
        self.agent.client.model = name
        if save:
            return self._save_model(name)
        self._toast_msg(f"model → {name}")
        if not self.cfg.remote_endpoint and not self.agent.client.has_model(name):
            self._append("error", f"heads up: {name!r} is not pulled — run: ollama pull {name}")

    def _save_model(self, name: str):
        from .config import save_settings
        try:
            path = save_settings(model=name)
        except OSError as e:
            return self._append("error", f"could not save the model: {e}")
        self._toast_msg(f"model → {name} (saved)")
        self._append("info", f"{name} is now this machine's default · {path}")

    def _model_missing(self, name: str):
        """The model this session wants is not on the machine.

        The old message said `ollama pull <it>` and stopped — which, when the
        wanted model was the 65 GB default and the machine had a 7B one sitting
        right there, was advice to download something that would not run. So
        say what is actually available, and how to make the good one stick.
        """
        from .config import load_settings

        try:
            pulled = self.agent.client.list_models()
        except Exception:  # noqa: BLE001
            pulled = []
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
    def _drain(self):
        while True:
            try:
                ev = self.events.get_nowait()
            except queue.Empty:
                return
            k = ev[0]
            if k == "thinking":
                if self.show_thinking:
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
            elif k == "__approval__":
                self.pending_approval = (ev[1], ev[2])
            elif k == "__idle__":
                self.busy = False

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
    def _rows(self, width):
        A = self.theme.A
        rows: list[tuple[str, list]] = []
        bw = max(20, min(int(width * 0.72), 76))       # bubble wrap width (right side)

        def L(segs):
            rows.append(("l", segs))

        def R(segs):
            rows.append(("r", segs))

        for b in self.blocks:
            hhmm = _dt.datetime.fromtimestamp(b.ts).strftime("%H:%M")
            if b.kind == "user":
                R([(f"you · {hhmm} ▐", A["user"])])
                for para in b.text.split("\n"):
                    for ln in (textwrap.wrap(para, width=bw, drop_whitespace=False) or [""]):
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
                            for ln in textwrap.wrap(para, width=width - 3,
                                                    drop_whitespace=False) or [""]:
                                L([("  ", 0)] + inline_segments(ln, A))
                    else:
                        _, lang, code = chunk
                        lg = norm_lang(lang)
                        label = lang.strip() or ("code" if lg == "generic" else lg)
                        L([("  ╭─ ", A["rule"]), (label + " ", A["hint"]),
                           ("─" * max(0, width - 8 - len(label)), A["rule"])])
                        for cl in code:
                            segs = [("  │ ", A["rule"])]
                            segs += highlight_line(cl[:width - 6], lg, A)
                            L(segs)
                        L([("  ╰" + "─" * max(0, width - 6), A["rule"])])
                rows.append(("l", [("", 0)]))
            elif b.kind == "thinking":
                live = (b is self.blocks[-1]) and self.busy
                dots = "." * (1 + (self.spin // 3) % 3) if live else ""
                hdr = f"   ╭╴ thinking{dots} "
                L([(hdr, A["think_hdr"]),
                   ("┄" * max(0, width - _slen(hdr) - 1), A["rule"])])
                body = b.text.strip("\n") or "…"
                for para in body.split("\n"):
                    for ln in textwrap.wrap(para, width=max(10, width - 6),
                                            drop_whitespace=False) or [""]:
                        L([("   ┆ ", A["think_gut"]), (ln, A["think"])])
                if not live:
                    L([("   ╰" + "┄" * 5, A["rule"])])
                rows.append(("l", [("", 0)]))
            elif b.kind == "tool":
                first = True
                for para in b.text.split("\n"):
                    for ln in textwrap.wrap(para, width=max(10, width - 6),
                                            drop_whitespace=False) or [""]:
                        L([("  ▸ " if first else "    ", A["tool"]), (ln, A["tool"])])
                        first = False
            elif b.kind == "tool_out":
                for para in b.text.split("\n"):
                    for ln in textwrap.wrap(para, width=max(10, width - 6),
                                            drop_whitespace=False) or [""]:
                        L([("   ╎ ", A["rule"]), (ln, A["out"])])
                rows.append(("l", [("", 0)]))
            else:
                pref = {"info": "  ", "btw": "  ~ btw · ", "error": "  ✗ "}.get(b.kind, "  ")
                attr = {"info": A["info"], "btw": A["hint"],
                        "error": A["error"]}.get(b.kind, A["assist"])
                first = True
                for para in b.text.split("\n"):
                    for ln in textwrap.wrap(para, width=max(10, width - len(pref)),
                                            drop_whitespace=False) or [""]:
                        L([((pref if first else " " * len(pref)) + ln, attr)])
                        first = False
                rows.append(("l", [("", 0)]))

        # a soft blinking caret on whatever Jojo is streaming right now
        if self.busy and self.blocks and self.blocks[-1].kind in ("assistant", "thinking") \
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
            try:
                win.addnstr(y, x, text, w - 1 - x, attr)
            except curses.error:
                pass
            x += _slen(text)

    def _wrap_input(self, text, width):
        """List of (start, end) index pairs, one per visual input line."""
        width = max(4, width)
        segs = []
        off = 0
        for part in text.split("\n"):
            if part == "":
                segs.append((off, off))
            else:
                s = 0
                while s < len(part):
                    e = min(s + width, len(part))
                    if e < len(part):
                        sp = part.rfind(" ", s, e)
                        if sp > s:
                            e = sp + 1
                    segs.append((off + s, off + e))
                    s = e
            off += len(part) + 1
        return segs or [(0, 0)]

    def _input_first(self, segs, rows):
        ci = 0
        for i, (s, e) in enumerate(segs):
            if s <= self.cur <= e:
                ci = i
        return 0 if ci < rows else ci - rows + 1

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
        self._safe(stdscr, y + 1, 0, seg.ljust(w - 1), A["status"], w)

        if self.pending_approval:
            name, args = self.pending_approval
            self._safe(stdscr, y + 2, 0,
                       f" ✓/✗ allow  {name} {_fmt_args(args)}   [y]es  [n]o  [a]lways ",
                       A["error"], w)
            for r in range(1, in_rows):
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
                    cur_row, cur_col = vr, len(pre) + (self.cur - s)
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
            win.addnstr(y, x, text, max(0, w - 1 - x), attr)
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
    def _command(self, line: str) -> bool:
        parts = line.split()
        cmd, arg = parts[0], " ".join(parts[1:])
        if cmd in ("/quit", "/exit"):
            return True
        elif cmd == "/help":
            self._append("info", HELP)
        elif cmd == "/reset":
            self.agent.reset()                      # wipes conversation messages
            self.blocks.clear()
            self.stats_line = ""
            self._btw.clear()
            self._clear_input()
            self.scroll = 0
            if self.store is not None:
                try:
                    from .store import Store
                    self.store = Store(self.cfg.host, self.cfg.embed_model,
                                       self.cfg.rag)          # brand-new session id
                    self.agent.store = self.store
                except Exception:  # noqa: BLE001
                    pass
            self._append("info", "fresh chat — nothing carried over")
        elif cmd == "/retry":
            if self.last_user and not self.busy:
                self._submit(self.last_user)
            else:
                self._append("error", "nothing to retry")
        elif cmd == "/tools":
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
            p = os.path.expanduser(arg)
            if arg and os.path.isdir(p):
                self.cfg.cwd = os.path.abspath(p)
                self.agent.ctx.cwd = self.cfg.cwd
                self._toast_msg(f"cwd → {self.cfg.cwd}")
            else:
                self._append("error", f"not a directory: {arg}")
        elif cmd == "/steps" and arg.isdigit():
            self.cfg.max_steps = int(arg)
            self._toast_msg(f"max steps → {arg}")
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
            self._save_transcript(arg.strip() or None)
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
            self._append("error", f"unknown command: {cmd}  (/help)")
        self.scroll = 0                   # a command's output should be visible
        return False

    def _rag_cmd(self, arg):
        if self.store is None:
            self._append("error", "history store unavailable")
            return
        if arg == "on":
            self.store.rag_enabled = True
            self._toast_msg("recall on")
        elif arg == "off":
            self.store.rag_enabled = False
            self._toast_msg("recall off")
        else:
            s = self.store.stats()
            embed = "ready" if s["embed_available"] else \
                "NOT pulled — run: ollama pull nomic-embed-text"
            state = "on" if self.store.rag_enabled else "off"
            self._append("info",
                         f"recall {state} · embed {s['embed_model']} ({embed}) · "
                         f"{s['chunks']} chunks from {s['sessions_indexed']} past sessions · "
                         f"{s['data_dir']}")

    def _recall_cmd(self, arg):
        if self.store is None:
            self._append("error", "history store unavailable")
            return
        use = arg.startswith("--use ")
        q = (arg[6:] if use else arg).strip()
        if not q:
            self._append("error", "usage: /recall [--use] <query>")
            return
        hits = self.store.retrieve(q, k=5, min_score=0.25)
        if not hits:
            self._append("info", "no matching context found in your other chats")
            return
        out = []
        for score, sess, ts, title, text in hits:
            when = _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
            out.append(f"[{when} · {title or sess} · {score:.2f}]\n{text}")
        self._append("info", "recalled (reference only):\n\n" + "\n\n".join(out))
        if use:
            self._btw.append("From an earlier chat: "
                             + " ".join(h[4][:300] for h in hits))
            self._toast_msg("folded into your next message")

    def _history_cmd(self):
        # /history opens the same interactive browser as Ctrl-R
        self._open_picker()

    # -- history browser (Ctrl-R) ----------------------- #
    def _open_picker(self):
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
        self._safe(stdscr, top, 1,
                   "chat history   ↑↓ move · Enter view · Ctrl-O resume · d delete · Esc close",
                   A["hint"], w)
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
            self._safe(stdscr, top + 2 + i, 0, line.ljust(w - 1), attr, w)
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
        extra = ""
        if self.store is not None:
            s = self.store.stats()
            extra = (f" · index {s['chunks']} chunks / {s['sessions_indexed']} sessions"
                     f" · {s['data_dir']}")
        self._append("info", f"model {self.cfg.model} · {msgs} messages in context · "
                             f"ctx {self.cfg.num_ctx}{extra}")

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

    # -- main loop ------------------------------------- #
    def loop(self, stdscr):
        try:
            curses.curs_set(0)              # hidden during the boot splash
        except curses.error:
            pass
        self._boot = time.time()
        stdscr.nodelay(True)
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
        self._enable_mouse(getattr(self.cfg, "mouse", True))
        self.theme.init()

        if not self.cfg.remote_endpoint:
            try:
                from .store import Store
                self.store = Store(self.cfg.host, self.cfg.embed_model, self.cfg.rag)
                self.agent.store = self.store
            except Exception as e:  # noqa: BLE001
                self._append("error", f"history store off: {e}")

        try:
            v = self.agent.client.version()
            if self.cfg.remote_endpoint:
                self._append("info", f"connected · {v} · tools run in {self.cfg.cwd}")
            else:
                self._append("info", f"connected · Ollama {v} · {self.cfg.cwd}")
                if not self.agent.client.has_model(self.cfg.model):
                    self._model_missing(self.cfg.model)
                if self.store is not None and self.cfg.rag and not self.store.embed_available():
                    self._append("info", "recall: embed model not pulled yet — "
                                         "run: ollama pull nomic-embed-text")
        except Exception as e:  # noqa: BLE001
            self._append("error", str(e))
        self._append("info", f"=^..^=  {random.choice(MEOWS)}")

        last_spin = 0.0
        while True:
            self._drain()
            now = time.time()
            self._phase += 0.18            # global animation clock
            if now - last_spin > 0.09:
                self.spin += 1
                last_spin = now
            if now - self._last_cat > (0.18 if (self.busy or not self._splash_done)
                                       else 0.9):
                self.cat_frame += 1
                self._last_cat = now
            self._draw(stdscr)

            try:
                ch = stdscr.get_wch()
            except curses.error:
                ch = None
            except KeyboardInterrupt:      # a SIGINT raw mode did not stop
                ch = "\x03"
            if ch is None:
                try:
                    time.sleep(0.033)
                    continue
                except KeyboardInterrupt:
                    ch = "\x03"

            if not self._splash_done:      # any key skips the splash
                self._splash_done = True
                try:
                    curses.curs_set(1)
                except curses.error:
                    pass
                continue

            if self.pending_approval:
                if ch in ("y", "Y"):
                    self.approve_reply.put(True); self.pending_approval = None
                elif ch in ("a", "A"):
                    self.cfg.auto_approve.add(self.pending_approval[0])
                    self.approve_reply.put(True); self.pending_approval = None
                elif ch in ("n", "N", "\x1b"):
                    self.approve_reply.put(False); self.pending_approval = None
                elif ch == "\x03":                                # Ctrl-C: deny, and stop
                    self.agent.interrupt()
                    self.approve_reply.put(False); self.pending_approval = None
                    self._toast_msg("interrupt requested")
                continue

            if self.picker is not None:
                pk = self.picker
                if ch in ("\x1b", "q", "Q", "\x12", "\x03"):  # Esc / q / Ctrl-R / Ctrl-C
                    self.picker = None
                elif ch in ("\n", "\r"):
                    self._view_session(pk["rows"][pk["sel"]][0])
                elif ch == "\x0f":                          # Ctrl-O resume
                    self._resume_session(pk["rows"][pk["sel"]][0])
                elif ch in ("d", "D"):
                    self._delete_session(pk["rows"][pk["sel"]][0])
                elif ch == curses.KEY_UP:
                    pk["sel"] = max(0, pk["sel"] - 1)
                elif ch == curses.KEY_DOWN:
                    pk["sel"] = min(len(pk["rows"]) - 1, pk["sel"] + 1)
                elif ch == curses.KEY_PPAGE:
                    pk["sel"] = max(0, pk["sel"] - 10)
                elif ch == curses.KEY_NPAGE:
                    pk["sel"] = min(len(pk["rows"]) - 1, pk["sel"] + 10)
                elif ch == curses.KEY_HOME:
                    pk["sel"] = 0
                elif ch == curses.KEY_END:
                    pk["sel"] = len(pk["rows"]) - 1
                continue

            # mouse wheel -> scroll the transcript (works only with mouse capture on)
            if ch == curses.KEY_MOUSE:
                try:
                    _id, _mx, _my, _mz, bstate = curses.getmouse()
                except curses.error:
                    continue
                if bstate & curses.BUTTON4_PRESSED:
                    self._scroll_by(3)
                elif bstate & getattr(curses, "BUTTON5_PRESSED", 0):
                    self._scroll_by(-3)
                continue

            if isinstance(ch, str):
                # paste detection: bytes in a paste arrive back-to-back. Count a
                # run of fast chars; only a newline landing INSIDE such a run is
                # treated as pasted text. A newline after any normal pause sends.
                if (time.time() - self._last_key) < 0.012:
                    self._burst_len += 1
                else:
                    self._burst_len = 0
                self._last_key = time.time()
                if ch in ("\n", "\r"):
                    if self._burst_len >= 2:                      # keep pasted newlines
                        self._ins("\n")
                        continue
                    line = self.input.strip()
                    if not line:
                        self._clear_input()
                        continue
                    self._clear_input()
                    if line.startswith("/"):
                        if self._command(line):
                            break
                    elif self.busy:
                        self._toast_msg("still working — Ctrl-C to interrupt")
                    else:
                        self._submit(line)
                elif ch == "\x03":                                # Ctrl-C
                    if self.busy:
                        self.agent.interrupt()
                        self._toast_msg("interrupt requested")
                    elif self.input:
                        self._clear_input()
                    else:
                        break
                elif ch == "\x19":                                # Ctrl-Y
                    self._toggle_yolo()
                elif ch == "\x14":                                # Ctrl-T
                    self.show_thinking = not self.show_thinking
                    self._toast_msg("reasoning " + ("shown" if self.show_thinking else "hidden"))
                elif ch == "\x0c":                                # Ctrl-L
                    self._redraw = True
                elif ch == "\x12":                                # Ctrl-R  history browser
                    self._open_picker()
                elif ch == "\x15":                                # Ctrl-U  clear
                    self._clear_input()
                elif ch == "\x17":                                # Ctrl-W  delete word
                    j = self.cur
                    while j > 0 and self.input[j - 1] == " ":
                        j -= 1
                    while j > 0 and self.input[j - 1] != " ":
                        j -= 1
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
            else:
                if ch == curses.KEY_RESIZE:
                    self._redraw = True
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
                elif ch == getattr(curses, "KEY_SR", -99):        # Shift-Up
                    self._scroll_by(1)
                elif ch == getattr(curses, "KEY_SF", -99):        # Shift-Down
                    self._scroll_by(-1)
                elif ch == curses.KEY_HOME:
                    if self.input:
                        self.cur = 0
                    else:
                        self.scroll = self._max_scroll           # jump to the top
                elif ch == curses.KEY_END:
                    if self.input:
                        self.cur = len(self.input)
                    else:
                        self.scroll = 0                          # jump to the newest
                elif ch == curses.KEY_UP:
                    if self.history:
                        self.hist_idx = (len(self.history) - 1 if self.hist_idx is None
                                         else max(0, self.hist_idx - 1))
                        self.input = self.history[self.hist_idx]
                        self.cur = len(self.input)
                elif ch == curses.KEY_DOWN:
                    if self.history and self.hist_idx is not None:
                        self.hist_idx += 1
                        if self.hist_idx >= len(self.history):
                            self.hist_idx = None
                            self._clear_input()
                        else:
                            self.input = self.history[self.hist_idx]
                            self.cur = len(self.input)


def _slen(s: str) -> int:
    return len(s)


# Tool output is whatever the command printed: colour codes (`ls --color`,
# npm, pytest), "\r" progress bars (pip, curl) and tabs. curses draws an
# escape as "^[", jumps to column 0 on "\r" and expands a tab past the width
# the layout counted — each one smeared the transcript.
_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?|[@-Z\\-_])")
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _clean(text: str) -> str:
    """Make finished text safe to draw: no escapes, no carriage returns, no tabs."""
    text = _ANSI.sub("", text).replace("\r\n", "\n")
    if "\r" in text:
        # a progress bar redraws its line: keep what it said last
        text = "\n".join(ln.rsplit("\r", 1)[-1] if ln.rstrip("\r") else ""
                         for ln in text.split("\n"))
    return _CTRL.sub("", text.expandtabs(4))


def _clean_delta(text: str) -> str:
    """The streaming version: a "\r\n" can be split across two deltas, so
    carriage returns are simply dropped rather than read as line breaks."""
    return _CTRL.sub("", text.replace("\r", "").replace("\t", "    "))


def _tname(think):
    return "off" if think in (False, None, "off") else str(think)


def _fmt_args(args: dict) -> str:
    if not args:
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
    curses.wrapper(TUI(cfg, agent_factory).loop)
