# JojoAI — working notes for Claude

JojoAI (`jojo`) is a terminal AI coding agent: a curses TUI that reads and edits
files, runs commands, and drives a task end to end, with an approval prompt
before every write. It runs against a local Ollama, or against a hosted server.

**This is not JojoCode.** `~/src/JojoCode` is the teaching platform — a separate
product with its own repo, database and deploy. The two share the `jojocode.in`
domain (JojoAI lives at `ai.jojocode.in`) and nothing else. Do not assume a
finding in one applies to the other; that mistake has already been made once,
over mail configuration.

---

## Layout

```
tui/jojocode_ai/     the Python TUI (the product) — module map: tui/jojocode_ai/README.md
  tui.py             curses UI (the big one)
  cols.py            display width (CJK = 2), wrapping, making text safe to draw
  agent.py           local agent loop: model → tool_calls → tools → repeat
  backends.py        picks the model client: Ollama, or OpenAI-compatible (/v1)
  ollama_client.py   /api/chat client; reads model capabilities, shapes `think`
  openai_client.py   the same interface over /v1/chat/completions (SSE)
  transport.py       interruptible HTTP for both clients; loopback never proxied
  toolcalls.py       tool calls a model wrote as TEXT (JSON, fences, tags), <think>
  tools.py           LOCAL tool schemas + implementations + approval set
  policy.py          which writes auto-edit may approve; remote READ_ONLY set
  textfiles.py       encoding/BOM/line-ending detection, atomic writes (both modes)
  shell.py           runs one command (both modes): empty stdin, tree kill
  remote_exec.py     tool implementations used in REMOTE mode (separate copy!)
  remote.py          websocket client for remote mode
  wsjson.py          the RFC 6455 client remote.py talks through (proxy CONNECT)
  prompts.py         the system prompt, rebuilt per request
  auth.py            OTP login client + credential store
  net.py             the User-Agent every outbound request carries (see below)
  websearch.py       web_search / fetch_url: SSRF guard + untrusted envelope
  config.py          settings.json + JOJO_HOST / JOJO_MODEL precedence
  store.py           chat history (JSONL) + optional recall index (SQLite)
  headless.py        plain REPL / one-shot runner without curses
tui/tests/           pytest suite — see tui/tests/README.md
server/
  backend/           runs the agent loop for remote mode; serves ai.jojocode.in
  control-plane/     accounts, grants, audit, OTP  ← deployed, :7460 (Postgres)
  shared/            protocol, rbac, authorize, Prisma schema
  scripts/           backup-db.sh
deploy/              systemd user units (nightly DB backup)
install/             install.sh (Linux/macOS), install.ps1 (Windows)
web/index.html       the ai.jojocode.in landing page (one file; three.js from cdnjs)
web/bg-test.mjs      browser regression for the backdrop (needs playwright)
docs/                ARCHITECTURE.md, BUILD-PLAN.md, WEB-SEARCH-PLAN.md
current_tasks.md     what is in flight right now — read this first
```

Every folder has a README.md listing its files and their functions. Keep them
current when you add, move or rename something.

## The two modes (and the trap)

| | Local | Remote |
|---|---|---|
| Drives the model | TUI → Ollama or an OpenAI-compatible server | `server/backend` → Ollama |
| **Runs the tools** | this machine | **still this machine** |
| Schema read from | `tools.py` | `server/backend/src/tools.ts` |
| Implementation | `tools.py` `IMPLS` | `remote_exec.py` |

In remote mode the server sends `tool_request` and the TUI executes it locally,
path-jailed to the project root. Tools never run on the server.

> ### ⚠️ Adding a tool means touching three files
> `tools.py` (schema + impl), `tools.ts` (schema), `remote_exec.py` (impl).
> `remote_exec.py` does **not** import from `tools.py` — it has its own `_jail`,
> `_clip` and `_dispatch`. (Running a command and reading/writing text files
> are the exceptions: both modes go through `shell.py` and `textfiles.py`, so
> stdin, timeout, encoding and line-ending handling cannot drift.)
>
> **The lists differ.** Local advertises `list_dir, read_file, write_file,
> edit_file, run_bash, search, change_dir, web_search, fetch_url, finish`.
> Remote advertises `list_dir, read_file, write_file, run_command, web_search,
> fetch_url`. `run_bash`/`run_command` are one idea with two names. Check both
> lists before believing a tool exists.
>
> **Remote approvals are decided by the client.** `remote.py` asks for every
> tool the server marks destructive *and* every tool not in
> `policy.READ_ONLY` — so a tool the server adds later is gated by default.

## Conventions

