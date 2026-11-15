# -*- coding: utf-8 -*-
"""HTTP-слушатель хуков (/api/enqueue, /api/transcribe, /api/show, /api/clear, /api/ping, /api/queue),
клиент доски команды (.swarm team-chat), Telegram-отправка и парсер сообщений доски."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Optional
from uuid import uuid4

if TYPE_CHECKING:
    from app import VoicePlayerApp

_log = lambda msg: None

DB_PATH: Path = Path(".")
HTTP_HOST: str = "127.0.0.1"
HTTP_PORT: int = 3737
POLL_BOARD_INTERVAL_S: float = 2.5


def set_logger(fn) -> None:
    """Установить функцию логирования (обычно app.log_line)."""
    global _log
    _log = fn


def log_line(msg: str) -> None:
    _log(msg)


def configure(
    *,
    db_path: Optional[Path] = None,
    http_host: Optional[str] = None,
    http_port: Optional[int] = None,
    poll_interval_s: Optional[float] = None,
) -> None:
    """Настроить пути и параметры HTTP/доски из app.py."""
    global DB_PATH, HTTP_HOST, HTTP_PORT, POLL_BOARD_INTERVAL_S
    if db_path is not None:
        DB_PATH = Path(db_path)
    if http_host is not None:
        HTTP_HOST = str(http_host)
    if http_port is not None:
        HTTP_PORT = int(http_port)
    if poll_interval_s is not None:
        POLL_BOARD_INTERVAL_S = float(poll_interval_s)


def post_to_board(text: str, author: str = "Антон") -> bool:
    """Опубликовать сообщение на доску команды (.swarm team-chat) от имени author."""
    try:
        now = int(time.time() * 1000)
        content = json.dumps(
            {
                "from": author,
                "message": text,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            },
            ensure_ascii=False,
        )
        eid = f"entry_{now}_{uuid4().hex[:10]}"
        key = f"{author.lower()}_voice_{now}"
        conn = sqlite3.connect(str(DB_PATH), timeout=3.0)
        conn.execute(
            "INSERT INTO memory_entries (id,key,namespace,content,type,created_at,updated_at,status) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (eid, key, "team-chat", content, "semantic", now, now, "active"),
        )
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        log_line(f"post_to_board error: {e}")
        return False


def post_to_telegram(text: str, chat_id: str) -> tuple:
    """Отправить текст в Telegram через бота (токен из окружения). (ok, error)."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        return (False, "нет TELEGRAM_BOT_TOKEN в окружении")
    if not chat_id:
        return (False, "не задан chat_id Telegram (укажи в настройках)")
    try:
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        resp = json.loads(urllib.request.urlopen(req, timeout=10).read().decode("utf-8"))
        return (bool(resp.get("ok")), resp.get("description", ""))
    except Exception as e:
        return (False, str(e))


# ============================ HTTP LISTENER ============================

class _HookHandler(BaseHTTPRequestHandler):
    app: "VoicePlayerApp" = None

    def log_message(self, fmt, *args):
        pass

    def _json(self, code: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if getattr(self, "close_connection", False):
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path == "/api/enqueue":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length).decode("utf-8") if length else ""
                data = json.loads(raw) if raw else {}
            except Exception:
                self._json(400, {"error": "bad json"})
                return
            text = str(data.get("text") or "").strip()
            if not text:
                self._json(204, {})
                return
            item = self.app.enqueue(
                source=data.get("source") or "claude-code",
                author=data.get("author") or f"Claude/{data.get('session') or 'sess'}",
                time_str=data.get("time") or time.strftime("%H:%M"),
                date_str=data.get("date") or "",
                text=text,
                prompt=str(data.get("prompt") or ""),
                title=str(data.get("title") or ""),
            )
            self._json(200, {"id": item.id if item else None, "queued": item is not None})
            return
        if self.path == "/api/transcribe":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                data = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            except Exception:
                self._json(400, {"error": "bad json"})
                return
            src = str(data.get("path") or data.get("url") or "").strip()
            if not src:
                self._json(400, {"error": "no path/url"})
                return
            self.app.root.after(0, lambda: self.app._api_transcribe(src))
            self._json(200, {"ok": True, "queued": True})
            return
        if self.path == "/api/show":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length:
                    self.rfile.read(length)
            except Exception:
                pass
            self.close_connection = True
            log_line("HTTP /api/show received -> activating window")
            self.app.root.after(0, self.app._show_window)
            self._json(200, {"ok": True})
            return
        if self.path == "/api/clear":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length:
                    self.rfile.read(length)
            except Exception:
                pass
            self.close_connection = True
            self.app.clear_queue()
            self._json(200, {"ok": True})
            return
        self._json(404, {"error": "not found"})

    def do_GET(self):
        if self.path == "/api/ping":
            self._json(200, {"ok": True, "queue": len(self.app.queue_items)})
            return
        if self.path == "/api/queue":
            items = [
                {
                    "author": it.author,
                    "source": it.source,
                    "text": it.text,
                    "prompt": it.prompt,
                    "title": it.title,
                    "played": it.played,
                    "transcribing": it.transcribing,
                }
                for it in self.app.queue_items
                if not it.deleted
            ]
            self._json(200, {"count": len(items), "items": items})
            return
        self._json(404, {"error": "not found"})


