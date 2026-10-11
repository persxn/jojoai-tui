"""The JojoAI agent loop: model <-> tools until the task is done.

── Models other than gpt-oss ───────────────────────────────────────────────

Tool calls reach this loop two ways. Models with native tool calling return
them in the `tool_calls` field; many also, or only, write them into their
reply as text, which `toolcalls.StreamFilter` lifts out before the user sees
it (see that file for the shapes and why). Models with no tool support at all
run in *text mode*: the tools are described in the system prompt, results go
back as ordinary user turns — their chat templates have no slot for a `tool`
message and would silently drop it — and the same filter reads the calls.

── What the user controls, and what the model does not ─────────────────────

`root` is the project the user chose (where jojo started, --cwd, /cwd). The
model moves `ctx.cwd` with change_dir, but it never moves `root`: auto-edit
pre-approval is measured from root (policy.py), so change_dir cannot carry
the approval boundary somewhere else. Ctrl-C (`interrupt`) is honoured
everywhere a turn can wait: while the model loads or streams, between the
calls of a batch, and inside a running command.

── Fitting the context window ───────────────────────────────────────────────

Ollama silently drops the oldest messages once the prompt outgrows num_ctx —
which, a few large tool results into a task, was the user's request itself.
Tool results are clipped to a share of the window, and when the history
nears it the oldest tool outputs are elided first; the system prompt and the
user's messages are never touched.
"""

from __future__ import annotations

import json
import time

from . import policy, shell
from .backends import make_client
from .config import Config
from .prompts import system_prompt, text_tool_protocol
from .toolcalls import StreamFilter
from .tools import APPROVAL_REQUIRED, TOOLS, Ctx, _resolve, run_tool

CHARS_PER_TOKEN = 3.5      # conservative for code; English prose runs nearer 4
NUDGE = ("(Your last reply was empty. Either call a tool or give your answer "
         "to the user now.)")


def _fit(text: str, limit: int) -> str:
    """Head and tail of a tool result, so neither the command line nor the
    error at the end is lost."""
    if len(text) <= limit:
        return text
    head = int(limit * 0.7)
    tail = max(0, limit - head - 80)
    return (text[:head] + f"\n... [{len(text) - head - tail} chars clipped to fit "
            f"the context window] ...\n" + (text[-tail:] if tail else ""))


_SCHEMA_CHARS = len(json.dumps(TOOLS))


