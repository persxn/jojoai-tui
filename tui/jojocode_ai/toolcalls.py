"""Tool calls that a model wrote as text instead of sending them properly.

── Why this file exists ────────────────────────────────────────────────────

JojoAI was built and tested end to end on gpt-oss, whose tool calls always come
back through Ollama's structured `tool_calls` field. Most of the Ollama library
does not behave that well:

- **qwen2.5-coder** — what the installer recommends for most laptops — often
  writes the call as plain JSON in its reply,
  `{"name": "list_dir", "arguments": {"path": "."}}`, without the
  `<tool_call>` tags Ollama's parser waits for. Ollama hands that back as
  ordinary content; the agent saw "no tool calls", took the JSON for the final
  answer and printed it at the user. The tool never ran.
- Others fence it (```json … ```), wrap it in `<tool_call>` or `<function=…>`,
  or prefix it with `[TOOL_CALLS]` / `<|python_tag|>`.
- Models with no tool support at all (codellama, gemma, …) are taught the tools
  in the system prompt instead (prompts.text_tool_protocol) and answer in one
  of the shapes above.

`StreamFilter` sits between the stream and the screen. Prose is shown as it
arrives; anything that could be the start of a call is held back until it can
be told apart, and calls come out as the same {"name", "arguments"} dicts that
native calls produce. Whatever turns out not to be a call is released
unchanged, so a JSON answer the user actually asked for still reaches them.

What counts as a call is deliberately narrow: an object that names a tool and
carries its arguments. Inside a code fence the name must also be one of our
tools, because a fenced example in an explanation is far likelier there. The
approval gate still stands in front of every write and command, whichever way
the call arrived.
"""

from __future__ import annotations

import ast
import json
import re

_NAME_KEYS = ("name", "tool", "tool_name", "function", "action")
_ARG_KEYS = ("arguments", "parameters", "args", "input", "params",
             "action_input", "tool_input")
_WRAPPER_KEYS = ("tool_calls", "function_calls", "calls")
# Keys a call object may carry besides its name and arguments.
_META_KEYS = {"id", "type", "index"}
# A whole reply that is nothing but {"response": "..."} is an answer in a JSON
# costume. Only names that are rarely a real payload on their own.
_REPLY_KEYS = ("response", "answer", "final_answer", "reply")

# Markers that start a call wherever they appear.
_TAGS = ("<tool_call>", "<|python_tag|>", "[TOOL_CALLS]", "<function=")
# Fence languages a call is written under. Anything else is ordinary code.
_CALL_LANGS = {"", "json", "json5", "jsonc", "tool_call", "tool_code", "tool",
               "tools", "function", "function_call", "functions"}
_CLOSE_FENCE = re.compile(r"^[ \t]*```[ \t]*$", re.M)
_MISSING = object()
# ast.literal_eval on model output is safe but not free; a call is never this big.
_LITERAL_LIMIT = 200_000


def tool_info(tools) -> dict:
    """{name: (all parameter names, required parameter names)} from a schema list."""
    out = {}
    for t in tools or []:
        fn = t.get("function", t)
        params = (fn.get("parameters") or {})
        out[fn["name"]] = (set((params.get("properties") or {})),
                           list(params.get("required") or []))
    return out


# --------------------------------------------------------------------------- #
# recognising a call in a finished piece of text
# --------------------------------------------------------------------------- #

def _load(text: str):
    text = text.strip()
    if not text:
        return _MISSING
    try:
        return json.loads(text)
    except ValueError:
        pass
    if len(text) <= _LITERAL_LIMIT:
        try:
            # {'name': 'read_file', ...} — a Python-dict habit some models have
            return ast.literal_eval(text)
        except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
            pass
    return _MISSING


