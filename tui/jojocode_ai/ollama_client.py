"""Minimal Ollama client (stdlib only) for the native /api/chat streaming endpoint.

── Asking the model what it can do ─────────────────────────────────────────

The request used to be the one gpt-oss wants — `think: "medium"` plus the tool
schema — sent to every model. Ollama refuses both outright for models that
lack the capability (server/routes.go):

    "qwen2.5-coder:7b" does not support thinking         (HTTP 400)
    …/codellama:7b does not support tools                (HTTP 400)

so every model but gpt-oss failed on its first turn — including the
qwen2.5-coder the installer itself recommends. Now the client reads the
model's `capabilities` from /api/show before the first request and shapes the
request to fit: reasoning levels only for gpt-oss, a plain `true`/`false` for
other thinking models, nothing at all for the rest. Ollama too old to report
capabilities is handled by learning from the 400 and retrying once.

Tool support is the one thing the client cannot paper over on its own: without
it the agent must describe the tools in the prompt instead, so a refusal comes
back as an error event tagged `unsupported: "tools"` for the agent to act on.
"""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request

from .net import headers as _headers

THINK_LEVELS = ("low", "medium", "high")


class OllamaError(RuntimeError):
    pass


def _error_text(raw: str) -> str:
    """Ollama's errors arrive as {"error": "..."}; show the sentence, not the JSON."""
    try:
        obj = json.loads(raw)
    except ValueError:
        return raw.strip()
    if isinstance(obj, dict) and isinstance(obj.get("error"), str):
        return obj["error"]
    return raw.strip()


def same_model(a: str, b: str) -> bool:
    """`qwen2.5-coder` and `qwen2.5-coder:latest` are one model; `:7b` and `:14b` are not."""
    def norm(n: str) -> str:
        n = n.strip()
        return n if ":" in n.rsplit("/", 1)[-1] else n + ":latest"
    return norm(a) == norm(b)