class Agent:
    """Drives one conversation. Emits (kind, *payload) events for a UI to render.

    Events:
      ("user", text)
      ("thinking", delta)
      ("assistant", delta)
      ("tool_start", name, args_dict)
      ("tool_end", name, result_str)
      ("tool_denied", name)
      ("info", text)
      ("final", text, already_streamed)
      ("error", text)
      ("stats", dict)
      ("turn_end",)
    """

    def __init__(self, cfg: Config, approver=None):
        self.cfg = cfg
        self.client = make_client(cfg)
        self.root = cfg.cwd                  # the user's project; see the header
        self.ctx = Ctx(cwd=cfg.cwd)
        self.ctx.cancel = lambda: self._interrupt
        # approver(name, args) -> bool ; default: allow everything
        self.approver = approver or (lambda n, a: True)
        self.store = None          # optional jojoai.store.Store
        self.messages = [self._system()]
        self._interrupt = False
        self._told: set[str] = set()

    # -- control from the UI -------------------------------------------------- #
    def reset(self):
        self.ctx.cwd = self.root             # change_dir does not outlive the chat
        self.messages = [self._system()]

    def set_cwd(self, path: str):
        """/cwd: the user moves the project. Unlike change_dir, this moves root."""
        self.cfg.cwd = self.root = self.ctx.cwd = path

    def interrupt(self):
        self._interrupt = True

    def close(self):
        self.interrupt()
        shell.kill_all()          # quitting must not leave a command running

    # -- policy --------------------------------------------------------------- #
    def _pre_approved(self, name: str, args: dict) -> bool:
        """Skip the approval prompt for trusted operations."""
        if name == "run_bash":
            return self.cfg.auto_bash
        if not (self.cfg.auto_edit and name in ("write_file", "edit_file")):
            return False
        path = args.get("path")
        if not isinstance(path, str) or not path:
            return False
        # exactly the path the tool will write — resolved the way it resolves it
        return policy.auto_write_ok(_resolve(self.ctx, path), self.root)

    def _note(self, key: str, text: str):
        """An info event, once per session per key."""
        if key in self._told:
            return None
        self._told.add(key)
        return ("info", text)

    # -- what goes over the wire ------------------------------------------ #
    def _system(self) -> dict:
        return {"role": "system", "content": system_prompt(self.ctx.cwd, self.cfg)}

    def _text_mode(self) -> bool:
        return self.client.capabilities().get("tools") is False

    def _budget_chars(self) -> int:
        return int(self.client.context_window() * CHARS_PER_TOKEN)

    def _fit_history(self, msgs: list[dict]) -> bool:
        """Elide the oldest tool outputs until the history fits ~75% of the
        window (the rest is for the reply). True if anything was elided."""
        budget = int(self._budget_chars() * 0.75)
        # Everything that is sent: gpt-oss's reasoning rides along on every
        # tool step, and the tool schema on every request. Counting only the
        # content let a long loop overflow num_ctx — and Ollama then drops the
        # oldest message, which is the user's own request.
        size = _SCHEMA_CHARS + sum(len(m.get("content") or "") + len(m.get("thinking") or "")
                                   + len(json.dumps(m.get("tool_calls", ""))) for m in msgs)
        if size <= budget:
            return False
        changed = False
        thought = [i for i, m in enumerate(msgs) if m.get("thinking")]
        for i in thought[:-1]:                       # old reasoning goes first
            if size <= budget:
                break
            size -= len(msgs[i]["thinking"])
            msgs[i] = {k: v for k, v in msgs[i].items() if k != "thinking"}
            changed = True
        tools_at = [i for i, m in enumerate(msgs) if m.get("role") == "tool"]
        for i in tools_at[:-2]:                      # keep the two newest whole
            if size <= budget:
                break
            body = msgs[i].get("content") or ""
            if len(body) > 200 and not body.startswith("[elided"):
                note = f"[elided: {len(body)} chars of earlier tool output, to fit the context window]"
                size -= len(body) - len(note)
                msgs[i] = {**msgs[i], "content": note}
                changed = True
        return changed

    def _wire(self, text_mode: bool, msgs: list[dict] | None = None) -> list[dict]:
        msgs = self.messages if msgs is None else msgs
        # The prompt names the current directory and the approval rules as
        # they are right now; it used to say whatever was true at startup.
        system = self._system()
        if msgs and msgs[0].get("role") == "system" and msgs[0].get("content") != system["content"]:
            msgs[0] = system
        if not text_mode:
            return msgs
        out: list[dict] = []
        for m in msgs:
            role = m["role"]
            if role == "system":
                m = {"role": "system",
                     "content": m["content"] + "\n\n" + text_tool_protocol(TOOLS)}
            elif role == "assistant" and m.get("tool_calls"):
                calls = "\n".join(
                    "<tool_call>" + json.dumps({"name": c["function"]["name"],
                                                "arguments": c["function"]["arguments"]})
                    + "</tool_call>" for c in m["tool_calls"])
                text = (m.get("content") or "").strip()
                m = {"role": "assistant", "content": f"{text}\n{calls}" if text else calls}
            elif role == "tool":
                m = {"role": "user", "content": f'<tool_result name="{m.get("tool_name", "")}">\n'
                                                f'{m["content"]}\n</tool_result>'}
            elif role == "assistant":
                m = {"role": "assistant", "content": m.get("content") or ""}
            if out and m["role"] == "user" and out[-1]["role"] == "user":
                # several results in a row read as one turn
                out[-1] = {"role": "user", "content": out[-1]["content"] + "\n\n" + m["content"]}
            else:
                out.append(m)
        return out

    @staticmethod
    def _pair_calls(msgs: list[dict]) -> None:
        """Every tool call answered: a turn cut off mid-batch (Ctrl-C in
        headless mode raises straight through) left calls without results,
        which strict chat templates refuse on every later request."""
        for i in range(len(msgs) - 1, -1, -1):
            m = msgs[i]
            if m.get("role") == "assistant" and m.get("tool_calls"):
                answered = sum(1 for x in msgs[i + 1:] if x.get("role") == "tool")
                for c in m["tool_calls"][answered:]:
                    msgs.append({"role": "tool", "tool_name": c["function"]["name"],
                                 "content": "error: interrupted before it ran"})
                return
            if m.get("role") == "user":
                return

    # -- the turn ------------------------------------------------------------- #
    def run(self, user_text: str):
        msgs = self.messages
        try:
            yield from self._run(user_text, msgs)
        finally:
            self._pair_calls(msgs)

    def _run(self, user_text: str, msgs: list[dict]):
        # msgs: a /reset mid-turn rebinds self.messages; this turn keeps its own
        self._interrupt = False
        sent = user_text
        if self.store is not None:
            self.store.set_title(user_text)
            # cross-chat context is pulled in ONLY when recall is explicitly on
            if getattr(self.store, "rag_enabled", False) and len(user_text.strip()) >= 12:
                try:
                    ctx = self.store.context_block(user_text)
                except Exception:  # noqa: BLE001
                    ctx = ""
                if ctx:
                    n = ctx.count("\n[")
                    yield ("recall", max(1, n))
                    sent = f"{ctx}\n\n---\n\n{user_text}"
            self.store.append("user", user_text)
        for m in msgs:
            m.pop("thinking", None)          # earlier turns' reasoning is spent
        if msgs and msgs[-1].get("role") == "user":
            # The last turn got no answer (an error, an empty reply, Ctrl-C
            # before any text). Two user turns in a row is a template error on
            # strict servers ("roles must alternate") for every later request;
            # /retry of the same words replaces it, anything else joins it.
            prev = msgs.pop().get("content") or ""
            if prev != sent:
                sent = f"{prev}\n\n{sent}"
        msgs.append({"role": "user", "content": sent})
        yield ("user", user_text)

        nudged = False
        for step in range(self.cfg.max_steps):
            if self._interrupt:
                yield ("error", "interrupted")
                break
            if self._fit_history(msgs):
                note = self._note("fit", "the conversation outgrew the model's context "
                                         "window; older tool output is being trimmed")
                if note:
                    yield note

            assistant_text = answer = thinking = ""
            tool_calls = []
            got_error = False
            attempt = 0
            stats = {}
            first_error = ""

            while True:
                text_mode = self._text_mode()
                if text_mode:
                    note = self._note("text:" + self.client.model,
                                      f"{self.client.model} has no native tool calling — "
                                      "describing the tools in the prompt instead")
                    if note:
                        yield note
                filt = StreamFilter(TOOLS)
                assistant_text = thinking = ""
                native = []
                got_error = switch = False
                wire = self._wire(text_mode, msgs)
                if nudged:
                    if wire and wire[-1].get("role") == "user":     # never user, user
                        wire = wire[:-1] + [{**wire[-1], "content":
                                             (wire[-1].get("content") or "") + "\n\n" + NUDGE}]
                    else:
                        wire = wire + [{"role": "user", "content": NUDGE}]
                for ev in self.client.chat(wire,
                                           tools=None if text_mode else TOOLS,
                                           think=self.cfg.think,
                                           cancel=lambda: self._interrupt,
                                           stop=["<tool_result"] if text_mode else None):
                    if self._interrupt:
                        got_error = True
                        break
                    et = ev["type"]
                    if et == "thinking":
                        thinking += ev["text"]
                        yield ("thinking", ev["text"])
                    elif et == "content":
                        shown = filt.feed(ev["text"])
                        inline = filt.take_thinking()
                        if inline:
                            yield ("thinking", inline)
                        if shown:
                            assistant_text += shown
                            yield ("assistant", shown)
                    elif et == "tool_calls":
                        native = ev["calls"]
                    elif et == "done":
                        stats = ev["stats"]
                        yield ("stats", stats)
                    elif et == "error":
                        if ev.get("unsupported") == "tools" and not text_mode:
                            switch = True     # the model refused tools: go to text mode
                            break
                        got_error = True
                        if not ev.get("interrupted"):
                            if attempt == 0 and not (assistant_text or native or filt.calls):
                                # may be retried: an error only if the retry fails too
                                # (a one-shot that recovered used to exit 1)
                                first_error = ev["message"]
                            else:
                                yield ("error", ev["message"])
                        break
                if not (got_error or switch or self._interrupt):
                    # Held-back text is let out only when the reply finished:
                    # cut off mid-call, it is half a JSON object — exactly the
                    # noise this filter exists to keep off the screen.
                    tail = filt.finish(cut=stats.get("done_reason") == "length")
                    inline = filt.take_thinking()
                    if inline:
                        yield ("thinking", inline)
                    if tail:
                        assistant_text += tail
                        yield ("assistant", tail)
                answer = filt.answer
                # Native calls win: when the server parsed some, anything the
                # filter also found is the same call echoed into the text.
                tool_calls = native or filt.calls
                if switch:
                    continue
                if self._interrupt:
                    break
                if not got_error or assistant_text or tool_calls:
                    break
                attempt += 1
                if attempt >= 2:
                    break
                if first_error:
                    yield ("info", f"{first_error} — retrying…")   # one retry on a stream error
                    first_error = ""
                time.sleep(1)

            if self._interrupt:
                # Whatever was parsed before Ctrl-C is not run: the user asked
                # to stop. Keep the words, so the next turn knows what was said.
                if answer.strip():
                    msgs.append({"role": "assistant", "content": answer})
                yield ("error", "interrupted")
                break
            if got_error and not (assistant_text or tool_calls):
                break
            if stats.get("done_reason") == "length":
                # third field: a script (headless) must not take half an answer for one
                yield ("info", "the reply was cut off: it reached the context limit", "truncated")

            if not tool_calls and not answer.strip():
                if not nudged:
                    nudged = True                    # one more go, with a nudge on the wire only
                    continue
                yield ("error", "the model returned an empty reply — try /retry, "
                                "or a different model")
                break
            nudged = False

            # record the assistant turn
            amsg = {"role": "assistant", "content": answer}
            if tool_calls:
                amsg["tool_calls"] = [
                    {"function": {"name": c["name"], "arguments": c["arguments"]},
                     **({"id": c["id"]} if c.get("id") else {})}
                    for c in tool_calls
                ]
                if thinking and not text_mode:
                    # gpt-oss expects its reasoning back between tool steps;
                    # without it every step started from a blank slate.
                    amsg["thinking"] = thinking
            msgs.append(amsg)

            if not tool_calls:
                final = answer.strip()
                if self.store is not None and final:
                    self.store.append("assistant", final)
                yield ("final", final, True)       # every word of it was streamed
                break

            # execute each requested tool
            finish_msg = None
            limit = max(2000, min(60000, self._budget_chars() // 4))
            for i, call in enumerate(tool_calls):
                name = call["name"]
                args = call["arguments"] if isinstance(call["arguments"], dict) else {}
                tid = {"tool_call_id": call["id"]} if call.get("id") else {}
                if self._interrupt:
                    # every call still gets an answer, or strict templates
                    # reject the history on the next turn
                    msgs.append({"role": "tool", "tool_name": name, **tid,
                                 "content": "error: interrupted by the user before it ran"})
                    continue

                if name == "run_bash":
                    if isinstance(args.get("command"), (list, tuple)):
                        # the user approves the exact string the shell will run,
                        # not a list whose quoting the shell reads differently
                        args = dict(args, command=shell.as_command(args["command"]))
                    why = shell.precheck(args.get("command", ""), self.ctx.cwd)
                    if why:
                        # never ask the user to approve what cannot run here
                        msgs.append({"role": "tool", "tool_name": name, **tid,
                                     "content": f"error: {why}"})
                        yield ("tool_end", name, f"error: {why}")
                        continue
                if name in APPROVAL_REQUIRED and not self.cfg.approves(name) \
                        and not self._pre_approved(name, args):
                    if not self.approver(name, args):
                        yield ("tool_denied", name)
                        msgs.append({"role": "tool", "tool_name": name, **tid,
                                     "content": "error: user denied this action"})
                        continue

                if name == "finish":
                    # The summary is the turn's answer; showing it as a tool
                    # call and its output too would print it three times.
                    result = run_tool(name, args, self.ctx)
                    finish_msg = result
                else:
                    yield ("tool_start", name, args)
                    result = run_tool(name, args, self.ctx)
                    yield ("tool_end", name, result)
                msgs.append({"role": "tool", "tool_name": name, **tid,
                             "content": _fit(result, limit)})

            if self._interrupt:
                yield ("error", "interrupted")
                break
            if finish_msg is not None:
                # It used to be whichever tool ran *last* — so `finish` followed
                # by anything else reported that tool's output as the summary.
                final = finish_msg.strip()
                if self.store is not None and final:
                    self.store.append("assistant", final)
                yield ("final", final, False)
                break
        else:
            yield ("final", "(stopped: reached max steps — say 'continue' to keep going)", False)

        yield ("turn_end",)
