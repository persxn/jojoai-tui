# Build plan — JojoCode AI TUI (public monorepo)

> Working checklist for turning this repo into a single public monorepo:
> an open-source terminal AI coding agent that runs **fully local against your
> own Ollama**, plus a **reference hosted service** (rental + RBAC) whose
> secrets live only in `.env.example`.
>
> Each task leaves the repo in a working state. Checked items are done.

## Target layout

```
jojoai-tui/              (public)
  tui/                   Python curses TUI — the JojoCode AI TUI. All platforms
                         (windows-curses on Windows). Two modes:
                           local  — drives your own Ollama directly
                           remote — OTP login + WebSocket to a hosted server
  install/               install.sh · install.ps1 · ollama-setup.sh · recommend-model
  web/                   ai.jojocode.in — landing + docs + live status (static; served by server/backend)
  server/                reference hosted service (self-host; bring your own infra)
    backend/             host-side WS gateway + agent orchestrator + Ollama client
    shared/              Prisma schema (control-plane DB) + CLI<->backend wire protocol
    admin/               minimal standalone admin panel (accounts, grants, sessions, config, audit)
  docs/                  ARCHITECTURE.md, findings, this build log
  .env.example
  README.md              recruiter-facing: what/why, architecture, quickstart, build story
  LICENSE
```

## Decisions driving this plan

- **One public monorepo.** OSS local TUI + installers + reference hosted service together.
  Nothing secret in git — only `.env.example`. Nothing pushed until the owner says so.
- **Python-only TUI.** The Go prototype in `cli/` is retired (kept in git history).
  The Python curses TUI is the single client on every platform; no aesthetic compromise.
- **Web frontend = landing + docs + live status.** No in-browser agent.
- **Model = `gpt-oss:120b`** via local Ollama for the TUI's local mode. A companion
  web app can point its own model config at the same local Ollama. Localhost testing only.

## Tasks

- [x] **T1 — Restructure skeleton + this plan.** New dirs; `SPEC/DISCOVERY-LOG/IMPLEMENTATION-PLAN`
      and `phase0/` moved under `docs/`; script/gitignore paths fixed; resume spike still green.
- [x] **T2 — Move server code, keep it building.** `backend/` -> `server/backend/`,
      `shared/` -> `server/shared/`; npm workspaces + tsconfig `extends` depth + `distDir` fixed;
      `cli/` (Go prototype) removed. Prisma validate + both typechecks + resume spike (12/12) pass.
- [x] **T3 — Port the Python TUI into `tui/`.** Package `jojocode_ai` + `pyproject.toml`, `jojo` entry point. Verified: compiles, `--version`/`--help`, headless agent loop (built a file via local Ollama), curses UI driven under tmux (`/help`, `/quit`). Splash + cat + gradient rules + meows intact.

- [x] **T4 — TUI hardening + cross-platform.** `windows-curses` dep; friendly no-`curses` message; crash log to `~/.local/share/jojocode-ai/crash.log` (terminal restored first); `KEY_RESIZE` redraw; tiny-terminal guard. `tui/COMPATIBILITY.md` written. Verified tiny-term + mid-run resize + clean quit.

- [x] **T5 — TUI remote mode.** `jojo --remote URL [--login | --code]`. Hand-rolled stdlib
      WebSocket client (`wsjson.py`, RFC 6455 client framing — keeps the core dependency-free).
      `RemoteAgent` presents the *same* event stream as the local `Agent`, so `tui.py` is
      untouched (just an injected `agent_factory`). hello/session_ready, streamed
      thinking+assistant, `tool_request` -> local path-jailed exec (`remote_exec.py`) + approval
      on destructive -> `tool_result`; per-id journal = exactly-once across reconnect; heartbeat
      thread; OTP + demo-code auth in `auth.py` (token stored 0600, keyed by endpoint).
      Verified end-to-end against the real backend: streamed reply + a `list_dir` tool
      round-trip both PASS. Also fixed the stale `jojoai-backend.service` ExecStart path left by
      the T2 move (daemon-reloaded; live demo untouched, picks it up on next restart).
- [x] **T6 — `install/ollama-setup.sh`.** POSIX `sh`; installs Ollama (official script on Linux,
      brew on macOS), starts it (systemd unit if present, else backgrounded `ollama serve`), sets
      `OLLAMA_NUM_PARALLEL`, waits for `:11434` to answer. Idempotent. `sh -n` clean.
- [x] **T7 — `install/recommend-model.py`.** stdlib only. Detects RAM, cores, NVIDIA VRAM,
      and unified memory (Apple arm64 + Grace/Tegra/GB10 via `/proc/device-tree/model` and
      `nvidia-smi -L`). Ranks `gpt-oss:120b`→`20b`→`qwen2.5-coder:{14b,7b,3b}` by a conservative
      usable-GB budget. `--pull` / `--run` / `--json`. On a unified-memory host: correctly → `gpt-oss:120b`.
