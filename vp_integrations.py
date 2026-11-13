"""Поиск установленных нейросетей и подключение их к плееру через штатные хуки и наблюдатели.

Плеер только находит инструмент сам; чужие файлы настроек меняем лишь по нажатию кнопки
и с резервной копией рядом. Отключение убирает только нашу запись.
"""
from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

FEED_MARKERS = ("vp_feed", "speak-response", "--feed", "voiceplayer", "voice-player")


def _feed_command(tool: str, app_dir: Path) -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --feed {tool}'
    py = Path(sys.executable)
    if py.name.lower() == "pythonw.exe":     # у pythonw нет вывода, а хукам нужен ответ в stdout
        py = py.with_name("python.exe")
    feed_script = app_dir / "hooks" / "vp_feed.py"
    if not feed_script.exists():
        fallback = Path.home() / ".voice-player" / "hooks" / "vp_feed.py"
        if fallback.exists():
            feed_script = fallback
    return f'"{py}" "{feed_script}" {tool}'


def _claude_entry(command: str) -> dict:
    return {"hooks": [{"type": "command", "command": command, "timeout": 10}]}


def _cursor_entry(command: str) -> dict:
    return {"command": command, "timeout": 10}


def _gemini_entry(command: str) -> dict:
    return {"hooks": [{"type": "command", "command": command, "timeout": 10000}]}


def _antigravity_entry(command: str) -> dict:
    return {"type": "command", "command": command, "timeout": 10}


def _generic_entry(command: str) -> dict:
    return {"command": command, "type": "command", "timeout": 15}


