/**
 * In-memory session manager (demo-grade). Seat accounting + the reconnect/
 * resume journal proven in phase0/spike-ws-resume (F2).
 *
 * Phase 1 replaces the Map with Postgres-backed AgentSession/AgentTurn so
 * resume also survives a backend restart (R8). Marked with TODO(persist).
 */
import { randomUUID } from 'node:crypto';
import type { WebSocket } from 'ws';
import type { OllamaMessage } from './ollama.ts';
import { config } from './config.ts';

export interface ToolWaiter {
  resolve: (r: ToolResultPayload) => void;
  timer: NodeJS.Timeout;
}

export interface ToolResultPayload {
  ok: boolean;
  stdout?: string | undefined;
  stderr?: string | undefined;
  exitCode?: number | undefined;
  content?: string | undefined;
  bytesWritten?: number | undefined;
  refused?: string | undefined;
  error?: string | undefined;
}

export interface Session {
  id: string;
  resumeToken: string;
  label: string; // demo: whatever the client sent as projectLabel / "guest"
  seatNo: number;
  ws: WebSocket | null;

  messages: OllamaMessage[]; // transcript fed to the model  TODO(persist)
  turnActive: boolean;
  abort: AbortController | null;

  pendingTools: Map<string, { name: string; args: Record<string, unknown>; dispatchedAt: number }>;
  toolResults: Map<string, ToolResultPayload>;
  toolWaiters: Map<string, ToolWaiter>;

  lastActivityAt: number;
  idleTimer: NodeJS.Timeout | null;
  disconnectTimer: NodeJS.Timeout | null;
}

export class SessionManager {
  private byId = new Map<string, Session>();
  private byResume = new Map<string, string>();
  private seats: (string | null)[]; // index = seatNo-1, value = sessionId

  private onIdleClose: (s: Session) => void;

  constructor(onIdleClose: (s: Session) => void) {
    this.onIdleClose = onIdleClose;
    this.seats = Array.from({ length: config.maxSeats }, () => null);
  }

  seatsUsed(): number {
    return this.seats.filter(Boolean).length;
  }
  seatsFree(): number {
    return config.maxSeats - this.seatsUsed();
  }
  list(): Session[] {
    return [...this.byId.values()];
  }

  private claimSeat(sessionId: string): number | null {
    const i = this.seats.findIndex((s) => s === null);
    if (i === -1) return null;
    this.seats[i] = sessionId;
    return i + 1;
  }
  private freeSeat(seatNo: number): void {
    if (seatNo >= 1 && seatNo <= this.seats.length) this.seats[seatNo - 1] = null;
  }

  /** Returns a new session, or null if no seat is free. */
  create(label: string): Session | null {
    const id = randomUUID();
    const seatNo = this.claimSeat(id);
    if (seatNo === null) return null;
    const s: Session = {
      id,
      resumeToken: randomUUID(),
      label,
      seatNo,
      ws: null,
      messages: [],
      turnActive: false,
      abort: null,
      pendingTools: new Map(),
      toolResults: new Map(),
      toolWaiters: new Map(),
      lastActivityAt: Date.now(),
      idleTimer: null,
      disconnectTimer: null,
    };
    this.byId.set(id, s);
    this.byResume.set(s.resumeToken, id);
    return s;
  }

  resume(resumeToken: string): Session | null {
    const id = this.byResume.get(resumeToken);
    return id ? (this.byId.get(id) ?? null) : null;
  }

  touch(s: Session): void {
    s.lastActivityAt = Date.now();
    if (s.idleTimer) clearTimeout(s.idleTimer);
    s.idleTimer = setTimeout(() => this.onIdleClose(s), config.idleReleaseMs);
  }

  attach(s: Session, ws: WebSocket): void {
    s.ws = ws;
    if (s.disconnectTimer) {
      clearTimeout(s.disconnectTimer);
      s.disconnectTimer = null;
    }
    this.touch(s);
  }

  detach(s: Session): void {
    s.ws = null;
    if (s.disconnectTimer) clearTimeout(s.disconnectTimer);
    s.disconnectTimer = setTimeout(() => {
      if (s.ws) return; // reconnected in time
      this.end(s, 'resume_window_elapsed');
    }, config.resumeWindowMs);
  }

  end(s: Session, reason: string): void {
    if (s.abort) s.abort.abort();
    if (s.idleTimer) clearTimeout(s.idleTimer);
    if (s.disconnectTimer) clearTimeout(s.disconnectTimer);
    for (const w of s.toolWaiters.values()) {
      clearTimeout(w.timer);
      w.resolve({ ok: false, refused: 'error', error: `session ended: ${reason}` });
    }
    s.toolWaiters.clear();
    this.freeSeat(s.seatNo);
    this.byId.delete(s.id);
    this.byResume.delete(s.resumeToken);
  }
}