def _one(obj, info: dict, strict: bool):
    """Normalise one call object, or None if it is not one.

    `strict` (inside a code fence) accepts only our own tool names. Outside a
    fence an unknown name in the canonical {"name", "arguments"} shape is still
    a call: small models misremember tool names, and the "unknown tool" error
    that comes back teaches them the right one, where printing the JSON would
    teach nobody anything.
    """
    if not isinstance(obj, dict):
        return None
    fn = obj.get("function")
    if isinstance(fn, dict) and "name" in fn:        # {"type": "function", "function": {...}}
        obj = fn
    name = next((obj[k] for k in _NAME_KEYS if isinstance(obj.get(k), str)), None)
    if not name or not name.strip():
        return None
    name = name.strip()
    if name.startswith("functions."):
        name = name[len("functions."):]
    known = name in info
    if strict and not known:
        return None

    present = [k for k in _ARG_KEYS if k in obj]
    if present:
        args = obj[present[0]]
        canonical = ("name" in obj and present[0] in ("arguments", "parameters")
                     and set(obj) <= {"name", present[0]} | _META_KEYS)
    else:
        # {"name": "read_file", "path": "x"} — arguments spilled into the top
        # level. Only believable when every other key is one of that tool's
        # parameters.
        rest = {k: v for k, v in obj.items() if k not in _NAME_KEYS and k not in _META_KEYS}
        if not known or not set(rest) <= info[name][0]:
            return None
        args, canonical = rest, False
    if not known and not canonical:
        return None

    if isinstance(args, str):
        parsed = _load(args)
        if isinstance(parsed, dict):
            args = parsed
        elif known and len(info[name][1]) == 1:
            args = {info[name][1][0]: args}          # {"action_input": "ls -la"}
        else:
            return None
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return None
    return {"name": name, "arguments": args}


def _from_value(obj, info: dict, strict: bool):
    if isinstance(obj, dict):
        for k in _WRAPPER_KEYS:
            if isinstance(obj.get(k), list) and set(obj) <= {k} | _META_KEYS:
                return _from_value(obj[k], info, strict)
        if isinstance(obj.get("function_call"), dict):
            obj = obj["function_call"]
        call = _one(obj, info, strict)
        return [call] if call else None
    if isinstance(obj, list) and obj:
        calls = [_one(o, info, strict) for o in obj]
        return calls if all(calls) else None
    return None


def _xml_calls(text: str):
    """`<function=read_file><parameter=path>x</parameter></function>` (qwen3-coder),
    `<function=read_file>{"path": "x"}</function>` (llama 3.1), and GLM's
    `read_file\\n<arg_key>path</arg_key><arg_value>x</arg_value>`."""
    calls = []
    for m in re.finditer(r"<function=([\w.\-]+)>(.*?)(?:</function>|\Z)", text, re.S):
        name, inner = m.group(1), m.group(2)
        if "<parameter=" in inner:
            args = {k: v.strip("\n") for k, v in re.findall(
                r"<parameter=([\w.\-]+)>(.*?)(?:</parameter>|(?=<parameter=)|\Z)", inner, re.S)}
        else:
            parsed = _load(inner)
            if parsed is _MISSING and not inner.strip():
                parsed = {}
            if not isinstance(parsed, dict):
                return None
            args = parsed
        calls.append({"name": name, "arguments": args})
    if calls:
        return calls
    if "<arg_key>" in text:
        name = text.split("<arg_key>", 1)[0].strip()
        if re.fullmatch(r"[\w.\-]+", name or ""):
            args = dict(re.findall(
                r"<arg_key>(.*?)</arg_key>\s*<arg_value>(.*?)</arg_value>", text, re.S))
            return [{"name": name, "arguments": args}]
    return None


def calls_in(text: str, info: dict, strict: bool = False):
    """The calls a block of text encodes, or None if it is not (only) calls."""
    obj = _load(text)
    if obj is not _MISSING:
        return _from_value(obj, info, strict)
    if "<tool_call>" in text:
        bodies = re.findall(r"<tool_call>(.*?)(?:</tool_call>|\Z)", text, re.S)
        calls = [c for b in bodies for c in (calls_in(b, info, strict) or [None])]
        return calls if calls and all(calls) else None
    if "<function=" in text or "<arg_key>" in text:
        return _xml_calls(text)
    # one JSON object per line
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) > 1:
        calls = []
        for ln in lines:
            got = calls_in(ln, info, strict)
            if not got:
                return None
            calls += got
        return calls
    return None


def reply_text(text: str):
    """The answer inside a reply that is only `{"response": "..."}`, else None."""
    obj = _load(text)
    if isinstance(obj, dict) and len(obj) == 1:
        (k, v), = obj.items()
        if k in _REPLY_KEYS and isinstance(v, str):
            return v
    return None


# --------------------------------------------------------------------------- #
# the streaming side
# --------------------------------------------------------------------------- #

