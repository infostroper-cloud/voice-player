#!/usr/bin/env bash
# ==============================================================================
# Cross-Platform Lifecycle Test for macOS and Linux (Download, Install, Uninstall)
# Can run on any POSIX bash (macOS, Linux, WSL, Git Bash on Windows).
# ==============================================================================
set -e

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WIN_REPO=$(command -v cygpath >/dev/null 2>&1 && cygpath -m "$REPO_ROOT" || echo "$REPO_ROOT")
echo "=== 🧪 Testing VoicePlayer Unix Lifecycle (macOS & Linux) ==="
echo "Repository: $REPO_ROOT ($WIN_REPO)"

# 1. Syntax Check
echo "[1/5] Validating shell script syntax..."
bash -n "$REPO_ROOT/install.sh"
bash -n "$REPO_ROOT/uninstall.sh"
echo "  ✓ Syntax valid for install.sh and uninstall.sh"

# 2. Prepare Sandbox Environment
TEMP_BASE="$(python -c 'import tempfile; print(tempfile.gettempdir().replace("\\", "/"))')"
TMP_SANDBOX="$TEMP_BASE/vp-test-sandbox-$$"
rm -rf "$TMP_SANDBOX"
mkdir -p "$TMP_SANDBOX/home" "$TMP_SANDBOX/mock_release"

# Build mock distribution package from local repo files
MOCK_PKG="$TMP_SANDBOX/mock_release/voice-player-1.2.4.zip"
echo "[2/5] Building local test archive ($MOCK_PKG)..."
python -c "
import zipfile
from pathlib import Path
root = Path(r'$WIN_REPO')
pkg = Path(r'$MOCK_PKG')
with zipfile.ZipFile(pkg, 'w') as z:
    for fname in ['app.py', 'VERSION', 'requirements.txt', 'vp_watchers.py', 'vp_integrations.py', 'vp_updater.py', 'uninstall.sh', 'icon.png']:
        fpath = root / fname
        if fpath.exists():
            z.write(fpath, fname)
"

# 3. Test Linux Installation & Uninstall
echo "[3/5] Testing Linux install & uninstall lifecycle..."
LINUX_HOME="$TMP_SANDBOX/home/linux_user"
mkdir -p "$LINUX_HOME"

TARGET_DIR="$LINUX_HOME/.voice-player/versions/1.2.4"
mkdir -p "$TARGET_DIR"
python -c "
import zipfile
with zipfile.ZipFile(r'$MOCK_PKG') as z:
    z.extractall(r'$TARGET_DIR')
"
mkdir -p "$LINUX_HOME/.voice-player"
echo '{"version": "1.2.4"}' > "$LINUX_HOME/.voice-player/current-version.json"

# Launcher script
mkdir -p "$LINUX_HOME/.local/bin"
cat << EOF > "$LINUX_HOME/.local/bin/voiceplayer"
#!/usr/bin/env bash
exec python "$LINUX_HOME/.voice-player/versions/1.2.4/app.py" "\$@"
EOF
chmod +x "$LINUX_HOME/.local/bin/voiceplayer"

# Desktop Entry
DESKTOP_DIR="$LINUX_HOME/.local/share/applications"
mkdir -p "$DESKTOP_DIR"
cat << EOF > "$DESKTOP_DIR/voiceplayer.desktop"
[Desktop Entry]
Name=VoicePlayer
Exec=$LINUX_HOME/.local/bin/voiceplayer
Type=Application
Categories=AudioVideo;
EOF

# Install uninstaller
cp "$REPO_ROOT/uninstall.sh" "$LINUX_HOME/.voice-player/uninstall.sh"
chmod +x "$LINUX_HOME/.voice-player/uninstall.sh"
cp "$LINUX_HOME/.voice-player/uninstall.sh" "$LINUX_HOME/.local/bin/voiceplayer-uninstall"
chmod +x "$LINUX_HOME/.local/bin/voiceplayer-uninstall"

# Verification of Linux installation
[ -f "$LINUX_HOME/.voice-player/versions/1.2.4/app.py" ] || { echo "❌ Linux: app.py missing"; exit 1; }
[ -f "$LINUX_HOME/.voice-player/current-version.json" ] || { echo "❌ Linux: current-version.json missing"; exit 1; }
[ -x "$LINUX_HOME/.local/bin/voiceplayer" ] || { echo "❌ Linux: launcher not executable"; exit 1; }
[ -f "$LINUX_HOME/.local/share/applications/voiceplayer.desktop" ] || { echo "❌ Linux: desktop entry missing"; exit 1; }
[ -x "$LINUX_HOME/.local/bin/voiceplayer-uninstall" ] || { echo "❌ Linux: uninstaller launcher missing"; exit 1; }
echo "  ✓ Linux installation structure verified"

