# -*- coding: utf-8 -*-
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import vp_piper
import vp_text


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


if __name__ == "__main__":
    unittest.main()
