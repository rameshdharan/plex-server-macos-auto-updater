#!/bin/bash
# ==============================================================================
# Plex Media Server macOS Auto-Updater Installer
# ==============================================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UPDATER_SCRIPT="$SCRIPT_DIR/plex_updater.py"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
PLIST_LABEL="com.plex.macos-auto-updater"
PLIST_FILE="$LAUNCH_AGENTS_DIR/$PLIST_LABEL.plist"
LOG_DIR="$HOME/Library/Logs"

echo "============================================================"
echo "    Plex Media Server macOS Auto-Updater Installer"
echo "============================================================"

# 1. Locate Python 3
PYTHON_BIN=""
if [ -x "/usr/bin/python3" ]; then
    PYTHON_BIN="/usr/bin/python3"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3)"
else
    echo "[-] Error: Python 3 was not found on your system."
    echo "    Please install Xcode Command Line Tools ('xcode-select --install') or Homebrew Python."
    exit 1
fi
echo "[+] Detected Python 3: $PYTHON_BIN ($($PYTHON_BIN --version))"

# 2. Make updater script executable
chmod +x "$UPDATER_SCRIPT"
echo "[+] Made $UPDATER_SCRIPT executable."

# 3. Initialize config.json if not present
if [ ! -f "$SCRIPT_DIR/config.json" ]; then
    cp "$SCRIPT_DIR/config.json.example" "$SCRIPT_DIR/config.json"
    echo "[+] Created default config file at $SCRIPT_DIR/config.json"
fi

# 4. Ensure LaunchAgents and Logs directories exist
mkdir -p "$LAUNCH_AGENTS_DIR"
mkdir -p "$LOG_DIR"

# 5. Delegate plist generation and loading to plex_updater.py
echo "[+] Installing macOS LaunchAgent service..."
"$PYTHON_BIN" "$UPDATER_SCRIPT" service install

echo ""
echo "============================================================"
echo "  [✓] Installation Successful!"
echo "============================================================"
echo "The service is now running in the background and will automatically"
echo "check for and install Plex updates according to your schedule."
echo ""
echo "Useful Commands:"
echo "  - Check status:        ./plex_updater.py status"
echo "  - Check for updates:   ./plex_updater.py check"
echo "  - Run update now:      ./plex_updater.py update"
echo "  - View service logs:   tail -f ~/Library/Logs/PlexAutoUpdater.log"
echo "  - Uninstall service:   ./uninstall.sh"
echo "============================================================"
