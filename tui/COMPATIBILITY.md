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
Reasoning a model writes inline in `<think>…</think>` is shown as reasoning
(hidden with `Ctrl-T`), never as the answer.

The context window comes from the model (`/api/show`) unless `--ctx` sets it;
old tool output is shortened first when a long session would overflow it, so
the newest results and the conversation itself survive.

### OpenAI-compatible servers

Any server speaking `/v1/chat/completions` works too: LM Studio, llama.cpp's
`llama-server`, vLLM, Jan, LocalAI, a hosted API. Point `--host` at a URL ending
in `/v1` (or pass `--api openai`):

```sh
jojo --host http://127.0.0.1:1234/v1 --model qwen2.5-coder-7b-instruct
JOJO_API_KEY=sk-… jojo --host https://api.example.com/v1 --model some-model
```

Tool calls go through the server's `tools` support when it has it; a server
that answers a `tools` request with an error (llama-server without `--jinja`,
a model with no tool template) is switched to the text protocol for the rest
of the session. `reasoning_content` streams as reasoning. A server that lists a
single model answers to any `--model`.

## Proxies

`HTTPS_PROXY` / `HTTP_PROXY` / `NO_PROXY` are honoured for the model server,
web tools and remote mode (the websocket tunnels through the proxy with
`CONNECT`). A model server on `localhost`, `127.0.0.1` or `::1` is always
reached directly — a corporate proxy that cannot see your machine's loopback
used to make a local Ollama unreachable.

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

`Ctrl-C` interrupts a running turn — the model stream and any command it is
running, process tree included — and denies a pending approval; with text in
the box it clears it; at an idle, empty prompt it quits. The TUI runs the
terminal in raw mode so Ctrl-C arrives as a key — in cbreak mode it was a
signal and quit the app mid-turn. Raw mode also means `Ctrl-Z` does not suspend
and `Ctrl-S`/`Ctrl-Q` are not flow control.

| Key | Action |
|---|---|
| `Enter` | send; while a turn runs, queue the message (it sends when the turn ends) |
| `Ctrl-A` / `Ctrl-E` | start / end of line |
| `Ctrl-W` | delete the word before the cursor |
| `Ctrl-U` / `Ctrl-K` | delete to start / end of line |
| `Ctrl-Y` | put back what the last delete removed |
| `↑` / `↓` | move between lines; on the first / last line, walk input history |
| `Tab` / `Shift-Tab` | cycle reasoning effort |
| `Ctrl-T` | show / hide reasoning |
| `Ctrl-R` | chat-history browser |
| `Ctrl-L` | redraw |

Auto-approve everything is `/yolo` only; no key toggles it, so it cannot be
switched on by a stray keystroke (`Ctrl-Y` used to). A message that itself
starts with `/` is sent with `//`; a mistyped command (`/hepl`) is reported,
not sent to the model, while a path such as `/etc/hosts …` is sent as text.

**Pasting.** Text pasted in one burst keeps its newlines instead of sending at
the first one. Terminals with bracketed paste (Windows Terminal, iTerm2, most
Linux terminals) mark the paste exactly; elsewhere, three or more characters
arriving in one read are treated as a paste. Pasted tabs become four spaces;
Windows CRLF becomes one newline.

**Approval prompt.** The panel shows the whole request — the full command,
path and working directory, an edit's old and new text, the first 60 lines of
a new file's content with the rest counted — with control characters made
visible (`␊`, `␛`) so nothing can hide after a newline or behind an escape
sequence. Keys typed in the first moment after it appears are ignored, so a
keystroke meant for the input box cannot approve it, and a paste never answers
it. `y` once, `n`/`Esc` deny,
`a` then `a` again within three seconds to always allow that tool for the
session; `PgUp`/`PgDn` scroll a long request.

**Auto-approved writes** (`--trust`, or `a` for `write_file` / `edit_file`)
apply only inside the project directory you started in — not wherever the
model has since moved with `change_dir` — and never to dot-files or
dot-directories (`.git`, `.bashrc`, `.env`), nor anywhere when the project is
your home directory or a filesystem root.

## Commands the agent runs

`run_bash` runs through the platform shell — `/bin/sh` on Linux and macOS,
**cmd.exe on Windows** (the system prompt tells the model which). Commands get
an empty stdin, so anything that waits for keyboard input fails fast instead of
hanging; output goes to temporary files, so a command that starts a server in
the background (`npm run dev &`) returns while the server keeps running. A
timeout or Ctrl-C stops the whole process tree; quitting stops anything still
running. Very long output keeps its beginning and end with the middle counted.
Files are always read and written as UTF-8 and keep their own line endings.

On Windows, two cmd.exe limits are refused before you are asked to approve
anything, with what to do instead: a multi-line command (cmd.exe would run only
the first line) and a project on a network (UNC) path (cmd.exe would silently
run in `C:\Windows`). Commands run without a console window of their own, so
nothing flashes up or steals the keyboard.

## Files the agent reads and writes

Text is decoded by what it is, not by the locale: a byte-order mark, UTF-16,
strict UTF-8, then the legacy code page. An edit is written back in the same
encoding, BOM and line endings, atomically (a crash never leaves half a file),
keeping its permissions and following symlinks. Binary files are refused
rather than read as garbage. JojoAI's own settings, credentials and chat
history are off limits to the tools.

## Known rough edges

- **Mouse on the classic Windows console.** Mouse capture is off by default in
  `conhost.exe` (no `WT_SESSION`): capturing it disables QuickEdit, so
  right-click paste and drag-select stop working, and Shift does not bring them
  back there. Use `PgUp`/`PgDn`, or `/mouse on` if you prefer the wheel.
- **Automated drivers** that type character by character should still pause
  before `Enter` when they mean it as a separate keystroke from the text.
- macOS Terminal.app has no `curses.init_color`, so the gradient is a 256-colour
  approximation. This is a Terminal.app limitation, not a bug.
- The header animation stops after 20 s without activity, so an idle session
  costs no CPU or terminal output (`JOJO_IDLE_ANIMATION=<seconds>` changes it).
