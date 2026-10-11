/**
 * Jojo AI — CLI ↔ backend wire protocol (WebSocket, JSON frames).
 *
 * Decision D2: the agent loop runs on the backend host; the CLI is a thin client that
 * executes streamed tool requests locally (D1) and returns results. This file
 * is the contract both sides compile against.
 *
 * Framing: one JSON object per WS message, discriminated by `t`.
 * Versioning: `PROTOCOL_VERSION` sent in `hello` / `session_ready`; a mismatch
 * the server can't support closes with `close: 4400`.
 *
 * Exactly-once tools + resume (validated by phase0/spike-ws-resume):
 *   - every `tool_request` carries a stable `id`
 *   - the client keeps a local journal: id → recorded result
 *   - on reconnect the server re-sends any `tool_request` it has no result for,
 *     wrapped in `replay_begin` / `replay_end`
 *   - the client, seeing an id already in its journal, returns the stored
 *     result WITHOUT re-executing the side effect
 */

export const PROTOCOL_VERSION = 1;

// ── Close codes (WebSocket) ────────────────────────────────────────────────
export const CloseCode = {
  NORMAL: 1000,
  PROTOCOL_UNSUPPORTED: 4400,
  UNAUTHENTICATED: 4401, // bad/absent token
  ACCESS_REVOKED: 4403, // authorize() went DENY mid-session (revoke/expire/suspend/out-of-hours)
  AT_CAPACITY: 4290, // seats full, whenFull=REJECT
  QUEUE_TIMEOUT: 4291, // waited past maxQueueWaitMinutes
  IDLE_CLOSED: 4408, // seat auto-released after idleReleaseMinutes
  SERVER_RESTART: 4500, // backend restarting; reconnect with resumeToken
  RESUME_EXPIRED: 4410, // resume window elapsed; start a fresh session
} as const;
export type CloseCodeName = keyof typeof CloseCode;

// ── Client → Server ───────────────────────────────────────────────────────
export interface HelloMsg {
  t: 'hello';
  protocol: number; // PROTOCOL_VERSION
  token: string; // opaque bearer token from OTP verify (D14)
  resumeToken?: string; // present ⇒ attempt to rebind an existing agent session
  client: { version: string; os: string; arch: string };
  projectLabel?: string; // human label only, never a path
  clientRootHash?: string; // hash of the project root dir, for continuity checks
}

export interface UserTurnMsg {
  t: 'user_turn';
  text: string;
}

export interface ToolResultMsg {
  t: 'tool_result';
  id: string; // echoes ToolRequestMsg.id
  ok: boolean;
  // shape depends on the tool; for run_command:
  stdout?: string;
  stderr?: string;
  exitCode?: number;
  // for file tools:
  content?: string;
  bytesWritten?: number;
  // populated when the client refused (policy) or the user rejected the approval
  refused?: 'policy' | 'user_rejected' | 'timeout' | 'error';
  error?: string;
  durationMs?: number;
}

export interface InterruptMsg {
  t: 'interrupt';
} // cancel the in-flight model stream + pending tool requests, return to idle

export interface HeartbeatMsg {
  t: 'heartbeat';
}

export interface ByeMsg {
  t: 'bye';
  reason?: 'user_quit';
}

export type ClientMsg =
  | HelloMsg
  | UserTurnMsg
  | ToolResultMsg
  | InterruptMsg
  | HeartbeatMsg
  | ByeMsg;

// ── Server → Client ───────────────────────────────────────────────────────
export interface SessionReadyMsg {
  t: 'session_ready';
  protocol: number;
  sessionId: string;
  seatNo: number;
  resumeToken: string; // store this; needed to rebind after a drop
  resumed: boolean; // true ⇒ this was a reconnect, replay may follow
}

export interface QueuedMsg {
  t: 'queued';
  position: number;
  aheadOf: number;
  etaSeconds?: number;
}

export interface AssistantDeltaMsg {
  t: 'assistant_delta';
  text: string;
}

export interface ThinkingDeltaMsg {
  t: 'thinking_delta';
  text: string;
} // gpt-oss reasoning stream; render dimmed / collapsible

export interface ToolRequestMsg {
  t: 'tool_request';
  id: string; // stable; the exactly-once key
  name: 'read_file' | 'list_dir' | 'write_file' | 'apply_patch' | 'run_command' | string;
  args: Record<string, unknown>;
  timeoutMs: number;
  destructive: boolean; // client MUST get explicit user approval when true (regardless of allowlist)
}

export interface ReplayBeginMsg {
  t: 'replay_begin';
  count: number;
} // N tool_requests without a recorded result follow; client answers from its journal
export interface ReplayEndMsg {
  t: 'replay_end';
}

export interface TurnDoneMsg {
  t: 'turn_done';
  usage: { tokensIn: number; tokensOut: number; modelMs: number; toolCalls: number };
  /** `max_tokens`: the model's reply hit its length limit (Ollama `done_reason: length`). */
  stopReason: 'model_done' | 'max_tokens' | 'max_turns' | 'max_wallclock' | 'interrupted' | 'error';
}

export interface ErrorMsg {
  t: 'error';
  code:
    | 'inference_unavailable'
    | 'rate_limited'
    | 'bad_message'
    | 'tool_timeout'
    | 'internal';
  message: string;
  retryAfterSeconds?: number;
}

export interface ClosingMsg {
  t: 'closing';
  code: number; // one of CloseCode
  reason: string; // human-readable; CLI shows this verbatim
  resumable: boolean;
}

export interface HeartbeatAckMsg {
  t: 'heartbeat_ack';
  serverTime: number;
}

export type ServerMsg =
  | SessionReadyMsg
  | QueuedMsg
  | AssistantDeltaMsg
  | ThinkingDeltaMsg
  | ToolRequestMsg
  | ReplayBeginMsg
  | ReplayEndMsg
  | TurnDoneMsg
  | ErrorMsg
  | ClosingMsg
  | HeartbeatAckMsg;
