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


# path — файл настроек от домашней папки; event — событие «ответ готов».
HOOK_TOOLS = {
    "antigravity": {"label": "Antigravity", "home": ".gemini/antigravity",
                    "path": ".gemini/config/hooks.json",
                    "event": "Stop", "entry": _antigravity_entry, "named_root": "voice-player"},
    "codex":       {"label": "Codex (App + CLI)", "home": ".codex", "path": ".codex/hooks.json",
                    "event": "Stop", "entry": _claude_entry},
    "claude-code": {"label": "Claude Code", "home": ".claude", "path": ".claude/settings.json",
                    "event": "Stop", "entry": _claude_entry},
    "cursor":      {"label": "Cursor", "home": ".cursor", "path": ".cursor/hooks.json",
                    "event": "afterAgentResponse", "entry": _cursor_entry},
    "gemini":      {"label": "Gemini CLI", "home": ".gemini", "path": ".gemini/settings.json",
                    "event": "AfterAgent", "entry": _gemini_entry},
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
    tool = HOOK_TOOLS[tool_id]
    try:
        cfg = _read_config((home or Path.home()) / tool["path"])
    except (OSError, ValueError):
        return False
    named = tool.get("named_root")
    if named:
        block = cfg.get(named)
        if not isinstance(block, dict) or block.get("enabled") is False:
            return False
        return any(_has_marker(e) for e in block.get(tool["event"]) or [])
    return any(_has_marker(e) for e in (cfg.get("hooks") or {}).get(tool["event"]) or [])


def _is_installed(tool_id: str, home: Path) -> bool:
    t = HOOK_TOOLS[tool_id]
    if (home / t["home"]).exists():
        return True
    if tool_id == "antigravity":
        return any((home / ".gemini" / p).exists()
                   for p in ("antigravity", "antigravity-ide", "antigravity-cli"))
    return False


def detect(home: Path = None) -> list:
    """Что установлено на компьютере. Определяем по папке настроек инструмента."""
    home = home or Path.home()
    return [{"id": tid, "label": t["label"], "installed": _is_installed(tid, home),
             "connected": is_connected(tid, home)} for tid, t in HOOK_TOOLS.items()]


def connect(tool_id: str, app_dir: Path, home: Path = None) -> None:
    tool = HOOK_TOOLS[tool_id]
    path = (home or Path.home()) / tool["path"]
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
    tool = HOOK_TOOLS[tool_id]
    path = (home or Path.home()) / tool["path"]
    cfg = _read_config(path)
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
