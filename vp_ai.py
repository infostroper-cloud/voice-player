# -*- coding: utf-8 -*-
"""AI-саммари, черновики ответов, извлечение сущностей и авто-теги.

Цепочка провайдеров (по умолчанию auto):
1. Настроенный OpenAI-совместимый API (OpenAI / DeepSeek / OpenRouter / Gemini / custom).
2. Локальная Ollama (127.0.0.1:11434 — бесплатно и офлайн).
3. Локальный CLI (claude / codex / gemini).
4. Улучшенный офлайн-экстрактивный пересказ (_extractive_summary).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.request
from pathlib import Path
from typing import Optional

_log = lambda msg: None

PROVIDER: str = "auto"  # auto | api | ollama | cli | offline
API_URL: str = ""
API_KEY: str = ""
API_MODEL: str = ""
OLLAMA_URL: str = "http://127.0.0.1:11434"
OLLAMA_MODEL: str = ""
UI_LANG: str = "ru"

_STOP_WORDS = {
    "что", "как", "это", "для", "или", "при", "так", "уже", "если", "того", "чтобы",
    "было", "быть", "все", "всё", "ещё", "еще", "только", "может", "можно", "нужно",
    "надо", "очень", "через", "после", "перед", "между", "когда", "где", "куда", "откуда",
    "the", "and", "for", "that", "this", "with", "from", "have", "will", "your", "are",
    "was", "were", "been", "being", "into", "about", "there", "their", "then", "than",
}


def set_logger(fn) -> None:
    """Установить функцию логирования (обычно app.log_line)."""
    global _log
    _log = fn


def log_line(msg: str) -> None:
    _log(msg)


def configure(
    *,
    provider: Optional[str] = None,
    api_url: Optional[str] = None,
    api_key: Optional[str] = None,
    api_model: Optional[str] = None,
    ollama_url: Optional[str] = None,
    ollama_model: Optional[str] = None,
    ui_lang: Optional[str] = None,
) -> None:
    """Настроить провайдера AI из настроек приложения."""
    global PROVIDER, API_URL, API_KEY, API_MODEL, OLLAMA_URL, OLLAMA_MODEL, UI_LANG
    if provider is not None:
        PROVIDER = (provider or "auto").strip().lower()
    if api_url is not None:
        API_URL = api_url.strip()
    if api_key is not None:
        API_KEY = api_key.strip()
    if api_model is not None:
        API_MODEL = api_model.strip()
    if ollama_url is not None:
        OLLAMA_URL = (ollama_url or "http://127.0.0.1:11434").strip().rstrip("/")
    if ollama_model is not None:
        OLLAMA_MODEL = ollama_model.strip()
    if ui_lang is not None:
        UI_LANG = (ui_lang or "ru").strip().lower()


def _read_secrets_env() -> dict[str, str]:
    """Прочитать ключи из ~/.voice-player-secrets.env (без перезаписи os.environ)."""
    out: dict[str, str] = {}
    p = Path.home() / ".voice-player-secrets.env"
    if not p.is_file():
        return out
    try:
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    return out


def _resolve_api_config() -> tuple[str, str, str]:
    """Вернуть (url, key, model) для OpenAI-совместимого API."""
    if API_KEY:
        url = API_URL or "https://api.openai.com/v1/chat/completions"
        if url.endswith("/v1"):
            url = url + "/chat/completions"
        elif not url.endswith("/chat/completions") and "chat/completions" not in url:
            url = url.rstrip("/") + "/v1/chat/completions"
        model = API_MODEL or "gpt-4o-mini"
        return url, API_KEY, model

    secrets = _read_secrets_env()
    env_get = lambda k: os.environ.get(k) or secrets.get(k, "")
    presets = [
        ("DEEPSEEK_API_KEY", "https://api.deepseek.com/v1/chat/completions", "deepseek-chat"),
        ("OPENAI_API_KEY", "https://api.openai.com/v1/chat/completions", "gpt-4o-mini"),
        ("OPENROUTER_API_KEY", "https://openrouter.ai/api/v1/chat/completions", "google/gemini-2.0-flash-001"),
        (
            "GEMINI_API_KEY",
            "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
            "gemini-2.0-flash",
        ),
    ]
    for env_name, def_url, def_model in presets:
        k = env_get(env_name)
        if k:
            return (API_URL or def_url), k, (API_MODEL or def_model)
    return "", "", ""


def _call_openai_compat(prompt: str, max_tokens: int = 450, system_msg: str = "") -> str:
    url, key, model = _resolve_api_config()
    if not url or not key:
        return ""
    msgs = []
    if system_msg:
        msgs.append({"role": "system", "content": system_msg})
    msgs.append({"role": "user", "content": prompt})
    payload = {
        "model": model,
        "messages": msgs,
        "temperature": 0.3,
        "max_tokens": max_tokens,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
            "User-Agent": "VoicePlayer/1.2",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=35) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        choices = data.get("choices") or []
        if choices:
            msg = choices[0].get("message") or {}
            return str(msg.get("content") or "").strip()
    except Exception as e:
        log_line(f"vp_ai API error ({model}): {e}")
    return ""


def _call_ollama(prompt: str) -> str:
    base = (OLLAMA_URL or "http://127.0.0.1:11434").rstrip("/")
    model = OLLAMA_MODEL
    try:
        if not model:
            with urllib.request.urlopen(f"{base}/api/tags", timeout=1.5) as r:
                tags = json.loads(r.read().decode("utf-8", errors="replace"))
            models = [m.get("name") for m in (tags.get("models") or []) if m.get("name")]
            if not models:
                return ""
            # Предпочитаем лёгкие текстовые модели (не embed)
            non_embed = [m for m in models if "embed" not in m.lower()]
            model = (non_embed or models)[0]
        body = {"model": model, "prompt": prompt, "stream": False}
        if getattr(_call_ollama, "_system_msg", ""):
            body["system"] = _call_ollama._system_msg
        req = urllib.request.Request(
            f"{base}/api/generate",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        return str(data.get("response") or "").strip()
    except Exception:
        return ""


def _call_cli(prompt: str) -> str:
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    candidates = [
        ("claude", ["-p", prompt]),
        ("gemini", ["-p", prompt]),
    ]
    for exe_name, args in candidates:
        exe = shutil.which(exe_name)
        if not exe:
            continue
        try:
            r = subprocess.run(
                [exe, *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=45,
                creationflags=flags,
            )
            out = (r.stdout or "").strip()
            if out and r.returncode == 0:
                return out
        except Exception as e:
            log_line(f"vp_ai CLI ({exe_name}) failed: {e}")
    return ""


def _clean_for_summary(text: str) -> str:
    s = re.sub(r"```[\s\S]*?```", " ", text or "")
    s = re.sub(r"`([^`]+)`", r"\1", s)
    s = re.sub(r"^[#>*\-•]+\s*", "", s, flags=re.MULTILINE)
    s = re.sub(r"\n{2,}", "\n", s)
    return s.strip()


def _extractive_summary(text: str, n: int = 4) -> str:
    """Локальный экстрактивный пересказ по предложениям и строкам без внешних зависимостей."""
    cleaned = _clean_for_summary(text)
    if not cleaned:
        return (text or "").strip()

    # Сначала режем по границам предложений; если предложений мало — дополняем разбивкой по строкам
    raw_units = [u.strip() for u in re.split(r"(?<=[.!?])\s+|\n+", cleaned) if len(u.strip()) > 18]
    if len(raw_units) <= n:
        joined = " ".join(raw_units) if raw_units else cleaned
        return joined[:700].rstrip() + ("…" if len(joined) > 700 else "")

    words = [w for w in re.findall(r"[a-zA-Zа-яА-ЯёЁ0-9]{3,}", cleaned.lower()) if w not in _STOP_WORDS]
    freq: dict[str, int] = {}
    for w in words:
        freq[w] = freq.get(w, 0) + 1

    total = len(raw_units)
    scored_idx: list[tuple[float, int]] = []
    for idx, unit in enumerate(raw_units):
        u_words = [w for w in re.findall(r"[a-zA-Zа-яА-ЯёЁ0-9]{3,}", unit.lower()) if w not in _STOP_WORDS]
        if not u_words:
            continue
        tf = sum(freq.get(w, 0) for w in u_words) / (len(u_words) ** 0.5)
        pos_bonus = 1.25 if idx == 0 else (1.1 if idx == total - 1 else 1.0)
        scored_idx.append((tf * pos_bonus, idx))

    top_indices = {idx for _, idx in sorted(scored_idx, reverse=True)[:n]}
    selected = [raw_units[i] for i in range(total) if i in top_indices]
    result = " • ".join(selected) if len(selected) > 1 and all("\n" in cleaned for _ in [0]) else " ".join(selected)
    if len(result) > 800:
        result = result[:797].rstrip() + "…"
    return result or cleaned[:700]


_LANG_NAMES = {
    "ru": "Russian (русский язык)",
    "en": "English",
    "de": "German (Deutsch)",
    "fr": "French (français)",
    "es": "Spanish (español)",
    "zh": "Chinese (中文)",
    "ja": "Japanese (日本語)",
    "hi": "Hindi (हिन्दी)",
}


def _detect_response_language(text: str) -> tuple[str, str]:
    """Определить язык исходного текста для ответа (если смешанный/код — язык текста с кириллицей -> ru)."""
    s = (text or "").strip()
    cyr = sum(1 for ch in s if "а" <= ch.lower() <= "я" or ch.lower() == "ё")
    if cyr >= 5:
        return "ru", _LANG_NAMES["ru"]
    try:
        import vp_i18n
        code = vp_i18n.detect_text_language(s)
        if code in _LANG_NAMES:
            return code, _LANG_NAMES[code]
    except Exception:
        pass
    code = UI_LANG if UI_LANG in _LANG_NAMES else "ru"
    return code, _LANG_NAMES[code]


def _run_llm(prompt: str, max_tokens: int = 450, system_msg: str = "") -> tuple[str, str]:
    """Выполнить LLM-запрос согласно PROVIDER. Возвращает (text, source_tag)."""
    mode = (PROVIDER or "auto").lower()
    order = (
        ["api", "ollama", "cli"]
        if mode == "auto"
        else [mode]
        if mode in ("api", "ollama", "cli")
        else []
    )
    for step in order:
        if step == "api":
            out = _call_openai_compat(prompt, max_tokens=max_tokens, system_msg=system_msg)
            if out:
                return out, "api"
        elif step == "ollama":
            _call_ollama._system_msg = system_msg
            out = _call_ollama(prompt)
            _call_ollama._system_msg = ""
            if out:
                return out, "ollama"
        elif step == "cli":
            full_p = f"{system_msg}\n\n{prompt}".strip() if system_msg else prompt
            out = _call_cli(full_p)
            if out:
                return out, "cli"
    return "", "offline"


def ai_summary(text: str) -> str:
    """Краткий пересказ на языке исходного текста: API -> Ollama -> CLI -> локальный экстрактивный."""
    src = (text or "").strip()
    if not src:
        return ""
    if len(src) > 100 and (PROVIDER or "auto").lower() != "offline":
        lang_code, lang_name = _detect_response_language(src)
        sys_msg = (
            f"You MUST reply strictly in {lang_name}. Never switch or translate to another language. "
            f"Write concise 3-5 bullet points or sentences with only key facts, no intro."
        )
        prompt = (
            f"Summarize the following text strictly in {lang_name} (3-5 short sentences or bullets, "
            f"only the essence, no intro):\n\n{src[:8000]}"
        )
        if lang_code == "ru":
            sys_msg = (
                "Отвечай СТРОГО на русском языке. Не переходи на английский язык. "
                "Дай краткую выжимку в 3–5 предложений или пунктов без вводных слов."
            )
            prompt = (
                "Перескажи кратко и СТРОГО на русском языке (3–5 предложений или коротких пунктов, "
                "только ключевая суть, без вступлений):\n\n" + src[:8000]
            )
        out, _ = _run_llm(prompt, max_tokens=450, system_msg=sys_msg)
        if out:
            return out
    return _extractive_summary(src, 4)


def extract_entities(text: str):
    """Найти задачи/даты/время/числа/имена для подсветки. Возвращает list[(start,end)]."""
    spans = []
    pats = [
        r"\b\d{1,2}[:.]\d{2}\b",                       # время
        r"\b\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?\b",     # дата
        r"\b\d[\d\s.,]*\b",                            # числа
        r"\b(?:нужно|надо|сделать|дедлайн|срочно|задача|до завтра|сегодня|завтра)\b",
        r"\b[А-ЯЁ][а-яё]{2,}\b",                       # слова с заглавной (имена)
    ]
    for p in pats:
        for m in re.finditer(p, text, flags=re.IGNORECASE):
            spans.append((m.start(), m.end()))
    return spans


def tag_for(text: str) -> str:
    """Авто-тег реплики по содержимому: 🔴 срочное, ✅ задача, ❓ вопрос."""
    t = (text or "").lower()
    if re.search(r"\b(срочно|asap|немедленно|критично|блокер)\b", t):
        return "🔴"
    if re.search(r"\b(нужно|надо|сделать|задача|дедлайн|подготовь|пришли|закрой)\b", t):
        return "✅"
    if "?" in text:
        return "❓"
    return ""


def ai_reply(text: str) -> str:
    """Черновик делового ответа на сообщение через API -> Ollama -> CLI."""
    src = (text or "").strip()
    if not src or (PROVIDER or "auto").lower() == "offline":
        return ""
    prompt = (
        "Напиши короткий деловой ответ по-русски на это сообщение, "
        "только текст ответа без вступлений:\n\n" + src[:6000]
    )
    out, _ = _run_llm(prompt, max_tokens=350)
    return out
