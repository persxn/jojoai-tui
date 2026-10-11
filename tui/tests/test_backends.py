"""The model-server plumbing: transport, the OpenAI-compatible client, backend
selection, where the host comes from, and the chat-history store.

Each group pins a failure real use ran into:

- **Only Ollama's /api/chat was spoken.** LM Studio, llama-server, vLLM and
  Ollama's own /v1 failed at the first request. The OpenAI client must turn
  SSE `data:` lines — tool calls streamed as argument *fragments* keyed by
  `index`, `reasoning_content` as thinking, `[DONE]` — into the same events
  the agent already runs, and send history back the way the protocol wants it:
  arguments as a JSON string, ids on every call, `tool_call_id` on every
  result. A server that refuses `tools` must drop the turn into text mode,
  not fail it.
- **Ctrl-C could not stop a turn that had not started streaming.** urlopen
  blocked until the first header — minutes while a big model loads. The
  transport must give up within a poll interval of the cancel flag.
- **A local Ollama was reached through http_proxy.** Loopback must be direct;
  every other host keeps the proxy it had.
- **Errors arrived as JSON** (`{"error": "..."}`) instead of a sentence.
- **Config ignored OLLAMA_HOST**, rejected the forms Ollama accepts
  (`:11434`, `0.0.0.0`), ignored a settings.json saved with a BOM (Notepad,
  PowerShell 5.1), and a save over a corrupt file threw a hand edit away.
- **The store indexed inside the turn** (one blocking embed per chunk before
  the model was asked), switched indexing off for good after one failed
  embed, compared vectors from different embedding models, and left chat
  history world-readable.

Every server here is a fake on 127.0.0.1; nothing touches the network.
"""

from __future__ import annotations

import importlib
import json
import os
import socket
import sqlite3
import stat
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tui"))

import jojocode_ai  # noqa: E402
from jojocode_ai import config as config_mod  # noqa: E402
from jojocode_ai import store as store_mod  # noqa: E402
from jojocode_ai import transport  # noqa: E402
from jojocode_ai.agent import Agent  # noqa: E402
from jojocode_ai.backends import make_client, pick_api  # noqa: E402
from jojocode_ai.config import Config, normalize_host  # noqa: E402
from jojocode_ai.ollama_client import OllamaClient, _error_text  # noqa: E402
from jojocode_ai.openai_client import OpenAIClient, to_openai  # noqa: E402
from jojocode_ai.store import Store  # noqa: E402

POSIX = os.name == "posix"
ANSWER = "There is one file here: hello.py."
PROXY_VARS = ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY",
              "all_proxy", "ALL_PROXY", "no_proxy", "NO_PROXY")


# --------------------------------------------------------------------------- #
# A scriptable HTTP server: routes are functions of (handler, request body).
# --------------------------------------------------------------------------- #

class FakeServer:
    def __init__(self, routes: dict):
        self.routes = routes
        self.requests: list[dict] = []        # {"method", "path", "headers", "body"}
        self._srv: ThreadingHTTPServer | None = None

    def __enter__(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def json(self, code, obj):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def sse(self, events):
                """Server-sent events the way OpenAI-compatible servers frame them,
                with the comment and `event:` lines some of them interleave."""
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                try:
                    self.wfile.write(b": keep-alive\n\n")
                    for ev in events:
                        payload = ev if isinstance(ev, str) else json.dumps(ev)
                        self.wfile.write(f"event: message\ndata: {payload}\n\n".encode())
                        self.wfile.flush()
                    self.wfile.write(b"data: [DONE]\n\n")
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def ndjson(self, objs):
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.end_headers()
                try:
                    for o in objs:
                        self.wfile.write((json.dumps(o) + "\n").encode())
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def _handle(self, method):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                try:
                    body = json.loads(raw) if raw else {}
                except ValueError:
                    body = {}
                fake.requests.append({"method": method, "path": self.path,
                                      "headers": dict(self.headers), "body": body})
                route = fake.routes.get(self.path)
                if route is None:
                    return self.json(404, {"error": "not found"})
                return route(self, body)

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._srv.serve_forever, kwargs={"poll_interval": 0.05},
                         daemon=True).start()
        return self

    def __exit__(self, *exc):
        if self._srv:
            self._srv.shutdown()
            self._srv.server_close()

    @property
    def url(self) -> str:
        assert self._srv is not None
        return f"http://127.0.0.1:{self._srv.server_address[1]}"

    def bodies(self, path: str) -> list[dict]:
        return [r["body"] for r in self.requests if r["path"] == path]


