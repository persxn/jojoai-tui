"""Client for Ollama's native API (/api/chat, /api/show, /api/tags), stdlib only.

── Asking the model what it can do ─────────────────────────────────────────

The request used to be the one gpt-oss wants — `think: "medium"` plus the tool
schema — sent to every model. Ollama refuses both outright for models that
lack the capability (server/routes.go):

    "qwen2.5-coder:7b" does not support thinking         (HTTP 400)
    …/codellama:7b does not support tools                (HTTP 400)

so every model but gpt-oss failed on its first turn — including the
qwen2.5-coder the installer itself recommends. Now the client reads the
model's `capabilities` from /api/show before the first request and shapes the
request to fit: reasoning levels only for gpt-oss (which cannot switch
reasoning off, so "off" means its lowest level), a plain `true`/`false` for
other thinking models, nothing at all for the rest. Ollama too old to report
capabilities is handled by learning from the 400 and retrying.

/api/show also gives the model's trained context length. Asking for a bigger
`num_ctx` than that does not buy context — Ollama clamps it at load — but it
does make Ollama budget the prompt against a window the model does not have.

Tool support is the one thing the client cannot paper over on its own: without
it the agent must describe the tools in the prompt instead, so a refusal comes
back as an error event tagged `unsupported: "tools"` for the agent to act on.

Requests go through transport.py: interruptible while Ollama is still loading
the model, and never through a proxy for a local Ollama.
"""

from __future__ import annotations

import json
import time

from . import transport
from .net import headers as _headers

THINK_LEVELS = ("low", "medium", "high")
PROBE_TIMEOUT = 5       # seconds for /api/show; it answers at once when Ollama is up
PROBE_RETRY = 30        # seconds before a failed probe is tried again


class OllamaError(RuntimeError):
    pass


def _error_text(raw: str) -> str:
    """Ollama's errors arrive as {"error": "..."}; show the sentence, not the JSON."""
    try:
        obj = json.loads(raw)
    except ValueError:
        return raw.strip()
    if isinstance(obj, dict):
        err = obj.get("error")
        if isinstance(err, str):
            return err
        if isinstance(err, dict) and isinstance(err.get("message"), str):
            return err["message"]                       # OpenAI-style servers
    return raw.strip()


def same_model(a: str, b: str) -> bool:
    """`qwen2.5-coder` and `qwen2.5-coder:latest` are one model; `:7b` and `:14b` are not."""
    def norm(n: str) -> str:
        n = n.strip().lower()
        return n if ":" in n.rsplit("/", 1)[-1] else n + ":latest"
    return norm(a) == norm(b)


