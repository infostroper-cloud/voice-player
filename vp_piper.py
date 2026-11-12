"""Локальная озвучка через Piper: работает на этом компьютере, без сети и без оплаты.

Русские голоса Piper не умеют читать латиницу, поэтому перед озвучкой она переводится
в русскую запись (`speakable`). Текст на экране при этом не меняется: в тайминги идёт исходный.
"""
import importlib.util
import re
import threading
import urllib.request
from pathlib import Path

VOICE_PREFIX = "piper:"
VOICES_DIR = Path.home() / ".voice-player" / "voices"
HF_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main/ru/ru_RU/{name}/medium/{model}.onnx{ext}"
SENTENCE_PAUSE_S = 0.22
MP3_BITRATE = 64

# подпись в списке голосов -> id
VOICE_LABELS = {
    "Денис — на компьютере (муж)": VOICE_PREFIX + "ru_RU-denis-medium",
    "Дмитрий — на компьютере (муж)": VOICE_PREFIX + "ru_RU-dmitri-medium",
    "Руслан — на компьютере (муж)": VOICE_PREFIX + "ru_RU-ruslan-medium",
    "Ирина — на компьютере (жен)": VOICE_PREFIX + "ru_RU-irina-medium",
}
DEFAULT_VOICE = VOICE_PREFIX + "ru_RU-denis-medium"

# Термины, которые по правилам звучали бы неверно. Ключ в нижнем регистре.
TERMS = {
    "claude": "клод", "anthropic": "антропик", "openai": "оупен эй ай", "chatgpt": "чат джи пи ти",
    "gpt": "джи пи ти", "gemini": "джемини", "cursor": "курсор", "codex": "кодекс", "copilot": "копайлот",
    "windsurf": "виндсёрф", "cline": "клайн", "whisper": "виспер", "piper": "пайпер", "silero": "силеро",
    "qwen": "квен", "edge": "эдж", "github": "гитхаб", "gitlab": "гитлаб", "git": "гит",
    "code": "код", "commit": "коммит", "push": "пуш", "pull": "пул", "merge": "мёрдж", "branch": "бранч",
    "main": "мейн", "origin": "ориджин", "repo": "репо", "hook": "хук", "hooks": "хуки",
    "python": "пайтон", "javascript": "джаваскрипт", "typescript": "тайпскрипт", "java": "джава",
    "node": "нод", "react": "реакт", "next": "некст", "tauri": "таури", "docker": "докер",
    "linux": "линукс", "windows": "виндоус", "mac": "мак", "macos": "мак оэс", "microsoft": "майкрософт",
    "google": "гугл", "azure": "азур", "yandex": "яндекс", "telegram": "телеграм", "notion": "ноушен",
    "readme": "ридми", "json": "джейсон", "yaml": "ямл", "html": "эйч ти эм эль", "css": "си эс эс",
    "sql": "эс кью эль", "api": "эй пи ай", "url": "ю ар эль", "cpu": "си пи ю", "gpu": "джи пи ю",
    "ui": "ю ай", "ux": "ю икс", "ai": "эй ай", "mp3": "эм пи три", "wav": "вав", "pdf": "пи ди эф",
    "server": "сервер", "error": "эррор", "file": "файл", "the": "зе", "a": "э", "an": "эн", "of": "оф",
    "to": "ту", "and": "энд", "is": "из", "are": "ар", "you": "ю", "your": "ёр", "for": "фор", "on": "он",
    "in": "ин", "it": "ит", "this": "зис", "that": "зэт", "with": "виз", "please": "плиз", "check": "чек",
    "logs": "логи", "log": "лог", "token": "токен", "test": "тест", "tests": "тесты", "web": "веб",
    "site": "сайт", "app": "апп", "py": "пай", "js": "джи эс", "ts": "ти эс", "md": "эм ди",
}

LETTERS = {
    "a": "эй", "b": "би", "c": "си", "d": "ди", "e": "и", "f": "эф", "g": "джи", "h": "эйч", "i": "ай",
    "j": "джей", "k": "кей", "l": "эль", "m": "эм", "n": "эн", "o": "оу", "p": "пи", "q": "кью", "r": "ар",
    "s": "эс", "t": "ти", "u": "ю", "v": "ви", "w": "дабл ю", "x": "икс", "y": "уай", "z": "зед",
}