def dead_port() -> int:
    """A loopback port nothing listens on: connecting is refused at once."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_for(cond, timeout: float = 5.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


# -- OpenAI-compatible stream pieces ----------------------------------------- #

def chunk(**delta) -> dict:
    return {"id": "chatcmpl-1", "object": "chat.completion.chunk", "model": "local",
            "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}


def tc(index, args="", name=None, cid=None) -> dict:
    """One streamed tool-call fragment."""
    frag: dict = {"index": index, "function": {"arguments": args}}
    if name is not None:
        frag["function"]["name"] = name
        frag["type"] = "function"
    if cid is not None:
        frag["id"] = cid
    return chunk(tool_calls=[frag])


def finish(reason="stop") -> dict:
    return {"id": "chatcmpl-1", "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {}, "finish_reason": reason}]}


def text_chunks(text: str, size: int = 4) -> list[dict]:
    return [chunk(content=text[i:i + size]) for i in range(0, len(text), size)]


def has_tool_result(body: dict) -> bool:
    for m in body.get("messages") or []:
        if m.get("role") == "tool":
            return True
        if m.get("role") == "user" and "<tool_result" in (m.get("content") or ""):
            return True
    return False


def openai_server(reply, models=("local-model",)) -> FakeServer:
    """`reply(body)` returns a list of SSE chunks, or (status, json) to refuse."""
    def chat(h, body):
        out = reply(body)
        if isinstance(out, tuple):
            return h.json(*out)
        return h.sse(out)

    return FakeServer({
        "/v1/models": lambda h, b: h.json(200, {"object": "list",
                                                "data": [{"id": m} for m in models]}),
        "/v1/chat/completions": chat,
    })


# -- agent helpers ------------------------------------------------------------ #

@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("JOJO_DATA", str(tmp_path / "data"))
    for k in ("JOJO_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "hello.py").write_text("print('hi')\n", encoding="utf-8")
    return proj


def run_turn(cfg: Config, text="what is here?"):
    agent = Agent(cfg)
    return agent, list(agent.run(text))


def shown(events) -> str:
    out = "".join(e[1] for e in events if e[0] == "assistant")
    for e in events:
        if e[0] == "final" and e[1] and not (len(e) > 2 and e[2]):
            out += e[1]
    return out


def errors(events) -> list[str]:
    return [e[1] for e in events if e[0] == "error"]


# =========================================================================== #
# OpenAI-compatible servers, end to end
# =========================================================================== #

def lists_then_answers(body):
    if has_tool_result(body):
        return text_chunks(ANSWER) + [
            finish(), {"id": "chatcmpl-1", "object": "chat.completion.chunk", "choices": [],
                       "usage": {"prompt_tokens": 42, "completion_tokens": 9}}]
    return [
        chunk(role="assistant", content=""),
        chunk(reasoning_content="Look at the "),
        chunk(reasoning_content="folder first."),
        tc(0, "", name="list_dir", cid="call_7"),
        tc(0, '{"pa'),
        tc(0, 'th": '),
        tc(0, '"."}'),
        finish("tool_calls"),
    ]


@pytest.mark.parametrize("how", ["api=openai", "host ends in /v1"])
def test_openai_turn_runs_the_streamed_tool_call_and_answers(project, how):
    with openai_server(lists_then_answers) as srv:
        if how == "api=openai":
            cfg = Config(host=srv.url, model="local-model", api="openai", cwd=str(project))
        else:
            cfg = Config(host=srv.url + "/v1", model="local-model", api="auto",
                         cwd=str(project))
        agent, events = run_turn(cfg)
        sent = srv.bodies("/v1/chat/completions")
        paths = [r["path"] for r in srv.requests]

    assert isinstance(agent.client, OpenAIClient)
    assert errors(events) == []
    assert not any("/v1/v1" in p for p in paths), paths
    assert [p for p in paths if p.endswith("/completions")] == ["/v1/chat/completions"] * 2, paths
    assert [e[1:] for e in events if e[0] == "tool_start"] == [("list_dir", {"path": "."})]
    assert "hello.py" in next(e[2] for e in events if e[0] == "tool_end")
    assert "".join(e[1] for e in events if e[0] == "thinking") == "Look at the folder first."
    assert shown(events) == ANSWER
    stats = next(e[1] for e in events if e[0] == "stats" and e[1].get("prompt_eval_count"))
    assert stats["prompt_eval_count"] == 42 and stats["eval_count"] == 9

    assert len(sent) == 2
    assert sent[0]["stream"] is True and sent[0]["tools"]
    second = sent[1]["messages"]
    call_msg, result = second[-2], second[-1]
    assert call_msg["role"] == "assistant"
    assert set(call_msg) <= {"role", "content", "tool_calls"}      # no "thinking" leak
    (call,) = call_msg["tool_calls"]
    assert call["id"] == "call_7" and call["type"] == "function"
    assert call["function"]["name"] == "list_dir"
    assert isinstance(call["function"]["arguments"], str)          # a JSON *string*
    assert json.loads(call["function"]["arguments"]) == {"path": "."}
    assert result["role"] == "tool" and result["tool_call_id"] == "call_7"
    assert set(result) == {"role", "tool_call_id", "content"}
    assert "hello.py" in result["content"]


def test_interleaved_parallel_calls_are_reassembled_by_index(project):
    """Two calls whose argument fragments alternate: concatenating in arrival
    order would produce two broken JSON objects."""
    def reply(body):
        if has_tool_result(body):
            return text_chunks(ANSWER)
        return [
            tc(0, "", name="read_file", cid="call_a"),
            tc(1, "", name="list_dir", cid="call_b"),
            tc(0, '{"path": "hel'),
            tc(1, '{"path": "'),
            tc(0, 'lo.py"}'),
            tc(1, '."}'),
            finish("tool_calls"),
        ]

    with openai_server(reply) as srv:
        _, events = run_turn(Config(host=srv.url, model="m", api="openai", cwd=str(project)))
        sent = srv.bodies("/v1/chat/completions")

    assert errors(events) == []
    assert [e[1:] for e in events if e[0] == "tool_start"] == [
        ("read_file", {"path": "hello.py"}), ("list_dir", {"path": "."})]
    msgs = sent[1]["messages"]
    calls = msgs[-3]["tool_calls"]
    assert [c["id"] for c in calls] == ["call_a", "call_b"]
    assert [m["tool_call_id"] for m in msgs[-2:]] == ["call_a", "call_b"]
    assert "print('hi')" in msgs[-2]["content"]
    assert shown(events) == ANSWER


def test_a_server_that_refuses_tools_drops_the_turn_into_text_mode(project):
    """llama-server without --jinja, or a model with no tool template: the
    400 names tools, and the same turn carries on with the protocol in the
    prompt instead of failing."""
    def reply(body):
        if body.get("tools"):
            return (400, {"error": {"message": "tools param requires --jinja flag",
                                    "type": "invalid_request_error"}})
        if has_tool_result(body):
            return text_chunks(ANSWER)
        return text_chunks('<tool_call>{"name": "list_dir", "arguments": {"path": "."}}'
                           '</tool_call>')

    with openai_server(reply) as srv:
        agent, events = run_turn(Config(host=srv.url, model="m", api="openai",
                                        cwd=str(project)))
        sent = srv.bodies("/v1/chat/completions")

    assert errors(events) == []
    assert agent.client.capabilities()["tools"] is False
    assert any(e[0] == "info" and "no native tool calling" in e[1] for e in events)
    assert [e[1] for e in events if e[0] == "tool_start"] == ["list_dir"]
    assert shown(events) == ANSWER
    assert sent[0].get("tools")
    assert all("tools" not in b for b in sent[1:])
    assert "<tool_call>" in sent[1]["messages"][0]["content"]
    assert sent[1]["stop"] == ["<tool_result"]
    assert not any(m["role"] == "tool" for m in sent[-1]["messages"])


def test_a_400_that_is_not_about_tools_is_an_error_not_text_mode():
    def reply(body):
        return (400, {"error": {"message": "context length exceeded (9000 > 4096)"}})

    with openai_server(reply) as srv:
        c = OpenAIClient(srv.url, "m")
        evs = list(c.chat([{"role": "user", "content": "hi"}], tools=[{"type": "function"}]))
    assert evs == [{"type": "error", "message": "HTTP 400: context length exceeded (9000 > 4096)"}]
    assert c.capabilities()["tools"] is None


def test_openai_reasoning_effort_only_for_gpt_oss(project):
    with openai_server(lambda b: text_chunks("ok")) as srv:
        list(OpenAIClient(srv.url, "gpt-oss-20b").chat(
            [{"role": "user", "content": "x"}], think="off"))
        list(OpenAIClient(srv.url, "gpt-oss-20b").chat(
            [{"role": "user", "content": "x"}], think="high"))
        list(OpenAIClient(srv.url, "qwen2.5-coder-7b").chat(
            [{"role": "user", "content": "x"}], think="high"))
        sent = srv.bodies("/v1/chat/completions")
    assert sent[0]["reasoning_effort"] == "low"        # gpt-oss cannot switch off
    assert sent[1]["reasoning_effort"] == "high"
    assert "reasoning_effort" not in sent[2]


def test_to_openai_gives_calls_without_ids_ids_and_pairs_results_in_order():
    wire = to_openai([
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"function": {"name": "list_dir", "arguments": {}}},
                        {"function": {"name": "read_file", "arguments": {"path": "a"}}}]},
        {"role": "tool", "tool_name": "list_dir", "content": "a"},
        {"role": "tool", "tool_name": "read_file", "content": "1\tx"},
    ])
    ids = [c["id"] for c in wire[1]["tool_calls"]]
    assert len(set(ids)) == 2 and all(ids)
    assert [m["tool_call_id"] for m in wire[2:]] == ids
    assert wire[1]["content"] is None                     # "" is refused by some servers
    assert [json.loads(c["function"]["arguments"]) for c in wire[1]["tool_calls"]] == [
        {}, {"path": "a"}]


def test_openai_client_sends_key_and_our_user_agent(monkeypatch):
    monkeypatch.setenv("JOJO_API_KEY", "sk-test")
    with openai_server(lambda b: text_chunks("hi"), models=("a", "b")) as srv:
        c = OpenAIClient(srv.url + "/v1/", "a")
        assert c.base == srv.url + "/v1"
        assert c.list_models() == ["a", "b"]
        assert c.has_model("a") and not c.has_model("c")
        list(c.chat([{"role": "user", "content": "x"}]))
        hdrs = [r["headers"] for r in srv.requests]
    assert all(h.get("Authorization") == "Bearer sk-test" for h in hdrs)
    assert all(h.get("User-Agent", "").startswith("JojoAI/") for h in hdrs)


def test_a_single_model_server_answers_to_any_name():
    with openai_server(lambda b: [], models=("qwen2.5-coder-7b-q4_k_m.gguf",)) as srv:
        assert OpenAIClient(srv.url, "whatever").has_model("whatever")


# =========================================================================== #
# Backend selection
# =========================================================================== #

@pytest.mark.parametrize("api,host,want", [
    (None, "http://127.0.0.1:11434", "ollama"),
    ("auto", "http://127.0.0.1:11434", "ollama"),
    ("auto", "http://127.0.0.1:1234/v1", "openai"),
    ("auto", "http://127.0.0.1:1234/v1/", "openai"),
    ("", "http://box:8080/v1", "openai"),
    ("openai", "http://127.0.0.1:11434", "openai"),
    (" OpenAI ", "http://127.0.0.1:11434", "openai"),
    ("oai", "http://x", "openai"),
    ("v1", "http://x", "openai"),
    ("ollama", "http://127.0.0.1:11434/v1", "ollama"),
    ("OLLAMA", "http://x", "ollama"),
])
def test_pick_api(api, host, want):
    assert pick_api(api, host) == want


def test_make_client_follows_pick_api(tmp_path):
    def cfg(**kw):
        return Config(cwd=str(tmp_path), model="m", num_ctx=4096, request_timeout=33, **kw)

    c = make_client(cfg(host="http://127.0.0.1:11434", api="auto"))
    assert isinstance(c, OllamaClient) and c.num_ctx == 4096 and c.timeout == 33
    c = make_client(cfg(host="http://127.0.0.1:1234/v1", api="auto"))
    assert isinstance(c, OpenAIClient) and c.base == "http://127.0.0.1:1234/v1"
    c = make_client(cfg(host="http://127.0.0.1:1234", api="openai"))
    assert isinstance(c, OpenAIClient) and c.base == "http://127.0.0.1:1234/v1"
    assert c.timeout == 33 and c.context_window() == 4096
    c = make_client(cfg(host="http://127.0.0.1:11434/v1", api="ollama"))
    assert isinstance(c, OllamaClient)


# =========================================================================== #
# Transport
# =========================================================================== #

class SilentServer:
    """Accepts, reads the request, and says nothing — Ollama loading a model.
    Records when the client hangs up (Ollama's cue to stop generating)."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.hung_up = threading.Event()
        self.release = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.sock.getsockname()[1]}"

    def _serve(self):
        try:
            conn, _ = self.sock.accept()
        except OSError:
            return
        conn.settimeout(0.05)
        try:
            while not self.release.is_set():
                try:
                    data = conn.recv(65536)
                except socket.timeout:
                    continue
                except OSError:
                    self.hung_up.set()
                    return
                if not data:
                    self.hung_up.set()
                    return
        finally:
            conn.close()

    def close(self):
        self.release.set()
        self.sock.close()


