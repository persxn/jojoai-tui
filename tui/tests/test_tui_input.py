"""The curses UI's input handling and layout, driven without a real terminal.

Each of these broke real use before the fix pass:

- **Pastes were typed, not pasted.** A paste was told apart from typing by a
  12 ms timing test that failed whenever a frame took longer: its newlines
  became Enter (half a message sent, the rest typed into the next one) and its
  tabs cycled the think level. Now everything buffered is read as one batch,
  and bracketed-paste markers (ESC[200~ … ESC[201~) are honoured even when the
  paste spans several batches.
- **Escape sequences curses did not know were typed into the input** (focus
  reports, odd Home/End encodings), and on Windows an emoji arrived as two
  surrogate halves that were each "typed".
- **The approval prompt could be answered by accident.** An `a` typed into a
  note answered "always allow run_bash"; type-ahead answered it before it was
  even drawn. It now flushes type-ahead, ignores keys for APPROVAL_GRACE, and
  "always" takes a second press. Ctrl-C denies *and* stops the turn.
- **Enter while busy threw the message away**; it is queued now. /reset,
  /model and /cwd mid-turn changed state under the running agent; they are
  refused. A path such as "/usr/bin/x is broken" was an unknown command, and
  a typo such as /hepl was sent to the model.
- **Ctrl-Y switched YOLO on** — the readline "yank" key silently auto-approved
  every tool. It puts back what Ctrl-U / Ctrl-K deleted now.
- **Layout measured code points, not columns**: CJK and emoji ran off the
  right edge and the input cursor sat in the wrong column. The whole
  transcript was re-wrapped 30 times a second; blocks now cache their rows.
- **An idle TUI repainted its header 30 times a second** down an SSH link.
  The pty test at the bottom checks it goes quiet.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tui"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

curses = pytest.importorskip("curses")

from jojocode_ai import cols, syntax, tui  # noqa: E402
from jojocode_ai.config import Config  # noqa: E402


# --------------------------------------------------------------------------- #
# a TUI with no terminal and no model
# --------------------------------------------------------------------------- #

class DummyAgent:
    def __init__(self):
        self.store = None
        self.messages = []
        self.ctx = SimpleNamespace(cwd=".")
        self.client = SimpleNamespace(model="m", list_models=lambda: [],
                                      has_model=lambda name: True)
        self.interrupts = 0
        self.resets = 0

    def interrupt(self):
        self.interrupts += 1

    def reset(self):
        self.resets += 1

    def run(self, text):
        return iter(())


class FakeStore:
    rag_enabled = False

    def __init__(self, rows):
        self.rows = list(rows)
        self.deleted: list[str] = []

    def recent_sessions(self, n):
        return list(self.rows)

    def delete_session(self, sid):
        self.deleted.append(sid)
        return 3

    def load_session(self, sid):
        return [{"role": "user", "content": "hi"}]


class FakeWin:
    """Just enough of a curses window for TUI._draw: records text and the cursor."""

    def __init__(self, h, w):
        self.h, self.w = h, w
        self.rows: dict[int, list[tuple[int, str]]] = {}
        self.moves: list[tuple[int, int]] = []

    def getmaxyx(self):
        return (self.h, self.w)

    def erase(self):
        self.rows.clear()

    def refresh(self):
        pass

    def clearok(self, flag):
        pass

    def addstr(self, y, x, s, attr=0):
        self.rows.setdefault(y, []).append((x, s))

    def addnstr(self, y, x, s, n, attr=0):
        self.addstr(y, x, s[:n], attr)

    def move(self, y, x):
        self.moves.append((y, x))


@pytest.fixture
def ui(monkeypatch):
    agent = DummyAgent()
    u = tui.TUI(Config(model="m"), lambda approver: agent)
    u._splash_done = True
    u.sent = []
    # what would have gone to the model; no worker thread is started
    monkeypatch.setattr(u, "_submit", u.sent.append)
    return u


def enter(ui, text):
    """Type `text` into the box (cursor at the end), then press Enter alone."""
    ui.input, ui.cur = text, len(text)
    return ui._handle_keys(["\n"])


def type_slowly(ui, text):
    """One key per batch — how a person types."""
    for ch in text:
        ui._handle_keys([ch])


def ask(ui, name="run_bash", args=None):
    ui.events.put(("__approval__", name, args or {"command": "rm -rf build"}))
    ui._drain()


def expire_grace(ui):
    ui._approval_at = time.time() - tui.APPROVAL_GRACE - 1


def screen(ui, width=80):
    return "\n".join("".join(t for t, _ in segs) for _, segs in ui._rows(width))


# --------------------------------------------------------------------------- #
# batches: a paste is text, a lone Enter is Enter
# --------------------------------------------------------------------------- #

def test_a_burst_with_a_newline_inserts_it_instead_of_submitting(ui):
    ui._handle_keys(["h", "i", "\n", "t", "h", "e", "r", "e"])
    assert ui.input == "hi\nthere"
    assert ui.cur == len("hi\nthere")
    assert ui.sent == []


def test_a_crlf_inside_a_burst_is_one_newline(ui):
    ui._handle_keys(list("ab\r\ncd"))
    assert ui.input == "ab\ncd"


@pytest.mark.parametrize("key", ["\n", "\r", curses.KEY_ENTER])
def test_a_lone_enter_submits(ui, key):
    type_slowly(ui, "hello")
    assert ui.input == "hello"
    ui._handle_keys([key])
    assert ui.sent == ["hello"]
    assert ui.input == "" and ui.cur == 0


def test_bracketed_paste_keeps_newlines_and_tabs_across_batches(ui):
    think = ui.cfg.think
    ui._handle_keys(list("\x1b[200~") + ["a", "\n"])     # one printable: not a burst
    assert ui._in_paste
    ui._handle_keys(["b", "\t", "c"])
    ui._handle_keys(["\r", "\n"])                         # CRLF from a Windows clipboard
    ui._handle_keys(["\n"])                               # a lone newline, still pasted
    ui._handle_keys(list("\x1b[201~"))
    assert not ui._in_paste
    assert ui.input == "a\nb    c\n\n"
    assert ui.sent == []
    assert ui.cfg.think == think

    ui._handle_keys(["\n"])                               # after the paste: Enter again
    assert ui.sent == ["a\nb    c"]


def test_a_pasted_tab_becomes_spaces_and_does_not_cycle_think(ui):
    ui.cfg.think = "medium"
    ui._handle_keys(list("x\ty = 1"))
    assert ui.input == "x    y = 1"
    assert ui.cfg.think == "medium"

    ui._handle_keys(list("\x1b[200~") + ["\t"] + list("\x1b[201~"))
    assert ui.input == "x    y = 1    "
    assert ui.cfg.think == "medium"

    ui._handle_keys(["\t"])                               # a typed Tab still cycles
    assert ui.cfg.think == "high"
    assert ui.input == "x    y = 1    "


# --------------------------------------------------------------------------- #
# escape sequences and surrogates
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("seq", ["\x1b[I", "\x1b[O", "\x1b[24;5~", "\x1b[200;1R", "\x1bOx"])
def test_unknown_escape_sequences_are_dropped(ui, seq):
    ui._handle_keys(list("ab") + list(seq) + list("cd"))
    assert ui.input == "abcd"


@pytest.mark.parametrize("seq", ["\x1b[1~", "\x1b[H", "\x1b[7~", "\x1bOH"])
def test_home_sequences_move_to_the_start(ui, seq):
    type_slowly(ui, "abc")
    ui._handle_keys(list(seq))
    assert ui.input == "abc" and ui.cur == 0
    ui._handle_keys(list("\x1b[4~"))                      # and End comes back
    assert ui.cur == 3


def test_home_and_end_scroll_when_the_input_is_empty(ui):
    ui._max_scroll = 7
    ui._handle_keys(list("\x1b[1~"))
    assert ui.scroll == 7 and ui.input == ""
    ui._handle_keys(list("\x1b[F"))
    assert ui.scroll == 0


def test_surrogate_halves_are_joined_into_one_character(ui):
    ui._handle_keys(["\ud83d", "\ude00"])                 # 😀 in one batch
    ui._handle_keys(["\ud83d"])                           # 🐱 split across two
    ui._handle_keys(["\udc31"])
    ui._handle_keys(["\udc31"])                           # a stray low half: dropped
    assert ui.input == "😀🐱"
    assert not any("\ud800" <= c <= "\udfff" for c in ui.input)
    ui.input.encode("utf-8")                              # would raise on a lone half


# --------------------------------------------------------------------------- #
# the approval prompt
# --------------------------------------------------------------------------- #

def test_approval_flushes_type_ahead_and_ignores_keys_inside_the_grace(ui, monkeypatch):
    flushed = []
    monkeypatch.setattr(curses, "flushinp", lambda: flushed.append(1))
    monkeypatch.setattr(tui, "APPROVAL_GRACE", 60.0)      # "inside" for certain
    ask(ui)
    assert flushed == [1]
    assert ui.pending_approval == ("run_bash", {"command": "rm -rf build"})

    for k in ("y", "a", "a", "n", "\x1b"):
        ui._handle_keys([k])
    assert ui.pending_approval is not None
    assert ui.approve_reply.empty()
    assert "run_bash" not in ui.cfg.auto_approve

    expire_grace(ui)
    ui._handle_keys(["y"])
    assert ui.approve_reply.get_nowait() is True
    assert ui.pending_approval is None
    assert tui.APPROVAL_GRACE > 0


def test_approval_grace_is_real_by_default():
    assert 0.2 <= tui.APPROVAL_GRACE <= 2.0


def test_y_allows_once(ui):
    ask(ui)
    expire_grace(ui)
    ui._handle_keys(["y"])
    assert ui.approve_reply.get_nowait() is True
    assert "run_bash" not in ui.cfg.auto_approve


@pytest.mark.parametrize("key", ["n", "N", "\x1b"])
def test_n_or_esc_denies(ui, key):
    ask(ui)
    expire_grace(ui)
    ui._handle_keys([key])
    assert ui.approve_reply.get_nowait() is False
    assert ui.pending_approval is None


def test_always_allow_needs_two_presses(ui):
    ask(ui)
    expire_grace(ui)
    ui._handle_keys(["a"])
    assert ui.pending_approval is not None and ui.approve_reply.empty()
    assert "run_bash" not in ui.cfg.auto_approve
    ui._handle_keys(["a"])
    assert ui.approve_reply.get_nowait() is True
    assert "run_bash" in ui.cfg.auto_approve
    assert ui.pending_approval is None


def test_any_other_key_cancels_a_half_pressed_always(ui):
    ask(ui)
    expire_grace(ui)
    ui._handle_keys(["a"])
    ui._handle_keys(["x"])
    ui._handle_keys(["a"])
    assert ui.pending_approval is not None and ui.approve_reply.empty()
    assert "run_bash" not in ui.cfg.auto_approve


def test_a_new_approval_does_not_inherit_a_half_pressed_always(ui):
    ask(ui)
    expire_grace(ui)
    ui._handle_keys(["a"])
    ui._handle_keys(["n"])                                # first one denied
    assert ui.approve_reply.get_nowait() is False
    ask(ui, "write_file", {"path": "x.py", "content": "1"})
    expire_grace(ui)
    ui._handle_keys(["a"])
    assert ui.pending_approval is not None and ui.approve_reply.empty()


def test_ctrl_c_denies_and_interrupts_even_inside_the_grace(ui):
    ask(ui)
    ui._handle_keys(["\x03"])
    assert ui.approve_reply.get_nowait() is False
    assert ui.agent.interrupts == 1
    assert ui.pending_approval is None


def test_an_approval_closes_the_history_picker(ui):
    ui.store = FakeStore([("ses_20260101-120000", 2, "hello")])
    ui._handle_keys(["\x12"])                             # Ctrl-R
    assert ui.picker is not None
    ask(ui)
    assert ui.picker is None
    ui._handle_keys(["\x12"])                             # cannot reopen it on top
    assert ui.picker is None
    assert ui.pending_approval is not None


@pytest.mark.parametrize("paste", [
    list("\x1b[200~") + list("yes") + list("\x1b[201~"),   # bracketed
    list("yes"),                                             # a burst (Windows: no markers)
], ids=["bracketed", "burst"])
def test_a_paste_does_not_answer_an_approval(ui, paste):
    # A paste is never a keypress: the header's own rule is that type-ahead
    # must not answer the prompt, and pasted text starting with "y" (or "aa")
    # is exactly that, arriving a little later.
    ask(ui)
    expire_grace(ui)
    ui._handle_keys(paste)
    assert ui.approve_reply.empty(), "pasted text answered the approval prompt"
    assert ui.pending_approval is not None


# --------------------------------------------------------------------------- #
# busy: queue, refuse, interrupt
# --------------------------------------------------------------------------- #

def test_enter_while_busy_queues_and_idle_sends_it(ui):
    ui.busy = True
    enter(ui, "next step")
    assert ui._queued == ["next step"]
    assert ui.sent == [] and ui.input == ""

    ui.events.put(("__idle__",))
    ui._drain()
    assert ui.busy is False
    assert ui.sent == ["next step"]
    assert ui._queued == []


def test_ctrl_c_while_busy_interrupts_and_drops_the_queue(ui):
    ui.busy = True
    enter(ui, "later")
    ui._handle_keys(["\x03"])
    assert ui.agent.interrupts == 1
    assert ui._queued == []


def test_state_changing_commands_are_refused_while_busy(ui, tmp_path):
    ui.busy = True
    ui.agent.ctx.cwd = str(tmp_path)
    cwd = ui.cfg.cwd
    ui.blocks.append(tui.Block("user", "keep me"))
    (tmp_path / "elsewhere").mkdir()

    enter(ui, "/reset")
    assert ui.agent.resets == 0 and len(ui.blocks) == 1
    assert "busy" in ui._toast

    ui._toast = ""
    enter(ui, "/model other:7b")
    assert ui.cfg.model == "m" and ui.agent.client.model == "m"
    assert "busy" in ui._toast

    ui._toast = ""
    enter(ui, f"/cwd {tmp_path / 'elsewhere'}")
    assert ui.agent.ctx.cwd == str(tmp_path) and ui.cfg.cwd == cwd
    assert "busy" in ui._toast

    assert ui.sent == [] and ui._queued == []


# --------------------------------------------------------------------------- #
# slash commands
# --------------------------------------------------------------------------- #

def test_a_double_slash_sends_a_message_that_starts_with_slash(ui):
    enter(ui, "//hello")
    assert ui.sent == ["/hello"]


def test_a_command_typo_is_not_sent_to_the_model(ui):
    enter(ui, "/hepl")
    assert ui.sent == []
    assert "unknown command /hepl" in ui._toast
    assert ui.input == "/hepl"                            # kept, to fix the typo


def test_a_path_is_a_message_not_a_command(ui):
    enter(ui, "/usr/bin/x is broken")
    assert ui.sent == ["/usr/bin/x is broken"]


@pytest.mark.parametrize("bad", ["0", "501", "-3", "ten", ""])
def test_steps_rejects_out_of_range(ui, bad):
    before = ui.cfg.max_steps
    enter(ui, f"/steps {bad}".strip())
    assert ui.cfg.max_steps == before
    assert ui.blocks and ui.blocks[-1].kind == "error"
    assert "/steps" in ui.blocks[-1].text


@pytest.mark.parametrize("good", [1, 500])
def test_steps_accepts_the_bounds(ui, good):
    enter(ui, f"/steps {good}")
    assert ui.cfg.max_steps == good


def test_cwd_accepts_a_quoted_relative_path(ui, tmp_path):
    (tmp_path / "my dir" / "sub dir").mkdir(parents=True)
    ui.agent.ctx.cwd = str(tmp_path)

    enter(ui, '/cwd "my dir"')
    assert ui.agent.ctx.cwd == str(tmp_path / "my dir")
    assert ui.cfg.cwd == str(tmp_path / "my dir")

    enter(ui, "/cwd 'sub dir'")
    assert ui.agent.ctx.cwd == str(tmp_path / "my dir" / "sub dir")

    enter(ui, "/cwd nope")
    assert ui.agent.ctx.cwd == str(tmp_path / "my dir" / "sub dir")
    assert ui.blocks[-1].kind == "error" and "not a directory" in ui.blocks[-1].text
    assert ui.sent == []


# --------------------------------------------------------------------------- #
# editing
# --------------------------------------------------------------------------- #

def test_ctrl_u_then_ctrl_y_puts_the_text_back(ui):
    ui.input, ui.cur = "hello world", 5
    ui._handle_keys(["\x15"])
    assert ui.input == " world" and ui.cur == 0
    ui._handle_keys(["\x19"])
    assert ui.input == "hello world" and ui.cur == 5
    assert "*" not in ui.cfg.auto_approve                # Ctrl-Y is not YOLO any more


def test_ctrl_k_kills_to_the_end(ui):
    ui.input, ui.cur = "hello world", 5
    ui._handle_keys(["\x0b"])
    assert ui.input == "hello" and ui.cur == 5
    ui._handle_keys(["\x19"])
    assert ui.input == "hello world"


def test_up_on_a_multiline_input_moves_the_cursor_up_a_line(ui):
    ui.history = ["old message"]
    text = "first line\nsecond"
    ui.input, ui.cur = text, len(text)
    ui._handle_keys([curses.KEY_UP])
    assert ui.input == text                               # not replaced by history
    assert ui.cur == len("second")                        # same column, line above
    assert ui.hist_idx is None


def test_vertical_moves_count_columns_not_characters(ui):
    text = "漢字漢字\nabc"
    ui.input, ui.cur = text, len(text)                    # column 3 on line 2
    ui._handle_keys([curses.KEY_UP])
    assert ui.cur == 1                                    # 漢 is 2 columns; 漢字 would be 4


def test_history_up_and_down_restore_the_unsent_draft(ui):
    ui.history = ["old message"]
    draft = "first line\nsecond"
    ui.input, ui.cur = draft, len(draft)
    ui._handle_keys([curses.KEY_UP])                      # up a line inside the draft
    ui._handle_keys([curses.KEY_UP])                      # first line: into history
    assert ui.input == "old message"
    ui._handle_keys([curses.KEY_DOWN])                    # past the newest: the draft
    assert ui.input == draft and ui.cur == len(draft)
    assert ui.hist_idx is None


# --------------------------------------------------------------------------- #
# the history picker
# --------------------------------------------------------------------------- #

def test_picker_delete_needs_two_presses(ui):
    rows = [("ses_20260101-120000", 2, "a"), ("ses_20260102-130000", 4, "b")]
    ui.store = FakeStore(rows)
    ui._open_picker()

    ui._handle_keys(["d"])
    assert ui.store.deleted == []
    ui._handle_keys(["d"])
    assert ui.store.deleted == ["ses_20260101-120000"]
    assert [r[0] for r in ui.picker["rows"]] == ["ses_20260102-130000"]

    ui._handle_keys(["d"])
    ui._handle_keys(["x"])                                # any other key disarms
    ui._handle_keys(["d"])
    assert ui.store.deleted == ["ses_20260101-120000"]
    assert ui.picker is not None


# --------------------------------------------------------------------------- #
# transcript rows
# --------------------------------------------------------------------------- #

def test_ctrl_t_hides_thinking_on_screen_but_keeps_the_text(ui):
    ui.blocks.append(tui.Block("thinking", "secret plan"))
    assert "secret plan" in screen(ui)

    ui._handle_keys(["\x14"])
    assert ui.show_thinking is False
    shown = screen(ui)
    assert "secret plan" not in shown
    assert "reasoning hidden (11 chars)" in shown

    ui.events.put(("thinking", " more"))                  # still recorded while hidden
    ui._drain()
    assert ui.blocks[0].text == "secret plan more"

    ui._handle_keys(["\x14"])
    assert "secret plan more" in screen(ui)


def test_a_block_is_rendered_again_only_when_its_version_changes(ui, monkeypatch):
    calls = []
    real = ui._render_block

    def counting(b, width, live, dots):
        calls.append((b.text, width))
        return real(b, width, live, dots)

    monkeypatch.setattr(ui, "_render_block", counting)
    old = tui.Block("assistant", "an older reply")
    new = tui.Block("assistant", "hi")
    ui.blocks += [old, new]

    ui._rows(80)
    ui._rows(80)
    assert len(calls) == 2                                # each block once

    new.add(" there")
    v = new.version
    ui._rows(80)
    assert calls[2:] == [("hi there", 80)]               # only the one that changed

    ui._rows(80)
    assert len(calls) == 3 and new.version == v

    new.text = "replaced"
    ui._rows(80)
    assert calls[-1] == ("replaced", 80) and len(calls) == 4

    ui._rows(60)                                          # a resize re-wraps everything
    assert len(calls) == 6
    ui._rows(60)
    assert len(calls) == 6


# --------------------------------------------------------------------------- #
# columns: CJK, emoji, combining marks
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text,cols_", [
    ("abc", 3), ("漢字", 4), ("🐱", 2), ("e\u0301", 1), ("नमस्ते", 4),
    ("a\u200db", 2), ("Ａ", 2), ("ｱ", 1), ("", 0),
])
def test_width_counts_terminal_columns(text, cols_):
    assert cols.width(text) == cols_


WIDE_SAMPLES = [
    "漢字のテキストを折り返すテストです",
    "emoji 🐱🐶🐭 party 🎉 time 🎉🎉🎉🎉",
    "混合 mixed 文字列 text 🐱 with  spaces 中文中文中文",
    "नमस्ते दुनिया, यह एक परीक्षण है",
    "ｱｲｳ ＡＢＣ　全角スペース",
]


@pytest.mark.parametrize("text", WIDE_SAMPLES)
def test_wrap_never_exceeds_the_width_and_loses_nothing(text):
    for w in range(2, 30):
        lines = cols.wrap(text, w)
        assert all(cols.width(ln) <= w for ln in lines), (w, lines)
        assert "".join(lines) == text, (w, lines)


def test_clip_measures_columns():
    assert cols.clip("漢字漢字", 5) == "漢字"
    assert cols.clip("ab🐱c", 3) == "ab"
    assert cols.clip("abc", 0) == ""


def test_input_wrap_measures_columns(ui):
    for text in ["漢" * 10, "abc 漢字漢字 🐱🐱 de", "混合 mixed 文字列 text"]:
        for width in range(4, 20):
            segs = ui._wrap_input(text, width)
            assert segs[0][0] == 0 and segs[-1][1] == len(text)
            for (s, e), (s2, _) in zip(segs, segs[1:]):
                assert e == s2                            # contiguous, nothing dropped
            for s, e in segs:
                assert cols.width(text[s:e]) <= width, (text, width, segs)
    assert ui._wrap_input("漢" * 10, 8) == [(0, 4), (4, 8), (8, 10)]


def _row_at(win, prefix):
    for y, parts in win.rows.items():
        for x, s in parts:
            if x == 0 and s.startswith(prefix):
                return y, s
    raise AssertionError(f"{prefix!r} was not drawn: {win.rows}")


def test_the_input_cursor_column_counts_wide_characters(ui):
    win = FakeWin(30, 100)
    ui.input, ui.cur = "漢字ab", 2                       # cursor just after 漢字
    ui._draw(win)
    y, s = _row_at(win, "❯ ")
    assert s == "❯ 漢字ab"
    assert win.moves[-1] == (y, 2 + 4)                    # "❯ " + two 2-column characters


def test_the_input_cursor_on_a_wrapped_wide_line(ui):
    win = FakeWin(30, 30)                                 # input box: 26 columns
    ui.input = "漢" * 20
    ui.cur = 16
    ui._draw(win)
    y0, first = _row_at(win, "❯ ")
    assert first == "❯ " + "漢" * 13                      # 26 columns, not 26 characters
    assert win.moves[-1] == (y0 + 1, 2 + 3 * 2)           # 3 characters into line two


# --------------------------------------------------------------------------- #
# making text safe to draw
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("raw,clean", [
    ("a\x1b(Bb", "ab"),                                   # charset select (tput sgr0)
    ("\x1b7x\x1b8", "x"),                                 # save / restore cursor
    ("\x1b]0;my title\x07done", "done"),                  # OSC title, BEL-terminated
    ("\x1b]2;t\x1b\\ok", "ok"),                           # OSC title, ST-terminated
    ("\x1b[1;31mred\x1b[0m", "red"),
])
def test_clean_strips_terminal_escapes(raw, clean):
    assert cols.clean(raw) == clean


def test_visible_shows_what_would_be_hidden():
    assert cols.visible("C:\\new\tdir") == "C:\\new␉dir"
    assert cols.visible("a\rb") == "a␍b"
    assert cols.visible("x\x1b[31my") == "x␛[31my"
    assert cols.visible("del\x7f") == "del␡"
    assert cols.visible("two\nlines") == "two\nlines"


# --------------------------------------------------------------------------- #
# code fences
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("opening,lang", [
    ("```c#", "c#"),
    ("```python:src/app.py", "python"),
    ("``` python", "python"),
    ("```js {1,3}", "js"),
    ("~~~", ""),
    ("~~~bash", "bash"),
])
def test_split_fences_reads_the_info_string(opening, lang):
    close = "~~~" if opening.startswith("~") else "```"
    body = f"intro\n{opening}\ncode line\n{close}\noutro"
    assert list(syntax.split_fences(body)) == [
        ("text", "intro"), ("code", lang, ["code line"]), ("text", "outro")]


def test_split_fences_nested_four_backticks():
    body = "````markdown\n```py\nx = 1\n```\n````\nafter"
    assert list(syntax.split_fences(body)) == [
        ("code", "markdown", ["```py", "x = 1", "```"]), ("text", "after")]


def test_a_tilde_fence_is_not_closed_by_backticks():
    body = "~~~\n```\nstill code\n~~~\nprose"
    assert list(syntax.split_fences(body)) == [
        ("code", "", ["```", "still code"]), ("text", "prose")]


def test_inline_triple_backticks_are_not_a_fence():
    assert list(syntax.split_fences("```inline```\nmore")) == [("text", "```inline```\nmore")]


# --------------------------------------------------------------------------- #
# startup knobs
# --------------------------------------------------------------------------- #

def test_a_bad_idle_animation_value_falls_back(monkeypatch):
    monkeypatch.setenv("JOJO_IDLE_ANIMATION_TEST", "1.5")
    assert tui._env_seconds("JOJO_IDLE_ANIMATION_TEST", 20.0) == 1.5
    monkeypatch.setenv("JOJO_IDLE_ANIMATION_TEST", "soon")
    assert tui._env_seconds("JOJO_IDLE_ANIMATION_TEST", 20.0) == 20.0
    monkeypatch.delenv("JOJO_IDLE_ANIMATION_TEST")
    assert tui._env_seconds("JOJO_IDLE_ANIMATION_TEST", 20.0) == 20.0


# --------------------------------------------------------------------------- #
# the real thing, through a pty: an idle TUI goes quiet
# --------------------------------------------------------------------------- #

@pytest.mark.skipif(os.name != "posix", reason="needs a pty")
def test_an_idle_tui_stops_writing_to_the_terminal(tmp_path):
    import fcntl
    import pty
    import select
    import struct
    import subprocess
    import termios

    from fake_ollama import FakeModel, FakeOllama, pieces

    def read(fd, secs):
        end, buf = time.time() + secs, b""
        while time.time() < end:
            r, _, _ = select.select([fd], [], [], 0.05)
            if r:
                try:
                    buf += os.read(fd, 65536)
                except OSError:
                    break
        return buf

    def exited(pid, secs):
        end = time.time() + secs
        while time.time() < end:
            done, status = os.waitpid(pid, os.WNOHANG)
            if done:
                return status
            time.sleep(0.05)
        return None

    with FakeOllama(FakeModel("idle:1b", ["completion", "tools"], lambda b: pieces("ok"))) as srv:
        # the fake server is on 127.0.0.1: no proxy may stand in the way
        env = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
        env.update(TERM="xterm-256color", JOJO_DATA=str(tmp_path / "d"),
                   JOJO_CONFIG=str(tmp_path / "c"), PYTHONPATH=str(ROOT / "tui"),
                   JOJO_IDLE_ANIMATION="1")
        fd, slave = pty.openpty()
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
        claim = ("import os, sys, fcntl, termios;"
                 "fd = os.open(os.ttyname(0), os.O_RDWR); fcntl.ioctl(fd, termios.TIOCSCTTY, 0);"
                 "os.execv(sys.executable, [sys.executable] + sys.argv[1:])")
        proc = subprocess.Popen(
            [sys.executable, "-c", claim, "-m", "jojocode_ai", "--host", srv.url,
             "--model", "idle:1b", "--think", "off", "--no-mouse"],
            stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True)
        os.close(slave)
        pid = proc.pid
        try:
            read(fd, 1.5)
            os.write(fd, b" ")                         # skip the splash
            lively = read(fd, 0.6)
            assert lively, "the TUI drew nothing after a key — the test would prove nothing"
            assert exited(pid, 0.1) is None

            read(fd, 2.0)                              # 1 s of animation, then one long wait
            quiet = read(fd, 3.0)
            assert len(quiet) < 16, (
                f"an idle TUI wrote {len(quiet)} bytes in 3 s: {quiet[:200]!r}")

            os.write(fd, b"\x03")                      # idle, empty input: quit
            status = exited(pid, 10)
            assert status is not None, "Ctrl-C at an idle, empty prompt should quit"
            assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
        finally:
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass
            os.close(fd)
