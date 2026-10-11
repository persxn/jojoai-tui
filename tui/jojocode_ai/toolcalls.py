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
- Reasoning models whose template cannot separate thinking (old deepseek-r1
  pulls, many hf.co GGUFs) write it inline as <think>…</think>. That text is
  reasoning, not an answer: it goes out on the thinking channel, and a call
  the model merely *considered* inside it is never run.

`StreamFilter` sits between the stream and the screen. Prose is shown as it
arrives; anything that could be the start of a call is held back until it can
be told apart, and calls come out as the same {"name", "arguments"} dicts that
native calls produce. Whatever turns out not to be a call is released
unchanged, so a JSON answer the user actually asked for still reaches them.

What counts as a call is deliberately narrow: an object that names a tool and
carries its arguments and nothing else — a tool *definition* with a
description, or a log line with a timestamp, is not one. Inside a code fence
the name must also be one of our tools, because a fenced example in an
explanation is far likelier there. The approval gate still stands in front of
every write and command, whichever way the call arrived.
"""

from __future__ import annotations

import ast
import json
import re
import warnings

_NAME_KEYS = ("name", "tool", "tool_name", "function", "action")
_ARG_KEYS = ("arguments", "parameters", "args", "input", "params",
             "action_input", "tool_input")
_WRAPPER_KEYS = ("tool_calls", "function_calls", "calls")
# Keys a call object may carry besides its name and arguments.
_META_KEYS = {"id", "type", "index", "call_id"}
# A whole reply that is nothing but {"response": "..."} is an answer in a JSON
# costume. Only names that are rarely a real payload on their own.
_REPLY_KEYS = ("response", "answer", "final_answer", "reply")

# Markers that start a call wherever they appear.
_TAGS = ("<tool_call>", "<|python_tag|>", "[TOOL_CALLS]", "<function=")
_THINK = ("<think>", "<thinking>")
_BARE_CLOSE = ("</think>", "</thinking>")
_RESULT = "<tool_result"
_MARKERS = _TAGS + _THINK + _BARE_CLOSE + (_RESULT,)
# Fence languages a call is written under. Anything else is ordinary code.
_CALL_LANGS = {"", "json", "json5", "jsonc", "tool_call", "tool_code", "tool",
               "tools", "function", "function_call", "functions"}
_CLOSE_FENCE = re.compile(r"^[ \t]*```[ \t]*$", re.M)
_MISSING = object()
# ast.literal_eval on model output is safe but not free; a call is never this big.
_LITERAL_LIMIT = 200_000
# A JSON string literal whose raw text starts like a Windows path with single
# backslashes: "C:\Users\me" — invalid JSON (\U), or silently wrong ("\t").
_STRING = re.compile(r'"((?:[^"\\]|\\.)*)"', re.S)
_RAW_WINPATH_DECODED = re.compile(r"^[A-Za-z]:")
_BAD_ESCAPE = re.compile(r'\\(?!["\\/bfnrtu])')


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

def _repair_backslashes(text: str) -> str:
    r"""Double the lone backslashes of string literals that hold escapes JSON
    does not have (C:\Users is not JSON). Only those: a string that already
    decodes is left alone — "x:\n  y: 1" is YAML, not a path, and doubling its
    backslashes wrote a literal backslash-n into the file. A path whose
    backslashes happen to be valid escapes (C:\temp\new) is fixed after
    parsing, by _fix_paths, where the key says it is a path."""
    def fix(m):
        body = m.group(1)
        if _BAD_ESCAPE.search(body):
            body = re.sub(r'\\(?![\\"])', r"\\\\", body)
        return f'"{body}"'
    return _STRING.sub(fix, text)


_PATH_KEYS = frozenset({"path", "cwd", "file", "file_path", "filename", "dir", "directory"})
_CTRL_ESCAPE = {"\n": "\\n", "\t": "\\t", "\r": "\\r", "\b": "\\b", "\f": "\\f"}


def _fix_paths(args: dict) -> dict:
    r""""C:\temp\new.txt" written raw is valid JSON that decodes to a tab and a
    newline. No path holds a control character, so in a path argument that
    starts like a Windows path they are put back as the backslashes they were."""
    out = None
    for k, v in args.items():
        if k in _PATH_KEYS and isinstance(v, str) and (_RAW_WINPATH_DECODED.match(v)
                                                       or v.startswith("\\")) \
                and any(c in v for c in _CTRL_ESCAPE):
            out = out if out is not None else dict(args)
            out[k] = "".join(_CTRL_ESCAPE.get(c, c) for c in v)
    return args if out is None else out


def _json_safe(obj):
    """literal_eval can return bytes, sets, complex — none of which survive the
    next request's json.dumps. Such a value is not a call we can keep."""
    try:
        return json.loads(json.dumps(obj))
    except (TypeError, ValueError):
        return _MISSING


