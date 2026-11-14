# VoicePlayer — Developer & AI Agent Guidelines (AGENTS.md)

This document defines the architectural rules, watcher mechanics, deployment boundaries, and engineering practices for VoicePlayer. Any AI model (Antigravity, Claude Code, Codex, Cursor, etc.) working on this repository MUST follow these rules.

---

## 1. Workspace Boundaries & Deployment Mirrors

* **Primary Repository**: `D:\AI\projects\pro\voice-player` (active branch: `voice-player-local-tts`).
* **Public Mirror**: `C:\dev\voice-player-public` (branch: `main`).
* **Installed Desktop Runtime**: `C:\Users\tiko_\AppData\Local\Programs\VoicePlayer\versions\1.2.5\`.
* **STRICT PROHIBITION**: NEVER touch or edit files in `C:\dev\pro` (this is a separate project).
* **NO HEADLESS GUI SPAWN**: NEVER launch `app.py` or `launch.pyw` via background agent subshell (`run_command` / `Start-Process`). Agent subshells run in non-interactive sessions without display rights. Doing so locks port 3737 with an invisible headless process that plays audio but cannot show a window on the user's monitor, blocking the user's desktop shortcut from opening. The user must always launch the desktop GUI directly.
* **Multi-Target Sync**: When fixing or updating files (`app.py`, `vp_watchers.py`, `vp_integrations.py`, `vp_i18n.py`, etc.):
  1. Make and test changes in `D:\AI\projects\pro\voice-player`.
  2. Copy updated files to `C:\dev\voice-player-public` and commit/push.
  3. Copy updated files to `C:\Users\tiko_\AppData\Local\Programs\VoicePlayer\versions\1.2.5\` so the live installed app runs the update.

---

## 2. AI Watchers Architecture (`vp_watchers.py`)

VoicePlayer automatically listens for responses from AI coding assistants and agents (Antigravity, Claude Code, Codex, Ollama, DeepSeek Harness).

### DeepSeek Harness (`.dsh/sessions/`)
1. **Target User Sessions Only**:
   * Glob pattern MUST be `*/session-*/session.v4.jsonl.zstd`.
   * NEVER use generic `*/*/session.v4.jsonl.zstd`. DeepSeek Harness runs internal graph-building workers in bare UUID folders (`<uuid>/subagent/descriptor`). A broad glob will flood the playlist with hundreds of internal subagents.
   * `_parse_zstd_session` must immediately reject any session containing `subagent/descriptor`.
2. **Intermediate Reasoning vs Final Output**:
   * Controlled by `ai_intermediate_reasoning` preference (default: `False`).
   * When `False`, `<think>...</think>` tags and reasoning token blocks are stripped so only the final assistant reply is spoken.
3. **Non-blocking Turn Emission**:
   * Do NOT require `turn/end` or `completed == True` before yielding text.
   * In multi-step tool calls or pauses waiting for human approval (`approval/asked`), assistant text must still be emitted in real-time so the user hears what is happening before approval.
4. **Diff Tracking**:
   * Track changes using `seen_texts` diffs per turn rather than raw character length, preventing lost edits during dynamic turn updates.

---

## 3. Queue & Session State Management (`app.py`)

1. **Playlist Persistence (`SESSION_FILE` = `~/.voice-player-session.json`)**:
   * `clear_queue()` MUST call `self._save_session()` immediately so the cleared state persists across restarts.
   * `clear_queue()` MUST also call `self.watchers.mark_all_seen()` to advance all watcher offsets and seen sets to EOF, ensuring past dialogues never re-emerge.
   * `_restore_session()` must never fall back to `_load_saved_items()` if `SESSION_FILE` exists (even if empty).
   * Pinned items (`📌`, `it.pinned`) are preserved during `clear_queue()`.
2. **App Startup Behavior**:
   * On startup, DO NOT automatically call `_pull_recent_ai(limit_per_source=5)`. Startup must strictly restore the saved `SESSION_FILE` and start watcher background threads.
   * Pulling past history is strictly on-demand (user presses F5, clicks the Refresh button, or uses the button in Settings).
3. **In-place Turn Updates (`enqueue`)**:
   * When a watcher yields progressive text for an ongoing turn, `enqueue()` must update `existing.text` and notify the renderer without creating duplicate queue entries.

---

## 4. UI & Layout Guidelines

1. **Toolbar & Pill Bar Responsive Wrapping**:
   * All multi-button rows (such as AI category filter tabs in Settings, document/audio loading rows, tools bars) must use `_wrap_bar(frame)` to prevent horizontal clipping on narrow window widths (e.g. 720px).
2. **Settings AI Filter Tab**:
   * Provides quick category filters (`all`, `connected`, `agents`, `ide`, `cli`, `local_llm`, `chat`, `terminal`).
   * `🟢 Подключённые ({n})` filters tools that are installed and connected or active GUI apps.

---

## 5. Testing & Verification Protocol

* Run full test suite before any commit:
  ```powershell
  cd "D:\AI\projects\pro\voice-player"
  python -m unittest discover tests
  ```
  All tests must pass (currently 73+ tests).
* Verify single-instance server health:
  ```powershell
  curl http://127.0.0.1:3737/api/ping
  ```