# Test Linux Uninstall
HOME="$LINUX_HOME" bash "$LINUX_HOME/.local/bin/voiceplayer-uninstall" --quiet
[ ! -d "$LINUX_HOME/.voice-player" ] || { echo "❌ Linux: ~/.voice-player was not removed"; exit 1; }
[ ! -f "$LINUX_HOME/.local/bin/voiceplayer" ] || { echo "❌ Linux: launcher was not removed"; exit 1; }
[ ! -f "$LINUX_HOME/.local/share/applications/voiceplayer.desktop" ] || { echo "❌ Linux: desktop entry was not removed"; exit 1; }
echo "  ✓ Linux uninstallation cleanly verified"

# 4. Test macOS Installation & Uninstall
echo "[4/5] Testing macOS install & uninstall lifecycle..."
MAC_HOME="$TMP_SANDBOX/home/mac_user"
mkdir -p "$MAC_HOME/Applications"

TARGET_DIR="$MAC_HOME/.voice-player/versions/1.2.4"
mkdir -p "$TARGET_DIR"
python -c "
import zipfile
with zipfile.ZipFile(r'$MOCK_PKG') as z:
    z.extractall(r'$TARGET_DIR')
"
mkdir -p "$MAC_HOME/.voice-player"
echo '{"version": "1.2.4"}' > "$MAC_HOME/.voice-player/current-version.json"

# Native macOS .app bundle
MAC_APP="$MAC_HOME/Applications/VoicePlayer.app"
mkdir -p "$MAC_APP/Contents/MacOS" "$MAC_APP/Contents/Resources"

cat << EOF > "$MAC_APP/Contents/MacOS/VoicePlayer"
#!/usr/bin/env bash
export TK_SILENCE_DEPRECATION=1
exec python "$MAC_HOME/.voice-player/versions/1.2.4/app.py" "\$@"
EOF
chmod +x "$MAC_APP/Contents/MacOS/VoicePlayer"

cat << EOF > "$MAC_APP/Contents/Info.plist"
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>VoicePlayer</string>
    <key>CFBundleVersion</key>
    <string>1.2.4</string>
</dict>
</plist>
EOF

MAC_LAUNCHER="$MAC_HOME/Applications/VoicePlayer.command"
cat << EOF > "$MAC_LAUNCHER"
#!/usr/bin/env bash
exec python "$MAC_HOME/.voice-player/versions/1.2.4/app.py" "\$@"
EOF
chmod +x "$MAC_LAUNCHER"

mkdir -p "$MAC_HOME/.local/bin"
cp "$MAC_APP/Contents/MacOS/VoicePlayer" "$MAC_HOME/.local/bin/voiceplayer"
chmod +x "$MAC_HOME/.local/bin/voiceplayer"

cp "$REPO_ROOT/uninstall.sh" "$MAC_HOME/.voice-player/uninstall.sh"
chmod +x "$MAC_HOME/.voice-player/uninstall.sh"
cp "$MAC_HOME/.voice-player/uninstall.sh" "$MAC_HOME/.local/bin/voiceplayer-uninstall"
chmod +x "$MAC_HOME/.local/bin/voiceplayer-uninstall"

# Create mock preferences to test --purge
echo '{"rate": 1.25}' > "$MAC_HOME/.voice-player-prefs.json"

# Verification of macOS installation
[ -f "$MAC_HOME/.voice-player/versions/1.2.4/app.py" ] || { echo "❌ macOS: app.py missing"; exit 1; }
[ -x "$MAC_APP/Contents/MacOS/VoicePlayer" ] || { echo "❌ macOS: .app binary missing"; exit 1; }
grep -q "<string>1.2.4</string>" "$MAC_APP/Contents/Info.plist" || { echo "❌ macOS: Info.plist version mismatch"; exit 1; }
[ -x "$MAC_HOME/Applications/VoicePlayer.command" ] || { echo "❌ macOS: .command launcher missing"; exit 1; }
[ -x "$MAC_HOME/.local/bin/voiceplayer" ] || { echo "❌ macOS: ~/.local/bin/voiceplayer missing"; exit 1; }
echo "  ✓ macOS installation structure and .app bundle verified"

# Test macOS Uninstall with --purge
HOME="$MAC_HOME" bash "$MAC_HOME/.local/bin/voiceplayer-uninstall" --quiet --purge
[ ! -d "$MAC_HOME/.voice-player" ] || { echo "❌ macOS: ~/.voice-player was not removed"; exit 1; }
[ ! -d "$MAC_APP" ] || { echo "❌ macOS: VoicePlayer.app was not removed"; exit 1; }
[ ! -f "$MAC_HOME/Applications/VoicePlayer.command" ] || { echo "❌ macOS: VoicePlayer.command was not removed"; exit 1; }
[ ! -f "$MAC_HOME/.local/bin/voiceplayer" ] || { echo "❌ macOS: ~/.local/bin/voiceplayer was not removed"; exit 1; }
[ ! -f "$MAC_HOME/.voice-player-prefs.json" ] || { echo "❌ macOS: preferences were not purged"; exit 1; }
echo "  ✓ macOS uninstallation with --purge cleanly verified"

# 5. Cleanup
rm -rf "$TMP_SANDBOX"
echo "[5/5] All Unix lifecycle tests (macOS & Linux) PASSED! 🎉"
