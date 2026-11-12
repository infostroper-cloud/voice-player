#!/bin/bash
# ==============================================================================
# VoicePlayer 1.2.0 — macOS Double-Click Installer
# Double-click this file in Finder to install VoicePlayer on macOS.
# ==============================================================================
clear
echo "============================================================"
echo "  🎧 VoicePlayer 1.2.0 Installer for macOS"
echo "  Apple Silicon (M1/M2/M3/M4) & Intel x86_64"
echo "============================================================"
echo ""

# Find Python 3
PY_BIN=""
if [ -x "/opt/homebrew/bin/python3" ]; then
    PY_BIN="/opt/homebrew/bin/python3"
elif [ -x "/usr/local/bin/python3" ]; then
    PY_BIN="/usr/local/bin/python3"
elif command -v python3 >/dev/null 2>&1; then
    PY_BIN="python3"
fi

if [ -z "$PY_BIN" ]; then
    echo "❌ Error: Python 3 was not found on your Mac."
    echo "Please install Python 3.10+ from https://www.python.org or run 'brew install python'."
    read -p "Press Enter to exit..."
    exit 1
fi

echo "✓ Using Python: $PY_BIN ($($PY_BIN --version))"
echo "Downloading and installing VoicePlayer..."

curl -fsSL https://raw.githubusercontent.com/infostroper-cloud/voice-player/main/install.sh | bash

echo ""
echo "============================================================"
echo "  🎉 Installation completed!"
echo "  You can find VoicePlayer in your Applications folder:"
echo "  ~/Applications/VoicePlayer.command"
echo "============================================================"
read -p "Press Enter to finish..."
