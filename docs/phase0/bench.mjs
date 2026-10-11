// Phase 0 — Ollama concurrency benchmark for gpt-oss:120b on a unified-memory host.
// Fires N concurrent streaming /api/chat requests, measures per-stream
// time-to-first-token and eval tok/s, and aggregate throughput.
//
//   node docs/phase0/bench.mjs [--host 127.0.0.1:11434] [--model gpt-oss:120b]
//        [--levels 1,2,4,8] [--predict 300] [--prompt "..."]

const args = Object.fromEntries(
  process.argv.slice(2).join(' ').split(/\s+--/).filter(Boolean).map((s) => {
    const i = s.indexOf(' ');
    return i === -1 ? [s.replace(/^--/, ''), true] : [s.replace(/^--/, '').slice(0, i - 0).split(' ')[0], s.slice(i + 1)];
  }),
);
const HOST = (args.host || '127.0.0.1:11434').toString();
const MODEL = (args.model || 'gpt-oss:120b').toString();
const LEVELS = (args.levels || '1,2,4,8').toString().split(',').map(Number);
const PREDICT = Number(args.predict || 300);
const PROMPT = (args.prompt ||
  'Write a Python function that returns the nth Fibonacci number iteratively, then explain its time complexity in two sentences.').toString();

const now = () => Number(process.hrtime.bigint()) / 1e6;

async function oneRequest(idx) {
  const started = now();
  let ttft = null;
  let evalCount = 0;
  let evalDurationNs = 0;
  let promptEvalCount = 0;
  let loadDurationNs = 0;
  let totalDurationNs = 0;

  const res = await fetch(`http://${HOST}/api/chat`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({
      model: MODEL,
      messages: [{ role: 'user', content: PROMPT }],
      stream: true,
      options: { temperature: 0, num_predict: PREDICT },
    }),
  });
  if (!res.ok) throw new Error(`req ${idx}: HTTP ${res.status} ${await res.text()}`);

  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = '';
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let nl;
    while ((nl = buf.indexOf('\n')) !== -1) {
      const line = buf.slice(0, nl).trim();
      buf = buf.slice(nl + 1);
      if (!line) continue;
      const msg = JSON.parse(line);
      const piece = msg.message?.content || msg.message?.thinking || '';
      if (ttft === null && piece) ttft = now() - started;
      if (msg.done) {
        evalCount = msg.eval_count || 0;
        evalDurationNs = msg.eval_duration || 0;
        promptEvalCount = msg.prompt_eval_count || 0;
        loadDurationNs = msg.load_duration || 0;
        totalDurationNs = msg.total_duration || 0;
      }
    }
  }
  const wall = now() - started;
  return {
    idx,
    ttftMs: ttft,
    wallMs: wall,
    evalCount,
    promptEvalCount,
    tokPerSec: evalDurationNs ? (evalCount / (evalDurationNs / 1e9)) : null,
    loadMs: loadDurationNs / 1e6,
    serverTotalMs: totalDurationNs / 1e6,
  };
}

function stats(xs) {
  const a = xs.filter((x) => x != null).sort((p, q) => p - q);
  if (!a.length) return null;
  const q = (p) => a[Math.min(a.length - 1, Math.floor(p * a.length))];
  return { min: a[0], p50: q(0.5), p95: q(0.95), max: a[a.length - 1], mean: a.reduce((s, v) => s + v, 0) / a.length };
}
const f = (n) => (n == null ? '  n/a' : n.toFixed(1).padStart(7));

console.log(`host=${HOST}  model=${MODEL}  num_predict=${PREDICT}  levels=${LEVELS.join(',')}`);
console.log('warming up (1 request)…');
await oneRequest(0);

for (const N of LEVELS) {
  const t0 = now();
  const results = await Promise.all(Array.from({ length: N }, (_, i) => oneRequest(i)));
  const wallAll = now() - t0;
  const totalEvalTokens = results.reduce((s, r) => s + r.evalCount, 0);
  const aggTokPerSec = totalEvalTokens / (wallAll / 1000);
  const ttft = stats(results.map((r) => r.ttftMs));
  const perStream = stats(results.map((r) => r.tokPerSec));
  const wall = stats(results.map((r) => r.wallMs));

  console.log(`\n=== concurrency ${N} ===`);
  console.log(`  wall (all done)      ${f(wallAll)} ms`);
  console.log(`  TTFT      min/p50/p95/max   ${f(ttft?.min)} / ${f(ttft?.p50)} / ${f(ttft?.p95)} / ${f(ttft?.max)} ms`);
  console.log(`  per-stream tok/s   min/p50/max   ${f(perStream?.min)} / ${f(perStream?.p50)} / ${f(perStream?.max)}`);
  console.log(`  per-req wall  p50/p95/max   ${f(wall?.p50)} / ${f(wall?.p95)} / ${f(wall?.max)} ms`);
  console.log(`  AGGREGATE throughput ${f(aggTokPerSec)} tok/s   (${totalEvalTokens} eval tokens / ${(wallAll / 1000).toFixed(1)} s)`);
}
console.log('\ndone.');
