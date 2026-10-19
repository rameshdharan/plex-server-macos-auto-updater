#!/bin/bash
# ==============================================================================
# Plex Media Server macOS Auto-Updater Uninstaller
# ==============================================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UPDATER_SCRIPT="$SCRIPT_DIR/plex_updater.py"

echo "============================================================"
echo "   Plex Media Server macOS Auto-Updater Uninstaller"
echo "============================================================"

if [ -f "$UPDATER_SCRIPT" ]; then
    python3 "$UPDATER_SCRIPT" service uninstall
else
    PLIST_FILE="$HOME/Library/LaunchAgents/com.plex.macos-auto-updater.plist"
    if [ -f "$PLIST_FILE" ]; then
        launchctl unload "$PLIST_FILE" 2>/dev/null || true
        rm -f "$PLIST_FILE"
        echo "[+] Removed $PLIST_FILE"
    fi
fi

echo ""
echo "[✓] Plex Auto-Updater LaunchAgent has been uninstalled."
echo "============================================================"
