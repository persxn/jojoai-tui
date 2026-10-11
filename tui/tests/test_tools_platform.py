"""The tools on Windows and Linux: text encoding, line endings, and commands.

What these pin down, each of which broke real use:

- File tools read and wrote in the *locale* encoding. On Windows that is
  cp1252: a UTF-8 source file's non-ASCII text came back garbled, and writing
  an emoji or any Hindi/CJK text failed with UnicodeEncodeError. The chat
  history did the same — and failed the whole turn on the user's own message.
- Text mode on Windows wrote "\\r\\n" for every "\\n": one edit_file turned an
  LF file into a whole-file diff.
- `bool("false")` is True, so replace_all="false" replaced everything.
- run_bash handed commands the user's terminal as stdin (a prompt hung the
  agent until the timeout), its timeout could not stop a command that left a
  child holding the pipe (a dev server hung it forever), and one undecodable
  byte of output became `error: UnicodeDecodeError`.
- `search` through ripgrep read JojoAI's own chat history, which the other
  tools refuse to touch.

The Windows-only behaviour cannot run here, so the encoding rule is also
checked statically: no text-mode open() in the package may rely on the locale.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "tui" / "jojocode_ai"
sys.path.insert(0, str(ROOT / "tui"))

from jojocode_ai import tools  # noqa: E402
from jojocode_ai.tools import Ctx, run_tool  # noqa: E402

POSIX = os.name == "posix"


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("JOJO_DATA", str(tmp_path / "data"))
    proj = tmp_path / "proj"
    proj.mkdir()
    return Ctx(cwd=str(proj))


# -- files ------------------------------------------------------------------ #

def test_write_and_read_are_utf8(ctx):
    text = "naïve → 🐱 नमस्ते 漢字\n"
    out = run_tool("write_file", {"path": "u.txt", "content": text}, ctx)
    assert out.startswith("created")
    assert (Path(ctx.cwd) / "u.txt").read_bytes() == text.encode("utf-8")
    assert "नमस्ते" in run_tool("read_file", {"path": "u.txt"}, ctx)


def test_edit_keeps_crlf_files_crlf(ctx):
    p = Path(ctx.cwd) / "w.txt"
    p.write_bytes(b"one\r\ntwo\r\nthree\r\n")
    out = run_tool("edit_file", {"path": "w.txt", "old_string": "one\ntwo",
                                 "new_string": "one\n1.5\ntwo"}, ctx)
    assert out.startswith("edited"), out
    assert p.read_bytes() == b"one\r\n1.5\r\ntwo\r\nthree\r\n"


def test_edit_keeps_lf_files_lf(ctx):
    p = Path(ctx.cwd) / "l.py"
    p.write_bytes(b"a = 1\nb = 2\n")
    run_tool("edit_file", {"path": "l.py", "old_string": "b = 2", "new_string": "b = 3"}, ctx)
    assert p.read_bytes() == b"a = 1\nb = 3\n"


def test_overwrite_keeps_the_files_line_endings(ctx):
    p = Path(ctx.cwd) / "c.bat"
    p.write_bytes(b"@echo off\r\necho hi\r\n")
    run_tool("write_file", {"path": "c.bat", "content": "@echo off\necho bye\n"}, ctx)
    assert p.read_bytes() == b"@echo off\r\necho bye\r\n"


def test_edit_does_not_mangle_bytes_that_are_not_utf8(ctx):
    p = Path(ctx.cwd) / "latin1.txt"
    p.write_bytes(b"caf\xe9\nkeep me\n")
    run_tool("edit_file", {"path": "latin1.txt", "old_string": "keep me",
                           "new_string": "kept"}, ctx)
    assert p.read_bytes() == b"caf\xe9\nkept\n"


def test_replace_all_false_as_a_string_means_false(ctx):
    p = Path(ctx.cwd) / "r.txt"
    p.write_text("x x x\n", encoding="utf-8")
    out = run_tool("edit_file", {"path": "r.txt", "old_string": "x", "new_string": "y",
                                 "replace_all": "false"}, ctx)
    assert "appears 3 times" in out
    assert p.read_text(encoding="utf-8") == "x x x\n"


def test_numbers_as_strings_or_null_are_accepted(ctx):
    (Path(ctx.cwd) / "n.txt").write_text("1\n2\n3\n", encoding="utf-8")
    out = run_tool("read_file", {"path": "n.txt", "offset": "2", "limit": None}, ctx)
    assert out.splitlines()[0].endswith("\t2")


def test_unknown_tool_names_the_real_ones(ctx):
    out = run_tool("list_files", {}, ctx)
    assert "unknown tool" in out and "list_dir" in out


# -- run_bash --------------------------------------------------------------- #

def test_commands_get_an_empty_stdin_not_the_terminal(tmp_path):
    """Run the tool in a child whose own stdin never reaches EOF — the way the
    TUI's terminal never does. A command reading stdin must not wait on it."""
    script = textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {str(ROOT / 'tui')!r})
        from jojocode_ai.tools import Ctx, run_tool
        t = time.time()
        out = run_tool("run_bash", {{"command": {sys.executable!r} + ' -c "import sys; print(len(sys.stdin.read()))"',
                                     "timeout": 12}}, Ctx(cwd={str(tmp_path)!r}))
        print(round(time.time() - t, 1), out.replace(chr(10), " | "))
    """)
    p = subprocess.Popen([sys.executable, "-c", script], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, text=True)
    try:
        p.wait(timeout=60)      # not communicate(): that would close stdin, and
        out = p.stdout.read()   # the point is a stdin that stays open, like a tty
    finally:
        p.kill()
        p.stdin.close()
        p.stdout.close()
    elapsed, _, result = out.strip().partition(" ")
    assert float(elapsed) < 8, out
    assert "0" in result and "[exit 0]" in result


@pytest.mark.skipif(not POSIX, reason="process groups; Windows uses taskkill /T")
def test_timeout_stops_children_that_hold_the_pipe(ctx):
    t = time.time()
    out = run_tool("run_bash", {"command": "sleep 60 & echo started; wait", "timeout": 1}, ctx)
    assert time.time() - t < 15, "the agent waited on a child the timeout should have killed"
    assert "timed out after 1s" in out and "started" in out


def test_undecodable_output_is_still_output(ctx):
    cmd = f'{sys.executable} -c "import sys; sys.stdout.buffer.write(bytes([255, 254]) + b\' ok\')"'
    out = run_tool("run_bash", {"command": cmd}, ctx)
    assert "ok" in out and "[exit 0]" in out and "UnicodeDecodeError" not in out


def test_timeout_in_milliseconds_is_capped(ctx, monkeypatch):
    # 120000 "seconds" from a model that meant milliseconds must not let one
    # hung command hold the agent for a day and a half
    seen = []
    monkeypatch.setattr(tools.shell, "run", lambda cmd, cwd, t: seen.append(t) or ("", "", 0, False))
    run_tool("run_bash", {"command": "true", "timeout": "120000"}, ctx)
    run_tool("run_bash", {"command": "true", "timeout": None}, ctx)
    assert seen == [3600, 120]


# -- search ----------------------------------------------------------------- #

@pytest.mark.parametrize("with_rg", [True, False], ids=["ripgrep", "python-walk"])
def test_search_never_reads_chat_history(ctx, tmp_path, monkeypatch, with_rg):
    import shutil

    if with_rg and not shutil.which("rg"):
        pytest.skip("ripgrep not installed")
    if not with_rg:
        monkeypatch.setattr(shutil, "which", lambda name: None)
    data = tmp_path / "data" / "sessions"
    data.mkdir(parents=True)
    (data / "old.jsonl").write_text('{"content": "PINEAPPLE secret"}\n', encoding="utf-8")
    (Path(ctx.cwd) / "code.py").write_text("PINEAPPLE = 1\n", encoding="utf-8")
    out = run_tool("search", {"pattern": "PINEAPPLE", "path": str(tmp_path)}, ctx)
    assert "code.py" in out
    assert "secret" not in out and "old.jsonl" not in out
    assert "not accessible" in run_tool("search", {"pattern": "x", "path": str(data)}, ctx)


# -- history store ---------------------------------------------------------- #

def test_history_store_round_trips_any_text(tmp_path, monkeypatch):
    monkeypatch.setenv("JOJO_DATA", str(tmp_path / "data"))
    from jojocode_ai.store import Store

    s = Store("http://127.0.0.1:9", rag_enabled=False)
    s._embed_ok = False                     # no Ollama here; history must not care
    s.append("user", "fix → this 🐛 कृपया")
    raw = Path(s.session_path).read_bytes()
    assert "🐛".encode("utf-8") in raw
    assert s.load_session(s.session_id)[0]["content"] == "fix → this 🐛 कृपया"


# -- the rule, statically --------------------------------------------------- #

def _text_mode(call: ast.Call) -> bool:
    mode = call.args[1] if len(call.args) > 1 else next(
        (k.value for k in call.keywords if k.arg == "mode"), None)
    return not (isinstance(mode, ast.Constant) and "b" in str(mode.value))


def _is_file_open(fn) -> bool:
    """open(...), io.open(...), os.fdopen(...) — not urllib's opener.open(...)."""
    if isinstance(fn, ast.Name):
        return fn.id == "open"
    if isinstance(fn, ast.Attribute):
        owner = getattr(fn.value, "id", "")
        return (fn.attr, owner) in (("open", "io"), ("fdopen", "os"))
    return False


def test_no_file_in_the_package_is_opened_in_the_locale_encoding():
    offenders = []
    for path in sorted(PKG.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            kws = {k.arg for k in node.keywords}
            if _is_file_open(node.func) and _text_mode(node) and "encoding" not in kws:
                offenders.append(f"{path.name}:{node.lineno}")
            if "text" in kws and "encoding" not in kws:
                offenders.append(f"{path.name}:{node.lineno} (subprocess text=True)")
    assert offenders == [], "no encoding= means cp1252 on Windows: " + ", ".join(offenders)
