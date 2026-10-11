"""Green theme + gradient / animation helpers for the curses UIs.

One palette, many jobs: a dark-forest -> mint gradient drives rules, the
wordmark and the ASCII art, while a small set of named pairs colour the chat.
Everything degrades cleanly: truecolor -> 8-colour green -> mono attributes.
"""

from __future__ import annotations

import curses
import math
import sys

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
BARS = "▁▂▃▄▅▆▇█"

A_ITALIC = getattr(curses, "A_ITALIC", 0)
# PDCurses (windows-curses) renders A_BOLD on a colour pair as "colour + 8":
# with the custom palette that is a different colour altogether, which turned
# the jojo label and the thinking header violet. Colour does the work there.
_BOLD = 0 if sys.platform == "win32" else curses.A_BOLD

# dark forest -> bright green -> pale mint  (Tailwind green 900..200), 0-255
_STOPS = [
    (20, 83, 45), (22, 101, 52), (21, 128, 61), (22, 163, 74),
    (34, 197, 94), (74, 222, 128), (134, 239, 172), (187, 247, 208),
]

_CBASE = 40   # custom color numbers start here
_PBASE = 16   # custom pair numbers start here (kept low: some terms cap at 64)


def _q(c: int) -> int:
    """0-255 channel -> 0-1000 for curses.init_color."""
    return c * 1000 // 255


