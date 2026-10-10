#!/bin/sh
# Jazi's Agent Board: installer.   https://github.com/jazithedev/agent-board
#
#   curl -fsSL https://raw.githubusercontent.com/jazithedev/agent-board/HEAD/install.sh | sh
#
# Puts the board in ~/.local/share/agent-board and an `agent-board` command in ~/.local/bin.
# Needs python3 (3.10+) and git or curl. No sudo, nothing installed system-wide, no packages from PyPI.
# Run it again at any time to update.
#
# Options (when piping:  curl ... | sh -s -- --with-limits-mod):
#   --with-limits-mod   also install the companion Claude Code mod that feeds your rate limits to the board
#   --uninstall         remove the board (your settings in ~/.config/agent-board are kept)
#   --help              this text
set -eu

REPO="${AGENT_BOARD_REPO:-jazithedev/agent-board}"
SOURCE="${AGENT_BOARD_SOURCE:-https://github.com/$REPO.git}"
HOME_DIR="${AGENT_BOARD_HOME:-$HOME/.local/share/agent-board}"
BIN_DIR="${AGENT_BOARD_BIN:-$HOME/.local/bin}"
SRC_DIR="$HOME_DIR/src"
LAUNCHER="$BIN_DIR/agent-board"

say()  { printf '%s\n' "$*"; }
warn() { printf '! %s\n' "$*" >&2; }
die()  { printf '✗ %s\n' "$*" >&2; exit 1; }

WITH_MOD=0
UNINSTALL=0
for arg in "$@"; do
  case "$arg" in
    --with-limits-mod) WITH_MOD=1 ;;
    --uninstall) UNINSTALL=1 ;;
    -h|--help) sed -n '2,15p' "$0" 2>/dev/null | sed 's/^# \{0,1\}//' ; exit 0 ;;
    *) die "unknown option: $arg (try --help)" ;;
  esac
done

if [ "$UNINSTALL" = 1 ]; then
  rm -f "$LAUNCHER"
  rm -rf "$HOME_DIR"
  say "✓ Removed $LAUNCHER and $HOME_DIR"
  say "  Your settings and tags are still in ~/.config/agent-board (delete that folder to forget them)."
  exit 0
fi

PY=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    PY="$(command -v "$candidate")"
    break
  fi
done
[ -n "$PY" ] || die "Python 3.10 or newer is required, and none was found on your PATH."
"$PY" -c 'import curses' 2>/dev/null || die "This Python has no curses module. On Debian/Ubuntu it ships with python3; on some systems install python3-curses."

say "🦎 Installing Jazi's Agent Board"
mkdir -p "$HOME_DIR" "$BIN_DIR"

if command -v git >/dev/null 2>&1; then
  if [ -d "$SRC_DIR/.git" ]; then
    say "  updating the existing copy"
    if ! git -C "$SRC_DIR" pull --ff-only --quiet; then
      warn "could not update in place, fetching a fresh copy"
      rm -rf "$SRC_DIR"
    fi
  fi
  if [ ! -d "$SRC_DIR/.git" ]; then
    rm -rf "$SRC_DIR"
    git clone --depth 1 --quiet "$SOURCE" "$SRC_DIR" || die "could not download $SOURCE"
  fi
else
  if ! command -v curl >/dev/null 2>&1 || ! command -v tar >/dev/null 2>&1; then
    die "Need git, or curl and tar, to download the board."
  fi
  tmp="$(mktemp -d)"
  curl -fsSL "https://github.com/$REPO/archive/HEAD.tar.gz" | tar -xz -C "$tmp" --strip-components=1 || die "could not download https://github.com/$REPO"
  rm -rf "$SRC_DIR"
  mv "$tmp" "$SRC_DIR"
fi

cat > "$LAUNCHER" <<EOF
#!/bin/sh
exec "$PY" "$SRC_DIR/agent_board.py" "\$@"
EOF
chmod +x "$LAUNCHER"
VERSION="$("$LAUNCHER" --version 2>/dev/null || echo "agent-board")"
say "✓ Installed $VERSION → $LAUNCHER"

if [ "$WITH_MOD" = 1 ]; then
  CLAUDE_BIN="${AGENT_BOARD_CLAUDE:-claude}"
  if command -v "$CLAUDE_BIN" >/dev/null 2>&1; then
    say "  adding the limits mod to Claude Code"
    if ! "$CLAUDE_BIN" plugin marketplace add "$REPO" || ! "$CLAUDE_BIN" plugin install agent-board-limits@agent-board; then
      warn "could not install the mod; inside Claude Code run: /plugin install agent-board-limits --marketplace $REPO"
    fi
  else
    warn "Claude Code (the 'claude' command) was not found, so the limits mod was skipped."
  fi
fi

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *)
    rc="$HOME/.profile"
    case "${SHELL:-}" in */zsh) rc="$HOME/.zshrc" ;; */bash) rc="$HOME/.bashrc" ;; esac
    say ""
    warn "$BIN_DIR is not on your PATH yet. Add it once, then open a new terminal:"
    say "    echo 'export PATH=\"$BIN_DIR:\$PATH\"' >> $rc"
    ;;
esac

say ""
"$PY" "$SRC_DIR/agent_board.py" --doctor || true
say ""
say "Start it with:   agent-board"
say "Try it without touching anything:   agent-board --demo"
