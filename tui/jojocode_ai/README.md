# `tui/jojocode_ai/` — the JojoAI client package

Everything `jojo` runs on the user's machine: the curses UI, the agent loop, the
tools, and the clients for model servers and the remote backend. Stdlib only
(plus `windows-curses` on Windows). Every module's header docstring explains
*why* it is shaped the way it is — read it before changing the module.

## How one turn flows

```
keyboard ─▶ tui.TUI._handle_keys ─▶ _enter ─▶ _submit ─▶ worker thread ─▶ Agent.run
                                                                          │
   ┌──────────────────────────────────────────────────────────────────────┘
   ▼
Agent._run ── backends.make_client ─▶ OllamaClient / OpenAIClient .chat()
   │              (transport.stream: interruptible HTTP, no proxy for localhost)
   │   content deltas ─▶ toolcalls.StreamFilter ─▶ ("assistant", text) / calls / thinking
   │   tool calls ─▶ policy + approver (TUI approval panel) ─▶ tools.run_tool
   │                                   tools ─▶ textfiles (files) · shell (commands) · websearch (web)
   ▼
events ("assistant", "tool_start", "final", …) ─▶ TUI.events queue ─▶ _drain ─▶ _draw
```

Remote mode swaps `Agent` for `remote.RemoteAgent`: the loop runs on the server,
tool requests arrive over `wsjson`, and `remote_exec` runs them here.

## Modules

### Entry points

