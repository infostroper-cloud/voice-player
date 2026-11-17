# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec для VoicePlayer.
#
#   Обычная (лёгкая) сборка:   pyinstaller packaging/VoicePlayer.spec
#   Полная сборка (Pro):       set VP_FULL=1  &&  pyinstaller packaging/VoicePlayer.spec
#
# Лёгкая сборка не тащит whisper/fastembed/soundcard (сотни МБ) — приложение
# само деградирует мягко: эти кнопки покажут «установите движок».
import os
import sys
from PyInstaller.utils.hooks import collect_all

HERE = SPECPATH                              # каталог этого .spec (=packaging/)
ROOT = os.path.dirname(HERE)                 # корень проекта (=voice-player/)
APP  = os.path.join(ROOT, "app.py")
ICON_ICO = os.path.join(ROOT, "icon.ico")
ICON_ICNS = os.path.join(ROOT, "VoicePlayer.icns")
ICON = ICON_ICNS if (sys.platform == "darwin" and os.path.exists(ICON_ICNS)) else ICON_ICO
FULL = os.environ.get("VP_FULL") == "1"

VERSION_FILE = os.path.join(ROOT, "VERSION")
APP_VER = "1.2.24"
if os.path.exists(VERSION_FILE):
    with open(VERSION_FILE, "r", encoding="utf-8") as _vf:
        APP_VER = _vf.read().strip() or APP_VER

datas, binaries, hiddenimports = [], [], ["edge_tts", "aiohttp", "certifi", "vp_feed"]
if os.path.exists(VERSION_FILE):
    datas.append((VERSION_FILE, "."))
if os.path.exists(ICON_ICO):
    datas.append((ICON_ICO, "."))
ICON_PNG = os.path.join(ROOT, "icon.png")
if os.path.exists(ICON_PNG):
    datas.append((ICON_PNG, "."))
HOOKS_DIR = os.path.join(ROOT, "hooks")
if os.path.exists(HOOKS_DIR):
    datas.append((HOOKS_DIR, "hooks"))

# пакеты с бинарниками/данными, которые PyInstaller не находит сам
core_pkgs = ["edge_tts", "sounddevice", "miniaudio", "audiotsm", "certifi", "aiohttp"]
full_pkgs = ["faster_whisper", "fastembed", "soundcard"]
for pkg in core_pkgs + (full_pkgs if FULL else []):
    try:
        d, b, h = collect_all(pkg)
        datas += d; binaries += b; hiddenimports += h
    except Exception:
        pass

excludes = ["matplotlib", "tkinter.test", "test", "pytest", "PyInstaller"]
if not FULL:
    excludes += ["faster_whisper", "ctranslate2", "fastembed", "onnxruntime",
                 "onnxruntime_gpu", "torch", "transformers", "scipy", "soundcard"]

a = Analysis(
    [APP],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="VoicePlayer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,                # оконное приложение, без чёрной консоли
    disable_windowed_traceback=False,
    icon=ICON if os.path.exists(ICON) else None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="VoicePlayer",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="VoicePlayer.app",
        icon=ICON if os.path.exists(ICON) else None,
        bundle_identifier="com.voiceplayer.app",
        info_plist={
            "CFBundleName": "VoicePlayer",
            "CFBundleDisplayName": "VoicePlayer",
            "CFBundleVersion": APP_VER,
            "CFBundleShortVersionString": APP_VER,
            "NSHighResolutionCapable": "True",
            "LSMinimumSystemVersion": "11.0",
        },
    )