def _load(text: str):
    text = text.strip()
    if not text:
        return _MISSING
    try:
        return json.loads(text)          # valid JSON means what it says
    except ValueError:
        pass
    if "\\" in text:
        fixed = _repair_backslashes(text)
        if fixed != text:
            try:
                return json.loads(fixed)
            except ValueError:
                pass
    if len(text) <= _LITERAL_LIMIT and text[:1] in "{[":
        with warnings.catch_warnings():
            # Python 3.12 warns on invalid escapes; on Windows that warning was
            # printed straight over the curses screen.
            warnings.simplefilter("ignore")
            try:
                # {'name': 'read_file', ...} — a Python-dict habit some models have
                return _json_safe(ast.literal_eval(text))
            except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
                pass
    return _MISSING


def _values(text: str):
    """Two or more JSON values back to back (pretty-printed calls in one fence)."""
    for candidate in (text, _repair_backslashes(text) if "\\" in text else None):
        if candidate is None:
            break
        out = _split_values(candidate)
        if out is not None:
            return out
    return None


def _split_values(text: str):
    dec, i, out = json.JSONDecoder(), 0, []
    while True:
        while i < len(text) and text[i] in " \t\r\n,":
            i += 1
        if i >= len(text):
            break
        try:
            value, i = dec.raw_decode(text, i)
        except ValueError:
            return None
        out.append(value)
    return out if len(out) > 1 else None


def _looks_like_schema(args: dict) -> bool:
    return args.get("type") == "object" and isinstance(args.get("properties"), dict)


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
        if set(obj) - {"function"} - _META_KEYS:
            return None
        obj = fn
    name_key = next((k for k in _NAME_KEYS if isinstance(obj.get(k), str)), None)
    if name_key is None or not obj[name_key].strip():
        return None
    name = obj[name_key].strip()
    if name.startswith("functions."):
        name = name[len("functions."):]
    known = name in info
    if strict and not known:
        return None

    present = [k for k in _ARG_KEYS if k in obj]
    if present:
        args_key = present[0]
        # Exactly a name and its arguments: a tool *definition* carries a
        # description, a log line a timestamp — neither is a call.
        if set(obj) - {name_key, args_key} - _META_KEYS:
            return None
        args = obj[args_key]
        canonical = name_key == "name" and args_key in ("arguments", "parameters")
    else:
        # {"name": "read_file", "path": "x"} — arguments spilled into the top
        # level. Only believable when every other key is one of that tool's
        # parameters.
        rest = {k: v for k, v in obj.items() if k != name_key and k not in _META_KEYS}
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
    if not isinstance(args, dict) or _looks_like_schema(args):
        return None
    return {"name": name, "arguments": _fix_paths(args)}


def _from_value(obj, info: dict, strict: bool):
    if isinstance(obj, dict):
        for k in _WRAPPER_KEYS:
            if isinstance(obj.get(k), list) and set(obj) <= {k} | _META_KEYS:
                return _from_value(obj[k], info, strict)
        if isinstance(obj.get("function_call"), dict) and len(obj) == 1:
            obj = obj["function_call"]
        call = _one(obj, info, strict)
        return [call] if call else None
    if isinstance(obj, list) and obj:
        calls = [_one(o, info, strict) for o in obj]
        return calls if all(calls) else None
    return None


def _trim_one_newline(v: str) -> str:
    v = v[1:] if v.startswith("\n") else v
    return v[:-1] if v.endswith("\n") else v


