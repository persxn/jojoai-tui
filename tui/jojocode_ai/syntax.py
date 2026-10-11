"""Tiny offline syntax highlighter -> curses (text, attr) segments.

No dependencies. Regex tokeniser per line; good enough for chat code blocks in
python, bash, js/ts, json, and a generic fallback.
"""

from __future__ import annotations

import re

_KW = {
    "python": set("""def class return if elif else for while in is not and or import from as
        with try except finally raise lambda yield pass break continue None True False self
        async await global nonlocal assert del print match case""".split()),
    "bash": set("""if then fi for in do done while until case esac function echo export local
        return cd set read shift source alias unalias trap exit test""".split()),
    "js": set("""function const let var return if else for while do new class extends import
        export default await async try catch finally throw typeof instanceof this null
        undefined true false of switch case break continue =>""".split()),
    "json": set("true false null".split()),
    "generic": set(),
}

_LANG_ALIAS = {
    "py": "python", "python3": "python",
    "sh": "bash", "shell": "bash", "zsh": "bash", "console": "bash",
    "js": "js", "javascript": "js", "ts": "js", "typescript": "js",
    "jsx": "js", "tsx": "js", "node": "js",
    "json": "json", "yaml": "generic", "yml": "generic", "toml": "generic",
    "": "generic", "text": "generic", "txt": "generic",
}

_TOKEN = re.compile(r"""
      (?P<str>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`)
    | (?P<num>\b\d+(?:\.\d+)?\b)
    | (?P<id>[A-Za-z_]\w*)
    | (?P<ws>\s+)
    | (?P<other>.)
""", re.VERBOSE)


def norm_lang(lang: str) -> str:
    lang = (lang or "").strip().lower()
    return _LANG_ALIAS.get(lang, lang if lang in _KW else "generic")


def highlight_line(line: str, lang: str, A: dict) -> list[tuple[str, int]]:
    """Return merged (text, attr) segments for one line of code."""
    lang = norm_lang(lang)
    kw = _KW.get(lang, set())
    a_kw = A["tool"]
    a_str = A["info"]
    a_num = A["assist"]
    a_com = A["think"]
    a_txt = A["assist"]

    # whole-line comment handling
    stripped = line.lstrip()
    if (lang in ("python", "bash", "generic") and stripped.startswith("#")) or \
       (lang == "js" and stripped.startswith("//")):
        return [(line, a_com)]

    segs: list[tuple[str, int]] = []
    pos = 0
    for m in _TOKEN.finditer(line):
        kind = m.lastgroup
        tok = m.group()
        # inline comment start
        if kind == "other" and (
            (lang in ("python", "bash", "generic") and tok == "#") or
            (lang == "js" and tok == "/" and line[m.start():m.start() + 2] == "//")
        ):
            segs.append((line[m.start():], a_com))
            pos = len(line)
            break
        if kind == "str":
            attr = a_str
        elif kind == "num":
            attr = a_num
        elif kind == "id":
            attr = a_kw if tok in kw else a_txt
        else:
            attr = a_txt
        segs.append((tok, attr))
        pos = m.end()
    if pos < len(line):
        segs.append((line[pos:], a_txt))

    # merge adjacent same-attr
    merged: list[tuple[str, int]] = []
    for text, attr in segs:
        if merged and merged[-1][1] == attr:
            merged[-1] = (merged[-1][0] + text, attr)
        else:
            merged.append((text, attr))
    return merged or [(line, a_txt)]


_INLINE = re.compile(r"`([^`\n]+)`")


def inline_segments(text: str, A: dict) -> list[tuple[str, int]]:
    """Split a prose line on `inline code`, colouring the code spans."""
    out: list[tuple[str, int]] = []
    last = 0
    for m in _INLINE.finditer(text):
        if m.start() > last:
            out.append((text[last:m.start()], A["assist"]))
        out.append((m.group(1), A["info"]))
        last = m.end()
    if last < len(text):
        out.append((text[last:], A["assist"]))
    return out or [(text, A["assist"])]


# An opening fence: three or more backticks (or tildes), then an info string
# whose first word is the language — "```c#", "```python:src/app.py" and
# "``` python" all used to fail the old bare-word pattern and swap prose and
# code for the rest of the reply.
_OPEN = re.compile(r"^\s*(`{3,}|~{3,})[ \t]*([^`\s]*)[^`]*$")


def _lang(info: str) -> str:
    return re.split(r"[:{,]", info, maxsplit=1)[0]


def split_fences(body: str):
    """Yield ('text', str) and ('code', lang, [lines]) chunks."""
    lines = body.split("\n")
    i = 0
    buf: list[str] = []
    while i < len(lines):
        m = _OPEN.match(lines[i])
        if m:
            if buf:
                yield ("text", "\n".join(buf)); buf = []
            fence, lang = m.group(1), _lang(m.group(2))
            # closes on the same character, at least as many, and nothing else
            close = re.compile(r"^\s*" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}\s*$")
            code: list[str] = []
            i += 1
            while i < len(lines) and not close.match(lines[i]):
                code.append(lines[i]); i += 1
            i += 1  # skip closing fence
            if any(l.strip() for l in code):
                yield ("code", lang, code)
            elif lang:
                yield ("text", fence + lang + fence)
        else:
            buf.append(lines[i]); i += 1
    if buf:
        yield ("text", "\n".join(buf))
