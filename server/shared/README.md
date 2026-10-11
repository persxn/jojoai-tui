# `server/shared/` — `@jojoai/shared`

Code and schema that more than one server package depends on, so the
contracts are written once:

- the **wire protocol** between the TUI's remote mode and the backend;
- **`authorize()`**, the one access decision (account → grant → hours →
  maintenance → payment), used by the control plane;
- **RBAC** roles and capabilities;
- the **Prisma schema** and migrations for the control-plane database.

| Path | What |
|---|---|
| `package.json` | Exports `.` (everything), `./protocol`, `./authorize`, `./rbac` straight from TypeScript sources. Scripts: `test`, `typecheck`, `prisma:generate`, `prisma:validate`. |
| `tsconfig.json` | Extends `../../tsconfig.base.json`; type-check only. |
| `src/` | Protocol, `authorize()`, RBAC and their tests. See [`src/README.md`](src/README.md). |
| `prisma/` | Database schema and migrations. See [`prisma/README.md`](prisma/README.md). |

The Prisma client is generated into `src/generated/` (gitignored). After a
fresh checkout, before the control plane can run with `JOJOAI_CP_STORE=prisma`:

```sh
npm run -w @jojoai/shared prisma:generate
```

The TUI does not import this package (it is Python); `tui/jojocode_ai/remote.py`
mirrors the protocol by hand. A change to `src/protocol.ts` needs the matching
change there.