- [x] **T8 — `install/install.sh` + `install.ps1`.** No root: Python ≥3.9 check, private venv,
      pip-install the TUI (PyPI → git fallback), `jojo` launcher on PATH, optional chaining of
      T6+T7. `install.sh` verified end-to-end into a sandbox prefix (venv + install + launcher +
      headless run). `install.ps1` syntax-reviewed (no Windows host to run it here).
- [x] **T9 — `web/` landing + docs + status.** `web/index.html` — one file, 32&nbsp;KB, no libs.
      Editorial treatment: the page *is* a terminal. Committed single-theme (green-on-near-black),
      one face (JetBrains Mono at 400–800), palette lifted verbatim from `tui/jojocode_ai/theme.py`
      (`--panel` is the app's own code-block bg). Hero is the TUI rendered in HTML at rest — the
      `❯❯ JojoAI` shimmer wordmark, the cat, a `┊` thinking gutter, a `write_file` approval prompt.
      Section headers are `❯ name` prompts; dividers are the app's `theme.rule()` gradient. Content
      is prose + hairline tables, not a card grid. Sections: run it · local/remote (+ inline SVG
      flow) · the loop · models · **every keyboard shortcut** · install · security · live status
      (from `/status.json`, degrades to offline). Backend serves `/` from `web/` + the installer
      scripts from `install/`. HTML structure-validated; published as a preview artifact.
- [x] **T10 — `server/` auth + RBAC.** `shared/authorize.ts` — pure decision fn (status → grant →
      open-hours → maintenance → payment, staff bypass), **10/10** truth-table tests. `shared/rbac.ts`
      — `USER/ADMIN/SUPERADMIN` ranked + capability map. `Role` added to the Prisma schema.
      `server/control-plane/` — standalone `node:http` service: `/api/auth/otp/{request,verify}`
      (anti-enumeration, attempt caps, rate limit), opaque tokens (`randomBytes(32)`, only SHA-256
      stored), `/api/authz/check` (service-token-gated) for the backend, `/api/auth/logout`.
      In-memory store now, Prisma-store seam for prod. **7/7** integration tests. Backend gateway
      now calls the control plane on connect (fail-closed; demo-code fallback when unwired).
      `server/.env.example` written. **E2E sanity PASS**: OTP → token → backend authz → `RemoteAgent`
      `list_dir` round-trip; bad token rejected.
- [ ] **T11 — `server/` WS gateway authz + seats/queue + rental.** authz on connect + per-message
      + periodic sweep; seat cap, idle release, queue|reject, orphan reaper; time-boxed
      `access_grant` activate/extend/revoke; Razorpay toggle stubbed via `.env.example`.
- [ ] **T12 — `server/admin/` panel.** Standalone (not embedded in any host web app):
      accounts, grants, live sessions + force-disconnect, config knobs, audit log. SuperAdmin login.
- [ ] **T13 — Model wiring + localhost tests.** Point a companion web app's model config at
      local `gpt-oss:120b` and exercise its AI feature; run the jojo-ai end-to-end
      `install -> jojo -> agent loop` on localhost, no tunnel. Results recorded in `docs/`.
- [x] **T14 — Docs + sanitisation (for the public push).** Root `README.md` rewritten;
      `docs/ARCHITECTURE.md` written; per-dir READMEs in place; `LICENSE` (MIT). The three
      discovery docs were trimmed to `ARCHITECTURE.md`; hardware and host-coupling references
      generalised. Secret audit: no `*.token` / `.demo-code` / `.cf-api-token` / `.env` tracked.
      `CONTRIBUTING.md` still to write.

## Fixes on top of the task work

- **Installers are turnkey.** `install.sh` / `install.ps1` now auto-install Python
  (+venv/pip) via the platform package manager (apt/dnf/pacman/zypper/apk/brew;
  winget on Windows), then Ollama, then a fitting model — a bare machine to a
  working `jojo` in one command. `JOJO_NO_OLLAMA=1` / `JOJO_MODEL=<tag>` to steer.
  Served live from `ai.jojocode.in` (no restart — the backend reads `install/`
  per request).

- **TUI scrolling** — the transcript could not be scrolled: the mouse was disabled
  (`mousemask(0)`), so terminals translated the wheel into arrow keys, which the
  loop routed to *input history*. Now: wheel capture on by default (`--no-mouse` /
  `/mouse` to disable), `PgUp`/`PgDn` page, `Shift-↑/↓` line, `Home`/`End` top/latest
  (empty input only). Streaming output no longer drags the view while scrolled up;
  a `↑N scrolled` indicator shows when you're not at the bottom. Also tightened the
  paste-burst heuristic so a fast `Enter` sends instead of inserting a newline.
  Verified via tmux (PgUp/End math + an X10 wheel sequence → scroll).

## Not blocking, needed later

- A companion web app's model config for **T13**.
- `CONTRIBUTING.md`.
