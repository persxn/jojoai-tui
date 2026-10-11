# `server/shared/prisma/` — control-plane database schema

The Postgres schema for the control plane (database `jojoai`, its own role, a
separate database from JojoCode's in the same container). Production has run
on it since 21 Sep 2026 (`JOJOAI_CP_STORE=prisma`).

| Path | What |
|---|---|
| `schema.prisma` | The data model. Generator writes the client to `../src/generated/client` (gitignored); the URL comes from `JOJOAI_DATABASE_URL`. |
| `migrations/` | SQL migrations applied by `prisma migrate deploy`. See [`migrations/README.md`](migrations/README.md). |

## Models in `schema.prisma`

| Model | Holds |
|---|---|
| `Account` | One person: email (stored lower-case), display name, `AccountStatus`, `Role`, `AccessMode` (free / payment required), approval and suspension history. |
| `AccessGrant` | A time-boxed right to use the service ("rental"): start, end, revocation. |
| `AccessRequest` | Someone asking for access, and its review status. |
| `OtpChallenge` | A login code: only its hash, expiry, attempts, consumed time. |
| `AuthSession` | A signed-in token: only its hash, expiry, last seen, revocation. |
| `AgentSession`, `AgentTurn` | Agent sessions and their turns (for persistence; the backend still keeps these in memory). |
| `SessionQueueEntry` | Waiting for a seat, and how it ended. |
| `Payment` | A payment linked to a grant. A grant marked paid by hand gets a zero-amount `manual:<grantId>` payment so "paid ⇔ a PAID payment is linked" stays true. |
| `AuditEvent` | Who did what to whom, and when. |
| `ServiceConfig` | One row of service-wide settings: enabled / maintenance message, seat cap, idle release, queue policy, inference concurrency, per-user rate limits, session TTL, open hours (`openHoursJson`). |

## Changing the schema

1. Edit `schema.prisma`.
2. Add a migration directory under `migrations/` (for example
   `npx prisma migrate dev --create-only --schema prisma/schema.prisma`
   against a scratch database).
3. Deploy with `prisma migrate deploy`, then
   `npm run -w @jojoai/shared prisma:generate`.
4. Update `server/control-plane/src/store.ts`, `prisma-store.ts` and
   `store.test.ts` together.
