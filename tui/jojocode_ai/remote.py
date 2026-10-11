"""RemoteAgent — the same event stream as `agent.Agent`, but the loop runs on a
server.

`agent.Agent.run()` yields ("user"|"thinking"|"assistant"|"tool_start"|
"tool_end"|"tool_denied"|"final"|"error"|"stats"|"info"|"turn_end", …). The TUI
consumes exactly that, from a worker thread, and doesn't care where the tokens
come from. RemoteAgent speaks the WebSocket wire protocol
(`server/shared/src/protocol.ts`) instead of calling Ollama:

  hello ─▶                       ◀─ session_ready {sessionId, seatNo, resumeToken}
  user_turn ─▶                   ◀─ thinking_delta / assistant_delta
                                 ◀─ tool_request {id, name, args, destructive, timeoutMs}
  tool_result {id, …} ─▶         ◀─ turn_done {usage, stopReason}

Tools execute locally (`remote_exec`), path-jailed to the project root. A
per-turn journal makes tool execution exactly-once across a reconnect: on
resume the server replays unanswered requests and we reply from the journal
without re-running the side effect.

── What this client now decides for itself ─────────────────────────────────

- **Which calls need the user.** Only tools in policy.READ_ONLY run unasked.
  The server's `destructive` flag can add a prompt, never remove one — a
  server regression used to mean silent command execution on every client.
  Within that, the user's own choices apply as in local mode: [a]lways
  (which used to be ignored here), --trust for commands, auto-edit for writes
  inside the project.
- **When a turn is over.** Ctrl-C is sent the moment it is pressed (it used to
  wait for a pause in the stream) and, if the server does not confirm within a
  few seconds, the turn ends here anyway. A socket that has said nothing for
  LIVENESS seconds is treated as dead and resumed.
- **When a session is over.** A refused resume (the 10-minute window passed)
  starts a fresh session instead of failing every later turn until /reset.
  /reset and quitting say `bye`, so the seat is free at once rather than held
  for the resume window; and an idle TUI gives its seat back after
  IDLE_RELEASE instead of heartbeating it away from everyone else forever.
- **Whether an approval still counts.** The server stops waiting for a tool
  after `timeoutMs`; an approval given after that is not executed — the model
  has already been told the tool did not run.
"""

from __future__ import annotations

import platform
import threading
import time
from types import SimpleNamespace

from . import __version__, policy, remote_exec
from .config import Config
from .wsjson import WSClient, WSClosed, WSError

_RESULT_KEYS = ("ok", "stdout", "stderr", "exitCode", "content",
                "bytesWritten", "refused", "error", "durationMs")
RESUME_EXPIRED = 4410
IDLE_CLOSED = 4408
LIVENESS = 75.0            # seconds of silence mid-turn before the socket counts as dead
INTERRUPT_GRACE = 6.0      # seconds the server gets to confirm an interrupt
IDLE_RELEASE = 600.0       # an idle client gives its seat back after this long
HEARTBEAT = 25.0
_STOP_NOTES = {
    "max_turns": "(stopped: the server's step limit for one turn — say 'continue' to go on)",
    "max_wallclock": "(stopped: the server's time limit for one turn — say 'continue' to go on)",
    "max_tokens": "(stopped: the reply reached the model's length limit — say 'continue' to go on)",
}


def ws_url(endpoint: str) -> str:
    e = endpoint.rstrip("/")
    if e.startswith("https://"):
        e = "wss://" + e[len("https://"):]
    elif e.startswith("http://"):
        e = "ws://" + e[len("http://"):]
    return e + "/agent"


class _Fresh(Exception):
    """The old session is gone; a new one was opened."""