# Полный реестр поддерживаемых нейросетей, CLI-ассистентов и редакторов (23 инструмента)
HOOK_TOOLS = {
    # 1. Antigravity
    "antigravity": {
        "label": "Google Antigravity",
        "icon": "🌌",
        "category": "agents",
        "home": ".gemini/antigravity",
        "path": ".gemini/config/hooks.json",
        "event": "Stop",
        "entry": _antigravity_entry,
        "named_root": "voice-player",
        "desc": "Google Antigravity IDE, App и CLI",
        "setup_tip": "https://antigravity.google",
    },
    # 2. Codex
    "codex": {
        "label": "Codex CLI & App",
        "icon": "⚡",
        "category": "agents",
        "home": ".codex",
        "path": ".codex/hooks.json",
        "event": "Stop",
        "entry": _claude_entry,
        "desc": "OpenAI Codex CLI и десктопное приложение",
        "setup_tip": "npm i -g @openai/codex",
    },
    # 3. Claude Code
    "claude-code": {
        "label": "Claude Code",
        "icon": "🤖",
        "category": "agents",
        "home": ".claude",
        "path": ".claude/settings.json",
        "event": "Stop",
        "entry": _claude_entry,
        "desc": "Anthropic Claude Code CLI агент",
        "setup_tip": "npm i -g @anthropic-ai/claude-code",
    },
    # 4. Cursor
    "cursor": {
        "label": "Cursor",
        "icon": "🎯",
        "category": "ide",
        "home": ".cursor",
        "path": ".cursor/hooks.json",
        "event": "afterAgentResponse",
        "entry": _cursor_entry,
        "desc": "Cursor AI IDE (Agent & Composer)",
        "setup_tip": "https://cursor.com",
    },
    # 5. Gemini CLI
    "gemini": {
        "label": "Gemini CLI",
        "icon": "💎",
        "category": "cli",
        "home": ".gemini",
        "path": ".gemini/settings.json",
        "event": "AfterAgent",
        "entry": _gemini_entry,
        "desc": "Google Gemini CLI инструмент",
        "setup_tip": "pip install gemini-cli",
    },
    # 6. OpenClaw
    "openclaw": {
        "label": "OpenClaw",
        "icon": "🦞",
        "category": "agents",
        "home": ".openclaw",
        "path": ".openclaw/config.json",
        "event": "onResponse",
        "entry": _generic_entry,
        "desc": "OpenClaw автономный агент разработки",
        "setup_tip": "npm i -g openclaw",
    },
    # 7. OpenCode
    "opencode": {
        "label": "OpenCode",
        "icon": "💻",
        "category": "agents",
        "home": ".opencode",
        "path": ".opencode/config.json",
        "event": "afterResponse",
        "entry": _generic_entry,
        "desc": "OpenCode AI coding assistant",
        "setup_tip": "https://github.com/opencode-ai",
    },
    # 8. Hermes Agent
    "hermes-agent": {
        "label": "Hermes Agent",
        "icon": "🕊️",
        "category": "agents",
        "home": ".hermes",
        "path": ".hermes/config.json",
        "event": "afterStep",
        "entry": _generic_entry,
        "desc": "Nous Hermes автономный агент",
        "setup_tip": "pip install hermes-agent",
    },
    # 9. Hermes Desktop
    "hermes-desktop": {
        "label": "Hermes Desktop",
        "icon": "🖥️",
        "category": "agents",
        "home": ".hermes-desktop",
        "path": ".hermes-desktop/settings.json",
        "event": "onMessage",
        "entry": _generic_entry,
        "desc": "Настольное приложение Hermes Desktop",
        "setup_tip": "https://hermes.nousresearch.com",
    },
    # 10. Droid
    "droid": {
        "label": "Droid",
        "icon": "🤖",
        "category": "agents",
        "home": ".droid",
        "path": ".droid/config.json",
        "event": "afterAgentResponse",
        "entry": _generic_entry,
        "desc": "Factory Droid AI агент",
        "setup_tip": "https://droid.factory.ai",
    },
    # 11. Pi
    "pi": {
        "label": "Pi",
        "icon": "🥧",
        "category": "cli",
        "home": ".pi",
        "path": ".pi/config.json",
        "event": "onResponse",
        "entry": _generic_entry,
        "desc": "Inflection Pi CLI ассистент",
        "setup_tip": "pip install pi-cli",
    },
    # 12. Cline
    "cline": {
        "label": "Cline",
        "icon": "🦾",
        "category": "agents",
        "home": ".cline",
        "path": ".cline/config.json",
        "event": "taskCompleted",
        "entry": _generic_entry,
        "desc": "Cline автономный агент в VS Code / CLI",
        "setup_tip": "VS Code Extension: saoudrizwan.claude-dev",
    },
    # 13. Copilot CLI
    "copilot-cli": {
        "label": "Copilot CLI",
        "icon": "🐙",
        "category": "cli",
        "home": ".copilot-cli",
        "path": ".copilot-cli/config.json",
        "event": "onCommand",
        "entry": _generic_entry,
        "desc": "GitHub Copilot CLI (gh copilot)",
        "setup_tip": "gh extension install github/gh-copilot",
    },
    # 14. Oh My Pi
    "oh-my-pi": {
        "label": "Oh My Pi",
        "icon": "🥧",
        "category": "cli",
        "home": ".oh-my-pi",
        "path": ".oh-my-pi/config.json",
        "event": "afterResponse",
        "entry": _generic_entry,
        "desc": "Oh My Pi расширенная среда",
        "setup_tip": "https://github.com/oh-my-pi",
    },
    # 15. DeepSeek Harness
    "deepseek": {
        "label": "DeepSeek Harness",
        "icon": "🐋",
        "category": "agents",
        "home": "AppData/Local/Programs/DeepSeek Harness",
        "path": "AppData/Roaming/@deepseek-ai/dsh-desktop/config.json",
        "is_gui_app": True,
        "desc": "DeepSeek десктоп-приложение (озвучка: буфер 📋 или Ctrl+Shift+S)",
        "setup_tip": "https://deepseek.com",
    },
    # 16. Qwen Code
    "qwen-code": {
        "label": "Qwen Code",
        "icon": "💠",
        "category": "agents",
        "home": ".qwen",
        "path": ".qwen/config.json",
        "event": "afterResponse",
        "entry": _generic_entry,
        "desc": "Alibaba Qwen Code агент и CLI",
        "setup_tip": "pip install qwen-code",
    },
    # 17. Ollama
    "ollama": {
        "label": "Ollama",
        "icon": "🦙",
        "category": "local_llm",
        "home": ".ollama",
        "path": ".ollama/config.json",
        "is_gui_app": True,
        "desc": "Локальные модели (терминал: ollama run | vp-say, либо авто-буфер 📋)",
        "setup_tip": "https://ollama.com",
    },
    # 18. Perplexity
    "perplexity": {
        "label": "Perplexity",
        "icon": "🔍",
        "category": "chat",
        "home": ".perplexity",
        "path": ".perplexity/settings.json",
        "is_gui_app": True,
        "desc": "Perplexity AI поиск (озвучка: буфер 📋 или Ctrl+Shift+S)",
        "setup_tip": "https://perplexity.ai",
    },
    # 19. Comet
    "comet": {
        "label": "Comet (Perplexity)",
        "icon": "☄️",
        "category": "chat",
        "home": "AppData/Local/Perplexity/Comet",
        "path": "AppData/Local/Perplexity/Comet/settings.json",
        "is_gui_app": True,
        "desc": "AI-браузер Comet от Perplexity (озвучка: буфер 📋 или Ctrl+Shift+S)",
        "setup_tip": "https://perplexity.ai/comet",
    },
    # 20. VS Code
    "vscode": {
        "label": "VS Code",
        "icon": "📝",
        "category": "ide",
        "home": ".vscode",
        "path": ".vscode/voice-player.json",
        "event": "afterChat",
        "entry": _generic_entry,
        "desc": "Visual Studio Code (Copilot Chat, Continue, Roo Code)",
        "setup_tip": "https://code.visualstudio.com",
    },
    # 21. Aider
    "aider": {
        "label": "Aider",
        "icon": "🤝",
        "category": "agents",
        "home": ".aider",
        "path": ".aider/voice-player.json",
        "event": "afterAgent",
        "entry": _generic_entry,
        "desc": "Aider консольный парный AI-программист",
        "setup_tip": "pip install aider-chat",
    },
    # 22. Windsurf
    "windsurf": {
        "label": "Windsurf",
        "icon": "🏄",
        "category": "ide",
        "home": ".codeium",
        "path": ".codeium/voice-player.json",
        "event": "afterCascade",
        "entry": _generic_entry,
        "desc": "Windsurf IDE (Codeium Cascade AI)",
        "setup_tip": "https://codeium.com/windsurf",
    },
    # Kimi Desktop
    "kimi": {
        "label": "Kimi (Moonshot AI)",
        "icon": "🌙",
        "category": "chat",
        "home": "AppData/Local/Programs/kimi-desktop",
        "path": "AppData/Local/Programs/kimi-desktop/Kimi.exe",
        "is_gui_app": True,
        "desc": "Kimi AI десктоп-чат (озвучка: кнопка 📋 или Ctrl+Shift+S)",
        "setup_tip": "https://kimi.moonshot.cn",
    },
    # 23. Terminal
    "terminal": {
        "label": "Terminal",
        "icon": "📟",
        "category": "terminal",
        "home": ".voice-player/terminal",
        "path": ".voice-player/terminal/config.json",
        "event": "onOutput",
        "entry": _generic_entry,
        "desc": "Универсальный терминал: пайп, логи и vp-say",
        "setup_tip": "команда | vp-say",
    },
}

