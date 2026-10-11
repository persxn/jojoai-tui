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

The same file holds `host`, so a box running Ollama elsewhere is configured
once rather than aliased into every shell.
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
    from starting, because its whole job is to make starting easier."""
    try:
        with open(settings_path(), encoding="utf-8") as f:
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

_settings = load_settings()

DEFAULT_HOST = (os.environ.get("JOJO_HOST")
                or _settings.get("host")
                or "http://127.0.0.1:11434")
DEFAULT_MODEL = (os.environ.get("JOJO_MODEL")
                 or _settings.get("model")
                 or FALLBACK_MODEL)


@dataclass
class Config:
    host: str = DEFAULT_HOST
    model: str = DEFAULT_MODEL
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
    mouse: bool = True

    # remote mode: when set, the agent loop runs on this server and `host`/`model`
    # above are ignored (the server owns them). Token comes from `jojo --login`.
    remote_endpoint: str | None = None
    project_label: str = field(
        default_factory=lambda: os.path.basename(os.getcwd()) or "project")

    def approves(self, tool_name: str) -> bool:
        return "*" in self.auto_approve or tool_name in self.auto_approve
