#!/bin/sh
# install.sh — set up everything needed to run `jojo` on Linux or macOS.
#
#   curl -fsSL https://ai.jojocode.in/install.sh | sh
#
# Turnkey: installs Python (+ venv/pip) if missing, installs the TUI into a
# private virtualenv, drops a `jojo` launcher on your PATH, installs and starts
# Ollama, and pulls a model that fits this machine. Nothing needs to already be
# present. The only privileged step is the OS package install for Python/Ollama,
# done via sudo when you are not root.
#
# Env overrides:
#   JOJO_TUI_SOURCE      pip target (default: the endpoint wheel, then the git repo)
#   JOJO_PREFIX          venv location      (default ~/.local/share/jojocode-ai)
#   JOJO_BINDIR          launcher location  (default ~/.local/bin)
#   JOJO_ENDPOINT        where to fetch helper scripts (default https://ai.jojocode.in)
#   JOJO_NO_OLLAMA=1     install the TUI only; skip Ollama + model
#   JOJO_MODEL=<tag>     pull this model instead of the auto-recommended one
#   JOJO_NONINTERACTIVE=1  never prompt (assume yes)
set -eu

# ── Everything below runs only once the whole script has been read ──────────
#
# `curl … | sh` streams: the shell executes each statement as the bytes arrive.
# This script installs OS packages partway through, which can take minutes on a
# slow mirror, and the HTTP connection feeding it sits idle that whole time. If
# the far end closes it, `sh` reaches EOF mid-script and stops at whatever line
# the bytes ran out on — reporting a syntax error in a file that is perfectly
# valid. Ubuntu hit this reproducibly: 189 lines of 308.
#
# Wrapping the body in a function fixes it at the parser level. The shell must
# read to the closing brace before it can run `_jojo_main`, so the download is
# complete before any package install begins.
_jojo_main() {

ENDPOINT="${JOJO_ENDPOINT:-https://ai.jojocode.in}"
PREFIX="${JOJO_PREFIX:-$HOME/.local/share/jojocode-ai}"
BINDIR="${JOJO_BINDIR:-$HOME/.local/bin}"
VENV="$PREFIX/venv"
REPO_PIP='jojocode-ai-tui @ git+https://github.com/persxn/jojoai-tui.git#subdirectory=tui'

say()  { printf '%s\n' "$*"; }
step() { printf '\n\033[1;32m▸ %s\033[0m\n' "$*"; }
warn() { printf '\033[33m! %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[31minstall: %s\033[0m\n' "$*" >&2; exit 1; }

OS="$(uname -s 2>/dev/null || echo unknown)"
case "$OS" in
  Linux|Darwin) : ;;
  *) die "this script is for Linux/macOS. On Windows run install.ps1 in PowerShell." ;;
esac
command -v curl >/dev/null 2>&1 || command -v wget >/dev/null 2>&1 || die "need curl or wget"
fetch() { if command -v curl >/dev/null 2>&1; then curl -fsSL "$1"; else wget -qO- "$1"; fi; }

# Do not let this run under `sudo`.
#
# It is the natural second thing to try when the first run fails, and it makes
# things strictly worse: sudo keeps $HOME, so every file below is written into
# the real user's home owned by root, and the *next* unprivileged run cannot
# read or repair any of it. This script already escalates on its own for the
# one step that needs it (the OS package install), so sudo is never the answer.
# A genuine root session — a container, a root-only box — has no SUDO_USER and
# is left alone.
if [ "$(id -u)" -eq 0 ] && [ -n "${SUDO_USER:-}" ]; then
  die "do not run this with sudo — it would write root-owned files into ${HOME}.

  Run it as yourself:  curl -fsSL $ENDPOINT/install.sh | sh
  It will ask for your password only if it has to install a system package.

  Already ran it with sudo? Clear the leftovers first:
      sudo rm -rf \"$PREFIX\" \"$BINDIR/jojo\""
fi

