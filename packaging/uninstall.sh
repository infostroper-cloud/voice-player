#!/usr/bin/env bash
# ==============================================================================
# VoicePlayer Clean Uninstaller (macOS & Linux)
# Usage:
#   ~/.local/bin/voiceplayer-uninstall [--quiet] [--purge]
#   or:
#   curl -fsSL https://raw.githubusercontent.com/infostroper-cloud/voice-player/main/uninstall.sh | bash
# ==============================================================================
set -e

PURGE=0
QUIET=0
for arg in "$@"; do
    case "$arg" in
        --purge|-p) PURGE=1 ;;
        --quiet|-q|--silent|-s) QUIET=1 ;;
    esac
done

if [ "$QUIET" -eq 0 ]; then
    echo "=== 🗑️ VoicePlayer Uninstaller (macOS & Linux) ==="
fi

OS_TYPE="$(uname -s)"
case "$OS_TYPE" in
    Darwin*) PLATFORM="macOS" ;;
    Linux*)  PLATFORM="Linux" ;;
    *)       PLATFORM="Unknown" ;;
esac

# 1. Terminate running processes
if [ "$QUIET" -eq 0 ]; then echo "Stopping running VoicePlayer processes..."; fi
pkill -f "voiceplayer" 2>/dev/null || true
pkill -f "voice-player" 2>/dev/null || true
pkill -f "VoicePlayer" 2>/dev/null || true

# 2. Remove launchers and shortcuts
if [ "$QUIET" -eq 0 ]; then echo "Removing launchers and application shortcuts..."; fi
rm -f "$HOME/.local/bin/voiceplayer"
rm -f "$HOME/.local/bin/voiceplayer-uninstall"
rm -f "$HOME/.local/share/applications/voiceplayer.desktop"

# macOS bundles and launchers (if present)
rm -rf "/Applications/VoicePlayer.app" 2>/dev/null || true
rm -rf "$HOME/Applications/VoicePlayer.app" 2>/dev/null || true
rm -f "$HOME/Applications/VoicePlayer.command" 2>/dev/null || true

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
fi

# 3. Clean up installation directory
INSTALL_DIR="$HOME/.voice-player"
if [ -d "$INSTALL_DIR" ]; then
    if [ "$QUIET" -eq 0 ]; then echo "Removing installation directory: $INSTALL_DIR"; fi
    rm -rf "$INSTALL_DIR"
fi

# 4. Optional purge of preferences, session history, and archive DB
if [ "$PURGE" -eq 1 ]; then
    if [ "$QUIET" -eq 0 ]; then echo "Purging user settings and session archives..."; fi
    rm -f "$HOME/.voice-player-prefs.json"
    rm -f "$HOME/.voice-player-saved.json"
    rm -f "$HOME/.voice-player-session.json"
    rm -f "$HOME/.voice-player-pronounce.json"
    rm -f "$HOME/.voice-player-archive.db"
    rm -f "$HOME/.voice-player-crash.log"
fi

# 5. Remove PATH additions from rc files
for RC in "$HOME/.zshrc" "$HOME/.bashrc" "$HOME/.bash_profile"; do
    if [ -f "$RC" ]; then
        sed -i.bak '/\.voice-player/d' "$RC" 2>/dev/null || true
        rm -f "${RC}.bak"
    fi
done

if [ "$QUIET" -eq 0 ]; then
    echo "=========================================================="
    echo "✅ VoicePlayer has been completely uninstalled."
    if [ "$PURGE" -eq 0 ]; then
        echo "💡 Your preferences and speech history were preserved in ~."
        echo "   To remove everything including settings, run with: --purge"
    fi
    echo "=========================================================="
fi
