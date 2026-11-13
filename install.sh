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

# Select best Python binary with Tkinter support
PY_BIN=""
if [ "$PLATFORM" = "macOS" ]; then
    MAC_CANDIDATES=(
        "/opt/homebrew/bin/python3"
        "/usr/local/bin/python3"
        "/Library/Frameworks/Python.framework/Versions/3.13/bin/python3"
        "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3"
        "/Library/Frameworks/Python.framework/Versions/3.11/bin/python3"
        "/Library/Frameworks/Python.framework/Versions/Current/bin/python3"
        "$HOME/.pyenv/shims/python3"
        "$(command -v python3 2>/dev/null || true)"
        "$(command -v python 2>/dev/null || true)"
    )
    for cand in "${MAC_CANDIDATES[@]}"; do
        if [ -n "$cand" ] && [ -x "$cand" ]; then
            if "$cand" -c "import tkinter as tk; r = tk.Tk(); r.withdraw(); r.destroy()" >/dev/null 2>&1; then
                PY_BIN="$cand"
                break
            elif [ -z "$PY_BIN" ]; then
                PY_BIN="$cand"
            fi
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

PY_VER=$($PY_BIN -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "Using Python: $PY_BIN (version $PY_VER)"

# Test Tkinter GUI capability
if ! $PY_BIN -c "import tkinter as tk; r = tk.Tk(); r.withdraw(); r.destroy()" >/dev/null 2>&1; then
    echo "⚠️ Warning: Tkinter GUI could not initialize on current Python ($PY_BIN)."
    if [ "$PLATFORM" = "macOS" ]; then
        if command -v brew >/dev/null 2>&1; then
            echo "🔧 Homebrew detected. Automatically installing Tkinter support..."
            brew install python-tk || true
            if [ -x "/opt/homebrew/bin/python3" ]; then PY_BIN="/opt/homebrew/bin/python3"; fi
            if [ -x "/usr/local/bin/python3" ]; then PY_BIN="/usr/local/bin/python3"; fi
        fi
        if ! $PY_BIN -c "import tkinter as tk; r = tk.Tk(); r.withdraw(); r.destroy()" >/dev/null 2>&1; then
            echo "❌ Tkinter GUI is missing. VoicePlayer requires Tkinter to display its window."
            echo "👉 Please install official Python with Tkinter from: https://www.python.org/downloads/macos/"
            echo "   or run in terminal: brew install python-tk"
            exit 1
        fi
    else
        echo "  👉 On Ubuntu/Debian: run 'sudo apt update && sudo apt install -y python3-tk libportaudio2'"
        echo "  👉 On Fedora/RHEL: run 'sudo dnf install -y python3-tkinter portaudio'"
        echo "  👉 On Arch Linux: run 'sudo pacman -S --noconfirm tk portaudio'"
    fi
fi

INSTALL_DIR="$HOME/.voice-player"
VERSIONS_DIR="$INSTALL_DIR/versions"
mkdir -p "$VERSIONS_DIR"

REQUESTED_VER="${1:-}"
DETECTED_VER=""
if [ -n "$REQUESTED_VER" ]; then
    DETECTED_VER="$REQUESTED_VER"
else
    if command -v curl >/dev/null 2>&1; then
        DETECTED_VER=$(curl -fsSL https://api.github.com/repos/infostroper-cloud/voice-player/releases/latest 2>/dev/null | grep -m 1 '"tag_name":' | sed -E 's/.*"tag_name":\s*"v?([^"]+)".*/\1/' || true)
    fi
    if [ -z "$DETECTED_VER" ]; then
        DETECTED_VER="1.2.4"
    fi
fi

echo "Downloading VoicePlayer v$DETECTED_VER for $PLATFORM..."

TMP_DIR="/tmp/vp-install-$$"
mkdir -p "$TMP_DIR/extract"

CANDIDATE_URLS=(
    "https://github.com/infostroper-cloud/voice-player/releases/latest/download/VoicePlayer-${DETECTED_VER}-macos-linux.tar.gz"
    "https://github.com/infostroper-cloud/voice-player/releases/latest/download/voice-player-${DETECTED_VER}.zip"
    "https://github.com/infostroper-cloud/voice-player/releases/latest/download/voice-player.zip"
    "https://github.com/infostroper-cloud/voice-player/archive/refs/tags/v${DETECTED_VER}.tar.gz"
    "https://github.com/infostroper-cloud/voice-player/archive/refs/heads/main.zip"
)

DOWNLOADED_FILE=""
for url in "${CANDIDATE_URLS[@]}"; do
    tmp_dest="$TMP_DIR/download_pkg"
    if command -v curl >/dev/null 2>&1; then
        if curl -fsSL "$url" -o "$tmp_dest" 2>/dev/null && [ -s "$tmp_dest" ]; then
            DOWNLOADED_FILE="$tmp_dest"
            break
        fi
    elif command -v wget >/dev/null 2>&1; then
        if wget -qO "$tmp_dest" "$url" 2>/dev/null && [ -s "$tmp_dest" ]; then
            DOWNLOADED_FILE="$tmp_dest"
            break
        fi
    fi
done

if [ -z "$DOWNLOADED_FILE" ]; then
    echo "❌ Error: Failed to download VoicePlayer package from GitHub Releases."
    rm -rf "$TMP_DIR"
    exit 1
fi

# Extract package
if tar -tzf "$DOWNLOADED_FILE" >/dev/null 2>&1; then
    tar -xzf "$DOWNLOADED_FILE" -C "$TMP_DIR/extract"
elif $PY_BIN -m zipfile -t "$DOWNLOADED_FILE" >/dev/null 2>&1; then
    $PY_BIN -m zipfile -e "$DOWNLOADED_FILE" "$TMP_DIR/extract"
else
    echo "❌ Error: Downloaded file is neither a valid tar.gz nor zip."
    rm -rf "$TMP_DIR"
    exit 1
fi

# Locate app.py in extracted tree
APP_FOUND=$(find "$TMP_DIR/extract" -type f -name "app.py" | head -n 1)
if [ -z "$APP_FOUND" ]; then
    NESTED_ZIP=$(find "$TMP_DIR/extract" -type f -name "*.zip" | head -n 1)
    if [ -n "$NESTED_ZIP" ]; then
        $PY_BIN -m zipfile -e "$NESTED_ZIP" "$TMP_DIR/extract_nested"
        APP_FOUND=$(find "$TMP_DIR/extract_nested" -type f -name "app.py" | head -n 1)
    fi
fi

if [ -z "$APP_FOUND" ]; then
    echo "❌ Error: app.py was not found after archive extraction."
    rm -rf "$TMP_DIR"
    exit 1
fi

SRC_DIR="$(dirname "$APP_FOUND")"

if [ -f "$SRC_DIR/VERSION" ]; then
    DETECTED_VER=$(tr -d ' \r\n' < "$SRC_DIR/VERSION")
fi

TARGET_DIR="$VERSIONS_DIR/$DETECTED_VER"
rm -rf "$TARGET_DIR"
mkdir -p "$TARGET_DIR"
cp -r "$SRC_DIR"/* "$TARGET_DIR/"
rm -rf "$TMP_DIR"

# Verify app.py exists
if [ ! -f "$TARGET_DIR/app.py" ]; then
    echo "❌ Error: app.py was not found in $TARGET_DIR after extraction."
    exit 1
fi

# Ensure persistent hooks directory
if [ -d "$TARGET_DIR/hooks" ]; then
    mkdir -p "$INSTALL_DIR/hooks"
    cp -r "$TARGET_DIR/hooks"/* "$INSTALL_DIR/hooks/" 2>/dev/null || true
fi

# Create stable pointer
mkdir -p "$INSTALL_DIR"
cat << EOF > "$INSTALL_DIR/current-version.json"
{"version": "$DETECTED_VER"}
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
LATEST=\$(ls -d "\$HOME/.voice-player/versions/"[0-9]* 2>/dev/null | sort -V | tail -n 1)
RUN_DIR="\${LATEST:-\$TARGET_DIR}"
cd "\$RUN_DIR"
exec "$PY_BIN" "\$RUN_DIR/app.py" "\$@"
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
    MAC_APP="/Applications/VoicePlayer.app"
    if [ ! -w "/Applications" ]; then
        MAC_APP="$HOME/Applications/VoicePlayer.app"
        mkdir -p "$HOME/Applications"
    fi
    rm -rf "$MAC_APP"
    mkdir -p "$MAC_APP/Contents/MacOS"
    mkdir -p "$MAC_APP/Contents/Resources"

    cat << EOF > "$MAC_APP/Contents/MacOS/VoicePlayer"
#!/usr/bin/env bash
export TK_SILENCE_DEPRECATION=1
LATEST=\$(ls -d "\$HOME/.voice-player/versions/"[0-9]* 2>/dev/null | sort -V | tail -n 1)
RUN_DIR="\${LATEST:-\$TARGET_DIR}"
cd "\$RUN_DIR"
exec "$PY_BIN" "\$RUN_DIR/app.py" "\$@"
EOF
    chmod +x "$MAC_APP/Contents/MacOS/VoicePlayer"

    cat << EOF > "$MAC_APP/Contents/Info.plist"
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>VoicePlayer</string>
    <key>CFBundleDisplayName</key>
    <string>VoicePlayer</string>
    <key>CFBundleIdentifier</key>
    <string>com.voiceplayer.app</string>
    <key>CFBundleVersion</key>
    <string>$DETECTED_VER</string>
    <key>CFBundleShortVersionString</key>
    <string>$DETECTED_VER</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleExecutable</key>
    <string>VoicePlayer</string>
    <key>CFBundleIconFile</key>
    <string>icon</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>LSMinimumSystemVersion</key>
    <string>10.15</string>
</dict>
</plist>
EOF

    if [ -f "$TARGET_DIR/icon.png" ]; then
        cp "$TARGET_DIR/icon.png" "$MAC_APP/Contents/Resources/icon.png" 2>/dev/null || true
    fi

    # Также сохраняем быстрый командный файл в $HOME/Applications
    MAC_APP_DIR="$HOME/Applications"
    mkdir -p "$MAC_APP_DIR"
    MAC_LAUNCHER="$MAC_APP_DIR/VoicePlayer.command"
    cat << EOF > "$MAC_LAUNCHER"
#!/usr/bin/env bash
export TK_SILENCE_DEPRECATION=1
LATEST=\$(ls -d "\$HOME/.voice-player/versions/"[0-9]* 2>/dev/null | sort -V | tail -n 1)
RUN_DIR="\${LATEST:-\$TARGET_DIR}"
cd "\$RUN_DIR"
exec "$PY_BIN" "\$RUN_DIR/app.py" "\$@"
EOF
    chmod +x "$MAC_LAUNCHER"

    # Снимаем атрибуты карантина Gatekeeper и подписываем приложение ad-hoc:
    # больше никаких системных предупреждений ни при первом, ни при повторных запусках!
    xattr -cr "$MAC_APP" 2>/dev/null || true
    xattr -cr "$MAC_APP_DIR" 2>/dev/null || true
    xattr -cr "$INSTALL_DIR" 2>/dev/null || true
    codesign --force --deep --sign - "$MAC_APP" 2>/dev/null || true

    echo "✓ macOS native app created in $MAC_APP"
    echo "✓ macOS Gatekeeper quarantine removed & ad-hoc signature applied"
fi

# Install uninstaller
if [ -f "$TARGET_DIR/uninstall.sh" ]; then
    cp "$TARGET_DIR/uninstall.sh" "$INSTALL_DIR/uninstall.sh"
    chmod +x "$INSTALL_DIR/uninstall.sh"
    ln -sf "$INSTALL_DIR/uninstall.sh" "$HOME/.local/bin/voiceplayer-uninstall" 2>/dev/null || \
    cp "$INSTALL_DIR/uninstall.sh" "$HOME/.local/bin/voiceplayer-uninstall" 2>/dev/null || true
    chmod +x "$HOME/.local/bin/voiceplayer-uninstall" 2>/dev/null || true
fi

echo "=========================================================="
echo "🎉 VoicePlayer v$DETECTED_VER installed successfully!"
echo "Run from Applications: VoicePlayer"
echo "Or from terminal: ~/.local/bin/voiceplayer"
echo "To uninstall: ~/.local/bin/voiceplayer-uninstall"
echo "Or start now..."
echo "=========================================================="

# Clean up any stale crash lock
rm -f "$TARGET_DIR/.attempts"
pkill -f "voice-player/versions" 2>/dev/null || true

# Start in GUI session
if [ "$PLATFORM" = "macOS" ]; then
    echo "Launching VoicePlayer window on macOS..."
    export TK_SILENCE_DEPRECATION=1
    if [ -d "$MAC_APP" ]; then
        open "$MAC_APP"
    else
        nohup "$PY_BIN" "$TARGET_DIR/app.py" >> "$INSTALL_DIR/crash.log" 2>&1 &
        sleep 1
        osascript -e 'tell application "System Events" to set frontmost of first process whose name contains "python" to true' 2>/dev/null || true
    fi
elif [ -n "$DISPLAY" ]; then
    echo "Launching VoicePlayer in background..."
    nohup "$LOCAL_LAUNCHER" >> "$INSTALL_DIR/crash.log" 2>&1 &
fi
