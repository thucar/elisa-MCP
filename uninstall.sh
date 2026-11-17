#!/usr/bin/env bash
# Remove the Elisa TV MCP server. Config and data are kept unless --purge is given.
set -euo pipefail
PREFIX="${XDG_DATA_HOME:-$HOME/.local/share}/elisa-tv"
CONF="${XDG_CONFIG_HOME:-$HOME/.config}/elisa-tv"
UNIT="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/elisa-tv.service"

if command -v systemctl >/dev/null; then
  systemctl --user disable --now elisa-tv.service 2>/dev/null || true
  rm -f "$UNIT"
  systemctl --user daemon-reload || true
fi
rm -f "$HOME/.local/bin/elisa-tv"
rm -rf "$PREFIX/venv"
if [ "${1:-}" = "--purge" ]; then
  rm -rf "$PREFIX" "$CONF"
  echo "Removed program, config, Elisa tokens and guide data."
else
  echo "Removed program and service. Kept config ($CONF) and data ($PREFIX); use --purge to delete them."
fi
echo "The TV still trusts this computer's ADB key; revoke it on the TV under Developer options if wanted."