class StreamFilter:
    """Feed content deltas in, get back the text that is safe to show now.

        f = StreamFilter(TOOLS)
        for delta in stream: show(f.feed(delta))
        show(f.finish());  f.calls  # -> [{"name", "arguments"}, ...]
    """

    def __init__(self, tools):
        self.info = tool_info(tools)
        self.calls: list[dict] = []
        self.shown = ""                 # everything released so far
        self._buf = ""
        self._pos = 0                   # _buf[:_pos] is decided
        self._line_start = True         # is _buf[_pos] at the start of a line?
        self._hold: dict | None = None  # what is being held back, if anything
        self._in_code = False           # inside a fence we let through
        self._after_call = False        # just dropped a call; its newline is next

    def feed(self, text: str) -> str:
        self._buf += text
        return self._advance(final=False)

    def finish(self) -> str:
        return self._advance(final=True)

    # -- driver ----------------------------------------------------------- #
    def _advance(self, final: bool) -> str:
        out = []
        while self._pos < len(self._buf):
            if self._after_call and not self._eat_newline(final):
                break
            if self._pos >= len(self._buf):
                break
            if self._hold is not None:
                got = self._resolve(final)
                if got is None:
                    break
                out.append(got)
            elif self._in_code:
                got = self._code_line(final)
                if got is None:
                    break
                out.append(got)
            else:
                text, waiting = self._text(final)
                out.append(text)
                if waiting:
                    break
        text = "".join(out)
        self.shown += text
        return text

    def _take(self, end: int) -> str:
        """Release _buf[_pos:end] as visible text."""
        text = self._buf[self._pos:end]
        if text:
            self._line_start = text.endswith("\n")
        self._pos = end
        return text

    def _consume(self, end: int):
        """Drop _buf[_pos:end] (it was a call). The rest of its line goes too
        if blank — see _eat_newline, since that newline may not be here yet."""
        self._pos = end
        self._line_start = True
        self._hold = None
        self._after_call = True

    def _eat_newline(self, final: bool) -> bool:
        """Swallow the blank remainder of a call's line. False = wait for more."""
        buf, i = self._buf, self._pos
        while i < len(buf) and buf[i] in " \t\r":
            i += 1
        if i >= len(buf) and not final:
            return False
        if i < len(buf) and buf[i] == "\n":
            self._pos = i + 1
        elif i >= len(buf):
            self._pos = i
        self._after_call = False
        return True

    # -- ordinary text ---------------------------------------------------- #
    def _text(self, final: bool):
        """Release text up to the next possible call. Returns (text, waiting)."""
        buf, pos = self._buf, self._pos
        cut, hold = len(buf), None

        for tag in _TAGS:
            i = buf.find(tag, pos)
            if 0 <= i < cut:
                cut, hold = i, {"kind": "tag", "tag": tag, "start": i}

        # a call written on its own line
        i = pos if self._line_start else buf.find("\n", pos) + 1 or -1
        while 0 <= i < cut:
            j = i
            while j < len(buf) and buf[j] in " \t":
                j += 1
            rest = buf[j:j + 12]
            if j >= len(buf) or (not final and (
                    "```".startswith(rest) or "[TOOL_CALLS]".startswith(rest))):
                if not final:          # can't tell yet what this line is
                    cut, hold = i, None
                break
            if any(buf.startswith(t, j) for t in _TAGS):
                pass                   # the tag search above has it
            elif buf[j] in "{[":
                cut, hold = i, {"kind": "json", "start": i, "open": j}
                break
            elif buf.startswith("```", j):
                cut, hold = i, {"kind": "fence", "start": i, "open": j}
                break
            nl = buf.find("\n", j)
            i = nl + 1 if nl >= 0 else -1

        # a tag that may be arriving in pieces: "...<tool_c"
        if not final and hold is None and cut == len(buf):
            base = max(pos, cut - 14)
            tail = buf[base:cut]
            for tag in _TAGS:
                for k in range(min(len(tag) - 1, len(tail)), 0, -1):
                    if tail.endswith(tag[:k]):
                        cut = min(cut, base + len(tail) - k)
                        break

        text = self._take(cut)
        if hold is not None:
            self._hold = hold
            return text, False
        return text, self._pos < len(buf)

    def _code_line(self, final: bool):
        """Inside a fence that is plainly code: pass it through line by line."""
        nl = self._buf.find("\n", self._pos)
        if nl < 0:
            return self._take(len(self._buf)) if final else None
        line = self._buf[self._pos:nl]
        if _CLOSE_FENCE.fullmatch(line):
            self._in_code = False
        return self._take(nl + 1)

    # -- held text -------------------------------------------------------- #
    def _resolve(self, final: bool):
        """Decide a held span. Returns released text, or None to wait."""
        h = self._hold
        kind = h["kind"]
        if kind == "json":
            return self._resolve_json(h, final)
        if kind == "fence":
            return self._resolve_fence(h, final)
        return self._resolve_tag(h, final)

    def _release(self, end: int) -> str:
        self._hold = None
        return self._take(end)

    def _brackets(self, h: dict, start: int):
        """Index of the bracket that closes the value opening at `start`, or None.

        Incremental: the scan resumes where the last delta left off.
        """
        st = h.setdefault("scan", {"i": start, "depth": 0, "quote": None, "esc": False})
        buf, i = self._buf, st["i"]
        depth, quote, esc = st["depth"], st["quote"], st["esc"]
        while i < len(buf):
            c = buf[i]
            if quote:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == quote:
                    quote = None
            elif c in "\"'":
                quote = c
            elif c in "{[":
                depth += 1
            elif c in "}]":
                depth -= 1
                if depth == 0:
                    st.update(i=i + 1, depth=0, quote=None, esc=False)
                    return i
            i += 1
        st.update(i=i, depth=depth, quote=quote, esc=esc)
        return None

    def _resolve_json(self, h: dict, final: bool):
        buf, o = self._buf, h["open"]
        if not h.get("plausible"):
            k = o + 1
            while k < len(buf) and buf[k] in " \t\r\n":
                k += 1
            if k >= len(buf):
                return self._release(len(buf)) if final else None
            # {"…  or  [{…  — anything else is prose that happens to start
            # with a bracket ("[1] see below", "{x | x > 0}") and goes now.
            if not ((buf[o] == "{" and buf[k] in "\"'") or (buf[o] == "[" and buf[k] == "{")):
                return self._release(o + 1)
            h["plausible"] = True
        end = self._brackets(h, o)
        if end is None:
            if not final:
                return None
            calls = calls_in(buf[o:], self.info)
            if calls:
                self.calls += calls
                self._consume(len(buf))
                return ""
            return self._release(len(buf))
        value = buf[o:end + 1]
        calls = calls_in(value, self.info)
        if calls:
            self.calls += calls
            self._consume(end + 1)
            return ""
        if not self.shown.strip():
            reply = reply_text(value)
            if reply is not None:
                self._consume(end + 1)
                return reply
        return self._release(end + 1)

    def _resolve_fence(self, h: dict, final: bool):
        buf, f = self._buf, h["open"]
        nl = buf.find("\n", f)
        if nl < 0:
            return self._release(len(buf)) if final else None
        if not h.get("plausible"):
            lang = buf[f + 3:nl].strip().lower()
            k = nl + 1
            while k < len(buf) and buf[k] in " \t\r\n":
                k += 1
            if lang in ("", "json", "json5", "jsonc"):
                if k >= len(buf):
                    if not final:
                        return None
                elif buf[k] not in "{[":
                    lang = "code"
            if lang not in _CALL_LANGS:
                self._in_code = True
                return self._release(nl + 1)
            h["plausible"] = True
        m = _CLOSE_FENCE.search(buf, nl + 1)
        # "```" at the very end may still become "```json" — a closer has to
        # finish its line before it counts.
        if not final and (m is None or m.end() == len(buf)):
            return None
        body = buf[nl + 1:m.start() if m else len(buf)]
        end = len(buf)
        if m:
            end = m.end() + (1 if buf[m.end():m.end() + 1] == "\n" else 0)
        calls = calls_in(body, self.info, strict=True)
        if calls:
            self.calls += calls
            self._consume(end)
            return ""
        return self._release(end)

    def _resolve_tag(self, h: dict, final: bool):
        buf, s, tag = self._buf, h["start"], h["tag"]
        body_at = s + len(tag)
        if tag in ("<tool_call>", "<function="):
            close = "</tool_call>" if tag == "<tool_call>" else "</function>"
            c = buf.find(close, body_at)
            if c < 0:
                if not final:
                    return None
                end = len(buf)
            else:
                end = c + len(close)
            span = buf[s:end]
            calls = calls_in(span, self.info)
        else:
            # [TOOL_CALLS] / <|python_tag|> — a JSON value follows, or mistral's
            # newer `[TOOL_CALLS]name[ARGS]{...}`.
            k = body_at
            while k < len(buf) and buf[k] in " \t\r\n":
                k += 1
            name = None
            if k < len(buf) and buf[k] not in "{[":
                a = buf.find("[ARGS]", k)
                if a < 0:
                    if not final:
                        return None
                    return self._release(len(buf))
                name, k = buf[k:a].strip(), a + len("[ARGS]")
            if k >= len(buf):
                return self._release(len(buf)) if final else None
            end = self._brackets(h, k)
            if end is None:
                if not final:
                    return None
                end = len(buf) - 1
            end += 1
            value = buf[k:end]
            if name:
                args = _load(value)
                calls = [{"name": name, "arguments": args}] if isinstance(args, dict) else None
            else:
                calls = calls_in(value, self.info)
        if calls:
            self.calls += calls
            self._consume(end)
            return ""
        return self._release(end)
