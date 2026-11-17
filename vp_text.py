"""Подготовка и нормализация текста для озвучки, извлечение документов и разбиение на части."""

from __future__ import annotations

import html
import re
import zipfile
from pathlib import Path

from vp_tables import normalize_tables

TTS_FIRST_PART_CHARS = 220   # первая часть (~8–12 с звука): быстрый старт + запас времени для рендера 2-й
TTS_SECOND_PART_CHARS = 380  # вторая часть: плавный разгон буфера без паузы после 1-й части
TTS_PART_CHARS = 600         # остальные части: каждая быстро рендерится без сетевого троттлинга

# словарь произношения: как читать имена/аббревиатуры (заполняется приложением)
_PRONOUNCE: dict = {}

# Встроенная база произношения: английские IT/бизнес-аббревиатуры, бренды и русские сокращения.
# Используется для кириллических текстов, чтобы русский TTS читал термины естественно.
DEFAULT_PRONOUNCE: dict[str, str] = {
    # Русские сокращения
    "ЛМК": "эл-эм-ка", "ОДК": "о-дэ-ка", "ГПР": "гэ-пэ-эр", "КП": "ка-пэ",
    "ТЗ": "тэ-зэ", "ИИ": "и-и", "БД": "бэ-дэ", "ПК": "пэ-ка",
    "ОС": "о-эс", "ПО": "пэ-о", "ТТХ": "тэ-тэ-ха", "НДС": "эн-дэ-эс",
    "ООО": "о-о-о", "ИП": "и-пэ", "СБП": "эс-бэ-пэ", "ФНС": "эф-эн-эс",
    # Английские IT и общие аббревиатуры
    "API": "эй-пи-ай", "SDK": "эс-ди-кей", "CLI": "си-эл-ай", "GUI": "гуи",
    "UI": "ю-ай", "UX": "ю-экс", "HTTP": "эйч-ти-ти-пи", "HTTPS": "эйч-ти-ти-пи-эс",
    "URL": "ю-эр-эл", "URI": "ю-эр-ай", "SQL": "эс-кью-эл", "NoSQL": "ноу-эс-кью-эл",
    "JSON": "джейсон", "YAML": "ямл", "XML": "икс-эм-эл", "HTML": "эйч-ти-эм-эл",
    "CSS": "си-эс-эс", "DOM": "дом", "JWT": "джей-дабл-ю-ти", "OAuth": "о-аут",
    "CI/CD": "си-ай си-ди", "LLM": "эл-эл-эм", "TTS": "ти-ти-эс", "STT": "эс-ти-ти",
    "RAG": "рэг", "IDE": "ай-ди-и", "CPU": "си-пи-ю", "GPU": "джи-пи-ю",
    "RAM": "рам", "SSD": "эс-эс-ди", "HDD": "эйч-ди-ди", "DNS": "ди-эн-эс",
    "SSH": "эс-эс-эйч", "TLS": "ти-эл-эс", "SSL": "эс-эс-эл", "VPN": "ви-пи-эн",
    "VPS": "ви-пи-эс", "SaaS": "саас", "CRM": "си-эр-эм", "ERP": "и-эр-пи",
    "KPI": "кей-пи-ай", "ROI": "ар-о-ай", "MVP": "эм-ви-пи", "B2B": "би-ту-би",
    "B2C": "би-ту-си", "PR": "пи-ар", "MR": "эм-эр", "QA": "кью-эй",
    "PM": "пи-эм", "CEO": "си-и-о", "CTO": "си-ти-о", "HR": "эйч-ар",
    "PDF": "пи-ди-эф", "CSV": "си-эс-ви", "NPM": "эн-пи-эм", "PIP": "пип",
    "WSL": "дабл-ю-эс-эл", "USB": "ю-эс-би", "AI": "эй-ай", "ML": "эм-эл",
    "OS": "о-эс", "DB": "ди-би", "IP": "ай-пи", "TCP": "ти-си-пи",
    "UDP": "ю-ди-пи", "REST": "рест", "gRPC": "джи-ар-пи-си", "GraphQL": "граф-кью-эл",
    "CRUD": "крад", "TTL": "ти-ти-эл", "UTC": "ю-ти-си", "UTF-8": "ю-ти-эф восемь",
    "EOF": "и-о-эф", "stdin": "стдин", "stdout": "стдаут", "stderr": "стдэрр",
    "regex": "реджекс", "async": "асинк", "await": "эвейт", "localhost": "локалхост",
    "Wi-Fi": "вай-фай", "HDMI": "эйч-ди-эм-ай", "FAQ": "эф-эй-кью",
    # Популярные языки, фреймворки, нейросети и сервисы
    "Python": "Пайтон", "TypeScript": "Тайпскрипт", "JavaScript": "Джаваскрипт",
    "React": "Реакт", "Vue": "Вью", "Angular": "Ангуляр", "Next.js": "Некст джи-эс",
    "Node.js": "Ноуд джи-эс", "Docker": "Докер", "Kubernetes": "Кубернетес",
    "PostgreSQL": "Постгрес", "Redis": "Редис", "MongoDB": "Монго-ди-би",
    "SQLite": "Эс-кью-лайт", "Nginx": "Энджинкс", "GitHub": "Гитхаб", "GitLab": "Гитлаб",
    "Supabase": "Супабейс", "Firebase": "Файрбейс", "Whisper": "Виспер",
    "Kokoro": "Кокоро", "Piper": "Пайпер", "Ollama": "Оллама", "DeepSeek": "Дипсик",
    "Claude": "Клод", "Gemini": "Джемини", "ChatGPT": "Чат-джи-пи-ти",
    "OpenAI": "Оупен-эй-ай", "Anthropic": "Антропик", "Speechify": "Спичифай",
    "Antigravity": "Антигравити", "Cursor": "Курсор", "Codex": "Кодекс",
    "Windows": "Виндоус", "Linux": "Линукс", "macOS": "мак-о-эс",
    "Android": "Андроид", "iOS": "ай-о-эс", "YouTube": "Ютуб",
    "Telegram": "Телеграм", "WhatsApp": "Вотсап", "Google": "Гугл",
    "Microsoft": "Майкрософт", "Apple": "Эппл", "NVIDIA": "Энвидиа",
    "PyTorch": "Пайторч", "ONNX": "Оникс", "FastAPI": "Фаст-эй-пи-ай",
    "Django": "Джанго", "Flask": "Фласк", "Tailwind": "Тейлвинд",
    "Vite": "Вит", "Webpack": "Вебпак", "Nuitka": "Нуитка", "Cython": "Сайтон",
}


