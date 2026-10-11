# `docs/` — design and history

Read these to understand *why* the system is shaped the way it is. The code's
own headers explain individual modules; these explain the whole.

| File | What it covers |
|---|---|
| `ARCHITECTURE.md` | The system design: remote-mode topology, the agent loop, exactly-once tools and resume, authentication and authorization, seats / queue / idle / reaper, the client's permission model as a security component, the data model, testing, and the numbered key decisions (D1, D2, …) that code comments refer to. |
| `BUILD-PLAN.md` | The build log: target layout, the decisions, every task (T1, T2, …) with what was verified, fixes on top, and what is still needed. |
| `WEB-SEARCH-PLAN.md` | Plan and record for the `web_search` / `fetch_url` tools: recon, design (provider, SSRF guard, token budget, prompt changes), implementation steps, and the test plan. |
| `phase0/` | Pre-build measurements and spikes that settled the risky decisions. See [`phase0/README.md`](phase0/README.md). |

For the TUI's terminal and platform behaviour, see `../tui/COMPATIBILITY.md`;
for what is in flight right now, `../current_tasks.md`.
