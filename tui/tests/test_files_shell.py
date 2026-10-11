"""Files, commands and the approval policy, after the bug-fix pass.

What these pin down — each one lost a user's data, hung the agent, or let a
write skip the prompt it should have asked for:

- **Encodings.** Files were read as UTF-8 whatever they were. A UTF-16 file
  (PowerShell 5.1's `>`, .reg, .rc) came back NUL-padded and edit_file could
  never match it; a cp1252 file came back full of U+FFFD, and a remote
  read-then-write replaced every accented letter with it for good. Now the
  encoding is detected and every write goes back in the same one, BOM and all
  (textfiles.py).
- **A failed write emptied the file.** open(path, "w") truncates before a byte
  is encoded, so a lone surrogate or a character cp1252 cannot hold left an
  empty file. Bytes are encoded first and swapped in atomically, keeping the
  permission bits and writing *through* a symlink rather than replacing it.
- **Mixed line endings** were normalised by any edit — a whole-file diff.
- **Model-shaped arguments.** content=null wrote "None"; limit=-1, offsets
  past the end, "~/x" (which created a directory called "~") and the like.
- **Listing and search.** One dangling symlink failed a whole listing; search
  skipped .github, searched .git, ran an `rg` shipped in the repository, and
  reported a look-around pattern as an error instead of using Python's re.
- **JojoAI's own credentials** are never a tool's business (_blocked).
- **Commands** (shell.py): a background job held the agent until the timeout
  and was then killed; 10 MB of output was buffered whole; Ctrl-C could not
  stop a command; cmd.exe silently ran only the first line of a multi-line
  command, or ran in C:\\Windows when started in a UNC directory.
- **Pre-approval** (policy.py) moved with change_dir, was checked on a
  different path than the one written, and covered dotfiles that run code.
- **Remote mode** (remote_exec.py) has its own jail and dispatcher; a bad
  argument used to escape as an exception, so no tool_result was ever sent and
  the server's turn hung.
"""

from __future__ import annotations

import codecs
import json
import os
import re
import stat
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tui"))

from jojocode_ai import policy, remote_exec, shell, textfiles, tools  # noqa: E402
from jojocode_ai.tools import Ctx, run_tool  # noqa: E402

POSIX = os.name == "posix"
PY = f'"{sys.executable}"'
HAS_RG = tools._which_rg() is not None


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A private home, so '~' and the default config dir never touch the real one."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    return h


@pytest.fixture
def ctx(tmp_path, monkeypatch, home):
    monkeypatch.setenv("JOJO_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("JOJO_CONFIG", str(tmp_path / "cfg"))
    proj = tmp_path / "proj"
    proj.mkdir()
    return Ctx(cwd=str(proj))


@pytest.fixture(params=["ripgrep", "python-walk"])
def search_mode(request, monkeypatch):
    """Both search engines must agree; ripgrep is used when it is installed."""
    if request.param == "ripgrep":
        if not HAS_RG:
            pytest.skip("ripgrep not installed")
    else:
        monkeypatch.setattr(tools, "_which_rg", lambda: None)
    return request.param


def _symlink(target, link):
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError) as e:
        pytest.skip(f"cannot create symlinks here: {e}")


def _mode(p) -> int:
    return stat.S_IMODE(os.stat(p).st_mode)


# --------------------------------------------------------------------------- #
# encodings
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("bom,codec", [(codecs.BOM_UTF16_LE, "utf-16-le"),
                                       (codecs.BOM_UTF16_BE, "utf-16-be")],
                         ids=["utf-16-le", "utf-16-be"])
def test_utf16_with_bom_is_read_and_edited_in_utf16(ctx, bom, codec):
    p = Path(ctx.cwd) / "out.txt"
    p.write_bytes(bom + "hello wörld\r\nline2\r\n".encode(codec))
    out = run_tool("read_file", {"path": "out.txt"}, ctx)
    assert "\x00" not in out, "UTF-16 read as bytes comes back NUL-padded"
    assert "1\thello wörld" in out and "2\tline2" in out
    assert run_tool("edit_file", {"path": "out.txt", "old_string": "line2",
                                  "new_string": "zeile2"}, ctx).startswith("edited")
    assert p.read_bytes() == bom + "hello wörld\r\nzeile2\r\n".encode(codec)


def test_cp1252_file_is_read_and_edited_in_cp1252(ctx):
    p = Path(ctx.cwd) / "legacy.txt"
    p.write_bytes(b"caf\xe9 costs 5\x80\nna\xefve\n")
    out = run_tool("read_file", {"path": "legacy.txt"}, ctx)
    assert "café costs 5€" in out and "naïve" in out and "\ufffd" not in out
    out = run_tool("edit_file", {"path": "legacy.txt", "old_string": "5€",
                                 "new_string": "6€"}, ctx)
    assert out.startswith("edited"), out
    assert p.read_bytes() == b"caf\xe9 costs 6\x80\nna\xefve\n"


