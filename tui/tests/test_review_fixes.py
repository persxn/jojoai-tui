"""Findings of the adversarial review, pinned.

A review of the rewritten client (one reviewer per area, each finding
reproduced before it was fixed) turned up these. Each test is the reviewer's
own failing case, so the fix cannot quietly come undone:

- **A YAML file written as `x:\\n  y: 1` got literal backslash-n on disk.** The
  Windows-path repair ran before the plain JSON parse and decided any string
  starting "letter, colon, backslash" was a path. Valid JSON is now taken at
  its word; a path argument is fixed after parsing, by its key.
- **Prose about `<think>…</think>` lost its answer and its calls.**
- **Two user turns in a row** (a turn that produced nothing, then the next
  one, or the empty-reply nudge) — a template error on strict servers.
- **The prompt said "the user confirms every command" after "always allow".**
- **A reply cut off by the length limit showed half a call's JSON.**
- **Auto-edit followed a symlink into .git**, and new files were made 0600,
  read-only files were silently replaced, `write_file` claimed to convert line
  endings it kept, the Windows tool schema advertised `&` for background jobs,
  ripgrep and the Python search disagreed about .gitignore'd files, a
  `rg.cmd` shim could turn a search pattern into a command, a lone `\\r` split
  lines edit_file could not match, a BOM-only file read as an error, a search
  through a file symlink read JojoAI's credentials, and one minified line was
  read whole into memory.
- **fetch_url ran without asking** although a URL can carry file contents out.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tui"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_ollama import FakeModel, FakeOllama, pieces  # noqa: E402
from jojocode_ai import (openai_client, policy, prompts, shell, textfiles,  # noqa: E402
                         toolcalls, tools, transport)
from jojocode_ai.agent import NUDGE, Agent  # noqa: E402
from jojocode_ai.config import Config  # noqa: E402
from jojocode_ai.tools import Ctx, run_tool  # noqa: E402

INFO = toolcalls.tool_info(tools.TOOLS)
POSIX = os.name == "posix"


@pytest.fixture(autouse=True)
def _private(tmp_path, monkeypatch):
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
                "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("JOJO_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("JOJO_CONFIG", str(tmp_path / "cfg"))


@pytest.fixture
def proj(tmp_path):
    p = tmp_path / "proj"
    p.mkdir()
    return p


def _calls(text: str):
    f = toolcalls.StreamFilter(tools.TOOLS)
    shown = f.feed(text) + f.finish()
    return shown, f.calls


# --------------------------------------------------------------------------- #
# text tool calls
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("content", ["x:\n  y: 1\n  z: 2\n", "a:\n\tcc a.c\n", "Q:\nWhat?\n"])
def test_valid_json_is_taken_at_its_word(content):
    text = json.dumps({"name": "write_file", "arguments": {"path": "f.yml", "content": content}})
    assert toolcalls.calls_in(text, INFO)[0]["arguments"]["content"] == content
    shown, calls = _calls(text)
    assert shown == "" and calls[0]["arguments"]["content"] == content


@pytest.mark.parametrize("raw,path", [
    (r'{"name": "read_file", "arguments": {"path": "C:\temp\new.txt"}}', "C:\\temp\\new.txt"),
    (r'{"name": "read_file", "arguments": {"path": "C:\Users\me\new.txt"}}', "C:\\Users\\me\\new.txt"),
])
def test_a_windows_path_written_raw_is_still_a_path(raw, path):
    assert toolcalls.calls_in(raw, INFO)[0]["arguments"]["path"] == path


def test_escaped_backslashes_in_a_multi_call_block_parse():
    block = "```json\n" + json.dumps({"name": "search", "arguments": {
        "pattern": "def \\w+", "path": "C:\\Users\\me"}}, indent=1) + "\n" + json.dumps(
        {"name": "list_dir", "arguments": {"path": "."}}, indent=1) + "\n```"
    shown, calls = _calls(block)
    assert shown == ""
    assert [c["name"] for c in calls] == ["search", "list_dir"]
    assert calls[0]["arguments"] == {"pattern": "def \\w+", "path": "C:\\Users\\me"}


def test_prose_about_think_tags_is_kept_whole():
    text = "Reasoning models wrap thoughts in <think>...</think> tags. Strip them."
    f = toolcalls.StreamFilter(tools.TOOLS)
    shown = "".join(f.feed(text[i:i + 3]) for i in range(0, len(text), 3)) + f.finish()
    assert shown == text and f.answer == text


def test_a_reply_cut_off_mid_call_shows_no_half_json():
    f = toolcalls.StreamFilter(tools.TOOLS)
    shown = f.feed('Writing it.\n{"name": "write_file", "arguments": {"path": "a.py", "content": "imp')
    shown += f.finish(cut=True)
    assert shown.strip() == "Writing it." and f.calls == []


def test_xml_parameters_keep_a_files_final_newline():
    text = '<function=write_file><parameter=path>\na.py\n</parameter><parameter=content>\nprint("hi")\n\n</parameter></function>'
    calls = toolcalls.calls_in(text, INFO)
    assert calls[0]["arguments"] == {"path": "a.py", "content": 'print("hi")\n'}


# --------------------------------------------------------------------------- #
# the agent
# --------------------------------------------------------------------------- #

def test_a_turn_after_one_that_got_no_answer_does_not_send_user_twice(proj):
    replies = iter([[], [], pieces("Hello.")])
    with FakeOllama(FakeModel("m:1b", ["completion", "tools"], lambda b: next(replies))) as srv:
        agent = Agent(Config(host=srv.url, model="m:1b", cwd=str(proj), think=False))
        list(agent.run("first"))                     # empty, nudged, empty again
        list(agent.run("second"))
        last = srv.requests[-1]["messages"]
    roles = [m["role"] for m in last]
    assert all(not (a == b == "user") for a, b in zip(roles, roles[1:])), roles
    assert "first" in last[-1]["content"] and last[-1]["content"].endswith("second")


def test_retrying_the_same_words_replaces_the_unanswered_turn(proj):
    replies = iter([[], [], pieces("Hello.")])
    with FakeOllama(FakeModel("m:1b", ["completion", "tools"], lambda b: next(replies))) as srv:
        agent = Agent(Config(host=srv.url, model="m:1b", cwd=str(proj), think=False))
        list(agent.run("hi there"))
        list(agent.run("hi there"))
        last = srv.requests[-1]["messages"]
    assert [m["content"] for m in last if m["role"] == "user"] == ["hi there"]
    assert NUDGE not in json.dumps([m for m in agent.messages])


def test_the_prompt_tracks_always_allow():
    cfg = Config(host="http://127.0.0.1:1", model="m")
    assert "confirms every `run_bash`" in prompts.system_prompt("/p", cfg)
    cfg.auto_approve.add("run_bash")
    assert "WITHOUT confirmation" in prompts.system_prompt("/p", cfg)


def test_context_fitting_counts_reasoning_and_drops_old_reasoning_first(proj):
    with FakeOllama(FakeModel("m:1b", ["completion", "tools"], lambda b: pieces("ok"))) as srv:
        agent = Agent(Config(host=srv.url, model="m:1b", cwd=str(proj), num_ctx=4096))
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
        for i in range(6):
            msgs.append({"role": "assistant", "content": "", "thinking": "t" * 3000,
                         "tool_calls": [{"function": {"name": "list_dir", "arguments": {}}}]})
            msgs.append({"role": "tool", "tool_name": "list_dir", "content": "x" * 100})
        assert agent._fit_history(msgs) is True
    kept = [m for m in msgs if m.get("thinking")]
    assert len(kept) <= 1 and msgs[1]["content"] == "u"


# --------------------------------------------------------------------------- #
# clients and transport
# --------------------------------------------------------------------------- #

def test_a_proxy_without_a_port_is_port_80_as_urllib_has_it(monkeypatch):
    monkeypatch.setattr(transport, "_proxy_for", lambda scheme, host: "http://proxy.example")
    conn, target, _ = transport._connect("http://gpu-box.lan:11434/api/chat", 5)
    assert (conn.host, conn.port) == ("proxy.example", 80)
    assert target == "http://gpu-box.lan:11434/api/chat"


@pytest.mark.parametrize("host,base", [
    ("http://127.0.0.1:1234", "http://127.0.0.1:1234/v1"),
    ("http://127.0.0.1:1234/v1", "http://127.0.0.1:1234/v1"),
    ("https://generativelanguage.googleapis.com/v1beta/openai",
     "https://generativelanguage.googleapis.com/v1beta/openai"),
])
def test_openai_base_is_used_as_given_when_it_has_a_path(host, base):
    assert openai_client.OpenAIClient(host, "m").base == base


def test_a_dead_ollama_is_probed_once_not_on_every_call(monkeypatch):
    from jojocode_ai import ollama_client
    c = ollama_client.OllamaClient("http://127.0.0.1:9", "m")
    n = []

    def boom(*a, **k):
        n.append(1)
        raise transport.TransportError("refused")
    monkeypatch.setattr(c, "_post", boom)
    for _ in range(4):
        c.capabilities()
    assert len(n) == 1


# --------------------------------------------------------------------------- #
# files, search, commands, approvals
# --------------------------------------------------------------------------- #

@pytest.mark.skipif(not POSIX, reason="symlinks")
def test_auto_edit_does_not_follow_a_link_into_git(proj):
    (proj / ".git" / "hooks").mkdir(parents=True)
    os.symlink(proj / ".git" / "hooks", proj / "hooks")
    assert policy.auto_write_ok(str(proj / "hooks" / "pre-commit"), str(proj)) is False
    assert policy.auto_write_ok(str(proj / "src.py"), str(proj)) is True


def test_fetch_url_asks_like_web_search():
    assert {"web_search", "fetch_url"} <= tools.APPROVAL_REQUIRED
    assert "fetch_url" not in policy.READ_ONLY


@pytest.mark.skipif(not POSIX, reason="POSIX modes")
def test_a_new_file_gets_the_umask_mode_and_a_read_only_one_is_refused(proj):
    old = os.umask(0o022)
    try:
        textfiles._UMASK = 0o022
        new = proj / "new.txt"
        textfiles.write_atomic(str(new), b"x")
        assert stat.S_IMODE(os.stat(new).st_mode) == 0o644
        ro = proj / "ro.txt"
        ro.write_text("keep", encoding="utf-8")
        os.chmod(ro, 0o444)
        with pytest.raises(PermissionError):
            textfiles.write_atomic(str(ro), b"gone")
        assert ro.read_text(encoding="utf-8") == "keep"
        assert sorted(os.listdir(proj)) == ["new.txt", "ro.txt"]     # no temp left behind
    finally:
        os.umask(old)
        textfiles._UMASK = textfiles._read_umask()


def test_write_file_says_what_it_kept_and_honours_explicit_crlf(proj):
    f = proj / "run.sh"
    f.write_bytes(b"a\r\nb\r\n")
    out = run_tool("write_file", {"path": "run.sh", "content": "a\nb\n"}, Ctx(cwd=str(proj)))
    assert "kept the file's CRLF line endings" in out
    run_tool("write_file", {"path": "x.bat", "content": "@echo off\r\necho hi\r\n"}, Ctx(cwd=str(proj)))
    assert (proj / "x.bat").read_bytes() == b"@echo off\r\necho hi\r\n"


def test_the_windows_schema_does_not_promise_ampersand_backgrounding():
    assert "&" not in tools._background_note(True).replace('start ""', "")
    assert 'start "" /b' in tools._background_note(True)
    assert "'&'" in tools._background_note(False)


def test_rg_from_a_cmd_shim_is_never_used(tmp_path, monkeypatch):
    d = tmp_path / "bin"
    d.mkdir()
    for name in ("rg.cmd", "rg.bat"):
        p = d / name
        p.write_text("@echo off", encoding="utf-8")
        p.chmod(0o755)
    monkeypatch.setattr(tools, "IS_WINDOWS", True)
    monkeypatch.setenv("PATH", str(d))
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    assert tools._which_rg() is None


@pytest.mark.parametrize("engine", ["ripgrep", "python-walk"])
def test_both_search_engines_see_gitignored_files(proj, monkeypatch, engine):
    if engine == "ripgrep" and tools._which_rg() is None:
        pytest.skip("ripgrep not installed")
    if engine == "python-walk":
        monkeypatch.setattr(tools, "_which_rg", lambda: None)
    (proj / ".gitignore").write_text(".env\n", encoding="utf-8")
    (proj / ".git").mkdir()
    (proj / ".env").write_text("DATABASE_URL=postgres://x\n", encoding="utf-8")
    (proj / "node_modules").mkdir()
    (proj / "node_modules" / "m.js").write_text("DATABASE_URL\n", encoding="utf-8")
    out = run_tool("search", {"pattern": "DATABASE_URL"}, Ctx(cwd=str(proj)))
    assert ".env:1:" in out and "node_modules" not in out


def test_a_lone_cr_does_not_split_lines_and_a_bom_only_file_is_empty(proj):
    (proj / "mac.txt").write_bytes(b"alpha\rbeta\ngamma\n")
    out = run_tool("read_file", {"path": "mac.txt"}, Ctx(cwd=str(proj)))
    assert "     1\talpha\rbeta" in out and "     2\tgamma" in out
    (proj / "bom.txt").write_bytes(b"\xef\xbb\xbf")
    assert run_tool("read_file", {"path": "bom.txt"}, Ctx(cwd=str(proj))) == "(empty file)"


def test_one_huge_line_is_shown_bounded(proj):
    (proj / "min.js").write_text("x" * 2_000_000 + "\nnext\n", encoding="utf-8")
    out = run_tool("read_file", {"path": "min.js"}, Ctx(cwd=str(proj)))
    assert "line continues" in out and "     2\tnext" in out
    assert len(out) < tools.MAX_OUTPUT + 200


@pytest.mark.skipif(not POSIX, reason="symlinks")
def test_the_python_search_does_not_follow_a_link_into_jojos_data(proj, tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "_which_rg", lambda: None)
    cfg = tmp_path / "cfg"
    cfg.mkdir(exist_ok=True)
    (cfg / "credentials").write_text("SECRET-TOKEN\n", encoding="utf-8")
    os.symlink(cfg / "credentials", proj / "creds.txt")
    out = run_tool("search", {"pattern": "SECRET"}, Ctx(cwd=str(proj)))
    assert "SECRET-TOKEN" not in out


def test_a_utf16_read_cut_inside_a_surrogate_pair_still_decodes():
    import codecs
    data = codecs.BOM_UTF16_LE + ("hello world " * 20 + "\U0001F600").encode("utf-16-le")
    tf = textfiles.decode(data[:-2], partial=True)          # half of the last pair
    assert tf is not None and tf.encoding == "utf-16-le"
    assert tf.text == "hello world " * 20 and "\x00" not in tf.text


def test_git_never_opens_an_editor_for_a_command(proj):
    r = shell.run('git var GIT_EDITOR' if shell.IS_WINDOWS else 'echo "$GIT_EDITOR"', str(proj), 10)
    assert r.out.strip() == "true"
