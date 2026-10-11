# `server/backend/src/` — the backend service

TypeScript run directly by `node --experimental-strip-types`; there is no build
output. The wire contract with the TUI is `@jojoai/shared/protocol`
(`../../shared/src/protocol.ts`).

## How a remote turn flows

```
TUI (remote.py) ──ws /agent──▶ gateway.ts ── hello ─▶ auth.checkAuth ─▶ SessionManager.create/resume
                                   │
                                   └─ user_turn ─▶ orchestrator.runTurn
                                                     ├─ ollama.chat (streams thinking/content deltas back)
                                                     ├─ tool calls ─▶ dispatchTool ─▶ tool_request ─▶ TUI runs it
                                                     │                 ◀── tool_result ── deliverToolResult
                                                     └─ turn_done { usage, stopReason }
```

## Files

| File | What it does | Key exports / functions |
|---|---|---|
| `index.ts` | Entry point. One `node:http` server: health/status JSON, static files (landing page, installers, `/dl/`), the `/api/auth/*` proxy to the control plane, and the websocket gateway on `/agent`. | `sendFile` (path-jailed static file), `serveStatic` (`/`, `/install.sh`, `/install.ps1`, `/ollama-setup.sh`, `/recommend-model.py`, `/dl/…`), `proxyAuth`; routes `/healthz`, `/status.json`. |
| `config.ts` | Every setting, from environment variables with defaults (port 7420, model, seats, idle release 15 min, resume window 10 min, iteration and wall-clock limits, tool timeout). | `config`, `Config`. |
| `auth.ts` | Connect-time auth. With the control plane wired, posts the token to `/api/authz/check` and honours its verdict (fail-closed); otherwise compares against the shared demo code in constant time. | `checkAuth`, `recheckAuth`, `AuthResult`. |
| `gateway.ts` | The websocket server: `hello` (auth, new session or resume — `session_ready` says whether a turn is still running — and replay of unanswered tool requests), `user_turn` (refused while a turn runs), `tool_result`, `interrupt`, `heartbeat`, `bye`; idle-release close. A socket's late close detaches the session only if it is still that socket's. | `createGateway`. |
| `session.ts` | In-memory sessions: seat accounting, the resume token and window, idle timer, pending tool waiters. Lost on restart (persistence is planned; see `TODO(persist)`). | `SessionManager` (`create`, `resume`, `attach`, `detach`, `touch`, `end`, `seatsUsed`, `seatsFree`, `list`), `Session`, `ToolResultPayload`, `ToolWaiter`. |
| `orchestrator.ts` | The agent loop: model → tool calls → wait for results → repeat, up to `maxToolIterations` or the wall-clock deadline. Trims the transcript oldest-first to a character budget. A replayed request carries the time left on its original timer. | `runTurn`, `deliverToolResult`, `replayPending`; internal `dispatchTool`, `toolResultToContent`, `trim`, `ensureSystem`. |
| `ollama.ts` | Streaming `/api/chat` client with native tool calls, `thinking`, and abort. | `chat`, `health`, types `OllamaTool`, `OllamaMessage`, `ChatDelta`, `ChatResult`. |
| `tools.ts` | The tool schemas the server's model sees (`list_dir`, `read_file`, `write_file`, `run_command`, `web_search`, `fetch_url`), which are destructive (the client must ask the user), and the remote system prompt. Implementations are in the TUI's `remote_exec.py`. | `TOOLS`, `isDestructive`, `SYSTEM_PROMPT`. |

## Things that are easy to get wrong here

- **A new tool needs the TUI too.** A schema added in `tools.ts` that
  `remote_exec.py` does not implement comes back to the model as an error.
- **The client decides approvals.** `isDestructive` is a floor, not the
  policy: the TUI also asks for anything it does not know to be read-only
  (`tui/jojocode_ai/policy.py` `READ_ONLY`).
- **Close codes are part of the protocol.** The TUI treats `4410`
  (`RESUME_EXPIRED`) as "start a fresh session" and `4408` (`IDLE_CLOSED`) as
  "the seat was released"; change them only together with `remote.py`.