def test_stream_is_interrupted_while_the_server_is_silent():
    srv = SilentServer()
    try:
        start = time.monotonic()
        flag_at = start + 0.3
        gen = transport.stream(srv.url + "/api/chat", b"{}", {}, timeout=30,
                               cancel=lambda: time.monotonic() >= flag_at)
        with pytest.raises(transport.Interrupted):
            next(gen)
        late = time.monotonic() - flag_at
        assert late < 0.5, f"Interrupted came {late:.2f}s after cancel() turned true"
        # walking away closes the socket, which is how the server learns to stop
        assert srv.hung_up.wait(1.0)
    finally:
        srv.close()


@pytest.mark.parametrize("which", ["ollama", "openai"])
def test_clients_report_an_interrupt_as_an_interrupted_event(which):
    srv = SilentServer()
    try:
        flag_at = time.monotonic() + 0.2
        if which == "ollama":
            c = OllamaClient(srv.url, "m")
            c._caps["m"] = {"tools": True, "thinking": False, "levels": False, "context": None}
        else:
            c = OpenAIClient(srv.url, "m")
        evs = list(c.chat([{"role": "user", "content": "x"}],
                          cancel=lambda: time.monotonic() >= flag_at))
        assert time.monotonic() - flag_at < 0.5
        assert evs == [{"type": "error", "message": "interrupted", "interrupted": True}]
    finally:
        srv.close()