# Порядок важен: сначала длинные сочетания.
_RULES = [
    ("tion", "шн"), ("sion", "жн"), ("ight", "айт"), ("ough", "оу"), ("sch", "ск"), ("tch", "ч"),
    ("igh", "ай"), ("ing", "инг"), ("sh", "ш"), ("ch", "ч"), ("th", "т"), ("ph", "ф"), ("ck", "к"),
    ("qu", "кв"), ("wh", "в"), ("kn", "н"), ("ee", "и"), ("ea", "и"), ("oo", "у"), ("ou", "ау"),
    ("ow", "оу"), ("ai", "ей"), ("ay", "ей"), ("ey", "и"), ("oi", "ой"), ("oy", "ой"), ("au", "о"),
    ("aw", "о"), ("er", "ер"), ("ll", "л"), ("ss", "с"), ("tt", "т"), ("pp", "п"), ("mm", "м"), ("nn", "н"),
    ("x", "кс"), ("j", "дж"), ("w", "в"), ("h", "х"), ("a", "а"), ("b", "б"), ("d", "д"), ("e", "е"),
    ("f", "ф"), ("i", "и"), ("k", "к"), ("l", "л"), ("m", "м"), ("n", "н"), ("o", "о"), ("p", "п"),
    ("q", "к"), ("r", "р"), ("s", "с"), ("t", "т"), ("u", "а"), ("v", "в"), ("z", "з"),
]
_MAGIC_E = {"a": "ей", "e": "и", "i": "ай", "o": "оу", "u": "ю", "y": "ай"}


def _spell(word: str) -> str:
    return " ".join(LETTERS[c] for c in word.lower() if c in LETTERS)


def _translit_word(word: str) -> str:
    """Английское слово -> приблизительная русская запись."""
    w = word.lower()
    if w in TERMS:
        return TERMS[w]
    if len(w) == 1 or not re.search(r"[aeiouy]", w):
        return _spell(w)
    # Немая e на конце: make -> мейк, file -> файл
    m = re.fullmatch(r"(.*?)([aeiouy])([bcdfgklmnprstvz])e", w)
    if m and not re.search(r"[aeiouy]", m.group(1)[-1:]):
        return _translit_word(m.group(1)) + _MAGIC_E[m.group(2)] + _translit_word(m.group(3))
    out, i = [], 0
    while i < len(w):
        ch = w[i]
        nxt = w[i + 1] if i + 1 < len(w) else ""
        if ch == "c":
            out.append("с" if nxt in ("e", "i", "y") else "к"); i += 1; continue
        if ch == "g":
            out.append("дж" if nxt in ("e", "i", "y") and i else "г"); i += 1; continue
        if ch == "y":
            out.append("й" if i == 0 else "и"); i += 1; continue
        for src, dst in _RULES:
            if w.startswith(src, i):
                out.append(dst); i += len(src); break
        else:
            i += 1
    return "".join(out)


def _say_token(tok: str) -> str:
    """Токен из латиницы и цифр, без разделителей."""
    low = tok.lower()
    if low in TERMS:
        return TERMS[low]
    if tok.isdigit():
        return tok
    # Хеш коммита и подобное читать незачем
    if len(tok) >= 7 and re.fullmatch(r"[0-9a-f]+", low) and re.search(r"\d", low) and re.search(r"[a-f]", low):
        return "хэш"
    if re.search(r"\d", tok):                              # v2, h264: буквы и цифры по отдельности
        return " ".join(_say_token(p) for p in re.findall(r"\d+|[A-Za-z]+", tok))
    if tok.isupper() and len(tok) <= 5:                    # сокращение: по буквам
        return _spell(tok)
    parts = re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+", tok)   # camelCase
    if len(parts) > 1:
        return " ".join(_say_token(p) for p in parts)
    return _translit_word(tok)


