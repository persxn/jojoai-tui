# `deploy/` — systemd units for the host

User-level (`systemctl --user`) units installed on the `ai.jojocode.in` host.
The backend and control-plane units themselves are not in the repository —
their shape is in `server/README.md`, and both read the mode-600 `server/.env`.

| File | What it does |
|---|---|
| `jojoai-backup.service` | One-shot unit that runs `server/scripts/backup-db.sh` (expects the checkout at `~/src/jojo-ai`). |
| `jojoai-backup.timer` | Starts that service daily at 02:40; `Persistent=true` runs a missed backup at the next boot. |

Install:

```sh
cp deploy/jojoai-backup.* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now jojoai-backup.timer
systemctl --user list-timers jojoai-backup.timer      # next run
journalctl --user -u jojoai-backup.service            # last result
```

A user timer runs only while the user has a session unless lingering is on
(`loginctl enable-linger <user>`).
