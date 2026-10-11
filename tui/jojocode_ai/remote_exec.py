"""Local execution of the tool_requests a remote server streams down.

The server runs the agent loop; these run on *this* machine (decision D1). Every
path is forced inside the project root, and the caller (remote.RemoteAgent) is
responsible for getting the user's approval on `destructive` requests before
calling here.

Returns a dict ready to drop into a `tool_result` frame (minus `t`/`id`).
"""

from __future__ import annotations

import json
import os
import time

from . import shell

MAX_READ = 100_000
MAX_OUT = 20_000


def _jail(root: str, path: str) -> str | None:
    """Resolve `path` under `root`; None if it escapes."""
    root = os.path.realpath(root)
    full = os.path.realpath(os.path.join(root, path or "."))
    # normcase: on Windows C:\Proj and c:\proj are the same root
    r, f = os.path.normcase(root), os.path.normcase(full)
    if f == r or f.startswith(r + os.sep):
        return full
    return None


def _clip(s: str, limit: int = MAX_OUT) -> str:
    if len(s) <= limit:
        return s
    return s[: limit - 400] + f"\n... [truncated {len(s) - limit + 400} chars] ...\n" + s[-300:]


def _crlf(path: str) -> bool:
    """Does an existing file use Windows line endings? (Keep them if so.)"""
    try:
        with open(path, "rb") as f:
            head = f.read(65536)
    except OSError:
        return False
    crlf = head.count(b"\r\n")
    return bool(crlf) and crlf >= head.count(b"\n") - crlf


def execute(name: str, args: dict, root: str) -> dict:
    t0 = time.time()
    try:
        payload = _dispatch(name, args or {}, root)
    except FileNotFoundError as e:
        payload = {"ok": False, "error": f"not found: {e}"}
    except PermissionError as e:
        payload = {"ok": False, "error": f"permission denied: {e}"}
    except OSError as e:
        payload = {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001
        # Anything else (a bad argument type, an encoding error) used to escape
        # here, so no tool_result was ever sent and the server's turn hung
        # waiting for one. The model gets told instead, like every other error.
        payload = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    payload.setdefault("ok", True)
    payload["durationMs"] = int((time.time() - t0) * 1000)
    return payload


def _dispatch(name: str, args: dict, root: str) -> dict:
    # The network tools are jail-exempt by nature: there is no path to confine,
    # and the confinement that matters for them is the SSRF guard in
    # `websearch`, which refuses anything resolving inside this network.
    if name == "web_search":
        from . import websearch
        try:
            results = websearch.search(
                args.get("query", ""),
                args.get("count", 5),
                provider=os.environ.get("JOJO_SEARCH_PROVIDER", ""),
                key=os.environ.get("JOJO_SEARCH_KEY", ""),
            )
        except websearch.WebError as e:
            return {"ok": False, "error": str(e)}
        if not results:
            return {"ok": True, "stdout": "no results came back (the search provider "
                                          "returned nothing usable)", "count": 0}
        body = "\n".join(
            f"{i}. {r.title}\n   {r.url}" + (f"\n   {r.snippet}" if r.snippet else "")
            for i, r in enumerate(results, 1)
        )
        return {"ok": True, "stdout": _clip(body), "count": len(results)}

    if name == "fetch_url":
        from . import websearch
        try:
            return {"ok": True, "content": websearch.fetch(args.get("url", ""))}
        except websearch.WebError as e:
            return {"ok": False, "error": str(e)}

    if name == "list_dir":
        full = _jail(root, args.get("path", "."))
        if full is None:
            return {"ok": False, "refused": "policy", "error": "path escapes project root"}
        if not os.path.isdir(full):
            return {"ok": False, "error": f"not a directory: {args.get('path', '.')}"}
        rows = []
        for e in sorted(os.listdir(full)):
            p = os.path.join(full, e)
            rows.append(f"{e}/" if os.path.isdir(p) else f"{e}  ({os.path.getsize(p)} B)")
        rel = os.path.relpath(full, root)
        return {"ok": True, "stdout": f"{rel}\n" + "\n".join(f"  {r}" for r in rows)}

    if name == "read_file":
        full = _jail(root, args.get("path", ""))
        if full is None:
            return {"ok": False, "refused": "policy", "error": "path escapes project root"}
        if not os.path.isfile(full):
            return {"ok": False, "error": f"no such file: {args.get('path')}"}
        cap = int(args.get("max_bytes", MAX_READ) or MAX_READ)
        with open(full, "r", encoding="utf-8", errors="replace") as f:
            data = f.read(cap + 1)
        more = len(data) > cap
        return {"ok": True, "content": data[:cap] + ("\n... [clipped]" if more else "")}

    if name == "write_file":
        full = _jail(root, args.get("path", ""))
        if full is None:
            return {"ok": False, "refused": "policy", "error": "path escapes project root"}
        content = args.get("content", "")
        if not isinstance(content, str):
            content = json.dumps(content, indent=2) if isinstance(content, (dict, list)) else str(content)
        content = content.replace("\r\n", "\n")
        os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
        # UTF-8 and the file's own line endings, as in tools.py: the locale
        # codec is cp1252 on Windows and text mode would CRLF every line.
        if _crlf(full):
            content = content.replace("\n", "\r\n")
        with open(full, "w", encoding="utf-8", newline="") as f:
            f.write(content)
        return {"ok": True, "bytesWritten": len(content.encode())}

    if name == "run_command":
        cmd = args.get("cmd") or args.get("command") or ""
        if not cmd:
            return {"ok": False, "error": "no command"}
        timeout = int(args.get("timeout_ms", 120_000) or 120_000) / 1000
        out, err, code, timed_out = shell.run(cmd, root, timeout)
        if timed_out:
            return {"ok": False, "refused": "timeout", "error": f"timed out after {timeout:.0f}s",
                    "stdout": _clip(out), "stderr": _clip(err)}
        return {
            "ok": code == 0,
            "stdout": _clip(out),
            "stderr": _clip(err),
            "exitCode": code,
        }

    return {"ok": False, "error": f"unknown tool {name!r}"}


def summarize(name: str, payload: dict) -> str:
    """A one-liner for the TUI's tool_end line."""
    if payload.get("refused"):
        return f"{payload['refused']}: {payload.get('error', '')}".strip()
    if payload.get("error"):
        return f"error: {payload['error']}"
    if name == "read_file":
        return f"{len(payload.get('content', ''))} chars"
    if name == "list_dir":
        return payload.get("stdout", "").split("\n", 1)[0]
    if name == "write_file":
        return f"wrote {payload.get('bytesWritten', 0)} bytes"
    if name == "run_command":
        return f"exit {payload.get('exitCode')}"
    if name == "web_search":
        return f"{payload.get('count', 0)} results"
    if name == "fetch_url":
        return f"{len(payload.get('content', ''))} chars"
    return "ok"
