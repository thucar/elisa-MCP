#!/usr/bin/env bash
# Install the Elisa TV MCP server for the current user and run the setup wizard.
#   ./install.sh               interactive setup
#   ./install.sh --help        setup options (passed to `elisa-tv setup`)
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${XDG_DATA_HOME:-$HOME/.local/share}/elisa-tv"
BIN="$HOME/.local/bin"

command -v python3 >/dev/null || { echo "python3 is required"; exit 1; }
python3 - <<'EOF' || { echo "Python 3.10+ is required"; exit 1; }
import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)
EOF
python3 -c 'import sqlite3; sqlite3.connect(":memory:").execute("create virtual table t using fts5(x)")' \
  || { echo "Python's sqlite3 lacks FTS5 support"; exit 1; }
command -v systemctl >/dev/null || echo "warning: systemd not found; use --no-service and run 'elisa-tv serve' yourself"

mkdir -p "$PREFIX" "$BIN"
if [ ! -x "$PREFIX/venv/bin/python" ]; then
  python3 -m venv "$PREFIX/venv" || { echo "creating a virtualenv failed (Debian/Ubuntu: sudo apt install python3-venv)"; exit 1; }
fi
echo "Installing elisa-tv-mcp into $PREFIX/venv ..."
"$PREFIX/venv/bin/pip" install -q --upgrade pip
"$PREFIX/venv/bin/pip" install -q --upgrade "$SRC"
ln -sf "$PREFIX/venv/bin/elisa-tv" "$BIN/elisa-tv"
case ":$PATH:" in *":$BIN:"*) ;; *) echo "note: add $BIN to your PATH to use the 'elisa-tv' command";; esac

exec "$PREFIX/venv/bin/elisa-tv" setup "$@"
