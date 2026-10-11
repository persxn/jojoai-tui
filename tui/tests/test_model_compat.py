"""Models other than gpt-oss, end to end: the agent against a fake Ollama.

JojoAI was tested end to end only on gpt-oss. On anything else a fresh install
failed in one of three ways, all reproduced here against a server that
validates requests the way Ollama's routes.go does:

1. `think: "medium"` (the default) on a model without thinking — every
   qwen2.5-coder the installer recommends — is a 400: "does not support thinking".
2. With thinking off, qwen2.5-coder's tool call arrived as JSON *text*; the
   agent printed it as the answer and never ran the tool.
3. `tools` on a model without tool support (codellama, gemma, …) is a 400:
   "does not support tools". Every turn failed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tui"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_ollama import FakeModel, FakeOllama, pieces, saw_tool_result  # noqa: E402
from jojocode_ai.agent import Agent  # noqa: E402
from jojocode_ai.config import Config  # noqa: E402
from jojocode_ai.ollama_client import OllamaClient  # noqa: E402

CALL = '{"name": "list_dir", "arguments": {"path": "."}}'
ANSWER = "There is one file here: hello.py."


def lists_then_answers(call_text=CALL):
    def reply(body):
        return pieces(ANSWER) if saw_tool_result(body) else pieces(call_text)
    return reply


def native_call(body):
    if saw_tool_result(body):
        return pieces(ANSWER)
    return [{"tool_calls": [{"function": {"name": "list_dir", "arguments": {"path": "."}}}]}]


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("JOJO_DATA", str(tmp_path / "data"))
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "hello.py").write_text("print('hi')\n")
    return proj


def turn(srv, model, project, think="medium", text="what is here?"):
    cfg = Config(host=srv.url, model=model, cwd=str(project), think=think)
    agent = Agent(cfg)
    return agent, list(agent.run(text))


def shown(events):
    """Everything the user would read as Jojo's words."""
    out = "".join(e[1] for e in events if e[0] == "assistant")
    for e in events:
        if e[0] == "final" and e[1] and not (len(e) > 2 and e[2]):
            out += e[1]
    return out


def errors(events):
    return [e[1] for e in events if e[0] == "error"]


# --------------------------------------------------------------------------- #

def test_qwen_coder_json_call_runs_instead_of_being_printed(project):
    qwen = FakeModel("qwen2.5-coder:7b", ["completion", "tools", "insert"], lists_then_answers())
    with FakeOllama(qwen) as srv:
        _, events = turn(srv, "qwen2.5-coder:7b", project)
    assert errors(events) == []
    assert [e[1] for e in events if e[0] == "tool_start"] == ["list_dir"]
    assert "hello.py" in next(e[2] for e in events if e[0] == "tool_end")
    assert shown(events) == ANSWER
    assert '"name"' not in shown(events)


def test_default_think_is_not_sent_to_a_model_that_cannot_think(project):
    qwen = FakeModel("qwen2.5-coder:7b", ["completion", "tools"], lists_then_answers())
    with FakeOllama(qwen) as srv:
        _, events = turn(srv, "qwen2.5-coder:7b", project, think="medium")
        assert all("think" not in r for r in srv.requests)
    assert not any("does not support thinking" in e for e in errors(events))


def test_other_thinking_models_get_a_boolean(project):
    qwen3 = FakeModel("qwen3:8b", ["completion", "tools", "thinking"], native_call, family="qwen3")
    with FakeOllama(qwen3) as srv:
        turn(srv, "qwen3:8b", project, think="high")
        assert srv.requests[0]["think"] is True
        srv.requests.clear()
        turn(srv, "qwen3:8b", project, think=False)
        # Ollama thinks by default for a capable model, so "off" is said out loud
        assert srv.requests[0]["think"] is False


def test_gpt_oss_still_gets_its_reasoning_level(project):
    oss = FakeModel("gpt-oss:20b", ["completion", "tools", "thinking"], native_call, family="gptoss")
    with FakeOllama(oss) as srv:
        _, events = turn(srv, "gpt-oss:20b", project, think="medium")
        assert srv.requests[0]["think"] == "medium"
        assert srv.requests[0]["tools"]
    assert shown(events) == ANSWER


