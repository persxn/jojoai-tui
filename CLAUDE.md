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
tui/jojocode_ai/     the Python TUI (the product)
  tui.py             curses UI (1111 lines — the big one)
  agent.py           local agent loop: model → tool_calls → tools → repeat
  ollama_client.py   /api/chat client; reads model capabilities, shapes `think`
  toolcalls.py       tool calls a model wrote as TEXT (JSON, fences, tags)
  tools.py           LOCAL tool schemas + implementations + approval set
  shell.py           runs one command (both modes): empty stdin, tree kill
  remote_exec.py     tool implementations used in REMOTE mode (separate copy!)
  remote.py          websocket client for remote mode
  prompts.py         the system prompt (43 lines)
  auth.py            OTP login client + credential store
  net.py             the User-Agent every outbound request carries (see below)
  websearch.py       web_search / fetch_url: SSRF guard + untrusted envelope
  config.py          settings.json + JOJO_HOST / JOJO_MODEL precedence
server/
  backend/           runs the agent loop for remote mode; serves ai.jojocode.in
  control-plane/     accounts, grants, audit, OTP  ← deployed, :7460 (memory store)
  shared/            protocol, rbac, authorize
install/             install.sh (Linux/macOS), install.ps1 (Windows)
web/index.html       the ai.jojocode.in landing page (single self-contained file)
web/bg-test.mjs      browser regression for the backdrop (needs playwright)
docs/                ARCHITECTURE.md, BUILD-PLAN.md, WEB-SEARCH-PLAN.md
current_tasks.md     what is in flight right now — read this first
```

## The two modes (and the trap)

| | Local | Remote |
|---|---|---|
| Drives the model | TUI → Ollama | `server/backend` → Ollama |
| **Runs the tools** | this machine | **still this machine** |
| Schema read from | `tools.py` | `server/backend/src/tools.ts` |
| Implementation | `tools.py` `IMPLS` | `remote_exec.py` |

In remote mode the server sends `tool_request` and the TUI executes it locally,
path-jailed to the project root. Tools never run on the server.

> ### ⚠️ Adding a tool means touching three files
> `tools.py` (schema + impl), `tools.ts` (schema), `remote_exec.py` (impl).
> `remote_exec.py` does **not** import from `tools.py` — it has its own `_jail`,
> `_clip` and `_dispatch`. (Running a command is the exception: both go
> through `shell.py`, so stdin and timeout handling cannot drift.)
>
> **This has already drifted.** Local advertises `list_dir, read_file,
> write_file, edit_file, run_bash, search, change_dir, finish`. Remote
> advertises `list_dir, read_file, write_file, run_command`. Four tools exist
> only locally, and `run_bash`/`run_command` are one idea with two names.
> Check both lists before believing a tool exists.

## Conventions

- **Approval gate.** `APPROVAL_REQUIRED = {"write_file", "edit_file",
  "run_bash"}` in `tools.py`. This is the main safety property of the product —
  do not weaken it to make a feature smoother.
- **Tool output is bounded.** Use the existing `_clip()`; never return a whole
  file or page into the model's context.
- **Tool errors are strings, not exceptions.** `run_tool()` catches and returns
  `error: …` so the model can read and recover. Keep that.
- **The prompt is part of the feature.** A tool `prompts.py` doesn't describe
  behaviourally is a tool the model won't reach for.
- **Not every model is gpt-oss.** The installer recommends qwen2.5-coder for
  most machines. Never send a request field only some models accept — ask
  `OllamaClient.capabilities()` (`think` levels are gpt-oss only; `tools` is a
  400 on codellama/gemma, which run in text mode). Model content always passes
  through `toolcalls.StreamFilter` before the UI sees it: qwen2.5-coder writes
  its tool calls as JSON *text*, and printing that was the bug.
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
.venv/bin/python -m pytest tui/tests -q       # 104 tests (fake Ollama, a pty)
node web/bg-test.mjs                          # the landing page, in a browser
```

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
