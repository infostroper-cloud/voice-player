import tempfile
import unittest
from pathlib import Path

import vp_ai
import vp_auth
import vp_text


class TestVpAi(unittest.TestCase):
    def test_detect_response_language(self):
        code_ru, _ = vp_ai._detect_response_language("Привет! API вернул JSON без ошибок.")
        self.assertEqual(code_ru, "ru")
        code_en, _ = vp_ai._detect_response_language("Hello world, this API returns JSON cleanly.")
        self.assertEqual(code_en, "en")

    def test_default_pronounce_preserves_english_and_helps_russian(self):
        ru = vp_text._apply_pronounce("Новый API для Python готов", vp_text.DEFAULT_PRONOUNCE)
        self.assertIn("эй-пи-ай", ru)
        self.assertIn("Пайтон", ru)
        en = vp_text._apply_pronounce("The new API for Python is ready", vp_text.DEFAULT_PRONOUNCE)
        self.assertIn("API", en)
        self.assertIn("Python", en)

    def test_extractive_summary_compresses_long_text(self):
        long_text = (
            "VoicePlayer озвучивает ответы ИИ-агентов и длинные статьи в реальном времени. "
            "Сегодня мы обновили движок синтеза речи и добавили локальный кэш фонем. "
            "Теперь реплики читаются по частям без блокировки интерфейса и без задержек. "
            "Также улучшена работа с таблицами и подсветкой активного слова при чтении. "
            "Кроме того, появилась поддержка Piper и Kokoro для полностью автономной работы. "
            "Это позволяет программистам слушать код-ревью и логи без нагрузки на глаза."
        )
        vp_ai.configure(provider="offline")
        res = vp_ai.ai_summary(long_text)
        self.assertTrue(res)
        self.assertLess(len(res), len(long_text))
        vp_ai.configure(provider="auto")

    def test_tags_and_entities(self):
        self.assertEqual(vp_ai.tag_for("Нужно срочно сделать деплой"), "🔴")
        self.assertEqual(vp_ai.tag_for("Надо подготовить отчёт"), "✅")
        self.assertEqual(vp_ai.tag_for("Когда будет релиз?"), "❓")
        spans = vp_ai.extract_entities("Созвон в 14:30 с Антон по задаче 42")
        self.assertGreaterEqual(len(spans), 2)


class TestVpAuth(unittest.TestCase):
    def test_pkce_and_account_roundtrip(self):
        verifier, challenge = vp_auth._pkce_pair()
        self.assertGreater(len(verifier), 32)
        self.assertNotIn("=", challenge)
        with tempfile.TemporaryDirectory() as td:
            acc_file = Path(td) / "account.json"
            vp_auth.configure(account_file=acc_file, lead_webhook_url="")
            self.assertTrue(vp_auth.should_prompt_onboarding())
            vp_auth.mark_skipped()
            self.assertFalse(vp_auth.should_prompt_onboarding())
            vp_auth.register_email_profile("dev@example.com", "Alex", marketing_opt_in=True)
            loaded = vp_auth.load_account()
            self.assertEqual(loaded["email"], "dev@example.com")
            self.assertTrue(vp_auth.is_signed_in())


if __name__ == "__main__":
    unittest.main()
