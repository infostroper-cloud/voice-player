#!/usr/bin/env bash
# ==============================================================================
# VoicePlayer Universal Installer (macOS & Linux)
# Run via:
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

# Select best Python binary (prefer Homebrew or python.org Python on macOS if available)
PY_BIN=""
if [ "$PLATFORM" = "macOS" ]; then
    for candidate in \
        "/opt/homebrew/bin/python3" \
        "/usr/local/bin/python3" \
        "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3" \
        "/Library/Frameworks/Python.framework/Versions/3.11/bin/python3" \
        "/Library/Frameworks/Python.framework/Versions/3.10/bin/python3" \
        "$HOME/.pyenv/shims/python3" \
        "$HOME/miniconda3/bin/python3" \
        "$HOME/miniforge3/bin/python3"
    do
        if [ -x "$candidate" ]; then
            PY_BIN="$candidate"
            break
        fi
    done
fi

if [ -z "$PY_BIN" ]; then
    if command -v python3 >/dev/null 2>&1; then
        PY_BIN="python3"
    elif command -v python >/dev/null 2>&1; then
        PY_BIN="python"
    else
        echo "❌ Error: Python 3.10+ is required. Please install Python first."
        exit 1
    fi
fi

PY_VER=$($PY_BIN -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || echo "unknown")
echo "Using Python: $PY_BIN (version $PY_VER)"

# Test Tkinter GUI capability
HAS_TK=0
if $PY_BIN -c "import tkinter as tk; r = tk.Tk(); r.withdraw(); r.destroy()" >/dev/null 2>&1; then
    HAS_TK=1
fi

if [ "$HAS_TK" -eq 0 ]; then
    echo "⚠️ Warning: Tkinter GUI could not initialize on current Python ($PY_BIN)."
    if [ "$PLATFORM" = "macOS" ]; then
        echo "=========================================================="
        echo "💡 IMPORTANT FOR macOS USERS:"
        echo "Apple's CommandLineTools Python lacks full Tkinter GUI support."
        echo "To use VoicePlayer with graphic UI, please either:"
        echo "  1) Install Python via Homebrew (recommended):"
        echo "     brew install python python-tk"
        echo "  2) Or install official Python 3.12 for macOS:"
        echo "     https://www.python.org/downloads/macos/"
        echo "=========================================================="
    else
        echo "  👉 On Ubuntu/Debian: run 'sudo apt update && sudo apt install -y python3-tk libportaudio2'"
        echo "  👉 On Fedora/RHEL: run 'sudo dnf install -y python3-tkinter portaudio'"
        echo "  👉 On Arch Linux: run 'sudo pacman -S --noconfirm tk portaudio'"
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
ZIP_URL="https://github.com/infostroper-cloud/voice-player/releases/latest/download/voice-player-1.2.0.zip"

DOWNLOADED=0
if command -v curl >/dev/null 2>&1; then
    if curl -fsSL "$DOWNLOAD_URL" -o "$TMP_TAR"; then
        DOWNLOADED=1
    elif curl -fsSL "$ZIP_URL" -o "/tmp/vp.zip"; then
        DOWNLOADED=2
    fi
elif command -v wget >/dev/null 2>&1; then
    if wget -qO "$TMP_TAR" "$DOWNLOAD_URL"; then
        DOWNLOADED=1
    elif wget -qO "/tmp/vp.zip" "$ZIP_URL"; then
        DOWNLOADED=2
    fi
fi

if [ "$DOWNLOADED" -eq 0 ]; then
    echo "❌ Error: Failed to download VoicePlayer package from GitHub Releases."
    exit 1
fi

mkdir -p /tmp/vp-extract
if [ "$DOWNLOADED" -eq 1 ] && [ -f "$TMP_TAR" ]; then
    tar -xzf "$TMP_TAR" -C /tmp/vp-extract
    rm -f "$TMP_TAR"
elif [ -f "/tmp/vp.zip" ]; then
    $PY_BIN -m zipfile -e "/tmp/vp.zip" /tmp/vp-extract
    rm -f "/tmp/vp.zip"
fi

