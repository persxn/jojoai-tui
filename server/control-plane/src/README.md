# `server/control-plane/src/` — the control-plane service

Plain `node:http`, run directly with `--experimental-strip-types`. Every rule
lives in `index.ts` and in `authorize()` (`../../shared/src/authorize.ts`); the
two stores only move rows.

## Routes (`index.ts`)

| Route | Body | Answer |
|---|---|---|
| `GET /healthz` | — | `{ ok }` |
| `POST /api/auth/otp/request` | `{ email }` | always `{ ok: true }` (no account enumeration); mails a code when the account may sign in |
| `POST /api/auth/otp/verify` | `{ email, code }` | `{ token, expiresAt, role, accountId }` or `400` |
| `POST /api/auth/logout` | `{ token }` | `{ ok: true }` |
| `POST /api/authz/check` | `{ token }` + header `x-jojoai-service` | `{ allow, role?, reason?, accountId?, email? }`, for the backend only |

## Files

| File | What it does | Key exports / functions |
|---|---|---|
| `index.ts` | Entry point and routes. Builds the store, seeds the owner account, serves the API. | `server`, `store`; internal `readJson` (size-capped body), `snapshot` (account + grants + config → `AuthzInput`), `otpRequest`, `otpVerify`, `authzCheck`, `logout`. |
| `config.ts` | Settings from environment variables: port, store, OTP length/TTL/attempts/rate, session TTL, mail transport, owner email. No secret has a usable production default. | `config`, `isProd`, `assertConfig`, `smtpSettings`. |
| `otp.ts` | One-time codes: CSPRNG digits, only the SHA-256 stored, constant-time compare, the mail text, delivery (`console` writes to stderr and a dev outbox; `smtp` sends). | `generateCode`, `hashCode`, `codeMatches`, `codeMessage`, `deliver`. |
| `smtp.ts` | Minimal SMTP submission client (EHLO, STARTTLS, AUTH LOGIN, MAIL, RCPT, DATA): multiline replies, dot-stuffing, and the plaintext buffer dropped at the STARTTLS upgrade. One message per connection, no retries. | `sendMail`, `buildMessage`, `dotStuff`, `addressOf`, `SmtpConfig`, `SmtpMessage`. |
| `tokens.ts` | Opaque bearer tokens (32 random bytes, base64url); only the hash is stored. | `mintToken`, `hashToken`, `secretEquals` (constant time). |
| `store.ts` | The storage interface the HTTP layer uses, row types, and the in-memory implementation. | `Store`, `MemoryStore`, `seedStore`, row types `AccountRow`, `GrantRow`, `OtpRow`, `AuthSessionRow`, `ServiceConfigRow`, `AuditRow`. |
| `prisma-store.ts` | `Store` over Postgres through the generated Prisma client. Lower-cases emails; represents a manually-paid grant as a zero-amount `manual:<grantId>` payment. | `PrismaStore`, `loadPrismaClient`. |
| `index.test.ts` | Integration test: real HTTP against the memory store (OTP request → verify → authz → logout). | — |
| `store.test.ts` | One contract run against `MemoryStore` always and `PrismaStore` when `JOJOAI_TEST_DATABASE_URL` is set, so the two cannot drift. | — |

## Rules worth keeping

- `otp/request` answers the same whether or not the address has an account.
- A token is never stored, only its SHA-256; revoking is one column write.
- Any new store method goes into `Store`, both implementations, and
  `store.test.ts`.
