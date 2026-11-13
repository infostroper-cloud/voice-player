"""Мост «нейросеть → VoicePlayer»: хук инструмента запускает этот скрипт после ответа.

Запуск: python vp_feed.py <инструмент>   (antigravity, claude-code, codex, cursor, gemini)
На вход (stdin) — JSON от инструмента. Текст ответа берём из поля, которое отдаёт
инструмент; если его нет — из последней реплики в файле переписки (transcript_path / transcriptPath).
Всегда завершается кодом 0 и печатает валидный JSON — чтобы никак не мешать работе нейросети.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path
from uuid import uuid4

LABELS = {
    "antigravity": "Antigravity",
    "claude-code": "Claude Code",
    "claude": "Claude Code",
    "codex": "Codex",
    "cursor": "Cursor",
    "gemini": "Gemini",
    "openclaw": "OpenClaw",
    "opencode": "OpenCode",
    "hermes-agent": "Hermes Agent",
    "hermes": "Hermes Agent",
    "hermes-desktop": "Hermes Desktop",
    "droid": "Droid",
    "pi": "Pi",
    "cline": "Cline",
    "copilot-cli": "Copilot CLI",
    "oh-my-pi": "Oh My Pi",
    "deepseek": "DeepSeek Harness",
    "qwen": "Qwen Code",
    "qwen-code": "Qwen Code",
    "ollama": "Ollama",
    "perplexity": "Perplexity",
    "comet": "Comet",
    "vscode": "VS Code",
    "aider": "Aider",
    "windsurf": "Windsurf",
    "terminal": "Terminal",
}
TEXT_FIELDS = ("last_assistant_message", "text", "prompt_response", "response")
SESSION_FIELDS = ("session_id", "conversation_id", "conversationId", "thread_id")
TRANSCRIPT_FIELDS = ("transcript_path", "transcriptPath")
PROMPT_FIELDS = ("lastUserInput", "last_user_message", "prompt")
TAIL_BYTES = 2_000_000


def _clean_user_req(s: str) -> str:
    s = s or ""
    a, b = s.find("<USER_REQUEST>"), s.find("</USER_REQUEST>")
    if a != -1 and b != -1:
        s = s[a + len("<USER_REQUEST>"): b]
    return s.strip()


def _text_from_transcript(path: str, tool: str = "") -> tuple[str, str]:
    """(Последний ответ ассистента, последний вопрос пользователя) из JSONL-переписки."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - TAIL_BYTES))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return "", ""
    answer = ""
    prompt = ""
    for line in reversed(lines):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if tool == "antigravity" or rec.get("type") in ("PLANNER_RESPONSE", "USER_INPUT"):
            kind = rec.get("type")
            if kind == "PLANNER_RESPONSE" and not answer and not rec.get("tool_calls"):
                c = str(rec.get("content") or "").strip()
                if c:
                    answer = c
            elif kind == "USER_INPUT" and rec.get("source") in ("USER_EXPLICIT", "USER"):
                prompt = _clean_user_req(rec.get("content") or "")
                if answer:
                    break
            continue
        if tool == "codex":
            payload = rec.get("payload") or {}
            if rec.get("type") != "response_item":
                continue
            if payload.get("role") == "user":
                parts = [
                    part.get("text", "") for part in payload.get("content", [])
                    if isinstance(part, dict) and isinstance(part.get("text"), str)
                ]
                prompt = "\n".join(p for p in parts if p).strip()
                if answer:
                    break
                continue
            if not answer and (payload.get("type"), payload.get("role"), payload.get("phase")) == (
                "message", "assistant", "final_answer"
            ):
                answer = "\n".join(
                    part["text"] for part in payload.get("content", [])
                    if isinstance(part, dict) and isinstance(part.get("text"), str)
                )
            continue
        if rec.get("type") == "user" and not prompt:
            msg = rec.get("message") or {}
            content = msg.get("content")
            if isinstance(content, str):
                prompt = content.strip()
            elif isinstance(content, list):
                prompt = "\n".join(
                    b.get("text", "") for b in content
                    if isinstance(b, dict) and isinstance(b.get("text"), str)
                ).strip()
            if answer:
                break
            continue
        if rec.get("type") != "assistant":
            continue
        if not answer:
            parts = [b.get("text", "") for b in (rec.get("message") or {}).get("content") or []
                     if isinstance(b, dict) and b.get("type") == "text"]
            if any(parts):
                answer = "\n".join(p for p in parts if p)
    return answer.strip(), prompt.strip()