# The same damage, detected from the other side: a previous sudo run left a
# prefix this user cannot write. Say so plainly instead of failing later with
# a permission error from somewhere deep inside pip.
if [ -e "$PREFIX" ] && [ ! -w "$PREFIX" ]; then
  die "\"$PREFIX\" exists but is not writable by $(id -un) (a previous sudo run?).

  Remove it and re-run without sudo:
      sudo rm -rf \"$PREFIX\" \"$BINDIR/jojo\""
fi

# --- privilege helper --------------------------------------------------
SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  if command -v sudo >/dev/null 2>&1; then SUDO="sudo"
  else warn "not root and no sudo — OS package installs will be skipped"; fi
fi
run_priv() { if [ -n "$SUDO" ] || [ "$(id -u)" -eq 0 ]; then $SUDO "$@"; else return 1; fi; }

# --- detect the OS package manager -----------------------------------
PM=""
for c in apt-get dnf pacman zypper apk brew; do
  command -v "$c" >/dev/null 2>&1 && { PM="$c"; break; }
done

pm_install() { # pm_install <pkg...>
  [ -n "$PM" ] || { warn "no known package manager; install manually: $*"; return 1; }
  case "$PM" in
    apt-get) run_priv apt-get update -qq && run_priv apt-get install -y "$@" ;;
    dnf)     run_priv dnf install -y "$@" ;;
    pacman)  run_priv pacman -Sy --noconfirm "$@" ;;
    zypper)  run_priv zypper --non-interactive install "$@" ;;
    apk)     run_priv apk add "$@" ;;
    brew)    brew install "$@" ;;   # brew must not run under sudo
  esac
}

# --- 1. Python >= 3.9 (+ venv + pip) --------------------------------
step "Python"
find_python() {
  for c in python3 python3.13 python3.12 python3.11 python3.10 python3.9 python; do
    command -v "$c" >/dev/null 2>&1 || continue
    "$c" -c 'import sys;raise SystemExit(0 if sys.version_info[:2]>=(3,9) else 1)' 2>/dev/null \
      && { printf '%s' "$c"; return 0; }
  done
  return 1
}
PY="$(find_python || true)"
if [ -z "$PY" ]; then
  say "  not found — installing"
  case "$PM" in
    apt-get) pm_install python3 python3-venv python3-pip ;;
    dnf)     pm_install python3 python3-pip ;;
    pacman)  pm_install python ;;
    zypper)  pm_install python3 python3-venv python3-pip ;;
    apk)     pm_install python3 py3-pip ;;
    brew)    pm_install python@3.12 ;;
    *)       : ;;
  esac || true
  PY="$(find_python || true)"
fi
[ -n "$PY" ] || die "could not get Python >= 3.9. Install it and re-run:
  Debian/Ubuntu:  sudo apt install python3 python3-venv python3-pip
  Fedora:         sudo dnf install python3 python3-pip
  Arch:           sudo pacman -S python
  macOS:          brew install python@3.12"
say "  $(command -v "$PY")  ($("$PY" --version 2>&1))"

# `venv` usable? (Debian splits it out — and not where you would look.)
#
# This used to ask `python3 -m venv --help`, which is the wrong question. On a
# python3-minimal box the venv *module* is present, so --help succeeds; what is
# missing is `ensurepip`, and that only fails when a venv is actually created.
# The check passed, creation failed a few lines later, and the installer carried
# on to report success having installed nothing runnable. So: probe by building
# a throwaway venv, which is the thing we are about to do for real.
venv_works() {
  probe="$(mktemp -d 2>/dev/null || echo "${TMPDIR:-/tmp}/jojo-venv-probe.$$")"
  mkdir -p "$probe" 2>/dev/null
  "$PY" -m venv "$probe/v" >/dev/null 2>&1 && [ -x "$probe/v/bin/python" ]
  rc=$?
  rm -rf "$probe" 2>/dev/null
  return $rc
}

