"""The agent loop and the stream filter in front of it, after the fix pass.

Each of these broke a real turn, and each is pinned here against the loop as
it runs — the fake Ollama below speaks the wire protocol, the agent drives it:

- **Reasoning was run as if it were a decision.** A model that writes its
  thinking inline (<think>…</think>, or a bare </think> when the template
  opened the tag for it) had calls it only *considered* lifted out and run,
  and its reasoning stored in history as the answer.
- **Things shaped like calls were run.** A tool *definition* the model quoted,
  or a log line with a timestamp, read as a call; a Python literal holding
  bytes was accepted and then failed the next request's json.dumps.
- **Calls shaped unusually were lost or mangled.** Two pretty-printed calls in
  one fence or one <tool_call> ran neither; a Windows path written with single
  backslashes ("C:\\Users\\me\\new.txt") became a path with a newline in it.
- **Text-mode models invented their own tool output** after a call, and the
  user read the made-up result as if it were real.
- **A 200 KB write_file call re-scanned the whole buffer per delta** — seconds
  of CPU on every big file the model wrote.
- **Ctrl-C did not stop a turn** while the model was still loading (urlopen
  blocks until the first token), ran a call it had already parsed, and a
  batch cut off midway left calls without results that strict chat templates
  then refused on every later request.
- **gpt-oss lost its reasoning between tool steps**, and kept the old turns'
  reasoning forever after.
- **The request did not fit the model.** num_ctx above the model's trained
  window; "off" sent to gpt-oss as nothing (= its default, "medium"); no stop
  sequence for text mode; an empty reply ended the turn silently; old tool
  output pushed the user's own request out of the window.
- **The approval boundary moved with the model.** change_dir carried auto-edit
  pre-approval wherever the model went, and writes under .git/ or to dotfiles
  (which run on their own) skipped the prompt.
- **The system prompt said what was true at startup** — the old directory, and
  "every run_bash requires confirmation" under --trust or YOLO.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tui"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_ollama import FakeModel, FakeOllama, pieces, saw_tool_result  # noqa: E402
from jojocode_ai import agent as agent_mod  # noqa: E402
from jojocode_ai.agent import NUDGE, Agent  # noqa: E402
from jojocode_ai.config import Config  # noqa: E402
from jojocode_ai.toolcalls import StreamFilter, calls_in, tool_info  # noqa: E402
from jojocode_ai.tools import TOOLS  # noqa: E402

POSIX = os.name == "posix"
ANSWER = "There is one file here: hello.py."
CAPS_TOOLS = ["completion", "tools"]
CAPS_OSS = ["completion", "tools", "thinking"]


# --------------------------------------------------------------------------- #
# a fake Ollama that can also be slow to load, and knows its context window
# --------------------------------------------------------------------------- #

class Ollama(FakeOllama):
    """FakeOllama plus two things real Ollama does that these tests need.

    `load_delay`: nothing at all — not even response headers — is sent until
    the model has loaded, which is what made Ctrl-C wait out the timeout.
    `context`: /api/show reports the model's trained context length.
    """

    def __init__(self, *models, context: int | None = None, load_delay: float = 0.0, **kw):
        super().__init__(*models, **kw)
        self.context = context
        self.load_delay = load_delay
        self.release = threading.Event()

    def _show(self, h, body):
        m = self._model(h, body.get("model") or body.get("name"))
        if m is None:
            return
        info = {"general.architecture": m.family}
        if self.context:
            info[f"{m.family}.context_length"] = self.context
        out = {"template": m.template, "details": {"family": m.family}, "model_info": info}
        if self.show_capabilities:
            out["capabilities"] = m.capabilities
        h._json(200, out)

    def _chat(self, h, body):
        if self.load_delay:
            self.release.wait(self.load_delay)      # "loading": silence, no headers
        try:
            super()._chat(h, body)
        except OSError:
            pass                                    # the client gave up on us — fine

    def stop(self):
        self.release.set()
        super().stop()


def native(*calls) -> list[dict]:
    """One streamed fragment carrying native tool calls: (name, args) pairs."""
    return [{"tool_calls": [{"function": {"name": n, "arguments": a}} for n, a in calls]}]


def tool_results(body: dict) -> int:
    """How many tool results the conversation sent back holds (native mode)."""
    return sum(1 for m in body.get("messages") or [] if m.get("role") == "tool")


# --------------------------------------------------------------------------- #
# fixtures and readers
# --------------------------------------------------------------------------- #

@pytest.fixture(autouse=True)
def _no_proxy(monkeypatch):
    # The fake server is on 127.0.0.1; the proxy rules of the machine running
    # the tests must not decide whether these pass.
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
                "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("JOJO_DATA", str(tmp_path / "data"))
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "hello.py").write_text("print('hi')\n", encoding="utf-8")
    return proj


def make_agent(srv, model, project, approver=None, **cfg) -> Agent:
    cfg.setdefault("think", False)
    return Agent(Config(host=srv.url, model=model, cwd=str(project), **cfg), approver)


def shown(events) -> str:
    return "".join(e[1] for e in events if e[0] == "assistant")


def thought(events) -> str:
    return "".join(e[1] for e in events if e[0] == "thinking")


def started(events) -> list[str]:
    return [e[1] for e in events if e[0] == "tool_start"]


def errors(events) -> list[str]:
    return [e[1] for e in events if e[0] == "error"]


def finals(events) -> list[str]:
    return [e[1] for e in events if e[0] == "final"]


def assistant_msgs(agent) -> list[dict]:
    return [m for m in agent.messages if m["role"] == "assistant"]


def filtered(text: str, size: int):
    """Feed `text` in `size`-char deltas: (shown, calls, thinking, answer)."""
    f = StreamFilter(TOOLS)
    out = think = ""
    for i in range(0, len(text), size):
        out += f.feed(text[i:i + size])
        think += f.take_thinking()
    out += f.finish()
    think += f.take_thinking()
    return out, [(c["name"], c["arguments"]) for c in f.calls], think, f.answer


def through_filter(text: str):
    """Same result whatever the chunking — tokens split markers anywhere."""
    results = {n: filtered(text, n) for n in (1, 3, 4, max(1, len(text)))}
    first = results[1]
    for n, got in results.items():
        assert got == first, f"{n}-char deltas disagree: {got!r} vs {first!r}"
    return first


# --------------------------------------------------------------------------- #
# the stream filter: inline reasoning
# --------------------------------------------------------------------------- #

def test_a_call_inside_think_is_thinking_not_a_call():
    text = ('<think>I could run {"name": "run_bash", "arguments": {"command": "rm -rf ."}}\n'
            '{"name": "list_dir", "arguments": {"path": "."}}\n</think>\nThe answer is 42.')
    out, calls, think, answer = through_filter(text)
    assert calls == []
    assert "run_bash" in think and "list_dir" in think
    assert out == answer == "The answer is 42."


def test_a_bare_close_discards_the_calls_before_it():
    """The template opened <think> for the model: everything up to </think>
    was reasoning, so a call found in it was only considered."""
    text = ('I should look first.\n{"name": "list_dir", "arguments": {"path": "."}}\n'
            '</think>\nNo tool is needed: the answer is 42.')
    _, calls, _, answer = through_filter(text)
    assert calls == []
    assert answer == "No tool is needed: the answer is 42."


def test_think_in_the_middle_of_an_answer_is_just_text():
    text = "Models like deepseek-r1 wrap reasoning in <think> tags."
    assert through_filter(text)[:3] == (text, [], "")


# --------------------------------------------------------------------------- #
# the stream filter: what is a call and what is not
# --------------------------------------------------------------------------- #

NOT_CALLS = {
    "a tool definition (name + description + parameters)": (
        '{"name": "read_file", "description": "Read a file.", '
        '"parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}'),
    "an OpenAI-style tool definition": (
        '{"type": "function", "function": {"name": "write_file", "description": "Write.", '
        '"parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}'),
    "a definition without a description (a JSON schema as arguments)": (
        '{"name": "read_file", "parameters": {"type": "object", '
        '"properties": {"path": {"type": "string"}}, "required": ["path"]}}'),
    "a fenced definition": (
        'The tool looks like this:\n```json\n{"name": "run_bash", "description": "Run it.", '
        '"parameters": {"type": "object", "properties": {"command": {"type": "string"}}}}\n```\n'),
    "a log line": (
        '{"tool": "read_file", "input": {"path": "a.py"}, "ts": "2026-10-11T10:00:00Z"}'),
    "log lines, JSONL": (
        'From the log:\n'
        '{"tool": "run_bash", "input": {"command": "ls"}, "ts": 1760000000}\n'
        '{"tool": "write_file", "input": {"path": "x", "content": "y"}, "ts": 1760000001}\n'),
    "a Python literal holding bytes": "{'name': 'read_file', 'arguments': {'path': b'/etc/passwd'}}",
}


@pytest.mark.parametrize("text", NOT_CALLS.values(), ids=list(NOT_CALLS))
def test_lookalikes_are_not_run(text):
    out, calls, _, _ = through_filter(text)
    assert calls == []
    assert out == text


def test_bytes_literal_is_rejected_at_the_parser_too():
    # Accepted, it would have failed the *next* request: bytes do not json.dumps.
    info = tool_info(TOOLS)
    assert calls_in("{'name': 'read_file', 'arguments': {'path': b'x'}}", info) is None
    assert calls_in("{'name': 'read_file', 'arguments': {'path': 'x'}}", info) == [
        {"name": "read_file", "arguments": {"path": "x"}}]


PRETTY_A = '{\n  "name": "read_file",\n  "arguments": {\n    "path": "a.py"\n  }\n}'
PRETTY_B = '{\n  "name": "read_file",\n  "arguments": {\n    "path": "b.py"\n  }\n}'
BOTH = [("read_file", {"path": "a.py"}), ("read_file", {"path": "b.py"})]


@pytest.mark.parametrize("text", [
    f"Reading both:\n```json\n{PRETTY_A}\n{PRETTY_B}\n```\n",
    f"Reading both:\n```\n{PRETTY_A}\n\n{PRETTY_B}\n```\n",
    f"<tool_call>\n{PRETTY_A}\n{PRETTY_B}\n</tool_call>",
], ids=["json fence", "bare fence", "one tool_call tag"])
def test_two_pretty_printed_calls_in_one_block_both_run(text):
    out, calls, _, _ = through_filter(text)
    assert calls == BOTH
    assert '"name"' not in out


@pytest.mark.parametrize("text,path", [
    ('{"name": "write_file", "arguments": {"path": "C:\\Users\\me\\new.txt", "content": "hi"}}',
     "C:\\Users\\me\\new.txt"),
    ('<tool_call>{"name": "write_file", "arguments": {"path": "C:\\temp\\new.txt", '
     '"content": "hi"}}</tool_call>', "C:\\temp\\new.txt"),
    ('```json\n{"name": "write_file", "arguments": {"path": "D:\\proj\\bin\\run.txt", '
     '"content": "hi"}}\n```', "D:\\proj\\bin\\run.txt"),
], ids=["bare JSON (invalid \\U)", "tool_call tag (\\t and \\n)", "fence (\\b and \\r)"])
def test_windows_paths_with_single_backslashes(text, path):
    # The Python source above holds ONE backslash per separator, as the model wrote it.
    assert "\\\\" not in text
    _, calls, _, _ = through_filter(text)
    assert calls == [("write_file", {"path": path, "content": "hi"})]
    assert "\n" not in calls[0][1]["path"] and "\t" not in calls[0][1]["path"]


def test_repairing_a_windows_path_leaves_real_escapes_in_other_strings():
    text = ('{"name": "write_file", "arguments": {"path": "C:\\Users\\me\\new.txt", '
            '"content": "line one\\nline two\\n"}}')
    _, calls, _, _ = through_filter(text)
    assert calls == [("write_file", {"path": "C:\\Users\\me\\new.txt",
                                     "content": "line one\nline two\n"})]


def test_an_invented_tool_result_ends_the_reply():
    text = ('<tool_call>{"name": "list_dir", "arguments": {"path": "."}}</tool_call>\n'
            '<tool_result name="list_dir">\nsecrets.txt\n</tool_result>\n'
            'The folder holds secrets.txt, so I will now delete it.')
    out, calls, _, answer = through_filter(text)
    assert calls == [("list_dir", {"path": "."})]
    assert out == answer == ""


def test_a_tool_result_tag_mentioned_before_any_call_is_just_text():
    text = 'Results come back in <tool_result name="x"> blocks.'
    assert through_filter(text)[:2] == (text, [])


@pytest.mark.parametrize("fenced", [True, False], ids=["json fence", "bare JSON"])
def test_a_200kb_write_file_call_parses_quickly(fenced):
    content = "".join(f"line {i}: \"quoted\" \\ back\tslash — ü\n" for i in range(5600))
    assert len(content) > 200_000
    call = json.dumps({"name": "write_file", "arguments": {"path": "big.txt",
                                                            "content": content}})
    text = (f"Writing it.\n```json\n{call}\n```\nDone.\n" if fenced
            else f"Writing it.\n{call}\nDone.\n")
    f = StreamFilter(TOOLS)
    out = ""
    t0 = time.perf_counter()
    for i in range(0, len(text), 4):
        out += f.feed(text[i:i + 4])
    out += f.finish()
    elapsed = time.perf_counter() - t0
    assert [c["name"] for c in f.calls] == ["write_file"]
    assert f.calls[0]["arguments"]["content"] == content
    assert out == "Writing it.\nDone.\n"
    assert elapsed < 2.0, f"{len(text) // 1000} KB in 4-char deltas took {elapsed:.2f}s"


# --------------------------------------------------------------------------- #
# the agent: inline reasoning end to end
# --------------------------------------------------------------------------- #

def test_agent_streams_think_spans_as_thinking_and_never_runs_their_calls(project):
    asked = []

    def model(body):
        return pieces('<think>Maybe {"name": "run_bash", "arguments": '
                      '{"command": "touch pwned"}}\n</think>\nThe answer is 42.')

    with Ollama(FakeModel("r1:7b", CAPS_TOOLS, model)) as srv:
        agent = make_agent(srv, "r1:7b", project, lambda n, a: asked.append(n) or True)
        events = list(agent.run("what is the answer?"))
    assert "touch pwned" in thought(events)
    assert started(events) == [] and asked == []
    assert not (project / "pwned").exists()
    assert "<think>" not in shown(events) and "run_bash" not in shown(events)
    assert finals(events) == ["The answer is 42."]
    assert assistant_msgs(agent)[-1]["content"] == "The answer is 42."


def test_agent_history_keeps_only_the_answer_after_a_bare_close(project):
    def model(body):
        return pieces('I might list the folder.\n{"name": "list_dir", "arguments": {"path": "."}}\n'
                      '</think>\nNo need: the answer is 42.')

    with Ollama(FakeModel("hf.co/x/r1:q4", CAPS_TOOLS, model)) as srv:
        agent = make_agent(srv, "hf.co/x/r1:q4", project)
        events = list(agent.run("what is the answer?"))
        assert len(srv.requests) == 1                 # nothing ran, nothing went back
    assert started(events) == []
    last = assistant_msgs(agent)[-1]
    assert last["content"].strip() == "No need: the answer is 42."
    assert "tool_calls" not in last
    assert "might list" not in last["content"]


def test_agent_runs_both_calls_of_a_pretty_printed_fence(project):
    (project / "a.py").write_text("A = 1\n", encoding="utf-8")
    (project / "b.py").write_text("B = 2\n", encoding="utf-8")

    def model(body):
        if saw_tool_result(body):
            return pieces("Read both.")
        return pieces(f"```json\n{PRETTY_A}\n{PRETTY_B}\n```\n")

    with Ollama(FakeModel("qwen2.5-coder:7b", CAPS_TOOLS, model)) as srv:
        events = list(make_agent(srv, "qwen2.5-coder:7b", project).run("read a and b"))
    assert started(events) == ["read_file", "read_file"]
    ends = [e[2] for e in events if e[0] == "tool_end"]
    assert "A = 1" in ends[0] and "B = 2" in ends[1]
    assert shown(events) == "Read both."


def test_agent_does_not_show_an_invented_tool_result(project):
    """codellama has no tool support: text mode, with a stop sequence — and if
    the server ignores the stop, the filter still drops the made-up output."""
    def model(body):
        if saw_tool_result(body):
            return pieces(ANSWER)
        return pieces('<tool_call>{"name": "list_dir", "arguments": {"path": "."}}</tool_call>\n'
                      '<tool_result name="list_dir">\nsecrets.txt\n</tool_result>\n'
                      'There is secrets.txt.')

    with Ollama(FakeModel("codellama:7b", ["completion"], model)) as srv:
        events = list(make_agent(srv, "codellama:7b", project).run("what is here?"))
        assert all(r["options"].get("stop") == ["<tool_result"] for r in srv.requests)
    assert started(events) == ["list_dir"]
    assert "secrets" not in shown(events)
    assert shown(events) == ANSWER


# --------------------------------------------------------------------------- #
# the agent: Ctrl-C
# --------------------------------------------------------------------------- #

def test_ctrl_c_while_the_model_loads_ends_the_turn_promptly(project):
    model = FakeModel("big:70b", CAPS_TOOLS, lambda b: pieces(ANSWER))
    with Ollama(model, load_delay=5.0) as srv:
        agent = make_agent(srv, "big:70b", project)
        pressed = {}

        def ctrl_c():
            pressed["at"] = time.monotonic()
            agent.interrupt()

        timer = threading.Timer(0.3, ctrl_c)
        timer.start()
        try:
            events = list(agent.run("hello"))
        finally:
            timer.cancel()
        done = time.monotonic()
    assert "at" in pressed
    assert done - pressed["at"] < 1.5, f"the turn ended {done - pressed['at']:.2f}s after Ctrl-C"
    assert "interrupted" in errors(events)
    assert events[-1] == ("turn_end",)
    assert ANSWER not in shown(events)


def test_ctrl_c_after_a_text_call_was_parsed_does_not_run_it(project):
    asked = []

    def model(body):
        yield from pieces('{"name": "write_file", "arguments": {"path": "x.txt", '
                          '"content": "boom"}}\nNow let me check that it worked')
        for word in " and then some more words to keep the stream open".split():
            time.sleep(0.05)
            yield {"content": " " + word}

    with Ollama(FakeModel("qwen2.5-coder:7b", CAPS_TOOLS, model)) as srv:
        agent = make_agent(srv, "qwen2.5-coder:7b", project,
                           lambda n, a: asked.append(n) or True)
        events = []
        for ev in agent.run("write x.txt"):
            events.append(ev)
            if ev[0] == "assistant" and "Now" in shown(events):
                agent.interrupt()                  # the call line is behind us
    assert "interrupted" in errors(events)
    assert started(events) == [] and asked == []
    assert not (project / "x.txt").exists()
    assert not any(m.get("tool_calls") for m in agent.messages)
    assert not any(m["role"] == "tool" for m in agent.messages)
    # the words are kept, so the next turn knows what was said
    assert assistant_msgs(agent)[-1]["content"].startswith("Now")


def test_ctrl_c_during_a_batch_stops_the_rest_and_answers_every_call(project):
    asked = []

    def model(body):
        if tool_results(body):
            return pieces("Stopped.")
        return native(("write_file", {"path": "a.txt", "content": "a"}),
                      ("write_file", {"path": "b.txt", "content": "b"}),
                      ("run_bash", {"command": "echo c > c.txt"}))

    with Ollama(FakeModel("qwen3:8b", CAPS_TOOLS, model)) as srv:
        agent = make_agent(srv, "qwen3:8b", project, auto_edit=False)

        def approver(name, args):
            asked.append(name)
            agent.interrupt()                        # Ctrl-C at the prompt
            return False

        agent.approver = approver
        events = list(agent.run("make three files"))
        assert len(srv.requests) == 1
        assert asked == ["write_file"]
        assert started(events) == []
        assert "interrupted" in errors(events)
        for name in ("a.txt", "b.txt", "c.txt"):
            assert not (project / name).exists()

        msgs = agent.messages
        at = next(i for i, m in enumerate(msgs) if m.get("tool_calls"))
        assert len(msgs[at]["tool_calls"]) == 3
        answers = [m for m in msgs[at + 1:] if m["role"] == "tool"]
        assert [m["tool_name"] for m in answers] == ["write_file", "write_file", "run_bash"]
        assert answers[0]["content"] == "error: user denied this action"
        assert all("interrupted" in m["content"] for m in answers[1:])

        # and the history is still one a strict template accepts
        list(agent.run("never mind"))
        wire = srv.requests[-1]["messages"]
        at = next(i for i, m in enumerate(wire) if m.get("tool_calls"))
        assert [m["role"] for m in wire[at + 1:at + 4]] == ["tool"] * 3


# --------------------------------------------------------------------------- #
# the agent: what goes on the wire
# --------------------------------------------------------------------------- #

def test_gpt_oss_keeps_its_reasoning_between_tool_steps_only(project):
    def model(body):
        if tool_results(body):
            return [{"thinking": "Now I can answer."}] + pieces(ANSWER)
        return [{"thinking": "I should list the folder."}] + native(("list_dir", {"path": "."}))

    oss = FakeModel("gpt-oss:20b", CAPS_OSS, model, family="gptoss")
    with Ollama(oss) as srv:
        agent = make_agent(srv, "gpt-oss:20b", project, think="medium")
        events = list(agent.run("what is here?"))
        assert started(events) == ["list_dir"]
        call_msg = next(m for m in agent.messages if m.get("tool_calls"))
        assert call_msg["thinking"] == "I should list the folder."
        assert "thinking" not in assistant_msgs(agent)[-1]          # the answer
        # sent back with the tool result, so step two continues step one
        sent = next(m for m in srv.requests[1]["messages"] if m.get("tool_calls"))
        assert sent.get("thinking") == "I should list the folder."

        list(agent.run("thanks"))
        assert not any("thinking" in m for m in srv.requests[-1]["messages"])
        assert not any("thinking" in m for m in agent.messages)


@pytest.mark.parametrize("trained,configured,expected", [
    (4096, 32768, 4096),            # the model's window caps the request
    (131072, 32768, 32768),         # a bigger model window does not raise it
])
def test_num_ctx_is_capped_by_the_models_context_length(project, trained, configured, expected):
    with Ollama(FakeModel("m:1b", CAPS_TOOLS, lambda b: pieces(ANSWER)), context=trained) as srv:
        list(make_agent(srv, "m:1b", project, num_ctx=configured).run("hi"))
        assert srv.requests[0]["options"]["num_ctx"] == expected


@pytest.mark.parametrize("think", [False, "off"])
def test_thinking_off_on_gpt_oss_sends_low(project, think):
    oss = FakeModel("gpt-oss:20b", CAPS_OSS, lambda b: pieces(ANSWER), family="gptoss")
    with Ollama(oss) as srv:
        list(make_agent(srv, "gpt-oss:20b", project, think=think).run("hi"))
        assert srv.requests[0]["think"] == "low"


def test_only_text_mode_asks_for_the_tool_result_stop(project):
    with Ollama(FakeModel("codellama:7b", ["completion"], lambda b: pieces(ANSWER)),
                FakeModel("qwen2.5-coder:7b", CAPS_TOOLS, lambda b: pieces(ANSWER))) as srv:
        list(make_agent(srv, "codellama:7b", project).run("hi"))
        assert srv.requests[-1]["options"]["stop"] == ["<tool_result"]
        assert "tools" not in srv.requests[-1]
        list(make_agent(srv, "qwen2.5-coder:7b", project).run("hi"))
        assert "stop" not in srv.requests[-1]["options"]


def test_an_empty_reply_is_retried_once_with_a_nudge_then_reported(project):
    with Ollama(FakeModel("m:1b", CAPS_TOOLS, lambda b: [])) as srv:
        agent = make_agent(srv, "m:1b", project)
        events = list(agent.run("hello"))
        reqs = srv.requests
    assert len(reqs) == 2
    assert reqs[0]["messages"][-1] == {"role": "user", "content": "hello"}
    # the nudge rides on the last user turn: two user turns in a row is a
    # template error on strict servers ("roles must alternate")
    assert reqs[1]["messages"][-1]["role"] == "user"
    assert reqs[1]["messages"][-1]["content"].endswith(NUDGE)
    assert [m["role"] for m in reqs[1]["messages"]] == ["system", "user"]
    assert any("empty reply" in e for e in errors(events))
    assert not any(NUDGE in (m.get("content") or "") for m in agent.messages)
    assert assistant_msgs(agent) == []


def test_the_nudge_stays_off_the_record_when_the_retry_answers(project):
    def model(body):
        nudged = body["messages"][-1]["content"].endswith(NUDGE)
        return pieces(ANSWER) if nudged else []

    with Ollama(FakeModel("m:1b", CAPS_TOOLS, model)) as srv:
        agent = make_agent(srv, "m:1b", project)
        events = list(agent.run("hello"))
        assert finals(events) == [ANSWER]
        assert errors(events) == []
        n = len(srv.requests)
        list(agent.run("again"))
        nxt = srv.requests[n]["messages"]                  # the next turn's first request
        assert nxt[-1] == {"role": "user", "content": "again"}
        assert not any(m["content"] == NUDGE for m in nxt)
    assert not any(NUDGE in (m.get("content") or "") for m in agent.messages)


def test_long_histories_elide_the_oldest_tool_output_first(project):
    with Ollama(FakeModel("m:1b", CAPS_TOOLS, lambda b: pieces(ANSWER))) as srv:
        agent = make_agent(srv, "m:1b", project, num_ctx=4096)   # ~14 K chars of window
        calls = [{"function": {"name": "read_file", "arguments": {"path": f"f{i}.py"}}}
                 for i in range(5)]
        agent.messages += [{"role": "user", "content": "read all five"},
                           {"role": "assistant", "content": "", "tool_calls": calls}]
        agent.messages += [{"role": "tool", "tool_name": "read_file",
                            "content": f"{i}" * 3000} for i in range(5)]
        agent.messages.append({"role": "assistant", "content": "Read them."})
        events = list(agent.run("now summarise"))
        wire = srv.requests[0]["messages"]
    tools = [m["content"] for m in wire if m["role"] == "tool"]
    assert len(tools) == 5
    assert tools[0].startswith("[elided")
    assert tools[-2:] == ["3" * 3000, "4" * 3000]               # the two newest stay whole
    assert wire[0]["role"] == "system" and "JojoAI" in wire[0]["content"]
    assert [m["content"] for m in wire if m["role"] == "user"] == ["read all five",
                                                                    "now summarise"]
    budget = int(4096 * agent_mod.CHARS_PER_TOKEN * 0.75)
    size = sum(len(m.get("content") or "") + len(json.dumps(m.get("tool_calls", "")))
               for m in wire)
    assert size <= budget
    assert any(e[0] == "info" and "context window" in e[1] for e in events)


# --------------------------------------------------------------------------- #
# the agent: approvals and the prompt that describes them
# --------------------------------------------------------------------------- #

def stepper(*steps):
    """A native-calling model that takes `steps` in order, one per request."""
    def model(body):
        n = tool_results(body)
        return native(*steps[n]) if n < len(steps) else pieces("Done.")
    return model


def test_change_dir_does_not_carry_auto_edit_approval(project, tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    asked = []
    model = stepper([("write_file", {"path": "in.txt", "content": "in"})],
                    [("change_dir", {"path": str(other)})],
                    [("write_file", {"path": "out.txt", "content": "out"})])
    with Ollama(FakeModel("qwen3:8b", CAPS_TOOLS, model)) as srv:
        agent = make_agent(srv, "qwen3:8b", project,
                           lambda n, a: asked.append((n, a.get("path"))) or False)
        events = list(agent.run("write two files"))
    assert asked == [("write_file", "out.txt")]           # inside: no prompt; outside: asked
    assert (project / "in.txt").read_text(encoding="utf-8") == "in"
    assert not (other / "out.txt").exists()
    assert ("tool_denied", "write_file") in events
    assert agent.root == str(project) and agent.ctx.cwd == str(other)


def test_dotfiles_and_git_internals_inside_the_project_ask(project):
    risky = [".git/hooks/pre-commit", ".git/config", ".envrc",
             "sub/.vscode/tasks.json", ".github/workflows/ci.yml"]
    asked = []
    batch = [("write_file", {"path": p, "content": "x"}) for p in risky]
    batch += [("write_file", {"path": "src/ok.py", "content": "OK = 1\n"}),
              ("edit_file", {"path": "hello.py", "old_string": "hi", "new_string": "hey"})]
    with Ollama(FakeModel("qwen3:8b", CAPS_TOOLS, stepper(batch))) as srv:
        agent = make_agent(srv, "qwen3:8b", project,
                           lambda n, a: asked.append(a.get("path")) or False)
        list(agent.run("set things up"))
    assert asked == risky
    for p in risky:
        assert not (project / p).exists(), p
    assert (project / "src" / "ok.py").exists()
    assert "hey" in (project / "hello.py").read_text(encoding="utf-8")


@pytest.mark.skipif(not POSIX, reason="symlinks need privileges on Windows")
def test_a_symlink_out_of_the_project_asks(project, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (project / "link").symlink_to(outside, target_is_directory=True)
    asked = []
    model = stepper([("write_file", {"path": "link/evil.txt", "content": "x"})])
    with Ollama(FakeModel("qwen3:8b", CAPS_TOOLS, model)) as srv:
        list(make_agent(srv, "qwen3:8b", project,
                        lambda n, a: asked.append(a.get("path")) or False).run("go"))
    assert asked == ["link/evil.txt"]
    assert not (outside / "evil.txt").exists()


def test_the_system_prompt_on_the_wire_is_current(project, tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    model = stepper([("change_dir", {"path": str(other)})])
    with Ollama(FakeModel("qwen3:8b", CAPS_TOOLS, model)) as srv:
        agent = make_agent(srv, "qwen3:8b", project)
        list(agent.run("go elsewhere"))
        first, second = (r["messages"][0]["content"] for r in srv.requests[:2])
        assert f"Working directory: {project}\n" in first
        assert f"Working directory: {other}\n" in second
        assert "the user confirms every `run_bash` command" in second

        agent.cfg.auto_bash = True                         # /trust
        list(agent.run("and now?"))
        sysmsg = srv.requests[-1]["messages"][0]["content"]
        assert "`run_bash` commands run WITHOUT confirmation" in sysmsg
        assert "the user confirms every `run_bash`" not in sysmsg

        agent.cfg.auto_approve.add("*")                    # YOLO
        list(agent.run("and now?"))
        sysmsg = srv.requests[-1]["messages"][0]["content"]
        assert "(YOLO)" in sysmsg and "Nothing you do is reviewed" in sysmsg

        agent.cfg.auto_approve.clear()
        agent.cfg.auto_bash = False
        agent.cfg.auto_edit = False                        # --safe
        list(agent.run("and now?"))
        sysmsg = srv.requests[-1]["messages"][0]["content"]
        assert "the user confirms every file you create or edit" in sysmsg
        assert "the user confirms every `run_bash` command" in sysmsg