def _slash_to_or(t: str) -> str:
    """Косую черту между словами читаем как «или»; между числами (дата/дробь) оставляем."""
    t = re.sub(r"\bи\s*/\s*или\b", "и или", t, flags=re.IGNORECASE)   # частый оборот «и/или»
    out, n = [], len(t)
    for i, ch in enumerate(t):
        if ch == "/":
            prev = t[i - 1] if i > 0 else ""
            nxt = t[i + 1] if i + 1 < n else ""
            is_path = prev.isascii() and nxt.isascii() and prev.isalnum() and nxt.isalnum()  # origin/main, 3/4
            out.append("/" if is_path else " или ")
        else:
            out.append(ch)
    return "".join(out)


def _apply_pronounce(t: str, pronounce: dict | None = None) -> str:
    d = _PRONOUNCE if pronounce is None else pronounce
    if not d:
        return t
    has_cyr = any("а" <= ch.lower() <= "я" or ch.lower() == "ё" for ch in t)
    for k in sorted(d, key=len, reverse=True):   # длинные ключи раньше
        v = d[k]
        if not k.strip():
            continue
        # Не кириллизуем чисто английский/иностранный текст, если ключ латинский, а значение кириллическое
        if not has_cyr and k.isascii() and any("а" <= ch.lower() <= "я" or ch.lower() == "ё" for ch in v):
            continue
        t = re.sub(r"(?<!\w)" + re.escape(k) + r"(?!\w)", v, t, flags=re.IGNORECASE)
    return t


