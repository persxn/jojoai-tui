"""JojoCode AI TUI — a curses-based agentic coding assistant.

Drives a Claude-Code-style tool loop (read/write/edit files, run shell commands,
search) against a model served by Ollama. Two modes:

- **local**  — talks straight to your own Ollama (default gpt-oss:120b); the
  agent loop runs on this machine, fully offline at runtime.
- **remote** — logs in to a hosted server that runs the loop for you; tools
  still execute locally, and you approve every write and command.
"""

__version__ = "0.3.0"
