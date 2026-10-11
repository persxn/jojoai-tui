# Spike: server-side agent loop + reconnect/resume + exactly-once tools (F2)

**De-risks:** R7 / R8 — the riskiest part of decision **D2** (server-side agent
loop over a persistent WebSocket). If a client drops *while a tool is running*,
can it reconnect without the tool being executed twice, and without losing the
turn?

## Mechanism under test

- Every `tool_request` carries a **stable `id`**.
- The client keeps a **local journal**: `resumeToken`, `sessionId`, and
  `id → recorded result`.
- The server keeps each session alive for a **resume window** after a disconnect,
  tracking which `tool_request`s have no recorded `tool_result`.
- On reconnect (`hello` with `resumeToken`), the server **replays** every
  unanswered `tool_request` inside `replay_begin` / `replay_end`.
- The client, seeing an `id` already in its journal, **returns the stored
  result and does NOT re-run the side effect**.

The "side effect" in the spike is appending one line to `sideeffect.log`.
Exactly-once ⇔ that file has exactly one line per tool id, regardless of crashes.

## Files

| File | What |
|---|---|
| `server.mjs` | Minimal server-side loop: sessions, a resume window, and replay of unanswered `tool_request`s inside `replay_begin` / `replay_end`. In memory only. |
| `client.mjs` | The CLI stand-in: runs each tool (appends a line to the side-effect log), journals results by tool id, answers replays from the journal, and can be told to crash before or after executing. |
| `run.sh` | The harness: starts the server, runs scenarios A–D below, and counts side-effect lines to prove exactly-once. |

## Run

```bash
npm run spike:resume        # from repo root
# or: bash phase0/spike-ws-resume/run.sh
```

## Scenarios (12 assertions, all passing 2026-09-07)

| | Scenario | Asserts |
|---|---|---|
| A | Clean run | turn completes; side effect runs once |
| B | Crash **after** exec, **before** the result is sent → reconnect | side effect ran once pre-crash; resume replays; **exactly-once holds** (still 1 line) |
| C | Crash **before** exec → reconnect | nothing ran pre-crash; resume replays; runs exactly once overall |
| D | Stale/unknown `resumeToken` | server closes cleanly with "resume window expired" |

## What the real implementation adds (Phase 1)

- Session state persisted to Postgres (`AgentSession` + `AgentTurn` in
  `shared/prisma/schema.prisma`), not an in-memory `Map` — so resume also
  survives a **backend restart** (R8), not just a client crash.
- Auth on `hello`, seat acquisition, heartbeats, the idle reaper.
- The orchestrator emits real tool calls from the model instead of a stub.
- Bounded journal size / result size; replay ordering guarantees under
  multiple concurrent pending tools.