# распространённые сокращения → полные слова (иначе читаются буквами/мусором).
# Однозначные только: «г.» (год/город), «с.»/«р.» намеренно НЕ трогаем.
_ABBREV = [
    (r"\bи\s+т\.?\s*д\.?",   "и так далее"),
    (r"\bи\s+т\.?\s*п\.?",   "и тому подобное"),
    (r"\bи\s+др\.",          "и другие"),
    (r"\bт\.\s*е\.",         "то есть"),
    (r"\bт\.\s*к\.",         "так как"),
    (r"\bв\s+т\.\s*ч\.",     "в том числе"),
    (r"\bруб\.",             "рублей"),
    (r"\bкоп\.",             "копеек"),
    (r"\bшт\.",              "штук"),
    (r"\bтыс\.",             "тысяч"),
    (r"\bмлн\b\.?",          "миллионов"),
    (r"\bмлрд\b\.?",         "миллиардов"),
    (r"\bкв\.\s*м\b",        "квадратных метров"),
    (r"\bнапр\.",            "например"),
    (r"\bп\.\s*(\d)",        r"пункт \1"),
]

# единицы измерения — раскрываем ТОЛЬКО после числа (иначе покалечим обычные слова).
# Порядок важен: более длинные/составные раньше, голая «м» — в самом конце.
_UNITS = [
    (r"км/ч",  "километров в час"),
    (r"м/с",   "метров в секунду"),
    (r"тыс\.?\s*руб", "тысяч рублей"),
    (r"кВт",   "киловатт"),
    (r"кг",    "килограммов"),
    (r"мм",    "миллиметров"),
    (r"см",    "сантиметров"),
    (r"дм",    "дециметров"),
    (r"км",    "километров"),
    (r"мл",    "миллилитров"),
    (r"мин",   "минут"),
    (r"сек",   "секунд"),
    (r"шт",    "штук"),
    (r"чел",   "человек"),
    (r"ГБ",    "гигабайт"),
    (r"МБ",    "мегабайт"),
    (r"КБ",    "килобайт"),
    (r"ТБ",    "терабайт"),
    (r"Гц",    "герц"),
    (r"°C|°С",  "градусов Цельсия"),
    (r"т",     "тонн"),
    (r"л",     "литров"),
    (r"ч",     "часов"),
    (r"м",     "метров"),
]


def _expand_abbrev(t: str) -> str:
    for pat, rep in _ABBREV:
        t = re.sub(pat, rep, t, flags=re.IGNORECASE)
    # площадь/объём раньше голых единиц (чтобы «5 м²» не стало «5 метров ²»)
    t = re.sub(r"(\d)\s*м²|(\d)\s*м2\b", lambda m: f"{m.group(1) or m.group(2)} квадратных метров ", t)
    t = re.sub(r"(\d)\s*м³|(\d)\s*м3\b", lambda m: f"{m.group(1) or m.group(2)} кубических метров ", t)
    for pat, rep in _UNITS:                         # единицы только после числа
        t = re.sub(rf"(\d)\s*(?:{pat})\b", rf"\1 {rep} ", t)
    t = t.replace("₽", " рублей ").replace("°", " градусов ")
    return t


def _structure_for_speech(t: str) -> str:
    """Разбить текст на смысловые предложения, чтобы нейроголос ловил интонацию и паузы.
    Каждая строка-мысль (пункт списка, заголовок) получает завершающий знак — иначе
    модель читает всё одной монотонной фразой. Переносы строк сохраняем для показа."""
    out = []
    for line in t.split("\n"):
        s = line.strip()
        if not s:
            out.append("")
            continue
        if re.fullmatch(r"[-=_·•*–—\s]{2,}", s):       # горизонтальный разделитель — выкинуть
            continue
        s = re.sub(r"^[\-–—•▪◦·]\s+", "", s)           # маркер списка в начале строки убрать
        if not s:
            continue
        if s[-1] not in ".!?…:,;":                      # нет завершающего знака → закрыть мысль
            s += "."
        out.append(s)
    return "\n".join(out)


