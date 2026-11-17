"""Локальный архив реплик (SQLite + fastembed семантический поиск) и именованные плейлисты."""

import json
import sqlite3
import threading
import time
from pathlib import Path

ARCHIVE_DB = (Path.home() / ".voice-player-archive.db").resolve()   # полный искомый архив
PLAYLISTS_FILE = Path.home() / ".voice-player-playlists.json"

_archive_lock = threading.Lock()
_embed_holder = {"model": None, "lock": threading.Lock()}
_log_fn = None


def set_logger(fn):
    global _log_fn
    _log_fn = fn


def _log(msg: str):
    if _log_fn is not None:
        try:
            _log_fn(msg)
        except Exception:
            pass


def archive_init():
    try:
        conn = sqlite3.connect(str(ARCHIVE_DB))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS items "
            "(id TEXT PRIMARY KEY, ts REAL, source TEXT, author TEXT, "
            "time_str TEXT, date_str TEXT, text TEXT)"
        )
        conn.commit()
        conn.close()
    except Exception as e:
        _log(f"archive init error: {e}")


def archive_add(item):
    try:
        with _archive_lock:
            conn = sqlite3.connect(str(ARCHIVE_DB), timeout=2.0)
            conn.execute(
                "INSERT OR IGNORE INTO items VALUES (?,?,?,?,?,?,?)",
                (item.id, time.time(), item.source, item.author, item.time_str, item.date_str, item.text),
            )
            conn.commit()
            conn.close()
    except Exception:
        pass


def archive_search(q: str, limit: int = 200):
    try:
        with _archive_lock:
            conn = sqlite3.connect(f"file:{ARCHIVE_DB}?mode=ro", uri=True, timeout=2.0)
            if q.strip():
                rows = conn.execute(
                    "SELECT author, date_str, time_str, text, source FROM items "
                    "WHERE text LIKE ? OR author LIKE ? ORDER BY ts DESC LIMIT ?",
                    (f"%{q}%", f"%{q}%", limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT author, date_str, time_str, text, source FROM items "
                    "ORDER BY ts DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            conn.close()
            return rows
    except Exception as e:
        _log(f"archive search error: {e}")
        return []


def _get_embedder():
    with _embed_holder["lock"]:
        if _embed_holder["model"] is None:
            from fastembed import TextEmbedding

            _embed_holder["model"] = TextEmbedding(
                model_name="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
            )
        return _embed_holder["model"]


def archive_semantic_search(q: str, limit: int = 40):
    import numpy as np

    with _archive_lock:
        conn = sqlite3.connect(str(ARCHIVE_DB), timeout=5.0)
        conn.execute("CREATE TABLE IF NOT EXISTS emb (id TEXT PRIMARY KEY, vec BLOB)")
        rows = conn.execute(
            "SELECT i.id,i.author,i.date_str,i.time_str,i.text,i.source,e.vec "
            "FROM items i LEFT JOIN emb e ON e.id=i.id "
            "ORDER BY i.ts DESC LIMIT 3000"
        ).fetchall()
        conn.commit()
        conn.close()
    rows = [r for r in rows if (r[4] or "").strip()]
    if not rows:
        return []
    model = _get_embedder()
    missing = [(r[0], r[4]) for r in rows if r[6] is None]
    new_vecs = {}
    if missing:
        embs = list(model.embed([t for _, t in missing]))
        for (iid, _), v in zip(missing, embs):
            new_vecs[iid] = np.asarray(v, dtype=np.float32)
        with _archive_lock:
            conn = sqlite3.connect(str(ARCHIVE_DB), timeout=5.0)
            conn.executemany(
                "INSERT OR REPLACE INTO emb VALUES (?,?)",
                [(iid, new_vecs[iid].tobytes()) for iid in new_vecs],
            )
            conn.commit()
            conn.close()
    qv = np.asarray(list(model.embed([q]))[0], dtype=np.float32)
    qn = qv / (np.linalg.norm(qv) + 1e-9)
    scored = []
    for r in rows:
        vec = new_vecs.get(r[0])
        if vec is None and r[6] is not None:
            vec = np.frombuffer(r[6], dtype=np.float32)
        if vec is None:
            continue
        sim = float(np.dot(qn, vec / (np.linalg.norm(vec) + 1e-9)))
        scored.append((sim, r))
    scored.sort(key=lambda x: -x[0])
    return [(r[1], r[2], r[3], r[4], r[5]) for _, r in scored[:limit]]


def playlists_load() -> dict:
    try:
        return json.loads(PLAYLISTS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def playlists_save(data: dict):
    try:
        PLAYLISTS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        _log(f"playlists save error: {e}")
