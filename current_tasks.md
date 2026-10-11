# JojoAI — current tasks

Status is honest: `done` means verified, not merely written.

Repo: `~/src/jojo-ai` (JojoAI: Python TUI + reference server + landing page).
Related repo: `~/src/JojoCode` (the teaching platform — separate product, shares
the `jojocode.in` domain and its Brevo mailer).

---

## 11 Oct — models other than gpt-oss; TUI bugs on Windows and Linux

### Raw JSON instead of an answer on non-gpt-oss models — **fixed; verified against a fake Ollama, not a real model**
Three stacked faults, all reproduced first:
1. `think: "medium"` (the default) is a 400 "does not support thinking" on every
   model without thinking — including every qwen2.5-coder the installer picks.
   The client now reads `capabilities` from `/api/show`: levels for gpt-oss
   only, `true/false` for other thinkers, nothing for the rest; old Ollama is
   handled by learning from the 400.
2. qwen2.5-coder writes its tool call as JSON *text*; the agent printed it as
   the answer. `toolcalls.StreamFilter` now lifts calls out of the stream (bare
   JSON, fences, `<tool_call>`, `<function=…>`, `[TOOL_CALLS]`) and releases
   look-alikes (a JSON answer the user asked for) untouched.
3. `tools` is a 400 on codellama/gemma/phi. Those now run in text mode: tools
   described in the prompt, results sent back as user turns.
Also: a `finish` summary no longer vanishes after streamed prose, and is no
longer replaced by the output of whichever tool ran last; Ollama errors read as
sentences, not `{"error": …}`; `has_model` needs the exact tag.

Verified with `tui/tests/fake_ollama.py`, which validates `think`/`tools` the way
Ollama's `server/routes.go` does, and through the real curses TUI in a pty.
**Not** run against real model weights — this environment cannot reach the
Ollama registry. First thing to do on a real box: `jojo --model
qwen2.5-coder:7b` and one task that needs a tool.

### TUI bugs, Windows and Linux — **fixed, tested on Linux; Windows not run**
- Ctrl-C mid-turn quit the app (cbreak mode → SIGINT). Raw mode now; pty test.
- File tools and chat history used the locale codec — cp1252 on Windows: UTF-8
  source garbled, emoji/Hindi/CJK raised and failed the turn. UTF-8 everywhere,
  enforced by a static test.
- `edit_file` on Windows rewrote LF files as CRLF. Files keep their endings.
- `run_bash`: stdin was the terminal (prompts hung the agent), the timeout could
  not kill a server the command started (agent hung forever), and one
  undecodable byte lost the output. `shell.py`, shared with remote mode.
- `replace_all: "false"` replaced everything; `search` via ripgrep read the
  chat-history directory; the prompt told Windows to use `python3` and claimed
  there was no internet while offering web search.

### Found, deliberately not changed
- Remote mode ignores `[a]lways` at the approval prompt (`remote.py` only checks
  `*`), so it asks every time. Fixing it loosens the approval gate: owner's call.
- `run_bash` cannot be interrupted mid-command; Ctrl-C lands after it returns.
- The transcript is re-wrapped in full every frame (~30/s): CPU grows with the
  length of the chat.
- Wide characters are counted as one column (layout clipping, not data loss).
- `/cwd` and `change_dir` do not update the "Working directory" line in the
  system prompt.

---

## 21 Sep — finishing the plate

### Web search in JojoCode — **done, deployed, verified live**
Built as a property of JojoAI rather than of any one feature: `ask()` /
`askJson()` in `@cjudge/ai` take `web: { question, surface, actor }` and the AI
package does the rest — decides whether to search (the model writes a query or
declines), reads the pages, enforces the switch, a per-person (12/h) and
per-process (20/min) budget, a 6 h in-memory cache and a 20 s deadline, and
hands back the sources. Every failure is an ordinary answer without the web.
A new service gets it by passing `web`; nothing else to wire.

Wired to every surface you named: **JojoBot** (concept questions — "Sources:"
under the embed; never on profile/class-data routes), **the Forge** (teacher,
student, pasted questions, MCP), problem drafting, **hints**, playground
**explain**, **editorials** ("Further reading" block), ask-about-an-editorial,
the teacher assistant's general chat and MCQ explanations.

The four open decisions, settled:
1. Surfaces wired — above.
2. `AppConfig.webSearchEnabled` — migrated on the live DB after a fresh backup
   (additive column, default on, subordinate to `aiEnabled`); toggle on
   /teacher/settings, read per call by web, worker, Discord and MCP.
3. Rate limit — in `@cjudge/ai/web.ts`, on top of the bot's existing
   hourly/daily ask limits.
4. Caching — in memory only. No third-party pages are written to Postgres, so
   the publishing question does not arise.

Verified: all JojoCode suites green — 1,900 tests incl. the injection suite; deployed with
`deploy.sh`; worker and bot restarted; live check — the bot's concept answer to
"why does scanf skip my fgets" came back grounded in two real pages with
sources, ~15 s end to end. Commits `35f0de4` + the instrumentation fix, local
only (not pushed). Details: `~/src/JojoCode/docs/web-search.md` §5.

### Postgres for the control plane — **done, live, verified**
`PrismaStore` (`server/control-plane/src/prisma-store.ts`) against the existing
schema; first migration `20260921090000_init`. Database `jojoai` / role
`jojoai` in the `cjudge-postgres` container, isolated from JojoCode's data
(CONNECT on `cjudge` revoked from PUBLIC). One contract test runs against both
stores. Live: `store=prisma`; a token minted before a restart was still honoured
after it, then revoked. Nightly dump via `jojoai-backup.timer`.
This switch signed everyone out one last time; from now on restarts do not.