SELECTION_NOTE = ("Чаты в браузере и приложения (ChatGPT, Claude, Perplexity) хуков не дают: "
                  "выдели ответ и нажми Ctrl+Alt+S.")


def _read_config(path: Path) -> dict:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8") or "{}")
    if not isinstance(data, dict):
        raise ValueError(f"{path.name}: ожидался JSON-объект")
    return data


def _write_config(path: Path, data: dict) -> None:
    if path.exists():
        shutil.copy2(path, path.with_name(f"{path.name}.bak-voiceplayer-{time.strftime('%Y%m%d-%H%M%S')}"))
    tmp = path.with_name(path.name + ".tmp-voiceplayer")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _has_marker(entry) -> bool:
    dumped = json.dumps(entry, ensure_ascii=False).lower()
    return any(m.lower() in dumped for m in FEED_MARKERS)


def is_connected(tool_id: str, home: Path = None) -> bool:
    tool = HOOK_TOOLS.get(tool_id)
    if not tool:
        return False
    if tool.get("is_gui_app"):
        return _is_installed(tool_id, home or Path.home())
    target_home = home or Path.home()
    if tool_id == "terminal":
        log_file = target_home / ".voice-player" / "terminal.log"
        cfg_file = target_home / tool["path"]
        return log_file.exists() or cfg_file.exists()
    try:
        cfg = _read_config(target_home / tool["path"])
    except (OSError, ValueError):
        return False
    named = tool.get("named_root")
    if named:
        block = cfg.get(named)
        if not isinstance(block, dict) or block.get("enabled") is False:
            return False
        return any(_has_marker(e) for e in block.get(tool.get("event", "onMessage")) or [])
    event = tool.get("event")
    if not event:
        return False
    return any(_has_marker(e) for e in (cfg.get("hooks") or {}).get(event) or [])


