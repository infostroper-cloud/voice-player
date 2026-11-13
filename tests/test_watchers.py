"""Тесты наблюдателей (Codex SQLite + CLI rollout JSONL + Antigravity transcript) и хуков."""
import json
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

import vp_feed
import vp_integrations as vi
import vp_watchers as vw


class CodexAndAntigravityWatchers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.codex = self.home / ".codex"
        self.codex.mkdir(parents=True)
        self.gemini = self.home / ".gemini"
        (self.gemini / "antigravity" / "brain").mkdir(parents=True)
        self.state_file = self.home / ".voice-player" / "watchers.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _init_codex_dbs(self, now: float):
        hist = self.codex / "thread_history_1.sqlite"
        con = sqlite3.connect(hist)
        con.execute("CREATE TABLE thread_turns (thread_id TEXT, turn_id TEXT, rollout_ordinal INT, "
                    "status TEXT, completed_at INT, final_agent_item_id TEXT, first_user_item_id TEXT)")
        con.execute("CREATE TABLE thread_items (thread_id TEXT, turn_id TEXT, item_id TEXT, "
                    "rollout_ordinal INT, item_json TEXT)")
        # старый ход до первого запуска
        con.execute("INSERT INTO thread_turns VALUES ('t1','turn0',1,'completed',?,'a0','u0')", (int(now - 60),))
        con.execute("INSERT INTO thread_items VALUES ('t1','turn0','u0',1,?)",
                    (json.dumps({"content": [{"type": "text", "text": "Старый вопрос"}]}),))
        con.execute("INSERT INTO thread_items VALUES ('t1','turn0','a0',2,?)",
                    (json.dumps({"text": "Старый ответ"}),))
        con.commit()
        con.close()

        st = self.codex / "state_5.sqlite"
        scon = sqlite3.connect(st)
        scon.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, name TEXT, title TEXT, "
                     "first_user_message TEXT, thread_source TEXT, originator TEXT, source TEXT)")
        scon.execute("INSERT INTO threads VALUES ('t1','Настройка VoicePlayer','Настройка','Привет','user','Codex Desktop','vscode')")
        scon.execute("INSERT INTO threads VALUES ('t_guard','Guardian','Guardian','','guardian_review','Codex Desktop','{\"subagent\":\"guardian\"}')")
        scon.commit()
        scon.close()
        return hist

    def test_codex_sqlite_and_rollout_dedup_and_prompts(self):
        now = time.time()
        hist = self._init_codex_dbs(now)
        watchers = vw.Watchers(self.state_file, home=self.home)
        # первый опрос инициализирует курсор и не отдаёт старую историю
        self.assertEqual(watchers.poll(), [])

        # добавляем новый ход в SQLite (с миллисекундным completed_at) и служебный ход guardian
        con = sqlite3.connect(hist)
        con.execute("INSERT INTO thread_turns VALUES ('t1','turn1',3,'completed',?,'a1','u1')",
                    (int((now + 2) * 1000),))
        con.execute("INSERT INTO thread_items VALUES ('t1','turn1','u1',3,?)",
                    (json.dumps({"content": [{"type": "text", "text": "Почини подтягивание диалогов"}]}),))
        con.execute("INSERT INTO thread_items VALUES ('t1','turn1','a1',4,?)",
                    (json.dumps({"text": "Готово, диалоги из Codex подтягиваются."}),))
        con.execute("INSERT INTO thread_turns VALUES ('t_guard','turn_g',1,'completed',?,'ag','ug')",
                    (int((now + 3) * 1000),))
        con.execute("INSERT INTO thread_items VALUES ('t_guard','turn_g','ag',1,?)",
                    (json.dumps({"text": "Служебная проверка"}),))
        con.commit()
        con.close()

        items = watchers.poll()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["source"], "codex")
        self.assertIn("Codex · Настройка VoicePlayer", items[0]["author"])
        self.assertEqual(items[0]["prompt"], "Почини подтягивание диалогов")
        self.assertEqual(items[0]["text"], "Готово, диалоги из Codex подтягиваются.")

        # Теперь терминальный Codex пишет rollout JSONL напрямую:
        # 1) дубль того же текста в t1 должен отсечься по сигнатуре
        # 2) новый ответ из терминального CLI должен прийти с меткой Codex CLI
        sdir = self.codex / "sessions" / "2026" / "11" / "11"
        sdir.mkdir(parents=True)
        rollout = sdir / "rollout-cli.jsonl"
        iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + 4))
        lines = [
            {"timestamp": iso, "type": "session_meta",
             "payload": {"session_id": "cli-sess-99", "originator": "codex-tui", "thread_source": "user", "source": "cli"}},
            {"timestamp": iso, "type": "response_item",
             "payload": {"type": "message", "role": "user",
                         "content": [{"type": "input_text", "text": "Запусти сборку в терминале"}]}},
            {"timestamp": iso, "type": "response_item",
             "payload": {"type": "message", "id": "m_cli_1", "role": "assistant", "phase": "final_answer",
                         "content": [{"type": "output_text", "text": "Сборка в терминальном Codex прошла успешно."}]}},
        ]
        rollout.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n", encoding="utf-8")

        items2 = watchers.poll()
        self.assertEqual(len(items2), 1)
        self.assertIn("Codex CLI · Запусти сборку в терминале", items2[0]["author"])
        self.assertEqual(items2[0]["prompt"], "Запусти сборку в терминале")
        self.assertEqual(items2[0]["text"], "Сборка в терминальном Codex прошла успешно.")

    def test_antigravity_watcher_and_feed_hook(self):
        now = time.time()
        iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
        watchers = vw.Watchers(self.state_file, home=self.home)
        self.assertEqual(watchers.poll(), [])

        tdir = self.gemini / "antigravity" / "brain" / "conv-12345678" / ".system_generated" / "logs"
        tdir.mkdir(parents=True)
        tfile = tdir / "transcript.jsonl"
        steps = [
            {"step_index": 0, "type": "USER_INPUT", "source": "USER_EXPLICIT", "created_at": iso,
             "content": "<USER_REQUEST>\nпривет давай проверим антигравити\n</USER_REQUEST>\n<ADDITIONAL_METADATA>"},
            {"step_index": 1, "type": "PLANNER_RESPONSE", "source": "MODEL", "created_at": iso,
             "content": "Ищу файлы…", "tool_calls": [{"name": "run_command"}]},
            {"step_index": 2, "type": "PLANNER_RESPONSE", "source": "MODEL", "created_at": iso,
             "content": "Антигравити подключён: диалог и финальный ответ видны в плеере."},
        ]
        tfile.write_text("\n".join(json.dumps(s, ensure_ascii=False) for s in steps) + "\n", encoding="utf-8")

        items = watchers.poll()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["source"], "antigravity")
        self.assertIn("Antigravity · привет давай проверим антигравити", items[0]["author"])
        self.assertEqual(items[0]["prompt"], "привет давай проверим антигравити")
        self.assertIn("Антигравити подключён", items[0]["text"])

        # проверим также vp_feed.extract_full на этом же файле Antigravity
        text, sess, prompt = vp_feed.extract_full(
            {"conversationId": "conv-12345678", "transcriptPath": str(tfile)},
            "antigravity",
        )
        self.assertIn("Антигравити подключён", text)
        self.assertEqual(sess, "conv-123")
        self.assertEqual(prompt, "привет давай проверим антигравити")

    def test_ollama_watcher(self):
        ollama_dir = self.home / "AppData" / "Local" / "Ollama"
        ollama_dir.mkdir(parents=True)
        db_path = ollama_dir / "db.sqlite"
        con = sqlite3.connect(db_path)
        con.execute("CREATE TABLE chats (id INTEGER PRIMARY KEY, title TEXT)")
        con.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, chat_id INTEGER, role TEXT, content TEXT, stream INTEGER, model_name TEXT, created_at TEXT)")
        con.execute("INSERT INTO chats VALUES (1, 'Чат с Ollama')")
        con.execute("INSERT INTO messages VALUES (1, 1, 'user', 'Объясни квантовые вычисления', 0, 'llama3.2', '2026-11-13T12:00:00Z')")
        con.execute("INSERT INTO messages VALUES (2, 1, 'assistant', 'Квантовые вычисления используют кубиты...', 0, 'llama3.2', '2026-11-13T12:00:05Z')")
        con.commit()
        con.close()

        watcher = vw.OllamaWatcher(home=self.home)
        self.assertTrue(watcher.available())
        # Первый запуск фиксирует last_id (пропускает старые сообщения)
        first_items = watcher.poll()
        self.assertEqual(len(first_items), 0)

        # Добавляем новое сообщение
        con = sqlite3.connect(db_path)
        con.execute("INSERT INTO messages VALUES (3, 1, 'user', 'Как это применить?', 0, 'llama3.2', '2026-11-13T12:01:00Z')")
        con.execute("INSERT INTO messages VALUES (4, 1, 'assistant', 'В криптографии и оптимизации.', 0, 'llama3.2', '2026-11-13T12:01:05Z')")
        con.commit()
        con.close()

        items = watcher.poll()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["source"], "ollama")
        self.assertIn("В криптографии и оптимизации", items[0]["text"])
        self.assertEqual(items[0]["prompt"], "Как это применить?")

    def test_deepseek_watcher(self):
        sess_dir = self.home / ".dsh" / "storages" / "session_projcache" / "sessions"
        sess_dir.mkdir(parents=True)
        sess_file = sess_dir / "session-test-uuid.json"
        now_ts = int(time.time() * 1000)
        session_data = {
            "record": {
                "rows": {
                    "turnOutline": {
                        "val": {
                            "turns": [
                                {
                                    "turnId": "t1",
                                    "prompt": "Как ускорить Python?",
                                    "response": "Используйте встроенные функции и Cython.",
                                    "timestamp": now_ts,
                                }
                            ]
                        }
                    }
                }
            }
        }
        sess_file.write_text(json.dumps(session_data, ensure_ascii=False), encoding="utf-8")

        watcher = vw.DeepSeekWatcher(home=self.home)
        self.assertTrue(watcher.available())
        # Первый запуск инициализирует состояние
        watcher.poll()

        # Добавляем новый ход
        session_data["record"]["rows"]["turnOutline"]["val"]["turns"].append({
            "turnId": "t2",
            "prompt": "А ещё варианты?",
            "response": "Также можно использовать PyPy и Numba.",
            "timestamp": now_ts + 1000,
        })
        sess_file.write_text(json.dumps(session_data, ensure_ascii=False), encoding="utf-8")

        items = watcher.poll()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["source"], "deepseek")
        self.assertIn("Также можно использовать PyPy", items[0]["text"])
        self.assertEqual(items[0]["prompt"], "А ещё варианты?")

    def test_kimi_watcher(self):
        wire_dir = self.home / ".kimi-desktop" / "daimon-share" / "daimon" / "runtime" / "kimi-code" / "home" / "sessions" / "s1" / "c1" / "agents" / "main"
        wire_dir.mkdir(parents=True)
        wire_file = wire_dir / "wire.jsonl"
        wire_file.write_text(json.dumps({"type": "session.start"}) + "\n", encoding="utf-8")

        watcher = vw.KimiWatcher(home=self.home)
        self.assertTrue(watcher.available())
        watcher.poll()

        # Добавляем сообщение
        evt = {
            "type": "context.append_message",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Привет! Я Kimi, готов помочь."}],
            }
        }
        with open(wire_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(evt, ensure_ascii=False) + "\n")

        items = watcher.poll()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["source"], "kimi")
        self.assertIn("Привет! Я Kimi", items[0]["text"])

    def test_antigravity_connect_disconnect_in_integrations(self):
        app_dir = Path("C:/app")
        vi.connect("antigravity", app_dir, self.home)
        self.assertTrue(vi.is_connected("antigravity", self.home))
        cfg = json.loads((self.home / ".gemini" / "config" / "hooks.json").read_text(encoding="utf-8"))
        self.assertIn("voice-player", cfg)
        self.assertEqual(len(cfg["voice-player"]["Stop"]), 1)
        vi.disconnect("antigravity", self.home)
        self.assertFalse(vi.is_connected("antigravity", self.home))


if __name__ == "__main__":
    unittest.main()
