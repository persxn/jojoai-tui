# `server/` — reference hosted service

Everything needed to offer the TUI as a rentable, multi-user service. This is a
**reference implementation**: self-host it, bring your own infra, and put your
own secrets in `.env` (only `.env.example` is in git).

| Path | What | Status |
|---|---|---|
| `shared/` | Prisma schema (control-plane DB), the CLI↔backend wire protocol, and two pure libs: `authorize.ts` (the access-decision function) + `rbac.ts` (roles + capabilities). [README](shared/README.md) | T2, T10 |
| `control-plane/` | Always-on `node:http` service: email-OTP login, opaque hashed tokens, `POST /api/authz/check` for the backend. In-memory store (dev/CI) or Prisma (prod). [README](control-plane/README.md) | T10 |
| `backend/` | host-side WebSocket gateway — auth on connect via the control plane (fail-closed), the server-side orchestrator loop, the Ollama client; serves the landing page and installers. Seat/queue/idle/reaper next. [README](backend/README.md) | T11 |
| `scripts/` | `backup-db.sh`, the nightly control-plane database dump. [README](scripts/README.md) | — |
| `.env.example` | The variables both services read, with placeholders. Copy to `.env` (mode 600, gitignored). | — |
| *(admin panel)* | Planned, not in the repository yet: accounts, grants, live sessions, config, audit. | T12 |

Auth is email + OTP (no long-lived password), opaque tokens (only the SHA-256 is
stored), RBAC (`SUPERADMIN` / `ADMIN` / `USER`), and time-boxed access grants
("rental") with an optional per-account paid toggle. Secrets: `../server/.env.example`.

### Run it locally (no database)

```sh
# control plane
JOJOAI_SERVICE_TOKEN=dev JOJOAI_OWNER_EMAIL=you@example.com \
  node --experimental-strip-types control-plane/src/index.ts      # :7460

# backend, wired to it
JOJOAI_CONTROL_PLANE_URL=http://127.0.0.1:7460 JOJOAI_SERVICE_TOKEN=dev \
  node --experimental-strip-types backend/src/index.ts            # :7420

npm test        # authorize (10) + control-plane (7) + ws-resume spike (12)
```

### Running it for real (what `ai.jojocode.in` does)

Both halves read one mode-600 `server/.env`; nothing lives in a unit file,
because a unit is world-readable and `systemctl cat` prints it.

```ini
# ~/.config/systemd/user/jojoai-control-plane.service
EnvironmentFile=%h/src/jojo-ai/server/.env
ExecStart=node --experimental-strip-types %h/src/jojo-ai/server/control-plane/src/index.ts
```

The backend unit reads the same file, which is what sets
`JOJOAI_CONTROL_PLANE_URL` — **without it every login attempt is answered
`503 sign-in is not configured on this server`**, whatever the client does.

Two things that follow from wiring it up, and surprise people:

- **The demo code stops working.** `checkAuth` only falls back to
  `JOJOAI_DEMO_CODE` when the control plane is *not* configured. Once it is,
  every token is minted by OTP.
- **Production uses Postgres** (`JOJOAI_CP_STORE=prisma` +
  `JOJOAI_DATABASE_URL`, since 21 Sep). Database `jojoai`, role `jojoai`, in the
  same Postgres container as JojoCode but a separate database the `jojoai` role
  alone can reach (JojoCode's `cjudge` database has CONNECT revoked from
  PUBLIC). Tokens now survive a restart. `JOJOAI_CP_STORE=memory` is still the
  default for dev/CI, and it still forgets everything on restart.
- **Schema changes:** edit `shared/prisma/schema.prisma`, add a migration under
  `shared/prisma/migrations/`, `prisma migrate deploy`, then
  `npm run -w @jojoai/shared prisma:generate` (the client is gitignored — a fresh
  checkout must generate it before the control plane will start with `prisma`).
- **Backups:** `server/scripts/backup-db.sh`, nightly at 02:40 via the user timer
  `jojoai-backup.timer` (`deploy/`). Dumps land in `backups/` (gitignored, 30 days).
  JojoCode's own backup does not cover this database.
- **Tests against Postgres:** `JOJOAI_TEST_DATABASE_URL=…/jojoai_test` runs the
  store contract (`control-plane/src/store.test.ts`) against both stores. That
  database is wiped by the test — never point it at `jojoai`.

Mail is `JOJOAI_MAIL=smtp` plus `JOJOAI_SMTP_HOST/PORT/USER/PASS`. Four plain
variables rather than a URL: a relay key is long and lands in the password
position, where an unescaped `@` or `/` silently truncates it.

> See [`../docs/BUILD-PLAN.md`](../docs/BUILD-PLAN.md).
