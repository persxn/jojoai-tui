"""Remote credentials: storage + the email-OTP login flow.

The token is opaque; we store only what the server gave us, in
`~/.config/jojocode-ai/credentials` (mode 600), keyed by endpoint. Until the
server's OTP endpoints exist you can also stash a demo access code with
`--code`, which the current gateway accepts as the token.
"""

from __future__ import annotations

import getpass
import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.request

from .net import headers as _headers


def creds_path() -> str:
    base = os.environ.get("JOJO_CONFIG") or os.path.expanduser("~/.config/jojocode-ai")
    return os.path.join(base, "credentials")


def _load_all() -> dict:
    try:
        with open(creds_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def load_token(endpoint: str) -> str | None:
    entry = _load_all().get(endpoint.rstrip("/"))
    return entry.get("token") if entry else None


def _write_all(data: dict) -> None:
    """Replace the credentials file atomically, private from the first byte.

    It used to be truncated in place: an interrupted save (Ctrl-C, a full
    disk) lost every saved token, not just the one being written."""
    path = creds_path()
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    if os.name != "nt":
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def save_token(endpoint: str, token: str, email: str | None = None) -> None:
    data = _load_all()
    data[endpoint.rstrip("/")] = {"token": token, "email": email, "savedAt": int(time.time())}
    _write_all(data)


def forget(endpoint: str) -> bool:
    data = _load_all()
    if data.pop(endpoint.rstrip("/"), None) is None:
        return False
    _write_all(data)
    return True


def _json(raw: bytes) -> dict:
    try:
        obj = json.loads(raw or b"{}")
    except ValueError:
        return {}           # a captive portal or proxy page, not our server
    return obj if isinstance(obj, dict) else {}


def _post(url: str, body: dict, timeout: int = 20) -> tuple[int, dict]:
    """Every failure is a RuntimeError with a sentence in it — --login used to
    end in a traceback on a timeout, a reset, or an HTML error page."""
    try:
        # The agent matters: a default urllib one is banned at the edge (see net.py).
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(), method="POST",
            headers=_headers({"Content-Type": "application/json"}),
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, _json(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, _json(e.read())
        except (OSError, http.client.HTTPException):   # an error page cut short
            return e.code, {}
    except urllib.error.URLError as e:
        raise RuntimeError(f"cannot reach {url}: {e.reason}") from e
    except (OSError, http.client.HTTPException, ValueError) as e:
        raise RuntimeError(f"cannot reach {url}: {e}") from e


def _read_code() -> str:
    # getpass on Windows talks to the console directly; under Git Bash
    # (mintty) that console is hidden, so the prompt never showed and the
    # login hung. A pipe gets a plain prompt (the code is single-use).
    if os.name == "nt" and not sys.stdin.isatty():
        return input("code: ").strip()
    return getpass.getpass("code: ").strip()


def otp_login(endpoint: str, email: str | None = None,
              code: str | None = None) -> str:
    """Interactive email + OTP. Returns a token and persists it."""
    endpoint = endpoint.rstrip("/")
    try:
        email = email or input("email: ").strip()
    except (EOFError, KeyboardInterrupt):
        raise RuntimeError("login cancelled") from None
    if not email:
        raise RuntimeError("no email given")

    status, _ = _post(f"{endpoint}/api/auth/otp/request", {"email": email})
    # The server always answers 200 ("if that address is registered, a code is
    # on its way") to avoid leaking who has an account.
    if status not in (200, 202):
        raise RuntimeError(f"otp request failed (HTTP {status})")
    print("a 6-digit code was sent if that address is registered.", file=sys.stderr)

    try:
        otp = code or _read_code()
    except (EOFError, KeyboardInterrupt):
        raise RuntimeError("login cancelled") from None
    status, data = _post(f"{endpoint}/api/auth/otp/verify", {"email": email, "code": otp})
    token = data.get("token")
    if status != 200 or not token:
        raise RuntimeError(data.get("error") or f"verification failed (HTTP {status})")

    save_token(endpoint, token, email)
    print(f"logged in — token saved to {creds_path()}", file=sys.stderr)
    return token


def stash_code(endpoint: str, code: str) -> str:
    """Save a demo access code as the bearer token (no OTP round-trip)."""
    save_token(endpoint, code, email=None)
    return code
