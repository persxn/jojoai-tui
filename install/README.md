# `install/` — one-command setup

| Script | Does |
|---|---|
| `install.sh` / `install.ps1` | **Turnkey.** Installs Python (+venv/pip) if missing — apt / dnf / pacman / zypper / apk / brew on Linux & macOS, winget on Windows — then a private venv + the TUI + a `jojo` launcher on PATH, then Ollama and a model that fits the machine. `JOJO_NO_OLLAMA=1` for the TUI only; `JOJO_MODEL=<tag>` to override the pick. This is what `curl -fsSL https://ai.jojocode.in/install.sh \| sh` (or `irm …/install.ps1 \| iex`) runs. The only privileged step is the OS package install (sudo when not root). |
| `ollama-setup.sh` | Detect OS/arch, install and start Ollama, set `OLLAMA_NUM_PARALLEL`. Idempotent. |
| `recommend-model.py` | Read CPU / RAM / NVIDIA VRAM / Apple + Grace/Tegra unified memory, recommend the largest model that runs well (`gpt-oss:120b` → `gpt-oss:20b` → `qwen2.5-coder:{14b,7b,3b}`), then `--pull` / `--run` it. stdlib only. |
| `test-install.sh` | Conformance run for `install.sh`: installs this working tree from scratch in clean containers (no Python, Python without venv, musl, dnf, a non-root user) and checks `jojo` actually starts. `./install/test-install.sh [distro]`; needs Docker. |
| `test-install-ps1.ps1` | Checks `install.ps1` without Windows: parses it, insists on plain ASCII (Windows PowerShell 5.1 reads BOM-less files as ANSI), runs PSScriptAnalyzer when present, and exercises `Invoke-Native` under strict error settings. `pwsh -NoProfile -File install/test-install-ps1.ps1`. |

The backend serves `install.sh`, `install.ps1`, `ollama-setup.sh` and
`recommend-model.py` straight from this directory (`server/backend/src/index.ts`),
so a change here is live as soon as the server's checkout is updated. The TUI
itself comes from the wheel named in `dist/dl/tui-wheel` on the server — see
`../tui/README.md` for publishing it.

Quick start:

```sh
curl -fsSL https://ai.jojocode.in/install.sh | sh          # TUI + launcher
curl -fsSL https://ai.jojocode.in/ollama-setup.sh | sh     # Ollama
python3 recommend-model.py --run                           # pick a model + go
```

Verified: `install.sh` into a sandbox prefix → Python detect → venv → pip install
→ `jojo` launcher, all green (Ollama step exercised separately).
`recommend-model.py` correctly identifies a unified-memory board and recommends
`gpt-oss:120b`. `install.ps1` is syntax- and brace-balance-reviewed; not yet run
on a Windows host.