def _strip_noise_tokens(t: str) -> str:
    """Служебные токены, которые голос читает по буквам/мусором, заменить на смысл или убрать:
    пути к файлам, UUID, git-хэши, @упоминания. Запускать ПОСЛЕ замены URL на «ссылка»."""
    # пути к файлам: Windows (C:\...) и явные POSIX (/usr,/var,/home,~/...) → «путь к файлу»
    t = re.sub(r"[A-Za-z]:\\[^\s]+", " путь к файлу ", t)
    t = re.sub(r"(?:~|/(?:usr|var|etc|home|tmp|opt|mnt|root))/[^\s]+", " путь к файлу ", t)
    # UUID → «идентификатор»
    t = re.sub(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b",
               " идентификатор ", t)
    # git-хэш / длинная hex-строка (минимум 7, обязательно с цифрой) → «хэш»
    t = re.sub(r"\b(?=[0-9a-f]*\d)[0-9a-f]{7,40}\b", " хэш ", t)
    # @упоминание → просто имя (без знака собачки, который читается «эт»)
    t = re.sub(r"(?<!\w)@([A-Za-zА-Яа-яёЁ_][\w]*)", r"\1", t)
    return t


def _smart_normalize(t: str) -> str:
    """Привести текст к «человеческому» произношению: символы → смысл, мусор убрать."""
    t = _apply_pronounce(t)                                    # словарь произношения
    t = re.sub(r"https?://\S+", " ссылка ", t)                 # URL → «ссылка» (до чистки путей)
    t = _strip_noise_tokens(t)                                 # хэши/пути/UUID/упоминания → смысл или прочь
    t = _expand_abbrev(t)                                      # сокращения → полные слова
    # эмодзи и пиктограммы (в т.ч. стрелки, значки) — убрать, они читаются мусором
    t = re.sub("["
               "\U0001F300-\U0001FAFF"   # эмодзи и пиктограммы
               "\U00002600-\U000027BF"   # символы и dingbats
               "\U0001F1E6-\U0001F1FF"   # флаги
               "\U00002190-\U000021FF"   # стрелки
               "\U00002B00-\U00002BFF"   # доп. стрелки/символы
               "\U0000FE0F"              # variation selector
               "]", " ", t)
    t = re.sub(r"-{1,2}>|=>", " ", t)                          # текстовые стрелки -> / =>
    t = _slash_to_or(t)                                        # / → «или»
    t = t.replace("&", " и ")
    t = re.sub(r"№\s*(\d)", r"номер \1", t)
    t = t.replace("№", " номер ")
    t = re.sub(r"(\d)\s*%", r"\1 процентов", t)
    t = t.replace("%", " процентов ")
    t = re.sub(r"(\d)\s*[xх×]\s*(\d)", r"\1 на \2", t)         # 24x36 → «24 на 36»
    t = re.sub(r"\s+([.,!?;:])", r"\1", t)                     # пробел перед знаком убрать
    return t


