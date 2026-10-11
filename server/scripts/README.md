# `server/scripts/` — operational scripts

| File | What it does |
|---|---|
| `backup-db.sh` | Nightly backup of the control-plane database: `pg_dump --clean --if-exists jojoai` inside the `cjudge-postgres` container, gzipped to `<repo>/backups/jojoai-<date>.sql.gz` (directory 700, file 600; written as `.partial` and renamed so a failed dump never looks complete). Deletes dumps older than 30 days. Run by `deploy/jojoai-backup.timer` at 02:40. |

Restore:

```sh
zcat backups/jojoai-<stamp>.sql.gz | docker exec -i cjudge-postgres psql -U cjudge -d jojoai
```

JojoCode's own backup does not cover this database; it is a separate database
in the same Postgres container (see `../README.md`).
