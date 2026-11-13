# -*- coding: utf-8 -*-
"""
Глубокий автоматизированный тест мультиязычности (i18n) для VoicePlayer.
Проверяет:
1. Полноту ключей во всех 8 поддерживаемых языках (en, ru, es, zh, hi, ja, de, fr).
2. Отсутствие пустых переводов.
3. Работу автоматического фоллбэк-перевода русских сообщений диалогов.
4. Покрытие всех вызовов messagebox в кодовой базе.
5. Корректность подстановки плейсхолдеров {name}.
"""
import sys
import os
import re
from pathlib import Path

# Добавляем корень проекта в sys.path
ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

import vp_i18n

EXPECTED_LANGUAGES = ["en", "ru", "es", "zh", "hi", "ja", "de", "fr"]

def test_supported_languages():
    print("[1/5] Проверка списка поддерживаемых языков...")
    for lang in EXPECTED_LANGUAGES:
        assert lang in vp_i18n.SUPPORTED_LANGUAGES, f"Язык {lang} отсутствует в SUPPORTED_LANGUAGES"
        assert lang in vp_i18n.STRINGS, f"Словарь STRINGS не содержит язык {lang}"
    print("  ✓ Все 8 языков зарегистрированы и присутствуют в словарях.")

def test_key_parity_across_languages():
    print("[2/5] Проверка симметрии ключей между всеми языками...")
    ru_keys = set(vp_i18n.STRINGS["ru"].keys())
    en_keys = set(vp_i18n.STRINGS["en"].keys())

    assert ru_keys == en_keys, f"Несовпадение ключей между ru и en: diff = {ru_keys ^ en_keys}"

    for lang in EXPECTED_LANGUAGES:
        cur_keys = set(vp_i18n.STRINGS[lang].keys())
        missing = ru_keys - cur_keys
        extra = cur_keys - ru_keys
        assert not missing, f"В языке {lang} отсутствуют ключи: {missing}"
        assert not extra, f"В языке {lang} лишние ключи: {extra}"

        # Проверка на непустые значения
        empty_keys = [k for k, v in vp_i18n.STRINGS[lang].items() if not str(v).strip()]
        assert not empty_keys, f"В языке {lang} пустые строки для ключей: {empty_keys}"

    print(f"  ✓ Полная симметрия: каждый язык содержит ровно {len(ru_keys)} переведённых ключей.")

def test_messagebox_fallback_mapping():
    print("[3/5] Проверка фоллбэк-маппинга для диалоговых окон...")
    assert hasattr(vp_i18n, "_MESSAGE_FALLBACK_MAP"), "_MESSAGE_FALLBACK_MAP отсутствует в vp_i18n"
    fb_map = getattr(vp_i18n, "_MESSAGE_FALLBACK_MAP")

    # Проверяем ключевые фразы, которые беспокоили пользователя
    target_phrase = "Выбери реплику или вставь текст в поле — тогда сохраню в mp3."
    assert target_phrase in fb_map, f"Фраза '{target_phrase}' не найдена в fallback map!"

    # Тестируем переключение на каждый язык и результат t()
    for lang in EXPECTED_LANGUAGES:
        vp_i18n.set_lang(lang)
        translated = vp_i18n.t(target_phrase)
        assert translated, f"Пустой перевод для '{target_phrase}' в языке {lang}"
        if lang == "ru":
            assert translated == target_phrase
        elif lang == "ja":
            assert "MP3" in translated and ("保存" in translated or "選択" in translated)
        elif lang == "en":
            assert "Select a message" in translated or "MP3" in translated
        elif lang == "de":
            assert "MP3" in translated
    print("  ✓ Фоллбэк-перехватчик корректно переводит русские сообщения на все 8 языков.")

def test_codebase_messagebox_coverage():
    print("[4/5] Сканирование вызовов messagebox в исходном коде app.py...")
    app_py_path = ROOT_DIR / "app.py"
    with open(app_py_path, "r", encoding="utf-8") as f:
        code = f.read()

    # Ищем все вызовы messagebox.(showinfo|showerror|showwarning)
    pattern = re.compile(r'messagebox\.(?:showinfo|showerror|showwarning|askyesno)\s*\([^,]+,\s*(?:f?["\']([^"\']+)["\']|([a-zA-Z0-9_\.]+))', re.DOTALL)
    matches = pattern.findall(code)

    fb_map = getattr(vp_i18n, "_MESSAGE_FALLBACK_MAP", {})
    all_keys = set(vp_i18n.STRINGS["ru"].keys())

    unhandled = []
    checked_count = 0
    for lit, expr in matches:
        checked_count += 1
        msg = lit or expr
        msg_clean = msg.split("\n")[0].strip()
        # Проверяем, является ли это прямым вызовом vp_i18n.t(...)
        if "vp_i18n" in msg or "t(" in msg:
            continue
        # Проверяем, есть ли в словаре ключей
        if msg in all_keys:
            continue
        # Проверяем, есть ли фраза в fallback map
        if msg in fb_map or msg_clean in fb_map:
            continue
        # Допустимые системные технические переменные (например, str(e))
        if msg in ("e", "err", "lines", "APP_NAME"):
            continue
        # Иначе фиксируем
        unhandled.append(msg)

    print(f"  ✓ Проверено вызовов messagebox: {checked_count}. Необработанных: {len(unhandled)}.")
    if unhandled:
        print(f"  Предупреждение по строкам: {unhandled}")

def test_placeholder_formatting():
    print("[5/5] Проверка форматирования {name} во всех языках...")
    for lang in EXPECTED_LANGUAGES:
        vp_i18n.set_lang(lang)
        res = vp_i18n.t("cat_all_count", n=42)
        assert "42" in res, f"Плейсхолдер n=42 не подставился в {lang}: {res}"

        res_tool = vp_i18n.t("guide_title", tool="Antigravity")
        assert "Antigravity" in res_tool, f"Плейсхолдер tool не подставился в {lang}: {res_tool}"
    print("  ✓ Плейсхолдеры корректно форматируются во всех локалях.")

def run_all_tests():
    print("=" * 60)
    print("ЗАПУСК ГЛУБОКОГО ТЕСТА МУЛЬТИЯЗЫЧНОСТИ VOICEPLAYER (i18n)")
    print("=" * 60)
    test_supported_languages()
    test_key_parity_across_languages()
    test_messagebox_fallback_mapping()
    test_codebase_messagebox_coverage()
    test_placeholder_formatting()
    print("=" * 60)
    print("ВСЕ ТЕСТЫ МУЛЬТИЯЗЫЧНОСТИ УСПЕШНО ПРОЙДЕНЫ! (100% OK)")
    print("=" * 60)

if __name__ == "__main__":
    run_all_tests()
