# `docs/phase0/` — measurements and spikes before the build

Throwaway-but-kept experiments that settled the risky decisions. Results are in
`FINDINGS.md`; the scripts are here so the numbers can be re-measured.

| Path | What |
|---|---|
| `FINDINGS.md` | F1: Ollama concurrency benchmark for gpt-oss:120b (why `OLLAMA_NUM_PARALLEL` and the seat count are what they are). F2: websocket reconnect/resume with exactly-once tools — PASS. F3: Cloudflare tunnel latency spike — blocked. |
| `bench.mjs` | F1's benchmark: fires N concurrent streaming `/api/chat` requests and reports time-to-first-token, per-stream and aggregate tokens/s. `npm run bench:ollama -- --levels 1,2,4,8` (or `node docs/phase0/bench.mjs`). |
| `smoke-agent.mjs` | End-to-end smoke test of a running backend: connects to `/agent`, drives one task, executes the tools for real in a temporary directory (auto-approving), prints the stream. `node docs/phase0/smoke-agent.mjs --port 7420 --code jojo-demo`. |
| `spike-ws-resume/` | F2's spike: a minimal server-side loop plus a client that crashes mid-tool-call, proving a tool runs exactly once across reconnect. See [`spike-ws-resume/README.md`](spike-ws-resume/README.md). Part of `npm test`. |