- **Approval gate.** `APPROVAL_REQUIRED = {"write_file", "edit_file",
  "run_bash", "web_search", "fetch_url"}` in `tools.py` — everything that
  changes state, runs code, or sends anything off the machine (a URL's query
  string is an outbound channel). This is the main safety property of the
  product — do not weaken it to make a feature smoother. Auto-edit (`--trust`, `a`) is
  bounded by `policy.auto_write_ok`: inside the project root the *user* chose
  (not the model's `change_dir`), never dot-files, never when the root is `~`
  or `/`. The approval panel shows the full request through `cols.visible`.
- **Tool output is bounded.** Use the existing `_clip()`; never return a whole
  file or page into the model's context.
- **Tool errors are strings, not exceptions.** `run_tool()` catches and returns
  `error: …` so the model can read and recover. Keep that.
- **The prompt is part of the feature.** A tool `prompts.py` doesn't describe
  behaviourally is a tool the model won't reach for.
- **Not every model is gpt-oss.** The installer recommends qwen2.5-coder for
  most machines. Never send a request field only some models accept — ask
  `capabilities()` (`think` levels are gpt-oss only; `tools` is a 400 on
  codellama/gemma, which run in text mode). Model content always passes
  through `toolcalls.StreamFilter` before the UI sees it: qwen2.5-coder writes
  its tool calls as JSON *text*, and printing that was the bug.
- **Not every server is Ollama.** `backends.make_client` returns an
  `OpenAIClient` for hosts ending in `/v1`; both clients yield the same event
  dicts (`content`, `thinking`, `tool_calls`, `done`, `error`). Add a feature
  to both, or to neither.
- **Model HTTP goes through `transport.py`**, not bare urllib: it is what
  makes Ctrl-C work while a server is silent, and what keeps a proxy from
  swallowing `localhost`.
- **Text on screen goes through `cols.py`.** Widths are display columns, not
  `len()`; model and tool text is passed through `clean()` so escape sequences
  cannot reach the terminal.
- **Files are UTF-8, always.** Every text `open()` names `encoding=` — the
  locale default is cp1252 on Windows. `test_tools_platform.py` enforces it.
- Stdlib-only in the TUI where possible: the installer's promise is a venv and
  no build tools.

## Running and testing

```sh
npm test          # @jojoai/shared + @jojoai/control-plane + ws-resume spike
npm run typecheck
```

TUI tests are `tui/tests/` and need pytest in a venv; they are **not** wired
into `npm test` yet:

```sh
python3 -m venv .venv && .venv/bin/pip install pytest
.venv/bin/python -m pytest tui/tests -q       # fake Ollama / OpenAI servers, a pty
node web/bg-test.mjs                          # the landing page, in a browser
```

Nothing here has run on real Windows or against real model weights: the
Windows paths are unit-tested with `os.name` patched, and models are faked by
`tui/tests/fake_ollama.py`, which validates requests the way Ollama does.

## Deployment reality (verified 17 Sep 2026, afternoon)

- `https://ai.jojocode.in` is **live** and serves the **backend**: `/healthz`,
  `/status.json`, `/install.sh`, `/dl/`, and the landing page.
- The **control plane runs beside it** (`jojoai-control-plane.service`, :7460)
  and the backend proxies `/api/auth/*` to it. Sign-in works end to end: a code
  is mailed through Brevo, verify mints a token, and the gateway honours the
  control plane's verdict on connect.
- Config for both halves is one mode-600 `server/.env` (gitignored), read by
  both systemd units. `server/README.md` has the shape.
- One consequence of that wiring that is easy to trip over: **the demo access
  code no longer works** (the fallback only applies when the control plane is
  absent).
- **The control plane is on Postgres** since 21 Sep (`JOJOAI_CP_STORE=prisma`,
  database `jojoai` beside JojoCode's in the `cjudge-postgres` container, its
  own role). Restarts no longer sign anybody out. The Prisma client is
  gitignored: `npm run -w @jojoai/shared prisma:generate` after a fresh
  checkout. Nightly dump: `jojoai-backup.timer` → `backups/`. See
  `server/README.md`.

### Cloudflare bans the default urllib agent

`ai.jojocode.in` sits behind Cloudflare, whose browser-integrity check answers
`403 error code: 1010` to `User-Agent: Python-urllib/3.x`. That is what made
every hosted login fail with `otp request failed (HTTP 403)` — a message that
looks like an account problem and is a header problem. **Anything new that
makes an HTTP request from the TUI must go through `net.headers()`.**

---

## Open work — read `current_tasks.md` for the live list

Summary of where things stopped:

1. **Windows installer — diagnosed and fixed, not yet run on Windows.** Windows
   PowerShell 5.1 with `$ErrorActionPreference='Stop'` turns any native stderr
   line into a terminating error, even with `2>$null` — so pip's "new release"
   notice killed the install. Every native call now goes through
   `Invoke-Native`. `install/test-install-ps1.ps1` checks it with pwsh on Linux
   (`~/.local/opt/pwsh/pwsh`); the first real Windows install is still the
   test that matters.
2. **Publishing the wheel.** `dist/dl/tui-wheel` is a pointer file naming the
   current wheel; the installers read it. Building a new TUI without updating
   both means reinstalls silently keep the old client.

## Style

Match the existing voice in this repo: comments explain *why*, not *what*, and
name the failure a rule exists to prevent. Files here carry real reasoning in
their headers — keep that standard rather than reverting to summary comments.