class OllamaClient:
    api = "ollama"

    def __init__(self, host: str, model: str, num_ctx: int = 32768, timeout: int = 600):
        self.host = host.rstrip("/")
        self.model = model
        self.num_ctx = num_ctx
        self.timeout = timeout
        self._caps: dict[str, dict] = {}     # model -> what it can do (only once known)
        self._probe_failed: dict = {}        # model -> (when, caps-as-unknown)

    # -- plumbing ------------------------------------------------------------ #
    def _get(self, path: str, timeout: float = 10):
        return json.loads(transport.request("GET", self.host + path, headers=_headers(),
                                            timeout=timeout) or b"{}")

    def _post(self, path: str, body: dict, timeout: float = 10):
        data = transport.request("POST", self.host + path, json.dumps(body).encode(),
                                 _headers({"Content-Type": "application/json"}), timeout)
        return json.loads(data or b"{}")

    # -- introspection ----------------------------------------------------- #
    def version(self) -> str:
        try:
            return self._get("/api/version").get("version", "?")
        except (transport.TransportError, ValueError) as e:
            raise OllamaError(f"cannot reach Ollama at {self.host}: {e}") from e

    def list_models(self):
        try:
            data = self._get("/api/tags")
        except (transport.TransportError, ValueError) as e:
            raise OllamaError(f"cannot list models: {e}") from e
        return [m["name"] for m in data.get("models", []) if isinstance(m, dict) and "name" in m]

    def has_model(self, name: str) -> bool:
        # It used to be enough for any tag of the same family to be pulled, so
        # a machine with only qwen2.5-coder:7b passed the check for :14b and
        # then failed the first request with "model not found".
        try:
            names = self.list_models()
        except OllamaError:
            return False
        return any(same_model(n, name) for n in names)

    def capabilities(self, model: str | None = None) -> dict:
        """{"tools", "thinking": bool|None, "levels": bool, "context": int|None}.

        None means unknown. Only a successful probe is remembered: Ollama that
        was not up yet at the first ask is asked again next time.
        """
        model = model or self.model
        caps = self._caps.get(model)
        if caps is None:
            failed = self._probe_failed.get(model)
            if failed and time.monotonic() - failed[0] < PROBE_RETRY:
                return dict(failed[1])
            caps, known = self._probe(model)
            if known:
                self._caps[model] = caps
            else:
                # Not asked again for a while: each step consults this several
                # times, and against a host that never answers that was 4 × 10 s
                # of waiting that Ctrl-C could not cut short.
                self._probe_failed[model] = (time.monotonic(), dict(caps))
        return caps

    def _remember(self, model: str, caps: dict) -> None:
        self._caps[model] = caps          # what a 400 taught us sticks either way

    def _probe(self, model: str) -> tuple[dict, bool]:
        caps = {"tools": None, "thinking": None, "levels": "gpt-oss" in model.lower(),
                "context": None}
        try:
            info = self._post("/api/show", {"model": model}, timeout=PROBE_TIMEOUT)
        except (transport.TransportError, ValueError):
            return caps, False
        listed = info.get("capabilities")
        if isinstance(listed, list):
            caps["tools"] = "tools" in listed
            caps["thinking"] = "thinking" in listed
        elif info.get("template"):
            # Before `capabilities` existed, Ollama decided tool support by
            # whether the template mentions .Tools — so can we.
            caps["tools"] = ".Tools" in info["template"]
        mi = info.get("model_info") or {}
        arch = str(mi.get("general.architecture", ""))
        family = str((info.get("details") or {}).get("family", ""))
        if "gptoss" in (arch + family).replace("-", "").lower():
            caps["levels"] = True
        for k, v in mi.items():
            if k.endswith(".context_length") and isinstance(v, int) and v > 0:
                caps["context"] = v
                break
        return caps, True

    def context_window(self) -> int:
        """The num_ctx actually sent: the configured one, capped by the model's."""
        ctx = self.capabilities().get("context")
        return min(self.num_ctx, ctx) if ctx else self.num_ctx

    def _think_value(self, think, caps: dict):
        """What to send as `think` — or None to leave it out entirely."""
        on = think not in (None, False, "off", "")
        if caps["thinking"] is False:
            return None                       # asking is a 400
        if caps["levels"]:
            # gpt-oss always reasons; leaving the field out meant its default,
            # "medium" — so "off" cost more than "low". Low is the floor.
            return think if on and think in THINK_LEVELS else ("medium" if on else "low")
        if not on:
            # Ollama turns thinking ON for a capable model that is not told
            # otherwise, so "off" has to be said out loud.
            return False if caps["thinking"] else None
        return True

    # -- chat ------------------------------------------------------------- #
    def chat(self, messages, tools=None, think="medium", cancel=None, stop=None):
        """Yield event dicts:
        {"type": "thinking", "text": str}
        {"type": "content",  "text": str}
        {"type": "tool_calls", "calls": [{"name": str, "arguments": dict}]}
        {"type": "done", "stats": dict}
        {"type": "error", "message": str[, "unsupported": "tools" | "interrupted": True]}
        """
        caps = dict(self.capabilities())
        for _ in range(3):                    # at most: learn thinking, learn levels
            options = {"num_ctx": self.context_window()}
            if stop:
                options["stop"] = list(stop)
            body = {"model": self.model, "messages": messages, "stream": True,
                    "options": options}
            tv = self._think_value(think, caps)
            if tv is not None:
                body["think"] = tv
            if tools:
                body["tools"] = tools
            try:
                data = json.dumps(body).encode()
            except (TypeError, ValueError) as e:
                yield {"type": "error", "message": f"cannot encode the request: {e}"}
                return
            try:
                lines = transport.stream(self.host + "/api/chat", data,
                                         _headers({"Content-Type": "application/json"}),
                                         self.timeout, cancel)
                yield from self._stream(lines)
                return
            except transport.Interrupted:
                yield {"type": "error", "message": "interrupted", "interrupted": True}
                return
            except transport.HTTPStatusError as e:
                detail = _error_text(e.body)
                low = detail.lower()
                if "does not support thinking" in low and caps["thinking"] is not False:
                    caps["thinking"] = False
                    self._remember(self.model, caps)
                    continue
                if "think value" in low and caps["levels"]:
                    caps["levels"] = False
                    self._remember(self.model, caps)
                    continue
                if "does not support tools" in low:
                    caps["tools"] = False
                    self._remember(self.model, caps)
                    yield {"type": "error", "message": detail, "unsupported": "tools"}
                    return
                yield {"type": "error", "message": f"HTTP {e.code}: {detail}"}
                return
            except transport.TransportError as e:
                yield {"type": "error", "message": f"cannot reach Ollama at {self.host}: {e}"}
                return
        yield {"type": "error", "message": "Ollama kept refusing the request"}

    def _stream(self, lines):
        """Parse the NDJSON stream. Transport errors propagate to chat()."""
        pending_calls = []
        got_any = False
        try:
            for raw in lines:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except ValueError:
                    continue
                got_any = True
                if "error" in obj:
                    yield {"type": "error", "message": _error_text(json.dumps(obj))}
                    return
                msg = obj.get("message") or {}
                if msg.get("thinking"):
                    yield {"type": "thinking", "text": msg["thinking"]}
                if msg.get("content"):
                    yield {"type": "content", "text": msg["content"]}
                for tc in msg.get("tool_calls") or []:
                    fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                    raw_args = fn.get("arguments", {})
                    if isinstance(raw_args, str):
                        try:
                            raw_args = json.loads(raw_args)
                        except ValueError:
                            raw_args = {"_raw": raw_args}
                    if not isinstance(raw_args, dict):
                        raw_args = {}
                    pending_calls.append({"name": str(fn.get("name", "")), "arguments": raw_args})
                if obj.get("done"):
                    if pending_calls:
                        yield {"type": "tool_calls", "calls": pending_calls}
                    stats = {k: obj.get(k) for k in ("prompt_eval_count", "eval_count",
                                                     "eval_duration", "load_duration",
                                                     "total_duration", "done_reason")}
                    yield {"type": "done", "stats": stats}
                    return
        except (transport.Interrupted, transport.HTTPStatusError, transport.ConnectError):
            raise                        # chat() decides: stop, or learn and retry
        except transport.TransportError as e:
            # Used to escape the generator as an exception: the TUI showed a
            # bare "TimeoutError: timed out" and headless mode crashed outright.
            yield {"type": "error", "message": f"connection lost mid-reply: {e}" if got_any
                   else f"no reply from Ollama at {self.host}: {e}"}
            return
        if pending_calls:
            yield {"type": "tool_calls", "calls": pending_calls}
        yield {"type": "error", "message": "Ollama closed the stream before finishing the reply"
                                           if got_any else "Ollama sent an empty response"}
