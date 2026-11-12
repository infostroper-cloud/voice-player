#!/usr/bin/env bash
# ==============================================================================
# VoicePlayer 1-Click Double-Clickable Installer for macOS
# ==============================================================================
set -e
export TK_SILENCE_DEPRECATION=1
clear

echo "=========================================================="
echo "🎧 VoicePlayer macOS 1-Click Installer"
echo "=========================================================="
echo ""
echo "Starting universal installation..."
curl -fsSL https://raw.githubusercontent.com/infostroper-cloud/voice-player/main/install.sh | bash

echo ""
echo "=========================================================="
echo "✅ VoicePlayer setup finished! You can close this window."
echo "=========================================================="
read -r -p "Press [Enter] to exit..." _ || true