class Theme:
    _KEYS = ("assist", "assist_label", "user", "think", "think_gut", "think_hdr",
             "tool", "out", "info", "error", "rule", "status", "prompt", "hint",
             "wordmark", "caret", "accent")

    def __init__(self):
        self.color = False
        self.grad: list[int] = []          # pair ids, dark -> mint
        # safe pre-init fallback so lookups never KeyError before .init()
        self.A: dict[str, int] = {k: 0 for k in self._KEYS}
        for k in ("user", "wordmark", "assist", "assist_label", "tool",
                  "think_hdr", "think_gut"):
            self.A[k] = curses.A_BOLD
        self.A["think"] = A_ITALIC
        self.A["hint"] = self.A["out"] = curses.A_DIM
        self.A["status"] = curses.A_REVERSE

    # ------------------------------------------------------------------ #
    def init(self):
        try:
            curses.start_color()
            curses.use_default_colors()
        except curses.error:
            pass
        self.color = curses.has_colors()

        if not self.color:
            B, D, R, I = curses.A_BOLD, curses.A_DIM, curses.A_REVERSE, A_ITALIC
            self.A = {
                "assist": B, "assist_label": B, "user": B,
                "think": I or D, "think_gut": B, "think_hdr": B,
                "tool": B, "out": D, "info": 0, "error": B | R, "rule": D,
                "status": R | B, "prompt": B, "hint": D, "wordmark": B,
                "caret": R, "accent": B,
            }
            return

        can = (curses.can_change_color()
               and curses.COLORS >= _CBASE + len(_STOPS) + 4)
        self.grad = []
        for i, (r, g, b) in enumerate(_STOPS):
            cnum = _CBASE + i
            if can:
                try:
                    curses.init_color(cnum, _q(r), _q(g), _q(b))
                    fg = cnum
                except curses.error:
                    fg = curses.COLOR_GREEN
            else:
                fg = curses.COLOR_GREEN
            pid = _PBASE + i
            try:
                curses.init_pair(pid, fg, -1)
            except curses.error:
                pass
            self.grad.append(pid)

        mid = (_CBASE + 3) if can else curses.COLOR_GREEN
        bright = (_CBASE + 5) if can else curses.COLOR_GREEN
        mint = (_CBASE + 7) if can else curses.COLOR_GREEN

        def _mkcolor(slot, rgb, fallback):
            if not can:
                return fallback
            try:
                curses.init_color(slot, _q(rgb[0]), _q(rgb[1]), _q(rgb[2]))
                return slot
            except curses.error:
                return fallback

        base = _CBASE + len(_STOPS)
        olive = _mkcolor(base, (189, 224, 70), curses.COLOR_YELLOW)      # #BDE046
        sage = _mkcolor(base + 1, (150, 205, 172), curses.COLOR_GREEN)   # readable reasoning
        _mkcolor(base + 2, (0, 0, 0), curses.COLOR_BLACK)               # reserved

        p = _PBASE + len(_STOPS) + 1

        def mk(fg, bg=-1):
            nonlocal p
            try:
                curses.init_pair(p, fg, bg)
            except curses.error:
                pass
            p += 1
            return curses.color_pair(p - 1)

        cp_assist = mk(curses.COLOR_WHITE)          # Jojo's prose: bright white
        cp_label = mk(olive)                        # the "jojo ·" label: olive-green
        cp_user = mk(mint)
        cp_think = mk(sage)                         # reasoning body: soft, *visible*
        cp_think_gut = mk(bright)                   # reasoning gutter bar
        cp_think_hdr = mk(olive)                    # reasoning header
        cp_tool = mk(bright)
        cp_out = mk(mid)
        cp_info = mk(mint)
        cp_err = mk(curses.COLOR_RED)
        cp_rule = mk(mid)
        cp_status = mk(curses.COLOR_BLACK, bright)
        cp_prompt = mk(bright)
        cp_word = mk(bright)
        cp_caret = mk(mint)

        self.A = {
            "assist": cp_assist | _BOLD,
            "assist_label": cp_label | _BOLD,
            "user": cp_user | _BOLD,
            "think": cp_think | A_ITALIC,
            "think_gut": cp_think_gut | _BOLD,
            "think_hdr": cp_think_hdr | _BOLD,
            "tool": cp_tool | _BOLD,
            "out": cp_out,
            "info": cp_info,
            "error": cp_err | _BOLD,
            "rule": cp_rule,
            "status": cp_status | _BOLD,
            "prompt": cp_prompt | _BOLD,
            "hint": cp_rule | curses.A_DIM,
            "wordmark": cp_word | _BOLD,
            "caret": cp_caret | _BOLD,
            "accent": cp_think_gut | _BOLD,
        }

    # ------------------------------------------------------------------ #
    def _pair_at(self, frac: float) -> int:
        if not self.grad:
            return self.A.get("rule", 0)
        i = int(max(0.0, min(1.0, frac)) * (len(self.grad) - 1))
        return curses.color_pair(self.grad[i])

    def _grad_id(self, frac: float) -> int:
        n = len(self.grad)
        return self.grad[int(max(0.0, min(1.0, frac)) * (n - 1))] if n else 0

    def rule(self, win, y: int, x0: int, width: int, ch: str = "━"):
        """A smooth left->right green gradient divider."""
        if width <= 0:
            return
        if not self.grad:
            try:
                win.addnstr(y, x0, ch * width, width, self.A.get("rule", curses.A_DIM))
            except curses.error:
                pass
            return
        for i in range(width):
            try:
                win.addstr(y, x0 + i, ch,
                           self._pair_at(i / max(1, width - 1)) | _BOLD)
            except curses.error:
                pass

    def wave_rule(self, win, y: int, x0: int, width: int, phase: float = 0.0,
                  ch: str = "━"):
        """A divider whose green tones drift sideways — a slow flowing sweep."""
        if width <= 0:
            return
        if not self.grad:
            return self.rule(win, y, x0, width, ch)
        n = len(self.grad)
        for i in range(width):
            f = 0.5 + 0.5 * math.sin(i / 7.0 - phase)
            try:
                win.addstr(y, x0 + i, ch,
                           curses.color_pair(self.grad[int(f * (n - 1))]) | _BOLD)
            except curses.error:
                pass

    def gradient_text(self, win, y: int, x0: int, text: str,
                      extra: int = _BOLD):
        """Draw text with each character stepped along the gradient."""
        n = len(text)
        if not self.grad:
            try:
                win.addnstr(y, x0, text, n, self.A.get("wordmark", curses.A_BOLD))
            except curses.error:
                pass
            return
        for i, cch in enumerate(text):
            try:
                win.addstr(y, x0 + i, cch, self._pair_at(i / max(1, n - 1)) | extra)
            except curses.error:
                pass

    def shimmer_text(self, win, y: int, x0: int, text: str, phase: float = 0.0,
                     extra: int = _BOLD):
        """gradient_text with a bright highlight band that slides along `phase`."""
        n = len(text)
        if not self.grad or n == 0:
            return self.gradient_text(win, y, x0, text, extra)
        top = len(self.grad) - 1
        head = (phase * 2.4) % (n + 8) - 4
        for i, cch in enumerate(text):
            if abs(i - head) < 2.2:
                gid, a = self.grad[top], extra | _BOLD
            else:
                gid = self._grad_id(i / max(1, n - 1))
                a = extra
            try:
                win.addstr(y, x0 + i, cch, curses.color_pair(gid) | a)
            except curses.error:
                pass

    def block_art(self, win, y0: int, x0: int, lines: list[str], phase: float = 0.0):
        """Paint a multi-line ASCII block: diagonal gradient + a moving glint."""
        h = len(lines)
        w = max((len(s) for s in lines), default=1)
        top = (len(self.grad) - 1) if self.grad else 0
        head = (phase * 3.2) % (w + 12) - 6
        for r, ln in enumerate(lines):
            for c, ch in enumerate(ln):
                if ch == " ":
                    continue
                if not self.grad:
                    attr = self.A.get("wordmark", curses.A_BOLD)
                else:
                    frac = (r / max(1, h - 1)) * 0.55 + (c / max(1, w - 1)) * 0.45
                    gid = self.grad[top] if abs(c - head) < 2.0 else self._grad_id(frac)
                    attr = curses.color_pair(gid) | _BOLD
                try:
                    win.addstr(y0 + r, x0 + c, ch, attr)
                except curses.error:
                    pass