def extract_full(data: dict, tool: str = "") -> tuple[str, str, str]:
    text = next((data[k] for k in TEXT_FIELDS if isinstance(data.get(k), str) and data[k].strip()), "")
    prompt = next((_clean_user_req(data[k]) for k in PROMPT_FIELDS
                   if isinstance(data.get(k), str) and data[k].strip()), "")
    tpath = next((str(data[k]) for k in TRANSCRIPT_FIELDS if data.get(k)), "")
    if tpath and (not text or not prompt):
        t_ans, t_prompt = _text_from_transcript(tpath, tool)
        if not text:
            text = t_ans
        if not prompt:
            prompt = t_prompt
    session = next((str(data[k]) for k in SESSION_FIELDS if data.get(k)), "")
    return text.strip(), session[:8], prompt[:300]


def extract(data: dict, tool: str = "") -> tuple:
    text, session, _prompt = extract_full(data, tool)
    return text, session


def _active_port() -> str:
    env_p = os.environ.get("VP_HTTP_PORT")
    if env_p:
        return env_p
    try:
        pfile = Path.home() / ".voice-player" / "port.txt"
        if pfile.exists():
            val = pfile.read_text(encoding="utf-8").strip()
            if val.isdigit():
                return val
    except Exception:
        pass
    return "3737"


def send(tool: str, text: str, session: str, prompt: str = "") -> None:
    port = _active_port()
    label = LABELS.get(tool, tool)
    short_q = " ".join(prompt.split())
    if len(short_q) > 34:
        short_q = short_q[:33].rstrip() + "…"
    author = f"{label} · {short_q}" if short_q else (f"{label}/{session}" if session else label)
    body = json.dumps({"source": tool, "author": author,
                       "session": session, "prompt": prompt,
                       "time": time.strftime("%H:%M"),
                       "text": text}, ensure_ascii=False).encode("utf-8")
    if tool in ("codex", "antigravity"):
        inbox = Path.home() / ".voice-player" / "codex-inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        entry = inbox / f"{time.time_ns()}-{uuid4().hex}.json"
        pending = entry.with_suffix(".tmp")
        pending.write_bytes(body)
        pending.replace(entry)
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/enqueue", data=body,
                                     headers={"Content-Type": "application/json; charset=utf-8"})
        # локальный адрес — мимо системного прокси/VPN
        urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=2.5).read()
    except Exception:
        if tool not in ("codex", "antigravity"):
            raise


def main(argv=None) -> int:
    argv = sys.argv if argv is None else argv
    tool = argv[1] if len(argv) > 1 else "ai"
    status = "no final text"
    try:
        raw = sys.stdin.buffer.read().decode("utf-8-sig", "replace").strip()
        if raw:
            text, session, prompt = "", "", ""
            try:
                data = json.loads(raw)
                if isinstance(data, dict):
                    text, session, prompt = extract_full(data, tool)
                elif isinstance(data, str):
                    text = data.strip()
            except Exception:
                # Обычный текст из пайпа консоли (например: echo 'Привет' | vp-say)
                text = raw
            if text:
                send(tool, text, session, prompt)
                status = "queued"
    except Exception as error:
        status = f"failed: {type(error).__name__}"
        if tool == "codex":
            print(f"VoicePlayer: {status}", file=sys.stderr)
    if tool in ("codex", "antigravity"):
        try:
            log = Path.home() / ".voice-player" / "codex-hook.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open("a", encoding="utf-8") as stream:
                stream.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{tool}] {status}\n")
        except OSError:
            pass
    if sys.stdout:
        if tool == "antigravity":
            print('{"decision": "allow"}')
        else:
            print("{}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