def test_cp1252_text_after_the_first_8k_is_not_replaced(ctx):
    """read_file sniffs only the head; the legacy bytes further down are the
    same file and must decode the same way edit_file decodes them."""
    p = Path(ctx.cwd) / "late.txt"
    p.write_bytes(b"# " + b"x" * 9000 + b"\ncaf\xe9\n")
    out = run_tool("read_file", {"path": "late.txt", "offset": 2}, ctx)
    assert "café" in out, out


def test_utf8_bom_is_kept_by_edit_and_overwrite(ctx):
    p = Path(ctx.cwd) / "bom.cs"
    p.write_bytes(codecs.BOM_UTF8 + b"a\nb\n")
    out = run_tool("read_file", {"path": "bom.cs"}, ctx)
    assert out.splitlines()[0] == "     1\ta", "the BOM is not part of line 1"
    run_tool("edit_file", {"path": "bom.cs", "old_string": "b", "new_string": "c"}, ctx)
    assert p.read_bytes() == codecs.BOM_UTF8 + b"a\nc\n"
    run_tool("write_file", {"path": "bom.cs", "content": "z\n"}, ctx)
    assert p.read_bytes() == codecs.BOM_UTF8 + b"z\n"


def test_binary_files_are_refused_and_untouched(ctx):
    p = Path(ctx.cwd) / "blob.bin"
    data = bytes(range(256)) * 8
    p.write_bytes(data)
    assert "binary file" in run_tool("read_file", {"path": "blob.bin"}, ctx)
    out = run_tool("edit_file", {"path": "blob.bin", "old_string": "A",
                                 "new_string": "B"}, ctx)
    assert out.startswith("error:") and "binary" in out
    assert p.read_bytes() == data


# --------------------------------------------------------------------------- #
# writes that fail must not destroy anything
# --------------------------------------------------------------------------- #

def test_lone_surrogate_leaves_the_original_untouched(ctx):
    p = Path(ctx.cwd) / "keep.txt"
    p.write_bytes(b"precious\n")
    out = run_tool("write_file", {"path": "keep.txt", "content": "bad \ud800 text"}, ctx)
    assert out.startswith("error:"), out
    out = run_tool("edit_file", {"path": "keep.txt", "old_string": "precious",
                                 "new_string": "\udc80"}, ctx)
    assert out.startswith("error:"), out
    assert p.read_bytes() == b"precious\n"
    # and a new file is not created empty
    out = run_tool("write_file", {"path": "new.txt", "content": "\ud800"}, ctx)
    assert out.startswith("error:") and not (Path(ctx.cwd) / "new.txt").exists()


def test_char_cp1252_cannot_hold_leaves_the_original_untouched(ctx):
    p = Path(ctx.cwd) / "legacy.txt"
    p.write_bytes(b"caf\xe9\n")
    out = run_tool("edit_file", {"path": "legacy.txt", "old_string": "caf",
                                 "new_string": "漢"}, ctx)
    assert out.startswith("error:") and "cp1252" in out, out
    out = run_tool("write_file", {"path": "legacy.txt", "content": "🐱\n"}, ctx)
    assert out.startswith("error:") and "cp1252" in out, out
    assert p.read_bytes() == b"caf\xe9\n"


def test_a_failed_write_leaves_the_original_and_no_temp_file(ctx, monkeypatch):
    p = Path(ctx.cwd) / "keep.txt"
    p.write_bytes(b"precious\n")

    def disk_full(fd):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(textfiles.os, "fsync", disk_full)
    out = run_tool("write_file", {"path": "keep.txt", "content": "new\n"}, ctx)
    assert out.startswith("error:") and "No space left" in out, out
    assert p.read_bytes() == b"precious\n"
    assert sorted(os.listdir(ctx.cwd)) == ["keep.txt"], "temp file left behind"


@pytest.mark.skipif(not POSIX, reason="POSIX permission bits")
@pytest.mark.parametrize("mode", [0o755, 0o600, 0o640])
def test_write_and_edit_keep_permission_bits(ctx, mode):
    p = Path(ctx.cwd) / "run.sh"
    p.write_bytes(b"#!/bin/sh\necho hi\n")
    os.chmod(p, mode)
    run_tool("write_file", {"path": "run.sh", "content": "#!/bin/sh\necho bye\n"}, ctx)
    assert _mode(p) == mode
    run_tool("edit_file", {"path": "run.sh", "old_string": "bye", "new_string": "ciao"}, ctx)
    assert _mode(p) == mode
    assert p.read_bytes() == b"#!/bin/sh\necho ciao\n"


def test_write_and_edit_go_through_a_symlink_without_replacing_it(ctx, tmp_path):
    elsewhere = tmp_path / "shared"
    elsewhere.mkdir()
    target = elsewhere / "real.txt"
    target.write_bytes(b"one\n")
    link = Path(ctx.cwd) / "link.txt"
    _symlink(str(target), str(link))
    out = run_tool("write_file", {"path": "link.txt", "content": "two\n"}, ctx)
    assert out.startswith("overwrote"), out
    assert link.is_symlink(), "the link was replaced by a regular file"
    assert target.read_bytes() == b"two\n"
    run_tool("edit_file", {"path": "link.txt", "old_string": "two", "new_string": "three"}, ctx)
    assert link.is_symlink() and target.read_bytes() == b"three\n"
    assert [f.name for f in elsewhere.iterdir()] == ["real.txt"]


