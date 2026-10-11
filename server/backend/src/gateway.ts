/**
 * WebSocket gateway. Auth on connect (demo: shared code), seat acquisition,
 * heartbeats, and wiring frames to the session + orchestrator.
 *
 * Wire contract: @jojoai/shared/protocol.
 */
import { WebSocketServer, type WebSocket } from 'ws';
import type { Server } from 'node:http';
import { config } from './config.ts';
import { checkAuth } from './auth.ts';
import { SessionManager, type Session } from './session.ts';
import { runTurn, deliverToolResult, replayPending } from './orchestrator.ts';
import { CloseCode, PROTOCOL_VERSION, type ClientMsg, type ServerMsg } from '@jojoai/shared/protocol';

const send = (ws: WebSocket, m: ServerMsg): void => {
  if (ws.readyState === ws.OPEN) ws.send(JSON.stringify(m));
};

export function createGateway(server: Server): SessionManager {
  const mgr = new SessionManager((s) => {
    if (s.ws) send(s.ws, { t: 'closing', code: CloseCode.IDLE_CLOSED, reason: `idle > ${config.idleReleaseMs / 60000} min; seat released`, resumable: false });
    s.ws?.close(CloseCode.IDLE_CLOSED);
    mgr.end(s, 'idle_timeout');
  });

  const wss = new WebSocketServer({ server, path: '/agent' });

  wss.on('connection', (ws) => {
    let session: Session | null = null;

    ws.on('message', (raw) => {
      let msg: ClientMsg;
      try {
        msg = JSON.parse(raw.toString()) as ClientMsg;
      } catch {
        send(ws, { t: 'error', code: 'bad_message', message: 'not JSON' });
        return;
      }

      switch (msg.t) {
        case 'hello': {
          if (msg.protocol !== PROTOCOL_VERSION) {
            send(ws, { t: 'closing', code: CloseCode.PROTOCOL_UNSUPPORTED, reason: `server speaks protocol v${PROTOCOL_VERSION}`, resumable: false });
            return ws.close(CloseCode.PROTOCOL_UNSUPPORTED);
          }
          void (async () => {
          const auth = await checkAuth(msg.token ?? '');
          if (!auth.ok) {
            send(ws, { t: 'closing', code: CloseCode.UNAUTHENTICATED, reason: auth.reason ?? 'unauthenticated', resumable: false });
            return ws.close(CloseCode.UNAUTHENTICATED);
          }

          if (msg.resumeToken) {
            const existing = mgr.resume(msg.resumeToken);
            if (!existing) {
              send(ws, { t: 'closing', code: CloseCode.RESUME_EXPIRED, reason: 'resume window elapsed; start a fresh session', resumable: false });
              return ws.close(CloseCode.RESUME_EXPIRED);
            }
            session = existing;
            mgr.attach(session, ws);
            send(ws, { t: 'session_ready', protocol: PROTOCOL_VERSION, sessionId: session.id, seatNo: session.seatNo, resumeToken: session.resumeToken, resumed: true, turnActive: session.turnActive, model: config.model });
            replayPending(session, (m) => send(ws, m));
            return;
          }

          const created = mgr.create(msg.projectLabel ?? 'guest');
          if (!created) {
            send(ws, { t: 'closing', code: CloseCode.AT_CAPACITY, reason: `all ${config.maxSeats} seats in use — try again shortly`, resumable: false });
            return ws.close(CloseCode.AT_CAPACITY);
          }
          session = created;
          mgr.attach(session, ws);
          send(ws, { t: 'session_ready', protocol: PROTOCOL_VERSION, sessionId: session.id, seatNo: session.seatNo, resumeToken: session.resumeToken, resumed: false, turnActive: false, model: config.model });
          })().catch((e: unknown) => {
            send(ws, { t: 'closing', code: CloseCode.UNAUTHENTICATED, reason: `auth error: ${(e as Error).message}`, resumable: false });
            ws.close(CloseCode.UNAUTHENTICATED);
          });
          return;
        }

        case 'user_turn': {
          if (!session) return;
          mgr.touch(session);
          if (session.turnActive) {
            send(ws, { t: 'error', code: 'bad_message', message: 'a turn is already in progress' });
            return;
          }
          void runTurn(session, msg.text, (m) => (session?.ws ? send(session.ws, m) : undefined));
          return;
        }

        case 'tool_result': {
          if (!session) return;
          mgr.touch(session);
          deliverToolResult(session, msg.id, {
            ok: msg.ok,
            stdout: msg.stdout,
            stderr: msg.stderr,
            exitCode: msg.exitCode,
            content: msg.content,
            bytesWritten: msg.bytesWritten,
            refused: msg.refused,
            error: msg.error,
          });
          return;
        }

        case 'interrupt': {
          if (session?.abort) session.abort.abort();
          return;
        }

        case 'heartbeat': {
          if (session) mgr.touch(session);
          send(ws, { t: 'heartbeat_ack', serverTime: Date.now() });
          return;
        }

        case 'bye': {
          if (session) mgr.end(session, 'user_quit');
          ws.close(CloseCode.NORMAL);
          return;
        }

        default:
          send(ws, { t: 'error', code: 'bad_message', message: `unknown t` });
      }
    });

    // Only this socket's own session binding: an old socket whose close
    // arrives minutes late (half-open link, edge timeout) used to detach a
    // session that had already resumed on a new one, dropping its output.
    const drop = (): void => {
      if (session && session.ws === ws) mgr.detach(session);
    };
    ws.on('close', drop);
    ws.on('error', drop);
  });

  return mgr;
}