def test_agent_turn_stops_promptly_when_interrupted_before_the_first_token(project):
    srv = SilentServer()
    try:
        agent = Agent(Config(host=srv.url, model="m", api="openai", cwd=str(project)))
        threading.Timer(0.2, agent.interrupt).start()
        t0 = time.monotonic()
        events = list(agent.run("hello"))
        assert time.monotonic() - t0 < 1.5
        assert ("turn_end",) in events
        assert errors(events) == ["interrupted"]
    finally:
        srv.close()


def _set_proxy_env(monkeypatch, proxy: str):
    for k in PROXY_VARS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("REQUEST_METHOD", raising=False)   # CGI mode ignores HTTP_PROXY
    for k in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY"):
        monkeypatch.setenv(k, proxy)


def test_loopback_is_reached_directly_even_with_a_dead_proxy(monkeypatch):
    _set_proxy_env(monkeypatch, f"http://127.0.0.1:{dead_port()}")
    with openai_server(lambda b: text_chunks("direct"), models=("m",)) as srv:
        data = json.loads(transport.request("GET", srv.url + "/v1/models"))
        assert data["data"][0]["id"] == "m"
        lines = list(transport.stream(srv.url + "/v1/chat/completions", b"{}"))
        assert b"data: [DONE]" in [ln.strip() for ln in lines]
        assert OpenAIClient(srv.url, "m").list_models() == ["m"]