def test_mixed_endings_keep_the_untouched_lines(ctx):
    p = Path(ctx.cwd) / "mixed.txt"
    p.write_bytes(b"a\r\nb\nc\r\nd\n")
    run_tool("edit_file", {"path": "mixed.txt", "old_string": "b", "new_string": "B"}, ctx)
    assert p.read_bytes() == b"a\r\nB\nc\r\nd\n"
    # a match that spans a CRLF line break, given in "\n" form
    out = run_tool("edit_file", {"path": "mixed.txt", "old_string": "c\nd",
                                 "new_string": "C\nD"}, ctx)
    assert out.startswith("edited"), out
    assert p.read_bytes() == b"a\r\nB\nC\r\nD\n"


# --------------------------------------------------------------------------- #
# model-shaped arguments
# --------------------------------------------------------------------------- #

def test_write_file_content_null_is_an_error_not_the_word_none(ctx):
    out = run_tool("write_file", {"path": "n.txt", "content": None}, ctx)
    assert out.startswith("error:") and "content" in out
    assert not (Path(ctx.cwd) / "n.txt").exists()


def test_write_file_dict_or_list_content_is_written_as_json(ctx):
    obj = {"name": "x", "deps": ["a", "b"]}
    run_tool("write_file", {"path": "p.json", "content": obj}, ctx)
    assert json.loads((Path(ctx.cwd) / "p.json").read_text(encoding="utf-8")) == obj
    run_tool("write_file", {"path": "l.json", "content": [1, 2]}, ctx)
    assert json.loads((Path(ctx.cwd) / "l.json").read_text(encoding="utf-8")) == [1, 2]


def test_read_offset_past_the_end_says_how_long_the_file_is(ctx):
    (Path(ctx.cwd) / "three.txt").write_text("1\n2\n3\n", encoding="utf-8")
    out = run_tool("read_file", {"path": "three.txt", "offset": 10}, ctx)
    assert out.startswith("error:") and "past the end" in out and "3 lines" in out, out


@pytest.mark.parametrize("limit", [-1, "-1", 0, "0"])
def test_read_limit_minus_one_or_zero_means_all(ctx, limit):
    (Path(ctx.cwd) / "five.txt").write_text("1\n2\n3\n4\n5\n", encoding="utf-8")
    out = run_tool("read_file", {"path": "five.txt", "limit": limit}, ctx)
    assert [ln.split("\t")[1] for ln in out.splitlines()] == ["1", "2", "3", "4", "5"]


@pytest.mark.parametrize("body", ["1\n2\n3\n4\n5", "1\n2\n3\n4\n5\n"],
                         ids=["no-final-newline", "final-newline"])
def test_more_lines_footer_counts_a_last_line_without_newline(ctx, body):
    (Path(ctx.cwd) / "f.txt").write_text(body, encoding="utf-8")
    out = run_tool("read_file", {"path": "f.txt", "limit": 2}, ctx)
    assert out.endswith("(3 more lines)"), out


def test_tilde_expands_to_home_instead_of_making_a_directory(ctx, home):
    (home / "notes.txt").write_text("from home\n", encoding="utf-8")
    assert "from home" in run_tool("read_file", {"path": "~/notes.txt"}, ctx)
    run_tool("write_file", {"path": "~/new.txt", "content": "x\n"}, ctx)
    assert (home / "new.txt").read_text(encoding="utf-8") == "x\n"
    assert not (Path(ctx.cwd) / "~").exists()
    assert "notes.txt" in run_tool("list_dir", {"path": "~"}, ctx)


def test_change_dir_moves_and_refuses_what_it_should(ctx, tmp_path, home):
    proj = Path(ctx.cwd)
    (proj / "sub").mkdir()
    (proj / "file.txt").write_text("x", encoding="utf-8")
    out = run_tool("change_dir", {"path": "sub"}, ctx)
    assert out.startswith("working directory is now") and ctx.cwd == str(proj / "sub")
    before = ctx.cwd
    assert run_tool("change_dir", {"path": str(proj / "file.txt")}, ctx).startswith("error:")
    assert run_tool("change_dir", {"path": "nope"}, ctx).startswith("error:")
    (tmp_path / "cfg").mkdir()
    assert "not accessible" in run_tool("change_dir", {"path": str(tmp_path / "cfg")}, ctx)
    assert ctx.cwd == before
    run_tool("change_dir", {"path": "~"}, ctx)
    assert ctx.cwd == str(home)


# --------------------------------------------------------------------------- #
# list_dir
# --------------------------------------------------------------------------- #

def test_list_dir_caps_at_500_entries(ctx):
    d = Path(ctx.cwd) / "many"
    d.mkdir()
    for i in range(tools.MAX_ENTRIES + 7):
        (d / f"f{i:04d}.txt").write_bytes(b"")
    out = run_tool("list_dir", {"path": "many"}, ctx)
    shown = [ln for ln in out.splitlines() if ln.strip().startswith("f")]
    assert len(shown) == tools.MAX_ENTRIES
    assert "and 7 more entries" in out


