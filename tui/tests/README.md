# `tui/tests/` — the client's test suite

pytest, no network: model servers, websocket gateways, proxies and web pages
are all fakes on `127.0.0.1`, and Windows behaviour is exercised by patching
`os.name` / `shell.IS_WINDOWS` (nothing here has run on real Windows). Each
file's header docstring names the failures its tests pin down — read it before
changing a test's expectation.

```sh
python3 -m venv .venv && .venv/bin/pip install pytest     # from the repo root
.venv/bin/python -m pytest tui/tests -q                    # ~30 s
.venv/bin/python -m pytest tui/tests/test_agent_loop.py -q -k think
```

The curses tests that drive the real TUI through a pty skip themselves on
non-POSIX systems; `test_websearch.py` has live-network cases that run only
with `JOJO_LIVE_TESTS=1`.

## Files

| File | Covers | Main targets |
|---|---|---|
| `fake_ollama.py` | Not a test: a fake Ollama (`/api/chat`, `/api/show`, `/api/tags`, `/api/version`) that rejects `think` / `tools` exactly as Ollama's `routes.go` does, scripted with canned replies. Used by most agent tests. | — |
| `test_model_compat.py` | Models other than gpt-oss end to end: `think` shaped by capability, qwen2.5-coder's JSON-text calls run (not printed), text mode for models without `tools`, learning from a 400 on old Ollama. | `ollama_client`, `agent`, `toolcalls` |
| `test_toolcalls.py` | Every text tool-call shape (bare JSON, fences, `<tool_call>`, `<function=…>`, `[TOOL_CALLS]`) fed whole and in tiny pieces, and the look-alikes that must be left alone. | `toolcalls.StreamFilter`, `calls_in` |
| `test_agent_loop.py` | The loop after the fix pass: inline `<think>` never run, schema/log look-alikes ignored, Windows paths in JSON, Ctrl-C while the model loads or at the approval prompt (history stays paired), context fitting, empty-reply nudge, auto-edit policy after `change_dir`, the live system prompt. | `agent`, `toolcalls`, `policy`, `prompts` |
| `test_backends.py` | OpenAI-compatible servers (SSE, fragmented tool calls, `reasoning_content`, falling back to text mode), backend choice, the interruptible transport and proxies (loopback direct, CONNECT), host normalisation and settings precedence, the history/recall store. | `openai_client`, `backends`, `transport`, `config`, `store` |
| `test_tools_platform.py` | UTF-8 everywhere (a static check that every text `open()` names `encoding=`), line endings kept, commands with empty stdin and a killable timeout, `replace_all: "false"`, search not reading JojoAI's own data. | `tools`, `shell` |
| `test_files_shell.py` | Encodings detected and preserved (BOM, UTF-16, cp1252), atomic writes (permissions, symlinks), mixed line endings, binary refusal, bounded command output, process-tree kill, background jobs, Windows `precheck`, the auto-approve policy, search via ripgrep and the fallback, remote `read_file` / `write_file` / `run_command`. | `textfiles`, `shell`, `tools`, `policy`, `remote_exec` |
| `test_websearch.py` | The SSRF guard's address classes, HTML → text, clipping, the untrusted-content envelope, search providers. | `websearch` |
| `test_web_hardening.py` | The hardening pass: CGNAT / site-local / IPv4-in-IPv6 addresses, DNS pinned to the vetted address, redirects re-vetted and capped, decompressed size and time caps, charset fallback, Content-Type deciding HTML, `start` paging, DuckDuckGo result links. | `websearch` |
| `test_remote_cli.py` | Remote mode against a fake gateway: interrupt, resume vs fresh session, liveness, `bye`, client-side approvals, late approvals, exactly-once replay, malformed frames; the websocket client (framing, masking, CONNECT proxy); OTP login and the credentials file; `__main__` flag handling; headless exit codes and prompts. | `remote`, `wsjson`, `auth`, `__main__`, `headless` |
| `test_tui.py` | The real curses TUI in a pty: Ctrl-C interrupts a turn instead of quitting, `finish` summaries shown, tool output sanitised. | `tui` |
| `test_tui_input.py` | Input without a terminal: paste detection (bracketed and burst), escape sequences and PDCurses key codes, Home/End, the approval prompt (grace period, two-press "always", pastes never answer it), slash commands and queueing, display widths, idle drawing. | `tui`, `cols` |
| `test_review_fixes.py` | The adversarial review's reproduced findings: valid JSON taken at its word (YAML content kept, Windows paths fixed by key), `<think>` prose kept, no two user turns in a row, the prompt tracking "always allow", cut-off calls hidden, proxy default port, OpenAI base paths, auto-edit vs symlinks into `.git`, `fetch_url` gated, new-file modes and read-only files, CRLF honoured, ripgrep and the fallback agreeing, bounded line reads, `GIT_EDITOR`. | `toolcalls`, `agent`, `prompts`, `transport`, `openai_client`, `policy`, `textfiles`, `tools`, `shell` |
| `test_review_round2.py` | The second review round: remote mode never reads JojoAI's own data, a replayed tool never runs twice, a message refused mid-turn is sent when the old turn ends, a turn that ended while disconnected ends here too, batched requests share the server's clock, `close()` on a dead socket; the web deadline over the whole exchange, 103 Early Hints, raw-UTF-8 redirects, address fallback, proxy-only DNS, tables and unclosed `<head>`, charset labels; bidi characters named in approvals; cmd.exe quoting, UTF-16 output, runnable files never auto-written, the mintty REPL and login. | `remote`, `remote_exec`, `wsjson`, `websearch`, `cols`, `shell`, `policy`, `headless`, `auth`, `store` |
| `test_config.py` | Which model a fresh terminal opens on: flag > `JOJO_MODEL` > `settings.json` > default, and the installer and the TUI agreeing on the file. | `config` |
| `test_net.py` | No outbound request goes out with urllib's default User-Agent (Cloudflare bans it). | `net` |

## Adding a test

- Reproduce the bug first; name the test after the behaviour, not the fix.
- Use the fakes (`fake_ollama.py`, the fake gateway and web servers inside
  the test files) rather than the network.
- Anything timing-based should allow for a loaded machine: assert "within a
  second", not "within 50 ms".
