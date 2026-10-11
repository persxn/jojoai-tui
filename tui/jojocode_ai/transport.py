"""HTTP for the model clients: interruptible, proxy-aware, stdlib only.

── Why not urllib.request.urlopen ──────────────────────────────────────────

Two things it got wrong for talking to a model server:

- **Ctrl-C could not stop a turn that had not started streaming.** Ollama
  sends no response headers until the first token, i.e. after the model has
  loaded and the whole prompt has been evaluated — minutes, for a big model on
  a laptop. urlopen blocks for all of it, so an interrupt waited up to
  `request_timeout` (600 s). Here the request runs on a helper thread and the
  caller waits on a queue it can walk away from; walking away shuts the socket
  down, which is also how Ollama learns to stop generating.
- **A local Ollama was reached through the proxy.** urllib honours
  http_proxy (and the Windows system proxy) for 127.0.0.1 unless no_proxy
  lists it, so `jojo` failed against an Ollama that the `ollama` CLI — which,
  like every Go program, never proxies loopback — reached fine. Loopback is
  always direct now; every other host keeps the proxy rules it had.
"""

from __future__ import annotations

import base64
import http.client
import ipaddress
import queue
import socket
import ssl
import threading
import urllib.parse
import urllib.request

POLL = 0.1          # how often a waiting stream looks at its cancel flag


class TransportError(Exception):
    """Could not reach the server, or the connection broke."""


class HTTPStatusError(TransportError):
    def __init__(self, code: int, body: str):
        super().__init__(f"HTTP {code}")
        self.code = code
        self.body = body


class Interrupted(TransportError):
    """The caller's cancel flag went up while waiting."""


class ConnectError(TransportError):
    """The request never reached the server (refused, DNS, proxy, TLS).

    Separate from a break mid-reply because stream() is a generator: both
    surface only once the caller starts reading, and a refused connection
    reported as "connection lost mid-reply" hid which host was unreachable.
    """


def _is_loopback(host: str) -> bool:
    host = (host or "").strip("[]").lower()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _proxy_for(scheme: str, host: str) -> str | None:
    if _is_loopback(host):
        return None
    try:
        if urllib.request.proxy_bypass(host):
            return None
        return urllib.request.getproxies().get(scheme)
    except Exception:  # noqa: BLE001 — a broken proxy config must not stop a direct call
        return None


def _describe(e: BaseException) -> str:
    if isinstance(e, ConnectionRefusedError):
        return "connection refused (is the server running?)"
    if isinstance(e, socket.gaierror):
        return f"cannot resolve host ({e})"
    if isinstance(e, (socket.timeout, TimeoutError)):
        return "timed out"
    return str(e) or type(e).__name__


def _connect(url: str, timeout: float):
    """(connection, request target) for url, through a proxy when one applies."""
    u = urllib.parse.urlsplit(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise TransportError(f"not an http(s) URL: {url!r}")
    port = u.port or (443 if u.scheme == "https" else 80)
    target = (u.path or "/") + (f"?{u.query}" if u.query else "")
    proxy = _proxy_for(u.scheme, u.hostname)
    extra: dict[str, str] = {}
    if proxy:
        p = urllib.parse.urlsplit(proxy if "://" in proxy else "http://" + proxy)
        if p.username:
            cred = f"{urllib.parse.unquote(p.username)}:{urllib.parse.unquote(p.password or '')}"
            extra["Proxy-Authorization"] = "Basic " + base64.b64encode(cred.encode()).decode()
        phost, pport = p.hostname, p.port or (443 if p.scheme == "https" else 80)   # as urllib
        if u.scheme == "https":
            conn = http.client.HTTPSConnection(phost, pport, timeout=timeout,
                                               context=ssl.create_default_context())
            conn.set_tunnel(u.hostname, port, headers=extra)
            return conn, target, {}
        conn = http.client.HTTPConnection(phost, pport, timeout=timeout)
        return conn, url, extra                    # plain-HTTP proxies take the full URL
    if u.scheme == "https":
        conn = http.client.HTTPSConnection(u.hostname, port, timeout=timeout,
                                           context=ssl.create_default_context())
    else:
        conn = http.client.HTTPConnection(u.hostname, port, timeout=timeout)
    return conn, target, {}


def request(method: str, url: str, body: bytes | None = None,
            headers: dict | None = None, timeout: float = 10) -> bytes:
    """A small, blocking request. Returns the body; raises on HTTP >= 400."""
    conn = None
    try:
        conn, target, extra = _connect(url, timeout)
        conn.request(method, target, body=body, headers={**(headers or {}), **extra})
        resp = conn.getresponse()
        data = resp.read()
        if resp.status >= 400:
            raise HTTPStatusError(resp.status, data.decode("utf-8", errors="replace"))
        return data
    except TransportError:
        raise
    except (OSError, http.client.HTTPException, ssl.SSLError) as e:
        raise TransportError(_describe(e)) from e
    finally:
        if conn is not None:
            conn.close()


def stream(url: str, body: bytes, headers: dict | None = None, timeout: float = 600,
           cancel=None):
    """POST, then yield the response line by line (bytes).

    Raises HTTPStatusError, TransportError, or Interrupted when `cancel()`
    turns true — checked every POLL seconds even while the server is silent.
    Closing the generator early (or an exception) aborts the connection.
    """
    q: queue.Queue = queue.Queue()
    state: dict = {"conn": None, "abort": False}

    def work():
        conn = None
        try:
            try:
                conn, target, extra = _connect(url, timeout)
                state["conn"] = conn
                conn.request("POST", target, body=body, headers={**(headers or {}), **extra})
            except BaseException as e:  # noqa: BLE001
                if not state["abort"]:
                    q.put(("connect", e))
                return
            if state["abort"]:
                return
            resp = conn.getresponse()
            if resp.status >= 400:
                q.put(("status", resp.status, resp.read(65536)))
                return
            for line in resp:
                if state["abort"]:
                    return
                q.put(("line", line))
            q.put(("end",))
        except BaseException as e:  # noqa: BLE001 — handed to the caller's thread
            if not state["abort"]:
                q.put(("exc", e))
        finally:
            if conn is not None:
                conn.close()

    threading.Thread(target=work, daemon=True, name="jojo-http").start()
    try:
        while True:
            try:
                item = q.get(timeout=POLL)
            except queue.Empty:
                if cancel is not None and cancel():
                    raise Interrupted("interrupted")
                continue
            kind = item[0]
            if kind == "line":
                yield item[1]
                if cancel is not None and cancel():
                    raise Interrupted("interrupted")
            elif kind == "end":
                return
            elif kind == "status":
                raise HTTPStatusError(item[1], item[2].decode("utf-8", errors="replace"))
            elif kind == "connect":
                e = item[1]
                raise ConnectError(str(e) if isinstance(e, TransportError) else _describe(e)) from e
            else:
                e = item[1]
                if isinstance(e, TransportError):
                    raise e
                raise TransportError(_describe(e)) from e
    finally:
        _abort(state)


def _abort(state: dict) -> None:
    state["abort"] = True
    conn = state.get("conn")
    sock = getattr(conn, "sock", None)
    if sock is not None:
        try:
            # shutdown, not just close: close() does not wake a recv() blocked
            # in another thread on Linux; shutdown does, on every platform.
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    if conn is not None:
        try:
            conn.close()
        except OSError:
            pass