@pytest.mark.parametrize("host", ["127.0.0.1", "127.8.9.10", "localhost", "LOCALHOST",
                                  "ollama.localhost", "::1", "[::1]"])
def test_loopback_hosts_never_take_the_proxy(monkeypatch, host):
    _set_proxy_env(monkeypatch, "http://proxy.invalid:3128")
    assert transport._proxy_for("http", host) is None


class RecordingProxy:
    """A forward proxy that answers every request itself and remembers it."""

    def __init__(self):
        self.seen: list[tuple[str, dict]] = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _rec(self):
                fake.seen.append((self.requestline, dict(self.headers)))

            def do_GET(self):
                self._rec()
                body = b'{"via": "proxy"}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_CONNECT(self):
                self._rec()
                self.send_response(403)
                self.send_header("Content-Length", "0")
                self.end_headers()

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05},
                         daemon=True).start()

    @property
    def port(self):
        return self.srv.server_address[1]

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def test_other_hosts_keep_their_proxy_with_credentials(monkeypatch):
    proxy = RecordingProxy()
    try:
        _set_proxy_env(monkeypatch, f"http://us%40er:p%3Ass@127.0.0.1:{proxy.port}")
        out = transport.request("GET", "http://models.jojo-test.invalid:8080/v1/models?x=1")
        assert json.loads(out) == {"via": "proxy"}
        line, hdrs = proxy.seen[0]
        assert line.startswith("GET http://models.jojo-test.invalid:8080/v1/models?x=1 ")
        # base64("us@er:p:ss")
        assert hdrs.get("Proxy-Authorization") == "Basic dXNAZXI6cDpzcw=="

        with pytest.raises(transport.TransportError):
            transport.request("GET", "https://models.jojo-test.invalid/v1/models")
        line, hdrs = proxy.seen[1]
        assert line.startswith("CONNECT models.jojo-test.invalid:443 ")
        assert hdrs.get("Proxy-Authorization") == "Basic dXNAZXI6cDpzcw=="
    finally:
        proxy.close()


def test_http_status_errors_carry_code_and_body():
    body = {"error": "model 'nope:7b' not found, try pulling it first"}
    with FakeServer({"/api/chat": lambda h, b: h.json(404, body),
                     "/api/tags": lambda h, b: h.json(503, body)}) as srv:
        with pytest.raises(transport.HTTPStatusError) as ei:
            transport.request("GET", srv.url + "/api/tags")
        assert ei.value.code == 503 and json.loads(ei.value.body) == body
        assert str(ei.value) == "HTTP 503"
        assert isinstance(ei.value, transport.TransportError)

        with pytest.raises(transport.HTTPStatusError) as ei:
            list(transport.stream(srv.url + "/api/chat", b"{}"))
        assert ei.value.code == 404 and json.loads(ei.value.body) == body


def test_an_unreachable_server_is_described_not_dumped():
    url = f"http://127.0.0.1:{dead_port()}"
    with pytest.raises(transport.TransportError) as ei:
        transport.request("GET", url + "/api/version")
    assert "connection refused" in str(ei.value)
    with pytest.raises(transport.TransportError):
        transport.request("GET", "ftp://127.0.0.1/x")


@pytest.mark.parametrize("which", ["ollama", "openai"])
def test_a_refused_connection_names_the_server_it_could_not_reach(which):
    """Nothing was ever received, so "connection lost mid-reply" is the wrong
    diagnosis: the user needs to hear which host did not answer."""
    url = f"http://127.0.0.1:{dead_port()}"
    if which == "ollama":
        c = OllamaClient(url, "m")
        c._caps["m"] = {"tools": True, "thinking": False, "levels": False, "context": None}
    else:
        c = OpenAIClient(url, "m")
    evs = list(c.chat([{"role": "user", "content": "x"}]))
    assert len(evs) == 1 and evs[0]["type"] == "error"
    msg = evs[0]["message"]
    assert "connection refused" in msg
    assert "mid-reply" not in msg, msg
    assert url in msg, msg


# -- errors as sentences ---------------------------------------------------- #

@pytest.mark.parametrize("raw,want", [
    ('{"error": "model \\"x:7b\\" not found, try pulling it first"}',
     'model "x:7b" not found, try pulling it first'),
    ('{"error": {"message": "The model `foo` does not exist", "type": "invalid_request_error"}}',
     "The model `foo` does not exist"),
    ("  upstream connect error  \n", "upstream connect error"),
    ('["not", "an", "object"]', '["not", "an", "object"]'),
])
def test_error_text_is_a_sentence(raw, want):
    assert _error_text(raw) == want