def _md_tables_to_text(t: str) -> str:
    """Markdown-таблицы → читаемые строки «Колонка: значение; …» (по строке на запись).
    И видно структурно в поле, и озвучивается естественно (палки/разделители убираются)."""
    lines = t.split("\n")

    def cells(line: str) -> list:
        line = line.strip()
        if line.startswith("|"):
            line = line[1:]
        if line.endswith("|"):
            line = line[:-1]
        return [c.strip() for c in line.split("|")]

    def is_sep(line: str) -> bool:
        if "|" not in line:
            return False
        cs = [c for c in cells(line) if c != ""]
        return bool(cs) and all(re.match(r"^:?-{1,}:?$", c) for c in cs)

    out, i, n = [], 0, len(lines)
    while i < n:
        line = lines[i]
        if "|" in line and i + 1 < n and is_sep(lines[i + 1]):
            header = cells(line)
            i += 2
            while i < n and "|" in lines[i] and lines[i].strip():
                row = cells(lines[i])
                i += 1
                pairs = []
                for idx, val in enumerate(row):
                    if not val:
                        continue
                    h = header[idx].strip() if idx < len(header) else ""
                    pairs.append(f"{h}: {val}" if h else val)
                if pairs:
                    out.append("; ".join(pairs))
            continue
        out.append(line)
        i += 1
    return "\n".join(out)


def _clean_for_tts(s: str, include_reasoning: bool = False) -> str:
    t = s
    if not include_reasoning:
        t = re.sub(r"(?is)<think>[\s\S]*?</think>", " ", t)
    else:
        t = re.sub(r"(?i)</?think>", "\n", t)
    t = normalize_tables(t)                        # таблицы из Excel, терминала, одной строкой
    t = re.sub(r"```[\s\S]*?```", " ", t)
    t = re.sub(r"`[^`]*`", " ", t)
    t = _md_tables_to_text(t)                      # таблицы → читаемые строки (до удаления |)
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)
    t = re.sub(r"[*#>`_]", "", t)
    t = t.replace("|", " ")                        # уцелевшие одиночные | (не таблица)
    t = _smart_normalize(t)
    t = _structure_for_speech(t)                   # смысловые предложения (интонация/паузы)
    # сохранить переносы строк (таблицы/списки), схлопнуть только лишние пробелы
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r" *\n *", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def _extract_document_text(path: Path) -> str:
    """Извлечь читаемый текст из документа: PDF (PyMuPDF→pypdf), DOCX (из zip без зависимостей),
    TXT/MD/CSV (как есть), RTF (грубая чистка управляющих слов)."""
    ext = path.suffix.lower()
    if ext == ".pdf":
        try:
            import fitz                                   # PyMuPDF
            doc = fitz.open(str(path))
            try:
                return "\n\n".join(pg.get_text() for pg in doc)
            finally:
                doc.close()
        except Exception:
            from pypdf import PdfReader
            return "\n\n".join((pg.extract_text() or "") for pg in PdfReader(str(path)).pages)
    if ext == ".docx":
        with zipfile.ZipFile(str(path)) as z:
            xml = z.read("word/document.xml").decode("utf-8", "replace")
        xml = re.sub(r"(?is)<w:tab[^>]*/>", "\t", xml)
        xml = re.sub(r"(?is)</w:p>", "\n", xml)
        return html.unescape(re.sub(r"(?is)<[^>]+>", "", xml))
    if ext == ".rtf":
        t = path.read_text(encoding="utf-8", errors="replace")
        t = re.sub(r"\\par[d]?\b", "\n", t)
        t = re.sub(r"\\'[0-9a-fA-F]{2}", " ", t)
        t = re.sub(r"\\[a-zA-Z]+-?\d* ?", "", t)
        return t.replace("{", "").replace("}", "")
    return path.read_text(encoding="utf-8", errors="replace")


def _chunk_text(text: str, size: int = 1800) -> list:
    """Разбить длинный документ на части по абзацам/предложениям (для удобной навигации
    и устойчивого рендера) — каждая часть станет отдельной репликой в очереди."""
    text = re.sub(r"\n{3,}", "\n\n", (text or "").strip())
    if len(text) <= size:
        return [text] if text else []
    chunks, cur = [], ""
    for para in re.split(r"\n\s*\n", text):
        if len(para) > size:
            for sent in re.split(r"(?<=[.!?…])\s+", para):
                if cur and len(cur) + len(sent) > size:
                    chunks.append(cur.strip())
                    cur = ""
                cur += sent + " "
        else:
            if cur and len(cur) + len(para) > size:
                chunks.append(cur.strip())
                cur = ""
            cur += para + "\n\n"
    if cur.strip():
        chunks.append(cur.strip())
    return chunks


