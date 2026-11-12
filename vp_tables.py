"""Таблицы в тексте реплик: приём из любого вида и разметка для показа.

Источник один — markdown-таблица в raw_text. Всё остальное приводим к ней:
  * Excel/Google Таблицы/Word в буфере — строки с табуляцией или HTML;
  * терминал Claude Code — псевдографика (│ ├─┼─┤);
  * реплики, у которых старый хук схлопнул переносы, — таблица одной строкой.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Callable

_FENCE = re.compile(r"```[\s\S]*?```")
_INLINE_CODE = re.compile(r"`[^`]*`")
_SEP_CELL = re.compile(r"^:?-{1,}:?$")
_COLLAPSED_SEP = re.compile(r"\|(?:\s*:?-{1,}:?\s*\|){2,}")
_BOX_CHARS = "┌┬┐├┼┤└┴┘─━═╞╪╡╔╦╗╠╬╣╚╩╝╒╤╕╘╧╛┏┳┓┣╋┫┗┻┛"
_BOX_RULE = re.compile(rf"^\s*[{_BOX_CHARS}]+\s*$")
_ASCII_RULE = re.compile(r"^\s*\+[-=+]+\+\s*$")
_BOX_CELL = re.compile(r"[│┃║]")


def split_row(line: str) -> list:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [c.strip() for c in line.split("|")]


def is_sep_line(line: str) -> bool:
    if "|" not in line:
        return False
    cells = [c for c in split_row(line) if c]
    return bool(cells) and all(_SEP_CELL.match(c) for c in cells)


def _md_table(rows: list) -> list:
    """Строки таблицы (первая — шапка) → строки markdown. Колонки выравниваем по числу."""
    width = max(len(r) for r in rows)
    fixed = [[(c.replace("|", "/").replace("\n", " ").strip()) for c in r] + [""] * (width - len(r))
             for r in rows]
    out = ["| " + " | ".join(fixed[0]) + " |", "|" + "---|" * width]
    out += ["| " + " | ".join(r) + " |" for r in fixed[1:]]
    return out


# ---------------- псевдографика из терминала ----------------

def _is_box_rule(line: str) -> bool:
    return bool(_BOX_RULE.match(line) or _ASCII_RULE.match(line))


def _is_box_row(line: str) -> bool:
    s = line.strip()
    return bool(s) and s[0] in "│┃║" and s[-1] in "│┃║"


def _box_cells(line: str) -> list:
    return [c.strip() for c in _BOX_CELL.split(line.strip())[1:-1]]


def _ascii_cells(line: str) -> list:
    return split_row(line)


def _box_region_to_rows(region: list, cells) -> list:
    """Строки между линейками: если линеек внутри ≥2 — каждая группа одна запись (длинные
    ячейки перенесены на несколько экранных строк); иначе линейка только под шапкой."""
    groups, cur = [], []
    for line in region:
        if _is_box_rule(line):
            if cur:
                groups.append(cur)
            cur = []
        else:
            cur.append(cells(line))
    if cur:
        groups.append(cur)
    if not groups:
        return []

    def merge(group):
        width = max(len(r) for r in group)
        return [" ".join(r[i] for r in group if i < len(r) and r[i]).strip() for i in range(width)]

    if len(groups) >= 3:
        return [merge(g) for g in groups]
    rows = [merge(groups[0])]
    for g in groups[1:]:
        rows += g
    return rows


def _boxes_to_md(lines: list) -> list:
    out, i, n = [], 0, len(lines)
    while i < n:
        line = lines[i]
        if _BOX_RULE.match(line) or _is_box_row(line):
            j = i
            while j < n and (_BOX_RULE.match(lines[j]) or _is_box_row(lines[j])):
                j += 1
            rows = _box_region_to_rows(lines[i:j], _box_cells)
            if len(rows) >= 2:
                out += _md_table(rows)
                i = j
                continue
        elif _ASCII_RULE.match(line):
            j = i
            while j < n and (_ASCII_RULE.match(lines[j]) or lines[j].strip().startswith("|")):
                j += 1
            rows = _box_region_to_rows(lines[i:j], _ascii_cells)
            if len(rows) >= 2:
                out += _md_table(rows)
                i = j
                continue
        out.append(line)
        i += 1
    return out


# ---------------- табуляция (Excel, Google Таблицы) ----------------

def _tsv_to_md(lines: list) -> list:
    out, i, n = [], 0, len(lines)
    while i < n:
        tabs = lines[i].count("\t")
        if tabs >= 1:
            j = i
            while j < n and lines[j].count("\t") == tabs:
                j += 1
            if j - i >= 2:
                out += _md_table([ln.split("\t") for ln in lines[i:j]])
                i = j
                continue
        out.append(lines[i])
        i += 1
    return out


# ---------------- таблица, схлопнутая в одну строку ----------------

def _expand_collapsed_line(line: str) -> list:
    m = _COLLAPSED_SEP.search(line)
    if not m:
        return [line]
    ncols = len(re.findall(r":?-+:?", m.group(0)))
    parts = line[:m.start()].split("|")
    if parts and not parts[-1].strip():
        parts = parts[:-1]
    if len(parts) - 1 < ncols:              # шапку не нашли — это не таблица
        return [line]
    header = [p.strip() for p in parts[-ncols:]]
    prefix = "|".join(parts[:-ncols]).strip()
    tokens = line[m.end():].split("|")
    rows, i = [], 1
    if tokens[0].strip():
        i = len(tokens)
    while i + ncols < len(tokens):
        rows.append([t.strip() for t in tokens[i:i + ncols]])
        i += ncols
        if tokens[i].strip():
            break
        i += 1
    tail = "|".join(tokens[i:]).strip() if rows and i < len(tokens) else ""
    if not rows:
        return [line]
    out = ([prefix, ""] if prefix else []) + _md_table([header] + rows)
    if tail:
        out += [""] + _expand_collapsed_line(tail)
    return out


def _collapsed_to_md(lines: list) -> list:
    out = []
    for line in lines:
        out += _expand_collapsed_line(line) if "|" in line and "-" in line else [line]
    return out


def normalize_tables(text: str) -> str:
    """Привести любые таблицы в тексте к markdown. Для текста без таблиц — без изменений."""
    if not text or not any(ch in text for ch in "|\t│┃║+"):
        return text
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "\n".join(_collapsed_to_md(_tsv_to_md(_boxes_to_md(lines))))


# ---------------- HTML из буфера обмена ----------------

class _HtmlToMd(HTMLParser):
    _BLOCK = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section"}
    _SKIP = {"script", "style", "head", "title", "meta"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.skip = 0
        self.rows = None
        self.row = None
        self.cell = None

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self.skip += 1
        elif tag == "table" and self.rows is None:
            self.rows = []
        elif tag == "tr" and self.rows is not None:
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.cell = []
        elif tag == "br" and self.cell is not None:
            self.cell.append(" ")
        elif tag in self._BLOCK and self.rows is None:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in ("td", "th") and self.cell is not None:
            self.row.append(re.sub(r"\s+", " ", "".join(self.cell)).strip())
            self.cell = None
        elif tag == "tr" and self.row is not None:
            if any(self.row):
                self.rows.append(self.row)
            self.row = None
        elif tag == "table" and self.rows is not None:
            if len(self.rows) >= 2:
                self.out.append("\n" + "\n".join(_md_table(self.rows)) + "\n")
            elif self.rows:
                self.out.append("\n" + " ".join(self.rows[0]) + "\n")
            self.rows = None
        elif tag in self._BLOCK and self.rows is None:
            self.out.append("\n")

    def handle_data(self, data):
        if self.skip:
            return
        if self.cell is not None:
            self.cell.append(data)
        elif self.rows is None:
            self.out.append(re.sub(r"[ \t\r\n]+", " ", data))


def html_to_markdown(html: str) -> str:
    """HTML из буфера обмена (формат CF_HTML) → текст с markdown-таблицами."""
    if "<!--StartFragment-->" in html:
        html = html.split("<!--StartFragment-->", 1)[1].split("<!--EndFragment-->", 1)[0]
    parser = _HtmlToMd()
    parser.feed(html)
    parser.close()
    text = "".join(parser.out)
    lines = [ln.strip() for ln in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


# ---------------- разметка для панели текста ----------------

def find_tables(raw: str) -> list:
    """Все таблицы реплики в порядке следования: [{"header": [...], "rows": [[...], ...]}]."""
    lines = _FENCE.sub(" ", normalize_tables(raw or "")).split("\n")
    tables, i, n = [], 0, len(lines)
    while i < n:
        if "|" in lines[i] and i + 1 < n and is_sep_line(lines[i + 1]):
            header = split_row(lines[i])
            i += 2
            rows = []
            while i < n and "|" in lines[i] and lines[i].strip():
                rows.append(split_row(lines[i]))
                i += 1
            if rows:
                tables.append({"header": header, "rows": rows})
            continue
        i += 1
    return tables


def display_cell(cell: str) -> str:
    """Ячейка для показа: без служебной разметки, но с содержимым кода."""
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", cell or "")
    return re.sub(r"[*`]|(?<!\w)_|_(?!\w)", "", t).strip()


def table_row_md(header: list, row: list) -> str:
    return "\n".join(_md_table([header, row]))


def _cell_spans(text: str, a: int, b: int, row: list) -> list:
    """Какой кусок озвучки соответствует какой ячейке: [(номер колонки, начало, конец)].
    Озвучка строки — «Колонка: значение; Колонка: значение». Если значения сами содержат
    «;» и сопоставить нельзя — вся строка считается одной областью."""
    filled = [i for i, c in enumerate(row) if _INLINE_CODE.sub(" ", c).strip()]
    chunks = text[a:b].split("; ")
    if len(chunks) != len(filled):
        return [(-1, a, b)]
    out, pos = [], a
    for col, chunk in zip(filled, chunks):
        out.append((col, pos, pos + len(chunk)))
        pos += len(chunk) + 2
    return out


def layout_segments(text: str, raw: str, clean: Callable[[str], str]) -> list:
    """Разметить озвучиваемый текст на куски для показа.

    Возвращает список: {"kind": "text", "a", "b"} и
    {"kind": "table", "a", "b", "header": [...], "rows": [{"cells", "a", "b", "spans"}]}.
    a, b — позиции в text, по ним идёт подсветка. Таблицу, строки которой в озвучке не нашлись,
    оставляем текстом: лучше показать «Колонка: значение», чем потерять синхронность."""
    segs, cursor = [], 0
    for table in find_tables(raw):
        located, pos, first = [], cursor, None
        ok = True
        for row in table["rows"]:
            spoken = clean(table_row_md(table["header"], row))
            if not spoken:
                located.append(None)
                continue
            idx = text.find(spoken, pos)
            if idx < 0:
                ok = False
                break
            if first is None:
                first = idx
            located.append((idx, idx + len(spoken)))
            pos = idx + len(spoken)
        if not ok or first is None:
            continue
        if first > cursor:
            segs.append({"kind": "text", "a": cursor, "b": first})
        rows = []
        for row, loc in zip(table["rows"], located):
            a, b = loc if loc else (first, first)
            rows.append({"cells": [display_cell(c) for c in row], "a": a, "b": b,
                         "spans": _cell_spans(text, a, b, row) if loc else []})
        segs.append({"kind": "table", "a": first, "b": pos,
                     "header": [display_cell(h) for h in table["header"]], "rows": rows})
        cursor = pos
    if cursor < len(text):
        segs.append({"kind": "text", "a": cursor, "b": len(text)})
    return segs
