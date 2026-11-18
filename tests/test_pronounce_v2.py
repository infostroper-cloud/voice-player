# -*- coding: utf-8 -*-
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import vp_piper
import vp_text
import vp_tts


class TestPronunciationV2(unittest.TestCase):
    def test_versions_currencies_and_multipliers(self):
        out = vp_text._smart_normalize("Релиз v1.2.24 стоит $12 или €10, ускорение 2.5×, около ~15 мс.")
        self.assertIn("версия 1.2.24", out)
        self.assertIn("12 долларов", out)
        self.assertIn("10 евро", out)
        self.assertIn("2.5 икс", out)
        self.assertIn("около 15 миллисекунд", out)

    def test_yo_restoration_and_abbreviations(self):
        out = vp_text._smart_normalize("Крон включен, файл сохранен, т.е. см. кол-во и т.н. 100K строк.")
        self.assertIn("включён", out)
        self.assertIn("сохранён", out)
        self.assertIn("то есть", out)
        self.assertIn("смотри", out)
        self.assertIn("количество", out)
        self.assertIn("так называемый", out)
        self.assertIn("100 тысяч", out)

    def test_piper_speakable_versions_and_terms(self):
        out = vp_piper.speakable("Обновлён webhook и PKCE для v1.2.24, rebase прошёл.")
        self.assertIn("версия 1 точка 2 точка 24" if "точка" in out else "версия 1.2.24", out)
        self.assertIn("вебхук", out)
        self.assertIn("пи кей си и", out)
        self.assertIn("ребейз", out)

    def test_brief_text_keeps_outcome_and_short_text_unchanged(self):
        short = "Короткий ответ: всё работает."
        self.assertEqual(vp_text.brief_text(short), short)
        long_msg = (
            "Начинаю проверку проекта и всех зависимостей в репозитории. "
            "Просмотрен первый промежуточный каталог с временными артефактами и логами. "
            "Просмотрен второй промежуточный каталог с резервными файлами и кэшем. "
            "Просмотрен третий промежуточный каталог со старыми логами и сборками. "
            "Просмотрен четвёртый промежуточный каталог для анализа структуры модулей. "
            "Просмотрен пятый промежуточный каталог с конфигурациями и тестами. "
            "Итог: все 87 тестов пройдены успешно, ошибка рендера исправлена. "
            "Следующий шаг: перезапустите плеер."
        )
        b = vp_text.brief_text(long_msg, max_sentences=4, max_chars=450)
        self.assertLess(len(b), len(long_msg))
        self.assertIn("Начинаю проверку", b)
        self.assertIn("87 тестов пройдены успешно", b)
        self.assertIn("перезапустите плеер", b)

    def test_render_audio_blocking_brief_mode(self):
        long_msg = "Первое предложение. " + ("Промежуточная строка с деталями. " * 20) + "Итог: готово."
        captured = []

        def fake_synth(text, rate, mp3_path, voice):
            captured.append(text)
            Path(mp3_path).write_bytes(b"ID3test")
            return []

        item = SimpleNamespace(
            id="brief-test", text=long_msg, parts_done=[], parts_fed=0,
            audio_path=None, audio_ready=False, audio_error="", more_parts=False,
            marks=[], rendered_voice="", deleted=False, source="codex",
        )
        with tempfile.TemporaryDirectory() as td:
            vp_tts.render_audio_blocking(
                item, voice="en-US-BrianMultilingualNeural",
                audio_dir=Path(td), synthesize_fn=fake_synth, brief=True,
            )
        self.assertTrue(captured)
        self.assertLess(len(captured[0]), len(long_msg))


if __name__ == "__main__":
    unittest.main()
