"""Tool calls written as text: found, executed, and never shown to the user.

The bug: qwen2.5-coder — the installer's pick for most machines — answers a
task with `{"name": "list_dir", "arguments": {"path": "."}}` in its *content*
instead of through Ollama's tool_calls field. The agent took that for the
final answer and printed the raw JSON at the user; the tool never ran. Other
models fence it, tag it, or prefix it. These tests pin every shape we read,
and — just as important — the look-alikes we must leave alone.

Every case is fed whole, three characters at a time and one at a time: real
tokens split `<tool_call>` and `{"` wherever they like.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tui"))

from jojocode_ai.toolcalls import StreamFilter, calls_in, tool_info  # noqa: E402
from jojocode_ai.tools import TOOLS  # noqa: E402


def run(text: str, size: int):
    f = StreamFilter(TOOLS)
    shown = ""
    for i in range(0, len(text), size):
        shown += f.feed(text[i:i + size])
    shown += f.finish()
    return shown, [(c["name"], c["arguments"]) for c in f.calls]


def check(text: str):
    """Same answer whatever the chunking; returns it."""
    results = {size: run(text, size) for size in (1, 3, max(1, len(text)))}
    first = results[1]
    for size, got in results.items():
        assert got == first, f"chunk size {size} disagrees: {got} vs {first}"
    return first


CALLS = {
    "qwen2.5-coder bare JSON (the reported bug)": (
        '{"name": "list_dir", "arguments": {"path": "."}}',
        "", [("list_dir", {"path": "."})]),
    "prose, then the call on its own line": (
        'Let me look.\n{"name": "read_file", "arguments": {"path": "a.py"}}\n',
        "Let me look.\n", [("read_file", {"path": "a.py"})]),
    "pretty-printed over several lines": (
        '{\n  "name": "read_file",\n  "arguments": {\n    "path": "a.py"\n  }\n}',
        "", [("read_file", {"path": "a.py"})]),
    "fenced as json": (
        'I will read it:\n```json\n{"name": "read_file", "arguments": {"path": "a.py"}}\n```\n',
        "I will read it:\n", [("read_file", {"path": "a.py"})]),
    "hermes/qwen <tool_call> tags": (
        '<tool_call>\n{"name": "run_bash", "arguments": {"command": "ls"}}\n</tool_call>',
        "", [("run_bash", {"command": "ls"})]),
    "tag in the middle of a line": (
        'Sure.<tool_call>{"name": "list_dir", "arguments": {}}</tool_call>',
        "Sure.", [("list_dir", {})]),
    "tag never closed": (
        '<tool_call>{"name": "list_dir", "arguments": {"path": "src"}}',
        "", [("list_dir", {"path": "src"})]),
    "one call per line": (
        '{"name": "list_dir", "arguments": {}}\n{"name": "read_file", "arguments": {"path": "x"}}',
        "", [("list_dir", {}), ("read_file", {"path": "x"})]),
    "array of calls, parameters spelling": (
        '[{"name": "list_dir", "arguments": {}}, {"name": "search", "parameters": {"pattern": "TODO"}}]',
        "", [("list_dir", {}), ("search", {"pattern": "TODO"})]),
    "openai shape with string arguments": (
        '{"type": "function", "function": {"name": "read_file", "arguments": "{\\"path\\": \\"x\\"}"}}',
        "", [("read_file", {"path": "x"})]),
    "python dict literal": (
        "{'name': 'read_file', 'arguments': {'path': 'x'}}",
        "", [("read_file", {"path": "x"})]),
    "mistral [TOOL_CALLS] array": (
        '[TOOL_CALLS][{"name": "list_dir", "arguments": {"path": "src"}}]',
        "", [("list_dir", {"path": "src"})]),
    "mistral [TOOL_CALLS]name[ARGS]": (
        '[TOOL_CALLS]read_file[ARGS]{"path": "a.py"}',
        "", [("read_file", {"path": "a.py"})]),
    "qwen3-coder xml parameters": (
        '<tool_call>\n<function=read_file>\n<parameter=path>\na.py\n</parameter>\n</function>\n</tool_call>',
        "", [("read_file", {"path": "a.py"})]),
    "llama 3.1 <function=...>": (
        '<function=list_dir>{"path": "."}</function>',
        "", [("list_dir", {"path": "."})]),
    "arguments spilled into the top level": (
        '{"name": "read_file", "path": "a.py"}',
        "", [("read_file", {"path": "a.py"})]),
    "react action / action_input string": (
        '{"action": "run_bash", "action_input": "git status"}',
        "", [("run_bash", {"command": "git status"})]),
    "a misremembered tool name still goes to the model as an error": (
        '{"name": "list_files", "arguments": {"dir": "."}}',
        "", [("list_files", {"dir": "."})]),
}


@pytest.mark.parametrize("text,shown,calls", CALLS.values(), ids=list(CALLS))
def test_calls_are_lifted_out(text, shown, calls):
    assert check(text) == (shown, calls)


NOT_CALLS = {
    "a JSON config the user asked for": (
        'Here is your config:\n```json\n{"name": "my-app", "version": "1.0.0"}\n```\n'),
    "package.json-ish bare object naming a real tool": '{"name": "search", "version": "2.0"}',
    "a fenced example of an unknown tool": (
        '```json\n{"name": "get_weather", "arguments": {"city": "Paris"}}\n```'),
    "python with dict literals": 'Try:\n```python\nd = {"a": 1}\nprint(d)\n```\nDone.',
    "markdown link and set notation": "[docs](https://x.y) are here.\n{x | x > 0} is a set.\nOK",
    "angle brackets that are not tags": "if a < b and c <tool not a tag",
    "plain prose": "The folder has one file: hello.py.",
    "unterminated JSON": 'Result:\n{"name": "read_file", "arguments": {"path": "a',
    "a JSON array of data": "[1, 2, 3]",
    "empty fence": "```\n```",
}


@pytest.mark.parametrize("text", NOT_CALLS.values(), ids=list(NOT_CALLS))
def test_lookalikes_are_shown_untouched(text):
    assert check(text) == (text, [])


def test_a_json_costume_answer_is_unwrapped():
    # some models answer "hi" with {"response": "..."} when tools are present
    assert check('{"response": "Hello! How can I help?"}') == ("Hello! How can I help?", [])


def test_prose_streams_before_the_call_is_complete():
    """Holding back must not mean buffering everything: prose shows at once."""
    f = StreamFilter(TOOLS)
    assert f.feed("Reading the file now.\n") == "Reading the file now.\n"
    assert f.feed('{"name": "read_file", ') == ""
    assert f.feed('"arguments": {"path": "a"}}') == ""
    assert f.finish() == ""
    assert f.calls == [{"name": "read_file", "arguments": {"path": "a"}}]


def test_code_fences_stream_line_by_line():
    f = StreamFilter(TOOLS)
    assert f.feed("```python\n") == "```python\n"
    assert f.feed("x = 1\n") == "x = 1\n"


def test_calls_in_rejects_mixed_lists():
    info = tool_info(TOOLS)
    assert calls_in('[{"name": "list_dir", "arguments": {}}, 7]', info) is None
    assert calls_in('{"name": "list_dir", "arguments": [1, 2]}', info) is None
