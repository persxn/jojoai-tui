"""Remote credentials: storage + the email-OTP login flow.

The token is opaque; we store only what the server gave us, in
`~/.config/jojocode-ai/credentials` (mode 600), keyed by endpoint. Until the
server's OTP endpoints exist you can also stash a demo access code with
`--code`, which the current gateway accepts as the token.
"""

from __future__ import annotations

import getpass
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


def save_token(endpoint: str, token: str, email: str | None = None) -> None:
    path = creds_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = _load_all()
    data[endpoint.rstrip("/")] = {"token": token, "email": email, "savedAt": int(time.time())}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.chmod(path, 0o600)


def forget(endpoint: str) -> bool:
    data = _load_all()
    if data.pop(endpoint.rstrip("/"), None) is None:
        return False
    fd = os.open(creds_path(), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    return True


def _post(url: str, body: dict, timeout: int = 20) -> tuple[int, dict]:
    # The agent matters: a default urllib one is banned at the edge (see net.py).
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers=_headers({"Content-Type": "application/json"}),
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}
    except urllib.error.URLError as e:
        raise RuntimeError(f"cannot reach {url}: {e.reason}")


def otp_login(endpoint: str, email: str | None = None,
              code: str | None = None) -> str:
    """Interactive email + OTP. Returns a token and persists it."""
    endpoint = endpoint.rstrip("/")
    email = email or input("email: ").strip()
    if not email:
        raise RuntimeError("no email given")

    status, _ = _post(f"{endpoint}/api/auth/otp/request", {"email": email})
    # The server always answers 200 ("if that address is registered, a code is
    # on its way") to avoid leaking who has an account.
    if status not in (200, 202):
        raise RuntimeError(f"otp request failed (HTTP {status})")
    print("a 6-digit code was sent if that address is registered.", file=sys.stderr)

    otp = code or getpass.getpass("code: ").strip()
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
