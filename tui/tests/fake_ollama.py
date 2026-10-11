"""A stand-in for the parts of Ollama's HTTP API that JojoAI depends on.

Real models cannot run in a test, and the bugs these tests pin down are not in
the model — they are in what the client sends and how it reads what comes back.
So this server is faithful in exactly those two places:

- **Validation mirrors Ollama's `server/routes.go`.** A truthy `think` sent to a
  model without the `thinking` capability is a 400 `"<model>" does not support
  thinking`; `tools` sent to a model without the `tools` capability is a 400
  `<model> does not support tools`. Those two refusals are how every model that
  is not gpt-oss used to fail on its first turn.
- **Replies are streamed in small pieces**, so a marker such as `<tool_call>`
  arrives split across chunks the way real tokens split it.

Each model's `reply` is a function of the request body that returns the list of
message fragments to stream: `{"content": ...}`, `{"thinking": ...}` or
`{"tool_calls": [...]}`.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def pieces(text: str, size: int = 3) -> list[dict]:
    """Split text into content fragments, deliberately cutting through markers."""
    return [{"content": text[i:i + size]} for i in range(0, len(text), size)]


class FakeModel:
    def __init__(self, name, capabilities, reply, family="llama", template=""):
        self.name = name
        self.capabilities = list(capabilities)
        self.reply = reply
        self.family = family
        self.template = template


class FakeOllama:
    def __init__(self, *models: FakeModel, show_capabilities: bool = True):
        self.models = {m.name: m for m in models}
        # Ollama before ~0.6 had no `capabilities` in /api/show; the client
        # must cope with that by learning from the 400s instead.
        self.show_capabilities = show_capabilities
        self.requests: list[dict] = []
        self._srv: ThreadingHTTPServer | None = None

    # -- lifecycle -------------------------------------------------------- #
    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()

    def start(self) -> str:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # keep pytest output clean
                pass

            def _json(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/api/version":
                    return self._json(200, {"version": "0.12.6"})
                if self.path == "/api/tags":
                    return self._json(200, {"models": [{"name": n} for n in fake.models]})
                return self._json(404, {"error": "not found"})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                if self.path == "/api/show":
                    return fake._show(self, body)
                if self.path == "/api/chat":
                    fake.requests.append(body)
                    return fake._chat(self, body)
                return self._json(404, {"error": "not found"})

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._srv.serve_forever, kwargs={"poll_interval": 0.05},
                         daemon=True).start()
        return self.url

    @property
    def url(self) -> str:
        assert self._srv is not None
        return f"http://127.0.0.1:{self._srv.server_address[1]}"

    def stop(self):
        if self._srv:
            self._srv.shutdown()
            self._srv.server_close()

    # -- endpoints -------------------------------------------------------- #
    def _model(self, h, name):
        m = self.models.get(name)
        if m is None:
            h._json(404, {"error": f"model {name!r} not found, try pulling it first"})
        return m

    def _show(self, h, body):
        m = self._model(h, body.get("model") or body.get("name"))
        if m is None:
            return
        out = {"template": m.template,
               "details": {"family": m.family},
               "model_info": {"general.architecture": m.family}}
        if self.show_capabilities:
            out["capabilities"] = m.capabilities
        h._json(200, out)

    def _chat(self, h, body):
        m = self._model(h, body.get("model"))
        if m is None:
            return
        think = body.get("think")
        if isinstance(think, str) and think not in ("low", "medium", "high", "max"):
            return h._json(400, {"error": f'invalid think value: "{think}"'})
        if think and "thinking" not in m.capabilities:
            return h._json(400, {"error": f'"{m.name}" does not support thinking'})
        if body.get("tools") and "tools" not in m.capabilities:
            return h._json(400, {"error": f"registry.ollama.ai/library/{m.name} "
                                          f"does not support tools"})

        h.send_response(200)
        h.send_header("Content-Type", "application/x-ndjson")
        h.end_headers()
        try:
            for frag in m.reply(body):
                msg = {"role": "assistant", "content": ""}
                msg.update(frag)
                line = {"model": m.name, "message": msg, "done": False}
                h.wfile.write((json.dumps(line) + "\n").encode())
                h.wfile.flush()
            h.wfile.write((json.dumps({
                "model": m.name, "message": {"role": "assistant", "content": ""},
                "done": True, "prompt_eval_count": 10, "eval_count": 5,
                "eval_duration": 1_000_000_000,
            }) + "\n").encode())
        except (BrokenPipeError, ConnectionResetError):
            pass        # the client hung up — an interrupt does exactly that


def last_role(body: dict) -> str:
    msgs = body.get("messages") or []
    return msgs[-1]["role"] if msgs else ""


def saw_tool_result(body: dict) -> bool:
    """True once a tool result is anywhere in the conversation sent back.

    Text-mode models get results as user turns, so look at content too.
    """
    for m in body.get("messages") or []:
        if m.get("role") == "system":
            continue        # the text-mode protocol quotes a <tool_result> example
        if m.get("role") == "tool" or "<tool_result" in (m.get("content") or ""):
            return True
    return False