def _is_installed(tool_id: str, home: Path) -> bool:
    t = HOOK_TOOLS.get(tool_id)
    if not t:
        return False
    if (home / t["home"]).exists():
        return True
    if tool_id == "antigravity":
        if any((home / ".gemini" / p).exists()
               for p in ("antigravity", "antigravity-ide", "antigravity-cli")):
            return True

    # Для изолированных модульных тестов с тестовым каталогом — проверяем только его
    is_real_user_home = (home == Path.home())
    if not is_real_user_home:
        return False

    # Системное обнаружение на реальной машине пользователя
    if tool_id == "terminal":
        return True
    if tool_id == "antigravity":
        return bool(shutil.which("agy")) or bool(shutil.which("antigravity"))
    if tool_id == "codex":
        return bool(shutil.which("codex"))
    if tool_id == "claude-code":
        return bool(shutil.which("claude"))
    if tool_id == "cursor":
        return ((home / "AppData/Roaming/Cursor").exists()
                or bool(shutil.which("cursor"))
                or Path("/Applications/Cursor.app").exists())
    if tool_id == "gemini":
        return bool(shutil.which("gemini"))
    if tool_id == "cline":
        cline_paths = [
            home / "AppData/Roaming/Code/User/globalStorage/saoudrizwan.claude-dev",
            home / ".config/Code/User/globalStorage/saoudrizwan.claude-dev",
            home / "Library/Application Support/Code/User/globalStorage/saoudrizwan.claude-dev",
        ]
        return any(p.exists() for p in cline_paths) or bool(shutil.which("cline"))
    if tool_id == "copilot-cli":
        return (bool(shutil.which("copilot"))
                or bool(shutil.which("github-copilot-cli"))
                or (home / ".config/gh/extensions/gh-copilot").exists())
    if tool_id == "ollama":
        return ((home / "AppData/Local/Programs/Ollama").exists()
                or bool(shutil.which("ollama")))
    if tool_id == "vscode":
        vscode_paths = [
            home / "AppData/Roaming/Code",
            home / ".config/Code",
            home / "Library/Application Support/Code",
            Path("/Applications/Visual Studio Code.app"),
        ]
        return any(p.exists() for p in vscode_paths) or bool(shutil.which("code"))
    if tool_id == "perplexity":
        perp_paths = [
            home / "AppData/Local/Perplexity",
            home / "AppData/Roaming/Perplexity",
            home / "AppData/Local/Programs/Perplexity",
            home / ".config/perplexity",
            Path("/Applications/Perplexity.app"),
        ]
        return any(p.exists() for p in perp_paths) or bool(shutil.which("perplexity"))
    if tool_id == "comet":
        comet_paths = [
            home / "AppData/Local/Perplexity/Comet",
            home / "AppData/Local/Perplexity/Comet/Application/comet.exe",
            home / "AppData/Roaming/Comet",
            Path("/Applications/Comet.app"),
        ]
        return any(p.exists() for p in comet_paths) or bool(shutil.which("comet"))
    if tool_id == "kimi":
        kimi_paths = [
            home / "AppData/Local/Programs/kimi-desktop/Kimi.exe",
            home / "AppData/Local/Programs/kimi-desktop",
            home / "AppData/Roaming/Kimi",
            Path("/Applications/Kimi.app"),
        ]
        return any(p.exists() for p in kimi_paths) or bool(shutil.which("kimi"))
    if tool_id == "deepseek":
        dsh_paths = [
            home / "AppData/Local/Programs/DeepSeek Harness",
            home / "AppData/Local/Programs/DeepSeek Harness/DeepSeek Harness.exe",
            home / "AppData/Roaming/@deepseek-ai",
            home / "AppData/Local/@deepseek-aidsh-desktop-updater",
            home / ".deepseek",
            Path("/Applications/DeepSeek Harness.app"),
            Path("/Applications/DeepSeek.app"),
        ]
        return any(p.exists() for p in dsh_paths) or bool(shutil.which("deepseek")) or bool(shutil.which("deepseek-harness"))
    if tool_id == "hermes-desktop":
        hermes_paths = [
            home / "AppData/Local/Programs/Hermes",
            Path("/Applications/Hermes.app"),
        ]
        return any(p.exists() for p in hermes_paths) or bool(shutil.which("hermes-desktop"))
    if tool_id == "aider":
        return (home / ".aider.chat.history.md").exists() or bool(shutil.which("aider"))
    if tool_id == "windsurf":
        wind_paths = [
            home / "AppData/Roaming/Windsurf",
            Path("/Applications/Windsurf.app"),
        ]
        return any(p.exists() for p in wind_paths) or bool(shutil.which("windsurf"))

    bin_name = tool_id.replace("-code", "").replace("-agent", "").replace("-cli", "")
    return bool(shutil.which(bin_name))


