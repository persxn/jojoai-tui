# Terminal & platform compatibility

The TUI is **stdlib-only** (`curses`, `urllib`, `sqlite3`, `threading`). The only
third-party dependency is `windows-curses`, and only on Windows.

## Support matrix

| Platform | Terminal | Status |
|---|---|---|
| Linux | any VTE / xterm / kitty / alacritty / tmux | Full — truecolor gradient, italics, wide glyphs |
| macOS | Terminal.app | Works; 256-colour approximation of the gradient (no `init_color`) |
| macOS | iTerm2 / kitty / WezTerm / Ghostty | Full truecolor |
| Windows | **Windows Terminal** (`pip install windows-curses`) | Full — recommended |
| Windows | legacy `conhost.exe` | Runs, but box-drawing/emoji may render as tofu; use Windows Terminal or WSL |
| any | `TERM=dumb`, no colours, `NO_COLOR=1` | Degrades to bold/dim/reverse attributes; layout intact |
| any | very small window (`< 40x18`) | Boot splash skipped; `< 12x4` shows "terminal too small" until resized |

## Graceful degradation, by capability

- **No `curses`** (Windows without `windows-curses`): `jojo` prints how to fix it
  and points at `--headless`. Exit code 2.
- **No truecolor** (`can_change_color()` false): the 8-stop green gradient collapses
  to the terminal's single green; every pair still resolves.
- **No colour at all**: `Theme.init()` falls back to `A_BOLD` / `A_DIM` / `A_REVERSE`
  / `A_ITALIC`. All shortcuts, the cat, and the layout still work.
- **Resize**: handled every frame via `getmaxyx()`; `KEY_RESIZE` forces a clean redraw.
- **Crash**: `curses.wrapper` restores the terminal, then the traceback is written to
  `~/.local/share/jojocode-ai/crash.log` (never dumped over the restored screen).

## Models

Local mode works with any chat model Ollama serves, not only gpt-oss. Before the
first request the client reads the model's `capabilities` from `/api/show` and
shapes the request to fit:

| The model has | What is sent | Examples |
|---|---|---|
| tools + thinking, gpt-oss | tools, `think: "low"/"medium"/"high"` | gpt-oss:20b, gpt-oss:120b |
| tools + thinking | tools, `think: true/false` | qwen3, deepseek-r1 (recent) |
| tools, no thinking | tools, no `think` | qwen2.5-coder, llama3.1/3.2, mistral |
| no tools | tools described in the system prompt instead | codellama, gemma, phi |

Many models write tool calls into their reply as text rather than through
Ollama's `tool_calls` field — qwen2.5-coder as bare JSON, others fenced, in
`<tool_call>` tags, as `<function=…>` or after `[TOOL_CALLS]`.
`jojocode_ai/toolcalls.py` lifts those out before they reach the screen, so the
tool runs instead of its JSON being printed. Ollama too old to report
capabilities still works: the client learns from its 400 and retries.

A model that cannot think simply does not: `Tab` / `/think` change nothing for it.

## Scrolling the conversation

| Gesture | Action |
|---|---|
| mouse wheel | scroll the transcript (3 lines/notch) |
| `PgUp` / `PgDn` | scroll a page |
| `Shift-↑` / `Shift-↓` | scroll one line |
| `Home` / `End` | jump to the top / to the newest — only when the input box is empty |

The wheel needs the terminal to report mouse events. The TUI turns that on at
start; `--no-mouse` (or `/mouse off` at runtime) leaves the wheel to the
terminal's own scrollback instead. While capture is on, hold **Shift** (or
**Option** on macOS) to select text with the mouse.

- **Inside `tmux`**, the wheel only reaches the app when tmux itself has
  `set -g mouse on`; otherwise use `PgUp`/`PgDn`.
- While you're scrolled up, new output does **not** yank the view down — the
  status line shows `↑N scrolled — End for latest`. Sending a message, or `End`,
  returns to the bottom.

## Keys

`Ctrl-C` interrupts a running turn (and denies a pending approval); at an idle,
empty prompt it quits. The TUI runs the terminal in raw mode so Ctrl-C arrives
as a key — in cbreak mode it was a signal and quit the app mid-turn. Raw mode
also means `Ctrl-Z` does not suspend and `Ctrl-S`/`Ctrl-Q` are not flow control.

## Commands the agent runs

`run_bash` runs through the platform shell — `/bin/sh` on Linux and macOS,
**cmd.exe on Windows** (the system prompt tells the model which). Commands get
an empty stdin, so anything that waits for keyboard input fails fast instead of
hanging; a timeout stops the whole process tree, servers it started included.
Files are always read and written as UTF-8 and keep their own line endings.

## Known rough edges

- **Wide characters** (CJK, most emoji) are measured as one column each, so a
  line full of them can be clipped at the right edge or misalign the right-hand
  "you" bubble.

- **Paste detection** keeps newlines (instead of submitting) only while a run of
  ≥2 keystrokes is arriving back-to-back (`< 12 ms` apart). A normal Enter after
  any pause always sends. Automated drivers should still pause before `Enter`.
- macOS Terminal.app has no `curses.init_color`, so the gradient is a 256-colour
  approximation. This is a Terminal.app limitation, not a bug.
