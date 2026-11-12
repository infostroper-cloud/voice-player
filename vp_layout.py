"""Показ реплики с исходной вёрсткой: абзацы, заголовки, списки, код, таблицы.

Голос читает очищенный текст, а на экране оригинал, поэтому для подсветки нужна связь между ними:
каждая исходная строка очищается отдельно, находится в озвучиваемом тексте и сопоставляется
с тем, что показано. Строка, которой в озвучке нет (например, код), показывается без подсветки.
"""
from __future__ import annotations

import difflib
import re
from typing import Callable

from vp_tables import display_cell, is_sep_line, layout_segments, normalize_tables, split_row

MAX_ALIGN_CHARS = 4000       # посимвольное сравнение очень длинной строки заметно тормозит окно
MIN_COVERAGE = 0.6           # меньше: текст правили отдельно от оригинала, показываем сам текст

_INLINE_RE = re.compile(r"\*\*(.+?)\*\*|__(.+?)__|`([^`]+)`|\[([^\]]+)\]\([^)]+\)|(?<![\w*])\*([^*\n]+)\*(?![\w*])")
_INLINE_STYLE = ("bold", "bold", "code", None, "italic")


def parse_inline(line: str) -> tuple:
    """Убрать разметку внутри строки: (текст для показа, [(начало, конец, стиль)])."""
    out, spans, pos, length = [], [], 0, 0
    for m in _INLINE_RE.finditer(line):
        before = line[pos:m.start()]
        out.append(before)
        length += len(before)
        group = next(i for i in range(5) if m.group(i + 1) is not None)
        inner, inner_spans = parse_inline(m.group(group + 1)) if group != 2 else (m.group(3), [])
        if _INLINE_STYLE[group]:
            spans.append((length, length + len(inner), _INLINE_STYLE[group]))
        spans += [(a + length, b + length, st) for a, b, st in inner_spans]
        out.append(inner)
        length += len(inner)
        pos = m.end()
    out.append(line[pos:])
    return "".join(out), spans