class OllamaClient:
    def __init__(self, host: str, model: str, num_ctx: int = 32768, timeout: int = 600):
        self.host = host.rstrip("/")
        self.model = model
        self.num_ctx = num_ctx
        self.timeout = timeout
        self._caps: dict[str, dict] = {}     # model -> what it can do

    # -- introspection ----------------------------------------------------- #
    def _get(self, path: str):
        req = urllib.request.Request(self.host + path, method="GET", headers=_headers())
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())

    def _post(self, path: str, body: dict, timeout: int = 10):
        req = urllib.request.Request(
            self.host + path, data=json.dumps(body).encode(), method="POST",
            headers=_headers({"Content-Type": "application/json"}))
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())

    def version(self) -> str:
        try:
            return self._get("/api/version").get("version", "?")
        except Exception as e:  # noqa: BLE001
            raise OllamaError(f"cannot reach Ollama at {self.host}: {e}")

    def list_models(self):
        try:
            data = self._get("/api/tags")
        except Exception as e:  # noqa: BLE001
            raise OllamaError(f"cannot list models: {e}")
        return [m["name"] for m in data.get("models", [])]

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
        """{"tools": bool|None, "thinking": bool|None, "levels": bool}; None = unknown."""
        model = model or self.model
        if model not in self._caps:
            self._caps[model] = self._probe(model)
        return self._caps[model]

    def _probe(self, model: str) -> dict:
        caps = {"tools": None, "thinking": None, "levels": "gpt-oss" in model.lower()}
        try:
            info = self._post("/api/show", {"model": model})
        except Exception:  # noqa: BLE001 — unknown is fine; the 400s will teach us
            return caps
        listed = info.get("capabilities")
        if isinstance(listed, list):
            caps["tools"] = "tools" in listed
            caps["thinking"] = "thinking" in listed
        elif info.get("template"):
            # Before `capabilities` existed, Ollama decided tool support by
            # whether the template mentions .Tools — so can we.
            caps["tools"] = ".Tools" in info["template"]
        arch = str((info.get("model_info") or {}).get("general.architecture", ""))
        family = str((info.get("details") or {}).get("family", ""))
        if "gptoss" in (arch + family).replace("-", "").lower():
            caps["levels"] = True
        return caps

    def _think_value(self, think, caps: dict):
        """What to send as `think` — or None to leave it out entirely."""
        on = think not in (None, False, "off", "")
        if caps["thinking"] is False:
            return None                       # asking is a 400
        if caps["levels"]:                    # gpt-oss: as it always was
            return think if on else None
        if not on:
            # Ollama turns thinking ON for a capable model that is not told
            # otherwise, so "off" has to be said out loud.
            return False if caps["thinking"] else None
        return True

    # -- chat ------------------------------------------------------------- #
    def chat(self, messages, tools=None, think="medium"):
        """Yield event dicts:
        {"type": "thinking", "text": str}
        {"type": "content",  "text": str}
        {"type": "tool_calls", "calls": [{"name": str, "arguments": dict}]}
        {"type": "done", "stats": dict}
        {"type": "error", "message": str[, "unsupported": "tools"]}
        """
        caps = self.capabilities()
        for _ in range(3):                    # at most: learn thinking, learn levels
            body = {
                "model": self.model,
                "messages": messages,
                "stream": True,
                "options": {"num_ctx": self.num_ctx},
            }
            tv = self._think_value(think, caps)
            if tv is not None:
                body["think"] = tv
            if tools:
                body["tools"] = tools

            req = urllib.request.Request(
                self.host + "/api/chat", data=json.dumps(body).encode(), method="POST",
                headers=_headers({"Content-Type": "application/json"}),
            )
            try:
                resp = urllib.request.urlopen(req, timeout=self.timeout)
            except urllib.error.HTTPError as e:
                detail = _error_text(e.read().decode(errors="replace"))
                low = detail.lower()
                if "does not support thinking" in low and caps["thinking"] is not False:
                    caps["thinking"] = False
                    continue
                if "think value" in low and caps["levels"]:
                    caps["levels"] = False
                    continue
                if "does not support tools" in low:
                    caps["tools"] = False
                    yield {"type": "error", "message": detail, "unsupported": "tools"}
                    return
                yield {"type": "error", "message": f"HTTP {e.code}: {detail}"}
                return
            except urllib.error.URLError as e:
                yield {"type": "error", "message": f"connection failed: {e.reason}"}
                return
            except (OSError, http.client.HTTPException) as e:
                # a timeout while the model loads, a reset — not wrapped by urllib
                yield {"type": "error", "message": f"connection failed: {e}"}
                return
            yield from self._stream(resp)
            return
        yield {"type": "error", "message": "Ollama kept refusing the request"}

    def _stream(self, resp):
        pending_calls = []
        try:
            with resp:
                for raw in resp:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        obj = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if "error" in obj:
                        yield {"type": "error", "message": _error_text(json.dumps(obj))}
                        return
                    msg = obj.get("message") or {}
                    if msg.get("thinking"):
                        yield {"type": "thinking", "text": msg["thinking"]}
                    if msg.get("content"):
                        yield {"type": "content", "text": msg["content"]}
                    for tc in msg.get("tool_calls") or []:
                        fn = tc.get("function", {})
                        name = fn.get("name", "")
                        raw_args = fn.get("arguments", {})
                        if isinstance(raw_args, str):
                            try:
                                raw_args = json.loads(raw_args)
                            except json.JSONDecodeError:
                                raw_args = {"_raw": raw_args}
                        if not isinstance(raw_args, dict):
                            raw_args = {}
                        pending_calls.append({"name": name, "arguments": raw_args})
                    if obj.get("done"):
                        if pending_calls:
                            yield {"type": "tool_calls", "calls": pending_calls}
                        yield {"type": "done", "stats": {
                            "prompt_eval_count": obj.get("prompt_eval_count"),
                            "eval_count": obj.get("eval_count"),
                            "eval_duration": obj.get("eval_duration"),
                            "load_duration": obj.get("load_duration"),
                            "total_duration": obj.get("total_duration"),
                        }}
                        return
        except (OSError, http.client.HTTPException, ValueError) as e:
            # Used to escape the generator as an exception: the TUI showed a
            # bare "TimeoutError: timed out" and headless mode crashed outright.
            yield {"type": "error", "message": f"connection lost mid-reply: {e}"}
            return
        yield {"type": "error", "message": "Ollama closed the stream before finishing the reply"}
