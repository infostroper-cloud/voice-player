"""Наблюдатели: забирают готовые ответы и диалоги нейросетей из их файлов истории.

Хуки (Stop) у Codex и Antigravity зависят от подтверждения доверия, песочницы и
оболочки, в которой они запускаются, и часто молча не срабатывают. Наблюдатель
работает со стороны плеера и только читает:

* Codex (приложение и терминал):
  1) ``~/.codex/thread_history_*.sqlite`` + ``~/.codex/state_*.sqlite`` — завершённые
     ходы ``final_agent_item_id``, запрос пользователя ``first_user_item_id`` и название диалога;
  2) ``~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`` — прямые сессии терминального
     ``codex`` / ``codex exec``, даже если фоновый демон приложения не запущен.
* Antigravity (приложение, IDE, CLI) — ``~/.gemini/<продукт>/brain/<id>/
  .system_generated/logs/transcript.jsonl``: вопрос из ``USER_INPUT`` и финальный
  ответ — ``PLANNER_RESPONSE`` с текстом и без вызовов инструментов.

Позиции чтения сохраняются в ``~/.voice-player/watchers.json``.
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Callable, Optional

CATCHUP_S = 20 * 60            # после перезапуска догружаем ответы не старше 20 минут
AGY_MAX_LINE = 5_000_000       # строка transcript больше этого — пропускаем
NAME_MAX = 34


def _short(name: str, limit: int = NAME_MAX) -> str:
    name = " ".join(str(name or "").split())
    return name if len(name) <= limit else name[: limit - 1].rstrip() + "…"


def _ro(db: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=1.0)
    con.execute("PRAGMA query_only = 1")
    return con


def _latest(home: Path, pattern: str) -> Optional[Path]:
    files = sorted(home.glob(pattern), key=lambda p: (len(p.name), p.name))
    return files[-1] if files else None


def _ts(v) -> float:
    """Codex хранит время то в секундах, то в миллисекундах."""
    try:
        v = float(v or 0)
    except (TypeError, ValueError):
        return 0.0
    return v / 1000.0 if v > 1e11 else v


def _iso_ts(s) -> float:
    if not s:
        return 0.0
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _sig(session: str, text: str) -> str:
    norm = " ".join((text or "").split())[:400]
    h = hashlib.sha1(norm.encode("utf-8", "replace")).hexdigest()[:12]
    return f"{(session or '')[:12]}:{h}"


def _extract_user_item_text(item_json: Optional[str]) -> str:
    if not item_json:
        return ""
    try:
        obj = json.loads(item_json)
    except ValueError:
        return ""
    if isinstance(obj.get("text"), str) and obj["text"].strip():
        return obj["text"].strip()
    parts = []
    for c in obj.get("content") or []:
        if isinstance(c, dict) and isinstance(c.get("text"), str):
            parts.append(c["text"].strip())
    return "\n".join(p for p in parts if p).strip()


# ------------------------------------------------------------------ Codex

class CodexWatcher:
    source = "codex"

    def __init__(self, codex_home: Path, state: dict):
        self.home = Path(codex_home)
        self.state = state

    def available(self) -> bool:
        return (_latest(self.home, "thread_history_*.sqlite") is not None
                or (self.home / "sessions").is_dir())

    def _thread_meta(self, ids: list) -> dict:
        db = _latest(self.home, "state_*.sqlite")
        if not db or not ids:
            return {}
        try:
            con = _ro(db)
            try:
                cols = {r[1] for r in con.execute("PRAGMA table_info(threads)")}
                want = [c for c in ("name", "title", "first_user_message",
                                    "thread_source", "originator", "source") if c in cols]
                q = (f"SELECT id, {', '.join(want)} FROM threads WHERE id IN "
                     f"({','.join('?' * len(ids))})")
                return {r[0]: dict(zip(want, r[1:])) for r in con.execute(q, ids)}
            finally:
                con.close()
        except sqlite3.Error:
            return {}

    def poll(self, now: Optional[float] = None) -> list:
        now = time.time() if now is None else now
        out = []
        seen_sigs = set(self.state.get("sigs") or [])
        for rec in self._poll_sqlite(now) + self._poll_rollouts(now):
            sig = _sig(rec.get("session", ""), rec.get("text", ""))
            if sig in seen_sigs:
                continue
            seen_sigs.add(sig)
            out.append(rec)
        if seen_sigs:
            self.state["sigs"] = list(seen_sigs)[-120:]
        return out

    def _poll_sqlite(self, now: float) -> list:
        db = _latest(self.home, "thread_history_*.sqlite")
        if not db:
            return []
        con = _ro(db)
        try:
            secs = ("(CASE WHEN t.completed_at > 100000000000 "
                    "THEN t.completed_at / 1000.0 ELSE t.completed_at END)")
            if "cursor" not in self.state:
                row = con.execute(f"SELECT MAX({secs}) FROM thread_turns t").fetchone()
                c0 = float(row[0]) if row and row[0] else now
                self.state["cursor"] = c0
                init_seen = con.execute(
                    f"SELECT t.turn_id FROM thread_turns t WHERE {secs} >= ?",
                    (int(c0) - 1,),
                ).fetchall()
                self.state["seen"] = [r[0] for r in init_seen]
                return []
            cursor = float(self.state.get("cursor") or 0)
            seen = set(self.state.get("seen") or [])
            rows = con.execute(
                "SELECT t.thread_id, t.turn_id, t.completed_at, i.item_json, u.item_json "
                "FROM thread_turns t "
                "JOIN thread_items i "
                "  ON i.thread_id = t.thread_id AND i.turn_id = t.turn_id "
                " AND i.item_id = t.final_agent_item_id "
                "LEFT JOIN thread_items u "
                "  ON u.thread_id = t.thread_id AND u.turn_id = t.turn_id "
                " AND u.item_id = t.first_user_item_id "
                "WHERE t.status = 'completed' AND t.final_agent_item_id IS NOT NULL "
                f"  AND {secs} >= ? ORDER BY {secs}, t.rollout_ordinal",
                (int(cursor),)).fetchall()
        finally:
            con.close()
        fresh = [r for r in rows if r[1] not in seen]
        if not fresh:
            return []
        meta = self._thread_meta(sorted({r[0] for r in fresh}))
        out = []
        for thread_id, turn_id, completed_at, item_json, user_json in fresh:
            done = _ts(completed_at)
            seen.add(turn_id)
            cursor = max(cursor, done)
            info = meta.get(thread_id, {})
            tsrc = str(info.get("thread_source") or "user")
            if tsrc not in ("user", "") or "subagent" in str(info.get("source") or ""):
                continue
            if now - done > CATCHUP_S:
                continue
            try:
                text = str(json.loads(item_json).get("text") or "").strip()
            except (ValueError, AttributeError):
                text = ""
            if not text:
                continue
            prompt = _extract_user_item_text(user_json)
            terminal = str(info.get("originator") or "").lower() in (
                "codex-tui", "codex_cli_rs", "codex-cli", "cli"
            )
            label = "Codex CLI" if terminal else "Codex"
            title = _short(info.get("name") or info.get("title")
                           or info.get("first_user_message") or prompt or thread_id[:8])
            out.append({
                "source": self.source,
                "author": f"{label} · {title}" if title else label,
                "session": thread_id,
                "title": title,
                "prompt": prompt[:300],
                "time": time.strftime("%H:%M", time.localtime(done or now)),
                "text": text,
                "key": f"codex:{turn_id}",
            })
        self.state["cursor"] = cursor
        self.state["seen"] = [r[1] for r in rows if abs(_ts(r[2]) - cursor) < 2.0]
        return out

    def _recent_rollout_files(self, now: float) -> list:
        sess_dir = self.home / "sessions"
        if not sess_dir.is_dir():
            return []
        pat = str(sess_dir / "*" / "*" / "*" / "rollout-*.jsonl")
        out = []
        for p in glob.glob(pat):
            try:
                if now - os.path.getmtime(p) <= CATCHUP_S:
                    out.append(p)
            except OSError:
                continue
        return sorted(out)

    def _poll_rollouts(self, now: float) -> list:
        rfiles = self.state.setdefault("rollouts", {})
        first_run = not self.state.get("rollouts_init")
        out = []
        for path in self._recent_rollout_files(now):
            try:
                st = os.stat(path)
            except OSError:
                continue
            rec = rfiles.get(path)
            if rec is None:
                rfiles[path] = rec = {
                    "off": st.st_size if first_run else 0,
                    "session": "",
                    "originator": "",
                    "title": "",
                    "q": "",
                    "subagent": False,
                }
                if first_run:
                    continue
            if st.st_size < rec.get("off", 0):
                rec["off"] = 0
            if st.st_size == rec.get("off", 0):
                continue
            out.extend(self._read_rollout_chunk(path, rec, now))
        self.state["rollouts_init"] = True
        for p in [p for p in rfiles if not os.path.exists(p)]:
            rfiles.pop(p, None)
        return out

    def _read_rollout_chunk(self, path: str, rec: dict, now: float) -> list:
        try:
            with open(path, "rb") as f:
                f.seek(rec.get("off", 0))
                chunk = f.read()
        except OSError:
            return []
        end = chunk.rfind(b"\n")
        if end == -1:
            return []
        rec["off"] = rec.get("off", 0) + end + 1
        out = []
        for raw in chunk[:end].split(b"\n"):
            if not raw.strip() or len(raw) > AGY_MAX_LINE:
                continue
            try:
                ev = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            etype = ev.get("type")
            pl = ev.get("payload") or {}
            if not isinstance(pl, dict):
                continue
            if etype == "session_meta":
                rec["session"] = str(pl.get("session_id") or pl.get("id") or rec.get("session") or "")
                rec["originator"] = str(pl.get("originator") or "")
                tsrc = str(pl.get("thread_source") or "user")
                src = str(pl.get("source") or "")
                rec["subagent"] = (tsrc not in ("user", "")) or ("subagent" in src)
                continue
            if rec.get("subagent"):
                continue
            if etype == "response_item" and pl.get("type") == "message":
                role = pl.get("role")
                if role == "user":
                    txt = _extract_user_item_text(json.dumps(pl, ensure_ascii=False))
                    if txt and not txt.startswith("<"):
                        rec["q"] = txt[:300]
                        if not rec.get("title"):
                            rec["title"] = _short(txt)
                elif role == "assistant" and pl.get("phase") == "final_answer":
                    parts = [
                        c.get("text", "") for c in (pl.get("content") or [])
                        if isinstance(c, dict) and isinstance(c.get("text"), str)
                    ]
                    text = "\n".join(p.strip() for p in parts if p.strip()).strip()
                    if not text:
                        continue
                    when = _iso_ts(ev.get("timestamp")) or now
                    if now - when > CATCHUP_S:
                        continue
                    sess = rec.get("session") or Path(path).stem[-36:]
                    meta = self._thread_meta([sess]).get(sess, {}) if sess else {}
                    orig = str(meta.get("originator") or rec.get("originator") or "").lower()
                    terminal = orig in ("codex-tui", "codex_cli_rs", "codex-cli", "cli") or not meta
                    label = "Codex CLI" if terminal else "Codex"
                    title = _short(meta.get("name") or meta.get("title")
                                   or rec.get("title") or rec.get("q") or sess[:8])
                    out.append({
                        "source": self.source,
                        "author": f"{label} · {title}" if title else label,
                        "session": sess,
                        "title": title,
                        "prompt": (rec.get("q") or "")[:300],
                        "time": time.strftime("%H:%M", time.localtime(when)),
                        "text": text,
                        "key": f"codex_msg:{pl.get('id') or int(when * 1000)}",
                    })
        return out


# ------------------------------------------------------------ Antigravity

def _user_text(content: str) -> str:
    s = content or ""
    a, b = s.find("<USER_REQUEST>"), s.find("</USER_REQUEST>")
    if a != -1 and b != -1:
        return s[a + len("<USER_REQUEST>"): b].strip()
    if "<CONTEXT_SUMMARY>" in s:
        # При сжатии контекста Antigravity кладёт список последних запросов в секцию # User Requests
        ur_idx = s.find("# User Requests")
        if ur_idx != -1:
            sec = s[ur_idx:].split("# Previous Session Summary", 1)[0]
            reqs = []
            for line in sec.splitlines():
                line = line.strip()
                if len(line) > 3 and line[0].isdigit() and ". " in line[:5]:
                    reqs.append(line.split(". ", 1)[1].strip())
            if reqs:
                return reqs[-1]
        return ""
    return s.strip()


def _full_step_content(transcript_path: str, step_index) -> str:
    """Если в компактном transcript.jsonl поле content усечено (truncated_fields),
    достать полный текст этого шага из соседнего transcript_full.jsonl."""
    if step_index is None:
        return ""
    full_path = Path(transcript_path).with_name("transcript_full.jsonl")
    if not full_path.exists():
        return ""
    needle = f'"step_index": {step_index}'.encode("utf-8")
    needle2 = f'"step_index":{step_index}'.encode("utf-8")
    try:
        with open(full_path, "rb") as f:
            for raw in f:
                if needle in raw or needle2 in raw:
                    step = json.loads(raw.decode("utf-8", "replace"))
                    if step.get("step_index") == step_index:
                        return str(step.get("content") or "").strip()
    except Exception:
        pass
    return ""


class AntigravityWatcher:
    source = "antigravity"
    PRODUCTS = {"antigravity": "Antigravity", "antigravity-ide": "Antigravity IDE",
                "antigravity-cli": "Antigravity CLI"}

    def __init__(self, gemini_home: Path, state: dict):
        self.home = Path(gemini_home)
        self.state = state

    def available(self) -> bool:
        return any((self.home / p / "brain").is_dir() for p in self.PRODUCTS)

    def _transcripts(self) -> list:
        out = []
        for product in self.PRODUCTS:
            pat = str(self.home / product / "brain" / "*" / ".system_generated" / "logs" / "transcript.jsonl")
            out.extend(glob.glob(pat))
        return out

    def poll(self, now: Optional[float] = None) -> list:
        now = time.time() if now is None else now
        files = self.state.setdefault("files", {})
        first_run = not self.state.get("init")
        out = []
        for path in self._transcripts():
            try:
                st = os.stat(path)
            except OSError:
                continue
            rec = files.get(path)
            if rec is None:
                title = self._peek_title(path)
                files[path] = rec = {
                    "off": st.st_size if first_run else 0,
                    "user": False,
                    "q": "",
                    "title": title,
                }
                if first_run:
                    continue
            if not rec.get("title"):
                rec["title"] = self._peek_title(path)
            if st.st_size < rec["off"]:
                rec["off"] = 0
            if st.st_size == rec["off"]:
                continue
            out.extend(self._read_new(path, rec, now))
        self.state["init"] = True
        for p in [p for p in files if not os.path.exists(p)]:
            files.pop(p, None)
        return out

    @staticmethod
    def _peek_title(path: str) -> str:
        """Прочитать первый запрос пользователя в переписке, чтобы дать диалогу осмысленное имя."""
        try:
            with open(path, "rb") as f:
                head = f.read(65536).decode("utf-8", "replace")
            for line in head.splitlines():
                if '"USER_INPUT"' not in line:
                    continue
                step = json.loads(line)
                if step.get("type") == "USER_INPUT" and step.get("source") in ("USER_EXPLICIT", "USER"):
                    q = _user_text(step.get("content") or "")
                    if q:
                        return _short(q)
        except Exception:
            pass
        return ""

    def _read_new(self, path: str, rec: dict, now: float) -> list:
        try:
            with open(path, "rb") as f:
                f.seek(rec["off"])
                chunk = f.read()
        except OSError:
            return []
        end = chunk.rfind(b"\n")
        if end == -1:
            return []
        rec["off"] += end + 1
        parts = Path(path).parts
        product = parts[-6] if len(parts) >= 6 else "antigravity"
        conv = parts[-4] if len(parts) >= 4 else ""
        label = self.PRODUCTS.get(product, "Antigravity")
        out = []
        for raw in chunk[:end].split(b"\n"):
            if not raw.strip() or len(raw) > AGY_MAX_LINE:
                continue
            try:
                step = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            kind = step.get("type")
            if kind == "USER_INPUT":
                if step.get("source") in ("USER_EXPLICIT", "USER"):
                    rec["user"] = True
                    q = _user_text(step.get("content") or "")
                    if q:
                        rec["q"] = q[:300]
                        if not rec.get("title"):
                            rec["title"] = _short(q)
                continue
            if kind != "PLANNER_RESPONSE" or step.get("tool_calls"):
                continue
            text = str(step.get("content") or "").strip()
            if "content" in (step.get("truncated_fields") or []):
                full_text = _full_step_content(path, step.get("step_index"))
                if full_text:
                    text = full_text
            if not text or not rec.get("user"):
                continue
            rec["user"] = False
            when = _iso_ts(step.get("created_at")) or now
            if now - when > CATCHUP_S:
                continue
            title = rec.get("title") or _short(rec.get("q") or conv[:8])
            out.append({
                "source": self.source,
                "author": f"{label} · {title}" if title else f"{label}/{conv[:8]}",
                "session": conv,
                "title": title,
                "prompt": (rec.get("q") or "")[:300],
                "time": time.strftime("%H:%M", time.localtime(when)),
                "date": time.strftime("%d.%m.%Y", time.localtime(when)),
                "text": text,
                "key": f"agy:{conv}:{step.get('step_index')}",
            })
        return out


# ------------------------------------------------------------------ Cline

class ClineWatcher:
    source = "cline"

    def __init__(self, home: Path, state: dict):
        self.home = Path(home)
        self.state = state

    def available(self) -> bool:
        return any(p.is_dir() for p in self._task_bases())

    def _task_bases(self) -> list[Path]:
        return [
            self.home / "AppData/Roaming/Code/User/globalStorage/saoudrizwan.claude-dev/tasks",
            self.home / ".config/Code/User/globalStorage/saoudrizwan.claude-dev/tasks",
            self.home / "Library/Application Support/Code/User/globalStorage/saoudrizwan.claude-dev/tasks",
        ]

    def _task_dirs(self) -> list[Path]:
        out = []
        for base in self._task_bases():
            if base.is_dir():
                try:
                    for d in base.iterdir():
                        if d.is_dir():
                            out.append(d)
                except OSError:
                    pass
        return out

    def poll(self, now: Optional[float] = None) -> list:
        now = time.time() if now is None else now
        dirs = self._task_dirs()
        if not dirs:
            return []
        seen = set(self.state.get("seen") or [])
        out = []
        for tdir in sorted(dirs, key=lambda d: d.stat().st_mtime if d.exists() else 0, reverse=True)[:8]:
            msg_file = tdir / "ui_messages.json"
            if not msg_file.exists():
                continue
            try:
                st = msg_file.stat()
                if now - st.st_mtime > CATCHUP_S:
                    continue
                data = json.loads(msg_file.read_text(encoding="utf-8", errors="replace"))
                if not isinstance(data, list):
                    continue
                last_user = ""
                for m in data:
                    if m.get("type") == "ask" and m.get("ask") == "user_feedback":
                        last_user = str(m.get("text") or "")[:300]
                    elif m.get("say") in ("text", "completion_result"):
                        text = str(m.get("text") or "").strip()
                        ts = m.get("ts") or int(st.st_mtime * 1000)
                        sig = f"cline:{tdir.name}:{ts}"
                        if sig in seen:
                            continue
                        seen.add(sig)
                        if text:
                            title = _short(last_user or tdir.name[:8])
                            out.append({
                                "source": self.source,
                                "author": f"Cline · {title}" if title else "Cline",
                                "session": tdir.name[:8],
                                "title": title,
                                "prompt": last_user,
                                "time": time.strftime("%H:%M", time.localtime(ts / 1000.0)),
                                "date": time.strftime("%d.%m.%Y", time.localtime(ts / 1000.0)),
                                "text": text,
                                "key": sig,
                            })
            except Exception:
                continue
        if seen:
            self.state["seen"] = list(seen)[-100:]
        return out


# ------------------------------------------------------------------ Aider

class AiderWatcher:
    source = "aider"

    def __init__(self, home: Path, state: dict):
        self.home = Path(home)
        self.state = state

    def available(self) -> bool:
        return (self.home / ".aider.chat.history.md").exists()

    def poll(self, now: Optional[float] = None) -> list:
        now = time.time() if now is None else now
        hist_file = self.home / ".aider.chat.history.md"
        if not hist_file.exists():
            return []
        try:
            st = hist_file.stat()
        except OSError:
            return []
        off = self.state.get("off", 0)
        if not self.state.get("init"):
            self.state["init"] = True
            self.state["off"] = st.st_size
            return []
        if st.st_size < off:
            off = 0
        if st.st_size == off:
            return []
        try:
            with open(hist_file, "rb") as f:
                f.seek(off)
                chunk = f.read()
        except OSError:
            return []
        self.state["off"] = off + len(chunk)
        raw_text = chunk.decode("utf-8", "replace").strip()
        if not raw_text:
            return []
        lines = []
        user_prompt = ""
        for line in raw_text.splitlines():
            if line.startswith("#### ") or line.startswith("# "):
                user_prompt = line.lstrip("#").strip()
            else:
                lines.append(line)
        text = "\n".join(lines).strip()
        if not text:
            return []
        title = _short(user_prompt or "Aider Chat")
        return [{
            "source": self.source,
            "author": f"Aider · {title}" if title else "Aider",
            "session": "aider",
            "title": title,
            "prompt": user_prompt,
            "time": time.strftime("%H:%M", time.localtime(now)),
            "date": time.strftime("%d.%m.%Y", time.localtime(now)),
            "text": text,
            "key": f"aider:{int(now*1000)}",
        }]


# ------------------------------------------------------------------ Terminal

class TerminalWatcher:
    source = "terminal"

    def __init__(self, vp_home: Path, state: dict):
        self.home = Path(vp_home)
        self.state = state

    def available(self) -> bool:
        return (self.home / "terminal.log").exists()

    def poll(self, now: Optional[float] = None) -> list:
        now = time.time() if now is None else now
        log_file = self.home / "terminal.log"
        if not log_file.exists():
            return []
        try:
            st = log_file.stat()
        except OSError:
            return []
        off = self.state.get("off", 0)
        if not self.state.get("init"):
            self.state["init"] = True
            self.state["off"] = st.st_size
            return []
        if st.st_size < off:
            off = 0
        if st.st_size == off:
            return []
        try:
            with open(log_file, "rb") as f:
                f.seek(off)
                chunk = f.read()
        except OSError:
            return []
        self.state["off"] = off + len(chunk)
        text = chunk.decode("utf-8", "replace").strip()
        if not text:
            return []
        return [{
            "source": self.source,
            "author": "Terminal · Вывод команды",
            "session": "cli",
            "title": "Терминал",
            "prompt": "",
            "time": time.strftime("%H:%M", time.localtime(now)),
            "date": time.strftime("%d.%m.%Y", time.localtime(now)),
            "text": text,
            "key": f"term:{int(now*1000)}",
        }]


# ------------------------------------------------------------------ Ollama
class OllamaWatcher:
    """Следит за новыми ответами локальной Ollama (база db.sqlite в AppData/Local/Ollama или ~/.ollama)."""
    source = "ollama"

    def __init__(self, home: Optional[Path] = None, state: Optional[dict] = None):
        self.home = Path(home or Path.home())
        self.state = state if state is not None else {}

    def _find_db(self) -> Optional[Path]:
        candidates = [
            Path(os.environ.get("LOCALAPPDATA", "")) / "Ollama" / "db.sqlite",
            self.home / ".ollama" / "db.sqlite",
            Path(os.environ.get("APPDATA", "")) / "Ollama" / "db.sqlite",
        ]
        return next((p for p in candidates if p.is_file()), None)

    def available(self) -> bool:
        db = self._find_db()
        return bool(db and db.is_file())

    def poll(self, now: Optional[float] = None) -> list:
        now = time.time() if now is None else now
        db = self._find_db()
        if not db:
            return []

        last_id = int(self.state.get("last_id", 0))
        # При первом запуске фиксируем текущий id (чтобы не зачитывать древнюю историю)
        if not self.state.get("init"):
            try:
                con = _ro(db)
                cur = con.cursor()
                cur.execute("SELECT max(id) FROM messages")
                row = cur.fetchone()
                self.state["last_id"] = int(row[0] or 0)
                self.state["init"] = True
                return []
            except Exception:
                self.state["init"] = True
                return []

        out = []
        try:
            con = _ro(db)
            cur = con.cursor()
            cur.execute(
                "SELECT id, chat_id, role, content, model_name, created_at, stream "
                "FROM messages "
                "WHERE id > ? AND role = 'assistant' AND (stream = 0 OR stream IS NULL) AND length(content) > 0 "
                "ORDER BY id ASC",
                (last_id,)
            )
            rows = cur.fetchall()
            for msg_id, chat_id, role, content, model_name, created_at, stream in rows:
                last_id = max(last_id, msg_id)
                p_cur = con.cursor()
                p_cur.execute(
                    "SELECT content FROM messages WHERE chat_id = ? AND role = 'user' AND id < ? ORDER BY id DESC LIMIT 1",
                    (chat_id, msg_id)
                )
                p_row = p_cur.fetchone()
                prompt = (p_row[0] or "").strip() if p_row else ""

                c_cur = con.cursor()
                c_cur.execute("SELECT title FROM chats WHERE id = ?", (chat_id,))
                c_row = c_cur.fetchone()
                title = (c_row[0] or "").strip() if c_row else ""

                raw_mod = (model_name or "local").split("/")[-1].split(":")[0]
                model_label = raw_mod.replace("-GGUF", "").replace("-abliterated", "")

                when = _iso_ts(created_at) or now
                out.append({
                    "source": "ollama",
                    "author": f"Ollama · {model_label}",
                    "session": chat_id,
                    "title": title or _short(prompt) or "Ollama",
                    "prompt": prompt[:300],
                    "time": time.strftime("%H:%M", time.localtime(when)),
                    "date": time.strftime("%d.%m.%Y", time.localtime(when)),
                    "text": content,
                    "key": f"ollama:{chat_id}:{msg_id}",
                })
            self.state["last_id"] = last_id
        except Exception:
            pass
        return out

    def fetch_recent(self, limit: int = 5) -> list:
        db = self._find_db()
        if not db:
            return []
        out = []
        try:
            con = _ro(db)
            cur = con.cursor()
            cur.execute(
                "SELECT id, chat_id, role, content, model_name, created_at "
                "FROM messages "
                "WHERE role = 'assistant' AND (stream = 0 OR stream IS NULL) AND length(content) > 0 "
                "ORDER BY id DESC LIMIT ?",
                (limit,)
            )
            rows = list(reversed(cur.fetchall()))
            now = time.time()
            for msg_id, chat_id, role, content, model_name, created_at in rows:
                p_cur = con.cursor()
                p_cur.execute(
                    "SELECT content FROM messages WHERE chat_id = ? AND role = 'user' AND id < ? ORDER BY id DESC LIMIT 1",
                    (chat_id, msg_id)
                )
                p_row = p_cur.fetchone()
                prompt = (p_row[0] or "").strip() if p_row else ""

                raw_mod = (model_name or "local").split("/")[-1].split(":")[0]
                model_label = raw_mod.replace("-GGUF", "").replace("-abliterated", "")
                when = _iso_ts(created_at) or now
                out.append({
                    "source": "ollama",
                    "author": f"Ollama · {model_label}",
                    "session": chat_id,
                    "title": _short(prompt) or "Ollama",
                    "prompt": prompt[:300],
                    "time": time.strftime("%H:%M", time.localtime(when)),
                    "date": time.strftime("%d.%m.%Y", time.localtime(when)),
                    "text": content,
                    "key": f"ollama:{chat_id}:{msg_id}",
                })
        except Exception:
            pass
        return out


# ------------------------------------------------------------------ DeepSeek Harness
class DeepSeekWatcher:
    """Следит за свежими сессиями DeepSeek Harness (~/.dsh)."""
    source = "deepseek"

    def __init__(self, home: Optional[Path] = None, state: Optional[dict] = None):
        self.home = Path(home or Path.home())
        self.state = state if state is not None else {}
        self.dsh_dir = self.home / ".dsh"

    def available(self) -> bool:
        return (self.dsh_dir / "storages" / "session_projcache" / "sessions").is_dir()

    def _get_recent_session_files(self) -> list:
        sess_dir = self.dsh_dir / "storages" / "session_projcache" / "sessions"
        if not sess_dir.is_dir():
            return []
        files = list(sess_dir.glob("session-*.json"))
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return files[:8]

    def poll(self, now: Optional[float] = None) -> list:
        now = time.time() if now is None else now
        if not self.available():
            return []

        seen = set(self.state.setdefault("seen", []))
        first_run = not self.state.get("init")
        out = []

        for fpath in self._get_recent_session_files():
            try:
                with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                    data = json.load(f)
                rows = data.get("record", {}).get("rows", {})
                turns = rows.get("turnOutline", {}).get("val", {}).get("turns", [])
                title = rows.get("title", {}).get("val", "") or rows.get("goal", {}).get("val", {}).get("current", {}).get("goal", {}).get("objective", "")
                sess_id = fpath.stem.replace("session-", "")

                for t in turns:
                    turn_num = t.get("turn", 0)
                    seq = t.get("seq", 0)
                    key = f"dsh:{sess_id}:{turn_num}:{seq}"
                    if key in seen:
                        continue
                    seen.add(key)
                    if first_run:
                        continue
                    prompt = (t.get("prompt") or "").strip()
                    resp = (t.get("response") or "").strip()
                    if not resp:
                        continue
                    mtime = fpath.stat().st_mtime
                    out.append({
                        "source": "deepseek",
                        "author": "DeepSeek Harness",
                        "session": sess_id,
                        "title": _short(title or prompt or "DeepSeek"),
                        "prompt": prompt[:300],
                        "time": time.strftime("%H:%M", time.localtime(mtime)),
                        "date": time.strftime("%d.%m.%Y", time.localtime(mtime)),
                        "text": resp,
                        "key": key,
                    })
            except Exception:
                continue

        self.state["seen"] = list(seen)[-500:]
        self.state["init"] = True
        return out

    def fetch_recent(self, limit: int = 5) -> list:
        if not self.available():
            return []
        out = []
        for fpath in self._get_recent_session_files():
            try:
                with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                    data = json.load(f)
                rows = data.get("record", {}).get("rows", {})
                turns = rows.get("turnOutline", {}).get("val", {}).get("turns", [])
                title = rows.get("title", {}).get("val", "") or rows.get("goal", {}).get("val", {}).get("current", {}).get("goal", {}).get("objective", "")
                sess_id = fpath.stem.replace("session-", "")
                mtime = fpath.stat().st_mtime
                for t in reversed(turns):
                    resp = (t.get("response") or "").strip()
                    prompt = (t.get("prompt") or "").strip()
                    if resp:
                        out.append({
                            "source": "deepseek",
                            "author": "DeepSeek Harness",
                            "session": sess_id,
                            "title": _short(title or prompt or "DeepSeek"),
                            "prompt": prompt[:300],
                            "time": time.strftime("%H:%M", time.localtime(mtime)),
                            "date": time.strftime("%d.%m.%Y", time.localtime(mtime)),
                            "text": resp,
                            "key": f"dsh:{sess_id}:{t.get('turn', 0)}:{t.get('seq', 0)}",
                        })
                        if len(out) >= limit:
                            return out
            except Exception:
                continue
        return out


# ------------------------------------------------------------------ Kimi Desktop
class KimiWatcher:
    """Следит за сессиями Kimi Desktop (wire.jsonl в AppData/Roaming/kimi-desktop)."""
    source = "kimi"

    def __init__(self, home: Optional[Path] = None, state: Optional[dict] = None):
        self.home = Path(home or Path.home())
        self.state = state if state is not None else {}
        self.base_dir = Path(os.environ.get("APPDATA", "")) / "kimi-desktop" / "daimon-share" / "daimon" / "runtime" / "kimi-code" / "home" / "sessions"

    def available(self) -> bool:
        return self.base_dir.is_dir()

    def _get_wire_files(self) -> list:
        if not self.base_dir.is_dir():
            return []
        wires = list(self.base_dir.glob("*/*/agents/main/wire.jsonl"))
        wires.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return wires[:8]

    def poll(self, now: Optional[float] = None) -> list:
        now = time.time() if now is None else now
        if not self.available():
            return []

        files_state = self.state.setdefault("files", {})
        first_run = not self.state.get("init")
        out = []

        for wpath in self._get_wire_files():
            sp = str(wpath)
            try:
                sz = wpath.stat().st_size
            except OSError:
                continue

            last_off = files_state.get(sp, 0)
            if first_run:
                files_state[sp] = sz
                continue
            if sz < last_off:
                last_off = 0
            if sz == last_off:
                continue

            try:
                with open(wpath, "r", encoding="utf-8", errors="ignore") as f:
                    f.seek(last_off)
                    chunk = f.read()
                    files_state[sp] = f.tell()
                lines = chunk.splitlines()
                conv_name = wpath.parent.parent.name
                mtime = wpath.stat().st_mtime
                for line in lines:
                    try:
                        ev = json.loads(line)
                        if ev.get("type") == "context.append_message":
                            msg = ev.get("message", {})
                            if msg.get("role") == "assistant":
                                content = msg.get("content", [])
                                txt = "".join(c.get("text", "") for c in content if isinstance(c, dict)).strip()
                                if txt:
                                    out.append({
                                        "source": "kimi",
                                        "author": "Kimi Desktop",
                                        "session": conv_name,
                                        "title": "Kimi",
                                        "prompt": "",
                                        "time": time.strftime("%H:%M", time.localtime(mtime)),
                                        "date": time.strftime("%d.%m.%Y", time.localtime(mtime)),
                                        "text": txt,
                                        "key": f"kimi:{conv_name}:{hash(txt)}",
                                    })
                    except Exception:
                        pass
            except Exception:
                continue

        self.state["init"] = True
        return out

    def fetch_recent(self, limit: int = 5) -> list:
        if not self.available():
            return []
        out = []
        for wpath in self._get_wire_files():
            conv_name = wpath.parent.parent.name
            mtime = wpath.stat().st_mtime
            try:
                with open(wpath, "r", encoding="utf-8", errors="ignore") as f:
                    for line in reversed(f.readlines()):
                        try:
                            ev = json.loads(line)
                            if ev.get("type") == "context.append_message":
                                msg = ev.get("message", {})
                                if msg.get("role") == "assistant":
                                    content = msg.get("content", [])
                                    txt = "".join(c.get("text", "") for c in content if isinstance(c, dict)).strip()
                                    if txt:
                                        out.append({
                                            "source": "kimi",
                                            "author": "Kimi Desktop",
                                            "session": conv_name,
                                            "title": "Kimi",
                                            "prompt": "",
                                            "time": time.strftime("%H:%M", time.localtime(mtime)),
                                            "date": time.strftime("%d.%m.%Y", time.localtime(mtime)),
                                            "text": txt,
                                            "key": f"kimi:{conv_name}:{hash(txt)}",
                                        })
                                        if len(out) >= limit:
                                            return out
                        except Exception:
                            pass
            except Exception:
                pass
        return out


# ---------------------------------------------------------------- общий цикл

class Watchers:
    """Держит наблюдателей и их позиции; ``poll()`` безопасно вызывать из фонового потока."""

    def __init__(self, state_file: Path, home: Optional[Path] = None,
                 log: Callable[[str], None] = lambda _m: None):
        self.state_file = Path(state_file)
        self.log = log
        home = Path(home or Path.home())
        try:
            self.state = json.loads(self.state_file.read_text(encoding="utf-8"))
            if not isinstance(self.state, dict):
                self.state = {}
        except (OSError, ValueError):
            self.state = {}
        self.items = {
            "codex": CodexWatcher(home / ".codex", self.state.setdefault("codex", {})),
            "antigravity": AntigravityWatcher(home / ".gemini", self.state.setdefault("antigravity", {})),
            "cline": ClineWatcher(home, self.state.setdefault("cline", {})),
            "aider": AiderWatcher(home, self.state.setdefault("aider", {})),
            "terminal": TerminalWatcher(home / ".voice-player", self.state.setdefault("terminal", {})),
            "ollama": OllamaWatcher(home, self.state.setdefault("ollama", {})),
            "deepseek": DeepSeekWatcher(home, self.state.setdefault("deepseek", {})),
            "kimi": KimiWatcher(home, self.state.setdefault("kimi", {})),
        }
        self._errors: dict = {}

    def available(self) -> dict:
        return {k: w.available() for k, w in self.items.items()}

    def poll(self, enabled: Optional[dict] = None) -> list:
        enabled = enabled or {}
        out = []
        changed = False
        for key, w in self.items.items():
            if not enabled.get(key, True):
                continue
            try:
                before = json.dumps(w.state, sort_keys=True)
                out.extend(w.poll())
                changed = changed or json.dumps(w.state, sort_keys=True) != before
                self._errors.pop(key, None)
            except Exception as e:  # noqa: BLE001 — наблюдатель не должен ронять плеер
                msg = f"{type(e).__name__}: {e}"
                if self._errors.get(key) != msg:
                    self._errors[key] = msg
                    self.log(f"watcher {key}: {msg}")
        if changed:
            self.save()
        return out

    def fetch_recent(self, limit_per_source: int = 5) -> list:
        """Явно подтянуть последние диалоги и ответы из Codex и Antigravity (по кнопке или при проверке)."""
        out = []
        now = time.time()
        # 1) Codex из SQLite
        cw: CodexWatcher = self.items["codex"]
        db = _latest(cw.home, "thread_history_*.sqlite")
        if db:
            try:
                con = _ro(db)
                try:
                    secs = ("(CASE WHEN t.completed_at > 100000000000 "
                            "THEN t.completed_at / 1000.0 ELSE t.completed_at END)")
                    rows = con.execute(
                        "SELECT t.thread_id, t.turn_id, t.completed_at, i.item_json, u.item_json "
                        "FROM thread_turns t "
                        "JOIN thread_items i "
                        "  ON i.thread_id = t.thread_id AND i.turn_id = t.turn_id "
                        " AND i.item_id = t.final_agent_item_id "
                        "LEFT JOIN thread_items u "
                        "  ON u.thread_id = t.thread_id AND u.turn_id = t.turn_id "
                        " AND u.item_id = t.first_user_item_id "
                        "WHERE t.status = 'completed' AND t.final_agent_item_id IS NOT NULL "
                        f"ORDER BY {secs} DESC LIMIT ?",
                        (limit_per_source * 3,)).fetchall()
                finally:
                    con.close()
                meta = cw._thread_meta(sorted({r[0] for r in rows}))
                picked = []
                for thread_id, turn_id, completed_at, item_json, user_json in rows:
                    info = meta.get(thread_id, {})
                    tsrc = str(info.get("thread_source") or "user")
                    if tsrc not in ("user", "") or "subagent" in str(info.get("source") or ""):
                        continue
                    try:
                        text = str(json.loads(item_json).get("text") or "").strip()
                    except Exception:
                        text = ""
                    if not text:
                        continue
                    done = _ts(completed_at) or now
                    prompt = _extract_user_item_text(user_json)
                    terminal = str(info.get("originator") or "").lower() in (
                        "codex-tui", "codex_cli_rs", "codex-cli", "cli"
                    )
                    label = "Codex CLI" if terminal else "Codex"
                    title = _short(info.get("name") or info.get("title")
                                   or info.get("first_user_message") or prompt or thread_id[:8])
                    picked.append({
                        "source": "codex",
                        "author": f"{label} · {title}" if title else label,
                        "session": thread_id,
                        "title": title,
                        "prompt": prompt[:300],
                        "time": time.strftime("%H:%M", time.localtime(done)),
                        "date": time.strftime("%d.%m.%Y", time.localtime(done)),
                        "text": text,
                        "key": f"codex:{turn_id}",
                    })
                    if len(picked) >= limit_per_source:
                        break
                out.extend(reversed(picked))
            except Exception as e:
                self.log(f"fetch_recent codex: {e}")

        # 1b) Дополняем свежими сессиями из rollout-*.jsonl (терминальный Codex CLI без записи в SQLite)
        try:
            seen_texts = {(it["session"], it["text"][:160]) for it in out}
            rpaths = sorted(cw._recent_rollout_files(now), key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0, reverse=True)
            rollout_items = []
            for rpath in rpaths[:limit_per_source * 2]:
                rec = {"off": 0, "session": "", "originator": "", "subagent": False, "q": "", "title": ""}
                for it in cw._read_rollout_chunk(rpath, rec, now):
                    sig = (it.get("session") or "", (it.get("text") or "")[:160])
                    if sig not in seen_texts:
                        seen_texts.add(sig)
                        it.setdefault("date", time.strftime("%d.%m.%Y", time.localtime(now)))
                        rollout_items.append(it)
            if rollout_items:
                out.extend(rollout_items[-limit_per_source:])
                out = out[-limit_per_source:]
        except Exception as e:
            self.log(f"fetch_recent codex rollouts: {e}")

        # 2) Antigravity из самых свежих transcript.jsonl (все завершённые ответы в последних диалогах)
        aw: AntigravityWatcher = self.items["antigravity"]
        tpaths = sorted(aw._transcripts(), key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0, reverse=True)
        agy_turns = []
        for path in tpaths[:limit_per_source]:
            try:
                title = aw._peek_title(path)
                parts = Path(path).parts
                product = parts[-6] if len(parts) >= 6 else "antigravity"
                conv = parts[-4] if len(parts) >= 4 else ""
                label = aw.PRODUCTS.get(product, "Antigravity")
                last_q = ""
                user_turn = False
                conv_turns = []
                with open(path, "rb") as f:
                    for raw in f:
                        if not raw.strip() or len(raw) > AGY_MAX_LINE:
                            continue
                        try:
                            step = json.loads(raw.decode("utf-8", "replace"))
                        except ValueError:
                            continue
                        kind = step.get("type")
                        if kind == "USER_INPUT":
                            if step.get("source") in ("USER_EXPLICIT", "USER"):
                                user_turn = True
                                q = _user_text(step.get("content") or "")
                                if q:
                                    last_q = q[:300]
                        elif kind == "PLANNER_RESPONSE" and not step.get("tool_calls"):
                            text = str(step.get("content") or "").strip()
                            if "content" in (step.get("truncated_fields") or []):
                                full_text = _full_step_content(path, step.get("step_index"))
                                if full_text:
                                    text = full_text
                            if text and user_turn:
                                when = _iso_ts(step.get("created_at")) or now
                                t_disp = title or _short(last_q or conv[:8])
                                conv_turns.append((when, {
                                    "source": "antigravity",
                                    "author": f"{label} · {t_disp}" if t_disp else f"{label}/{conv[:8]}",
                                    "session": conv,
                                    "title": t_disp,
                                    "prompt": last_q,
                                    "time": time.strftime("%H:%M", time.localtime(when)),
                                    "date": time.strftime("%d.%m.%Y", time.localtime(when)),
                                    "text": text,
                                    "key": f"agy:{conv}:{step.get('step_index')}",
                                }))
                                user_turn = False
                agy_turns.extend(conv_turns[-3:])
            except Exception as e:
                self.log(f"fetch_recent agy: {e}")
        agy_turns.sort(key=lambda pair: pair[0])
        out.extend(item for _, item in agy_turns[-limit_per_source:])

        # 3) Ollama
        ow = self.items.get("ollama")
        if ow and hasattr(ow, "fetch_recent"):
            try:
                out.extend(ow.fetch_recent(limit=limit_per_source))
            except Exception as e:
                self.log(f"fetch_recent ollama: {e}")

        # 4) DeepSeek Harness
        dw = self.items.get("deepseek")
        if dw and hasattr(dw, "fetch_recent"):
            try:
                out.extend(dw.fetch_recent(limit=limit_per_source))
            except Exception as e:
                self.log(f"fetch_recent deepseek: {e}")

        # 5) Kimi Desktop
        kw = self.items.get("kimi")
        if kw and hasattr(kw, "fetch_recent"):
            try:
                out.extend(kw.fetch_recent(limit=limit_per_source))
            except Exception as e:
                self.log(f"fetch_recent kimi: {e}")

        return out

    def save(self) -> None:
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.state_file)
        except OSError as e:
            self.log(f"watchers save: {e}")