def parse_blocks(raw: str) -> list:
    """Исходный текст -> блоки по порядку. У каждого: style, source (что очищать для озвучки), display."""
    lines = normalize_tables(raw or "").split("\n")
    blocks, i, n = [], 0, len(lines)
    while i < n:
        line = lines[i]
        fence = re.match(r"\s*```", line)
        if fence:
            j = i + 1
            while j < n and not re.match(r"\s*```", lines[j]):
                j += 1
            blocks.append({"style": "code", "source": "", "display": "\n".join(lines[i + 1:j])})
            i = j + 1
            continue
        if "|" in line and i + 1 < n and is_sep_line(lines[i + 1]):
            j = i + 2
            while j < n and "|" in lines[j] and lines[j].strip():
                j += 1
            blocks.append({"style": "table", "source": "\n".join(lines[i:j]), "display": ""})
            i = j
            continue
        i += 1
        text = line.strip()
        if not text:
            blocks.append({"style": "gap", "source": "", "display": ""})
        elif re.fullmatch(r"[-=_*]{3,}", text):
            blocks.append({"style": "rule", "source": "", "display": "―" * 24})
        elif (m := re.match(r"(#{1,6})\s+(.*)", text)):
            blocks.append({"style": "heading", "source": line, "display": m.group(2)})
        elif (m := re.match(r">\s?(.*)", text)):
            blocks.append({"style": "quote", "source": line, "display": m.group(1)})
        elif (m := re.match(r"[-*+•]\s+(.*)", text)):
            indent = "    " * (min(len(line) - len(line.lstrip()), 8) // 2)
            blocks.append({"style": "list", "source": line, "display": indent + "•  " + m.group(1)})
        elif (m := re.match(r"(\d+[.)])\s+(.*)", text)):
            indent = "    " * (min(len(line) - len(line.lstrip()), 8) // 2)
            blocks.append({"style": "list", "source": line, "display": f"{indent}{m.group(1)}  {m.group(2)}"})
        else:
            blocks.append({"style": "para", "source": line, "display": text})
    return blocks


def align(spoken: str, shown: str) -> list:
    """Совпадающие куски двух текстов: [(начало в озвучке, начало на экране, длина)]."""
    if spoken == shown:
        return [(0, 0, len(spoken))]
    if not spoken or not shown:
        return []
    if max(len(spoken), len(shown)) > MAX_ALIGN_CHARS:
        return []
    matcher = difflib.SequenceMatcher(None, spoken, shown, autojunk=False)
    return [tuple(b) for b in matcher.get_matching_blocks() if b.size]


def spoken_to_shown(seg: dict, char: int, end: bool = False) -> int:
    """Позиция в озвучиваемом тексте -> позиция в показанной строке.

    Слово, заменённое для голоса, не совпадает с показанным целиком. Начало такого места
    переходит к началу показанного слова, конец (end=True) — к его концу, так что подсветка
    закрывает слово на экране, как бы оно ни произносилось."""
    offset = char - seg["a"]
    length_spoken = seg["b"] - seg["a"]
    length_shown = len(seg["display"])
    if not seg.get("map"):
        return round(offset * length_shown / length_spoken) if length_spoken else 0
    gap_start = 0                 # где на экране кончился предыдущий совпавший кусок
    for sp, sh, size in seg["map"]:
        if offset < sp or (end and offset == sp):
            return sh if end else gap_start
        if offset < sp + size:
            return sh + (offset - sp)
        gap_start = sh + size
    return length_shown if end else gap_start


def shown_to_spoken(seg: dict, pos: int) -> int:
    """Обратно: куда перемотать по клику."""
    if not seg.get("map"):
        shown = len(seg["display"])
        return seg["a"] + (round(pos * (seg["b"] - seg["a"]) / shown) if shown else 0)
    for sp, sh, size in seg["map"]:
        if pos < sh:
            return seg["a"] + sp
        if pos < sh + size:
            return seg["a"] + sp + (pos - sh)
    return seg["b"]


def plain_text(raw: str) -> str:
    """Текст реплики одной строкой без разметки и кода: для списка очереди и превью."""
    out = []
    for block in parse_blocks(raw):
        if block["style"] in ("code", "gap", "rule"):
            continue
        if block["style"] == "table":
            rows = [ln for ln in block["source"].split("\n") if not is_sep_line(ln)]
            out += [" ".join(display_cell(c) for c in split_row(ln)) for ln in rows]
        else:
            out.append(parse_inline(block["display"])[0].strip())
    return "\n".join(x for x in out if x)


def _plain_fallback(text: str) -> list:
    return [{"kind": "text", "a": 0, "b": len(text), "display": text, "style": "para", "spans": [],
             "map": [(0, 0, len(text))]}]


def document_segments(text: str, raw: str, clean: Callable[[str], str]) -> list:
    """Куски для показа в порядке исходного текста.

    Текст: {"kind": "text", "a", "b", "display", "style", "spans", "map"}, a и b — позиции
    в озвучке (a == b: этой строки голос не читает). Таблица: как в vp_tables.layout_segments."""
    segs, cursor, covered = [], 0, 0
    for block in parse_blocks(raw):
        if block["style"] == "table":
            found = [s for s in layout_segments(text[cursor:], block["source"], clean) if s["kind"] == "table"]
            if found:
                table = _shift_table(found[0], cursor)
                segs.append(table)
                covered += table["b"] - table["a"]
                cursor = table["b"]
            else:                                      # строки не нашлись: показать как есть, без подсветки
                segs.append(_text_seg(cursor, cursor, block["source"], "code", [], []))
            continue
        shown, spans = parse_inline(block["display"]) if block["style"] != "code" else (block["display"], [])
        spoken = clean(block["source"]) if block["source"] else ""
        idx = text.find(spoken, cursor) if spoken else -1
        if idx < 0:
            segs.append(_text_seg(cursor, cursor, shown, block["style"], spans, []))
            continue
        end = idx + len(spoken)
        segs.append(_text_seg(idx, end, shown, block["style"], spans, align(spoken, shown)))
        covered += len(spoken)
        cursor = end
    if not segs or (text.strip() and covered < len(text.strip()) * MIN_COVERAGE):
        return _plain_fallback(text)
    return segs


def _text_seg(a: int, b: int, display: str, style: str, spans: list, mapping: list) -> dict:
    return {"kind": "text", "a": a, "b": b, "display": display, "style": style, "spans": spans, "map": mapping}


def _shift_table(table: dict, by: int) -> dict:
    table = dict(table, a=table["a"] + by, b=table["b"] + by)
    table["rows"] = [dict(r, a=r["a"] + by, b=r["b"] + by,
                          spans=[(c, a + by, b + by) for c, a, b in r["spans"]]) for r in table["rows"]]
    return table