def start_http_listener(app: "VoicePlayerApp") -> ThreadingHTTPServer:
    global HTTP_PORT
    _HookHandler.app = app
    last_err = None
    for cand in [HTTP_PORT, 3738, 3739, 3740, 3741]:
        try:
            srv = ThreadingHTTPServer((HTTP_HOST, cand), _HookHandler)
            HTTP_PORT = cand
            try:
                pfile = Path.home() / ".voice-player" / "port.txt"
                pfile.parent.mkdir(parents=True, exist_ok=True)
                pfile.write_text(str(cand), encoding="utf-8")
            except Exception:
                pass

            def _safe_serve(server):
                try:
                    server.serve_forever()
                except (OSError, ValueError):
                    pass

            threading.Thread(target=_safe_serve, args=(srv,), daemon=True, name="hook-listener").start()
            return srv
        except OSError as e:
            last_err = e
    raise last_err or RuntimeError("cannot bind HTTP listener")


# ============================ BOARD POLLER ============================

# самоназвания агентов на доске (как они подписываются)
KNOWN_NAMES = [
    "Платон",
    "Кузьма",
    "Денис",
    "Семён",
    "Семен",
    "Марк",
    "Прохор",
    "Савва",
    "Антон",
]


def _grab_name_token(s: str) -> Optional[str]:
    """Самое левое известное имя в строке (= отправитель), с полным токеном (суффикс -2 и т.п.)."""
    best_idx, best_nm = None, None
    for nm in KNOWN_NAMES:
        idx = s.find(nm)
        if idx >= 0 and (best_idx is None or idx < best_idx):
            best_idx, best_nm = idx, nm
    if best_idx is None:
        return None
    tok = s[best_idx:].split()[0]
    return tok.strip(" :,.()—–-") or best_nm


def parse_message(raw: str) -> tuple[str, str, str]:
    s = raw.strip()
    # 0) JSON-сообщение {"from":..., "to":..., "message"/"text"/"body":...}
    if s.startswith("{"):
        try:
            d = json.loads(s)
        except Exception:
            d = None
        if isinstance(d, dict) and (d.get("from") or d.get("author")):
            author = str(d.get("from") or d.get("author") or "доска").strip()
            body = (
                d.get("message")
                or d.get("text")
                or d.get("body")
                or d.get("content")
                or d.get("subject")
                or ""
            )
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False)
            body = str(body).strip()
            tm = ""
            mt = re.search(r"(\d{1,2}:\d{2})", str(d.get("timestamp") or d.get("time") or ""))
            if mt:
                tm = mt.group(1)
            return (author, tm, body or s)
    # 1) [Имя / время] тело
    m = re.match(r"^\[([^/\]]+?)\s*/\s*([^\]]+?)\]\s*(.*)$", s, re.DOTALL)
    if m:
        return (m.group(1).strip(), m.group(2).strip(), m.group(3).strip() or raw)
    # 2) [Имя → ... ] / [Имя ...] без слэша — отправитель = первый именной токен
    m = re.match(r"^\[([^\]]+)\]\s*(.*)$", s, re.DOTALL)
    if m:
        inside, body = m.group(1).strip(), m.group(2).strip()
        name = _grab_name_token(inside) or re.split(r"[→/+(]", inside)[0].strip() or "доска"
        return (name, "", body or raw)
    # 3) без скобок — известное имя в начале (после эмодзи/маркера)
    name = _grab_name_token(s[:48])
    if name:
        return (name, "", s)
    return ("доска", "", raw)


def board_poll_loop(app: "VoicePlayerApp"):
    last_seen_ts: int = int(time.time() * 1000)
    last_db_mtime: tuple[float, float] = (-1.0, -1.0)
    wal_path = Path(str(DB_PATH) + "-wal")
    while not app.stop_event.is_set():
        try:
            if DB_PATH.exists():
                cur_mtime = (
                    DB_PATH.stat().st_mtime,
                    wal_path.stat().st_mtime if wal_path.exists() else 0.0,
                )
                if cur_mtime != last_db_mtime:
                    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=2.0)
                    try:
                        rows = conn.execute(
                            "SELECT id, content, created_at FROM memory_entries "
                            "WHERE namespace='team-chat' AND status='active' AND created_at > ? "
                            "ORDER BY created_at ASC LIMIT 100",
                            (last_seen_ts,),
                        ).fetchall()
                    finally:
                        conn.close()
                    last_db_mtime = cur_mtime
                    for _id, content, created_at in rows:
                        last_seen_ts = max(last_seen_ts, int(created_at))
                        text = str(content or "")
                        author, time_str, body = parse_message(text)
                        ts_val = time_str or time.strftime("%H:%M", time.localtime(created_at / 1000))
                        ds_val = time.strftime("%d.%m.%Y", time.localtime(created_at / 1000))
                        app._ui_after(
                            lambda a=author, ts=ts_val, ds=ds_val, b=body: app.enqueue(
                                source="board", author=a, time_str=ts, date_str=ds, text=b
                            )
                        )
        except Exception as e:
            log_line(f"board poll error: {e}")
        app.stop_event.wait(POLL_BOARD_INTERVAL_S)