def test_a_model_without_tools_works_through_the_prompt(project):
    seen = []

    def codellama(body):
        seen.append(body)
        if saw_tool_result(body):
            return pieces(ANSWER)
        return pieces('<tool_call>{"name": "list_dir", "arguments": {"path": "."}}</tool_call>')

    with FakeOllama(FakeModel("codellama:7b", ["completion", "insert"], codellama)) as srv:
        _, events = turn(srv, "codellama:7b", project, think=False)
        assert all("tools" not in r for r in srv.requests)
    assert errors(events) == []
    assert shown(events) == ANSWER
    assert [e[1] for e in events if e[0] == "tool_start"] == ["list_dir"]
    # the protocol is in the system prompt, and the result came back as a
    # user turn — these templates have no slot for a `tool` message
    assert "<tool_call>" in seen[0]["messages"][0]["content"]
    second = seen[1]["messages"]
    assert not any(m["role"] == "tool" for m in second)
    assert '<tool_result name="list_dir">' in second[-1]["content"]
    assert second[-2]["role"] == "assistant" and "<tool_call>" in second[-2]["content"]


def test_old_ollama_without_capabilities_learns_from_the_400s(project):
    """Before /api/show listed capabilities the client cannot ask — so the
    refusals themselves must steer it, within the same turn."""
    def model(body):
        return pieces(ANSWER) if saw_tool_result(body) else pieces(CALL)

    with FakeOllama(FakeModel("codellama:7b", ["completion"], model),
                    show_capabilities=False) as srv:
        _, events = turn(srv, "codellama:7b", project, think="medium")
        sent = srv.requests
    assert errors(events) == []
    assert shown(events) == ANSWER
    assert "think" in sent[0] and "think" not in sent[-1]
    assert "tools" not in sent[-1]


def test_finish_summary_is_shown_even_after_streamed_prose(project):
    def model(body):
        if saw_tool_result(body):
            return pieces('All done.\n{"name": "finish", "arguments": {"message": "Listed the folder."}}')
        return pieces(CALL)

    with FakeOllama(FakeModel("m:1b", ["completion", "tools"], model)) as srv:
        _, events = turn(srv, "m:1b", project)
    final = [e for e in events if e[0] == "final"]
    assert final == [("final", "Listed the folder.", False)]
    assert not any(e[0] == "tool_start" and e[1] == "finish" for e in events)


def test_finish_reports_its_own_message_not_the_last_tools_output(project):
    def model(body):
        return pieces('{"name": "finish", "arguments": {"message": "Done."}}\n' + CALL)

    with FakeOllama(FakeModel("m:1b", ["completion", "tools"], model)) as srv:
        _, events = turn(srv, "m:1b", project)
    assert [e[1] for e in events if e[0] == "final"] == ["Done."]


def test_errors_read_as_sentences_not_json(project):
    with FakeOllama() as srv:          # no models: everything is a 404
        _, events = turn(srv, "missing:7b", project)
    assert errors(events)
    assert all('{"error"' not in e for e in errors(events))
    assert "not found" in errors(events)[0]


def test_has_model_needs_the_exact_tag():
    # a pulled :7b used to satisfy a check for :14b, which then failed at chat time
    from jojocode_ai.ollama_client import same_model

    with FakeOllama(FakeModel("qwen2.5-coder:7b", ["completion"], lambda b: []),
                    FakeModel("llama3.2:latest", ["completion"], lambda b: [])) as srv:
        c = OllamaClient(srv.url, "x")
        assert c.has_model("qwen2.5-coder:7b")
        assert not c.has_model("qwen2.5-coder:14b")
        assert c.has_model("llama3.2")
    assert same_model("hf.co/org/model", "hf.co/org/model:latest")


def test_text_mode_history_is_rendered_for_the_template(project):
    agent = Agent(Config(host="http://127.0.0.1:9", model="x", cwd=str(project)))
    agent.messages += [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"function": {"name": "list_dir", "arguments": {}}},
                        {"function": {"name": "read_file", "arguments": {"path": "a"}}}]},
        {"role": "tool", "tool_name": "list_dir", "content": "a"},
        {"role": "tool", "tool_name": "read_file", "content": "1\tx"},
    ]
    wire = agent._wire(text_mode=True)
    assert [m["role"] for m in wire] == ["system", "user", "assistant", "user"]
    assert wire[2]["content"].count("<tool_call>") == 2
    json.loads(wire[2]["content"].split("<tool_call>")[1].split("</tool_call>")[0])
    assert wire[3]["content"].count("<tool_result") == 2
    assert agent._wire(text_mode=False) is agent.messages
