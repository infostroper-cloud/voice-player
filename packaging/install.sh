#!/usr/bin/env bash
# ==============================================================================
# VoicePlayer Universal Installer (macOS & Linux)
# Run via curl:
#   curl -fsSL https://raw.githubusercontent.com/infostroper-cloud/voice-player/main/install.sh | bash
# ==============================================================================
set -e

echo "=== 🎧 VoicePlayer Universal Installer (macOS & Linux) ==="

OS_TYPE="$(uname -s)"
case "$OS_TYPE" in
    Darwin*)    PLATFORM="macOS" ;;
    Linux*)     PLATFORM="Linux" ;;
    *)          PLATFORM="Unknown" ;;
esac

echo "Detected platform: $PLATFORM"

# Check Python 3
if command -v python3 >/dev/null 2>&1; then
    PY_BIN="python3"
elif command -v python >/dev/null 2>&1; then
    PY_BIN="python"
else
    echo "Error: Python 3.10+ is required. Please install Python first."
    exit 1
fi

PY_VER=$($PY_BIN -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "Using Python: $PY_BIN (version $PY_VER)"

# Check tkinter
if ! $PY_BIN -c "import tkinter" >/dev/null 2>&1; then
    echo "⚠ Warning: Tkinter is not found for $PY_BIN."
    if [ "$PLATFORM" = "macOS" ]; then
        echo "  👉 On macOS with Homebrew: run 'brew install python-tk' or reinstall official python.org package."
    else
        echo "  👉 On Ubuntu/Debian: run 'sudo apt install python3-tk libportaudio2'"
        echo "  👉 On Fedora/RHEL: run 'sudo dnf install python3-tkinter portaudio'"
        echo "  👉 On Arch Linux: run 'sudo pacman -S tk portaudio'"
    fi
fi

INSTALL_DIR="$HOME/.voice-player"
VERSIONS_DIR="$INSTALL_DIR/versions"
TARGET_DIR="$VERSIONS_DIR/1.2.0"
mkdir -p "$TARGET_DIR"

# Download VoicePlayer files
echo "Downloading VoicePlayer v1.2.0 for $PLATFORM..."
TMP_TAR="/tmp/voice-player-1.2.0.tar.gz"
DOWNLOAD_URL="https://github.com/infostroper-cloud/voice-player/releases/latest/download/VoicePlayer-1.2.0-macos-linux.tar.gz"

if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$DOWNLOAD_URL" -o "$TMP_TAR" || \
    curl -fsSL "https://github.com/infostroper-cloud/voice-player/releases/latest/download/voice-player-1.2.0.zip" -o "/tmp/vp.zip"
elif command -v wget >/dev/null 2>&1; then
    wget -qO "$TMP_TAR" "$DOWNLOAD_URL" || \
    wget -qO "/tmp/vp.zip" "https://github.com/infostroper-cloud/voice-player/releases/latest/download/voice-player-1.2.0.zip"
else
    echo "Error: curl or wget is required."
    exit 1
fi

if [ -f "$TMP_TAR" ]; then
    mkdir -p /tmp/vp-extract
    tar -xzf "$TMP_TAR" -C /tmp/vp-extract
    SRC_DIR=$(find /tmp/vp-extract -type d -name "voice-player" | head -n 1)
    if [ -d "$SRC_DIR" ]; then
        cp -r "$SRC_DIR"/* "$TARGET_DIR/"
    else
        cp -r /tmp/vp-extract/* "$TARGET_DIR/"
    fi
    rm -rf "$TMP_TAR" /tmp/vp-extract
elif [ -f "/tmp/vp.zip" ]; then
    unzip -q -o "/tmp/vp.zip" -d "$TARGET_DIR"
    rm -f "/tmp/vp.zip"
fi

# Create stable pointer
mkdir -p "$INSTALL_DIR"
cat << EOF > "$INSTALL_DIR/current-version.json"
{"version": "1.2.0"}
EOF

# Install python dependencies in user mode
echo "Installing Python dependencies (sounddevice, edge-tts, piper-tts, etc.)..."
$PY_BIN -m pip install --upgrade --user --quiet -r "$TARGET_DIR/requirements.txt" || \
$PY_BIN -m pip install --upgrade --quiet -r "$TARGET_DIR/requirements.txt" || true

# Launcher script
mkdir -p "$HOME/.local/bin"
LOCAL_LAUNCHER="$HOME/.local/bin/voiceplayer"

cat << EOF > "$LOCAL_LAUNCHER"
#!/usr/bin/env bash
exec "$PY_BIN" "$TARGET_DIR/app.py" "\$@"
EOF
chmod +x "$LOCAL_LAUNCHER"

# Desktop integration for Linux
if [ "$PLATFORM" = "Linux" ]; then
    DESKTOP_DIR="$HOME/.local/share/applications"
    mkdir -p "$DESKTOP_DIR"
    cat << EOF > "$DESKTOP_DIR/voiceplayer.desktop"
[Desktop Entry]
Name=VoicePlayer
Comment=AI Voice & TTS Player
Exec=$LOCAL_LAUNCHER
Icon=$TARGET_DIR/icon.png
Terminal=false
Type=Application
Categories=AudioVideo;Audio;Player;Utility;
EOF
    chmod +x "$DESKTOP_DIR/voiceplayer.desktop"
    echo "✓ Linux desktop launcher created in $DESKTOP_DIR/voiceplayer.desktop"
fi

# Desktop integration for macOS
if [ "$PLATFORM" = "macOS" ]; then
    MAC_APP_DIR="$HOME/Applications"
    mkdir -p "$MAC_APP_DIR"
    MAC_LAUNCHER="$MAC_APP_DIR/VoicePlayer.command"
    cat << EOF > "$MAC_LAUNCHER"
#!/usr/bin/env bash
exec "$PY_BIN" "$TARGET_DIR/app.py" "\$@"
EOF
    chmod +x "$MAC_LAUNCHER"
    echo "✓ macOS launcher created in $MAC_LAUNCHER"
fi

echo "=========================================================="
echo "🎉 VoicePlayer v1.2.0 installed successfully!"
echo "Run from terminal: ~/.local/bin/voiceplayer"
echo "Or start now..."
echo "=========================================================="

# Start in background if GUI session is active
if [ -n "$DISPLAY" ] || [ "$PLATFORM" = "macOS" ]; then
    nohup "$LOCAL_LAUNCHER" >/dev/null 2>&1 &
fi
