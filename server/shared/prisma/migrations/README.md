# `server/shared/prisma/migrations/` — applied database migrations

Prisma Migrate's history for the control-plane database. Each subdirectory is
one migration, applied in name (timestamp) order by `prisma migrate deploy`;
Prisma records each applied migration's checksum in the database's
`_prisma_migrations` table.

| Path | What |
|---|---|
| `migration_lock.toml` | Written by Prisma; pins the provider (`postgresql`). Do not edit. |
| `20260921090000_init/` | The first migration: creates every enum and table in `../schema.prisma`. See its README. |

**Never edit a migration that has been deployed.** Its checksum is stored in
production; a changed file makes `migrate deploy` refuse to run. Add a new
migration instead. (Prisma reads only `migration.sql` inside each directory and
only directories here, so these README files do not affect it.)
