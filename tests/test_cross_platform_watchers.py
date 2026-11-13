"""
Tests verifying that Ollama, DeepSeek, and Kimi watchers correctly discover
configurations and data paths across Windows, macOS, and Linux structures.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vp_watchers import OllamaWatcher, DeepSeekWatcher, KimiWatcher
import vp_integrations


class TestCrossPlatformWatchers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    # --- Ollama ---
    def test_ollama_discovery_windows(self):
        db_path = self.home / "AppData" / "Local" / "Ollama" / "db.sqlite"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        db_path.write_text("sqlite dummy")
        watcher = OllamaWatcher(home=self.home)
        self.assertTrue(watcher.available())
        self.assertEqual(watcher._find_db(), db_path)

    def test_ollama_discovery_macos(self):
        db_path = self.home / "Library" / "Application Support" / "Ollama" / "db.sqlite"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        db_path.write_text("sqlite dummy")
        watcher = OllamaWatcher(home=self.home)
        self.assertTrue(watcher.available())
        self.assertEqual(watcher._find_db(), db_path)

    def test_ollama_discovery_linux(self):
        db_path = self.home / ".local" / "share" / "ollama" / "db.sqlite"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        db_path.write_text("sqlite dummy")
        watcher = OllamaWatcher(home=self.home)
        self.assertTrue(watcher.available())
        self.assertEqual(watcher._find_db(), db_path)

    # --- Kimi Desktop ---
    def test_kimi_discovery_windows(self):
        sess_dir = self.home / "AppData" / "Roaming" / "kimi-desktop" / "daimon-share" / "daimon" / "runtime" / "kimi-code" / "home" / "sessions"
        sess_dir.mkdir(parents=True, exist_ok=True)
        watcher = KimiWatcher(home=self.home)
        self.assertTrue(watcher.available())
        self.assertEqual(watcher._get_base_dir(), sess_dir)

    def test_kimi_discovery_macos(self):
        sess_dir = self.home / "Library" / "Application Support" / "kimi-desktop" / "daimon-share" / "daimon" / "runtime" / "kimi-code" / "home" / "sessions"
        sess_dir.mkdir(parents=True, exist_ok=True)
        watcher = KimiWatcher(home=self.home)
        self.assertTrue(watcher.available())
        self.assertEqual(watcher._get_base_dir(), sess_dir)

    def test_kimi_discovery_linux(self):
        sess_dir = self.home / ".config" / "kimi-desktop" / "daimon-share" / "daimon" / "runtime" / "kimi-code" / "home" / "sessions"
        sess_dir.mkdir(parents=True, exist_ok=True)
        watcher = KimiWatcher(home=self.home)
        self.assertTrue(watcher.available())
        self.assertEqual(watcher._get_base_dir(), sess_dir)

    # --- DeepSeek ---
    def test_deepseek_discovery_default_dsh(self):
        sess_dir = self.home / ".dsh" / "storages" / "session_projcache" / "sessions"
        sess_dir.mkdir(parents=True, exist_ok=True)
        watcher = DeepSeekWatcher(home=self.home)
        self.assertTrue(watcher.available())

    def test_deepseek_discovery_macos(self):
        sess_dir = self.home / "Library" / "Application Support" / "DeepSeek" / "storages" / "session_projcache" / "sessions"
        sess_dir.mkdir(parents=True, exist_ok=True)
        watcher = DeepSeekWatcher(home=self.home)
        self.assertTrue(watcher.available())

    def test_deepseek_discovery_linux(self):
        sess_dir = self.home / ".config" / "deepseek" / "storages" / "session_projcache" / "sessions"
        sess_dir.mkdir(parents=True, exist_ok=True)
        watcher = DeepSeekWatcher(home=self.home)
        self.assertTrue(watcher.available())

    # --- Integrations detect ---
    def test_integrations_detect_cross_platform(self):
        (self.home / "Library" / "Application Support" / "Ollama").mkdir(parents=True, exist_ok=True)
        (self.home / ".config" / "kimi-desktop").mkdir(parents=True, exist_ok=True)
        (self.home / ".config" / "deepseek").mkdir(parents=True, exist_ok=True)
        tools = {t["id"]: t["installed"] for t in vp_integrations.detect(home=self.home)}
        self.assertTrue(tools.get("ollama"))
        self.assertTrue(tools.get("kimi"))
        self.assertTrue(tools.get("deepseek"))


if __name__ == "__main__":
    unittest.main()