if ! venv_works; then
  say "  installing the venv module"
  # Debian names the package after the interpreter's own version
  # (python3.11-venv); the unversioned metapackage does not always exist or
  # does not always point where this interpreter needs. Try both.
  PYVER="$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "")"
  case "$PM" in
    apt-get)
      pm_install python3-venv || true
      [ -n "$PYVER" ] && ! venv_works && pm_install "python${PYVER}-venv" || true
      ;;
    zypper) pm_install python3-venv || true ;;
    *) : ;;
  esac
  venv_works || die "python cannot create a virtualenv — install the venv module:

  Debian/Ubuntu:  sudo apt install python3-venv${PYVER:+ or python${PYVER}-venv}
  Fedora:         sudo dnf install python3-libs
  Alpine:         sudo apk add python3"
fi

# --- 2. virtualenv + the TUI ---------------------------------------
step "The TUI"
mkdir -p "$PREFIX"
# Checked, not assumed. An unchecked `venv` here is how the previous version
# walked past a failed creation and spent the rest of the run reporting
# progress it had not made.
if [ ! -x "$VENV/bin/python" ]; then
  "$PY" -m venv "$VENV" || die "could not create the virtualenv at $VENV"
  [ -x "$VENV/bin/python" ] || die "the virtualenv at $VENV has no interpreter"
fi
PIP() { "$VENV/bin/python" -m pip install --quiet "$@"; }
PIP --upgrade pip >/dev/null 2>&1 || true

installed=0
if [ -n "${JOJO_TUI_SOURCE:-}" ]; then
  PIP "$JOJO_TUI_SOURCE" && installed=1 || die "could not install '$JOJO_TUI_SOURCE'"
else
  # 1) a prebuilt wheel from the endpoint — needs only pip; no git, no build tools
  WHEEL="$(fetch "$ENDPOINT/dl/tui-wheel" 2>/dev/null | tr -d ' \r\n' || true)"
  case "$WHEEL" in
    *.whl) PIP "$ENDPOINT/dl/$WHEEL" && { installed=1; say "  from $ENDPOINT/dl/$WHEEL"; } ;;
  esac
  # 2) PyPI
  [ "$installed" = 0 ] && PIP jojocode-ai-tui && { installed=1; say "  from PyPI"; }
  # 3) the git repo (needs git — install it if we can)
  if [ "$installed" = 0 ]; then
    command -v git >/dev/null 2>&1 || pm_install git || true
    if command -v git >/dev/null 2>&1; then
      PIP "$REPO_PIP" && { installed=1; say "  from the git repo"; }
    fi
  fi
fi
[ "$installed" = 1 ] || die "could not install the TUI (no wheel, no PyPI, no git)"
say "  $("$VENV/bin/jojo" --version 2>/dev/null || echo installed)"

# --- 3. launcher + PATH -------------------------------------------
step "Launcher"
mkdir -p "$BINDIR"
printf '#!/bin/sh\nexec "%s/bin/jojo" "$@"\n' "$VENV" > "$BINDIR/jojo"
chmod 0755 "$BINDIR/jojo"
say "  $BINDIR/jojo"

# Put BINDIR on PATH for every shell this person might actually open.
#
# Writing one rc file was the commonest "it installed fine but `jojo` is not
# found". A *login* bash on macOS reads ~/.bash_profile and never ~/.bashrc; a
# POSIX login shell reads ~/.profile; zsh reads ~/.zshrc (interactive) and
# ~/.zprofile (login); fish reads none of them and needs its own syntax; and
# $SHELL is very often unset or wrong under `curl | sh`. So we do not guess —
# we write to all of them, idempotently.
#
# The one rule that is not "write everywhere": ~/.bash_profile and ~/.zprofile
# are only touched if they ALREADY exist. Creating a .bash_profile makes bash
# stop reading .profile for login shells, which would silently break whatever
# else the user had set up there. Shadowing a file we did not create is not
# ours to do.
MARK='# jojocode-ai (added by install.sh)'
# Guarded rather than an unconditional prepend, so re-sourcing an rc file does
# not stack the same directory onto PATH ten times.
PATH_LINE="case \":\$PATH:\" in *\":$BINDIR:\"*) ;; *) PATH=\"$BINDIR:\$PATH\" ;; esac; export PATH"

