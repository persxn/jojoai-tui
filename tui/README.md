# `tui/` — the JojoAI terminal client (`jojo`)

A full-screen curses coding agent in Python: it reads and edits files, runs
commands and searches, with an approval prompt before every write. Standard
library only, plus `windows-curses` on Windows. This file is also the package
description of the wheel.

| Mode | How it runs | Use when |
|---|---|---|
| **local** (default) | Drives a model server on this machine: Ollama (`127.0.0.1:11434`), or any OpenAI-compatible server (LM Studio, llama.cpp, vLLM, Jan…) with a host ending in `/v1`. The agent loop runs here. | You have a machine that can run a model |
| **remote** (`--remote https://host`) | Email-OTP login, then a websocket session to a server that runs the loop and the model. Tools still run here, jailed to the project, with your approval. | You want a model you cannot host |

Works with models that call tools natively (gpt-oss, qwen3, llama3.1…) and with
models that write tool calls as text (qwen2.5-coder, codellama, gemma…): those
calls are recognised and run, never printed at you.

## Folder contents

| Path | What |
|---|---|
| `jojocode_ai/` | The package. Module-by-module map, the flow of one turn, and a "where to look" table: [`jojocode_ai/README.md`](jojocode_ai/README.md). |
| `tests/` | pytest suite (fake model server, a real pty for the curses tests). See [`tests/README.md`](tests/README.md). |
| `pyproject.toml` | Package metadata (`jojocode-ai-tui`), the `jojo` entry point, `windows-curses` on Windows only. Built with hatchling. |
| `COMPATIBILITY.md` | Terminals, platforms, models and keys: what works where, and how each degrades. |

## Run from source

```sh
cd tui
python3 -m jojocode_ai --help
python3 -m jojocode_ai                                  # local, current directory
python3 -m jojocode_ai --model qwen2.5-coder:7b         # another Ollama model
python3 -m jojocode_ai --host http://127.0.0.1:1234/v1  # an OpenAI-compatible server
python3 -m jojocode_ai --headless "explain main.py"     # one task, no curses, then exit
python3 -m jojocode_ai --remote https://ai.jojocode.in --login
```

Settings precedence: flags > `JOJO_HOST` / `JOJO_MODEL` > `settings.json` >
`OLLAMA_HOST` > defaults (`config.py`). Settings and credentials live in
`~/.config/jojocode-ai/` (`JOJO_CONFIG` overrides); history, the recall index
and `crash.log` in `~/.local/share/jojocode-ai/` (`JOJO_DATA` overrides). The
same paths are used under your home directory on Windows.

## Test

```sh
python3 -m venv .venv && .venv/bin/pip install pytest
.venv/bin/python -m pytest tui/tests -q
```

## Build and publish the wheel

```sh
cd tui && python3 -m pip wheel --no-deps -w ../dist/dl .
echo jojocode_ai_tui-<version>-py3-none-any.whl > ../dist/dl/tui-wheel
```

The installers read `dist/dl/tui-wheel` to find the wheel. Bump `version` in
`pyproject.toml` and `jojocode_ai/__init__.py` for every published build: pip
treats an installed package of the same version as up to date, so a rebuilt
wheel with an unchanged version never reaches people who already have it.