class RemoteAgent:
    def __init__(self, cfg: Config, token: str, approver=None):
        self.cfg = cfg
        self.endpoint = (cfg.remote_endpoint or "").rstrip("/")
        self.token = token
        self.approver = approver or (lambda n, a: True)
        self.project_label = cfg.project_label

        # attributes the TUI pokes at — kept compatible with agent.Agent
        self.store = None
        self.messages: list[dict] = []
        self.ctx = SimpleNamespace(cwd=cfg.cwd)
        self.client = SimpleNamespace(
            model=cfg.model,
            api="remote",
            version=lambda: f"remote {self.endpoint}",
            has_model=lambda _m: True,
            list_models=lambda: [],
            capabilities=lambda: {},
        )

        self._ws: WSClient | None = None
        self._interrupt = False
        self._stop = False
        self._hb: threading.Thread | None = None
        self._turn_active = False
        self._last_turn = time.monotonic()
        self.session_id = ""
        self.resume_token: str | None = None
        self.seat = 0
        self.journal: dict[str, dict] = {}

    # -- TUI-facing control ------------------------------------------- #
    def interrupt(self) -> None:
        self._interrupt = True

    def reset(self) -> None:
        self._bye()
        self._hard_close()
        self.resume_token = None
        self.journal.clear()
        self.messages.clear()

    def set_cwd(self, path: str) -> None:
        """/cwd: the project root the tools are jailed to."""
        self.cfg.cwd = self.ctx.cwd = path

    def close(self) -> None:
        self._stop = True
        self._interrupt = True
        self._bye()
        self._hard_close()

    # -- connection ------------------------------------------------- #
    def _bye(self) -> None:
        ws = self._ws
        if ws is not None:
            try:
                ws.send_json({"t": "bye", "reason": "user_quit"})
            except (WSError, OSError):
                pass

    def _hard_close(self) -> None:
        w, self._ws = self._ws, None
        if w:
            try:
                w.close()
            except OSError:
                pass

    def _connect(self, *, resume: bool) -> None:
        ws = WSClient(ws_url(self.endpoint))
        ws.connect()
        hello = {
            "t": "hello",
            "protocol": 1,
            "token": self.token,
            "client": {"version": __version__, "os": platform.system(),
                       "arch": platform.machine()},
            "projectLabel": self.project_label,
        }
        if resume and self.resume_token:
            hello["resumeToken"] = self.resume_token
        ws.send_json(hello)
        msg = ws.recv_json(timeout=25)
        if not isinstance(msg, dict) or not msg:
            ws.close()
            raise WSError("no session_ready from server")
        if msg.get("t") == "closing":
            ws.close()
            raise WSClosed(msg.get("code"), msg.get("reason", "server refused the connection"))
        if msg.get("t") != "session_ready":
            ws.close()
            raise WSError(f"unexpected first frame: {msg.get('t')}")
        self._ws = ws
        self.session_id = msg.get("sessionId", "")
        self.resume_token = msg.get("resumeToken")
        self.seat = msg.get("seatNo", 0)
        self._start_heartbeat()

    def _ensure(self) -> None:
        if self._ws is None:
            self._connect(resume=bool(self.resume_token))

    def _reconnect(self) -> bool:
        """Resume the session; if the server has ended it, open a fresh one
        and raise _Fresh (the turn in flight is lost). False if neither works."""
        self._hard_close()
        for attempt in range(3):
            if self._interrupt:
                return False
            try:
                self._connect(resume=bool(self.resume_token))
                return True
            except WSClosed as e:
                if e.code in (RESUME_EXPIRED, IDLE_CLOSED) and self.resume_token:
                    self.resume_token = None
                    self.journal.clear()
                    try:
                        self._connect(resume=False)
                    except (WSError, OSError):
                        return False
                    raise _Fresh() from e
            except (WSError, OSError):
                pass
            time.sleep(1.5 * (attempt + 1))
        return False

    def _start_heartbeat(self) -> None:
        if self._hb and self._hb.is_alive():
            return

        def beat() -> None:
            while not self._stop:
                time.sleep(HEARTBEAT)
                ws = self._ws
                if ws is None:
                    return
                if not self._turn_active and time.monotonic() - self._last_turn > IDLE_RELEASE:
                    # Give the seat back; the next turn opens a new session.
                    self._bye()
                    self._hard_close()
                    self.resume_token = None
                    return
                try:
                    ws.send_json({"t": "heartbeat"})
                except Exception:  # noqa: BLE001 — a dying socket is the reader's problem
                    return

        self._hb = threading.Thread(target=beat, daemon=True)
        self._hb.start()

    # -- the turn ------------------------------------------------- #
    def run(self, text: str):
        self._interrupt = False
        self._turn_active = True
        self.journal.clear()               # replays only ever concern the current turn
        yield ("user", text)
        self.messages.append({"role": "user", "content": text})
        try:
            yield from self._turn(text)
        finally:
            self._turn_active = False
            self._last_turn = time.monotonic()
        yield ("turn_end",)

    def _send(self, frame: dict) -> bool:
        ws = self._ws
        if ws is None:
            return False
        try:
            ws.send_json(frame)
            return True
        except (WSError, WSClosed, OSError):
            return False

    def _turn(self, text: str):
        try:
            self._ensure()
        except (WSError, WSClosed, OSError) as e:
            if isinstance(e, WSClosed) and e.code in (RESUME_EXPIRED, IDLE_CLOSED):
                self.resume_token = None
                try:
                    self._connect(resume=False)
                except (WSError, OSError) as e2:
                    yield ("error", f"cannot connect to {self.endpoint}: {e2}")
                    return
            else:
                yield ("error", f"cannot connect to {self.endpoint}: {e}")
                return

        if not self._send({"t": "user_turn", "text": text}):
            try:
                ok = self._reconnect()
            except _Fresh:
                ok = True
            if not ok or not self._send({"t": "user_turn", "text": text}):
                yield ("error", "connection lost and could not resume")
                return

        streamed = False
        last_rx = time.monotonic()
        interrupted_at = None
        last_sent = 0.0
        while True:
            now = time.monotonic()
            if self._interrupt:
                if interrupted_at is None:
                    interrupted_at = now
                if now - last_sent > 1.0:
                    self._send({"t": "interrupt"})     # at once, not after a pause
                    last_sent = now
                if now - interrupted_at > INTERRUPT_GRACE:
                    yield ("error", "interrupted (the server did not confirm in time; "
                                    "the next message starts on a fresh connection)")
                    self._hard_close()
                    return
            ws = self._ws
            if ws is None:
                yield ("error", "connection closed")
                return
            try:
                msg = ws.recv_json(timeout=0.5)
                if msg is None and time.monotonic() - last_rx > LIVENESS:
                    raise WSClosed(reason="no word from the server")
            except WSClosed as e:
                if self._interrupt:
                    yield ("error", "interrupted")
                    self._hard_close()
                    return
                try:
                    if self._reconnect():
                        yield ("info", "connection dropped — resumed")
                        last_rx = time.monotonic()
                        continue
                except _Fresh:
                    yield ("error", "the server had ended the session, so this turn was "
                                    "lost — send your message again")
                    return
                yield ("error", f"disconnected: {e}")
                self._hard_close()
                return
            except WSError as e:
                yield ("error", str(e))
                self._hard_close()
                return

            if msg is None:  # idle tick
                continue
            last_rx = time.monotonic()
            if not isinstance(msg, dict):    # valid JSON, but not a protocol frame
                continue

            t = msg.get("t")
            if t == "thinking_delta":
                yield ("thinking", msg.get("text", ""))
            elif t == "assistant_delta":
                streamed = True
                yield ("assistant", msg.get("text", ""))
            elif t == "tool_request":
                yield from self._tool(msg)
            elif t == "replay_begin":
                yield ("info", f"reconnected — replaying {msg.get('count', 0)} tool call(s)")
            elif t == "queued":
                yield ("info", f"queued — position {msg.get('position')} "
                               f"({msg.get('aheadOf')} ahead)")
            elif t == "session_ready":  # a mid-turn resume landed
                self.session_id = msg.get("sessionId", self.session_id)
                self.resume_token = msg.get("resumeToken", self.resume_token)
            elif t == "turn_done":
                u = msg.get("usage", {}) or {}
                yield ("stats", {
                    "prompt_eval_count": u.get("tokensIn"),
                    "eval_count": u.get("tokensOut"),
                    "eval_duration": (u.get("modelMs") or 0) * 1_000_000 or None,
                })
                reason = msg.get("stopReason")
                if reason == "interrupted" or self._interrupt:
                    yield ("error", "interrupted")
                elif reason in _STOP_NOTES:
                    yield ("final", _STOP_NOTES[reason], False)
                elif reason == "error":
                    yield ("error", "the server stopped the turn with an error")
                elif not streamed:
                    yield ("final", "", True)
                self.messages.append({"role": "assistant", "content": ""})
                return
            elif t == "error":
                yield ("error", f"{msg.get('code', 'error')}: {msg.get('message', '')}")
            elif t == "closing":
                yield ("error", msg.get("reason", "server closing the session"))
                if not msg.get("resumable"):
                    self.resume_token = None
                self._hard_close()
                return
            # heartbeat_ack, replay_end: nothing to do

    # -- tools ---------------------------------------------------- #
    def _pre_approved(self, name: str, args: dict) -> bool:
        if self.cfg.approves(name):
            return True
        if name == "run_command":
            return self.cfg.auto_bash
        if name == "write_file" and self.cfg.auto_edit:
            full = remote_exec._jail(self.ctx.cwd, args.get("path"))
            return full is not None and policy.auto_write_ok(full, self.ctx.cwd)
        return False

    def _refuse(self, tid: str, name: str, why: str, refused: str = "user_rejected"):
        payload = {"ok": False, "refused": refused, "error": why}
        self.journal[tid] = payload
        self._send_result(tid, payload)

    def _tool(self, msg: dict):
        tid = str(msg.get("id", ""))
        name = str(msg.get("name", ""))
        args = msg.get("args")
        if not isinstance(args, dict):
            args = {}
        # parsed, not divided: a string or object here used to raise out of
        # run() and leave the server waiting for a result that never came
        budget = max(1.0, remote_exec._millis(msg.get("timeoutMs"), 120_000) / 1000)
        t0 = time.monotonic()

        if tid in self.journal:  # exactly-once on replay — no re-execution
            self._send_result(tid, self.journal[tid])
            return
        if self._interrupt:
            self._refuse(tid, name, "interrupted by the user")
            yield ("tool_denied", name)
            return

        needs_ok = bool(msg.get("destructive")) or name not in policy.READ_ONLY
        if needs_ok and not self._pre_approved(name, args):
            if not self.approver(name, args) or self._interrupt:
                self._refuse(tid, name, "the user declined")
                yield ("tool_denied", name)
                return
            if time.monotonic() - t0 > budget - 1:
                self._refuse(tid, name, "approved after the server stopped waiting; not run",
                             refused="timeout")
                yield ("info", f"{name} was approved too late — the server had already "
                               "moved on, so it was not run")
                return

        if name == "run_command":
            # finish, or be killed, inside the server's window
            left_ms = max(1000, int((budget - (time.monotonic() - t0) - 2) * 1000))
            args = dict(args, timeout_ms=min(int(remote_exec._millis(args.get("timeout_ms"), 120_000)),
                                             left_ms))
        yield ("tool_start", name, args)
        payload = remote_exec.execute(name, args, self.ctx.cwd, cancel=lambda: self._interrupt)
        self.journal[tid] = payload
        self._send_result(tid, payload)
        yield ("tool_end", name, remote_exec.summarize(name, payload))

    def _send_result(self, tid: str, payload: dict) -> None:
        frame = {"t": "tool_result", "id": tid}
        frame.update({k: payload[k] for k in _RESULT_KEYS if k in payload})
        self._send(frame)  # if it fails, the server replays this request on reconnect