def test_ollama_errors_reach_the_user_as_sentences():
    def chat(h, body):
        if body.get("model") == "crash":
            return h.json(500, {"error": "llama runner process has terminated: exit status 2"})
        return h.ndjson([{"message": {"role": "assistant", "content": "par"}, "done": False},
                         {"error": "an error was encountered while running the model"}])

    with FakeServer({"/api/chat": chat,
                     "/api/show": lambda h, b: h.json(200, {"capabilities": ["completion"]})}) as srv:
        evs = list(OllamaClient(srv.url, "crash").chat([{"role": "user", "content": "x"}]))
        assert evs == [{"type": "error",
                        "message": "HTTP 500: llama runner process has terminated: exit status 2"}]
        evs = list(OllamaClient(srv.url, "midway").chat([{"role": "user", "content": "x"}]))
        assert evs[-1] == {"type": "error",
                           "message": "an error was encountered while running the model"}


def test_openai_errors_reach_the_user_as_sentences():
    def reply(body):
        if body["model"] == "missing":
            return (404, {"error": {"message": "The model `missing` does not exist",
                                    "type": "invalid_request_error"}})
        return [chunk(content="par"), {"error": {"message": "server overloaded"}}]

    with openai_server(reply) as srv:
        evs = list(OpenAIClient(srv.url, "missing").chat([{"role": "user", "content": "x"}]))
        assert evs == [{"type": "error",
                        "message": "HTTP 404: The model `missing` does not exist"}]
        evs = list(OpenAIClient(srv.url, "m").chat([{"role": "user", "content": "x"}]))
        assert evs[-1] == {"type": "error", "message": "server overloaded"}
    assert all("{" not in e["message"] for e in evs if e["type"] == "error")


# =========================================================================== #
# Config: the host, its precedence, and settings.json
# =========================================================================== #

@pytest.mark.parametrize("raw,want", [
    ("localhost:11434", "http://localhost:11434"),
    ("0.0.0.0", "http://127.0.0.1:11434"),
    ("0.0.0.0:8080", "http://127.0.0.1:8080"),
    (":11434", "http://127.0.0.1:11434"),
    ("::1", "http://[::1]:11434"),
    ("[::1]:8080", "http://[::1]:8080"),
    ("::", "http://127.0.0.1:11434"),
    ("http://[::]:11434", "http://127.0.0.1:11434"),
    ("http://0.0.0.0:11434/", "http://127.0.0.1:11434"),
    ("host/v1", "http://host:11434/v1"),
    ("https://x/v1", "https://x/v1"),
    ("https://x/v1/", "https://x/v1"),
    ("box", "http://box:11434"),
    ("HTTP://Box:11434", "http://Box:11434"),
    ("  192.168.1.5:11434/  ", "http://192.168.1.5:11434"),
    ("", "http://127.0.0.1:11434"),
    ("http://0.0.0.0.example.com:80", "http://0.0.0.0.example.com:80"),
])
def test_normalize_host(raw, want):
    assert normalize_host(raw) == want


