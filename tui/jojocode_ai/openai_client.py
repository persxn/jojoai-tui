"""Client for OpenAI-compatible chat servers (stdlib only), behind the same
interface as OllamaClient.

── Why a second client ─────────────────────────────────────────────────────

Ollama is one way to run a model locally; it is not the only one people have.
LM Studio, llama.cpp's `llama-server`, vLLM, Jan, LocalAI — and Ollama itself
on `/v1` — all speak the OpenAI chat-completions protocol instead. Pointing
`jojo` at any of them used to fail at the first request, because the client
only knew Ollama's native `/api/chat`.

This client turns that protocol into the exact event stream the agent already
consumes (see OllamaClient.chat), so nothing above it changes: the same tool
loop, the same text-tool-call filter for models that write calls as text, the
same text mode for servers that refuse a tool schema.

Selected with `--api openai`, `JOJO_API=openai`, `"api": "openai"` in
settings.json, or simply a host URL ending in `/v1`. A key, when the server
wants one, comes from JOJO_API_KEY (or OPENAI_API_KEY).
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse

from . import transport
from .net import headers as _headers
from .ollama_client import OllamaError, _error_text as _err, same_model


def to_openai(messages: list[dict]) -> list[dict]:
    """The agent's history (Ollama's shape) in the shape the protocol requires.

    Differences that matter: tool-call arguments travel as a JSON *string*,
    every call has an id, and each tool result names the id it answers.
    Calls recorded without ids get them here, paired with results in order.
    """
    out: list[dict] = []
    pending: list[str] = []
    n = 0
    for m in messages:
        role = m.get("role")
        if role == "assistant" and m.get("tool_calls"):
            calls = []
            for c in m["tool_calls"]:
                fn = c.get("function", {})
                cid = c.get("id") or f"call_{n}"
                n += 1
                pending.append(cid)
                calls.append({"id": cid, "type": "function", "function": {
                    "name": fn.get("name", ""),
                    "arguments": json.dumps(fn.get("arguments") or {})}})
            out.append({"role": "assistant", "content": m.get("content") or None,
                        "tool_calls": calls})
        elif role == "tool":
            cid = m.get("tool_call_id") or (pending.pop(0) if pending else f"call_{n}")
            if m.get("tool_call_id") in pending:
                pending.remove(m["tool_call_id"])
            out.append({"role": "tool", "tool_call_id": cid, "content": m.get("content", "")})
        else:
            out.append({"role": role, "content": m.get("content", "")})
    return out


def _first_int(d: dict, keys) -> int | None:
    for k in keys:
        v = d.get(k)
        if isinstance(v, int) and not isinstance(v, bool) and v > 0:
            return v
    return None


class OpenAIClient:
    api = "openai"

    def __init__(self, host: str, model: str, num_ctx: int = 32768, timeout: int = 600):
        host = host.rstrip("/")
        # A bare server gets /v1; a URL that already names its API path
        # (…/v1, or Gemini's …/v1beta/openai) is used as given.
        self.base = host + "/v1" if urllib.parse.urlsplit(host).path in ("", "/") else host
        self.host = host
        self.model = model
        self.num_ctx = num_ctx
        self.timeout = timeout
        self.key = os.environ.get("JOJO_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
        self._caps: dict[str, dict] = {}
        self._ctx: int | None = None     # the context the server has loaded, once asked

    def _hdrs(self, extra=None) -> dict:
        h = {"Content-Type": "application/json"}
        if self.key:
            h["Authorization"] = f"Bearer {self.key}"
        h.update(extra or {})
        return _headers(h)

    def _get(self, path: str):
        return json.loads(transport.request("GET", self.base + path, headers=self._hdrs(),
                                            timeout=10) or b"{}")

    # -- introspection ---------------------------------------------------- #
    def version(self) -> str:
        try:
            self._get("/models")
        except (transport.TransportError, ValueError) as e:
            raise OllamaError(f"cannot reach an OpenAI-compatible server at {self.base}: {e}") from e
        return "OpenAI-compatible API"

    def list_models(self):
        try:
            data = self._get("/models")
        except (transport.TransportError, ValueError) as e:
            raise OllamaError(f"cannot list models: {e}") from e
        return [m.get("id", "") for m in data.get("data", []) if m.get("id")]

    def has_model(self, name: str) -> bool:
        try:
            names = self.list_models()
        except OllamaError:
            return False
        # Servers that host one model (llama-server) list it under a file name
        # and answer to any name; an empty or single listing is not "missing".
        return len(names) <= 1 or any(n == name or same_model(n, name) for n in names)

    def capabilities(self, model: str | None = None) -> dict:
        """Unknown until the server refuses something; see chat()."""
        model = model or self.model
        return self._caps.setdefault(model, {"tools": None, "thinking": None,
                                             "levels": "gpt-oss" in model.lower(),
                                             "context": None})

    def context_window(self) -> int:
        """num_ctx, capped by what the server actually loaded: LM Studio or
        llama-server running an 8k context answered every request past it with
        a 400 while the agent budgeted for 32k."""
        if self._ctx is None:
            # Asked in the background: up to three probes against a server
            # that does not answer must not hold up a turn Ctrl-C cannot cut
            # short. Until it answers, the configured num_ctx stands.
            self._ctx = 0
            threading.Thread(target=self._learn_context, daemon=True,
                             name="jojo-ctx-probe").start()
        return min(self.num_ctx, self._ctx) if self._ctx else self.num_ctx

    def _learn_context(self) -> None:
        self._ctx = self._loaded_context() or 0

    def _loaded_context(self) -> int | None:
        root = self.base[:-3] if self.base.endswith("/v1") else self.base
        probes = (
            (self.base + "/models", lambda d: next(
                (_first_int(m, ("max_model_len", "loaded_context_length", "context_length",
                                "max_context_length"))
                 for m in d.get("data", []) if isinstance(m, dict) and m.get("id") == self.model),
                None)),                                                     # vLLM, others
            (root + "/props", lambda d: _first_int(d.get("default_generation_settings") or {},
                                                   ("n_ctx",)) or _first_int(d, ("n_ctx",))),
            (root + "/api/v0/models/" + urllib.parse.quote(self.model, safe=""),
             lambda d: _first_int(d, ("loaded_context_length",))),          # LM Studio
        )
        for url, pick in probes:
            try:
                data = json.loads(transport.request("GET", url, headers=self._hdrs(),
                                                    timeout=3) or b"{}")
                n = pick(data) if isinstance(data, dict) else None
            except (transport.TransportError, ValueError, AttributeError, TypeError):
                n = None
            if n:
                return n
        return None

    # -- chat --------------------------------------------------------------- #
    def chat(self, messages, tools=None, think="medium", cancel=None, stop=None):
        caps = self.capabilities()
        body = {"model": self.model, "messages": to_openai(messages), "stream": True}
        if tools:
            body["tools"] = tools
        if stop:
            body["stop"] = list(stop)[:4]           # the protocol allows four
        on = think not in (None, False, "off", "")
        if caps["levels"]:
            body["reasoning_effort"] = think if on and isinstance(think, str) else "low"
        try:
            data = json.dumps(body).encode()
        except (TypeError, ValueError) as e:
            yield {"type": "error", "message": f"cannot encode the request: {e}"}
            return
        try:
            yield from self._stream(transport.stream(self.base + "/chat/completions", data,
                                                     self._hdrs(), self.timeout, cancel))
        except transport.Interrupted:
            yield {"type": "error", "message": "interrupted", "interrupted": True}
        except transport.HTTPStatusError as e:
            detail = _err(e.body)
            if tools and "tool" in detail.lower() and e.code in (400, 422, 500, 501):
                # llama-server without --jinja, a model with no tool template…
                caps["tools"] = False
                yield {"type": "error", "message": detail, "unsupported": "tools"}
                return
            yield {"type": "error", "message": f"HTTP {e.code}: {detail}"}
        except transport.TransportError as e:
            yield {"type": "error", "message": f"cannot reach {self.base}: {e}"}

    def _stream(self, lines):
        calls: dict[int, dict] = {}
        t0 = time.time()
        first = None
        n_out = 0
        usage = {}
        seen = False
        finish = None
        try:
            for raw in lines:
                seen = True
                line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue                         # blank, ": keep-alive", event:
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except ValueError:
                    continue
                if not isinstance(obj, dict):
                    continue
                if obj.get("error"):
                    yield {"type": "error", "message": _err(json.dumps(obj))}
                    return
                if isinstance(obj.get("usage"), dict):
                    usage = obj["usage"]
                for ch in obj.get("choices") or []:
                    if not isinstance(ch, dict):
                        continue
                    if ch.get("finish_reason"):
                        finish = ch["finish_reason"]
                    d = ch.get("delta") or ch.get("message") or {}
                    think = d.get("reasoning_content") or d.get("reasoning")
                    if think:
                        yield {"type": "thinking", "text": think}
                    if d.get("content"):
                        first = first or time.time()
                        n_out += 1
                        yield {"type": "content", "text": d["content"]}
                    for tc in d.get("tool_calls") or []:
                        slot = calls.setdefault(tc.get("index", len(calls)),
                                                {"name": "", "args": "", "id": ""})
                        fn = tc.get("function") or {}
                        slot["id"] = tc.get("id") or slot["id"]
                        slot["name"] += fn.get("name") or ""
                        a = fn.get("arguments")
                        slot["args"] += a if isinstance(a, str) else (json.dumps(a) if a else "")
        except (transport.Interrupted, transport.HTTPStatusError, transport.ConnectError):
            raise
        except transport.TransportError as e:
            yield {"type": "error", "message": f"connection lost mid-reply: {e}" if seen
                   else f"no reply from {self.base}: {e}"}
            return
        if calls:
            out = []
            for i in sorted(calls):
                c = calls[i]
                try:
                    args = json.loads(c["args"]) if c["args"].strip() else {}
                except ValueError:
                    args = {"_raw": c["args"]}
                out.append({"name": c["name"], "arguments": args if isinstance(args, dict) else {},
                            "id": c["id"]})
            yield {"type": "tool_calls", "calls": out}
        dur = time.time() - (first or t0)
        yield {"type": "done", "stats": {
            "prompt_eval_count": usage.get("prompt_tokens"),
            "eval_count": usage.get("completion_tokens") or n_out or None,
            "eval_duration": int(dur * 1e9) if dur > 0 else None,
            "done_reason": finish,          # "length": cut off, as Ollama says it
        }}