### Windows installer — **diagnosed and fixed; not run on Windows**
Root cause: Windows PowerShell 5.1 (what `irm | iex` gets on a stock machine)
with `$ErrorActionPreference='Stop'` makes any native stderr line a terminating
error, even with `2>$null`. pip's "a new release of pip" notice therefore
failed the wheel install, and the git fallback died with a raw exception.
Fixed with one `Invoke-Native` helper for every native call, exit codes checked
(venv creation, pip present — the Windows twin of the Linux ensurepip bug),
`$args` no longer shadowed, file made pure ASCII. `install/test-install-ps1.ps1`
proves the helper under pwsh 7 here; already live (the backend serves
`install/` from disk).

### CLAUDE.md — **updated** to match the above.

---

## 17 Sep — the four you asked for

### 1 · Login from your terminal — **done, verified**
Two separate faults, stacked:

1. **Cloudflare answered 403 before the request reached the server.** urllib
   sends `User-Agent: Python-urllib/3.13` when nobody sets one, and the browser
   integrity check bans that string (`error code: 1010`). Reproduced, then fixed
   in `tui/jojocode_ai/net.py` — every outbound request, including the websocket
   upgrade, now names the product. `tui/tests/test_net.py` is the regression.
2. **The control plane was not running, and the backend did not know where it
   would be**, so the endpoint answered `503 sign-in is not configured`. It runs
   now as `jojoai-control-plane.service`, mail wired to the same Brevo relay
   JojoCode uses, with the owner's address seeded as SUPERADMIN.

Verified: a code was mailed (control-plane log, 15:54), request→verify→token
mints a 12 h SUPERADMIN session, a client installed from the published wheel
gets 200 where it used to get 403, and the websocket reaches the gateway through
Cloudflare and is answered by the control plane's verdict.

**You must reinstall to get the fix** — the client is what was banned. Wheel
0.2.1 is published at `/dl/`, so re-running the installer is enough.

Known limits, written down rather than discovered later:
- `JOJOAI_CP_STORE=memory` — a control-plane restart signs everyone out.
  Accounts are re-seeded; tokens are not. Postgres before this matters.
- The **demo access code no longer works**. Real auth is now wired, and
  `checkAuth` only falls back to it when the control plane is absent.

### 2 · The TUI opened on a model the machine had not pulled — **done**
The installer recommended and pulled qwen2.5-coder:7b; `jojo` still opened on
gpt-oss:120b, because the recommendation had nowhere to be written down. There
is now `~/.config/jojocode-ai/settings.json`, written by `recommend-model.py`
after a successful pull and read by the TUI. `--model` > `JOJO_MODEL` > that
file > the built-in default. `/model` shows and `/model <tag> --save` sets it.
Arrives with the reinstall.

### 3 · The landing page backdrop — **done, verified in a browser**
It froze past `scrollY > innerHeight * 1.6`, which was written into the loop.
Gone: it animates the whole way down and the scroll now drifts the camera.
Rewritten as glowing green embers with a real half-life (2.6 s, six to a cycle)
and a two-lobe additive glow — hot core, wide halo, white at the centre.
`web/bg-test.mjs` asserts the regression in a real browser.

### 4 · Web search — **scope settled, shared machinery built, no surface wired**

**Your decision: every surface.** The TUI agent while coding, editorials, the
Forge, student hints and explain, and JojoBot in Discord — not one pilot.

Built and tested in `~/src/JojoCode` (156 tests in `@cjudge/ai`, none touching
the internet):

- `packages/ai/src/websearch.ts` — search (keyless DuckDuckGo, or Brave with a
  key), fetch-one-page-as-text, an SSRF guard that re-resolves on **every**
  redirect hop, and the untrusted-content envelope.
- `packages/ai/src/research.ts` — the step all five surfaces share: write one
  query, read the best two or three results, return a digest plus its sources.
  **The model never names a URL** — it writes a search phrase and this picks
  what to read, which is what keeps a student's pasted text from becoming a
  second prompt. A search engine that is down costs the background reading, not
  the student's hint.
- `packages/ai/test/no-guard-bypass.test.ts` — the build fails if the guard's
  one test-only off-switch ever appears under `src/`.
- `docs/web-search.md` there holds the recon, the design and the rollout order.

**The TUI already has it.** `web_search` and `fetch_url` are live in both local
and remote modes, behind the approval gate. Nothing to do there.

**Not built, and deliberately not done quietly** — each needs your go-ahead
because JojoCode is a live class:

1. Wiring the four JojoCode surfaces (a prompt change and a source list each).
2. `AppConfig.webSearchEnabled` — a Prisma migration against the live database.
3. A per-user rate limit, needed before Discord goes on: one question can cost
   three page fetches.
4. Whether fetched pages are cached in Postgres — a new table of third-party
   content, which is a publishing decision as much as a technical one.

---

## Still open

- **A real Windows install.** The fix is reasoned and tested under pwsh 7 on
  Linux; nobody has run it on Windows 5.1 yet.
- **Push both repos.** JojoCode `35f0de4` and the JojoAI changes are local
  commits; nothing has been pushed.
- **"A lot of other things to do"** — still awaiting your list.