_URL_RE = re.compile(r"\b(?:https?://|www\.)\S+", re.I)
_LATIN_RUN_RE = re.compile(r"[A-Za-z0-9]*[A-Za-z][A-Za-z0-9]*(?:[._/\\:\-+#][A-Za-z0-9]+)*")
_SEPARATORS = {".": " точка ", "/": " слэш ", "\\": " слэш ", ":": " двоеточие ", "+": " плюс ", "#": " решётка "}


def speakable(text: str) -> str:
    """Заменить латиницу на русскую запись. Кириллицу, числа и знаки препинания не трогает."""
    text = _URL_RE.sub("ссылка", text)

    def repl(m):
        out = []
        for piece in re.split(r"([._/\\:\-+#])", m.group(0)):
            if not piece:
                continue
            if piece in ("-", "_"):
                out.append(" ")
            elif piece in _SEPARATORS:
                out.append(_SEPARATORS[piece])
            else:
                out.append(_say_token(piece))
        return re.sub(r" +", " ", "".join(out)).strip()

    return _LATIN_RUN_RE.sub(repl, text)


def split_sentences(text: str) -> list:
    """Предложения и строки; исходный текст каждого сохраняется для подсветки."""
    out = []
    for line in text.splitlines():
        for s in re.split(r"(?<=[.!?…])\s+", line):
            s = s.strip()
            if re.search(r"\w", s):
                out.append(s)
    return out


def is_local(voice: str) -> bool:
    return bool(voice) and voice.startswith(VOICE_PREFIX)


def is_available() -> bool:
    # Без импорта: библиотека голоса тяжёлая, а эта проверка идёт при запуске окна
    return all(importlib.util.find_spec(m) is not None for m in ("piper", "lameenc"))


def _ensure_model(voice: str) -> Path:
    model = voice[len(VOICE_PREFIX):]
    path = VOICES_DIR / f"{model}.onnx"
    name = model.split("-")[1]
    VOICES_DIR.mkdir(parents=True, exist_ok=True)
    for target, ext in ((path, ""), (Path(str(path) + ".json"), ".json")):
        if target.exists() and target.stat().st_size > 0:
            continue
        tmp = target.with_name(target.name + ".part")
        urllib.request.urlretrieve(HF_URL.format(name=name, model=model, ext=ext), tmp)
        tmp.replace(target)
    return path


_lock = threading.Lock()      # модель общая: части одной реплики озвучиваются по очереди
_loaded = {}


def _voice_obj(voice: str):
    if voice not in _loaded:
        from piper import PiperVoice
        _loaded.clear()       # держим в памяти один голос
        _loaded[voice] = PiperVoice.load(str(_ensure_model(voice)))
    return _loaded[voice]


def preload(voice: str) -> None:
    """Загрузить голос заранее, чтобы первая реплика не ждала несколько секунд."""
    with _lock:
        _voice_obj(voice)


def synthesize(text: str, voice: str, mp3_path: Path) -> list:
    """Записать mp3, вернуть тайминги предложений [(начало, конец, исходный текст)]."""
    import lameenc
    marks, pcm = [], bytearray()
    t, rate = 0.0, 22050
    with _lock:
        engine = _voice_obj(voice)
        for sentence in split_sentences(text):
            start = t
            for chunk in engine.synthesize(speakable(sentence)):
                rate = chunk.sample_rate
                pcm += chunk.audio_int16_bytes
                t += len(chunk.audio_int16_bytes) / 2 / rate
            if t > start:
                marks.append((start, t, sentence))
                gap = int(SENTENCE_PAUSE_S * rate)
                pcm += b"\0\0" * gap
                t += gap / rate
    if not pcm:
        raise RuntimeError("в тексте нечего озвучить")
    enc = lameenc.Encoder()
    enc.set_bit_rate(MP3_BITRATE)
    enc.set_in_sample_rate(rate)
    enc.set_channels(1)
    enc.set_quality(2)
    tmp = mp3_path.with_name(mp3_path.name + ".tmp")
    tmp.write_bytes(bytes(enc.encode(bytes(pcm))) + bytes(enc.flush()))
    tmp.replace(mp3_path)
    return marks
