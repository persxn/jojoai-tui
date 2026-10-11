"""The JojoAI agent loop: model <-> tools until the task is done.

── Models other than gpt-oss ───────────────────────────────────────────────

Tool calls reach this loop two ways. Models with native tool calling return
them in Ollama's `tool_calls` field; many also, or only, write them into their
reply as text, which `toolcalls.StreamFilter` lifts out before the user sees
it (see that file for the shapes and why). Models with no tool support at all
run in *text mode*: the tools are described in the system prompt, results go
back as ordinary user turns — their chat templates have no slot for a `tool`
message and would silently drop it — and the same filter reads the calls.
"""

from __future__ import annotations

import json
import os
import time

from .config import Config
from .ollama_client import OllamaClient
from .prompts import system_prompt, text_tool_protocol
from .toolcalls import StreamFilter
from .tools import APPROVAL_REQUIRED, TOOLS, Ctx, run_tool


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
        self.client = OllamaClient(cfg.host, cfg.model, cfg.num_ctx, cfg.request_timeout)
        self.ctx = Ctx(cwd=cfg.cwd)
        # approver(name, args) -> bool ; default: allow everything
        self.approver = approver or (lambda n, a: True)
        self.store = None          # optional jojoai.store.Store
        self.messages = [{"role": "system", "content": system_prompt(cfg.cwd)}]
        self._interrupt = False
        self._told_text_mode: set[str] = set()

    def reset(self):
        self.messages = [{"role": "system", "content": system_prompt(self.cfg.cwd)}]

    def interrupt(self):
        self._interrupt = True

    def _pre_approved(self, name: str, args: dict) -> bool:
        """Skip the approval prompt for trusted operations."""
        if name == "run_bash":
            return self.cfg.auto_bash
        if not (self.cfg.auto_edit and name in ("write_file", "edit_file")):
            return False
        try:
            # normcase: on Windows C:\Proj and c:\proj are one directory
            target = os.path.normcase(os.path.realpath(
                os.path.join(self.ctx.cwd, args.get("path", ""))))
            root = os.path.normcase(os.path.realpath(self.ctx.cwd))
            return os.path.commonpath([target, root]) == root
        except (ValueError, OSError, TypeError):
            return False

    # -- what goes over the wire ------------------------------------------ #
    def _text_mode(self) -> bool:
        return self.client.capabilities().get("tools") is False

    def _wire(self, text_mode: bool) -> list[dict]:
        if not text_mode:
            return self.messages
        out: list[dict] = []
        for m in self.messages:
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
            if out and m["role"] == "user" and out[-1]["role"] == "user":
                # several results in a row read as one turn
                out[-1] = {"role": "user", "content": out[-1]["content"] + "\n\n" + m["content"]}
            else:
                out.append(m)
        return out

    # -- the turn ------------------------------------------------------------- #
    def run(self, user_text: str):
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
        self.messages.append({"role": "user", "content": sent})
        yield ("user", user_text)

        for step in range(self.cfg.max_steps):
            if self._interrupt:
                yield ("error", "interrupted")
                break

            assistant_text = ""
            tool_calls = []
            got_error = False
            attempt = 0

            while True:
                text_mode = self._text_mode()
                if text_mode and self.client.model not in self._told_text_mode:
                    self._told_text_mode.add(self.client.model)
                    yield ("info", f"{self.client.model} has no native tool calling — "
                                   "describing the tools in the prompt instead")
                filt = StreamFilter(TOOLS)
                assistant_text = ""
                native = []
                got_error = switch = False
                for ev in self.client.chat(self._wire(text_mode),
                                           tools=None if text_mode else TOOLS,
                                           think=self.cfg.think):
                    if self._interrupt:
                        got_error = True
                        yield ("error", "interrupted")
                        break
                    et = ev["type"]
                    if et == "thinking":
                        yield ("thinking", ev["text"])
                    elif et == "content":
                        shown = filt.feed(ev["text"])
                        if shown:
                            assistant_text += shown
                            yield ("assistant", shown)
                    elif et == "tool_calls":
                        native = ev["calls"]
                    elif et == "done":
                        yield ("stats", ev["stats"])
                    elif et == "error":
                        if ev.get("unsupported") == "tools" and not text_mode:
                            switch = True     # the model refused tools: go to text mode
                            break
                        got_error = True
                        yield ("error", ev["message"])
                        break
                if not (got_error or switch):
                    # Held-back text is let out only when the reply finished:
                    # cut off mid-call, it is half a JSON object — exactly the
                    # noise this filter exists to keep off the screen.
                    tail = filt.finish()
                    if tail:
                        assistant_text += tail
                        yield ("assistant", tail)
                # Native calls win: when Ollama parsed some, anything the filter
                # also found is the same call echoed into the text.
                tool_calls = native or filt.calls
                if switch:
                    continue
                if not got_error or self._interrupt or assistant_text or tool_calls:
                    break
                attempt += 1
                if attempt >= 2:
                    break
                yield ("error", "retrying…")       # one retry on a stream error
                time.sleep(1)

            if got_error and not (assistant_text or tool_calls):
                break

            # record the assistant turn
            amsg = {"role": "assistant", "content": assistant_text}
            if tool_calls:
                amsg["tool_calls"] = [
                    {"function": {"name": c["name"], "arguments": c["arguments"]}}
                    for c in tool_calls
                ]
            self.messages.append(amsg)

            if not tool_calls:
                final = assistant_text.strip()
                if self.store is not None and final:
                    self.store.append("assistant", final)
                yield ("final", final, True)       # every word of it was streamed
                break

            # execute each requested tool
            finish_msg = None
            for call in tool_calls:
                name = call["name"]
                args = call["arguments"] if isinstance(call["arguments"], dict) else {}

                if name in APPROVAL_REQUIRED and not self.cfg.approves(name) \
                        and not self._pre_approved(name, args):
                    if not self.approver(name, args):
                        yield ("tool_denied", name)
                        self.messages.append({
                            "role": "tool", "tool_name": name,
                            "content": "error: user denied this action",
                        })
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
                self.messages.append({
                    "role": "tool", "tool_name": name,
                    "content": result if len(result) < 60000 else result[:60000] + "\n...[clipped]",
                })

            if finish_msg is not None:
                # It used to be whichever tool ran *last* — so `finish` followed
                # by anything else reported that tool's output as the summary.
                final = finish_msg.strip()
                if self.store is not None and final:
                    self.store.append("assistant", final)
                yield ("final", final, False)
                break
        else:
            yield ("final", "(stopped: reached max steps)", False)

        yield ("turn_end",)
