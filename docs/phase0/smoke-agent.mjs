/**
 * Throwaway end-to-end smoke test for the demo backend.
 * Connects to the WS gateway, drives one coding task, executes the demo tools
 * FOR REAL in a temp project dir (auto-approving), prints the stream.
 *
 *   node docs/phase0/smoke-agent.mjs [--port 7420] [--code jojo-demo] [--task "..."]
 */
import WebSocket from 'ws';
import { mkdtempSync, readdirSync, readFileSync, writeFileSync } from 'node:fs';
import { execFile } from 'node:child_process';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

const arg = (n, d) => { const i = process.argv.indexOf(`--${n}`); return i === -1 ? d : process.argv[i + 1]; };
const PORT = Number(arg('port', 7420));
const CODE = arg('code', 'jojo-demo');
const TASK = arg('task', "Create a file called greeting.txt whose contents are exactly 'hello from jojo ai'. Then run `cat greeting.txt` to verify it, and tell me what it printed.");

const ROOT = mkdtempSync(join(tmpdir(), 'jojo-smoke-'));
console.log('project root:', ROOT);

const jail = (p) => {
  const abs = resolve(ROOT, p ?? '.');
  if (abs !== ROOT && !abs.startsWith(ROOT + '/')) throw new Error(`path escapes project root: ${p}`);
  return abs;
};

async function runTool(name, args) {
  try {
    if (name === 'list_dir') {
      return { ok: true, stdout: readdirSync(jail(args.path ?? '.')).join('\n') || '(empty)' };
    }
    if (name === 'read_file') {
      return { ok: true, content: readFileSync(jail(args.path), 'utf8').slice(0, args.max_bytes ?? 100000) };
    }
    if (name === 'write_file') {
      const abs = jail(args.path);
      writeFileSync(abs, String(args.content ?? ''));
      console.log(`   [auto-approved write_file ${args.path}]`);
      return { ok: true, bytesWritten: Buffer.byteLength(String(args.content ?? '')) };
    }
    if (name === 'run_command') {
      console.log(`   [auto-approved run_command: ${args.cmd}]`);
      return await new Promise((res) => {
        execFile('bash', ['-lc', args.cmd], { cwd: ROOT, timeout: args.timeout_ms ?? 120000 }, (err, stdout, stderr) => {
          res({ ok: !err, exitCode: err?.code ?? 0, stdout: String(stdout).slice(0, 20000), stderr: String(stderr).slice(0, 20000) });
        });
      });
    }
    return { ok: false, refused: 'policy', error: `unknown tool ${name}` };
  } catch (e) {
    return { ok: false, error: e.message };
  }
}

const ws = new WebSocket(`ws://127.0.0.1:${PORT}/agent`);
let thinking = false;

ws.on('open', () => {
  ws.send(JSON.stringify({
    t: 'hello', protocol: 1, token: CODE,
    client: { version: '0.0.0-smoke', os: 'linux', arch: 'arm64' },
    projectLabel: 'smoke',
  }));
});

ws.on('message', async (raw) => {
  const m = JSON.parse(raw.toString());
  switch (m.t) {
    case 'session_ready':
      console.log(`\n[session ${m.sessionId.slice(0, 8)} seat ${m.seatNo}]  task: ${TASK}\n`);
      ws.send(JSON.stringify({ t: 'user_turn', text: TASK }));
      break;
    case 'thinking_delta':
      if (!thinking) { process.stdout.write('\n\x1b[90m<thinking> '); thinking = true; }
      process.stdout.write('\x1b[90m' + m.text + '\x1b[0m');
      break;
    case 'assistant_delta':
      if (thinking) { process.stdout.write('\x1b[90m </thinking>\x1b[0m\n\n'); thinking = false; }
      process.stdout.write(m.text);
      break;
    case 'tool_request': {
      if (thinking) { process.stdout.write('\x1b[90m </thinking>\x1b[0m\n'); thinking = false; }
      console.log(`\n\x1b[36m→ tool_request ${m.id.slice(-6)} ${m.name}(${JSON.stringify(m.args).slice(0, 120)}) destructive=${m.destructive}\x1b[0m`);
      const result = await runTool(m.name, m.args);
      console.log(`\x1b[36m← tool_result ${m.id.slice(-6)} ok=${result.ok}\x1b[0m`);
      ws.send(JSON.stringify({ t: 'tool_result', id: m.id, ...result }));
      break;
    }
    case 'replay_begin': console.log(`\x1b[33m[replay ${m.count}]\x1b[0m`); break;
    case 'turn_done':
      console.log(`\n\n\x1b[32m[turn_done] stop=${m.stopReason} toolCalls=${m.usage.toolCalls} tokIn=${m.usage.tokensIn} tokOut=${m.usage.tokensOut} modelMs=${m.usage.modelMs}\x1b[0m`);
      ws.send(JSON.stringify({ t: 'bye', reason: 'user_quit' }));
      setTimeout(() => process.exit(0), 100);
      break;
    case 'closing':
      console.log(`\x1b[31m[closing ${m.code}] ${m.reason}\x1b[0m`);
      process.exit(1);
    case 'error':
      console.log(`\x1b[31m[error ${m.code}] ${m.message}\x1b[0m`);
      break;
  }
});

ws.on('error', (e) => { console.error('ws error:', e.message); process.exit(1); });
