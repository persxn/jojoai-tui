"""Local execution of the tool_requests a remote server streams down.

The server runs the agent loop; these run on *this* machine (decision D1). Every
path is forced inside the project root, and the caller (remote.RemoteAgent) is
responsible for getting the user's approval before calling here — it decides
which tools need it itself (policy.READ_ONLY), never trusting the server's
`destructive` flag alone.

Returns a dict ready to drop into a `tool_result` frame (minus `t`/`id`).
Never raises: a request that cannot be served is an `ok: False` result, because
a missing tool_result leaves the server's turn waiting for good.

Files go through textfiles.py and commands through shell.py, the same helpers
local mode uses: a file read and written back keeps its encoding (it used to
come back with every accented letter replaced by U+FFFD), and a command gets
no keyboard, a bounded output, and a timeout that kills its whole tree.
"""

from __future__ import annotations

import json
import os
import re
import time

from . import shell, textfiles

MAX_READ = 100_000
MAX_OUT = 20_000
MAX_ENTRIES = 500


def _jail(root: str, path) -> str | None:
    """Resolve `path` under `root`; None if it escapes."""
    if not isinstance(path, str):
        path = "."
    root = os.path.realpath(root)
    full = os.path.realpath(os.path.join(root, path or "."))
    # normcase: on Windows C:\Proj and c:\proj are the same root
    r, f = os.path.normcase(root), os.path.normcase(full)
    # a drive or filesystem root already ends in the separator ("C:\", "/")
    prefix = r if r.endswith(os.sep) else r + os.sep
    if f == r or f.startswith(prefix):
        return full
    return None


def _clip(s: str, limit: int = MAX_OUT) -> str:
    if len(s) <= limit:
        return s
    head, tail = s[: limit - 400], s[-300:]
    middle = s[len(head):len(s) - len(tail)]
    lost = shell.omitted_bytes(middle)      # the command's own marker, if cut here
    also = f"; {lost} bytes omitted before that" if lost else ""
    return head + f"\n... [truncated {len(middle)} chars{also}] ...\n" + tail


def _num(value, default: float) -> float:
    """A number from 120000, "120000" or null."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    m = re.search(r"\d+(\.\d+)?", str(value or ""))
    return float(m.group()) if m else default


_UNIT_MS = {"": 1, "ms": 1, "s": 1000, "sec": 1000, "secs": 1000, "second": 1000,
            "seconds": 1000, "m": 60_000, "min": 60_000, "mins": 60_000,
            "minute": 60_000, "minutes": 60_000}


def _millis(value, default: float) -> float:
    """Milliseconds from 120000, "120000", "30s", "2 min" or null. A bare "30s"
    read as 30 ms killed the command thirty times sooner than asked."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    m = re.match(r"\s*(\d+(?:\.\d+)?)\s*([a-z]*)", str(value or "").lower())
    if not m:
        return default
    return float(m.group(1)) * _UNIT_MS.get(m.group(2), 1)


def execute(name: str, args: dict, root: str, cancel=None) -> dict:
    t0 = time.time()
    try:
        payload = _dispatch(name, args if isinstance(args, dict) else {}, root, cancel)
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


def _dispatch(name: str, args: dict, root: str, cancel=None) -> dict:
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
            return {"ok": True, "content": websearch.fetch(
                args.get("url", ""), start=int(_num(args.get("start"), 0)))}
        except websearch.WebError as e:
            return {"ok": False, "error": str(e)}

    if name == "list_dir":
        full = _jail(root, args.get("path", "."))
        if full is None:
            return {"ok": False, "refused": "policy", "error": "path escapes project root"}
        if not os.path.isdir(full):
            return {"ok": False, "error": f"not a directory: {args.get('path', '.')}"}
        entries = sorted(os.scandir(full), key=lambda e: e.name)
        rows = []
        for e in entries[:MAX_ENTRIES]:
            try:
                rows.append(f"{e.name}/" if e.is_dir() else f"{e.name}  ({e.stat().st_size} B)")
            except OSError:
                # one dangling link used to fail the whole listing
                rows.append(f"{e.name}  (broken link)" if e.is_symlink() else f"{e.name}  (unreadable)")
        if len(entries) > MAX_ENTRIES:
            rows.append(f"... and {len(entries) - MAX_ENTRIES} more entries")
        # relative to the *resolved* root: a mapped or SUBST drive resolves to
        # a different volume, and relpath across volumes raises
        rel = os.path.relpath(full, os.path.realpath(root))
        return {"ok": True, "stdout": _clip(f"{rel}\n" + "\n".join(f"  {r}" for r in rows))}

    if name == "read_file":
        full = _jail(root, args.get("path", ""))
        if full is None:
            return {"ok": False, "refused": "policy", "error": "path escapes project root"}
        if not os.path.isfile(full):
            return {"ok": False, "error": f"no such file: {args.get('path')}"}
        cap = int(min(max(_num(args.get("max_bytes"), MAX_READ), 1), MAX_READ))
        with open(full, "rb") as f:
            data = f.read(cap + 1)
        more = len(data) > cap
        # partial: the cap can split a character, which used to make strict
        # UTF-8 fail and the whole read come back as cp1252 mojibake
        tf = textfiles.decode(data[:cap], partial=more)
        if tf is None:
            return {"ok": False, "error": f"{args.get('path')} is a binary file"}
        return {"ok": True, "content": tf.text + ("\n... [clipped]" if more else "")}

    if name == "write_file":
        full = _jail(root, args.get("path", ""))
        if full is None:
            return {"ok": False, "refused": "policy", "error": "path escapes project root"}
        content = args.get("content")
        if content is None:
            return {"ok": False, "error": "content is required (an empty string for an empty file)"}
        if not isinstance(content, str):
            content = json.dumps(content, indent=2) if isinstance(content, (dict, list)) else str(content)
        if os.path.isdir(full):
            return {"ok": False, "error": f"{args.get('path')} is a directory"}
        # an existing file keeps its encoding, BOM and line endings
        tf = (textfiles.read(full) if os.path.isfile(full) else None) or textfiles.new_file()
        try:
            data = tf.encode_text(content)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
        textfiles.write_atomic(full, data)
        return {"ok": True, "bytesWritten": len(data)}

    if name == "run_command":
        cmd = shell.as_command(args.get("cmd") or args.get("command") or "")
        if not cmd.strip():
            return {"ok": False, "error": "no command"}
        timeout = min(max(_millis(args.get("timeout_ms"), 120_000), 1000), 3_600_000) / 1000
        why = shell.precheck(cmd, root)
        if why:
            return {"ok": False, "error": why}
        r = shell.run(cmd, root, timeout, cancel=cancel)
        if r.timed_out or r.interrupted:
            why = f"timed out after {timeout:.0f}s" if r.timed_out else "interrupted by the user"
            return {"ok": False, "refused": "timeout" if r.timed_out else "user_rejected",
                    "error": why, "stdout": _clip(r.out), "stderr": _clip(r.err)}
        return {
            "ok": r.code == 0,
            "stdout": _clip(r.out),
            "stderr": _clip(r.err),
            "exitCode": r.code,
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