def _xml_calls(text: str):
    """`<function=read_file><parameter=path>x</parameter></function>` (qwen3-coder),
    `<function=read_file>{"path": "x"}</function>` (llama 3.1), and GLM's
    `read_file\\n<arg_key>path</arg_key><arg_value>x</arg_value>`."""
    calls = []
    for m in re.finditer(r"<function=([\w.\-]+)>(.*?)(?:</function>|\Z)", text, re.S):
        name, inner = m.group(1), m.group(2)
        if "<parameter=" in inner:
            # one newline each side, as the template writes them (Ollama's
            # qwen3coder parser does the same); strip("\n") ate a file's last newline
            args = {k: _trim_one_newline(v) for k, v in re.findall(
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
    values = _values(text)
    if values is not None:
        calls = []
        for v in values:
            got = _from_value(v, info, strict)
            if not got:
                return None
            calls += got
        return calls
    # one Python-literal object per line
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
        for delta in stream:
            show(f.feed(delta)); think(f.take_thinking())
        show(f.finish());  f.calls  # -> [{"name", "arguments"}, ...]

    `answer` is the reply minus inline reasoning (what belongs in history).
    """

    def __init__(self, tools):
        self.info = tool_info(tools)
        self.calls: list[dict] = []
        self.shown = ""                 # everything released so far
        self.answer_from = 0            # shown[answer_from:] is the answer proper
        self._thinking: list[str] = []
        self._buf = ""
        self._pos = 0                   # _buf[:_pos] is decided
        self._line_start = True         # is _buf[_pos] at the start of a line?
        self._hold: dict | None = None  # what is being held back, if anything
        self._in_code = False           # inside a fence we let through
        self._after_call = False        # just dropped a call; its newline is next
        self._stopped = False           # past an invented <tool_result>: drop the rest
        self._tags_are_text = False     # a <think> was shown as text: tags are being discussed

    @property
    def answer(self) -> str:
        return self.shown[self.answer_from:]

    def feed(self, text: str) -> str:
        # Through a local, so CPython can grow the string in place; `+=` on
        # an attribute copies the whole buffer on every delta.
        buf, self._buf = self._buf, ""
        buf += text
        self._buf = buf
        return self._advance(final=False)

    def finish(self, cut: bool = False) -> str:
        """The rest of the reply. `cut`: it was stopped by the length limit, so
        a call still being held is half a JSON object — dropped, not shown."""
        if cut and self._hold is not None:
            self._buf = self._buf[:self._hold["start"]]
            self._hold = None
        return self._advance(final=True)

    def take_thinking(self) -> str:
        """Inline reasoning found since the last call."""
        text, self._thinking = "".join(self._thinking), []
        return text

    # -- driver ----------------------------------------------------------- #
    def _advance(self, final: bool) -> str:
        before = len(self.shown)
        last, stalled = self._pos, 0
        while self._pos < len(self._buf) and not self._stopped:
            if self._after_call and not self._eat_newline(final):
                break
            if self._pos >= len(self._buf):
                break
            if self._hold is not None:
                if not self._resolve(final):
                    break
            elif self._in_code:
                if not self._code_line(final):
                    break
            elif self._text(final):
                break
            stalled = stalled + 1 if self._pos == last else 0
            last = self._pos
            if stalled > 16:
                # Steps that keep going round without consuming anything would
                # repeat forever, freezing the turn where Ctrl-C cannot reach.
                # Never expected; if a rule regresses, show the text and move on.
                self._hold = None
                self._take(self._pos + 1)
                stalled = 0
        if self._stopped:
            self._pos = len(self._buf)
        if self._hold is None and self._pos > 8192:
            # Every hold stores absolute offsets, so only trim between holds.
            # Without this the buffer grew for the whole reply and every
            # delta copied all of it.
            self._buf = self._buf[self._pos:]
            self._pos = 0
        return self.shown[before:]

    def _take(self, end: int) -> None:
        """Release _buf[_pos:end] as visible text."""
        text = self._buf[self._pos:end]
        if text:
            self._line_start = text.endswith("\n")
            self.shown += text
        self._pos = end

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
    def _text(self, final: bool) -> bool:
        """Release text up to the next possible call. Returns True to wait."""
        buf, pos = self._buf, self._pos
        cut, hold = len(buf), None

        for tag in _MARKERS:
            i = buf.find(tag, pos, cut + len(tag))
            if 0 <= i < cut:
                cut, hold = i, {"kind": "tag", "tag": tag, "start": i}

        # a call written on its own line
        i = pos if self._line_start else buf.find("\n", pos, cut) + 1 or -1
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
            if any(buf.startswith(t, j) for t in _MARKERS):
                pass                   # the marker search above has it
            elif buf[j] in "{[":
                cut, hold = i, {"kind": "json", "start": i, "open": j}
                break
            elif buf.startswith("```", j):
                cut, hold = i, {"kind": "fence", "start": i, "open": j}
                break
            nl = buf.find("\n", j, cut)
            i = nl + 1 if nl >= 0 else -1

        # a marker that may be arriving in pieces: "...<tool_c"
        if not final and hold is None and cut == len(buf):
            base = max(pos, cut - 16)
            tail = buf[base:cut]
            for tag in _MARKERS:
                for k in range(min(len(tag) - 1, len(tail)), 0, -1):
                    if tail.endswith(tag[:k]):
                        cut = min(cut, base + len(tail) - k)
                        break

        self._take(cut)
        if hold is not None:
            self._hold = hold
            return False
        return self._pos < len(buf)

    def _code_line(self, final: bool) -> bool:
        """Inside a fence that is plainly code: pass it through line by line."""
        nl = self._buf.find("\n", self._pos)
        if nl < 0:
            if final:
                self._take(len(self._buf))
                return True
            return False
        if _CLOSE_FENCE.fullmatch(self._buf[self._pos:nl]):
            self._in_code = False
        self._take(nl + 1)
        return True

    # -- held text -------------------------------------------------------- #
    def _resolve(self, final: bool) -> bool:
        """Work on a held span. Returns False to wait for more input."""
        h = self._hold
        kind = h["kind"]
        if kind == "json":
            return self._resolve_json(h, final)
        if kind == "fence":
            return self._resolve_fence(h, final)
        tag = h["tag"]
        if tag in _THINK:
            if self.shown.strip() or self.calls:
                # Reasoning opens a reply; a <think> in the middle of an answer
                # is the answer talking *about* the tag.
                self._tags_are_text = True
                return self._release(h["start"] + len(tag))
            return self._resolve_think(h, final)
        if tag in _BARE_CLOSE and self._tags_are_text:
            # "...wrap thoughts in <think>...</think> tags": the close belongs
            # to an open the answer already showed as text. Treating it as the
            # end of hidden reasoning cut the answer and dropped its calls.
            return self._release(h["start"] + len(tag))
        if tag in _BARE_CLOSE:
            # Reasoning that began in the prompt (the template opened <think>
            # for the model): everything so far was thought, not answer, and
            # any call found in it was only considered.
            self.calls = []
            self.answer_from = len(self.shown)
            self._consume(h["start"] + len(tag))
            return True
        if tag == _RESULT:
            if self.calls:
                # Text mode: the model is inventing the tool's output. The
                # real one is coming; nothing after this is worth showing.
                self._stopped = True
                self._hold = None
                return True
            return self._release(h["start"] + len(tag))
        return self._resolve_tag(h, final)

    def _release(self, end: int) -> bool:
        self._hold = None
        self._take(end)
        return True

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

    def _resolve_json(self, h: dict, final: bool) -> bool:
        buf, o = self._buf, h["open"]
        if not h.get("plausible"):
            k = o + 1
            while k < len(buf) and buf[k] in " \t\r\n":
                k += 1
            if k >= len(buf):
                return self._release(len(buf)) if final else False
            # {"…  or  [{…  — anything else is prose that happens to start
            # with a bracket ("[1] see below", "{x | x > 0}") and goes now.
            if not ((buf[o] == "{" and buf[k] in "\"'") or (buf[o] == "[" and buf[k] == "{")):
                return self._release(o + 1)
            h["plausible"] = True
        end = self._brackets(h, o)
        if end is None:
            if not final:
                return False
            calls = calls_in(buf[o:], self.info)
            if calls:
                self.calls += calls
                self._consume(len(buf))
                return True
            return self._release(len(buf))
        value = buf[o:end + 1]
        calls = calls_in(value, self.info)
        if calls:
            self.calls += calls
            self._consume(end + 1)
            return True
        if not self.shown.strip():
            reply = reply_text(value)
            if reply is not None:
                self._consume(end + 1)
                self.shown += reply
                return True
        return self._release(end + 1)

    def _resolve_fence(self, h: dict, final: bool) -> bool:
        buf, f = self._buf, h["open"]
        nl = buf.find("\n", f)
        if nl < 0:
            return self._release(len(buf)) if final else False
        if not h.get("plausible"):
            lang = buf[f + 3:nl].strip().lower()
            k = nl + 1
            while k < len(buf) and buf[k] in " \t\r\n":
                k += 1
            if lang in ("", "json", "json5", "jsonc"):
                if k >= len(buf):
                    if not final:
                        return False
                elif buf[k] not in "{[":
                    lang = "code"
            if lang not in _CALL_LANGS:
                self._in_code = True
                return self._release(nl + 1)
            h["plausible"] = True
        close = self._closing_fence(h, nl + 1, final)
        if close is False:
            return False
        body = buf[nl + 1:close[0] if close else len(buf)]
        end = close[1] if close else len(buf)
        calls = calls_in(body, self.info, strict=True)
        if calls:
            self.calls += calls
            self._consume(end)
            return True
        return self._release(end)

    def _closing_fence(self, h: dict, start: int, final: bool):
        """(start, end) of the line that closes the fence; None if the reply
        ended without one; False to wait.

        Incremental: looks for "```" only in text it has not seen, so a call
        written as one 200 KB line is scanned once, not once per delta.
        """
        buf = self._buf
        k = h.get("fscan", start)
        while True:
            k = buf.find("```", k)
            if k < 0:
                h["fscan"] = max(start, len(buf) - 2)
                return None if final else False
            ls = buf.rfind("\n", 0, k) + 1
            le = buf.find("\n", k)
            if ls >= start and not buf[ls:k].strip(" \t"):
                if le < 0 and not final:
                    h["fscan"] = k          # "```" may still become "```json"
                    return False
                tail = buf[k + 3:le if le >= 0 else len(buf)]
                if not tail.strip(" \t\r"):
                    return ls, (le + 1 if le >= 0 else len(buf))
            k += 3

    def _resolve_think(self, h: dict, final: bool) -> bool:
        """Stream reasoning out on the thinking channel as it arrives."""
        buf, tag = self._buf, h["tag"]
        close = "</" + tag[1:]
        body = h.setdefault("body", h["start"] + len(tag))
        c = buf.find(close, body)
        if c < 0:
            # keep back what could be the start of the closing tag
            safe = len(buf) if final else max(body, len(buf) - len(close) + 1)
            if safe > body:
                self._thinking.append(buf[body:safe])
                h["body"] = safe
            if final:
                self._consume(len(buf))
                return True
            return False
        if c > body:
            self._thinking.append(buf[body:c])
        self._consume(c + len(close))
        return True

    def _resolve_tag(self, h: dict, final: bool) -> bool:
        buf, s, tag = self._buf, h["start"], h["tag"]
        body_at = s + len(tag)
        if tag in ("<tool_call>", "<function="):
            close = "</tool_call>" if tag == "<tool_call>" else "</function>"
            c = buf.find(close, h.get("seen", body_at))
            if c < 0:
                if not final:
                    h["seen"] = max(body_at, len(buf) - len(close) + 1)
                    return False
                end = len(buf)
            else:
                end = c + len(close)
            calls = calls_in(buf[s:end], self.info)
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
                    return self._release(len(buf)) if final else False
                name, k = buf[k:a].strip(), a + len("[ARGS]")
            if k >= len(buf):
                return self._release(len(buf)) if final else False
            end = self._brackets(h, k)
            if end is None:
                if not final:
                    return False
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
            return True
        return self._release(end)