| File | What it does | Key functions |
|---|---|---|
| `__init__.py` | Package version (`__version__`). | — |
| `__main__.py` | `jojo` command line: parses flags, picks local / remote / headless, guards the TUI with a crash log. | `main` (argument handling and mode choice), `_remote_url` (normalises `--remote`), `_console_ok` (can PDCurses drive this terminal?), `_run_tui_guarded` (crash → `crash.log`). |
| `headless.py` | Plain REPL and one-shot runner without curses (scripts, CI, terminals curses cannot drive). | `run` (REPL / one-shot; exit code 1 on a failed turn), `_render` (prints one turn's events, sanitised), `_approver` (full-request y/N/a prompt), `_enable_ansi` (Windows console colours). |
| `tui.py` | The curses UI. | `TUI.loop` (main loop: drain events, draw only when dirty, read keys in batches), `_handle_keys` / `_tokens` / `_key` (input: paste detection, escape sequences, shortcuts), `_enter` / `_command` (submit, queue, slash commands), `_approval_key` / `_approval_lines` (approval panel), `_drain` (agent events → transcript blocks), `_rows` / `_block_rows` / `_render_block` (wrapping with a per-block cache), `_draw` (layout), `_startup` (connection checks off the UI thread), `run` (curses wrapper; closes the agent on exit). `Block` holds one transcript entry. |
| `theme.py` | Colours, gradients and the header animations. | `Theme.init` (palette per terminal capability), `rule`, `wave_rule`, `shimmer_text`, `block_art`. |
| `syntax.py` | Tiny syntax highlighter and Markdown code-fence splitter. | `split_fences` (prose vs code blocks), `highlight_line`, `inline_segments`, `norm_lang`. |
| `cols.py` | Terminal text: display width (CJK = 2 columns, combining marks = 0) and making text safe to draw. | `width`, `clip`, `wrap` (wrapping by columns), `clean` / `clean_delta` (strip escape sequences, `\r`, tabs), `visible` (approval prompt: control characters shown as symbols). |

### Local agent

| File | What it does | Key functions |
|---|---|---|
| `agent.py` | The tool loop: model → calls → approval → tools → repeat. Owns the conversation history. | `Agent.run` (one user turn; pairs every call with a result even if interrupted), `_run` (the loop), `_wire` (history → request; text-mode conversion; live system prompt), `_fit_history` (elides old tool output to fit the context window), `_pre_approved` (auto-edit / --trust rules via `policy`), `set_cwd`, `reset`, `interrupt`, `close`. |
| `prompts.py` | The system prompt, rebuilt each request from the current directory and approval settings. | `system_prompt`, `text_tool_protocol` (how to call tools, for models without native tool support), `_approvals`, `_shell_advice`. |
| `toolcalls.py` | Tool calls a model wrote as *text* (bare JSON, fences, `<tool_call>`, `<function=…>`, `[TOOL_CALLS]`) and inline `<think>` reasoning. | `StreamFilter.feed` / `finish` / `take_thinking` / `answer`, `calls_in` (recognise calls in a finished block), `_one` (what counts as a call), `_repair_backslashes` (Windows paths). |
| `policy.py` | Which writes auto-edit may approve, and which remote tools are read-only. | `auto_write_ok` (inside the project, not a dot-file or `.git` — as named and as resolved through symlinks — not `~` or `/`), `is_inside`, `READ_ONLY` (no tool that can send data out: `fetch_url` and `web_search` ask). |
| `tools.py` | Local tool schemas (`TOOLS`) and implementations (`IMPLS`); approval set `APPROVAL_REQUIRED`. | `run_tool` (dispatch; errors become `error: …` strings), `t_read_file` (streams, detects binary/encoding), `t_write_file` / `t_edit_file` (encoding- and line-ending-preserving, atomic), `t_run_bash`, `t_search` (`_rg` streamed and capped, `_walk_search` fallback, `_which_rg` never from cwd), `t_list_dir`, `t_change_dir`, `t_web_search`, `t_fetch_url`, `_resolve` (paths: `~`, quotes, Windows escapes), `_blocked` (JojoAI's own data is off limits). `Ctx` carries the tool cwd and the Ctrl-C flag. |
| `textfiles.py` | Reading/writing text files as their owner left them (shared with `remote_exec`). | `sniff` (encoding/BOM or binary), `decode` / `read` → `TextFile`, `TextFile.encode_text`, `write_atomic` (never leaves a truncated file). |
| `shell.py` | Running one command (shared with `remote_exec`). | `run` (empty stdin, output to temp files bounded head/tail, timeout and Ctrl-C kill the whole tree, background `&` jobs survive), `precheck` (Windows cmd.exe limits, refused before approval), `kill_all` (on quit), `as_command` (argv lists). |
| `websearch.py` | `web_search` / `fetch_url`: SSRF guard, DNS-pinned connections, size/time limits, HTML → text, the untrusted-content envelope. | `search`, `fetch`, `check_url` / `_vet` / `_is_public`, `_open`, `html_to_text` (`_Text` parser), `wrap_untrusted`, `clip`. |
| `store.py` | Chat history (JSONL per session) and the optional recall index (SQLite + embeddings, indexed on a background thread). | `Store.append`, `retrieve`, `context_block`, `recent_sessions`, `load_session`, `delete_session`, `stats`, `embed_available`. |

### Model servers

| File | What it does | Key functions |
|---|---|---|
| `backends.py` | Chooses the client: Ollama native API, or any OpenAI-compatible server (host ends in `/v1`, or `--api openai`). | `make_client`, `pick_api`. |
| `ollama_client.py` | Ollama `/api/chat`: asks `/api/show` what the model supports and shapes `think`, `tools`, `num_ctx` to fit. | `OllamaClient.chat` (event stream; learns from 400s), `capabilities`, `context_window`, `has_model`, `list_models`, `version`; `same_model`. |
| `openai_client.py` | The same interface over `/v1/chat/completions` (LM Studio, llama.cpp, vLLM, Jan, LocalAI…). | `OpenAIClient.chat` (SSE; tool-call fragments reassembled), `to_openai` (history → OpenAI message shape). |
| `transport.py` | HTTP for the model clients: a request on a helper thread so Ctrl-C works while the server is silent; loopback never via a proxy. | `stream`, `request`, `HTTPStatusError`, `Interrupted`, `TransportError`. |
| `net.py` | The User-Agent every request to our own service carries (Cloudflare bans urllib's default). | `headers`. |
| `config.py` | Settings precedence (flags > env > `settings.json` > `OLLAMA_HOST` > defaults) and the `Config` dataclass. | `Config`, `load_settings`, `save_settings`, `normalize_host`, `config_dir`, `settings_path`. |

### Remote mode

| File | What it does | Key functions |
|---|---|---|
| `remote.py` | `RemoteAgent`: the same event stream as `Agent`, over the server's websocket protocol. Decides approvals client-side, handles interrupt, liveness, resume/fresh sessions, idle seat release. | `RemoteAgent.run`, `_turn`, `_tool`, `_pre_approved`, `reset`, `close`, `set_cwd`; `ws_url`. |
| `remote_exec.py` | Runs the server's tool requests here, jailed to the project root; never raises. | `execute`, `_dispatch`, `_jail`, `summarize`. |
| `wsjson.py` | Minimal RFC 6455 websocket client for JSON frames, with HTTP CONNECT proxy support. | `WSClient.connect` / `send_json` / `recv_json` / `close`, `_mask`. |
| `auth.py` | Email-OTP login and the credentials file (mode 600, written atomically). | `otp_login`, `load_token`, `save_token`, `forget`, `stash_code`. |

## Debugging: where to look

| Symptom | Start here |
|---|---|
| Raw JSON / `<tool_call>` text shown instead of a tool running | `toolcalls.StreamFilter` (`_text`, `_resolve_*`), then `agent._run` |
| HTTP 400 "does not support thinking/tools" | `ollama_client.OllamaClient._think_value` / `chat`, `capabilities()` |
| A model ignores tools entirely | `agent._text_mode`, `prompts.text_tool_protocol` |
| Ctrl-C does not stop something | `transport.stream` (model), `shell.run` `cancel` (commands), `tui._key` / `_approval_key` |
| A write did / did not ask for approval | `agent._pre_approved`, `policy.auto_write_ok`, `tools.APPROVAL_REQUIRED` |
| File content or line endings changed unexpectedly | `textfiles` (`sniff`, `TextFile.encode_text`), `tools.t_edit_file` |
| A command hangs or its output is lost | `shell.run`, `shell._read_bounded`, `shell.precheck` (Windows) |
| Screen garbled, wrong width, paste sent early | `tui._draw`, `cols.width` / `wrap`, `tui._handle_keys` |
| `fetch_url` refuses or mangles a page | `websearch._vet`, `_open`, `_decode`, `html_to_text` |
| Remote turn never ends / seat stays held | `remote.RemoteAgent._turn` (interrupt grace, liveness), heartbeat in `_start_heartbeat` |
| Login fails with HTTP 403 | `net.headers` (User-Agent), `auth._post` |

Tests for all of this live in `../tests/` (see its README); `fake_ollama.py`
there stands in for a model server.