def detect(home: Path = None) -> list:
    """Что установлено на компьютере. Определяем по папке настроек инструмента."""
    home = home or Path.home()
    return [{
        "id": tid,
        "label": t["label"],
        "icon": t.get("icon", "🤖"),
        "category": t.get("category", "agents"),
        "desc": t.get("desc", ""),
        "setup_tip": t.get("setup_tip", ""),
        "is_gui_app": bool(t.get("is_gui_app", False)),
        "installed": _is_installed(tid, home),
        "connected": is_connected(tid, home)
    } for tid, t in HOOK_TOOLS.items()]


def connect(tool_id: str, app_dir: Path, home: Path = None) -> None:
    tool = HOOK_TOOLS[tool_id]
    target_home = home or Path.home()
    path = target_home / tool["path"]

    if tool_id == "terminal":
        bin_dir = target_home / ".voice-player" / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        term_log = target_home / ".voice-player" / "terminal.log"
        if not term_log.exists():
            term_log.write_text("# VoicePlayer Terminal Feed\n", encoding="utf-8")
        feed_script = app_dir / "hooks" / "vp_feed.py"
        bat_file = bin_dir / "vp-say.bat"
        sh_file = bin_dir / "vp-say"
        if getattr(sys, "frozen", False):
            exe = sys.executable
            bat_file.write_text(f'@echo off\n"{exe}" --feed terminal %*\n', encoding="utf-8")
            sh_file.write_text(f'#!/bin/sh\n"{exe}" --feed terminal "$@"\n', encoding="utf-8")
        else:
            py = Path(sys.executable)
            if py.name.lower() == "pythonw.exe":
                py = py.with_name("python.exe")
            bat_file.write_text(f'@echo off\n"{py}" "{feed_script}" terminal %*\n', encoding="utf-8")
            sh_file.write_text(f'#!/bin/sh\n"{py}" "{feed_script}" terminal "$@"\n', encoding="utf-8")
        try:
            sh_file.chmod(0o755)
        except Exception:
            pass

    cfg = _read_config(path)                 # битый файл — исключение, ничего не перезаписываем
    named = tool.get("named_root")
    if named:
        block = cfg.setdefault(named, {})
        block.pop("enabled", None)
        entries = block.setdefault(tool["event"], [])
        cleaned = [e for e in entries if not _has_marker(e)]
        cleaned.append(tool["entry"](_feed_command(tool_id, app_dir)))
        block[tool["event"]] = cleaned
    else:
        hooks = cfg.setdefault("hooks", {})
        if tool_id == "cursor":
            cfg.setdefault("version", 1)
        entries = hooks.setdefault(tool["event"], [])
        cleaned = [e for e in entries if not _has_marker(e)]
        cleaned.append(tool["entry"](_feed_command(tool_id, app_dir)))
        hooks[tool["event"]] = cleaned
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_config(path, cfg)


def disconnect(tool_id: str, home: Path = None) -> None:
    tool = HOOK_TOOLS.get(tool_id)
    if not tool:
        return
    target_home = home or Path.home()
    path = target_home / tool["path"]
    if tool_id == "terminal":
        term_log = target_home / ".voice-player" / "terminal.log"
        if term_log.exists():
            try:
                term_log.unlink()
            except OSError:
                pass
    try:
        cfg = _read_config(path)
    except (OSError, ValueError):
        return
    named = tool.get("named_root")
    if named:
        block = cfg.get(named)
        if not isinstance(block, dict):
            return
        entries = block.get(tool["event"]) or []
        kept = [e for e in entries if not _has_marker(e)]
        if len(kept) == len(entries):
            return
        if kept:
            block[tool["event"]] = kept
        else:
            block.pop(tool["event"], None)
        if not block:
            cfg.pop(named, None)
        _write_config(path, cfg)
        return
    hooks = cfg.get("hooks") or {}
    entries = hooks.get(tool["event"]) or []
    kept = [e for e in entries if not _has_marker(e)]
    if len(kept) == len(entries):
        return
    if kept:
        hooks[tool["event"]] = kept
    else:
        hooks.pop(tool["event"], None)
    _write_config(path, cfg)
