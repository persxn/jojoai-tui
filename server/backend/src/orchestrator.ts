/**
 * The agent loop (D2). Runs on the backend. Emits tool REQUESTS to the CLI and
 * waits for results (executed locally, D1). Streams model output as it comes.
 *
 * Demo-grade: in-memory transcript, oldest-first trim. Phase 1 adds proper
 * context summarization and per-turn persistence.
 */
import { chat, type OllamaMessage } from './ollama.ts';
import { TOOLS, SYSTEM_PROMPT, isDestructive } from './tools.ts';
import { config } from './config.ts';
import type { Session, ToolResultPayload } from './session.ts';
import type { ServerMsg } from '@jojoai/shared/protocol';

type Send = (m: ServerMsg) => void;

function ensureSystem(s: Session): void {
  if (s.messages[0]?.role !== 'system') {
    s.messages.unshift({ role: 'system', content: SYSTEM_PROMPT });
  }
}

/** Oldest-first trim to a char budget, always keeping the system + last user turn. */
function trim(s: Session): void {
  const budget = config.maxTranscriptChars;
  const size = () => s.messages.reduce((n, m) => n + m.content.length + (m.thinking?.length ?? 0), 0);
  while (size() > budget && s.messages.length > 3) {
    // remove the oldest non-system message
    const idx = s.messages[0]?.role === 'system' ? 1 : 0;
    s.messages.splice(idx, 1);
  }
}

function toolResultToContent(name: string, r: ToolResultPayload): string {
  if (r.refused) return `[tool ${name} not run: ${r.refused}${r.error ? ` — ${r.error}` : ''}]`;
  if (r.error) return `[tool ${name} error: ${r.error}]`;
  if (name === 'read_file') return r.content ?? '';
  if (name === 'list_dir') return r.stdout ?? r.content ?? '';
  if (name === 'write_file') return `wrote ${r.bytesWritten ?? 0} bytes`;
  if (name === 'run_command') {
    return [
      `exit ${r.exitCode ?? 'null'}`,
      r.stdout ? `stdout:\n${r.stdout}` : '',
      r.stderr ? `stderr:\n${r.stderr}` : '',
    ]
      .filter(Boolean)
      .join('\n');
  }
  return JSON.stringify(r);
}

/** Dispatch one tool call to the CLI and await its result (or replay from journal). */
function dispatchTool(
  s: Session,
  tc: { id: string; name: string; args: Record<string, unknown> },
  send: Send,
): Promise<ToolResultPayload> {
  const cached = s.toolResults.get(tc.id);
  if (cached) return Promise.resolve(cached); // resume replay — already answered

  s.pendingTools.set(tc.id, { name: tc.name, args: tc.args });
  return new Promise<ToolResultPayload>((resolve) => {
    const timer = setTimeout(() => {
      s.toolWaiters.delete(tc.id);
      s.pendingTools.delete(tc.id);
      resolve({ ok: false, refused: 'timeout', error: `no result in ${config.toolTimeoutMs}ms` });
    }, config.toolTimeoutMs);
    s.toolWaiters.set(tc.id, { resolve, timer });
    send({
      t: 'tool_request',
      id: tc.id,
      name: tc.name,
      args: tc.args,
      timeoutMs: config.toolTimeoutMs,
      destructive: isDestructive(tc.name),
    });
  });
}

/** Called by the gateway when a tool_result frame arrives. */
export function deliverToolResult(s: Session, id: string, payload: ToolResultPayload): void {
  const w = s.toolWaiters.get(id);
  s.toolResults.set(id, payload);
  s.pendingTools.delete(id);
  if (!w) return; // duplicate / late — journal keeps it idempotent
  clearTimeout(w.timer);
  s.toolWaiters.delete(id);
  w.resolve(payload);
}

/** Re-send any tool_request we're still waiting on (reconnect/resume path). */
export function replayPending(s: Session, send: Send): void {
  const outstanding = [...s.pendingTools.entries()];
  if (!outstanding.length) return;
  send({ t: 'replay_begin', count: outstanding.length });
  for (const [id, { name, args }] of outstanding) {
    send({ t: 'tool_request', id, name, args, timeoutMs: config.toolTimeoutMs, destructive: isDestructive(name) });
  }
  send({ t: 'replay_end' });
}

export async function runTurn(s: Session, text: string, send: Send): Promise<void> {
  ensureSystem(s);
  s.messages.push({ role: 'user', content: text });
  s.turnActive = true;
  const deadline = Date.now() + config.turnWallclockMs;
  let totalIn = 0;
  let totalOut = 0;
  let totalModelMs = 0;
  let toolCallCount = 0;

  try {
    for (let iter = 0; iter < config.maxToolIterations; iter++) {
      trim(s);
      s.abort = new AbortController();
      const result = await chat(
        s.messages,
        TOOLS,
        (d) => {
          if (d.thinking) send({ t: 'thinking_delta', text: d.thinking });
          if (d.content) send({ t: 'assistant_delta', text: d.content });
        },
        s.abort.signal,
      );
      totalIn += result.usage.promptTokens;
      totalOut += result.usage.evalTokens;
      totalModelMs += result.usage.modelMs;
      s.messages.push(result.message);

      if (result.toolCalls.length === 0) {
        send({
          t: 'turn_done',
          usage: { tokensIn: totalIn, tokensOut: totalOut, modelMs: totalModelMs, toolCalls: toolCallCount },
          stopReason: result.stoppedBy === 'length' ? 'max_tokens' : 'model_done',
        });
        return;
      }

      toolCallCount += result.toolCalls.length;
      const results = await Promise.all(
        result.toolCalls.map(async (tc) => ({ tc, payload: await dispatchTool(s, tc, send) })),
      );
      for (const { tc, payload } of results) {
        s.messages.push({
          role: 'tool',
          tool_call_id: tc.id,
          content: toolResultToContent(tc.name, payload),
        });
      }

      if (Date.now() > deadline) {
        send({
          t: 'turn_done',
          usage: { tokensIn: totalIn, tokensOut: totalOut, modelMs: totalModelMs, toolCalls: toolCallCount },
          stopReason: 'max_wallclock',
        });
        return;
      }
    }
    send({
      t: 'turn_done',
      usage: { tokensIn: totalIn, tokensOut: totalOut, modelMs: totalModelMs, toolCalls: toolCallCount },
      stopReason: 'max_turns',
    });
  } catch (err) {
    if ((err as Error).name === 'AbortError') {
      send({
        t: 'turn_done',
        usage: { tokensIn: totalIn, tokensOut: totalOut, modelMs: totalModelMs, toolCalls: toolCallCount },
        stopReason: 'interrupted',
      });
    } else {
      send({ t: 'error', code: 'internal', message: `orchestrator: ${(err as Error).message}` });
      send({
        t: 'turn_done',
        usage: { tokensIn: totalIn, tokensOut: totalOut, modelMs: totalModelMs, toolCalls: toolCallCount },
        stopReason: 'error',
      });
    }
  } finally {
    s.turnActive = false;
    s.abort = null;
  }
}
