# `server/backend/` — `@jojoai/backend`

The host-side half of remote mode, and the process that serves
`https://ai.jojocode.in`. It runs the **agent loop** against the server's
Ollama and streams it to the TUI over a websocket. **Tools never run here:**
each call is sent to the client as a `tool_request`, executed on the user's
machine by `tui/jojocode_ai/remote_exec.py`, and the result comes back.

It also serves the landing page, the installers and `/dl/`, and proxies
`/api/auth/*` to the control plane (`../control-plane/`).

| Path | What |
|---|---|
| `package.json` | Workspace package. `start` / `dev` run `src/index.ts` under `node --experimental-strip-types` (no build step); `typecheck` runs `tsc`. Depends on `@jojoai/shared`, `ws`. |
| `tsconfig.json` | Extends `../../tsconfig.base.json`; type-check only (`noEmit`). |
| `src/` | The service. See [`src/README.md`](src/README.md). |

## Run

```sh
# demo auth (no control plane): `jojo login --code jojo-demo`
JOJOAI_DEMO_CODE=jojo-demo node --experimental-strip-types src/index.ts       # :7420

# real auth, wired to a local control plane
JOJOAI_CONTROL_PLANE_URL=http://127.0.0.1:7460 JOJOAI_SERVICE_TOKEN=dev \
  node --experimental-strip-types src/index.ts
```

Every setting is an environment variable read in `src/config.ts`; production
reads them from the mode-600 `server/.env` (see `../README.md`).

## Where to look

| Symptom | File |
|---|---|
| Login answers `503 sign-in is not configured` | `JOJOAI_CONTROL_PLANE_URL` unset → `src/index.ts` `proxyAuth` |
| Websocket closes `4401` / `4403` | `src/auth.ts` (`checkAuth`), then the control plane's `/api/authz/check` |
| Session closes `4408` / `4410` | `src/session.ts` (idle release, resume window) |
| A tool call never answers | `src/orchestrator.ts` `dispatchTool` (`toolTimeoutMs`), then the client's `remote.py` |
| Model errors / no output | `src/ollama.ts` `chat`, `health` (also `/status.json`) |
| Tool list differs from local mode | `src/tools.ts` vs `tui/jojocode_ai/tools.py` (they are separate on purpose; see the root `CLAUDE.md`) |