def reload_config(monkeypatch, home: Path, env: dict[str, str] | None = None):
    """config.py fresh — its defaults are read at import time. The module that
    every other test imported is put back afterwards."""
    monkeypatch.setenv("JOJO_CONFIG", str(home))
    for k in ("JOJO_MODEL", "JOJO_HOST", "JOJO_API", "OLLAMA_HOST"):
        monkeypatch.delenv(k, raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(jojocode_ai, "config", sys.modules["jojocode_ai.config"])
    monkeypatch.delitem(sys.modules, "jojocode_ai.config")
    return importlib.import_module("jojocode_ai.config")


def write_settings(home: Path, obj, bom=False) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    p = home / "settings.json"
    p.write_text(json.dumps(obj), encoding="utf-8-sig" if bom else "utf-8")
    return p


def test_ollama_host_is_used_when_nothing_else_says(monkeypatch, tmp_path):
    cfg = reload_config(monkeypatch, tmp_path / "cfg", {"OLLAMA_HOST": "0.0.0.0:11500"})
    assert cfg.DEFAULT_HOST == "http://127.0.0.1:11500"
    assert cfg.Config().host == "http://127.0.0.1:11500"


def test_nothing_configured_is_local_ollama(monkeypatch, tmp_path):
    cfg = reload_config(monkeypatch, tmp_path / "cfg")
    assert cfg.Config().host == "http://127.0.0.1:11434"
    assert cfg.Config().api == "auto"


def test_settings_host_beats_ollama_host(monkeypatch, tmp_path):
    home = tmp_path / "cfg"
    write_settings(home, {"host": "box:11434", "api": "openai"})
    cfg = reload_config(monkeypatch, home, {"OLLAMA_HOST": "elsewhere:1"})
    assert cfg.Config().host == "http://box:11434"
    assert cfg.Config().api == "openai"


def test_jojo_host_beats_settings_and_ollama_host(monkeypatch, tmp_path):
    home = tmp_path / "cfg"
    write_settings(home, {"host": "http://box:11434", "api": "openai"})
    cfg = reload_config(monkeypatch, home, {"JOJO_HOST": ":9999", "OLLAMA_HOST": "other:1",
                                            "JOJO_API": "ollama"})
    assert cfg.Config().host == "http://127.0.0.1:9999"
    assert cfg.Config().api == "ollama"


def test_settings_saved_with_a_bom_are_read(monkeypatch, tmp_path):
    home = tmp_path / "cfg"
    p = write_settings(home, {"model": "qwen2.5-coder:7b", "host": "lan-box"}, bom=True)
    assert p.read_bytes().startswith(b"\xef\xbb\xbf")
    cfg = reload_config(monkeypatch, home)
    assert cfg.load_settings() == {"model": "qwen2.5-coder:7b", "host": "lan-box"}
    assert cfg.Config().model == "qwen2.5-coder:7b"
    assert cfg.Config().host == "http://lan-box:11434"
    # and a save over a BOM file merges rather than treating it as corrupt
    cfg.save_settings(api="openai")
    assert cfg.load_settings() == {"model": "qwen2.5-coder:7b", "host": "lan-box",
                                   "api": "openai"}
    assert not (home / "settings.json.bak").exists()


def test_save_over_a_corrupt_file_keeps_a_backup(monkeypatch, tmp_path):
    home = tmp_path / "cfg"
    home.mkdir()
    broken = '{"model": "a:1", "host": "http://box:11434",}\n'     # trailing comma
    (home / "settings.json").write_text(broken, encoding="utf-8")
    cfg = reload_config(monkeypatch, home)
    path = cfg.save_settings(model="b:2")
    assert json.loads(Path(path).read_text(encoding="utf-8")) == {"model": "b:2"}
    assert (home / "settings.json.bak").read_text(encoding="utf-8") == broken
    assert not (home / "settings.json.tmp").exists()


def test_save_over_a_good_file_makes_no_backup(monkeypatch, tmp_path):
    home = tmp_path / "cfg"
    write_settings(home, {"model": "a:1"})
    cfg = reload_config(monkeypatch, home)
    cfg.save_settings(host="http://box:11434")
    assert cfg.load_settings() == {"model": "a:1", "host": "http://box:11434"}
    assert not (home / "settings.json.bak").exists()


# =========================================================================== #
# Store: background indexing, model-scoped recall, permissions, retry
# =========================================================================== #

class FakeEmbedder:
    """/api/embeddings and /v1/embeddings. Every text maps to the same vector,
    so any two rows that are compared at all score 1.0."""

    def __init__(self):
        self.ok = True
        self.gate: threading.Event | None = None      # set → hold every reply
        self.dims = {"m-big": 4}
        self.calls: list[dict] = []
        self.server = FakeServer({"/api/embeddings": self._ollama,
                                  "/v1/embeddings": self._openai})

    def _vec(self, model):
        return [1.0] + [0.0] * (self.dims.get(model, 3) - 1)

    def _wait(self):
        if self.gate is not None:
            self.gate.wait(5)

    def _ollama(self, h, body):
        self.calls.append(body)
        self._wait()
        if not self.ok:
            return h.json(404, {"error": f'model "{body.get("model")}" not found, try pulling it first'})
        return h.json(200, {"embedding": self._vec(body.get("model"))})

    def _openai(self, h, body):
        self.calls.append(body)
        self._wait()
        if not self.ok:
            return h.json(404, {"error": {"message": "no such model"}})
        return h.json(200, {"object": "list",
                            "data": [{"index": 0, "embedding": self._vec(body.get("model"))}]})

    def __enter__(self):
        self.server.__enter__()
        return self

    def __exit__(self, *exc):
        if self.gate is not None:
            self.gate.set()
        self.server.__exit__(*exc)

    @property
    def url(self):
        return self.server.url


@pytest.fixture
def data_home(tmp_path, monkeypatch):
    d = tmp_path / "data"
    monkeypatch.setenv("JOJO_DATA", str(d))
    stores: list[Store] = []
    yield d, stores
    for s in stores:
        s.flush(2)
        try:
            s.db.close()
        except sqlite3.Error:
            pass


def rows(s: Store) -> int:
    with s._lock:
        return s.db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]


def test_append_returns_at_once_while_the_embedder_is_slow(data_home):
    d, stores = data_home
    with FakeEmbedder() as emb:
        emb.gate = threading.Event()                    # the embedder "takes forever"
        s = Store(emb.url, "m1")
        stores.append(s)
        t0 = time.monotonic()
        s.append("user", "how do I set the proxy?")
        s.append("assistant", "Set http_proxy.")
        took = time.monotonic() - t0
        assert took < 0.5, f"append blocked for {took:.2f}s on indexing"
        # the history line is written now, not after indexing
        lines = Path(s.session_path).read_text(encoding="utf-8").splitlines()
        assert [json.loads(x)["role"] for x in lines] == ["user", "assistant"]
        assert wait_for(lambda: len(emb.calls) >= 1)       # indexing did start…
        assert rows(s) == 0                                 # …and is still waiting
        emb.gate.set()
        assert wait_for(lambda: rows(s) == 2)