def test_list_dir_survives_a_dangling_symlink(ctx):
    proj = Path(ctx.cwd)
    (proj / "ok.txt").write_bytes(b"12345")
    _symlink(str(proj / "gone"), str(proj / "dangling"))
    out = run_tool("list_dir", {"path": "."}, ctx)
    assert not out.startswith("error:"), out
    assert "dangling  (broken link)" in out and "ok.txt  (5 bytes)" in out


# --------------------------------------------------------------------------- #
# search
# --------------------------------------------------------------------------- #

def test_search_finds_hidden_files_and_skips_git(ctx, search_mode):
    proj = Path(ctx.cwd)
    (proj / ".github" / "workflows").mkdir(parents=True)
    (proj / ".github" / "workflows" / "ci.yml").write_text("run: NEEDLE ci\n", encoding="utf-8")
    (proj / ".git").mkdir()
    (proj / ".git" / "config").write_text("[core] NEEDLE in git\n", encoding="utf-8")
    (proj / "a.py").write_text("x = 'NEEDLE code'\n", encoding="utf-8")
    out = run_tool("search", {"pattern": "NEEDLE"}, ctx)
    assert "ci.yml" in out and "NEEDLE ci" in out
    assert "NEEDLE code" in out
    assert "in git" not in out


def test_search_a_single_file(ctx, search_mode):
    (Path(ctx.cwd) / "s.txt").write_text("nothing\nNEEDLE here\n", encoding="utf-8")
    (Path(ctx.cwd) / "other.txt").write_text("NEEDLE elsewhere\n", encoding="utf-8")
    out = run_tool("search", {"pattern": "NEEDLE", "path": "s.txt"}, ctx)
    assert re.search(r"(^|:)2:NEEDLE here$", out, re.M), out
    assert "elsewhere" not in out


def test_search_missing_path_is_an_error(ctx, search_mode):
    out = run_tool("search", {"pattern": "x", "path": "no/such/dir"}, ctx)
    assert out.startswith("error:") and "no such file" in out


def test_search_invalid_regex_is_an_error(ctx, search_mode):
    (Path(ctx.cwd) / "a.txt").write_text("(\n", encoding="utf-8")
    out = run_tool("search", {"pattern": "(unclosed"}, ctx)
    assert out.startswith("error:"), out


@pytest.mark.parametrize("pattern", [r"foo(?=bar)", r"(?<=x)foo", r"(o)\1"],
                         ids=["lookahead", "lookbehind", "backreference"])
def test_search_falls_back_to_python_re_for_lookaround(ctx, search_mode, pattern):
    (Path(ctx.cwd) / "a.txt").write_text("xfoobar\n", encoding="utf-8")
    out = run_tool("search", {"pattern": pattern}, ctx)
    assert not out.startswith("error:"), out
    assert "a.txt:1:xfoobar" in out


def test_search_never_reads_jojo_credentials(ctx, tmp_path, search_mode):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "credentials").write_text('{"token": "NEEDLE-secret"}\n', encoding="utf-8")
    (Path(ctx.cwd) / "code.py").write_text("NEEDLE = 1\n", encoding="utf-8")
    out = run_tool("search", {"pattern": "NEEDLE", "path": str(tmp_path)}, ctx)
    assert "code.py" in out and "secret" not in out


def _fake_rg(d: Path, marker: Path) -> None:
    d.mkdir(parents=True, exist_ok=True)
    for name in ("rg", "rg.exe"):
        f = d / name
        f.write_text(f"#!/bin/sh\ntouch '{marker}'\n", encoding="utf-8")
        os.chmod(f, 0o755)


