<h1 align="center">JojoAI · <code>jojo</code></h1>

<p align="center">
  A terminal AI coding agent. Reads and edits files, runs commands, and drives a
  task end&nbsp;to&nbsp;end — with a curses UI, streamed reasoning, and an approval
  prompt before every write.
</p>

<p align="center">
  <b>Run it fully local</b> against your own Ollama or any OpenAI-compatible
  server, or point it at a <b>hosted server</b> and keep your laptop light.
</p>

```
❯❯ JojoAI                                              /\_/\
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ ( o.o ) ~

                     you · 14:22  ▐
  add a --json flag to the export command, plus a test

▌ jojo · 14:22
┊ export.py parses argv by hand — I'll add the flag there,
┊ branch the writer, and cover both shapes in test_export.py.
▸ read_file  cli/export.py

approve  write_file  cli/export.py  ?
│ + def _emit_json(rows): ...
│ + if args.json: return _emit_json(rows)
└─ [y]es  [n]o  [a]lways this session
```

---

## Quickstart

**Local** — one command, no root. Creates a private venv, installs the TUI, drops
a `jojo` launcher on your PATH, and offers to set up Ollama + pick a model.

```sh
curl -fsSL https://ai.jojocode.in/install.sh | sh     # Linux / macOS
irm https://ai.jojocode.in/install.ps1 | iex          # Windows (PowerShell)

jojo                        # in any project directory
```

Other model servers (LM Studio, llama.cpp, vLLM, Jan…): `jojo --host
http://127.0.0.1:1234/v1 --model <name>`. Models without native tool calling
(qwen2.5-coder, codellama, gemma…) work too — see
[`tui/COMPATIBILITY.md`](tui/COMPATIBILITY.md).

Or from source:

```sh
pipx install ./tui          # (or: pip install ./tui)
python3 install/recommend-model.py --run   # detect hardware → pull a model → launch
```

**Remote** — no local GPU needed:

```sh
jojo --remote https://ai.jojocode.in --login    # email + one-time code
```

The agent loop and the big model run on the server; **tool calls still execute on
your machine**, path-jailed to the project, and you approve every destructive one.

---

## What's in here

Every folder has a `README.md` naming its files and what each one does — start
from the one nearest the code you are looking at.

```
tui/                         Python curses client (stdlib-only; windows-curses on Windows)
  jojocode_ai/               the package: UI, agent loop, tools, model + remote clients
  tests/                     pytest suite (fake model server, real pty)
install/                     install.sh · install.ps1 · ollama-setup.sh · recommend-model.py (+ their tests)
web/                         ai.jojocode.in — one-file landing / docs / live status page
server/                      reference hosted service (self-host)
  control-plane/src/         always-on: email-OTP login, opaque tokens, authz check, audit
  backend/src/               WS gateway + agent orchestrator + Ollama client; serves web/ and install/
  shared/src/                wire protocol · authorize() · RBAC
  shared/prisma/             control-plane database schema + migrations
  scripts/                   backup-db.sh
deploy/                      systemd user units (nightly database backup)
docs/                        ARCHITECTURE.md · BUILD-PLAN.md · WEB-SEARCH-PLAN.md
  phase0/                    benchmarks and the exactly-once resume spike
CLAUDE.md                    working notes for AI agents (and people): modes, traps, conventions
current_tasks.md             what is in flight right now
```

| Looking for… | Read |
|---|---|
| Which TUI module does what, and where a symptom starts | [`tui/jojocode_ai/README.md`](tui/jojocode_ai/README.md) |
| Terminal / platform / model behaviour | [`tui/COMPATIBILITY.md`](tui/COMPATIBILITY.md) |
| How remote mode works end to end | [`server/backend/src/README.md`](server/backend/src/README.md), [`server/shared/src/README.md`](server/shared/src/README.md) |
| Sign-in and access decisions | [`server/control-plane/src/README.md`](server/control-plane/src/README.md) |
| Installing, and publishing a new client | [`install/README.md`](install/README.md), [`tui/README.md`](tui/README.md) |

Full design: **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**. Every task and what
was verified: **[docs/BUILD-PLAN.md](docs/BUILD-PLAN.md)**.

---

## Design notes worth a look

- **Exactly-once tools across a crash.** Every `tool_request` has a stable id; the
  client journals results. Kill the client mid-tool-call, reconnect — the server
  replays the unanswered request and the client answers from its journal without
  re-running the side effect. (`docs/phase0/spike-ws-resume/`, 12/12.)
- **A hand-rolled RFC&nbsp;6455 WebSocket client** (`tui/jojocode_ai/wsjson.py`) so
  the core client keeps *zero* runtime dependencies — no `websockets`, no `aiohttp`.
- **`authorize()` is one pure function** (`server/shared/src/authorize.ts`) —
  account status → time-boxed grant → open-hours → maintenance → payment, with an
  RBAC staff bypass. 10-case truth table; run on connect, on every message, and on
  a sweep, and **fail-closed** if the control plane is unreachable.
- **Opaque hashed tokens, email-OTP only.** No JWT, no long-lived password; revoke
  is one column write.
- **Benchmark-driven concurrency.** `OLLAMA_NUM_PARALLEL` and the inference-slot
  count come from a real measurement, not a guess (`docs/phase0/FINDINGS.md`).
- **The client's permission model is a security component** — path jail, approval
  wall, untrusted-output framing, crash-safe terminal restore.
- **One TypeScript runtime, no build step** — the backend runs under
  `node --experimental-strip-types`.

---

## Develop

```sh
npm install
npm test              # authorize() (10) · control-plane (7) · ws-resume spike (12)
npm run typecheck     # shared · backend · control-plane

# run the reference server locally, no database:
JOJOAI_SERVICE_TOKEN=dev JOJOAI_OWNER_EMAIL=you@example.com \
  node --experimental-strip-types server/control-plane/src/index.ts   # :7460
JOJOAI_CONTROL_PLANE_URL=http://127.0.0.1:7460 JOJOAI_SERVICE_TOKEN=dev \
  node --experimental-strip-types server/backend/src/index.ts         # :7420
```

The TUI: `cd tui && python3 -m jojocode_ai --help`. Terminal / platform notes in
[`tui/COMPATIBILITY.md`](tui/COMPATIBILITY.md).

---

## Status

Local mode, the installers, the landing page, and the auth + RBAC core are done
and tested. The multi-user seat/queue enforcement and the admin panel are in
progress — see the checklist in [`docs/BUILD-PLAN.md`](docs/BUILD-PLAN.md).

## License

[MIT](LICENSE).