# Locate app.py in extracted tree and copy to TARGET_DIR
APP_FOUND=$(find /tmp/vp-extract -type f -name "app.py" | head -n 1)
if [ -n "$APP_FOUND" ]; then
    SRC_DIR="$(dirname "$APP_FOUND")"
    cp -r "$SRC_DIR"/* "$TARGET_DIR/"
else
    # Check if a zip exists inside extracted files
    NESTED_ZIP=$(find /tmp/vp-extract -type f -name "*.zip" | head -n 1)
    if [ -n "$NESTED_ZIP" ]; then
        $PY_BIN -m zipfile -e "$NESTED_ZIP" "$TARGET_DIR"
    else
        cp -r /tmp/vp-extract/* "$TARGET_DIR/"
    fi
fi
rm -rf /tmp/vp-extract

# If target directory somehow has a nested zip, unpack it
if [ -f "$TARGET_DIR/voice-player-1.2.0.zip" ]; then
    $PY_BIN -m zipfile -e "$TARGET_DIR/voice-player-1.2.0.zip" "$TARGET_DIR"
    rm -f "$TARGET_DIR/voice-player-1.2.0.zip"
fi

# Verify app.py exists
if [ ! -f "$TARGET_DIR/app.py" ]; then
    echo "❌ Error: app.py was not found in $TARGET_DIR after extraction."
    exit 1
fi

# Create stable pointer
mkdir -p "$INSTALL_DIR"
cat << EOF > "$INSTALL_DIR/current-version.json"
{"version": "1.2.0"}
EOF

# Install python dependencies in user mode
echo "Installing Python dependencies (sounddevice, edge-tts, piper-tts, etc.)..."
if [ -f "$TARGET_DIR/requirements.txt" ]; then
    $PY_BIN -m pip install --user --quiet -r "$TARGET_DIR/requirements.txt" || \
    $PY_BIN -m pip install --quiet -r "$TARGET_DIR/requirements.txt" || true
else
    echo "Installing core dependencies directly..."
    $PY_BIN -m pip install --user --quiet sounddevice numpy edge-tts miniaudio pillow lameenc audiotsm || true
fi

# Launcher script
mkdir -p "$HOME/.local/bin"
LOCAL_LAUNCHER="$HOME/.local/bin/voiceplayer"

cat << EOF > "$LOCAL_LAUNCHER"
#!/usr/bin/env bash
cd "$TARGET_DIR"
exec "$PY_BIN" "$TARGET_DIR/app.py" "\$@"
EOF
chmod +x "$LOCAL_LAUNCHER"

# Add ~/.local/bin to PATH in shell profile if needed
for RC in "$HOME/.zshrc" "$HOME/.bashrc" "$HOME/.bash_profile"; do
    if [ -f "$RC" ] && ! grep -q '\.local/bin' "$RC"; then
        echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$RC"
    fi
done

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
cd "$TARGET_DIR"
exec "$PY_BIN" "$TARGET_DIR/app.py" "\$@"
EOF
    chmod +x "$MAC_LAUNCHER"
    echo "✓ macOS launcher created in $MAC_LAUNCHER"
fi

echo "=========================================================="
echo "🎉 VoicePlayer v1.2.0 installed successfully!"
echo "Run from terminal: ~/.local/bin/voiceplayer"
if [ "$PLATFORM" = "macOS" ]; then
    echo "Or launch from Finder: ~/Applications/VoicePlayer.command"
fi
echo "=========================================================="

# Start in background if GUI session is active and Tkinter works
if [ "$HAS_TK" -eq 1 ] && { [ -n "$DISPLAY" ] || [ "$PLATFORM" = "macOS" ]; }; then
    nohup "$LOCAL_LAUNCHER" >/dev/null 2>"$INSTALL_DIR/crash.log" &
elif [ "$PLATFORM" = "macOS" ] && [ "$HAS_TK" -eq 0 ]; then
    echo "ℹ️  Skipped automatic launch because Tkinter is missing."
    echo "   After running 'brew install python python-tk', start with: ~/.local/bin/voiceplayer"
fi