def test_which_rg_ignores_relative_and_cwd_path_entries(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _fake_rg(repo, tmp_path / "ran")
    _fake_rg(repo / "bin", tmp_path / "ran")
    monkeypatch.chdir(repo)
    bad = os.pathsep.join(["", ".", "bin", "./bin", str(repo)])
    monkeypatch.setenv("PATH", bad)
    assert tools._which_rg() is None
    good = tmp_path / "usr-bin"
    _fake_rg(good, tmp_path / "ran")
    monkeypatch.setenv("PATH", bad + os.pathsep + str(good))
    found = tools._which_rg()
    assert found is not None and Path(found).parent == good


@pytest.mark.skipif(not POSIX, reason="the fake rg is a shell script")
def test_search_never_runs_an_rg_from_the_repository(ctx, tmp_path, monkeypatch):
    proj = Path(ctx.cwd)
    marker = tmp_path / "ran"
    _fake_rg(proj, marker)
    (proj / "a.txt").write_text("NEEDLE\n", encoding="utf-8")
    monkeypatch.chdir(proj)
    monkeypatch.setenv("PATH", os.pathsep.join([".", str(proj)]))
    out = run_tool("search", {"pattern": "NEEDLE"}, ctx)
    assert "a.txt:1:NEEDLE" in out
    assert not marker.exists(), "search executed the repository's own rg"


# --------------------------------------------------------------------------- #
# JojoAI's own data
# --------------------------------------------------------------------------- #

def _config_dir_cases(tmp_path, monkeypatch, home, where):
    if where == "JOJO_CONFIG":
        cfg = tmp_path / "cfg"
    else:
        monkeypatch.delenv("JOJO_CONFIG", raising=False)
        cfg = home / ".config" / "jojocode-ai"
    cfg.mkdir(parents=True)
    (cfg / "credentials").write_text('{"token": "secret"}\n', encoding="utf-8")
    return cfg


@pytest.mark.parametrize("where", ["JOJO_CONFIG", "default"])
def test_blocked_covers_the_config_dir(ctx, tmp_path, monkeypatch, home, where):
    cfg = _config_dir_cases(tmp_path, monkeypatch, home, where)
    cred = str(cfg / "credentials")
    assert tools._blocked(cred) and tools._blocked(str(cfg))
    assert not tools._blocked(str(Path(ctx.cwd) / "credentials"))
    for name, args in [("read_file", {"path": cred}),
                       ("list_dir", {"path": str(cfg)}),
                       ("search", {"pattern": "token", "path": str(cfg)}),
                       ("change_dir", {"path": str(cfg)}),
                       ("write_file", {"path": cred, "content": "{}"}),
                       ("edit_file", {"path": cred, "old_string": "secret",
                                      "new_string": "x"}),
                       ("write_file", {"path": str(cfg / "settings.json"), "content": "{}"})]:
        out = run_tool(name, args, ctx)
        assert out.startswith("error:") and "JojoAI's own data" in out, (name, out)
    assert (cfg / "credentials").read_text(encoding="utf-8") == '{"token": "secret"}\n'
    assert not (cfg / "settings.json").exists()


def test_blocked_sees_through_a_symlink_to_the_config_dir(ctx, tmp_path, monkeypatch, home):
    cfg = _config_dir_cases(tmp_path, monkeypatch, home, "JOJO_CONFIG")
    _symlink(str(cfg), str(Path(ctx.cwd) / "innocent"))
    out = run_tool("read_file", {"path": "innocent/credentials"}, ctx)
    assert out.startswith("error:") and "secret" not in out


# --------------------------------------------------------------------------- #
# run_bash / shell.py
# --------------------------------------------------------------------------- #

def _alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        pass
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


@pytest.mark.skipif(not POSIX, reason="sh job control")
def test_background_job_returns_at_once_and_keeps_running(ctx):
    t = time.monotonic()
    out = run_tool("run_bash", {"command": "sleep 30 & echo started $!", "timeout": 20}, ctx)
    elapsed = time.monotonic() - t
    m = re.search(r"started (\d+)", out)
    assert m, out
    pid = int(m.group(1))
    try:
        assert elapsed < 2, f"the call waited {elapsed:.1f}s on a background job"
        assert "[exit 0]" in out
        assert _alive(pid), "the background job was killed when the call returned"
    finally:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


_TEN_MB = (f'{PY} -c "import sys; sys.stdout.write(chr(120) * 10000000); '
           f'sys.stderr.write(chr(121) * 10000000)"')


def test_ten_megabytes_of_output_are_bounded_in_shell(tmp_path):
    r = shell.run(_TEN_MB, str(tmp_path), 60)
    assert r.code == 0
    dropped = 10_000_000 - shell.HEAD - shell.TAIL
    for text, ch in ((r.out, "x"), (r.err, "y")):
        assert len(text) < shell.HEAD + shell.TAIL + 100
        assert f"[{dropped} bytes omitted]" in text
        assert text.startswith(ch * 100) and text.endswith(ch * 100)


@pytest.mark.parametrize("mode", ["local", "remote"])
def test_ten_megabytes_reach_the_model_bounded_and_marked_omitted(ctx, mode):
    """The shell counts what it dropped; the model must still be told."""
    if mode == "local":
        out = run_tool("run_bash", {"command": _TEN_MB}, ctx)
        assert len(out) <= 2 * tools.MAX_OUTPUT
    else:
        r = remote_exec.execute("run_command", {"cmd": _TEN_MB}, ctx.cwd)
        assert r["ok"] is True
        out = r["stdout"]
        assert len(out) <= 2 * remote_exec.MAX_OUT
    assert "bytes omitted" in out, (
        "the model sees only a 'truncated' count of the already-clipped text: "
        + re.findall(r"\[[^\]]*\]", out)[0])


def test_split_utf8_char_at_the_cut_is_not_mojibake(monkeypatch):
    """Simulated Windows: the fallback codec is the OEM code page. A UTF-8
    character cut in half by the head/tail bound must not send the whole
    kept output through it."""
    monkeypatch.setattr(shell, "_oem_codepage", lambda: "cp437")
    with tempfile.TemporaryFile() as f:
        f.write(b"a" + "€".encode("utf-8") * 40_000)
        text = shell._read_bounded(f)
    head, tail = re.split(r"\n\.\.\. \[\d+ bytes omitted\] \.\.\.\n", text)
    assert head.startswith("a€€€"), head[:20]
    assert set(head) <= {"a", "€"} and set(tail) <= {"€"}, (head[:12], tail[:12])
    assert len(tail) > 5000


def test_cancel_interrupts_a_running_command_within_a_second(tmp_path):
    t0 = time.monotonic()
    ctx = Ctx(cwd=str(tmp_path), cancel=lambda: time.monotonic() - t0 > 0.3)
    out = run_tool("run_bash", {"command": f'{PY} -c "import time; time.sleep(30)"',
                                "timeout": 60}, ctx)
    elapsed = time.monotonic() - t0
    assert elapsed < 2, f"cancel took {elapsed:.1f}s"
    assert "interrupted by the user" in out


def test_a_list_command_is_joined_and_quoted(ctx):
    out = run_tool("run_bash", {"command": ["echo", "a b"]}, ctx)
    assert "a b" in out and "[exit 0]" in out
    if POSIX:
        assert out.splitlines()[0] == "a b"
        assert shell.as_command(["echo", "a b"]) == "echo 'a b'"


def test_as_command_quotes_for_cmd_on_windows(monkeypatch):
    monkeypatch.setattr(shell, "IS_WINDOWS", True)
    assert shell.as_command(["echo", "a b"]) == 'echo "a b"'


@pytest.fixture
def no_spawn(monkeypatch):
    """Simulated Windows where starting any process fails the test."""
    spawned = []

    def popen(*a, **kw):
        spawned.append(a)
        raise AssertionError(f"a command was started: {a!r}")

    monkeypatch.setattr(shell, "IS_WINDOWS", True)
    monkeypatch.setattr(shell.subprocess, "Popen", popen)
    return spawned


def test_windows_precheck_refuses_multiline_commands(no_spawn, tmp_path):
    why = shell.precheck("echo a\necho b", str(tmp_path))
    assert why and "first line" in why
    assert shell.precheck(["python", "-c", "a\nb"], str(tmp_path))
    assert shell.precheck("echo a\n", str(tmp_path)) is None, "a trailing newline is one line"
    r = shell.run("echo a\r\necho b", str(tmp_path), 10)
    assert r.code is None and "first line" in r.err
    out = run_tool("run_bash", {"command": "echo a\necho b"}, Ctx(cwd=str(tmp_path)))
    assert "first line" in out
    assert no_spawn == []


def test_windows_precheck_refuses_unc_directories(no_spawn):
    unc = r"\\server\share\proj"
    why = shell.precheck("dir", unc)
    assert why and "UNC" in why
    r = shell.run("dir", unc, 10)
    assert r.code is None and "UNC" in r.err
    assert "UNC" in run_tool("run_bash", {"command": "dir"}, Ctx(cwd=unc))
    assert shell.precheck("dir", r"C:\proj") is None
    assert no_spawn == []


def test_kill_all_stops_a_running_command(tmp_path):
    box = {}

    def go():
        box["r"] = shell.run(f'{PY} -c "import time; time.sleep(30)"', str(tmp_path), 60)

    th = threading.Thread(target=go, daemon=True)
    t0 = time.monotonic()
    th.start()
    while not shell._LIVE and time.monotonic() - t0 < 10:
        time.sleep(0.02)
    assert shell._LIVE, "the command never started"
    shell.kill_all()
    th.join(timeout=5)
    assert not th.is_alive(), "kill_all did not stop the command"
    assert time.monotonic() - t0 < 8
    assert box["r"].code != 0 and not shell._LIVE


# --------------------------------------------------------------------------- #
# policy.auto_write_ok
# --------------------------------------------------------------------------- #

@pytest.fixture
def proj(tmp_path, home):
    p = tmp_path / "work" / "proj"
    (p / "src").mkdir(parents=True)
    return p


def test_auto_write_inside_the_project_is_ok(proj):
    assert policy.auto_write_ok(str(proj / "src" / "a.py"), str(proj))
    assert policy.auto_write_ok(str(proj / "new" / "deep" / "b.py"), str(proj))


@pytest.mark.parametrize("rel", [".envrc", ".git/config", ".git/hooks/pre-commit",
                                 ".github/workflows/ci.yml", ".vscode/tasks.json",
                                 "src/.hidden", ".husky/pre-push"])
def test_auto_write_never_covers_dotfiles(proj, rel):
    assert not policy.auto_write_ok(str(proj / rel), str(proj))


def test_auto_write_outside_the_project_asks(proj, tmp_path):
    assert not policy.auto_write_ok(str(tmp_path / "work" / "other.py"), str(proj))
    assert not policy.auto_write_ok(str(proj) + "2" + os.sep + "x.py", str(proj))
    assert not policy.auto_write_ok(os.path.join(str(proj), "..", "x.py"), str(proj))
    assert not policy.auto_write_ok("", str(proj))
    assert not policy.auto_write_ok(str(proj / "a.py"), "")


def test_auto_write_through_a_symlink_that_escapes_asks(proj, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "f.txt").write_text("x", encoding="utf-8")
    _symlink(str(outside), str(proj / "out"))
    _symlink(str(outside / "f.txt"), str(proj / "link.txt"))
    assert not policy.auto_write_ok(str(proj / "out" / "x.py"), str(proj))
    assert not policy.auto_write_ok(str(proj / "link.txt"), str(proj))


def test_auto_write_with_home_or_a_filesystem_root_as_the_project_asks(home):
    (home / "proj").mkdir()
    assert not policy.auto_write_ok(str(home / "proj" / "a.py"), str(home))
    assert policy.auto_write_ok(str(home / "proj" / "a.py"), str(home / "proj"))
    fs_root = os.path.abspath(os.sep)
    assert not policy.auto_write_ok(str(home / "proj" / "a.py"), fs_root)


# --------------------------------------------------------------------------- #
# remote_exec
# --------------------------------------------------------------------------- #

def test_jail_with_a_filesystem_root_allows_its_children():
    fs_root = os.path.abspath(os.sep)
    child = os.path.join(fs_root, "etc", "jojo-test-hostname")
    expect = os.path.realpath(child)
    assert remote_exec._jail(fs_root, child) == expect
    assert remote_exec._jail(fs_root, os.path.join("etc", "jojo-test-hostname")) == expect
    assert remote_exec._jail(fs_root, ".") == os.path.realpath(fs_root)


def test_jail_refuses_escapes(tmp_path):
    root = tmp_path / "proj"
    (root / "sub").mkdir(parents=True)
    (tmp_path / "proj2").mkdir()
    assert remote_exec._jail(str(root), "sub/x.txt") == os.path.realpath(root / "sub" / "x.txt")
    assert remote_exec._jail(str(root), "../proj2/x") is None
    assert remote_exec._jail(str(root), str(tmp_path / "proj2" / "x")) is None
    _symlink(str(tmp_path), str(root / "up"))
    assert remote_exec._jail(str(root), "up/x") is None
    r = remote_exec.execute("write_file", {"path": "../evil.txt", "content": "x"}, str(root))
    assert r["ok"] is False and r.get("refused") == "policy"
    assert not (tmp_path / "evil.txt").exists()


@pytest.mark.parametrize("max_bytes,kept", [(-5, 1), (0, 1), (10**12, remote_exec.MAX_READ),
                                            ("999999999999", remote_exec.MAX_READ),
                                            (None, remote_exec.MAX_READ), ("abc", remote_exec.MAX_READ),
                                            (50, 50)])
def test_remote_read_max_bytes_is_clamped(tmp_path, max_bytes, kept):
    body = "abcdefghij" * 15_000
    (tmp_path / "big.txt").write_text(body, encoding="utf-8")
    r = remote_exec.execute("read_file", {"path": "big.txt", "max_bytes": max_bytes}, str(tmp_path))
    assert r["ok"] is True, r
    assert r["content"] == body[:kept] + "\n... [clipped]"


def test_remote_read_clip_does_not_split_utf8_into_mojibake(tmp_path):
    """A clip that lands inside a multi-byte character must not make the whole
    read look like cp1252 — a model that writes it back corrupts the file."""
    (tmp_path / "u.txt").write_bytes(("a" + "é" * 60_000).encode("utf-8"))
    r = remote_exec.execute("read_file", {"path": "u.txt"}, str(tmp_path))
    assert r["ok"] is True
    assert r["content"].startswith("aééé"), r["content"][:12]
    assert "Ã" not in r["content"]
    r = remote_exec.execute("read_file", {"path": "u.txt", "max_bytes": 4}, str(tmp_path))
    assert r["content"].startswith("aé"), r["content"]


def test_remote_write_null_content_is_an_error(tmp_path):
    (tmp_path / "keep.txt").write_bytes(b"precious\n")
    for path in ("keep.txt", "new.txt"):
        r = remote_exec.execute("write_file", {"path": path, "content": None}, str(tmp_path))
        assert r["ok"] is False and "content" in r["error"]
    assert (tmp_path / "keep.txt").read_bytes() == b"precious\n"
    assert not (tmp_path / "new.txt").exists()


def test_remote_write_dict_content_is_json(tmp_path):
    r = remote_exec.execute("write_file", {"path": "c.json", "content": {"a": [1]}}, str(tmp_path))
    assert r["ok"] is True
    assert json.loads((tmp_path / "c.json").read_text(encoding="utf-8")) == {"a": [1]}


@pytest.mark.parametrize("raw,old,new,expect", [
    (b"caf\xe9\r\nna\xefve\r\nprice: 5\x80\r\n", "5€", "6€",
     b"caf\xe9\r\nna\xefve\r\nprice: 6\x80\r\n"),
    (b"undefined in cp1252: \x81\x8d\nedit me\n", "edit me", "edited",
     b"undefined in cp1252: \x81\x8d\nedited\n"),
    (codecs.BOM_UTF16_LE + "ünïcode\r\nedit me\r\n".encode("utf-16-le"), "edit me", "done",
     codecs.BOM_UTF16_LE + "ünïcode\r\ndone\r\n".encode("utf-16-le")),
    (codecs.BOM_UTF8 + "größe\nedit me\n".encode("utf-8"), "edit me", "done",
     codecs.BOM_UTF8 + "größe\ndone\n".encode("utf-8")),
], ids=["cp1252-crlf", "latin-1", "utf-16-bom", "utf-8-bom"])
def test_remote_read_then_write_keeps_every_byte_not_edited(tmp_path, raw, old, new, expect):
    (tmp_path / "f.txt").write_bytes(raw)
    r = remote_exec.execute("read_file", {"path": "f.txt"}, str(tmp_path))
    assert r["ok"] is True and "\ufffd" not in r["content"] and old in r["content"]
    w = remote_exec.execute("write_file", {"path": "f.txt",
                                           "content": r["content"].replace(old, new)}, str(tmp_path))
    assert w["ok"] is True, w
    assert (tmp_path / "f.txt").read_bytes() == expect


def test_remote_write_that_cannot_encode_leaves_the_file(tmp_path):
    (tmp_path / "f.txt").write_bytes(b"caf\xe9\n")
    r = remote_exec.execute("write_file", {"path": "f.txt", "content": "漢\n"}, str(tmp_path))
    assert r["ok"] is False and "cp1252" in r["error"]
    assert (tmp_path / "f.txt").read_bytes() == b"caf\xe9\n"


def _capture_timeout(monkeypatch):
    seen = []
    monkeypatch.setattr(remote_exec.shell, "run",
                        lambda cmd, cwd, t, cancel=None: seen.append(t) or shell.Result("", "", 0))
    return seen


def test_remote_timeout_ms_as_text_parses(tmp_path, monkeypatch):
    seen = _capture_timeout(monkeypatch)
    for value in ("30s", "120000", 5000, None, "soon", True):
        r = remote_exec.execute("run_command", {"cmd": "true", "timeout_ms": value}, str(tmp_path))
        assert r["ok"] is True, (value, r)
    assert seen[1:] == [120.0, 5.0, 120.0, 120.0, 120.0]
    assert 1 <= seen[0] <= 3600


def test_remote_timeout_ms_30s_means_thirty_seconds(tmp_path, monkeypatch):
    """A unit the model wrote is a unit: "30s" is not 30 milliseconds."""
    seen = _capture_timeout(monkeypatch)
    remote_exec.execute("run_command", {"cmd": "true", "timeout_ms": "30s"}, str(tmp_path))
    assert seen == [30.0], f'"30s" became a {seen[0]:g}s timeout'


@pytest.mark.parametrize("name,args", [
    ("no_such_tool", {}),
    ("read_file", None),
    ("read_file", [1, 2]),
    ("read_file", {"path": 123}),
    ("read_file", {"path": None}),
    ("read_file", {"path": "a\x00b"}),
    ("read_file", {"path": "x.txt", "max_bytes": {"a": 1}}),
    ("list_dir", {"path": ["a"]}),
    ("list_dir", {"path": "x.txt"}),
    ("write_file", {}),
    ("write_file", {"path": ".", "content": "x"}),
    ("write_file", {"path": "a\x00b", "content": "x"}),
    ("write_file", {"path": "n.txt", "content": 42}),
    ("run_command", {}),
    ("run_command", {"cmd": None}),
    ("run_command", {"cmd": "   "}),
    ("run_command", {"cmd": ["echo", "hi there"], "timeout_ms": [1]}),
    ("run_command", {"command": "echo ok", "timeout_ms": {"x": 1}}),
])
def test_remote_bad_args_never_raise(tmp_path, name, args):
    (tmp_path / "x.txt").write_text("hello\n", encoding="utf-8")
    r = remote_exec.execute(name, args, str(tmp_path))
    assert isinstance(r, dict) and isinstance(r.get("ok"), bool) and "durationMs" in r
    assert r["ok"] or r.get("error"), r
    remote_exec.summarize(name, r)        # the TUI's one-liner must not raise either
    if name == "run_command" and r["ok"]:
        assert r["exitCode"] == 0 and r["stdout"].strip() in ("hi there", "ok")


def test_remote_list_dir_survives_a_dangling_symlink(tmp_path):
    (tmp_path / "ok.txt").write_bytes(b"123")
    _symlink(str(tmp_path / "gone"), str(tmp_path / "dangling"))
    r = remote_exec.execute("list_dir", {"path": "."}, str(tmp_path))
    assert r["ok"] is True and "dangling  (broken link)" in r["stdout"]
    assert "ok.txt  (3 B)" in r["stdout"]


def test_remote_run_command_gets_no_keyboard_and_reports_exit(tmp_path):
    r = remote_exec.execute("run_command", {"cmd": f'{PY} -c "import sys; print(len(sys.stdin.read()))"'},
                            str(tmp_path))
    assert r["ok"] is True and r["stdout"].strip() == "0" and r["exitCode"] == 0
    r = remote_exec.execute("run_command", {"cmd": f'{PY} -c "raise SystemExit(3)"'}, str(tmp_path))
    assert r["ok"] is False and r["exitCode"] == 3

