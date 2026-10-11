"""A minimal, dependency-free WebSocket client (RFC 6455) for JSON messaging.

Just enough for one long-lived client connection: the opening handshake, masked
text frames out, unmasked frames in, automatic ping/pong, and a clean close.
stdlib only (`socket`, `ssl`) so the TUI keeps zero runtime dependencies.

Not a general-purpose library: no permessage-deflate, no huge (>2^32) frames,
one message per frame on send.

Behind a proxy (HTTPS_PROXY / the system proxy) the connection is tunnelled
with CONNECT, as the login request already was by urllib — remote mode used to
fail behind any proxy that login worked through. Once a frame has started
arriving the socket has a read timeout, so a connection that dies half-way
through a frame surfaces as WSClosed instead of hanging the turn for good.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import selectors
import socket
import ssl
import struct
import threading
import urllib.parse
import urllib.request
from urllib.parse import urlparse

from .net import headers as _headers

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
READ_TIMEOUT = 60.0        # once a frame is under way, how long a silent socket is given


def _mask(data: bytes, key: bytes) -> bytes:
    """XOR with the 4-byte key — as one big integer, not byte by byte in Python."""
    n = len(data)
    if not n:
        return b""
    k = (key * (n // 4 + 1))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(k, "big")).to_bytes(n, "big")


def _proxy(scheme: str, host: str) -> str | None:
    """The proxy this connection should go through, if any (same rules as urllib,
    except that loopback is always direct — see transport.py)."""
    from .transport import _is_loopback
    if _is_loopback(host):
        return None
    try:
        if urllib.request.proxy_bypass(host):
            return None
        proxies = urllib.request.getproxies()
    except Exception:  # noqa: BLE001
        return None
    return proxies.get("https" if scheme == "wss" else "http")

OP_CONT, OP_TEXT, OP_BIN, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


class WSError(RuntimeError):
    pass


class WSClosed(WSError):
    """The peer closed the connection (or it dropped)."""

    def __init__(self, code: int | None = None, reason: str = ""):
        super().__init__(f"websocket closed ({code}) {reason}".strip())
        self.code = code
        self.reason = reason


class WSClient:
    def __init__(self, url: str, headers: dict[str, str] | None = None,
                 connect_timeout: float = 15.0):
        self.url = url
        # The upgrade request is an ordinary HTTP request and is filtered like
        # one — an unnamed or default-urllib client is refused at the edge
        # before the server ever sees it. See net.py.
        self.headers = _headers(headers)
        self.connect_timeout = connect_timeout
        self._sock: socket.socket | None = None
        self._buf = b""
        self._send_lock = threading.Lock()
        self._closed = False

    # -- lifecycle ------------------------------------------------------- #
    def connect(self) -> None:
        u = urlparse(self.url)
        secure = u.scheme == "wss"
        if not u.hostname:
            raise WSError(f"no host in {self.url!r}")
        host = u.hostname
        port = u.port or (443 if secure else 80)
        path = u.path or "/"
        if u.query:
            path += "?" + u.query

        proxy = _proxy(u.scheme, host)
        if proxy:
            raw = self._tunnel(proxy, host, port)
        else:
            raw = socket.create_connection((host, port), timeout=self.connect_timeout)
        if secure:
            ctx = ssl.create_default_context()
            raw = ctx.wrap_socket(raw, server_hostname=host)
        self._sock = raw

        key = base64.b64encode(os.urandom(16)).decode()
        lines = [
            f"GET {path} HTTP/1.1",
            f"Host: {host}:{port}" if u.port else f"Host: {host}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
        ]
        for k, v in self.headers.items():
            lines.append(f"{k}: {v}")
        self._sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())

        # read headers up to the blank line
        self._sock.settimeout(self.connect_timeout)
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise WSError("connection closed during handshake")
            head += chunk
        header_blob, _, rest = head.partition(b"\r\n\r\n")
        status_line = header_blob.split(b"\r\n", 1)[0].decode("latin1")
        if "101" not in status_line:
            raise WSError(f"handshake failed: {status_line}")
        accept = base64.b64encode(hashlib.sha1((key + _GUID).encode()).digest()).decode()
        if accept.lower() not in header_blob.decode("latin1").lower():
            raise WSError("handshake failed: bad Sec-WebSocket-Accept")
        self._buf = rest
        self._sock.settimeout(READ_TIMEOUT)

    def _tunnel(self, proxy: str, host: str, port: int) -> socket.socket:
        p = urllib.parse.urlsplit(proxy if "://" in proxy else "http://" + proxy)
        sock = socket.create_connection((p.hostname, p.port or 8080), timeout=self.connect_timeout)
        lines = [f"CONNECT {host}:{port} HTTP/1.1", f"Host: {host}:{port}"]
        if p.username:
            cred = f"{urllib.parse.unquote(p.username)}:{urllib.parse.unquote(p.password or '')}"
            lines.append("Proxy-Authorization: Basic " + base64.b64encode(cred.encode()).decode())
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = sock.recv(4096)
            if not chunk:
                sock.close()
                raise WSError("the proxy closed the connection")
            head += chunk
        status = head.split(b"\r\n", 1)[0].decode("latin1")
        if " 200" not in status:
            sock.close()
            raise WSError(f"the proxy refused the tunnel: {status}")
        return sock

    def close(self, code: int = 1000) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._send_frame(OP_CLOSE, struct.pack(">H", code))
        except OSError:
            pass
        try:
            self._sock.close()  # type: ignore[union-attr]
        except OSError:
            pass

    # -- framing ------------------------------------------------------- #
    def _send_frame(self, opcode: int, payload: bytes) -> None:
        if self._sock is None:
            raise WSClosed()
        fin_op = 0x80 | opcode
        mask = os.urandom(4)
        n = len(payload)
        if n < 126:
            hdr = struct.pack(">BB", fin_op, 0x80 | n)
        elif n < (1 << 16):
            hdr = struct.pack(">BBH", fin_op, 0x80 | 126, n)
        else:
            hdr = struct.pack(">BBQ", fin_op, 0x80 | 127, n)
        masked = _mask(payload, mask)
        with self._send_lock:
            try:
                self._sock.sendall(hdr + mask + masked)
            except OSError as e:
                raise WSClosed(reason=str(e))

    def _recv_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            try:
                chunk = self._sock.recv(65536)  # type: ignore[union-attr]
            except (OSError, AttributeError) as e:
                raise WSClosed(reason=str(e))
            if not chunk:
                raise WSClosed(reason="eof")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _read_frame(self) -> tuple[int, bool, bytes]:
        b0, b1 = self._recv_exact(2)
        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        length = b1 & 0x7F
        if length == 126:
            (length,) = struct.unpack(">H", self._recv_exact(2))
        elif length == 127:
            (length,) = struct.unpack(">Q", self._recv_exact(8))
        # servers must not mask; tolerate it anyway — the key comes before the
        # payload, not inside it (reading it as payload desynced the stream)
        mk = self._recv_exact(4) if masked else None
        payload = self._recv_exact(length) if length else b""
        if mk:
            payload = _mask(payload, mk)
        return opcode, fin, payload

    # -- public messaging -------------------------------------------- #
    def send_json(self, obj: dict) -> None:
        self._send_frame(OP_TEXT, json.dumps(obj, separators=(",", ":")).encode())

    def recv_json(self, timeout: float | None = None) -> dict | None:
        """Next JSON message, or None if the socket has no data within `timeout`.

        Handles ping/pong and close control frames transparently. Reassembles
        fragmented text messages. Raises WSClosed when the peer goes away.
        """
        if self._sock is None:
            raise WSClosed()
        pending = getattr(self._sock, "pending", None)
        if timeout is not None and not self._buf and not (pending and pending()):
            # (TLS may already hold decrypted bytes the socket will never signal)
            sel = selectors.DefaultSelector()
            sel.register(self._sock, selectors.EVENT_READ)
            ready = sel.select(timeout)
            sel.close()
            if not ready:
                return None

        data = b""
        while True:
            opcode, fin, payload = self._read_frame()
            if opcode == OP_PING:
                self._send_frame(OP_PONG, payload)
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                code = struct.unpack(">H", payload[:2])[0] if len(payload) >= 2 else None
                self._closed = True
                raise WSClosed(code, payload[2:].decode("utf-8", "replace"))
            if opcode in (OP_TEXT, OP_BIN):
                data = payload
            elif opcode == OP_CONT:
                data += payload
            if fin:
                break
        try:
            return json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            raise WSError(f"bad JSON frame: {e}")
