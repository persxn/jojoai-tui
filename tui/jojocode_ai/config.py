"""Runtime configuration for JojoAI.

── Where the model comes from ──────────────────────────────────────────────

The installer looks at the machine, picks a model that will actually fit, and
pulls it. It used to stop there — and the TUI then started with a hard-coded
`gpt-oss:120b` regardless, so a laptop that had just been told "qwen2.5-coder:7b
suits this machine", and had pulled exactly that, opened on a 65 GB model it did
not have and reported it as missing. The recommendation was real work thrown
away at the last step.

So there is now one written-down answer, `settings.json` beside the saved
credentials, and everything reads it. Highest wins:

    --model <tag>            this run only
    JOJO_MODEL=<tag>         this shell
    settings.json "model"    what the installer chose for this machine
    FALLBACK_MODEL           a machine nobody has measured

The same file holds `host` (and `api`), so a box running Ollama elsewhere is
configured once rather than aliased into every shell. Host precedence is
--host, JOJO_HOST, settings.json, then OLLAMA_HOST — the variable the user's
own Ollama already reads, which used to be ignored here — and a host is
accepted the way Ollama accepts it: `localhost:11434`, `0.0.0.0`, `:11434`.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field


def config_dir() -> str:
    """Where credentials and settings live (shared with auth.py)."""
    return os.environ.get("JOJO_CONFIG") or os.path.expanduser("~/.config/jojocode-ai")


def settings_path() -> str:
    return os.path.join(config_dir(), "settings.json")


def load_settings() -> dict:
    """Never raises: a missing or corrupt settings file must not stop the TUI
    from starting, because its whole job is to make starting easier.

    utf-8-sig: Notepad and PowerShell 5.1 save with a BOM, which plain utf-8
    rejected — and the whole file was then silently ignored."""
    try:
        with open(settings_path(), encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(**values) -> str:
    """Merge `values` in and write the file back. Returns the path written.

    A value of None removes that key, so "unset the model again" is expressible
    without hand-editing JSON.
    """
    path = settings_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = load_settings()
    if not data and os.path.exists(path):
        # Unreadable, not empty: keep it rather than overwrite a hand edit
        # (with a typo in it) that held more than this one key.
        try:
            os.replace(path, path + ".bak")
        except OSError:
            pass
    for k, v in values.items():
        if v is None:
            data.pop(k, None)
        else:
            data[k] = v
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)          # atomic: never leaves a half-written file
    return path


# The model to use when nobody — not the installer, not the environment, not the
# command line — has said anything. Deliberately the big one: an unconfigured
# install is usually the hosted DGX, where it is the right answer.
FALLBACK_MODEL = "gpt-oss:120b"

def normalize_host(host: str) -> str:
    """A URL for whatever the user wrote, read the way Ollama reads OLLAMA_HOST.

    No scheme means http and port 11434; 0.0.0.0 / :: (an address to listen
    on, not to connect to — Windows refuses the connection) means loopback.
    """
    h = (host or "").strip().rstrip("/")
    if not h:
        return "http://127.0.0.1:11434"
    scheme, sep, rest = h.partition("://")
    if not sep:
        scheme, rest = "http", h
        hostport, slash, path = rest.partition("/")
        if hostport.startswith(":") and hostport[1:].isdigit():
            hostport = "127.0.0.1" + hostport                # ":11434"
        if hostport.count(":") > 1 and not hostport.startswith("["):
            hostport = f"[{hostport}]"                       # a bare IPv6 address
        has_port = hostport.rsplit(":", 1)[-1].isdigit() and not hostport.endswith("]")
        if not has_port:
            hostport += ":11434"
        rest = hostport + (slash + path if slash else "")
    for any_addr in ("0.0.0.0", "[::]"):
        if rest == any_addr or rest.startswith(any_addr + ":") or rest.startswith(any_addr + "/"):
            rest = "127.0.0.1" + rest[len(any_addr):]
    return f"{scheme.lower()}://{rest}"


def _default_mouse() -> bool:
    # Mouse reporting in a classic Windows console (conhost) turns QuickEdit
    # off: right-click paste and drag-to-select stop working, and "hold Shift"
    # does not bring them back there. Windows Terminal sets WT_SESSION.
    return not (os.name == "nt" and not os.environ.get("WT_SESSION"))


_settings = load_settings()

DEFAULT_HOST = normalize_host(os.environ.get("JOJO_HOST")
                              or _settings.get("host")
                              or os.environ.get("OLLAMA_HOST")
                              or "http://127.0.0.1:11434")
DEFAULT_MODEL = (os.environ.get("JOJO_MODEL")
                 or _settings.get("model")
                 or FALLBACK_MODEL)
# "ollama", "openai" (any OpenAI-compatible server) or "auto" (by the host URL)
DEFAULT_API = os.environ.get("JOJO_API") or _settings.get("api") or "auto"


@dataclass
class Config:
    host: str = DEFAULT_HOST
    model: str = DEFAULT_MODEL
    api: str = DEFAULT_API
    cwd: str = field(default_factory=os.getcwd)
    # gpt-oss reasoning: True/False, or "low" / "medium" / "high"
    think: object = "medium"
    num_ctx: int = 32768
    max_steps: int = 40
    request_timeout: int = 600
    # each chat is isolated. History is always saved per-session, but context
    # from other chats is pulled in ONLY when rag is explicitly enabled
    # (--rag / `/rag on` / `/recall --use`).
    rag: bool = False
    embed_model: str = os.environ.get("JOJO_EMBED_MODEL", "nomic-embed-text")
    # create / edit files without a prompt (still scoped to the working dir).
    # Turn off with --safe.
    auto_edit: bool = True
    # run shell commands without a prompt too. Off by default; --trust turns it on.
    auto_bash: bool = False
    # tool names that may run without asking; "*" means everything
    auto_approve: set = field(default_factory=set)

    # capture the mouse wheel to scroll the transcript (--no-mouse / `/mouse` to disable;
    # hold Shift, or Option on macOS, to select text while it's on)
    mouse: bool = field(default_factory=_default_mouse)

    # remote mode: when set, the agent loop runs on this server and `host`/`model`
    # above are ignored (the server owns them). Token comes from `jojo --login`.
    remote_endpoint: str | None = None
    project_label: str = field(
        default_factory=lambda: os.path.basename(os.getcwd()) or "project")

    def approves(self, tool_name: str) -> bool:
        return "*" in self.auto_approve or tool_name in self.auto_approve
