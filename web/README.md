# `web/` — ai.jojocode.in

Static landing + docs + a live status panel. No in-browser agent.

| File | What |
|---|---|
| `index.html` | The whole site in one file: markup, CSS and JS inline; three.js (cdnjs) for the animated backdrop. |
| `bg-test.mjs` | Browser regression for the backdrop: still animating at the bottom of the page, shaders compile, no console errors. `node web/bg-test.mjs [url]`; needs Playwright (not a dependency of this repo). |

- Hero + the one-line install command, with platform tabs
- How it works (local mode vs remote mode), model card
- Live status: seats used/free, model health, protocol version — fetched from
  the backend's `/status.json`, renders even when the server is offline

`index.html` is one self-contained file. Design system adapted from JojoCode's
`/product` page (eyebrow / title+gradient-accent / lede, pill buttons, section
rhythm, reveal-on-scroll, pointer-lit cards, sticky scroll-spy bar) but in the
TUI's green-on-near-black identity. Space Grotesk + JetBrains Mono via Google
Fonts, with system fallbacks, and three.js from cdnjs for the backdrop; everything
else inline, no other external requests.

Sections: hero + copyable install command (OS tabs) · live status strip ·
how-it-works (local vs remote + an inline SVG data-flow diagram) · the agent
loop · models · **every keyboard shortcut in the TUI** · install (TUI / Ollama /
model, update & remove) · security · a full live status panel. All figures come
from `/status.json` and degrade to "unreachable" when the server is down.

Served by `server/backend`: `/` → `web/index.html`; `/install.sh`, `/install.ps1`,
`/ollama-setup.sh`, `/recommend-model.py` → `install/`. Override the dirs with
`JOJOAI_WEB_DIR` / `JOJOAI_INSTALL_DIR`.

```sh
JOJOAI_PORT=7433 node --experimental-strip-types ../server/backend/src/index.ts
# open http://127.0.0.1:7433/
```