def _derive_word_marks(body: str, sent_marks: list) -> list:
    """Приблизительные тайминги слов: длительность предложения делим между словами
    пропорционально их длине. edge-tts не отдаёт WordBoundary для наших голосов, поэтому
    «караоке»-подсветку по словам строим из границ предложений — работает для всех голосов."""
    out = []
    for (start, end, a, b) in sent_marks:
        seg = body[a:b]
        toks = [(m.start(), m.group()) for m in re.finditer(r"\S+", seg)]
        if not toks:
            continue
        total = sum(len(t) for _, t in toks) or 1
        dur = max(0.0, end - start)
        acc = 0
        for off, tok in toks:
            ws = start + dur * (acc / total)
            acc += len(tok)
            we = start + dur * (acc / total)
            out.append((ws, we, a + off, a + off + len(tok)))
    return out


def _map_marks_to_chars(text: str, sents: list) -> list:
    """Сопоставить предложения (из таймингов) с диапазонами символов в text."""
    out = []
    cursor = 0
    for start, end, stext in sents:
        stext = (stext or "").strip()
        if not stext:
            continue
        idx = text.find(stext, cursor)
        if idx < 0:
            idx = text.find(stext)        # вдруг порядок сбился
        if idx < 0:
            continue
        out.append((start, end, idx, idx + len(stext)))
        cursor = idx + len(stext)
    return out


def _split_for_tts(
    body: str,
    first_chars: int = TTS_FIRST_PART_CHARS,
    second_chars: int = TTS_SECOND_PART_CHARS,
    part_chars: int = TTS_PART_CHARS,
) -> list:
    """Разрезать текст на части [(начало, конец)]: плавный старт без паузы после 1-й части.
    Первая часть ~120–220 символов, вторая ~380, остальные по TTS_PART_CHARS; хвосты без букв/цифр склеиваются."""
    out, a, n = [], 0, len(body)
    part_idx = 0

    def _limit_for(idx: int) -> int:
        if idx == 0:
            return first_chars
        if idx == 1:
            return second_chars
        return part_chars

    while n - a > _limit_for(part_idx):
        limit = _limit_for(part_idx)
        win = body[a:a + limit]
        cut = limit
        min_cut = int(limit * (0.55 if part_idx == 0 else 0.35))
        found = False
        for sep in ("\n", ". ", "! ", "? ", "… ", ";\n"):
            p = win.rfind(sep)
            if p >= min_cut:
                cut = p + len(sep)
                found = True
                break
        if not found:
            for sep in ("; ", ", ", " "):
                p = win.rfind(sep)
                if p >= min_cut:
                    cut = p + len(sep)
                    break
        out.append((a, a + cut))
        a += cut
        part_idx += 1
    if a < n:
        if out and not any(ch.isalnum() for ch in body[a:n]):
            pa, pb = out[-1]
            prev_limit = _limit_for(len(out) - 1)
            cur_limit = _limit_for(len(out))
            if n - pa <= prev_limit:
                out[-1] = (pa, n)
            else:
                split_pos = None
                if n - pb < cur_limit and any(ch.isalnum() for ch in body[pa:pb]):
                    search_from = max(0, (n - cur_limit) - pa)
                    pos = body[pa:pb].rfind(" ", search_from, max(search_from, pb - pa - 1))
                    if pos > 0 and any(ch.isalnum() for ch in body[pa:pa + pos]) and any(ch.isalnum() for ch in body[pa + pos:n]):
                        split_pos = pa + pos + 1
                if split_pos is not None:
                    out[-1] = (pa, split_pos)
                    out.append((split_pos, n))
                else:
                    out.append((a, n))
        else:
            out.append((a, n))
    return out
