# `server/shared/src/` — protocol, `authorize()`, RBAC

Pure TypeScript with no I/O, so it can be imported anywhere and tested as a
truth table.

| File | What it does | Key exports |
|---|---|---|
| `index.ts` | Re-exports the three modules below. | — |
| `protocol.ts` | The TUI ↔ backend websocket contract: one JSON object per frame, discriminated by `t`. Client sends `hello`, `user_turn`, `tool_result`, `interrupt`, `heartbeat`, `bye`; server sends `session_ready`, `queued`, `assistant_delta`, `thinking_delta`, `tool_request`, `replay_begin` / `replay_end`, `turn_done`, `error`, `closing`, `heartbeat_ack`. Every `tool_request` has a stable `id` so a resumed session can replay it and the client answers from its journal without re-running the side effect; its `timeoutMs` is the time left on the server's timer. `session_ready` carries `turnActive` (is a turn still running after a resume?) and `model`. | `PROTOCOL_VERSION`, `CloseCode` (`4401` unauthenticated, `4403` access revoked, `4408` idle closed, `4410` resume expired, `4290` at capacity, …), `ClientMsg`, `ServerMsg` and each message type. |
| `authorize.ts` | The single access gate. Given an account, its grants and the service config, returns allow (with role) or deny with a reason: `no_account`, `not_approved`, `suspended`, `deleted`, `no_active_grant`, `not_yet_started`, `expired`, `revoked`, `out_of_hours`, `maintenance`, `payment_required`. Staff (`ADMIN`, `SUPERADMIN`) need no grant and ignore open hours and payment; maintenance still stops everyone but `SUPERADMIN`. Seat availability is deliberately not decided here. | `authorize`, `withinOpenHours`, `AuthzInput`, `AuthzResult`, `DenyReason`, `GrantSnapshot`, `OpenHours`. |
| `rbac.ts` | Three ranked roles (`USER` < `ADMIN` < `SUPERADMIN`) and what each may do (`session.start`, `grant.create`, `config.write`, `role.assign`, …). | `Role`, `Capability`, `ROLE_RANK`, `can`, `atLeast`, `capabilitiesOf`, `isStaff`. |
| `authorize.test.ts` | Truth table for `authorize()` (`node --test`). | — |

Generated at build time and gitignored: `generated/client/` (the Prisma
client, from `../prisma/schema.prisma`).
