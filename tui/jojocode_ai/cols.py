"""Terminal text: how many columns it takes, and making it safe to draw.

Used by the curses UI and by headless mode (which must not import curses: on
Windows it may not be installed).

── Width ─────────────────────────────────────────────────────────────────

len() counts code points. A terminal draws CJK and most emoji two columns wide
and combining marks (Devanagari vowel signs, accents) zero wide, so laying a
chat out by len() clipped Chinese, Japanese and emoji lines at the right edge,
pushed the right-aligned "you" bubble off screen, and put the input cursor in
the wrong column. ASCII stays on the fast path — it is nearly all of the text.
"""

from __future__ import annotations

import re
import textwrap
import unicodedata
from functools import lru_cache

_ZERO = {"​", "‌", "‍", "⁠", "︎", "️", "﻿"}


@lru_cache(maxsize=4096)
def char_width(ch: str) -> int:
    if ch in _ZERO or unicodedata.combining(ch):
        return 0
    cat = unicodedata.category(ch)
    if cat in ("Mn", "Me", "Cf", "Cc"):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def width(s: str) -> int:
    if s.isascii():
        return len(s)
    return sum(char_width(c) for c in s)


def clip(s: str, cols: int) -> str:
    """The longest prefix of s that fits in `cols` columns."""
    if cols <= 0:
        return ""
    if s.isascii():
        return s[:cols]
    used = 0
    for i, c in enumerate(s):
        w = char_width(c)
        if used + w > cols:
            return s[:i]
        used += w
    return s


def wrap(text: str, cols: int) -> list[str]:
    """textwrap.wrap(text, cols, drop_whitespace=False) measured in columns."""
    cols = max(1, cols)
    if text.isascii():
        return textwrap.wrap(text, width=cols, drop_whitespace=False) or [""]
    out: list[str] = []
    line, used = "", 0
    word = ""
    tokens: list[str] = []
    for c in text:                      # split into runs of space / non-space
        if word and (c == " ") != (word[-1] == " "):
            tokens.append(word)
            word = ""
        word += c
    if word:
        tokens.append(word)
    for tok in tokens:
        w = width(tok)
        if used + w <= cols:
            line += tok
            used += w
            continue
        if line and not tok.startswith(" ") and w <= cols:
            out.append(line)
            line, used = tok, w
            continue
        # break the token itself (CJK has no spaces to break at)
        for c in tok:
            cw = char_width(c)
            if used + cw > cols and line:
                out.append(line)
                line, used = "", 0
            line += c
            used += cw
    if line or not out:
        out.append(line)
    return out


# ── Safe text ──────────────────────────────────────────────────────────────
#
# Tool output is whatever the command printed: colour codes (`ls --color`,
# npm, pytest), "\r" progress bars (pip, curl) and tabs. curses draws an
# escape as "^[", jumps to column 0 on "\r" and expands a tab past the width
# the layout counted — each one smeared the transcript.
_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?|[ -/]*[0-~])")
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def clean(text: str) -> str:
    """Make finished text safe to draw: no escapes, no carriage returns, no tabs."""
    text = _ANSI.sub("", text).replace("\r\n", "\n")
    if "\r" in text:
        # a progress bar redraws its line: keep what it said last
        text = "\n".join(ln.rsplit("\r", 1)[-1] if ln.rstrip("\r") else ""
                         for ln in text.split("\n"))
    return _CTRL.sub("", text.expandtabs(4))


def clean_delta(text: str) -> str:
    """The streaming version: a "\r\n" can be split across two deltas, so
    carriage returns are simply dropped rather than read as line breaks."""
    return _CTRL.sub("", _ANSI.sub("", text).replace("\r", "").replace("\t", "    "))


def visible(text: str) -> str:
    """For the approval prompt: nothing hidden. Escapes go, but a tab, a
    carriage return or any other control character is drawn as its symbol —
    a Windows path whose "\\t" became a real tab must not look like a path."""
    text = _ANSI.sub(lambda m: "␛" + m.group()[1:], text)
    return "".join(c if c == "\n" or c >= " " and c != "\x7f" else
                   ("␡" if c == "\x7f" else chr(0x2400 + ord(c))) for c in text)