def test_recall_only_compares_rows_from_the_same_embedding_model(data_home):
    d, stores = data_home
    with FakeEmbedder() as emb:
        old = Store(emb.url, "m1")
        stores.append(old)
        old.append("user", "configure the corporate proxy for ollama")
        assert wait_for(lambda: rows(old) == 1)

        other_model = Store(emb.url, "m2")                  # same dim, other model
        stores.append(other_model)
        assert other_model.retrieve("proxy") == []

        bigger = Store(emb.url, "m-big")                    # other dim
        stores.append(bigger)
        assert bigger.retrieve("proxy") == []

        same = Store(emb.url, "m1")
        stores.append(same)
        hits = same.retrieve("proxy")
        assert [h[4] for h in hits] == ["configure the corporate proxy for ollama"]

        # a row of the same model but another dimension is never scored
        # (zip() would happily compare its first two components)
        with same._lock:
            same.db.execute("INSERT INTO chunks(session,ts,role,title,text,dim,emb,model) "
                            "VALUES(?,?,?,?,?,?,?,?)",
                            ("elsewhere", time.time(), "user", "", "wrong-dim", 2,
                             store_mod._pack([1.0, 0.0]), "m1"))
            same.db.commit()
            same._units = None
        assert [h[4] for h in same.retrieve("proxy")] == [
            "configure the corporate proxy for ollama"]


def test_openai_embeddings_endpoint_is_used_for_an_openai_server(data_home):
    d, stores = data_home
    with FakeEmbedder() as emb:
        s = Store(emb.url + "/v1", "m1", api="openai")
        stores.append(s)
        s.append("user", "remember this")
        assert wait_for(lambda: rows(s) == 1)
        paths = [r["path"] for r in emb.server.requests]
    assert paths == ["/v1/embeddings"]                     # not /v1/v1/embeddings
    assert emb.calls[0] == {"model": "m1", "input": "remember this"}


@pytest.mark.skipif(not POSIX, reason="POSIX permission bits")
def test_history_is_private_on_posix(data_home):
    d, stores = data_home
    (d / "sessions").mkdir(parents=True)
    os.chmod(d, 0o755)
    os.chmod(d / "sessions", 0o755)
    old = os.umask(0o022)
    try:
        s = Store(f"http://127.0.0.1:{dead_port()}", "m1")
        stores.append(s)
        s.append("user", "my secret plan")
    finally:
        os.umask(old)
    mode = lambda p: stat.S_IMODE(os.stat(p).st_mode)  # noqa: E731
    assert mode(d) == 0o700
    assert mode(d / "sessions") == 0o700
    assert mode(s.session_path) == 0o600
    assert mode(d / "index.db") == 0o600


def test_a_failed_embed_is_retried_after_the_backoff(data_home, monkeypatch):
    d, stores = data_home
    monkeypatch.setattr(store_mod, "RETRY_AFTER", 0.5)
    with FakeEmbedder() as emb:
        emb.ok = False                                    # embed model not pulled yet
        s = Store(emb.url, "m1")
        stores.append(s)
        s.append("user", "first")
        assert wait_for(lambda: s._embed_ok is False)
        assert len(emb.calls) == 1

        # inside the backoff: no request at all, and recall says "unavailable"
        s.append("user", "second")
        assert s.flush(2)
        time.sleep(0.1)
        assert len(emb.calls) == 1
        assert s.embed_available() is False and len(emb.calls) == 1
        assert rows(s) == 0

        emb.ok = True                                     # the user ran `ollama pull`
        time.sleep(0.6)
        s.append("user", "third")
        assert wait_for(lambda: rows(s) == 1)
        assert len(emb.calls) == 2                        # indexing came back on its own
        assert s.embed_available() is True


def test_flush_waits_for_an_item_the_worker_has_already_taken(data_home):
    """flush() is how a clean exit (and every test above) knows indexing is
    done. The worker takes an item off the queue and only then marks itself
    busy; a thread switch in between makes the queue look empty and the worker
    look idle. The sleep stands in for that preemption."""
    d, stores = data_home
    with FakeEmbedder() as emb:
        s = Store(emb.url, "m1")
        stores.append(s)
        real_get = s._q.get

        def preempted_get(*a, **kw):
            item = real_get(*a, **kw)
            time.sleep(0.3)
            return item

        # The worker is already blocked in the original get(); it hands back
        # "one" unpatched, and every get() after that is the patched one.
        s._q.get = preempted_get
        s.append("user", "one")
        assert wait_for(lambda: rows(s) == 1)
        time.sleep(0.1)                                   # worker now waits in preempted_get
        s.append("user", "two")
        time.sleep(0.05)                                  # …and has taken "two" off the queue
        assert s.flush(2) is True
        assert rows(s) == 2, "flush() reported drained while an item was still being indexed"
