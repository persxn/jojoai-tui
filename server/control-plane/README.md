# `server/control-plane/` — `@jojoai/control-plane`

The always-on half of the hosted service: email-OTP login, opaque bearer
tokens, accounts, access grants, service config and the audit trail. The
backend asks it `POST /api/authz/check` on every websocket connect and fails
closed if it cannot reach it. Deployed beside the backend as
`jojoai-control-plane.service` on `127.0.0.1:7460`.

It deliberately has **no runtime dependencies** beyond `@jojoai/shared` (SMTP
is hand-written in `src/smtp.ts`): it mints login tokens, so its supply chain
is kept small.

| Path | What |
|---|---|
| `package.json` | Workspace package. `start` / `dev` run `src/index.ts` with `--experimental-strip-types`; `test` runs `src/**/*.test.ts` with `node --test`; `typecheck` runs `tsc`. |
| `tsconfig.json` | Extends `../../tsconfig.base.json`; maps `@jojoai/shared` to its sources. |
| `src/` | The service and its tests. See [`src/README.md`](src/README.md). |

## Run and test

```sh
JOJOAI_SERVICE_TOKEN=dev JOJOAI_OWNER_EMAIL=you@example.com \
  node --experimental-strip-types src/index.ts                # :7460, memory store

npm test -w @jojoai/control-plane                             # memory store only
JOJOAI_TEST_DATABASE_URL=postgresql://…/jojoai_test \
  npm test -w @jojoai/control-plane                           # also against Postgres (WIPES that db)
```

Store selection: `JOJOAI_CP_STORE=memory` (default; forgets everything on
restart) or `prisma` with `JOJOAI_DATABASE_URL` (production). The Prisma
client is generated, not committed: `npm run -w @jojoai/shared prisma:generate`.

## Where to look

| Symptom | File |
|---|---|
| No code arrives by mail | `src/otp.ts` `deliver`, `src/smtp.ts` `sendMail`; `JOJOAI_MAIL` and the `JOJOAI_SMTP_*` variables in `src/config.ts` |
| "Code incorrect" / too many attempts | `src/index.ts` `otpVerify`, `src/otp.ts` `codeMatches` |
| Signed in but the websocket is refused | `src/index.ts` `authzCheck` → `snapshot` → `authorize()` in `../shared/src/authorize.ts` |
| Everyone signed out after a restart | `JOJOAI_CP_STORE` is `memory` |
| Refuses to start in production | `src/config.ts` `assertConfig` (a missing secret is a hard error) |
