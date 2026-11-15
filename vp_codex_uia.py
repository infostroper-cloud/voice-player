"""Чтение текста облачных тредов Codex Desktop через Windows UI Automation.

Используется только на Windows и только по триггеру из ~/.codex/sqlite/codex.db
(когда меняется source_updated_at у chatgpt:-треда), без постоянного опроса окна.
"""
from __future__ import annotations

import importlib.util
import re
import sys
import time
from collections import deque

_CHROME_IGNORE = {
    "codex", "copy", "edit", "share", "regenerate", "new chat", "new thread",
    "копировать", "изменить", "поделиться", "новый чат", "thinking", "думаю",
    "stopped", "send", "stop", "отправить", "остановить",
}
_TIME_RE = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?$")


def is_available() -> bool:
    return sys.platform == "win32" and importlib.util.find_spec("uiautomation") is not None


def _clean_blocks(raw_text: str) -> list[str]:
    blocks = []
    cur = []
    for line in (raw_text or "").splitlines():
        s = " ".join(line.split()).strip()
        if not s:
            if cur:
                blocks.append("\n".join(cur))
                cur = []
            continue
        low = s.lower()
        if low in _CHROME_IGNORE or _TIME_RE.match(s):
            continue
        cur.append(s)
    if cur:
        blocks.append("\n".join(cur))
    return [b for b in blocks if len(b) >= 20 and any(ch.isalpha() for ch in b)]


def _find_codex_window(auto, thread_title: str = ""):
    root = auto.GetRootControl()
    title_low = (thread_title or "").strip().lower()
    fallback = None
    for win in root.GetChildren():
        try:
            if getattr(win, "ClassName", "") != "Chrome_WidgetWin_1":
                continue
            name = (getattr(win, "Name", "") or "").strip()
            low = name.lower()
            if title_low and title_low in low:
                return win
            if "codex" in low:
                fallback = fallback or win
        except Exception:
            continue
    return fallback


def _extract_doc_text(doc) -> str:
    try:
        tp = doc.GetTextPattern()
        if tp is not None:
            txt = tp.DocumentRange.GetText(-1) or ""
            if txt.strip():
                return txt
    except Exception:
        pass

    lines = []
    seen = set()
    q = deque([doc])
    nodes = 0
    deadline = time.monotonic() + 0.65
    while q and nodes < 900 and time.monotonic() < deadline:
        ctrl = q.popleft()
        nodes += 1
        try:
            ctype = getattr(ctrl, "ControlTypeName", "")
            name = (getattr(ctrl, "Name", "") or "").strip()
            if ctype in ("TextControl", "EditControl", "DocumentControl") and name and name not in seen:
                seen.add(name)
                lines.append(name)
            for child in ctrl.GetChildren():
                q.append(child)
        except Exception:
            continue
    return "\n".join(lines)


def read_codex_blocks(thread_title: str = "") -> list[str]:
    """Вернуть список непустых текстовых блоков из открытого окна Codex Desktop."""
    if not is_available():
        return []
    import uiautomation as auto

    with auto.UIAutomationInitializerInThread():
        win = _find_codex_window(auto, thread_title)
        if win is None:
            return []
        doc = win.DocumentControl(searchDepth=8)
        if not doc.Exists(0.15, 0.05):
            return []
        raw = _extract_doc_text(doc)
        return _clean_blocks(raw)
