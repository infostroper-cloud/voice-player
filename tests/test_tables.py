"""Запуск: python -m unittest discover -s tests  (из папки voice-player)."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

import vp_feed
import vp_integrations as vi
from vp_tables import (find_tables, html_to_markdown, layout_segments, normalize_tables)


class NormalizeTables(unittest.TestCase):
    def assertTable(self, text, header, rows):
        tables = find_tables(text)
        self.assertEqual(len(tables), 1, normalize_tables(text))
        self.assertEqual(tables[0]["header"], header)
        self.assertEqual(tables[0]["rows"], rows)

    def test_markdown_kept(self):
        md = "Итог:\n\n| Что | Цена |\n|---|---|\n| Болт | 10 |\n| Гайка | 5 |\n\nВсё."
        self.assertEqual(normalize_tables(md), md)
        self.assertTable(md, ["Что", "Цена"], [["Болт", "10"], ["Гайка", "5"]])

    def test_plain_text_untouched(self):
        for text in ("Просто текст без таблиц.", "A | B — одна палка в строке",
                     "Один\tтаб в одной строке", "2 + 2 = 4", "| --- |"):
            self.assertEqual(normalize_tables(text), text)
            self.assertEqual(find_tables(text), [])

    def test_excel_tabs(self):
        self.assertTable("Имя\tВозраст\nАня\t30\nБорис\t41", ["Имя", "Возраст"],
                         [["Аня", "30"], ["Борис", "41"]])

    def test_terminal_box_with_rules_between_rows(self):
        box = ("┌─────┬──────┐\n│ Что │ Цена │\n├─────┼──────┤\n│ Длинное │ 10 │\n"
               "│ имя │    │\n├─────┼──────┤\n│ Гайка │ 5 │\n└─────┴──────┘")
        self.assertTable(box, ["Что", "Цена"], [["Длинное имя", "10"], ["Гайка", "5"]])

    def test_terminal_box_rule_under_header_only(self):
        box = "┌───┬───┐\n│ A │ B │\n├───┼───┤\n│ 1 │ 2 │\n│ 3 │ 4 │\n└───┴───┘"
        self.assertTable(box, ["A", "B"], [["1", "2"], ["3", "4"]])

    def test_ascii_box(self):
        box = "+---+---+\n| A | B |\n+---+---+\n| 1 | 2 |\n| 3 | 4 |\n+---+---+"
        self.assertTable(box, ["A", "B"], [["1", "2"], ["3", "4"]])

    def test_collapsed_single_line(self):
        line = "Вот итог: | Что | Цена | |---|---| | Болт | 10 | | Гайка | 5 | Дальше обычный текст."
        norm = normalize_tables(line)
        self.assertTable(line, ["Что", "Цена"], [["Болт", "10"], ["Гайка", "5"]])
        self.assertIn("Вот итог:", norm)
        self.assertIn("Дальше обычный текст.", norm)

    def test_html_clipboard(self):
        html = ("Version:0.9\nStartHTML:0<html><body><!--StartFragment--><p>Сводка</p>"
                "<table><tr><th>Что</th><th>Цена</th></tr><tr><td>Болт</td><td>1<br>0</td></tr></table>"
                "<p>Конец &amp; всё</p><!--EndFragment--></body></html>")
        md = html_to_markdown(html)
        self.assertTable(md, ["Что", "Цена"], [["Болт", "1 0"]])
        self.assertIn("Сводка", md)
        self.assertIn("Конец & всё", md)
        self.assertNotIn("Version", md)

    def test_table_in_code_block_is_not_shown(self):
        self.assertEqual(find_tables("```\n| A | B |\n|---|---|\n| 1 | 2 |\n```"), [])


class Layout(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.argv = ["x"]
        import app
        cls.clean = staticmethod(app._clean_for_tts)

    def test_segments_cover_spoken_text(self):
        raw = "Начало.\n\n| Что | Цена |\n|---|---|\n| Болт **M8** | 10% |\n| Гайка | 5 |\n\nКонец."
        text = self.clean(raw)
        segs = layout_segments(text, raw, self.clean)
        self.assertEqual([s["kind"] for s in segs], ["text", "table", "text"])
        table = segs[1]
        self.assertEqual(table["header"], ["Что", "Цена"])
        self.assertEqual(table["rows"][0]["cells"], ["Болт M8", "10%"])
        spoken = text[table["rows"][0]["a"]:table["rows"][0]["b"]]
        self.assertIn("Болт", spoken)
        self.assertIn("10 процентов", spoken)
        col1 = [s for s in table["rows"][0]["spans"] if s[0] == 1][0]
        self.assertIn("процентов", text[col1[1]:col1[2]])

    def test_cell_with_semicolon_falls_back_to_row(self):
        raw = "| A | B |\n|---|---|\n| 1; 2 | 3 |"
        text = self.clean(raw)
        row = layout_segments(text, raw, self.clean)[0]["rows"][0]
        self.assertEqual(row["spans"][0][0], -1)

    def test_no_tables_is_one_segment(self):
        text = self.clean("Просто текст.")
        self.assertEqual(layout_segments(text, "Просто текст.", self.clean),
                         [{"kind": "text", "a": 0, "b": len(text)}])

    def test_split_keeps_every_char_and_limit(self):
        import app
        body = "\n".join(f"Колонка: значение {i}; Вторая: ещё одно длинное значение." for i in range(300))
        spans = app._split_for_tts(body)
        self.assertGreater(len(spans), 3)
        self.assertEqual("".join(body[a:b] for a, b in spans), body)
        self.assertLessEqual(spans[0][1] - spans[0][0], app.TTS_FIRST_PART_CHARS)
        self.assertTrue(all(b - a <= app.TTS_PART_CHARS for a, b in spans))
        self.assertEqual(app._split_for_tts("короткий"), [(0, 8)])


class RenderParts(unittest.TestCase):
    """Озвучка частями без сети: подменяем только вызов сервиса."""

    def setUp(self):
        import app
        self.app = app
        self.tmp = tempfile.TemporaryDirectory()
        self.old = (app.AUDIO_DIR, app._synthesize, app.TTS_RETRY_DELAY_S)
        app.AUDIO_DIR = Path(self.tmp.name)
        app.TTS_RETRY_DELAY_S = 0
        self.calls = []
        self.voices = []
        self.fail_on = set()

        def fake(text, rate, path, voice):
            self.calls.append(text)
            self.voices.append(voice)
            if any(mark in text for mark in self.fail_on):
                raise RuntimeError("TimeoutError: рендер не завершился")
            Path(path).write_bytes(text.encode("utf-8"))
            return [(0.0, 2.0, text)], []
        app._synthesize = fake
        self.body = "\n".join(f"Строка номер {i} с достаточно длинным содержанием для разбиения." for i in range(60))

    def tearDown(self):
        self.app.AUDIO_DIR, self.app._synthesize, self.app.TTS_RETRY_DELAY_S = self.old
        self.tmp.cleanup()

    def item(self):
        return self.app.Item(id="x", source="manual", author="a", time_str="", text=self.body, raw_text=self.body)

    def test_all_parts_in_order_then_one_file(self):
        item, seen = self.item(), []
        self.app.render_audio_blocking(item, on_part=lambda it: seen.append(len(it.parts_done)))
        self.assertEqual(seen, list(range(1, len(seen) + 1)))
        self.assertGreater(len(seen), 2)
        self.assertTrue(item.audio_ready)
        self.assertFalse(item.more_parts)
        self.assertIsNone(item.audio_error)
        self.assertEqual(item.audio_path.name, "x.mp3")
        joined = item.audio_path.read_bytes().decode("utf-8").replace("\n", "")
        self.assertEqual(joined, self.body.replace("\n", ""))

    def test_failed_part_keeps_done_and_retry_resumes(self):
        item = self.item()
        self.fail_on = {"номер 30 "}
        self.app.render_audio_blocking(item, on_part=lambda it: None)
        self.assertIn("TimeoutError", item.audio_error)
        self.assertTrue(item.audio_ready)            # начало уже можно слушать
        self.assertTrue(item.more_parts)
        done = len(item.parts_done)
        self.assertGreater(done, 0)
        self.fail_on = set()
        self.calls.clear()
        self.app.render_audio_blocking(item, on_part=lambda it: None)
        self.assertFalse(item.more_parts)
        self.assertIsNone(item.audio_error)
        self.assertEqual(len(self.calls), len(item.parts_done) - done)   # готовые части не озвучиваем заново

    def test_short_text_is_single_file_as_before(self):
        item = self.item()
        item.text = "Короткая реплика."
        self.app.render_audio_blocking(item)
        self.assertEqual(item.audio_path.name, "x.mp3")
        self.assertTrue(item.audio_ready)
        self.assertEqual(len(self.calls), 1)

    def test_explicit_voice_not_overridden_by_language_autodetect(self):
        item = self.item()
        item.text = "Привет! Проверка дикции."
        self.app.render_audio_blocking(item, voice="en-US-AndrewMultilingualNeural")
        self.assertEqual(self.voices, ["en-US-AndrewMultilingualNeural"])
        self.assertEqual(item.rendered_voice, "en-US-AndrewMultilingualNeural")

        item2 = self.app.Item(id="y", source="manual", author="Мой текст", time_str="",
                              text="Привет ещё раз.", raw_text="Привет ещё раз.")
        self.app.render_audio_blocking(item2, voice=self.app.TTS_VOICE)
        self.assertEqual(self.voices[-1], self.app.TTS_VOICE)

        item3 = self.app.Item(id="z", source="manual", author="Мой текст", time_str="",
                              text="Привет без голоса.", raw_text="Привет без голоса.")
        self.app.render_audio_blocking(item3, voice="")
        self.assertEqual(self.voices[-1], "ru-RU-DmitryNeural")

    def test_resolve_item_voice_manual_vs_stream(self):
        author_voices = {
            "Мой текст": "piper:ru_RU-dmitri-medium",
            "Claude/123": "en-US-AvaMultilingualNeural",
        }
        default_v = "en-US-AndrewMultilingualNeural"

        manual = self.app.Item(id="m1", source="manual", author="Мой текст", time_str="", text="Hi", raw_text="Hi")
        self.assertEqual(self.app._resolve_item_voice(manual, author_voices, default_v), default_v)

        manual_custom = self.app.Item(id="m2", source="manual", author="Мой текст", time_str="",
                                      text="Hi", raw_text="Hi", custom_voice="fr-FR-RemyMultilingualNeural")
        self.assertEqual(self.app._resolve_item_voice(manual_custom, author_voices, default_v),
                         "fr-FR-RemyMultilingualNeural")

        manual_clip = self.app.Item(id="m3", source="manual", author="Claude", time_str="", text="Hi", raw_text="Hi")
        self.assertEqual(self.app._resolve_item_voice(manual_clip, {"Claude": "ru-RU-DmitryNeural"}, default_v),
                         default_v)

        stream = self.app.Item(id="s1", source="claude", author="Claude/123", time_str="", text="Hi", raw_text="Hi")
        self.assertEqual(self.app._resolve_item_voice(stream, author_voices, default_v),
                         "en-US-AvaMultilingualNeural")

        stream_def = self.app.Item(id="s2", source="codex", author="Codex", time_str="", text="Hi", raw_text="Hi")
        self.assertEqual(self.app._resolve_item_voice(stream_def, author_voices, default_v), default_v)


class Integrations(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.app_dir = Path("C:/app")

    def tearDown(self):
        self.tmp.cleanup()

    def test_detect_only_existing(self):
        (self.home / ".cursor").mkdir()
        found = {t["id"]: t for t in vi.detect(self.home)}
        self.assertTrue(found["cursor"]["installed"])
        self.assertFalse(found["cursor"]["connected"])
        self.assertFalse(found["codex"]["installed"])

    def test_connect_keeps_foreign_hooks_and_disconnect_restores(self):
        cfg = self.home / ".cursor" / "hooks.json"
        cfg.parent.mkdir()
        foreign = {"command": "./other.sh"}
        cfg.write_text(json.dumps({"version": 1, "hooks": {"afterAgentResponse": [foreign]},
                                   "extra": True}), encoding="utf-8")
        vi.connect("cursor", self.app_dir, self.home)
        vi.connect("cursor", self.app_dir, self.home)        # повторно — без дублей
        data = json.loads(cfg.read_text(encoding="utf-8"))
        self.assertEqual(len(data["hooks"]["afterAgentResponse"]), 2)
        self.assertTrue(vi.is_connected("cursor", self.home))
        self.assertTrue(data["extra"])
        self.assertEqual(len(list(cfg.parent.glob("hooks.json.bak-voiceplayer-*"))), 1)
        vi.disconnect("cursor", self.home)
        data = json.loads(cfg.read_text(encoding="utf-8"))
        self.assertEqual(data["hooks"]["afterAgentResponse"], [foreign])
        self.assertFalse(vi.is_connected("cursor", self.home))

    def test_broken_config_is_not_overwritten(self):
        cfg = self.home / ".codex" / "hooks.json"
        cfg.parent.mkdir()
        cfg.write_text("{oops", encoding="utf-8")
        with self.assertRaises(ValueError):
            vi.connect("codex", self.app_dir, self.home)
        self.assertEqual(cfg.read_text(encoding="utf-8"), "{oops")


class Feed(unittest.TestCase):
    def test_fields_per_tool(self):
        self.assertEqual(vp_feed.extract({"last_assistant_message": " Готово ", "session_id": "abcdefgh12"}),
                         ("Готово", "abcdefgh"))
        self.assertEqual(vp_feed.extract({"text": "Из Cursor", "conversation_id": "c1"}), ("Из Cursor", "c1"))
        self.assertEqual(vp_feed.extract({"prompt_response": "Из Gemini"})[0], "Из Gemini")
        self.assertEqual(vp_feed.extract({}), ("", ""))

    def test_transcript_fallback_keeps_newlines(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.jsonl"
            lines = [{"type": "assistant", "message": {"content": [{"type": "text", "text": "старое"}]}},
                     {"type": "user", "message": {"content": "вопрос"}},
                     {"type": "assistant", "message": {"content": [{"type": "text", "text": "| A | B |\n|---|---|"}]}},
                     {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "x"}]}}]
            p.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines), encoding="utf-8")
            self.assertEqual(vp_feed.extract({"transcript_path": str(p)})[0], "| A | B |\n|---|---|")


if __name__ == "__main__":
    unittest.main()