add_line() { # add_line <file> <line>
  _f="$1"; _l="$2"
  if [ ! -e "$_f" ]; then
    [ -d "$(dirname "$_f")" ] || return 1
    : > "$_f" 2>/dev/null || return 1
  fi
  # A stable marker, not the whole line: the user may reformat what we wrote,
  # and an exact-line grep would then append a duplicate on every re-run.
  grep -qF "$MARK" "$_f" 2>/dev/null && return 0
  printf '\n%s\n%s\n' "$MARK" "$_l" >> "$_f" 2>/dev/null || return 1
  say "  PATH set in $_f"
  return 0
}

for _rc in "$HOME/.profile" "$HOME/.bashrc" "$HOME/.zshrc"; do
  add_line "$_rc" "$PATH_LINE" || true
done
for _rc in "$HOME/.bash_profile" "$HOME/.zprofile"; do
  [ -e "$_rc" ] && { add_line "$_rc" "$PATH_LINE" || true; }
done
if [ -d "$HOME/.config/fish" ]; then
  add_line "$HOME/.config/fish/config.fish" "fish_add_path $BINDIR" || true
fi

# Is it usable *right now*? `curl | sh` runs in a child process and cannot put
# anything on the parent shell's PATH, so say the one command that does rather
# than only "open a new shell".
ON_PATH=0
case ":$PATH:" in *":$BINDIR:"*) ON_PATH=1 ;; esac

# --- 4. Ollama + a model ---------------------------------------
if [ "${JOJO_NO_OLLAMA:-0}" = "1" ]; then
  step "Skipping Ollama (JOJO_NO_OLLAMA=1)"
else
  step "Ollama"
  # Ollama's own installer extracts a zstd archive and stops with
  # "This version requires zstd for extraction" on any image that does not ship
  # it — debian-slim among them, which is most containers and plenty of fresh
  # servers. It is a one-package fix and the failure is otherwise the last step
  # of a long install, so it is worth pre-empting rather than reporting.
  if ! command -v ollama >/dev/null 2>&1 && ! command -v zstd >/dev/null 2>&1; then
    say "  installing zstd (Ollama's installer needs it)"
    pm_install zstd || warn "could not install zstd — the Ollama step may fail"
  fi
  if command -v ollama >/dev/null 2>&1; then
    say "  already installed: $(ollama --version 2>/dev/null | head -1)"
  else
    fetch "$ENDPOINT/ollama-setup.sh" | sh || warn "Ollama setup had a problem — re-run: $ENDPOINT/ollama-setup.sh"
  fi
  if command -v ollama >/dev/null 2>&1 || [ -x "$HOME/.ollama/bin/ollama" ]; then
    step "Model"
    tmp_rec="$(mktemp)"; trap 'rm -f "$tmp_rec"' EXIT
    fetch "$ENDPOINT/recommend-model.py" > "$tmp_rec"
    if [ -n "${JOJO_MODEL:-}" ]; then
      "$VENV/bin/python" "$tmp_rec" --model "$JOJO_MODEL" --pull --yes || warn "model pull failed"
    else
      "$VENV/bin/python" "$tmp_rec" --pull --yes || warn "model pull failed — pick one later with recommend-model.py"
    fi
  fi
fi

if [ "$ON_PATH" = 1 ]; then
  READY="    jojo"
else
  READY="    export PATH=\"$BINDIR:\$PATH\"     # this shell, once
    jojo                                    # new shells: already set up"
fi

cat <<EOF

$(printf '\033[1;32m✓ ready\033[0m')  Start it in any project directory:

$READY

  Full path, always works:  $BINDIR/jojo

    jojo --model gpt-oss:20b                       # a specific model
    jojo --remote $ENDPOINT --login   # a hosted server

  Update:  re-run this installer — it always fetches the current build
             curl -fsSL $ENDPOINT/install.sh | sh
  Remove:  rm -rf "$PREFIX" "$BINDIR/jojo"   # and drop the jojocode-ai lines from your shell rc
EOF

}

_jojo_main "$@"
