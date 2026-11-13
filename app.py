"""
voice-player desktop app — нативное Windows-окно (tkinter), без браузера.

Источники реплик:
  1) ruflo team-chat доска (pro/.swarm/memory.db, namespace='team-chat')
  2) POST /api/enqueue на http://127.0.0.1:3737  ← хук Claude Code

TTS: edge-tts, голос en-US-BrianMultilingualNeural, темп +15%.
Аудио: Windows MCI (winmm.dll через ctypes) — ноль внешних зависимостей.

Запуск: pythonw app.py  (или start-app.cmd)
"""
from __future__ import annotations
import ctypes
import json
import os
os.environ["TK_SILENCE_DEPRECATION"] = "1"
import queue
import random
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tkinter import ttk, messagebox, filedialog
from typing import Optional
from uuid import uuid4

import vp_updater
if __name__ == "__main__":
    vp_updater.bootstrap(Path(__file__).resolve().parent)   # до тяжёлых библиотек: могла выйти версия новее

import vp_integrations
import vp_layout
import vp_piper
import vp_watchers
import vp_i18n
from vp_i18n import t
from vp_tables import find_tables, html_to_markdown, normalize_tables

# ============================ ВИНДОВЫЕ ИНТЕГРАЦИИ ============================

# AppUserModelID — чтобы Windows распознавал нас как отдельное приложение
# (для группировки в taskbar и закрепления).
APP_USER_MODEL_ID = "odolopo.voice-player.1"

IS_WIN = (os.name == "nt")
IS_MAC = (sys.platform == "darwin")

if IS_WIN:
    if getattr(sys, "frozen", False):
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
        except Exception:
            pass
    _winmm = ctypes.WinDLL("winmm")
    _winmm.mciSendStringW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_void_p]
    _winmm.mciSendStringW.restype = ctypes.c_uint
else:
    _winmm = None


def _read_clipboard_html() -> str:
    """Формат «HTML Format» буфера обмена Windows (там браузер и Word хранят таблицы)."""
    if not IS_WIN:
        return ""
    from ctypes import wintypes
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalSize.restype = ctypes.c_size_t
    fmt = user32.RegisterClipboardFormatW("HTML Format")
    if not fmt or not user32.IsClipboardFormatAvailable(fmt) or not user32.OpenClipboard(None):
        return ""
    try:
        handle = user32.GetClipboardData(fmt)
        if not handle:
            return ""
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return ""
        try:
            return ctypes.string_at(ptr, kernel32.GlobalSize(handle)).split(b"\0", 1)[0].decode("utf-8", "replace")
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def _open_in_file_manager(path):
    """Открыть папку/файл в проводнике — кросс-платформенно (Windows/macOS/Linux)."""
    p = str(path)
    if IS_WIN:
        os.startfile(p)            # type: ignore[attr-defined]
    elif IS_MAC:
        import subprocess; subprocess.Popen(["open", p])
    else:
        import subprocess; subprocess.Popen(["xdg-open", p])


def mci(cmd: str) -> tuple[int, str]:
    if _winmm is None:
        return (1, "MCI is Windows-only")
    buf = ctypes.create_unicode_buffer(512)
    err = _winmm.mciSendStringW(cmd, buf, 511, None)
    return (err, buf.value)


class MciPlayer:
    def __init__(self):
        self._lock = threading.Lock()
        self._alias: Optional[str] = None
        self._paused = False
        self._volume = 1.0

    def _close(self):
        if self._alias:
            mci(f'close {self._alias}')
            self._alias = None
            self._paused = False

    def load_and_play(self, path: Path, speed: float = 1.0):
        with self._lock:
            self._close()
            alias = f"vp_{uuid4().hex[:8]}"
            err, reply = mci(f'open "{path}" type mpegvideo alias {alias}')
            if err != 0:
                err, reply = mci(f'open "{path}" alias {alias}')
                if err != 0:
                    raise RuntimeError(f"MCI open error {err}: {reply}")
            self._alias = alias
            self._paused = False
            mci(f'setaudio {alias} volume to {int(max(0.0, min(1.0, self._volume)) * 1000)}')
            err, reply = mci(f'play {alias}')
            if err != 0:
                raise RuntimeError(f"MCI play error {err}: {reply}")

    def pause(self):
        with self._lock:
            if self._alias and not self._paused:
                mci(f'pause {self._alias}'); self._paused = True

    def resume(self):
        with self._lock:
            if self._alias and self._paused:
                mci(f'resume {self._alias}'); self._paused = False

    def stop(self):
        with self._lock:
            self._close()

    def set_volume(self, v: float):
        with self._lock:
            self._volume = max(0.0, min(1.0, v))
            if self._alias:
                mci(f'setaudio {self._alias} volume to {int(self._volume * 1000)}')

    def is_paused(self) -> bool:
        return self._paused

    def is_playing(self) -> bool:
        with self._lock:
            if not self._alias:
                return False
            err, reply = mci(f'status {self._alias} mode')
            return err == 0 and reply.strip().lower() == "playing"

    def has_track(self) -> bool:
        return self._alias is not None

    def is_finished(self) -> bool:
        with self._lock:
            if not self._alias:
                return False
            err, reply = mci(f'status {self._alias} mode')
            return err == 0 and reply.strip().lower() in ("stopped", "not ready")

    def set_speed(self, mult: float):
        pass  # MCI не умеет менять темп на лету — заглушка для совместимости интерфейса

    def duration(self) -> float:
        with self._lock:
            if not self._alias:
                return 0.0
            err, reply = mci(f'status {self._alias} length')
            try: return int(reply) / 1000.0 if err == 0 else 0.0
            except ValueError: return 0.0

    def position(self) -> float:
        with self._lock:
            if not self._alias:
                return 0.0
            err, reply = mci(f'status {self._alias} position')
            try: return int(reply) / 1000.0 if err == 0 else 0.0
            except ValueError: return 0.0

    def seek(self, seconds: float):
        with self._lock:
            if not self._alias:
                return
            mci(f'seek {self._alias} to {int(max(0.0, seconds) * 1000)}')
            mci(f'play {self._alias}')
            self._paused = False

    def envelope(self, buckets: int = 500):
        return None   # MCI не даёт PCM — waveform недоступна


# ---- потоковый плеер с мгновенной регулировкой скорости (тон сохраняется) ----
# Бэкенд: miniaudio (декод mp3) + audiotsm WSOLA (time-stretch) + sounddevice (вывод).
try:
    import numpy as _np
    import miniaudio as _miniaudio
    import sounddevice as _sd
    from audiotsm import wsola as _wsola
    from audiotsm.io.array import ArrayReader as _ArrayReader, ArrayWriter as _ArrayWriter
    _STREAM_OK = True
except Exception as _e:  # библиотек нет — останемся на MCI
    _STREAM_OK = False
    _STREAM_IMPORT_ERR = str(_e)



class AfPlayer:
    """Нативный аудиоплеер для macOS (afplay) и Linux (aplay/ffplay/mpv) без внешних C-библиотек."""
    def __init__(self):
        self._lock = threading.Lock()
        self._proc = None
        self._paused = False
        self._volume = 1.0
        self._speed = 1.0
        self._cur_path = None
        self._start_time = 0.0
        self._duration = 0.0

    def load_and_play(self, path: Path, speed: float = 1.0):
        with self._lock:
            self.stop()
            self._cur_path = Path(path)
            self._speed = max(0.5, min(2.5, speed))
            self._start_time = time.time()
            # Попробуем оценить длительность через ffprobe или miniaudio
            self._duration = 0.0
            if _MINIAUDIO_OK:
                try:
                    info = _ma.get_file_info(str(path))
                    self._duration = float(info.duration)
                except Exception:
                    pass
            cmd = []
            if sys.platform == "darwin":
                # afplay встроен в macOS: поддерживает темп -r и громкость -v (шкала 0..1)
                cmd = ["/usr/bin/afplay", "-r", f"{self._speed:g}", "-v", f"{self._volume:g}", str(path)]
            elif shutil.which("mpv"):
                cmd = ["mpv", "--no-video", f"--speed={self._speed:g}", f"--volume={int(self._volume*100)}", str(path)]
            elif shutil.which("ffplay"):
                cmd = ["ffplay", "-nodisp", "-autoexit", "-af", f"atempo={self._speed:g},volume={self._volume:g}", str(path)]
            elif shutil.which("aplay"):
                cmd = ["aplay", "-q", str(path)]
            if cmd:
                try:
                    self._proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except Exception as e:
                    log_line(f"AfPlayer popen error: {e}")
                    self._proc = None

    def pause(self):
        with self._lock:
            if self._proc and self._proc.poll() is None and not self._paused:
                try:
                    self._proc.send_signal(signal.SIGSTOP)
                    self._paused = True
                except Exception:
                    pass

    def resume(self):
        with self._lock:
            if self._proc and self._proc.poll() is None and self._paused:
                try:
                    self._proc.send_signal(signal.SIGCONT)
                    self._paused = False
                except Exception:
                    pass

    def stop(self):
        with self._lock:
            if self._proc:
                try:
                    if self._proc.poll() is None:
                        self._proc.terminate()
                        self._proc.wait(timeout=0.3)
                except Exception:
                    try:
                        self._proc.kill()
                    except Exception:
                        pass
                self._proc = None
            self._paused = False

    def set_volume(self, v: float):
        self._volume = max(0.0, min(1.0, v))

    def is_paused(self) -> bool:
        return self._paused

    def is_playing(self) -> bool:
        with self._lock:
            if not self._proc:
                return False
            return self._proc.poll() is None and not self._paused

    def has_track(self) -> bool:
        return self._cur_path is not None

    def is_finished(self) -> bool:
        with self._lock:
            if not self._proc:
                return True
            return self._proc.poll() is not None

    def set_speed(self, mult: float):
        self._speed = max(0.5, min(2.5, mult))
        if self.is_playing() and self._cur_path:
            self.load_and_play(self._cur_path, self._speed)

    def duration(self) -> float:
        return self._duration

    def position(self) -> float:
        if self._start_time > 0 and self.is_playing():
            return (time.time() - self._start_time) * self._speed
        return 0.0


class StreamPlayer:
    """Плеер с time-stretch: скорость 0.5–2.0× меняется на лету без пересоздания файла
    и без изменения высоты голоса. Тяжёлый стретч идёт в фоновом воркере, поэтому ни
    UI-поток, ни аудио-колбэк не блокируются."""

    def __init__(self):
        self._lock = threading.RLock()
        self._stream = None
        self._src = None          # исходный PCM (N, ch) float32
        self._sr = 44100
        self._ch = 2
        self._out = None          # обработанный сегмент (M, ch) для текущей скорости
        self._out_pos = 0
        self._seg_src_start = 0   # позиция в исходнике, с которой начат текущий сегмент
        self._speed = 1.0
        self._target_speed = 1.0
        self._volume = 1.0
        self._paused = False
        self._has = False
        self._finished = False
        self._seek_req = None      # запрос перемотки (кадр исходника) для воркера
        self._gen = 0              # номер загруженного трека: дозагрузка не попадёт в чужой
        # воркер перестройки сегмента (скорость + перемотка)
        self._speed_evt = threading.Event()
        self._shutdown = False
        threading.Thread(target=self._speed_worker, daemon=True, name="stream-speed").start()

    # ---------- DSP ----------
    @staticmethod
    def _stretch(frames, speed):
        if frames is None or frames.shape[0] == 0:
            return frames
        if abs(speed - 1.0) < 1e-3:
            return frames.copy()
        data = frames.T.copy()                       # (ch, N)
        reader = _ArrayReader(data)
        writer = _ArrayWriter(channels=data.shape[0])
        tsm = _wsola(channels=data.shape[0], speed=speed)
        tsm.run(reader, writer)
        return writer.data.T.copy()                  # (M, ch)

    def _src_pos_now(self) -> int:
        return self._seg_src_start + int(self._out_pos * self._speed)

    def _speed_worker(self):
        while not self._shutdown:
            self._speed_evt.wait()
            if self._shutdown:
                break
            self._speed_evt.clear()
            time.sleep(0.04)                         # коалесцируем серию сдвигов ползунка
            with self._lock:
                if self._src is None:
                    continue
                target = self._target_speed
                seek = self._seek_req
                self._seek_req = None
                if seek is not None:
                    src_start = max(0, min(int(seek), self._src.shape[0]))
                elif abs(target - self._speed) < 1e-3:
                    continue                          # нет изменений — нечего перестраивать
                else:
                    src_start = self._src_pos_now()
                src = self._src
                gen = self._gen
            try:
                new_out = self._stretch(src[src_start:], target)   # тяжёлое — вне лока
            except Exception:
                continue
            with self._lock:
                if self._src is src:                  # трек ещё тот же
                    self._out = new_out
                    self._out_pos = 0
                    self._seg_src_start = src_start
                    self._speed = target
                    self._finished = False
                elif self._gen == gen and self._seek_req is None:
                    # пока считали, в трек дозагрузилась часть — пересчитать с той же точки
                    self._seek_req = src_start
                    self._speed_evt.set()

    # ---------- аудио-колбэк (поток PortAudio) ----------
    def _callback(self, outdata, frames, time_info, status):
        # Колбэк PortAudio — НИКОГДА не должен бросать исключение (иначе аварийный стоп/краш).
        try:
            with self._lock:
                if self._paused or self._out is None:
                    outdata.fill(0); return
                chunk = self._out[self._out_pos:self._out_pos + frames]
                n = chunk.shape[0]
                if n > 0:
                    outdata[:n] = chunk * self._volume
                if n < frames:
                    outdata[n:] = 0
                    self._finished = True
                self._out_pos += n
        except Exception:
            try: outdata.fill(0)
            except Exception: pass

    # ---------- API (совместим с MciPlayer) ----------
    def set_volume(self, v: float):
        with self._lock:
            self._volume = max(0.0, min(1.0, float(v)))

    def set_speed(self, mult: float):
        mult = max(0.5, min(2.0, float(mult)))
        with self._lock:
            self._target_speed = mult
            if self._src is None:
                self._speed = mult
                return
        self._speed_evt.set()                         # будим воркер — мгновенно, без тяжёлой работы

    def seek(self, seconds: float):
        with self._lock:
            if self._src is None:
                return
            self._seek_req = int(max(0.0, seconds) * self._sr)
            self._paused = False
        self._speed_evt.set()

    def duration(self) -> float:
        with self._lock:
            return (self._src.shape[0] / self._sr) if self._src is not None else 0.0

    def position(self) -> float:
        with self._lock:
            if self._src is None:
                return 0.0
            total = self._src.shape[0]
            return min(self._src_pos_now(), total) / self._sr

    def envelope(self, buckets: int = 500):
        """Огибающая амплитуды исходника по бакетам (0..1) — для отрисовки waveform."""
        with self._lock:
            src = self._src
        if src is None or src.shape[0] == 0:
            return None
        try:
            mono = src.mean(axis=1) if src.ndim == 2 else src
            n = mono.shape[0]
            step = max(1, n // buckets)
            usable = (n // step) * step
            arr = _np.abs(mono[:usable]).reshape(-1, step).max(axis=1)
            peak = float(arr.max()) or 1.0
            return (arr / peak)
        except Exception:
            return None

    def load_and_play(self, path, speed: float = 1.0):
        dec = _miniaudio.decode_file(str(path), output_format=_miniaudio.SampleFormat.FLOAT32)
        pcm = _np.array(dec.samples, dtype=_np.float32).reshape(-1, dec.nchannels)
        speed = max(0.5, min(2.0, float(speed)))
        out0 = self._stretch(pcm, speed)             # стартовый сегмент готовим вне лока
        with self._lock:
            # Переиспользуем ОДИН поток на всю сессию (параметры edge-tts постоянны):
            # пересоздаём только если формат изменился — иначе просто подменяем буфер.
            need_new = (self._stream is None or self._sr != dec.sample_rate or self._ch != dec.nchannels)
            self._src = pcm; self._sr = dec.sample_rate; self._ch = dec.nchannels
            self._speed = speed; self._target_speed = speed
            self._out = out0; self._out_pos = 0; self._seg_src_start = 0
            self._finished = False; self._paused = False; self._has = True
            self._gen += 1
            if need_new:
                self._open_stream()

    def generation(self) -> int:
        with self._lock:
            return self._gen

    def append_file(self, path, gen: int) -> bool:
        """Дописать продолжение в конец играющего трека (длинная реплика озвучивается частями)."""
        dec = _miniaudio.decode_file(str(path), output_format=_miniaudio.SampleFormat.FLOAT32)
        pcm = _np.array(dec.samples, dtype=_np.float32).reshape(-1, dec.nchannels)
        with self._lock:
            if self._src is None or gen != self._gen or dec.sample_rate != self._sr or dec.nchannels != self._ch:
                return False
            speed = self._speed
        extra = self._stretch(pcm, speed)
        with self._lock:
            if self._src is None or gen != self._gen:
                return False
            self._src = _np.concatenate((self._src, pcm))
            if abs(speed - self._speed) < 1e-3 and self._seek_req is None:
                self._out = _np.concatenate((self._out, extra))
                self._finished = False
            else:                                  # скорость или позиция меняются — воркер пересоберёт
                self._speed_evt.set()
            return True

    def _open_stream(self):
        self._close_stream()
        st = _sd.OutputStream(samplerate=self._sr, channels=self._ch,
                              dtype="float32", callback=self._callback)
        st.start()
        with self._lock:
            self._stream = st

    def _close_stream(self):
        with self._lock:
            st = self._stream
            self._stream = None
        if st is not None:
            # ВАЖНО: закрываем поток ВНЕ self._lock, иначе PortAudio ждёт завершения
            # _callback, а _callback ждёт self._lock → нативный дедлок драйвера звука!
            try:
                st.abort()
                st.close()
            except Exception:
                pass

    def pause(self):
        with self._lock:
            if self._has and not self._paused:
                self._paused = True

    def resume(self):
        with self._lock:
            if self._has and self._paused:
                self._paused = False

    def stop(self):
        # Останавливаем воспроизведение, но поток НЕ закрываем (переиспользуем) —
        # колбэк просто выдаёт тишину. Закрываем поток только в shutdown().
        with self._lock:
            self._src = None; self._out = None
            self._has = False; self._paused = False
            self._finished = False; self._out_pos = 0

    def is_playing(self) -> bool:
        with self._lock:
            return self._has and not self._paused and not self._finished

    def is_paused(self) -> bool:
        with self._lock:
            return self._paused

    def has_track(self) -> bool:
        with self._lock:
            return self._has

    def is_finished(self) -> bool:
        with self._lock:
            return self._has and self._finished

    def shutdown(self):
        self._shutdown = True
        self._speed_evt.set()
        self.stop()
        self._close_stream()


# ============================ КОНСТАНТЫ ============================

APP_NAME    = "VoicePlayer"
if getattr(sys, "frozen", False):                       # собрано PyInstaller'ом
    APP_DIR = Path(sys.executable).resolve().parent
    _RES_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))  # ресурсы внутри bundle
else:
    APP_DIR = Path(__file__).resolve().parent
    _RES_DIR = APP_DIR
APP_VERSION = vp_updater.read_version(APP_DIR)
UPDATE_FIRST_S = int(os.environ.get("VP_UPDATE_FIRST_S") or 15)        # первая проверка через 15 сек после запуска
UPDATE_EVERY_S = int(os.environ.get("VP_UPDATE_EVERY_S") or 7200)       # регулярная проверка каждые 2 часа
UPDATE_IDLE_S  = int(os.environ.get("VP_UPDATE_IDLE_S") or 180)        # фоновый тихий перезапуск при простое 3 мин
SPEECH_REVERT_AFTER = 1793608200.0                                     # 02.11.2026 12:30: вышла 1.1.4
UPDATE_HEALTHY_S = 20                                                  # столько окно должно проработать
HTTP_HOST   = "127.0.0.1"
HTTP_PORT   = int(os.environ.get("VP_HTTP_PORT") or 3737)


def _find_board_db() -> Path:
    """Доска команды: явный путь, рядом с проектом или на старом месте в OneDrive,
    откуда её пока не переносили (см. CLAUDE.md проекта)."""
    origin = os.environ.get("VP_ORIGIN_DIR")                # установленная версия: рядом с копией, из которой запущена
    candidates = [*([Path(origin).parent / ".swarm" / "memory.db"] if origin else []),
                  APP_DIR.parent / ".swarm" / "memory.db",
                  Path.home() / "OneDrive" / "Desktop" / "pro" / ".swarm" / "memory.db"]
    env = os.environ.get("VP_BOARD_DB")
    if env:
        return Path(env)
    return next((p for p in candidates if p.exists()), candidates[0])


DB_PATH     = _find_board_db()
AUDIO_DIR   = Path(tempfile.gettempdir()) / "voice-player-audio"
ICON_PATH   = (APP_DIR / "icon.ico") if (APP_DIR / "icon.ico").exists() else (_RES_DIR / "icon.ico")
_PIPER_OK   = vp_piper.is_available()
TTS_VOICE   = vp_piper.DEFAULT_VOICE if _PIPER_OK else "en-US-BrianMultilingualNeural"
# каталог голосов для выбора в UI: подпись → id (piper:… работает на компьютере, остальные — edge-tts)
VOICES = {
    **(vp_piper.VOICE_LABELS if _PIPER_OK else {}),
    # Мультиязычные нейроголоса Edge TTS
    "English — Brian (Multilingual)":    "en-US-BrianMultilingualNeural",
    "English — Ava (Expressive)":        "en-US-AvaMultilingualNeural",
    "English — Andrew":                  "en-US-AndrewMultilingualNeural",
    "Русский — Дмитрий":                 "ru-RU-DmitryNeural",
    "Русский — Светлана":                "ru-RU-SvetlanaNeural",
    "Español — Alvaro (España)":         "es-ES-AlvaroNeural",
    "Español — Elvira (España)":         "es-ES-ElviraNeural",
    "中文 — 晓晓 Xiaoxiao":               "zh-CN-XiaoxiaoNeural",
    "中文 — 云希 Yunxi":                 "zh-CN-YunxiNeural",
    "हिन्दी — Madhur (India)":           "hi-IN-MadhurNeural",
    "हिन्दी — Swara (India)":            "hi-IN-SwaraNeural",
    "日本語 — 七海 Nanami":               "ja-JP-NanamiNeural",
    "日本語 — 圭太 Keita":                "ja-JP-KeitaNeural",
    "Deutsch — Conrad":                  "de-DE-ConradNeural",
    "Deutsch — Katja":                   "de-DE-KatjaNeural",
    "Français — Henri":                  "fr-FR-HenriNeural",
    "Français — Denise":                 "fr-FR-DeniseNeural",
}

# Чёткие монохромные иконки из системного шрифта Segoe Fluent Icons / MDL2 (векторные,
# резкие на любом размере, в цвет темы). Значение = (codepoint, эмодзи-фолбэк если шрифта нет).
ICON_GLYPHS = {
    "prev": ('\ue892', '⏮'), "play": ('\ue768', '▶'), "pause": ('\ue769', '⏸'),
    "next": ('\ue893', '⏭'), "repeat": ('\ue8ee', '🔁'), "delete": ('\ue74d', '🗑'),
    "listenall": ('\ueb9d', '⏩'), "queue": ('\ue8fd', '☰'), "textpanel": ('\ue7c3', '📄'),
    "mini": ('\ue8a7', '❐'), "more": ('\ue712', '⋮'), "close": ('\ue711', '✕'),
    "bookmark": ('\ue840', '🔖'), "mic": ('\ue720', '🎤'), "sysaudio": ('\ue767', '🔊'),
    "live": ('\ue93c', '🎙'), "archive": ('\ue8f1', '📚'), "summary": ('\ue8a5', '📝'),
    "dict": ('\ue8bd', '🗣'), "playlistsave": ('\ue74e', '💾'), "playlists": ('\uec4f', '🎵'),
    "digest": ('\ue789', '📰'), "commands": ('\ue7f6', '🎧'), "diag": ('\ue9d9', '🩺'),
    "copy": ('\ue8c8', '📋'), "globe": ('\ue774', '🌐'), "broom": ('\uea99', '🧹'),
    "search": ('\ue721', '🔍'), "scrolldown": ('\ue70d', '⤓'), "settings": ('\ue713', '⚙'),
    "restore": ('\ue73f', '⤢'), "sentprev": ('\ue76b', '◄◄'), "sentnext": ('\ue76c', '►►'),
    "refresh": ('\ue72c', '🔄'), "robot": ('\ue99a', '🤖'),
}
TTS_RATE    = "+0%"   # нейтрально: скорость регулирует плеер (time-stretch), а не edge-tts
TTS_TIMEOUT_S = 90    # верхний предел: edge-tts временами отдаёт звук медленно, не убиваем живой рендер
                      # (реальные обрывы дочерний процесс ловит сам через connect/receive_timeout ~18с)
TTS_RETRIES   = 1
TTS_FIRST_PART_CHARS = 300   # первая часть короче: длинный ответ начинает звучать через несколько секунд
TTS_PART_CHARS = 700         # остальные части: каждая с запасом успевает в TTS_TIMEOUT_S
TTS_PARALLEL_PARTS = 3       # части одной реплики озвучиваем параллельно, чтобы звук не догонял рендер
TTS_RETRY_DELAY_S = 2.0
TTS_AUTO_RETRY     = 3      # авто-повтор упавших по таймауту реплик (самолечение при сбое сети)
TTS_AUTO_RETRY_BASE_S = 20 # задержка авто-повтора нарастает: 20с, 40с, 60с (предохранитель от шторма)
POLL_BOARD_INTERVAL_S = 2.5
AI_SOURCES = {"antigravity", "claude-code", "codex", "cursor", "gemini"}   # реплики нейросетей (а не команды)
PREFS_FILE  = Path.home() / ".voice-player-prefs.json"
SAVED_FILE  = Path.home() / ".voice-player-saved.json"   # закреплённые + избранные (легаси-формат)
SESSION_FILE = Path.home() / ".voice-player-session.json"  # вся очередь — восстанавливается при старте
SESSION_MAX = 300                                        # сколько последних реплик помнить
PRONOUNCE_FILE = Path.home() / ".voice-player-pronounce.json"   # словарь произношения
ARCHIVE_DB  = Path.home() / ".voice-player-archive.db"   # полный искомый архив


def _geometry_ok(geom) -> bool:
    """Геометрия «WxH+X+Y» пригодна для восстановления окна (не 1x1 и не уехавшая за левый/верхний край)."""
    s = str(geom or "")
    if "+-" in s:
        return False
    m = re.match(r"(\d+)x(\d+)", s)
    return bool(m) and int(m.group(1)) >= 760 and int(m.group(2)) >= 460
LOG_FILE    = Path.home() / ".voice-player.log"

AUDIO_DIR.mkdir(parents=True, exist_ok=True)


def cleanup_audio_cache(max_age_days: int = 7):
    """Удалить из аудио-кэша файлы старше N дней — чтобы %TEMP% не рос бесконечно."""
    try:
        cutoff = time.time() - max_age_days * 86400
        for f in AUDIO_DIR.glob("*.mp3"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except Exception:
                pass
    except Exception:
        pass


def log_line(msg: str):
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass


# ============================ ЛОГ ПАДЕНИЙ (диагностика) ============================
# Плеер запускается через pythonw без консоли — ошибки иначе теряются. Пишем ВСЁ в файл:
# необработанные исключения главного потока и потоков, ошибки tkinter-колбэков и даже
# жёсткие падения нативных библиотек (segfault) через faulthandler.
import traceback as _traceback
import faulthandler as _faulthandler

CRASH_LOG = Path.home() / ".voice-player-crash.log"


def log_crash(where: str, exc_type=None, exc=None, tb=None):
    if exc_type is None:
        exc_type, exc, tb = sys.exc_info()
    try:
        with CRASH_LOG.open("a", encoding="utf-8") as f:
            f.write(f"\n{'='*64}\n{time.strftime('%Y-%m-%d %H:%M:%S')}  CRASH @ {where}\n")
            _traceback.print_exception(exc_type, exc, tb, file=f)
            f.flush()
    except Exception:
        pass
    try:
        name = getattr(exc_type, "__name__", str(exc_type))
        log_line(f"CRASH @ {where}: {name}: {exc}")
    except Exception:
        pass


def install_crash_logging():
    # 1) stderr/stdout -> файл (pythonw их выбрасывает). Сюда же пишет faulthandler.
    try:
        _err = open(CRASH_LOG, "a", encoding="utf-8", buffering=1)
        sys.stderr = _err
        sys.stdout = _err
    except Exception:
        _err = None
    # 2) faulthandler — ловит даже segfault нативных аудио-библиотек
    try:
        if _err is not None:
            _faulthandler.enable(file=_err, all_threads=True)
    except Exception:
        pass
    # 3) необработанные исключения главного потока
    sys.excepthook = lambda t, v, tb: log_crash("excepthook", t, v, tb)
    # 4) исключения в фоновых потоках
    try:
        threading.excepthook = lambda a: log_crash(
            f"thread:{getattr(a.thread,'name','?')}", a.exc_type, a.exc_value, a.exc_traceback)
    except Exception:
        pass
    log_line(f"=== START pid={os.getpid()} python={sys.version.split()[0]} ===")


@dataclass
class Item:
    id:        str
    source:    str
    author:    str
    time_str:  str
    text:      str
    raw_text:  str
    date_str:  str = ""
    rendered_rate: str = ""        # темп edge-tts, с которым реально отрендерен mp3
    rendered_voice: str = ""       # голос готовых частей: досказывать реплику им же (mp3 склеиваются)
    shown_cache: tuple = field(default=("", ""), compare=False, repr=False)   # (исходник, текст для показа)
    audio_path: Optional[Path] = None
    audio_ready: bool = False
    audio_error: Optional[str] = None
    played:    bool = False
    deleted:   bool = False        # не удаляем из списка реально — флаг, чтобы скрывать
    pinned:    bool = False        # закреплённая реплика (переживает очистку/перезагрузку)
    favorite:  bool = False        # в избранном
    marks:     list = field(default_factory=list)   # тайминги предложений: (start,end,char0,char1)
    word_marks: list = field(default_factory=list)  # тайминги слов для подсветки «по словам»
    is_file:   bool = False         # загруженный аудиофайл (не озвучка edge-tts)
    file_path: Optional[Path] = None
    transcribing: bool = False      # идёт распознавание
    bookmarks: list = field(default_factory=list)   # закладки (секунды)
    created_at: float = field(default_factory=time.time)
    render_attempts: int = 0        # счётчик авто-повторов рендера (предохранитель от шторма)
    parts_done: list = field(default_factory=list)  # готовые части длинной реплики (файлы по порядку)
    parts_fed: int = 0              # сколько частей уже отдано плееру
    more_parts: bool = False        # реплика уже играет, но продолжение ещё озвучивается
    forced: bool = False            # пользователь сам нажал «Озвучить» — фильтры не прячут
    prompt: str = ""                # запрос пользователя (вопрос диалога из Antigravity/Codex)
    title: str = ""                 # заголовок диалога/треда


# ============================ TTS ============================

def _slash_to_or(t: str) -> str:
    """Косую черту между словами читаем как «или»; между числами (дата/дробь) оставляем."""
    import re
    t = re.sub(r"\bи\s*/\s*или\b", "и или", t, flags=re.IGNORECASE)   # частый оборот «и/или»
    out, n = [], len(t)
    for i, ch in enumerate(t):
        if ch == "/":
            prev = t[i - 1] if i > 0 else ""
            nxt = t[i + 1] if i + 1 < n else ""
            is_path = prev.isascii() and nxt.isascii() and prev.isalnum() and nxt.isalnum()  # origin/main, 3/4
            out.append("/" if is_path else " или ")
        else:
            out.append(ch)
    return "".join(out)


# словарь произношения: как читать имена/аббревиатуры (заполняется приложением)
_PRONOUNCE: dict = {}


def _apply_pronounce(t: str) -> str:
    if not _PRONOUNCE:
        return t
    import re
    for k in sorted(_PRONOUNCE, key=len, reverse=True):   # длинные ключи раньше
        v = _PRONOUNCE[k]
        if not k.strip():
            continue
        t = re.sub(r"(?<!\w)" + re.escape(k) + r"(?!\w)", v, t, flags=re.IGNORECASE)
    return t


# распространённые сокращения → полные слова (иначе читаются буквами/мусором).
# Однозначные только: «г.» (год/город), «с.»/«р.» намеренно НЕ трогаем.
_ABBREV = [
    (r"\bи\s+т\.?\s*д\.?",   "и так далее"),
    (r"\bи\s+т\.?\s*п\.?",   "и тому подобное"),
    (r"\bи\s+др\.",          "и другие"),
    (r"\bт\.\s*е\.",         "то есть"),
    (r"\bт\.\s*к\.",         "так как"),
    (r"\bв\s+т\.\s*ч\.",     "в том числе"),
    (r"\bруб\.",             "рублей"),
    (r"\bкоп\.",             "копеек"),
    (r"\bшт\.",              "штук"),
    (r"\bтыс\.",             "тысяч"),
    (r"\bмлн\b\.?",          "миллионов"),
    (r"\bмлрд\b\.?",         "миллиардов"),
    (r"\bкв\.\s*м\b",        "квадратных метров"),
    (r"\bнапр\.",            "например"),
    (r"\bп\.\s*(\d)",        r"пункт \1"),
]

# единицы измерения — раскрываем ТОЛЬКО после числа (иначе покалечим обычные слова).
# Порядок важен: более длинные/составные раньше, голая «м» — в самом конце.
_UNITS = [
    (r"км/ч",  "километров в час"),
    (r"м/с",   "метров в секунду"),
    (r"тыс\.?\s*руб", "тысяч рублей"),
    (r"кВт",   "киловатт"),
    (r"кг",    "килограммов"),
    (r"мм",    "миллиметров"),
    (r"см",    "сантиметров"),
    (r"дм",    "дециметров"),
    (r"км",    "километров"),
    (r"мл",    "миллилитров"),
    (r"мин",   "минут"),
    (r"сек",   "секунд"),
    (r"шт",    "штук"),
    (r"чел",   "человек"),
    (r"ГБ",    "гигабайт"),
    (r"МБ",    "мегабайт"),
    (r"КБ",    "килобайт"),
    (r"ТБ",    "терабайт"),
    (r"Гц",    "герц"),
    (r"°C|°С",  "градусов Цельсия"),
    (r"т",     "тонн"),
    (r"л",     "литров"),
    (r"ч",     "часов"),
    (r"м",     "метров"),
]


def _expand_abbrev(t: str) -> str:
    import re
    for pat, rep in _ABBREV:
        t = re.sub(pat, rep, t, flags=re.IGNORECASE)
    # площадь/объём раньше голых единиц (чтобы «5 м²» не стало «5 метров ²»)
    t = re.sub(r"(\d)\s*м²|(\d)\s*м2\b", lambda m: f"{m.group(1) or m.group(2)} квадратных метров ", t)
    t = re.sub(r"(\d)\s*м³|(\d)\s*м3\b", lambda m: f"{m.group(1) or m.group(2)} кубических метров ", t)
    for pat, rep in _UNITS:                         # единицы только после числа
        t = re.sub(rf"(\d)\s*(?:{pat})\b", rf"\1 {rep} ", t)
    t = t.replace("₽", " рублей ").replace("°", " градусов ")
    return t


def _structure_for_speech(t: str) -> str:
    """Разбить текст на смысловые предложения, чтобы нейроголос ловил интонацию и паузы.
    Каждая строка-мысль (пункт списка, заголовок) получает завершающий знак — иначе
    модель читает всё одной монотонной фразой. Переносы строк сохраняем для показа."""
    import re
    out = []
    for line in t.split("\n"):
        s = line.strip()
        if not s:
            out.append(""); continue
        if re.fullmatch(r"[-=_·•*–—\s]{2,}", s):       # горизонтальный разделитель — выкинуть
            continue
        s = re.sub(r"^[\-–—•▪◦·]\s+", "", s)           # маркер списка в начале строки убрать
        if not s:
            continue
        if s[-1] not in ".!?…:,;":                      # нет завершающего знака → закрыть мысль
            s += "."
        out.append(s)
    return "\n".join(out)


def _strip_noise_tokens(t: str) -> str:
    """Служебные токены, которые голос читает по буквам/мусором, заменить на смысл или убрать:
    пути к файлам, UUID, git-хэши, @упоминания. Запускать ПОСЛЕ замены URL на «ссылка»."""
    import re
    # пути к файлам: Windows (C:\...) и явные POSIX (/usr,/var,/home,~/...) → «путь к файлу»
    t = re.sub(r"[A-Za-z]:\\[^\s]+", " путь к файлу ", t)
    t = re.sub(r"(?:~|/(?:usr|var|etc|home|tmp|opt|mnt|root))/[^\s]+", " путь к файлу ", t)
    # UUID → «идентификатор»
    t = re.sub(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b",
               " идентификатор ", t)
    # git-хэш / длинная hex-строка (минимум 7, обязательно с цифрой) → «хэш»
    t = re.sub(r"\b(?=[0-9a-f]*\d)[0-9a-f]{7,40}\b", " хэш ", t)
    # @упоминание → просто имя (без знака собачки, который читается «эт»)
    t = re.sub(r"(?<!\w)@([A-Za-zА-Яа-яёЁ_][\w]*)", r"\1", t)
    return t


def _smart_normalize(t: str) -> str:
    """Привести текст к «человеческому» произношению: символы → смысл, мусор убрать."""
    import re
    t = _apply_pronounce(t)                                    # словарь произношения
    t = re.sub(r"https?://\S+", " ссылка ", t)                 # URL → «ссылка» (до чистки путей)
    t = _strip_noise_tokens(t)                                 # хэши/пути/UUID/упоминания → смысл или прочь
    t = _expand_abbrev(t)                                      # сокращения → полные слова
    # эмодзи и пиктограммы (в т.ч. стрелки, значки) — убрать, они читаются мусором
    t = re.sub("["
               "\U0001F300-\U0001FAFF"   # эмодзи и пиктограммы
               "\U00002600-\U000027BF"   # символы и dingbats
               "\U0001F1E6-\U0001F1FF"   # флаги
               "\U00002190-\U000021FF"   # стрелки
               "\U00002B00-\U00002BFF"   # доп. стрелки/символы
               "\U0000FE0F"              # variation selector
               "]", " ", t)
    t = re.sub(r"-{1,2}>|=>", " ", t)                          # текстовые стрелки -> / =>
    t = _slash_to_or(t)                                        # / → «или»
    t = t.replace("&", " и ")
    t = re.sub(r"№\s*(\d)", r"номер \1", t); t = t.replace("№", " номер ")
    t = re.sub(r"(\d)\s*%", r"\1 процентов", t); t = t.replace("%", " процентов ")
    t = re.sub(r"(\d)\s*[xх×]\s*(\d)", r"\1 на \2", t)         # 24x36 → «24 на 36»
    t = re.sub(r"\s+([.,!?;:])", r"\1", t)                     # пробел перед знаком убрать
    return t


def _md_tables_to_text(t: str) -> str:
    """Markdown-таблицы → читаемые строки «Колонка: значение; …» (по строке на запись).
    И видно структурно в поле, и озвучивается естественно (палки/разделители убираются)."""
    import re
    lines = t.split("\n")

    def cells(line: str) -> list:
        line = line.strip()
        if line.startswith("|"): line = line[1:]
        if line.endswith("|"): line = line[:-1]
        return [c.strip() for c in line.split("|")]

    def is_sep(line: str) -> bool:
        if "|" not in line:
            return False
        cs = [c for c in cells(line) if c != ""]
        return bool(cs) and all(re.match(r"^:?-{1,}:?$", c) for c in cs)

    out, i, n = [], 0, len(lines)
    while i < n:
        line = lines[i]
        if "|" in line and i + 1 < n and is_sep(lines[i + 1]):
            header = cells(line)
            i += 2
            while i < n and "|" in lines[i] and lines[i].strip():
                row = cells(lines[i]); i += 1
                pairs = []
                for idx, val in enumerate(row):
                    if not val:
                        continue
                    h = header[idx].strip() if idx < len(header) else ""
                    pairs.append(f"{h}: {val}" if h else val)
                if pairs:
                    out.append("; ".join(pairs))
            continue
        out.append(line)
        i += 1
    return "\n".join(out)


def _clean_for_tts(s: str) -> str:
    import re
    t = normalize_tables(s)                        # таблицы из Excel, терминала, одной строкой
    t = re.sub(r"```[\s\S]*?```", " ", t)
    t = re.sub(r"`[^`]*`", " ", t)
    t = _md_tables_to_text(t)                      # таблицы → читаемые строки (до удаления |)
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)
    t = re.sub(r"[*#>`_]", "", t)
    t = t.replace("|", " ")                        # уцелевшие одиночные | (не таблица)
    t = _smart_normalize(t)
    t = _structure_for_speech(t)                   # смысловые предложения (интонация/паузы)
    # сохранить переносы строк (таблицы/списки), схлопнуть только лишние пробелы
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r" *\n *", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def _wrap_bar(frame, pad: int = 6):
    """Сделать ряд кнопок переносимым: при нехватке ширины кнопки уходят на новую
    строку (как flex-wrap), а не прячутся за краем окна. Раскладывает детей через grid
    по событию <Configure>; ширины кнопок фиксированы, поэтому пересчёт только при
    смене ширины контейнера (без циклов перерисовки)."""
    state = {"w": -1}

    def reflow(_e=None):
        width = frame.winfo_width()
        if width <= 1 or width == state["w"]:
            return
        state["w"] = width
        kids = list(frame.winfo_children())
        if not kids:
            return
        for k in kids:                            # сперва снять ВСЕ с pack: нельзя мешать
            if k.winfo_manager() == "pack":       # pack и grid в одном контейнере (TclError)
                k.pack_forget()
        x = r = c = 0
        for k in kids:
            kw = max(36, k.winfo_reqwidth()) + pad
            if c > 0 and x + kw > width:          # не влезает — на новую строку
                r += 1; c = 0; x = 0
            k.grid(row=r, column=c, padx=(0, pad), pady=2, sticky="w")
            x += kw; c += 1

    frame.bind("<Configure>", reflow)
    return frame


_tts_tls = threading.local()


def _get_tts_loop():
    """Один переиспользуемый SelectorEventLoop на поток.

    Почему так: `asyncio.run()` создаёт и закрывает НОВЫЙ event loop на каждый вызов.
    На Windows дефолтный loop — ProactorEventLoop, и его повторное создание/закрытие
    в рабочем потоке со временем течёт IOCP-хэндлами → новые сетевые соединения
    зависают (свежий процесс работает, поживший — таймаутит на каждом рендере).
    SelectorEventLoop легче и надёжнее в потоках; переиспользуем его без churn.
    """
    import asyncio
    loop = getattr(_tts_tls, "loop", None)
    if loop is None or loop.is_closed():
        try:
            loop = asyncio.SelectorEventLoop()
        except Exception:
            loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        _tts_tls.loop = loop
    return loop


def _synthesize_inproc(text: str, rate: str, mp3_path: "Path", voice: str = TTS_VOICE) -> tuple:
    """Озвучка edge-tts ВНУТРИ процесса (фолбэк для собранного .exe, где нет отдельного python)."""
    import asyncio, edge_tts

    async def _run():
        comm = edge_tts.Communicate(text, voice or TTS_VOICE, rate=rate,
                                    connect_timeout=12, receive_timeout=18)
        sents = []; words = []
        with open(mp3_path, "wb") as f:
            async for chunk in comm.stream():
                t = chunk.get("type")
                if t == "audio":
                    f.write(chunk["data"])
                elif t in ("SentenceBoundary", "WordBoundary"):
                    start = chunk["offset"] / 1e7
                    end = start + chunk["duration"] / 1e7
                    (sents if t == "SentenceBoundary" else words).append(
                        (start, end, str(chunk.get("text") or "")))
        return sents, words

    loop = _get_tts_loop()
    return loop.run_until_complete(asyncio.wait_for(_run(), timeout=TTS_TIMEOUT_S))


# Скрипт для дочернего python-процесса: пишет mp3 и JSON-тайминги. Изолирует сеть
# от потоков/GIL/накопленного состояния главного процесса (standalone всегда работает).
_TTS_CHILD_CODE = r'''
import sys, asyncio, json, os
text_path, mp3_path, marks_path, voice, rate, done_path = sys.argv[1:7]
import edge_tts
text = open(text_path, encoding="utf-8").read()
async def run():
    comm = edge_tts.Communicate(text, voice, rate=rate, connect_timeout=12, receive_timeout=18)
    sents = []; words = []
    with open(mp3_path, "wb") as f:
        async for ch in comm.stream():
            ty = ch.get("type")
            if ty == "audio":
                f.write(ch["data"])
            elif ty == "SentenceBoundary":
                s = ch["offset"] / 1e7; e = s + ch["duration"] / 1e7
                sents.append([s, e, str(ch.get("text") or "")])
            elif ty == "WordBoundary":
                s = ch["offset"] / 1e7; e = s + ch["duration"] / 1e7
                words.append([s, e, str(ch.get("text") or "")])
    json.dump({"sents": sents, "words": words}, open(marks_path, "w", encoding="utf-8"))
    # файл-сигнал «готово» пишем САМЫМ последним — родитель ждёт именно его,
    # не полагаясь на корректный выход дочернего процесса
    with open(done_path, "w") as d:
        d.write("ok"); d.flush(); os.fsync(d.fileno())
asyncio.run(run())
'''


def _synthesize(text: str, rate: str, mp3_path: "Path", voice: str = TTS_VOICE) -> tuple:
    """Озвучить text. Основной путь — ОТДЕЛЬНЫЙ python-процесс: каждый рендер в чистом
    изолированном процессе (как standalone, который работает всегда), без зависаний из-за
    состояния/потоков/GIL живого приложения. В собранном .exe отдельного python нет —
    тогда фолбэк на внутрипроцессную озвучку. Локальный голос Piper работает внутри процесса:
    сети нет, а модель долго загружать на каждую часть."""
    voice = voice or TTS_VOICE
    if vp_piper.is_local(voice):
        return vp_piper.synthesize(text, voice, mp3_path), []
    if getattr(sys, "frozen", False):
        return _synthesize_inproc(text, rate, mp3_path, voice)

    import subprocess, json
    txt_path = mp3_path.with_suffix(".in.txt")
    marks_path = mp3_path.with_suffix(".marks.json")
    err_path = mp3_path.with_suffix(".err.txt")
    done_path = mp3_path.with_suffix(".done")
    proc = None
    try:
        for p in (marks_path, done_path):
            try: p.unlink()
            except Exception: pass
        txt_path.write_text(text, encoding="utf-8")
        _t0 = time.time()
        # БЕЗ каналов (pipes): stdin/stdout в DEVNULL, ошибки в файл, данные в файлы.
        # Не ждём корректного выхода дочернего процесса (он по неясной причине иногда
        # зависает на выходе именно когда порождён из этого долгоживущего процесса) —
        # опрашиваем файл-сигнал .done, который ребёнок пишет последним.
        errf = open(err_path, "wb")
        try:
            proc = subprocess.Popen(
                [sys.executable, "-c", _TTS_CHILD_CODE,
                 str(txt_path), str(mp3_path), str(marks_path), voice or TTS_VOICE, rate, str(done_path)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errf,
                close_fds=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            deadline = _t0 + TTS_TIMEOUT_S
            while time.time() < deadline:
                if done_path.exists():
                    break                                  # готово (звук + тайминги записаны)
                if proc.poll() is not None:
                    break                                  # процесс завершился (успех/ошибка)
                time.sleep(0.15)
        finally:
            errf.close()
        done = done_path.exists()
        # прибиваем дочерний процесс (если ещё жив — например, завис на выходе)
        if proc.poll() is None:
            try: proc.kill()
            except Exception: pass
        if not done:
            err_txt = ""
            try: err_txt = err_path.read_text(encoding="utf-8", errors="replace")
            except Exception: pass
            last = (err_txt.strip().splitlines() or ["TimeoutError: рендер не завершился"])[-1]
            raise RuntimeError(last[:200])
        if os.environ.get("VP_TTS_DEBUG"):
            log_line(f"  tts subprocess за {time.time()-_t0:.1f}с voice={voice}")
        sents = []; words = []
        if marks_path.exists():
            data = json.loads(marks_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):                       # новый формат {sents, words}
                for row in data.get("sents", []):
                    sents.append((row[0], row[1], row[2]))
                for row in data.get("words", []):
                    words.append((row[0], row[1], row[2]))
            else:                                            # старый формат — список предложений
                for row in data:
                    sents.append((row[0], row[1], row[2]))
        return sents, words
    finally:
        if proc is not None and proc.poll() is None:
            try: proc.kill()
            except Exception: pass
        for p in (txt_path, marks_path, err_path, done_path):
            try: p.unlink()
            except Exception: pass


def _extract_document_text(path: "Path") -> str:
    """Извлечь читаемый текст из документа: PDF (PyMuPDF→pypdf), DOCX (из zip без зависимостей),
    TXT/MD/CSV (как есть), RTF (грубая чистка управляющих слов)."""
    import re, html
    ext = path.suffix.lower()
    if ext == ".pdf":
        try:
            import fitz                                   # PyMuPDF
            doc = fitz.open(str(path))
            try:
                return "\n\n".join(pg.get_text() for pg in doc)
            finally:
                doc.close()
        except Exception:
            from pypdf import PdfReader
            return "\n\n".join((pg.extract_text() or "") for pg in PdfReader(str(path)).pages)
    if ext == ".docx":
        import zipfile
        with zipfile.ZipFile(str(path)) as z:
            xml = z.read("word/document.xml").decode("utf-8", "replace")
        xml = re.sub(r"(?is)<w:tab[^>]*/>", "\t", xml)
        xml = re.sub(r"(?is)</w:p>", "\n", xml)
        return html.unescape(re.sub(r"(?is)<[^>]+>", "", xml))
    if ext == ".rtf":
        t = path.read_text(encoding="utf-8", errors="replace")
        t = re.sub(r"\\par[d]?\b", "\n", t)
        t = re.sub(r"\\'[0-9a-fA-F]{2}", " ", t)
        t = re.sub(r"\\[a-zA-Z]+-?\d* ?", "", t)
        return t.replace("{", "").replace("}", "")
    return path.read_text(encoding="utf-8", errors="replace")


def _chunk_text(text: str, size: int = 1800) -> list:
    """Разбить длинный документ на части по абзацам/предложениям (для удобной навигации
    и устойчивого рендера) — каждая часть станет отдельной репликой в очереди."""
    import re
    text = re.sub(r"\n{3,}", "\n\n", (text or "").strip())
    if len(text) <= size:
        return [text] if text else []
    chunks, cur = [], ""
    for para in re.split(r"\n\s*\n", text):
        if len(para) > size:
            for sent in re.split(r"(?<=[.!?…])\s+", para):
                if cur and len(cur) + len(sent) > size:
                    chunks.append(cur.strip()); cur = ""
                cur += sent + " "
        else:
            if cur and len(cur) + len(para) > size:
                chunks.append(cur.strip()); cur = ""
            cur += para + "\n\n"
    if cur.strip():
        chunks.append(cur.strip())
    return chunks


def _derive_word_marks(body: str, sent_marks: list) -> list:
    """Приблизительные тайминги слов: длительность предложения делим между словами
    пропорционально их длине. edge-tts не отдаёт WordBoundary для наших голосов, поэтому
    «караоке»-подсветку по словам строим из границ предложений — работает для всех голосов."""
    import re
    out = []
    for (start, end, a, b) in sent_marks:
        seg = body[a:b]
        toks = [(m.start(), m.group()) for m in re.finditer(r"\S+", seg)]
        if not toks:
            continue
        total = sum(len(t) for _, t in toks) or 1
        dur = max(0.0, end - start)
        acc = 0
        for off, tok in toks:
            ws = start + dur * (acc / total)
            acc += len(tok)
            we = start + dur * (acc / total)
            out.append((ws, we, a + off, a + off + len(tok)))
    return out


def _map_marks_to_chars(text: str, sents: list) -> list:
    """Сопоставить предложения (из таймингов) с диапазонами символов в text."""
    out = []
    cursor = 0
    for start, end, stext in sents:
        stext = (stext or "").strip()
        if not stext:
            continue
        idx = text.find(stext, cursor)
        if idx < 0:
            idx = text.find(stext)        # вдруг порядок сбился
        if idx < 0:
            continue
        out.append((start, end, idx, idx + len(stext)))
        cursor = idx + len(stext)
    return out


def _split_for_tts(body: str) -> list:
    """Разрезать текст на части [(начало, конец)]: по строкам (строка таблицы остаётся целой),
    иначе по предложениям. Первая часть короткая — чтобы быстрее зазвучать."""
    out, a, n = [], 0, len(body)
    limit = TTS_FIRST_PART_CHARS
    while n - a > limit:
        win = body[a:a + limit]
        cut = limit
        for sep in ("\n", ". ", "! ", "? ", "… ", "; ", ", ", " "):
            p = win.rfind(sep)
            if p >= limit * 0.4:
                cut = p + len(sep)
                break
        out.append((a, a + cut))
        a += cut
        limit = TTS_PART_CHARS
    if a < n:
        out.append((a, n))
    return out


def _audio_duration(path: "Path", fallback: float) -> float:
    try:
        return float(_miniaudio.get_file_info(str(path)).duration)
    except Exception:
        return fallback


def _has_local_fallback_voice(voice: str) -> bool:
    if vp_piper.is_local(voice) or not vp_piper.is_local(TTS_VOICE):
        return False
    model = vp_piper.VOICES_DIR / (TTS_VOICE.removeprefix(vp_piper.VOICE_PREFIX) + ".onnx")
    return model.is_file() and model.with_suffix(".onnx.json").is_file()


def generate_supporter_key(seed: str = "") -> str:
    """Сгенерировать офлайн-ключ Supporter формата VP-PRO-XXXX-XXXX-CCCC."""
    import hashlib
    raw = (seed.strip() or str(uuid4())).encode("utf-8")
    body_hex = hashlib.sha256(b"vp-seed:" + raw).hexdigest().upper()[:8]
    p1, p2 = body_hex[:4], body_hex[4:8]
    sig = hashlib.sha256(f"VoicePlayer-Supporter-v1:{p1}-{p2}".encode("ascii")).hexdigest().upper()[:4]
    return f"VP-PRO-{p1}-{p2}-{sig}"


def verify_supporter_key(key: str) -> bool:
    """Проверить офлайн-ключ Supporter формата VP-PRO-XXXX-XXXX-CCCC."""
    import hashlib, re
    k = (key or "").strip().upper()
    m = re.fullmatch(r"VP-PRO-([0-9A-F]{4})-([0-9A-F]{4})-([0-9A-F]{4})", k)
    if not m:
        return False
    p1, p2, sig = m.groups()
    expected = hashlib.sha256(f"VoicePlayer-Supporter-v1:{p1}-{p2}".encode("ascii")).hexdigest().upper()[:4]
    return sig == expected


def _synthesize_with_retries(text: str, rate: str, mp3_file: "Path", voice: str, item_id: str) -> tuple:
    """Одна часть: (sents, words), либо RuntimeError после всех попыток."""
    last_err = "render failed"
    max_retries = 0 if _has_local_fallback_voice(voice) else TTS_RETRIES
    for attempt in range(1, max_retries + 2):
        try:
            sents, words = _synthesize(text, rate, mp3_file, voice)
            if mp3_file.exists() and mp3_file.stat().st_size > 0:
                if attempt > 1:
                    log_line(f"render OK [{item_id}] attempt={attempt} text='{text[:60]}'")
                return sents, words
            last_err = "озвучка вернула пустой файл"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"[:300]
        log_line(f"render FAIL [{item_id}] {mp3_file.stem} attempt={attempt}/{max_retries+1}: {last_err}")
        if attempt <= max_retries:
            time.sleep(TTS_RETRY_DELAY_S)
    try:
        if mp3_file.exists() and mp3_file.stat().st_size == 0:
            mp3_file.unlink()
    except OSError:
        pass
    raise RuntimeError(last_err)


def render_audio_blocking(item: Item, rate: str = TTS_RATE, voice: str = TTS_VOICE,
                          lead: str = "", on_part=None) -> None:
    """Озвучить реплику. Длинную — частями: каждая часть короткая и не упирается в таймаут,
    on_part вызывается по порядку, как только готова очередная часть (можно играть, не дожидаясь
    всего). Упала часть — готовые остаются, повтор продолжит с места сбоя."""
    from concurrent.futures import ThreadPoolExecutor
    body = _clean_for_tts(item.text)
    if not body:
        item.audio_error = "пустой текст"
        log_line(f"render skip [{item.id}]: empty text")
        return

    # Автоопределение языка текста: если включен автодетект и выбран голос по умолчанию,
    # подбираем нативный голос под язык текста (en, zh, es, ja, hi, de, fr, ru)
    if (not item.parts_done) and (voice == TTS_VOICE or not voice):
        detected = vp_i18n.detect_text_language(body)
        if detected in vp_i18n.DEFAULT_VOICES_BY_LANG:
            voice = vp_i18n.DEFAULT_VOICES_BY_LANG[detected]

    spans = _split_for_tts(body)
    mp3_file = AUDIO_DIR / f"{item.id}.mp3"
    if not item.parts_done:
        item.marks = []; item.word_marks = []
        item.rendered_voice = voice
    else:
        voice = item.rendered_voice or voice   # голоса пишут звук в разном формате, склеить их нельзя
    done = len(item.parts_done)
    t_offset = sum(_audio_duration(p, 0.0) for p in item.parts_done)
    item.more_parts = len(spans) > 1

    def part_file(i: int) -> "Path":
        return mp3_file if len(spans) == 1 else AUDIO_DIR / f"{item.id}.p{i:03d}.mp3"

    def part_text(i: int) -> str:
        a, b = spans[i]
        chunk = body[a:b].strip()
        # lead (имя автора голосом) звучит перед телом; тайминги маппим по телу, лид в них не попадёт
        return (lead.rstrip(". ") + ".\n" + chunk) if (lead and i == 0) else chunk

    with ThreadPoolExecutor(max_workers=TTS_PARALLEL_PARTS, thread_name_prefix="tts-part") as pool:
        futures = {i: pool.submit(_synthesize_with_retries, part_text(i), rate, part_file(i), voice, item.id)
                   for i in range(done, len(spans))}
        try:
            for i in range(done, len(spans)):
                sents, words = futures[i].result()
                a, b = spans[i]
                shift = lambda marks: [(s + t_offset, e + t_offset, ca + a, cb + a) for s, e, ca, cb in marks]
                chunk = body[a:b]
                item.marks = item.marks + shift(_map_marks_to_chars(chunk, sents))
                new_words = (_map_marks_to_chars(chunk, words) if words
                             else _derive_word_marks(chunk, _map_marks_to_chars(chunk, sents)))
                item.word_marks = item.word_marks + shift(new_words)
                t_offset += _audio_duration(part_file(i), sents[-1][1] if sents else 0.0)
                item.parts_done = item.parts_done + [part_file(i)]
                item.audio_error = None
                item.rendered_rate = rate
                if i == 0:
                    item.audio_path = part_file(0)
                if on_part is not None:
                    item.audio_ready = True          # уже можно играть — остальное догрузится
                    on_part(item)
        except Exception as e:
            for f in futures.values():
                f.cancel()
            item.audio_error = str(e)[:300] or "render failed"
            if on_part is None or not item.parts_done:
                item.audio_ready = False
    if item.audio_error:
        # Wait for the outstanding Edge workers before reusing their output paths.
        # Keep already published parts on their original voice.
        fallback_voice = TTS_VOICE
        if not item.parts_done and not vp_piper.is_local(voice) and vp_piper.is_local(fallback_voice):
            model = vp_piper.VOICES_DIR / (fallback_voice.removeprefix(vp_piper.VOICE_PREFIX) + ".onnx")
            if model.is_file() and model.with_suffix(".onnx.json").is_file():
                log_line(f"render fallback [{item.id}]: {voice} -> {fallback_voice}")
                render_audio_blocking(item, rate, fallback_voice, lead, on_part)
        return
    if len(spans) > 1:                               # склеить в один файл: перемотка, сохранение, запуск заново
        try:
            tmp = mp3_file.with_suffix(".tmp")
            with open(tmp, "wb") as out:
                for p in item.parts_done:
                    out.write(Path(p).read_bytes())
            tmp.replace(mp3_file)
            item.audio_path = mp3_file
        except OSError as e:
            log_line(f"render concat [{item.id}]: {e}")
    else:
        item.audio_path = mp3_file
    item.audio_ready = True
    item.more_parts = False


# ============================ ТРАНСКРИБАЦИЯ (faster-whisper) ============================

try:
    from faster_whisper import WhisperModel as _WhisperModel
    _WHISPER_OK = True
except Exception as _e:
    _WHISPER_OK = False
    _WHISPER_IMPORT_ERR = str(_e)

WHISPER_MODEL_SIZE = "base"   # tiny/base/small/medium — баланс скорость/качество
_whisper_holder = {"model": None, "size": None, "lock": threading.Lock()}


def _get_whisper(size: str):
    with _whisper_holder["lock"]:
        if _whisper_holder["model"] is None or _whisper_holder["size"] != size:
            _whisper_holder["model"] = _WhisperModel(size, device="cpu", compute_type="int8")
            _whisper_holder["size"] = size
        return _whisper_holder["model"]


def transcribe_file(path, model_size: str = WHISPER_MODEL_SIZE,
                    language: Optional[str] = None, translate: bool = False,
                    word_level: bool = False):
    """Распознать речь из аудиофайла любого формата (ogg/mp3/m4a/wav/flac).
    translate=True — переводит в английский. word_level=True — тайминги по словам.
    Возвращает (text, marks). Блокирующая."""
    import numpy as _np
    import miniaudio as _ma
    dec = _ma.decode_file(str(path), output_format=_ma.SampleFormat.FLOAT32,
                          nchannels=1, sample_rate=16000)
    audio = _np.array(dec.samples, dtype=_np.float32)
    model = _get_whisper(model_size)
    segments, info = model.transcribe(audio, language=(None if translate else language),
                                      task=("translate" if translate else "transcribe"),
                                      beam_size=1, vad_filter=True,
                                      word_timestamps=word_level)
    marks, text = [], ""
    for seg in segments:
        if word_level and getattr(seg, "words", None):
            for wd in seg.words:
                s = (wd.word or "").strip()
                if not s:
                    continue
                text += (s if not text else " " + s)
                char0 = len(text) - len(s)
                marks.append((float(wd.start), float(wd.end), char0, char0 + len(s)))
        else:
            s = seg.text.strip()
            if not s:
                continue
            text += (s if not text else " " + s)
            char0 = len(text) - len(s)
            marks.append((float(seg.start), float(seg.end), char0, char0 + len(s)))
    return text.strip(), marks


# ============================ ЗАПИСЬ С МИКРОФОНА ============================

class MicRecorder:
    def __init__(self, samplerate=16000, channels=1):
        self.sr = samplerate; self.ch = channels
        self._frames = []; self._stream = None; self._rec = False

    def start(self, device=None):
        import sounddevice as sd
        self._frames = []; self._rec = True

        def cb(indata, frames, t, status):
            if self._rec:
                self._frames.append(indata.copy())
        self._stream = sd.InputStream(samplerate=self.sr, channels=self.ch,
                                      dtype="float32", callback=cb, device=device)
        self._stream.start()

    def stop(self, path) -> bool:
        import numpy as np, wave
        self._rec = False
        if self._stream is not None:
            try: self._stream.stop(); self._stream.close()
            except Exception: pass
            self._stream = None
        if not self._frames:
            return False
        data = np.concatenate(self._frames, axis=0)
        pcm = (np.clip(data, -1, 1) * 32767).astype("<i2")
        with wave.open(str(path), "wb") as w:
            w.setnchannels(self.ch); w.setsampwidth(2); w.setframerate(self.sr)
            w.writeframes(pcm.tobytes())
        return True


def _write_wav(frames, path, sr=16000):
    import numpy as np, wave
    if not frames:
        return False
    data = np.concatenate(frames, axis=0)
    if data.ndim > 1:
        data = data.mean(axis=1)
    pcm = (np.clip(data, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr); w.writeframes(pcm.tobytes())
    return True


def find_loopback_device():
    """Индекс входного устройства «Стерео микшер»/loopback (для захвата системного звука)."""
    try:
        import sounddevice as sd
        for i, d in enumerate(sd.query_devices()):
            if d.get("max_input_channels", 0) > 0:
                n = d["name"].lower()
                if any(k in n for k in ("loopback", "stereo mix", "стерео микшер", "what u hear")):
                    return i
    except Exception:
        pass
    return None


class SystemRecorder:
    """Запись системного звука. Приоритет — «Стерео микшер» через sounddevice (надёжно,
    без COM); если его нет — soundcard loopback динамика."""
    def __init__(self, samplerate=16000):
        self.sr = samplerate; self._frames = []; self._rec = False
        self._stream = None; self._thread = None; self._mode = None

    def start(self):
        self._frames = []; self._rec = True
        self._ready = threading.Event()
        try:
            import soundcard  # noqa
            self._mode = "sc"
        except Exception:
            self._mode = "sd"
        if self._mode == "sc":
            def run():
                try:
                    if os.name == "nt":
                        try: ctypes.windll.ole32.CoInitializeEx(None, 0)   # MTA
                        except Exception: pass
                    import soundcard as sc
                    spk = sc.default_speaker()
                    mic = sc.get_microphone(spk.id, include_loopback=True)
                    with mic.recorder(samplerate=self.sr, channels=1) as r:
                        self._ready.set()                # рекордер открыт и пишет
                        while self._rec:
                            self._frames.append(r.record(numframes=self.sr // 4))
                except Exception as e:
                    log_line(f"system record error: {e}")
                    self._ready.set()
            self._thread = threading.Thread(target=run, daemon=True, name="sysrec")
            self._thread.start()
            self._ready.wait(timeout=3)               # дождаться готовности устройства
        else:
            dev = find_loopback_device()
            import sounddevice as sd

            def cb(indata, frames, t, status):
                if self._rec:
                    self._frames.append(indata.copy())
            self._stream = sd.InputStream(channels=2, dtype="float32", callback=cb, device=dev)
            self.sr = int(self._stream.samplerate)
            self._stream.start()

    def stop(self, path) -> bool:
        self._rec = False
        if self._stream is not None:
            try: self._stream.stop(); self._stream.close()
            except Exception: pass
            self._stream = None
        if self._thread is not None:
            self._thread.join(timeout=2); self._thread = None
        return _write_wav(self._frames, path, self.sr)


# ============================ AI-САММАРИ + СУЩНОСТИ ============================

def _extractive_summary(text: str, n: int = 3) -> str:
    import re
    sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if len(s.strip()) > 20]
    if len(sents) <= n:
        return text.strip()
    words = [w for w in re.findall(r"\w+", text.lower()) if len(w) > 3]
    freq = {}
    for w in words:
        freq[w] = freq.get(w, 0) + 1
    scored = sorted(sents, key=lambda s: sum(freq.get(w, 0) for w in re.findall(r"\w+", s.lower())),
                    reverse=True)[:n]
    return " ".join(s for s in sents if s in scored)


def ai_summary(text: str) -> str:
    """Краткий пересказ: реальный Claude CLI (без ключа), иначе локальный экстрактивный."""
    import shutil, subprocess
    claude = shutil.which("claude")
    if claude and len(text) > 120:
        prompt = ("Перескажи кратко и по-русски, 3–5 предложений, только суть, без вступлений:\n\n"
                  + text[:8000])
        try:
            r = subprocess.run([claude, "-p", prompt], capture_output=True, text=True,
                               timeout=120, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            out = (r.stdout or "").strip()
            if out:
                return out
        except Exception as e:
            log_line(f"claude summary failed: {e}")
    return _extractive_summary(text, 3)


def extract_entities(text: str):
    """Найти задачи/даты/время/числа/имена для подсветки. Возвращает list[(start,end)]."""
    import re
    spans = []
    pats = [
        r"\b\d{1,2}[:.]\d{2}\b",                       # время
        r"\b\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?\b",     # дата
        r"\b\d[\d\s.,]*\b",                            # числа
        r"\b(?:нужно|надо|сделать|дедлайн|срочно|задача|до завтра|сегодня|завтра)\b",
        r"\b[А-ЯЁ][а-яё]{2,}\b",                       # слова с заглавной (имена)
    ]
    for p in pats:
        for m in re.finditer(p, text, flags=re.IGNORECASE):
            spans.append((m.start(), m.end()))
    return spans


def tag_for(text: str) -> str:
    """Авто-тег реплики по содержимому: 🔴 срочное, ✅ задача, ❓ вопрос."""
    import re
    t = (text or "").lower()
    if re.search(r"\b(срочно|asap|немедленно|критично|блокер)\b", t):
        return "🔴"
    if re.search(r"\b(нужно|надо|сделать|задача|дедлайн|подготовь|пришли|закрой)\b", t):
        return "✅"
    if "?" in text:
        return "❓"
    return ""


def ai_reply(text: str) -> str:
    """Черновик делового ответа на сообщение через Claude CLI (без ключа)."""
    import shutil, subprocess
    claude = shutil.which("claude")
    if not claude:
        return ""
    prompt = ("Напиши короткий деловой ответ по-русски на это сообщение, "
              "только текст ответа без вступлений:\n\n" + text[:6000])
    try:
        r = subprocess.run([claude, "-p", prompt], capture_output=True, text=True,
                           timeout=120, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        return (r.stdout or "").strip()
    except Exception as e:
        log_line(f"ai_reply failed: {e}")
        return ""


def post_to_board(text: str, author: str = "Антон") -> bool:
    """Опубликовать сообщение на доску команды (.swarm team-chat) от имени author."""
    try:
        now = int(time.time() * 1000)
        content = json.dumps({"from": author, "message": text,
                              "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S")}, ensure_ascii=False)
        eid = f"entry_{now}_{uuid4().hex[:10]}"
        key = f"{author.lower()}_voice_{now}"
        conn = sqlite3.connect(str(DB_PATH), timeout=3.0)
        conn.execute("INSERT INTO memory_entries (id,key,namespace,content,type,created_at,updated_at,status) "
                     "VALUES (?,?,?,?,?,?,?,?)",
                     (eid, key, "team-chat", content, "semantic", now, now, "active"))
        conn.commit(); conn.close()
        return True
    except Exception as e:
        log_line(f"post_to_board error: {e}")
        return False


def post_to_telegram(text: str, chat_id: str) -> tuple:
    """Отправить текст в Telegram через бота (токен из окружения). (ok, error)."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        return (False, "нет TELEGRAM_BOT_TOKEN в окружении")
    if not chat_id:
        return (False, "не задан chat_id Telegram (укажи в настройках)")
    try:
        import urllib.request, urllib.parse
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        resp = json.loads(urllib.request.urlopen(req, timeout=10).read().decode("utf-8"))
        return (bool(resp.get("ok")), resp.get("description", ""))
    except Exception as e:
        return (False, str(e))


# ============================ АРХИВ (SQLite) ============================

_archive_lock = threading.Lock()


def archive_init():
    try:
        conn = sqlite3.connect(str(ARCHIVE_DB))
        conn.execute("CREATE TABLE IF NOT EXISTS items "
                     "(id TEXT PRIMARY KEY, ts REAL, source TEXT, author TEXT, "
                     "time_str TEXT, date_str TEXT, text TEXT)")
        conn.commit(); conn.close()
    except Exception as e:
        log_line(f"archive init error: {e}")


def archive_add(item):
    try:
        with _archive_lock:
            conn = sqlite3.connect(str(ARCHIVE_DB), timeout=2.0)
            conn.execute("INSERT OR IGNORE INTO items VALUES (?,?,?,?,?,?,?)",
                         (item.id, time.time(), item.source, item.author,
                          item.time_str, item.date_str, item.text))
            conn.commit(); conn.close()
    except Exception:
        pass


def archive_search(q: str, limit: int = 200):
    try:
        with _archive_lock:
            conn = sqlite3.connect(f"file:{ARCHIVE_DB}?mode=ro", uri=True, timeout=2.0)
            if q.strip():
                rows = conn.execute(
                    "SELECT author, date_str, time_str, text, source FROM items "
                    "WHERE text LIKE ? OR author LIKE ? ORDER BY ts DESC LIMIT ?",
                    (f"%{q}%", f"%{q}%", limit)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT author, date_str, time_str, text, source FROM items "
                    "ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
            conn.close()
            return rows
    except Exception as e:
        log_line(f"archive search error: {e}")
        return []


# ---- семантический поиск по архиву (fastembed) ----
_embed_holder = {"model": None, "lock": threading.Lock()}


def _get_embedder():
    with _embed_holder["lock"]:
        if _embed_holder["model"] is None:
            from fastembed import TextEmbedding
            _embed_holder["model"] = TextEmbedding(
                model_name="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
        return _embed_holder["model"]


def archive_semantic_search(q: str, limit: int = 40):
    import numpy as np
    with _archive_lock:
        conn = sqlite3.connect(str(ARCHIVE_DB), timeout=5.0)
        conn.execute("CREATE TABLE IF NOT EXISTS emb (id TEXT PRIMARY KEY, vec BLOB)")
        rows = conn.execute("SELECT i.id,i.author,i.date_str,i.time_str,i.text,i.source,e.vec "
                            "FROM items i LEFT JOIN emb e ON e.id=i.id "
                            "ORDER BY i.ts DESC LIMIT 3000").fetchall()
        conn.commit(); conn.close()
    rows = [r for r in rows if (r[4] or "").strip()]
    if not rows:
        return []
    model = _get_embedder()
    missing = [(r[0], r[4]) for r in rows if r[6] is None]
    new_vecs = {}
    if missing:
        embs = list(model.embed([t for _, t in missing]))
        for (iid, _), v in zip(missing, embs):
            new_vecs[iid] = np.asarray(v, dtype=np.float32)
        with _archive_lock:
            conn = sqlite3.connect(str(ARCHIVE_DB), timeout=5.0)
            conn.executemany("INSERT OR REPLACE INTO emb VALUES (?,?)",
                             [(iid, new_vecs[iid].tobytes()) for iid in new_vecs])
            conn.commit(); conn.close()
    qv = np.asarray(list(model.embed([q]))[0], dtype=np.float32)
    qn = qv / (np.linalg.norm(qv) + 1e-9)
    scored = []
    for r in rows:
        vec = new_vecs.get(r[0])
        if vec is None and r[6] is not None:
            vec = np.frombuffer(r[6], dtype=np.float32)
        if vec is None:
            continue
        sim = float(np.dot(qn, vec / (np.linalg.norm(vec) + 1e-9)))
        scored.append((sim, r))
    scored.sort(key=lambda x: -x[0])
    return [(r[1], r[2], r[3], r[4], r[5]) for _, r in scored[:limit]]


# ---- плейлисты (именованные очереди) ----
PLAYLISTS_FILE = Path.home() / ".voice-player-playlists.json"


def playlists_load() -> dict:
    try:
        return json.loads(PLAYLISTS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def playlists_save(data: dict):
    try:
        PLAYLISTS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log_line(f"playlists save error: {e}")


# ============================ HTTP LISTENER ============================

class _HookHandler(BaseHTTPRequestHandler):
    app: "VoicePlayerApp" = None
    def log_message(self, fmt, *args): pass

    def _json(self, code: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path == "/api/enqueue":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length).decode("utf-8") if length else ""
                data = json.loads(raw) if raw else {}
            except Exception:
                self._json(400, {"error": "bad json"}); return
            text = str(data.get("text") or "").strip()
            if not text:
                self._json(204, {}); return
            item = self.app.enqueue(
                source=data.get("source") or "claude-code",
                author=data.get("author") or f"Claude/{data.get('session') or 'sess'}",
                time_str=data.get("time") or time.strftime("%H:%M"),
                date_str=data.get("date") or "",
                text=text,
                prompt=str(data.get("prompt") or ""),
                title=str(data.get("title") or ""),
            )
            self._json(200, {"id": item.id if item else None, "queued": item is not None}); return
        if self.path == "/api/transcribe":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                data = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            except Exception:
                self._json(400, {"error": "bad json"}); return
            src = str(data.get("path") or data.get("url") or "").strip()
            if not src:
                self._json(400, {"error": "no path/url"}); return
            self.app.root.after(0, lambda: self.app._api_transcribe(src))
            self._json(200, {"ok": True, "queued": True}); return
        if self.path == "/api/show":
            self.app.root.after(0, self.app._show_window)
            self._json(200, {"ok": True}); return
        if self.path == "/api/clear":
            self.app.clear_queue()
            self._json(200, {"ok": True}); return
        self._json(404, {"error": "not found"})

    def do_GET(self):
        if self.path == "/api/ping":
            self._json(200, {"ok": True, "queue": len(self.app.queue_items)}); return
        if self.path == "/api/queue":
            items = [{"author": it.author, "source": it.source, "text": it.text,
                      "prompt": it.prompt, "title": it.title,
                      "played": it.played, "transcribing": it.transcribing}
                     for it in self.app.queue_items if not it.deleted]
            self._json(200, {"count": len(items), "items": items}); return
        self._json(404, {"error": "not found"})


def start_http_listener(app: "VoicePlayerApp") -> ThreadingHTTPServer:
    global HTTP_PORT
    _HookHandler.app = app
    last_err = None
    for cand in [HTTP_PORT, 3738, 3739, 3740, 3741]:
        try:
            srv = ThreadingHTTPServer((HTTP_HOST, cand), _HookHandler)
            HTTP_PORT = cand
            try:
                pfile = Path.home() / ".voice-player" / "port.txt"
                pfile.parent.mkdir(parents=True, exist_ok=True)
                pfile.write_text(str(cand), encoding="utf-8")
            except Exception:
                pass
            threading.Thread(target=srv.serve_forever, daemon=True, name="hook-listener").start()
            return srv
        except OSError as e:
            last_err = e
    raise last_err or RuntimeError("cannot bind HTTP listener")


# ============================ BOARD POLLER ============================

# самоназвания агентов на доске (как они подписываются)
KNOWN_NAMES = ["Платон", "Кузьма", "Денис", "Семён", "Семен",
               "Марк", "Прохор", "Савва", "Антон"]


def _grab_name_token(s: str) -> Optional[str]:
    """Самое левое известное имя в строке (= отправитель), с полным токеном (суффикс -2 и т.п.)."""
    best_idx, best_nm = None, None
    for nm in KNOWN_NAMES:
        idx = s.find(nm)
        if idx >= 0 and (best_idx is None or idx < best_idx):
            best_idx, best_nm = idx, nm
    if best_idx is None:
        return None
    tok = s[best_idx:].split()[0]
    return tok.strip(" :,.()—–-") or best_nm


def parse_message(raw: str) -> tuple[str, str, str]:
    import re
    s = raw.strip()
    # 0) JSON-сообщение {"from":..., "to":..., "message"/"text"/"body":...}
    if s.startswith("{"):
        try:
            d = json.loads(s)
        except Exception:
            d = None
        if isinstance(d, dict) and (d.get("from") or d.get("author")):
            author = str(d.get("from") or d.get("author") or "доска").strip()
            body = (d.get("message") or d.get("text") or d.get("body")
                    or d.get("content") or d.get("subject") or "")
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False)
            body = str(body).strip()
            tm = ""
            mt = re.search(r"(\d{1,2}:\d{2})", str(d.get("timestamp") or d.get("time") or ""))
            if mt: tm = mt.group(1)
            return (author, tm, body or s)
    # 1) [Имя / время] тело
    m = re.match(r"^\[([^/\]]+?)\s*/\s*([^\]]+?)\]\s*(.*)$", s, re.DOTALL)
    if m:
        return (m.group(1).strip(), m.group(2).strip(), m.group(3).strip() or raw)
    # 2) [Имя → ... ] / [Имя ...] без слэша — отправитель = первый именной токен
    m = re.match(r"^\[([^\]]+)\]\s*(.*)$", s, re.DOTALL)
    if m:
        inside, body = m.group(1).strip(), m.group(2).strip()
        name = _grab_name_token(inside) or re.split(r"[→/+(]", inside)[0].strip() or "доска"
        return (name, "", body or raw)
    # 3) без скобок — известное имя в начале (после эмодзи/маркера)
    name = _grab_name_token(s[:48])
    if name:
        return (name, "", s)
    return ("доска", "", raw)


def board_poll_loop(app: "VoicePlayerApp"):
    last_seen_ts: int = int(time.time() * 1000)
    while not app.stop_event.is_set():
        try:
            if DB_PATH.exists():
                conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=2.0)
                try:
                    rows = conn.execute(
                        "SELECT id, content, created_at FROM memory_entries "
                        "WHERE namespace='team-chat' AND status='active' AND created_at > ? "
                        "ORDER BY created_at ASC LIMIT 100",
                        (last_seen_ts,),
                    ).fetchall()
                finally:
                    conn.close()
                for _id, content, created_at in rows:
                    last_seen_ts = max(last_seen_ts, int(created_at))
                    text = str(content or "")
                    author, time_str, body = parse_message(text)
                    app.enqueue(
                        source="board", author=author,
                        time_str=time_str or time.strftime("%H:%M", time.localtime(created_at / 1000)),
                        date_str=time.strftime("%d.%m.%Y", time.localtime(created_at / 1000)),
                        text=body,
                    )
        except Exception as e:
            log_line(f"board poll error: {e}")
        app.stop_event.wait(POLL_BOARD_INTERVAL_S)


# ============================ ПОДСКАЗКИ (tooltips) ============================

class Tooltip:
    """Всплывающая подсказка при наведении на виджет."""
    def __init__(self, widget, text, delay=450):
        self.widget = widget; self.text = text; self.delay = delay
        self._after = None; self._tip = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _e=None):
        self._cancel()
        try: self._after = self.widget.after(self.delay, self._show)
        except Exception: pass

    def _cancel(self):
        if self._after:
            try: self.widget.after_cancel(self._after)
            except Exception: pass
            self._after = None

    def _show(self):
        if self._tip or not self.text:
            return
        try:
            x = self.widget.winfo_rootx() + 14
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        except Exception:
            return
        self._tip = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        try: tw.attributes("-topmost", True)
        except Exception: pass
        tw.wm_geometry(f"+{x}+{y}")
        translated_text = vp_i18n.translate_tip(self.text)
        tk.Label(tw, text=translated_text, bg="#1d1d1f", fg="#ffffff",
                 font=("Segoe UI", 9), padx=8, pady=4, justify="left").pack()

    def _hide(self, _e=None):
        self._cancel()
        if self._tip:
            try: self._tip.destroy()
            except Exception: pass
            self._tip = None


# ============================ ОСНОВНОЕ ОКНО ============================

class VoicePlayerApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.stop_event = threading.Event()
        self.queue_items: list[Item] = []
        self.current_idx: int = -1
        self.user_paused: bool = False
        self.search_query: str = ""
        self.sort_col: Optional[str] = None
        self.sort_reverse: bool = False
        self._rate_after_id = None
        self._play_fail_streak = 0
        self._seeking = False
        self._seek_dur = 0.0
        self._hl_range = None        # текущий подсвеченный диапазон в панели текста
        self._hl_word = None         # текущий подсвеченный слово-диапазон
        self.mini = None
        self._mini_dx = 0
        self._mini_dy = 0

        self.prefs = self._load_prefs()
        self.mini_geometry: str = self.prefs.get("mini_geometry", "")
        self.var_filter_author = tk.StringVar(value="все")
        self.var_filter_date   = tk.StringVar(value="все")
        self.var_rate     = tk.DoubleVar(value=self.prefs.get("rate", 1.0))
        self.var_volume   = tk.DoubleVar(value=self.prefs.get("volume", 1.0))
        src_pref = self.prefs.get("source", "all")
        self.var_source   = tk.StringVar(value="ai" if src_pref == "claude-code" else src_pref)
        self._src_labels = {
            "all":         "Все источники",
            "ai":          "🤖 Только нейросети",
            "board":       "👥 Только доска команды",
            "favorite":    "⭐ Только избранное",
        }
        self._src_rev = {v: k for k, v in self._src_labels.items()}
        self.var_source_label = tk.StringVar(
            value=self._src_labels.get(self.var_source.get(), "Все источники"))
        self.var_autoplay = tk.BooleanVar(value=self.prefs.get("autoplay", True))
        # «не беспокоить» + тихие часы: новые реплики копятся и рендерятся, но не озвучиваются сами
        self.var_dnd      = tk.BooleanVar(value=self.prefs.get("dnd", False))
        self.var_quiet    = tk.BooleanVar(value=self.prefs.get("quiet_hours", False))
        self._quiet_from  = self.prefs.get("quiet_from", "22:00")
        self._quiet_to    = self.prefs.get("quiet_to", "08:00")
        # таймер сна: дедлайн для подписи «осталось …» (хэндл живёт в self._sleep_after)
        self._sleep_deadline = 0.0
        # своя скорость для каждого источника (бот/команда)
        self.var_rate_per_source = tk.BooleanVar(value=self.prefs.get("rate_per_source", False))
        self._rate_by_source = dict(self.prefs.get("rate_by_source", {}))
        self.var_speak_author = tk.BooleanVar(value=self.prefs.get("speak_author", True))
        self._speak_author_on = bool(self.prefs.get("speak_author", True))   # копия для фон. потока
        self.var_show_text = tk.BooleanVar(value=self.prefs.get("show_text_panel", True))
        self.var_show_queue = tk.BooleanVar(value=self.prefs.get("show_queue", True))
        self.var_autoscroll = tk.BooleanVar(value=self.prefs.get("autoscroll", True))
        self.var_newest_first = tk.BooleanVar(value=self.prefs.get("newest_first", True))
        self.var_model     = tk.StringVar(value=self.prefs.get("whisper_model", WHISPER_MODEL_SIZE))
        self.var_translate = tk.BooleanVar(value=self.prefs.get("translate_en", False))
        # копии для фоновых потоков: читать tk-переменные не из главного потока нельзя
        # (до mainloop это падает с «main thread is not in main loop»)
        self._model_size  = self.var_model.get()
        self._translate_en = bool(self.var_translate.get())
        self.var_model.trace_add("write", lambda *_: setattr(self, "_model_size", self.var_model.get()))
        self.var_translate.trace_add("write", lambda *_: setattr(self, "_translate_en", bool(self.var_translate.get())))
        self._voice        = self.prefs.get("voice", TTS_VOICE)
        if _PIPER_OK and not self.prefs.get("voice_local_v1"):   # один раз: локальный голос вместо сетевого
            self.prefs["voice_local_v1"] = True
            self._voice = TTS_VOICE
        if self._voice not in set(VOICES.values()):   # сохранён удалённый голос → дефолт (без ошибки рендера)
            self._voice = TTS_VOICE
        _vlabel = next((k for k, v in VOICES.items() if v == self._voice), list(VOICES)[0])
        self.var_voice     = tk.StringVar(value=_vlabel)
        self._muted        = set(self.prefs.get("muted", []))   # авторы, которых не озвучиваем
        self.var_mode      = tk.StringVar(value=self.prefs.get("mode", "Очередь"))
        self.var_autoclip  = tk.BooleanVar(value=False)         # авто-озвучка буфера (не персистим)
        self._last_clip    = ""
        self.var_search    = tk.StringVar(value="")
        self.var_search.trace_add("write", lambda *_: self._on_search_change())
        self._render_after = None           # троттлинг перерисовки списка
        self._sleep_after = None            # таймер сна
        self._edit_item = None              # элемент, чей текст редактируем в панели
        self._resume = self.prefs.get("resume")   # возобновление с места (одноразово)
        self._load_pronounce()                     # словарь произношения → _PRONOUNCE
        archive_init()
        self._recorder = MicRecorder()
        self._recording = False
        self._recording_to_field = False     # диктовка ответа в поле, а не в очередь
        self._sysrec = SystemRecorder()
        self._recording_sys = False
        self._author_voices = {a: v for a, v in dict(self.prefs.get("author_voices", {})).items()
                               if v in set(VOICES.values())}   # автор → id голоса (отсев удалённых)
        self._author_names = dict(self.prefs.get("author_names", {}))      # автор → отображаемое имя (псевдоним)
        self.var_tg_chat = tk.StringVar(value=self.prefs.get("tg_chat_id", ""))
        self._voicecmd = False               # режим голосовых команд
        self._vc_stream = None
        self._vc_frames = []
        self._watch = set(self.prefs.get("watch", []))   # авторы под наблюдением
        self._live = False
        self._live_frames = []
        self._live_stream = None
        self._live_item = None
        self._live_lock = threading.Lock()

        cleanup_audio_cache(7)              # %TEMP%: удалить mp3 старше недели

        # приоритетная очередь рендера: (prio, seq, item). Меньший prio — раньше.
        # Это даёт «прогрев наперёд»: реплику, которую вот-вот слушать, рендерим без очереди.
        self.render_queue: "queue.PriorityQueue" = queue.PriorityQueue()
        self._render_seq = 0
        self._render_seq_lock = threading.Lock()
        self._parts_lock = threading.Lock()     # порядок частей при дозагрузке в плеер
        self._loaded_item = None                # какая реплика сейчас загружена в плеер
        self._loaded_gen = 0
        self._net_offline = False               # для спокойного, не пугающего статуса при потере сети
        self._rendering_item_id: Optional[str] = None
        self._wave_phase: float = 0.0
        self._wave_anim_after = None
        self._row_wave_canvases: list[tk.Canvas] = []
        self._iid_by_item_id: dict[str, str] = {}
        self._spawn_render_workers(workers=1)   # 1 поток: без параллельных запросов к edge-tts,
                                                # иначе Microsoft троттлит одновременные соединения → таймауты
        self._trans_queue: queue.Queue = queue.Queue()
        threading.Thread(target=self._transcribe_worker_loop, daemon=True, name="whisper").start()
        if _WHISPER_OK:                     # предзагрузка модели в фоне — первая транскрибация без зависания
            threading.Thread(target=self._preload_whisper, daemon=True, name="whisper-preload").start()

        self._auto_update = bool(self.prefs.get("auto_update", True))
        self._update_ready = ""
        self._stats_seconds = float(self.prefs.get("stats_seconds", 0.0))
        self._stats_items = int(self.prefs.get("stats_items", 0))
        self._supporter_key = str(self.prefs.get("supporter_key", ""))
        self._last_support_prompt_ts = float(self.prefs.get("last_support_prompt_ts", 0.0))
        try:
            import vp_telemetry
            self.telemetry = vp_telemetry.TelemetryManager(
                self.prefs, APP_VERSION, is_supporter_fn=self._is_supporter
            )
            self.telemetry.record_session_start()
        except Exception as e:
            self.telemetry = None
            log_line(f"telemetry init: {e}")
        # Интернационализация
        saved_lang = self.prefs.get("lang") or vp_i18n.detect_system_lang()
        vp_i18n.set_lang(saved_lang)
        self.var_lang = tk.StringVar(value=saved_lang)
        self.var_filter_author.set(vp_i18n.t("filter_all"))
        self.var_filter_date.set(vp_i18n.t("filter_all"))
        saved_mode = self.prefs.get("mode", "Очередь")
        mode_idx = self._parse_mode_to_idx(saved_mode)
        mode_vals = self._get_mode_values()
        self.var_mode.set(mode_vals[mode_idx])
        threading.Thread(target=self._update_loop, daemon=True, name="update").start()
        if vp_piper.is_local(self._voice):  # голос в памяти заранее — первая реплика не ждёт загрузки
            threading.Thread(target=self._preload_voice, args=(self._voice,), daemon=True, name="voice-preload").start()

        self._build_ui()

        # Плеер: StreamPlayer (мгновенная скорость, тон сохраняется) если есть библиотеки,
        # иначе откат на MciPlayer (скорость только при пересоздании файла).
        if _STREAM_OK:
            self.player = StreamPlayer()
            self._stream_backend = True
            try:
                _dev = _sd.query_devices(kind="output")
                log_line(f"backend=StreamPlayer, output='{_dev['name']}'")
            except Exception as e:
                log_line(f"backend=StreamPlayer, output device unknown: {e}")
        elif IS_WIN and _winmm is not None:
            self.player = MciPlayer()
            self._stream_backend = False
            log_line(f"backend=MCI (StreamPlayer недоступен: {_STREAM_IMPORT_ERR})")
        else:
            self.player = AfPlayer()
            self._stream_backend = False
            log_line("backend=AfPlayer (нативный системный плеер macOS/Linux)")
        self.player.set_volume(self.var_volume.get())

        threading.Thread(target=board_poll_loop, args=(self,), daemon=True, name="board-poll").start()
        try:
            self.http_srv = start_http_listener(self)
            self._set_status(f"слушаю POST /api/enqueue на :{HTTP_PORT}", ok=True)
        except OSError as e:
            self.http_srv = None
            self._set_status(f"порт {HTTP_PORT} занят: {e}", ok=False)

        self._tray = None
        self._start_global_hotkey()   # Ctrl+Alt+S — озвучить выделенный текст в любом окне
        self._setup_dnd()             # перетаскивание аудиофайлов в окно
        self._setup_tray()            # иконка в системном трее
        if IS_WIN:
            self.root.after(1200, lambda: vp_updater.register_windows_uninstaller(APP_VERSION))
        self._restore_session()       # вся очередь прошлого сеанса (или легаси: закреплённые/избранные)
        self.root.after(15000, self._autosave_session)   # периодически пересохранять очередь
        self.root.after(2500, self._refresh_ai_badge)    # найти установленные нейросети
        self.root.after(3500, self._trigger_telemetry_ping)
        self.root.after(4500, self._trigger_sponsor_check)
        self.root.after(45000, self._maybe_show_support_reminder)

        self.watchers = vp_watchers.Watchers(Path.home() / ".voice-player" / "watchers.json", log=log_line)
        self._ensure_ai_hooks_connected()
        self._poll_codex_inbox()
        threading.Thread(target=self._watcher_poll_loop, daemon=True, name="ai-watchers").start()
        self.root.after(600, lambda: self._pull_recent_ai(limit_per_source=5, silent=True))
        self._tick()
        self.root.after(300, self._restore_sashes)   # вернуть размеры секций
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ------------------ закреплённые + избранные (сохраняются между запусками) ------------------
    def _load_saved_items(self):
        try:
            data = json.loads(SAVED_FILE.read_text(encoding="utf-8"))
        except Exception:
            return
        was_auto = self.var_autoplay.get()
        self.var_autoplay.set(False)
        try:
            for rec in (data.get("pinned", []) + data.get("favorites", [])):
                it = self.enqueue(source=rec.get("source") or "manual",
                                  author=rec.get("author") or "—",
                                  time_str=rec.get("time_str") or "",
                                  date_str=rec.get("date_str") or "",
                                  text=rec.get("text") or "")
                if it is not None:
                    it.pinned = bool(rec.get("pinned"))
                    it.favorite = bool(rec.get("favorite"))
        finally:
            self.var_autoplay.set(was_auto)
        self._render_list()

    def _save_saved_items(self):
        def rec(it):
            return {"source": it.source, "author": it.author, "time_str": it.time_str,
                    "date_str": it.date_str, "text": it.raw_text,
                    "pinned": it.pinned, "favorite": it.favorite}
        pinned = [rec(it) for it in self.queue_items if it.pinned and not it.deleted]
        favs   = [rec(it) for it in self.queue_items if it.favorite and not it.pinned and not it.deleted]
        try:
            SAVED_FILE.write_text(json.dumps({"pinned": pinned, "favorites": favs},
                                             ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            log_line(f"save pinned/fav error: {e}")

    # ------------------ ВСЯ очередь (восстанавливается при перезапуске) ------------------
    def _save_session(self):
        """Сохранить весь список реплик, чтобы при следующем открытии он был как был.
        mp3 хранятся отдельно по id (7 дней) — восстановленные реплики играют без перерендера."""
        def rec(it):
            return {"id": it.id, "source": it.source, "author": it.author,
                    "time_str": it.time_str, "date_str": it.date_str,
                    "text": it.text, "raw_text": it.raw_text, "rendered_rate": it.rendered_rate,
                    "prompt": getattr(it, "prompt", ""), "title": getattr(it, "title", ""),
                    "played": it.played, "pinned": it.pinned, "favorite": it.favorite,
                    "marks": it.marks, "word_marks": it.word_marks,
                    "bookmarks": it.bookmarks, "created_at": it.created_at,
                    "is_file": it.is_file, "file_path": str(it.file_path) if it.file_path else ""}
        try:
            live = [it for it in self.queue_items if not it.deleted]
            if len(live) > SESSION_MAX:           # помним последние N + все закреплённые/избранные
                recent = {id(it) for it in live[-SESSION_MAX:]}
                live = [it for it in live if id(it) in recent or it.pinned or it.favorite]
            cur_id = (self.queue_items[self.current_idx].id
                      if 0 <= self.current_idx < len(self.queue_items) else "")
            SESSION_FILE.write_text(json.dumps({"items": [rec(it) for it in live], "current": cur_id},
                                               ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            log_line(f"save session error: {e}")

    def _restore_session(self):
        """Восстановить очередь прошлого сеанса. Если файла сессии нет — легаси-режим
        (только закреплённые/избранные из старого формата)."""
        try:
            data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
            recs = data.get("items", [])
        except Exception:
            recs = []
        if not recs:
            self._load_saved_items()
            return
        cur_id = data.get("current", "")
        to_render = []
        for r in recs:
            try:
                it = Item(id=r.get("id") or str(uuid4()), source=r.get("source") or "board",
                          author=r.get("author") or "—", time_str=r.get("time_str") or "",
                          text=r.get("text") or "", raw_text=r.get("raw_text") or r.get("text") or "",
                          date_str=r.get("date_str") or "",
                          prompt=r.get("prompt") or "", title=r.get("title") or "")
                it.rendered_rate = r.get("rendered_rate") or ""
                it.played = bool(r.get("played")); it.pinned = bool(r.get("pinned"))
                it.favorite = bool(r.get("favorite")); it.marks = r.get("marks") or []
                it.word_marks = r.get("word_marks") or []
                it.bookmarks = r.get("bookmarks") or []
                it.created_at = r.get("created_at") or time.time()
                it.is_file = bool(r.get("is_file"))
                fp = r.get("file_path") or ""
                if it.is_file:
                    p = Path(fp) if fp else None
                    if not p or not p.exists():
                        continue                  # временный аудиофайл уже удалён — пропускаем
                    it.file_path = p; it.audio_path = p; it.audio_ready = True
                else:
                    mp3 = AUDIO_DIR / f"{it.id}.mp3"
                    # Записи версий 1.1.4–1.1.5 (своя скорость голоса) играли бы неверно: озвучить заново.
                    # Метка rendered_speed есть только в сессиях этих версий, поэтому сработает один раз.
                    if "rendered_speed" in r and (r["rendered_speed"] != 1.0 or it.created_at >= SPEECH_REVERT_AFTER):
                        it.marks = []; it.word_marks = []
                        mp3.unlink(missing_ok=True)
                    if mp3.exists() and mp3.stat().st_size > 0:
                        it.audio_path = mp3; it.audio_ready = True   # готовое аудио, без перерендера
                    else:
                        to_render.append(it)      # mp3 нет — дорендерим в фоне
                self.queue_items.append(it)
            except Exception:
                continue
        if cur_id:
            for i, it in enumerate(self.queue_items):
                if it.id == cur_id:
                    self.current_idx = i; break
        if self._dnd_active():           # тишина на старте — не начинать озвучку сами
            self.user_paused = True
        for it in to_render:
            self._q_put(it, 100)
        if to_render:
            self.root.after(0, self._request_render)
        self._render_list()
        log_line(f"session restored: {len(self.queue_items)} items, {len(to_render)} to render")

    def _autosave_session(self):
        """Периодически сохранять очередь — чтобы пережить и аварийное закрытие."""
        try:
            self._save_session()
        except Exception:
            pass
        if not self.stop_event.is_set():
            self.root.after(15000, self._autosave_session)

    def _toggle_pin(self):
        idx = self._selected_idx()
        if idx is None or not (0 <= idx < len(self.queue_items)): return
        self.queue_items[idx].pinned = not self.queue_items[idx].pinned
        self._save_session(); self._render_list()

    def _toggle_favorite(self):
        idx = self._selected_idx()
        if idx is None or not (0 <= idx < len(self.queue_items)): return
        self.queue_items[idx].favorite = not self.queue_items[idx].favorite
        self._save_session(); self._render_list()

    # ------------------ prefs ------------------
    def _load_prefs(self) -> dict:
        try: return json.loads(PREFS_FILE.read_text(encoding="utf-8"))
        except Exception: return {}

    def _save_prefs(self):
        try:
            geom = self.root.geometry() if hasattr(self, "root") else ""
            # свёрнутое в трей/мини-плеер окно отдаёт 1x1 — не затираем им нормальный размер
            if not _geometry_ok(geom):
                geom = self.prefs.get("geometry", "") if _geometry_ok(self.prefs.get("geometry", "")) else ""
            sashes = self.prefs.get("sashes") or [420]
            try:
                if hasattr(self, "body") and self.body.winfo_width() >= 600 and len(self.body.panes()) >= 2:
                    sp = int(self.body.sashpos(0))
                    if 280 <= sp <= self.body.winfo_width() - 320:
                        sashes = [sp]
            except Exception:
                pass
            PREFS_FILE.write_text(json.dumps({
                "rate":     self.var_rate.get(),
                "volume":   self.var_volume.get(),
                "source":   self.var_source.get(),
                "autoplay": self.var_autoplay.get(),
                "dnd": self.var_dnd.get(),
                "quiet_hours": self.var_quiet.get(),
                "quiet_from": self._quiet_from,
                "quiet_to": self._quiet_to,
                "rate_per_source": self.var_rate_per_source.get(),
                "rate_by_source": self._rate_by_source,
                "tools_open": self.var_tools_open.get() if hasattr(self, "var_tools_open") else False,
                "adv_open": self.var_adv_open.get() if hasattr(self, "var_adv_open") else False,
                "show_text_panel": self.var_show_text.get(),
                "show_queue": self.var_show_queue.get(),
                "speak_author": self.var_speak_author.get(),
                "autoscroll": self.var_autoscroll.get(),
                "newest_first": self.var_newest_first.get(),
                "lang": self.var_lang.get() if hasattr(self, "var_lang") else "en",
                "whisper_model": self.var_model.get(),
                "translate_en": self.var_translate.get(),
                "text_font_size": getattr(self, "_text_font_size", 12),
                "accent": self.prefs.get("accent", "#7ee2b8"),
                "theme": self.var_theme.get() if hasattr(self, "var_theme") else "Тёмная",
                "ui_v2_theme": True,
                "ui_v2_layout": True,
                "ui_v2_layout_2": True,
                "disabled_ai_tools": self.prefs.get("disabled_ai_tools", []),
                "voice": getattr(self, "_voice", TTS_VOICE),
                "voice_local_v1": bool(self.prefs.get("voice_local_v1")),
                "auto_update": bool(getattr(self, "_auto_update", True)),
                "stats_seconds": round(float(getattr(self, "_stats_seconds", 0.0)), 1),
                "stats_items": int(getattr(self, "_stats_items", 0)),
                "supporter_key": str(getattr(self, "_supporter_key", "")),
                "last_support_prompt_ts": float(getattr(self, "_last_support_prompt_ts", 0.0)),
                "muted": sorted(getattr(self, "_muted", set())),
                "mode": self.var_mode.get() if hasattr(self, "var_mode") else "Очередь",
                "author_voices": getattr(self, "_author_voices", {}),
                "author_names": getattr(self, "_author_names", {}),
                "tg_chat_id": self.var_tg_chat.get() if hasattr(self, "var_tg_chat") else "",
                "watch": sorted(getattr(self, "_watch", set())),
                "resume": self._resume_snapshot(),
                "geometry": geom,
                "sashes": sashes,
                "mini_geometry": getattr(self, "mini_geometry", "") or self.prefs.get("mini_geometry", ""),
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            self.prefs["sashes"] = sashes
        except Exception:
            pass

    def _resume_snapshot(self):
        try:
            if hasattr(self, "player") and 0 <= self.current_idx < len(self.queue_items) \
                    and self.player.has_track():
                pos = round(self.player.position(), 2)
                if pos > 1:
                    return {"text": self.queue_items[self.current_idx].raw_text, "pos": pos}
        except Exception:
            pass
        return self.prefs.get("resume")

    def _restore_sashes(self):
        saved = self.prefs.get("sashes") or []
        try:
            target = int(saved[0]) if (saved and 420 <= int(saved[0]) <= 720) else 480
            if len(self.body.panes()) >= 2:
                self.body.sashpos(0, target)
        except Exception:
            pass

    def _guard_sash(self, _e=None):
        """Не давать левой колонке очереди схлопнуться в 0px при первичной раскладке PanedWindow."""
        if getattr(self, "_in_guard_sash", False):
            return
        self._in_guard_sash = True
        try:
            if hasattr(self, "body") and len(self.body.panes()) >= 2 and self.body.winfo_width() >= 600:
                cur = int(self.body.sashpos(0))
                if 0 <= cur < 400:
                    saved = self.prefs.get("sashes") or []
                    target = int(saved[0]) if (saved and 420 <= int(saved[0]) <= 720) else 480
                    self.body.sashpos(0, target)
        except Exception:
            pass
        finally:
            self._in_guard_sash = False

    # ------------------ UI ------------------
    @staticmethod
    def _f(size: int = 10):
        return ("Helvetica Neue" if IS_MAC else "Segoe UI", size)

    @staticmethod
    def _f_semi(size: int = 10):
        if IS_MAC:
            return ("Helvetica Neue", size, "bold")
        return ("Segoe UI Semibold", size)

    @staticmethod
    def _bind_context(widget, callback, add="+"):
        """Кроссплатформенная привязка контекстного меню (ПКМ):
        - Windows/Linux: <Button-3>, <Control-Button-1>
        - macOS: <Button-2> (клик/тап двумя пальцами на тачпаде Mac), <Button-3> (внешняя мышь), <Control-Button-1>
        """
        widget.bind("<Button-3>", callback, add=add)
        widget.bind("<Control-Button-1>", callback, add=add)
        if IS_MAC:
            widget.bind("<Button-2>", callback, add=add)

    @staticmethod
    def _tt(widget, text):
        """Навесить подсказку и вернуть виджет (для цепочки .grid()/.pack())."""
        Tooltip(widget, text)
        return widget

    @staticmethod
    def _on_signal_fg(hexc: str) -> str:
        """Контрастный текст поверх акцентной заливки (тёмный на яркой мяте, белый на тёмных цветах)."""
        h = str(hexc or "").lstrip("#")
        if len(h) == 6:
            r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
            if (0.299 * r + 0.587 * g + 0.114 * b) > 145:
                return "#0d0f0e"
        return "#ffffff"

    def _palette(self, theme):
        accent = self.prefs.get("accent", "#7ee2b8")
        if str(theme).lower().startswith("т") or theme == "dark":   # Тёмная (изумрудно-графитовая из макета)
            return dict(bg="#0d0f0e", panel="#131615", panel2="#1c211f", ink="#e8ecea",
                        mute="#7c8782", signal=accent, err="#ff6b6b", hairline="#232927",
                        sel="#183329", active="#262e2b")
        sig_light = "#109868" if accent.lower() == "#7ee2b8" else accent
        return dict(bg="#f4f6f5", panel="#ffffff", panel2="#e9eeec", ink="#161a18",      # Светлая
                    mute="#6c7772", signal=sig_light, err="#d70015", hairline="#d5ddd9",
                    sel="#daf5ea", active="#dce4e0")

    @staticmethod
    def _hex_to_colorref(hexc: str) -> int:
        h = str(hexc or "").lstrip("#")
        if len(h) != 6:
            return 0x00151613
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        return (b << 16) | (g << 8) | r

    def _apply_window_titlebar_theme(self, win=None, c: Optional[dict] = None):
        """Окрасить верхнюю полосу окна Windows (DWM titlebar) в цвет текущей темы (Тёмная / Светлая)."""
        if not IS_WIN:
            return
        win = win or self.root
        c = c or getattr(self, "colors", None) or self._palette(
            self.var_theme.get() if hasattr(self, "var_theme") else "Тёмная"
        )
        dark = str(self.var_theme.get()).lower().startswith("т") if hasattr(self, "var_theme") else True
        try:
            win.update_idletasks()
            wid = win.winfo_id()
            hwnd = ctypes.windll.user32.GetParent(wid) or wid
            dwmapi = ctypes.windll.dwmapi
            dark_val = ctypes.c_int(1 if dark else 0)
            for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE
                dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(dark_val), ctypes.sizeof(dark_val))
            cap_col = ctypes.c_uint(self._hex_to_colorref(c["panel"]))
            txt_col = ctypes.c_uint(self._hex_to_colorref(c["ink"]))
            brd_col = ctypes.c_uint(self._hex_to_colorref(c["hairline"]))
            dwmapi.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(cap_col), ctypes.sizeof(cap_col))  # DWMWA_CAPTION_COLOR
            dwmapi.DwmSetWindowAttribute(hwnd, 36, ctypes.byref(txt_col), ctypes.sizeof(txt_col))  # DWMWA_TEXT_COLOR
            dwmapi.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(brd_col), ctypes.sizeof(brd_col))  # DWMWA_BORDER_COLOR
            ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0027)  # SWP_FRAMECHANGED
        except Exception:
            pass

    @staticmethod
    def _draw_modern_app_icon(size: int = 256):
        """Сгенерировать современную векторную иконку VoicePlayer (изумрудно-графитовый сквёркл + мятная звуковая волна)."""
        from PIL import Image, ImageDraw
        ss = 4 if size <= 128 else 2
        S = size * ss
        im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        pad = int(S * 0.06)
        rad = int(S * 0.24)
        # Внешний корпус-сквёркл с тонкой мятно-изумрудной гранью
        d.rounded_rectangle([pad, pad, S - 1 - pad, S - 1 - pad], radius=rad,
                            fill=(15, 21, 19, 255), outline=(52, 108, 86, 255), width=max(2, int(S * 0.022)))
        in_pad = pad + max(2, int(S * 0.026))
        d.rounded_rectangle([in_pad, in_pad, S - 1 - in_pad, S - 1 - in_pad],
                            radius=max(4, rad - int(S * 0.02)),
                            fill=(20, 29, 26, 255))
        # Мягкий внутренний блик в верхней части
        glow_pad = in_pad + int(S * 0.04)
        d.rounded_rectangle([glow_pad, glow_pad, S - 1 - glow_pad, int(S * 0.48)],
                            radius=max(4, int(rad * 0.7)),
                            fill=(27, 42, 37, 255))
        # 5 скульптурных полос звуковой волны в фирменном мятном цвете (#7ee2b8)
        cx, cy = S / 2.0, S / 2.0
        bar_w = max(4, int(S * 0.088))
        gap = int(S * 0.132)
        heights = [0.24, 0.46, 0.64, 0.46, 0.24]
        colors = [
            (72, 204, 152, 255),
            (110, 222, 178, 255),
            (158, 245, 208, 255),
            (110, 222, 178, 255),
            (72, 204, 152, 255),
        ]
        for idx, (hf, col) in enumerate(zip(heights, colors)):
            bx = int(cx + (idx - 2) * gap)
            bh = max(bar_w, int(S * hf))
            x0 = bx - bar_w // 2
            y0 = int(cy - bh / 2)
            x1 = x0 + bar_w
            y1 = int(cy + bh / 2)
            d.rounded_rectangle([x0, y0, x1, y1], radius=bar_w // 2, fill=col)
        return im.resize((size, size), Image.LANCZOS)

    def _apply_modern_icon(self):
        if IS_WIN:
            if ICON_PATH.exists():
                try:
                    self.root.iconbitmap(default=str(ICON_PATH))
                except Exception:
                    pass
            self.root.after(100, self._apply_win_taskbar_identity)
            self.root.after(1000, self._apply_win_taskbar_identity)
            return

        try:
            from PIL import ImageTk
            self._app_icon_imgs = [
                ImageTk.PhotoImage(self._draw_modern_app_icon(64)),
                ImageTk.PhotoImage(self._draw_modern_app_icon(32)),
                ImageTk.PhotoImage(self._draw_modern_app_icon(16)),
            ]
            self.root.iconphoto(True, *self._app_icon_imgs)
        except Exception:
            pass

    def _top_hwnd(self, win=None):
        if not IS_WIN:
            return 0
        win = win or self.root
        win.update_idletasks()
        u32 = ctypes.windll.user32
        wid = win.winfo_id()
        # winfo_id() — внутренний контейнер Tk; реальный top-level frame окна (заголовок, иконка, taskbar) — GA_ROOT (2)
        GA_ROOT = 2
        top = u32.GetAncestor(wid, GA_ROOT)
        return top or wid

    def _apply_win_taskbar_identity(self):
        """Windows: 1) явно ставим иконку окна через WM_SETICON на top-level frame;
        2) задаём окну AppUserModelID и команду перезапуска — тогда «Закрепить на панели задач»
        создаёт ярлык VoicePlayer с нашей иконкой, который реально запускает плеер."""
        if not IS_WIN:
            return
        from ctypes import wintypes
        try:
            hwnd = self._top_hwnd()
        except Exception:
            return
        stable_ico = Path.home() / ".voice-player" / "icon.ico"
        parent_ico = Path(APP_DIR).parent / "icon.ico"
        ico = ICON_PATH if ICON_PATH.exists() else (parent_ico if parent_ico.exists() else stable_ico)
        # ---- 1. Иконки окна ----
        try:
            if ico.exists():
                u32 = ctypes.windll.user32
                u32.LoadImageW.restype = wintypes.HANDLE
                u32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                                           ctypes.c_int, ctypes.c_int, wintypes.UINT]
                u32.SendMessageW.restype = ctypes.c_ssize_t
                u32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
                set_cls = getattr(u32, "SetClassLongPtrW", None) or u32.SetClassLongW
                set_cls.restype = ctypes.c_size_t
                set_cls.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_size_t]
                big_sz = max(32, u32.GetSystemMetrics(11)) * 2      # SM_CXICON, с запасом под HiDPI
                small_sz = max(16, u32.GetSystemMetrics(49))         # SM_CXSMICON
                LR_LOADFROMFILE, IMAGE_ICON, WM_SETICON = 0x10, 1, 0x0080
                h_big = u32.LoadImageW(None, str(ico), IMAGE_ICON, big_sz, big_sz, LR_LOADFROMFILE)
                h_small = u32.LoadImageW(None, str(ico), IMAGE_ICON, small_sz, small_sz, LR_LOADFROMFILE)
                if h_big:
                    u32.SendMessageW(hwnd, WM_SETICON, 1, h_big)     # ICON_BIG — панель задач/Alt+Tab
                    set_cls(hwnd, -14, h_big)                         # GCLP_HICON
                if h_small:
                    u32.SendMessageW(hwnd, WM_SETICON, 0, h_small)   # ICON_SMALL — заголовок окна
                    set_cls(hwnd, -34, h_small)                       # GCLP_HICONSM
                self._win_icon_handles = (h_big, h_small)            # держим, чтобы не освободились
        except Exception as e:
            log_line(f"taskbar icon error: {e}")
        # ---- 2. AppUserModelID + команда перезапуска для закрепления ----
        try:
            import uuid as _uuid
            launch_cmd = Path.home() / ".voice-player" / "launch.cmd"
            launcher = Path.home() / ".voice-player" / "launch.pyw"
            gui_py = vp_updater.gui_python()
            target = launcher if launcher.exists() else (APP_DIR / "app.py")
            relaunch_cmd = str(launch_cmd) if launch_cmd.exists() else f'"{gui_py}" "{target}"'

            class GUID(ctypes.Structure):
                _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                            ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]

            class PROPERTYKEY(ctypes.Structure):
                _fields_ = [("fmtid", GUID), ("pid", wintypes.DWORD)]

            class PROPVARIANT(ctypes.Structure):
                _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort),
                            ("r3", ctypes.c_ushort), ("p", ctypes.c_void_p), ("p2", ctypes.c_void_p)]

            def _guid(s):
                return GUID.from_buffer_copy(_uuid.UUID(s).bytes_le)

            try:
                ctypes.windll.ole32.CoInitialize(None)
            except Exception:
                pass
            shell32 = ctypes.windll.shell32
            shell32.SHGetPropertyStoreForWindow.restype = ctypes.c_long
            shell32.SHGetPropertyStoreForWindow.argtypes = [wintypes.HWND, ctypes.POINTER(GUID),
                                                            ctypes.POINTER(ctypes.c_void_p)]
            iid = _guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99")      # IID_IPropertyStore
            pps = ctypes.c_void_p()
            hr = shell32.SHGetPropertyStoreForWindow(hwnd, ctypes.byref(iid), ctypes.byref(pps))
            if hr != 0 or not pps.value:
                raise OSError(f"SHGetPropertyStoreForWindow hr={hr}")
            vtbl = ctypes.cast(pps, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
            SetValue = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(PROPERTYKEY),
                                          ctypes.POINTER(PROPVARIANT))(vtbl[6])
            Commit = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)(vtbl[7])
            Release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])
            fmt = _guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3")       # System.AppUserModel.*
            keep = []
            for pid, val in ((2, relaunch_cmd),               # RelaunchCommand
                             (4, "VoicePlayer"),              # RelaunchDisplayNameResource
                             (3, f"{ico},0"),                 # RelaunchIconResource
                             (5, APP_USER_MODEL_ID)):         # ID — ставим последним
                buf = ctypes.create_unicode_buffer(val)
                keep.append(buf)
                pv = PROPVARIANT(vt=31, p=ctypes.cast(buf, ctypes.c_void_p))   # VT_LPWSTR
                SetValue(pps, ctypes.byref(PROPERTYKEY(fmt, pid)), ctypes.byref(pv))
            Commit(pps)
            Release(pps)
        except Exception as e:
            log_line(f"taskbar relaunch props error: {e}")

    def _retranslate_ui(self):
        """Динамически перевести все надписи, кнопки и подсказки интерфейса без перезапуска приложения."""
        try:
            lang_name = vp_i18n.SUPPORTED_LANGUAGES.get(vp_i18n.get_lang(), vp_i18n.get_lang())
            self.root.title(vp_i18n.t("app_title"))

            # Шапка очереди (левая колонка)
            if hasattr(self, "lbl_queue_title"):
                self.lbl_queue_title.configure(text=vp_i18n.t("queue"))
            if hasattr(self, "btn_ai_top"):
                self.btn_ai_top.configure(text="🤖 " + vp_i18n.t("tab_ai"))

            # Таблетки фильтров
            if hasattr(self, "_src_pill_btns") and isinstance(self._src_pill_btns, dict):
                if "all" in self._src_pill_btns:
                    self._src_pill_btns["all"].configure(text=vp_i18n.t("tab_all"))
                if "ai" in self._src_pill_btns:
                    self._src_pill_btns["ai"].configure(text=vp_i18n.t("tab_ai"))
                if "board" in self._src_pill_btns:
                    self._src_pill_btns["board"].configure(text=vp_i18n.t("tab_board"))
                if "favorite" in self._src_pill_btns:
                    self._src_pill_btns["favorite"].configure(text="★")

            # Колонки таблицы
            if hasattr(self, "tree"):
                self.tree.heading("author", text=vp_i18n.t("col_source") + " ⇅")
                self.tree.heading("date", text=vp_i18n.t("col_date") + " ⇅")
                self.tree.heading("time", text=vp_i18n.t("col_time") + " ⇅")
                self.tree.heading("text", text=vp_i18n.t("col_text"))

            # Нижний блок левой колонки (ввод своего текста)
            if hasattr(self, "lbl_custom_text"):
                self.lbl_custom_text.configure(text=vp_i18n.t("custom_text_title"))
            if hasattr(self, "btn_paste_input"):
                self.btn_paste_input.configure(text="📋 " + vp_i18n.t("paste"))
            if hasattr(self, "btn_speak_input"):
                self.btn_speak_input.configure(text="🔊 " + vp_i18n.t("speak_btn"))
            if hasattr(self, "btn_dictate"):
                self.btn_dictate.configure(text=f"🎤 {vp_i18n.t('dictate')}")

            # Правая колонка: кнопки действий
            if hasattr(self, "btn_copy_text"):
                self.btn_copy_text.configure(text=vp_i18n.t("copy"))
            if hasattr(self, "btn_export_mp3_btn"):
                self.btn_export_mp3_btn.configure(text=vp_i18n.t("save_mp3"))
            if hasattr(self, "btn_edit_text"):
                self.btn_edit_text.configure(text=f"✏ {vp_i18n.t('edit')}")
            if hasattr(self, "btn_highlight_btn"):
                self.btn_highlight_btn.configure(text=f"✨ {vp_i18n.t('highlight')}")
            if hasattr(self, "btn_tools"):
                is_open = getattr(self, "var_tools_open", None) and self.var_tools_open.get()
                self.btn_tools.configure(text=f"🛠 {vp_i18n.t('tools')} " + ("▾" if is_open else "▸"))

            # Выдвижная панель инструментов
            if hasattr(self, "btn_tool_doc"):
                self.btn_tool_doc.configure(text="📑 " + vp_i18n.t("tool_doc"))
            if hasattr(self, "btn_tool_audio"):
                self.btn_tool_audio.configure(text="📂 " + vp_i18n.t("tool_audio"))
            if hasattr(self, "btn_tool_folder"):
                self.btn_tool_folder.configure(text="📁 " + vp_i18n.t("tool_folder"))
            if hasattr(self, "btn_tool_board"):
                self.btn_tool_board.configure(text=vp_i18n.t("tool_board"))
            if hasattr(self, "btn_tool_session"):
                self.btn_tool_session.configure(text="💾 " + vp_i18n.t("tool_session"))
            if hasattr(self, "btn_tool_cache"):
                self.btn_tool_cache.configure(text="📁 " + vp_i18n.t("tool_cache"))
            if hasattr(self, "btn_record"):
                self.btn_record.configure(text="🎤 " + vp_i18n.t("tool_record"))
            if hasattr(self, "btn_sysrec"):
                self.btn_sysrec.configure(text="🔊 " + vp_i18n.t("tool_sysaudio"))
            if hasattr(self, "btn_voicecmd"):
                self.btn_voicecmd.configure(text="🎧 " + vp_i18n.t("tool_commands"))

            # Нижняя подсказка
            if hasattr(self, "lbl_hint"):
                self.lbl_hint.configure(text=vp_i18n.t("bottom_hint"))

            # Нижняя панель (Темп, Звук, Режим)
            if hasattr(self, "lbl_tempo"):
                self.lbl_tempo.configure(text=vp_i18n.t("tempo") + ":")
            if hasattr(self, "lbl_sound"):
                self.lbl_sound.configure(text=vp_i18n.t("sound") + ":")
            if hasattr(self, "lbl_mode_title"):
                self.lbl_mode_title.configure(text=vp_i18n.t("mode") + ":")
            if hasattr(self, "cmb_mode"):
                cur_mode_idx = self._get_mode_index()
                m_vals = self._get_mode_values()
                self.cmb_mode["values"] = m_vals
                idx = cur_mode_idx if 0 <= cur_mode_idx < len(m_vals) else 0
                self.var_mode.set(m_vals[idx])
                try:
                    self.cmb_mode.current(idx)
                except Exception:
                    pass

            # Фильтры автора, даты и сброса
            if hasattr(self, "lbl_filt_author"):
                self.lbl_filt_author.configure(text=vp_i18n.t("author") + ":")
            if hasattr(self, "lbl_filt_date"):
                self.lbl_filt_date.configure(text=vp_i18n.t("date") + ":")
            if hasattr(self, "btn_filt_reset"):
                self.btn_filt_reset.configure(text=vp_i18n.t("reset"))
            ALL_FILTER_VALUES = {"все", "all", "todos", "全部", "सभी", "すべて", "alle", "tous", ""}
            if hasattr(self, "var_filter_author") and self.var_filter_author.get().strip().lower() in ALL_FILTER_VALUES:
                self.var_filter_author.set(vp_i18n.t("filter_all"))
            if hasattr(self, "var_filter_date") and self.var_filter_date.get().strip().lower() in ALL_FILTER_VALUES:
                self.var_filter_date.set(vp_i18n.t("filter_all"))
            if hasattr(self, "lbl_now_prompt") and hasattr(self, "prompt_frame") and self.prompt_frame.winfo_ismapped():
                cur_item = self.queue_items[self.current_idx] if 0 <= self.current_idx < len(self.queue_items) else None
                if cur_item and getattr(cur_item, "prompt", ""):
                    prompt_flat = " ".join(cur_item.prompt.split())
                    if len(prompt_flat) > 220:
                        prompt_flat = prompt_flat[:220] + "…"
                    self.lbl_now_prompt.configure(text=f"💬 {vp_i18n.t('prompt_label')}: {prompt_flat}")
            if hasattr(self, "lbl_now_author") and self.current_idx < 0:
                self.lbl_now_author.configure(text=vp_i18n.t("empty_hero_hint"))
            if hasattr(self, "_list_menu") and hasattr(self, "_sleep_menu_index"):
                try:
                    self._list_menu.entryconfigure(self._sleep_menu_index, label=self._sleep_remaining_label())
                except Exception:
                    pass

            if hasattr(self, "var_sleep"):
                off_states = {"выкл", "off", "desactivado", "关闭", "बंद", "オフ", "aus", "désactivé", "0", ""}
                cur_s = str(self.var_sleep.get()).strip()
                if any(cur_s.lower().startswith(x) for x in off_states):
                    self.var_sleep.set(vp_i18n.t("off"))
                else:
                    digits = "".join(c for c in cur_s if c.isdigit())
                    if digits:
                        self.var_sleep.set(f"{digits} {vp_i18n.t('min_short')}")

            # Перевод подсказок кнопок транспорта и шапки
            if hasattr(self, "btn_preview_voice"):
                self._tt(self.btn_preview_voice, vp_i18n.t("listen_selected_voice"))
            if hasattr(self, "btn_toggle"):
                self._tt(self.btn_toggle, vp_i18n.translate_tip("Пауза / продолжить (Пробел)"))
            if hasattr(self, "btn_prev_ctrl"):
                self._tt(self.btn_prev_ctrl, vp_i18n.translate_tip("Предыдущая реплика (←)"))
            if hasattr(self, "btn_next_ctrl"):
                self._tt(self.btn_next_ctrl, vp_i18n.translate_tip("Следующая реплика (→)"))
            if hasattr(self, "btn_repeat_ctrl"):
                self._tt(self.btn_repeat_ctrl, vp_i18n.translate_tip("Повторить текущую с начала (R)"))
            if hasattr(self, "btn_listenall_ctrl"):
                self._tt(self.btn_listenall_ctrl, vp_i18n.translate_tip("Слушать всё подряд — непрослушанные по очереди"))
            if hasattr(self, "btn_filt_reset"):
                self._tt(self.btn_filt_reset, vp_i18n.translate_tip("Сбросить фильтры автора и даты"))
            if hasattr(self, "_src_pill_btns") and isinstance(self._src_pill_btns, dict):
                for sk, tip_k in (("all", "tip_tab_all"), ("ai", "tip_tab_ai"), ("board", "tip_tab_board"), ("favorite", "tip_tab_fav")):
                    if sk in self._src_pill_btns:
                        self._tt(self._src_pill_btns[sk], vp_i18n.t(tip_k))
            if hasattr(self, "btn_ai_top"):
                self.btn_ai_top.configure(text="🤖 " + vp_i18n.t("tab_ai"))
                self._tt(self.btn_ai_top, vp_i18n.translate_tip("Какие нейросети озвучивать (Antigravity, Codex, Claude Code…)"))
            if hasattr(self, "btn_theme"):
                self._tt(self.btn_theme, vp_i18n.translate_tip("Переключить тему (Тёмная / Светлая)"))
            if hasattr(self, "btn_support"):
                self._tt(self.btn_support, vp_i18n.translate_tip("О проекте VoicePlayer · Статистика прослушивания · Поддержать разработку"))
            if hasattr(self, "btn_listmenu"):
                self._tt(self.btn_listmenu, vp_i18n.translate_tip("Действия со списком: подтянуть диалоги, слушать всё, поиск, очистка"))

            # Пересоздать контекстное меню и меню списка с локализованными пунктами
            try:
                self._build_list_menu()
                self._build_context_menu()
            except Exception:
                pass

            # Транспорт
            if hasattr(self, "btn_toggle") and not getattr(self, "_is_playing", False):
                self.btn_toggle.configure(text=self._g_play)

            # Обновить счётчик реплик с текущим языком
            try:
                self._render_list()
            except Exception:
                pass

            self._set_status(vp_i18n.t("lang_applied", lang=lang_name), ok=True)
            log_line(f"UI dynamically retranslated to: {lang_name}")
        except Exception as e:
            log_line(f"retranslate_ui error: {e}")

    def _auto_heal_audio(self):
        """Автоматическое самовосстановление аудиопотока при сбое звукового устройства."""
        try:
            log_line("Auto-healing audio subsystem...")
            if hasattr(self, "player") and self.player:
                try:
                    self.player.stop()
                except Exception:
                    pass
            if _STREAM_OK:
                self.player = StreamPlayer()
                self._stream_backend = True
            else:
                self.player = MciPlayer()
                self._stream_backend = False
            self._set_status("✓ Аудиосистема автоматически восстановлена", ok=True)
        except Exception as e:
            log_line(f"Audio auto-heal failed: {e}")

    def _build_list_menu(self):
        """Создать или обновить выпадающее меню списка очереди с переводом."""
        cm = self.colors
        pri_fg = self._on_signal_fg(cm["signal"])
        if hasattr(self, "_list_menu") and self._list_menu:
            try: self._list_menu.delete(0, "end")
            except Exception: pass
        else:
            self._list_menu = tk.Menu(self.root, tearoff=0, bg=cm["panel"], fg=cm["ink"],
                                      activebackground=cm["signal"], activeforeground=pri_fg, bd=0)
        self._list_menu.add_command(label=vp_i18n.t("menu_pull_ai"),
                                    command=lambda: self._pull_recent_ai(limit_per_source=8))
        self._list_menu.add_command(label=vp_i18n.t("menu_listen_all"), command=self._listen_all)
        self._list_menu.add_command(label=vp_i18n.t("menu_search"), command=self._toggle_search)
        self._list_menu.add_command(label=vp_i18n.t("menu_scroll_current"), command=self._scroll_to_current)
        self._list_menu.add_separator()
        self._list_menu.add_command(label=vp_i18n.t("menu_mark_all"), command=self._mark_all_played)
        self._list_menu.add_command(label=vp_i18n.t("menu_clear_played"), command=self._delete_played)
        self._list_menu.add_command(label=vp_i18n.t("menu_clear_all"), command=self.clear_queue)
        self._list_menu.add_separator()
        self._sleep_menu = tk.Menu(self._list_menu, tearoff=0, bg=cm["panel"], fg=cm["ink"],
                                   activebackground=cm["signal"], activeforeground=pri_fg, bd=0)
        for _m in (15, 30, 45, 60):
            self._sleep_menu.add_command(label=vp_i18n.t("menu_in_m_mins", m=_m),
                                         command=lambda mm=_m: self._set_sleep_timer(mm))
        self._sleep_menu.add_separator()
        self._sleep_menu.add_command(label=vp_i18n.t("menu_sleep_off"), command=lambda: self._set_sleep_timer(0))
        self._list_menu.add_cascade(label=vp_i18n.t("menu_sleep_timer"), menu=self._sleep_menu)
        self._sleep_menu_index = self._list_menu.index("end")
        self._list_menu.add_checkbutton(label=vp_i18n.t("menu_dnd"), variable=self.var_dnd,
                                        command=self._on_dnd_toggle)
        self._list_menu.add_separator()
        self._list_menu.add_command(label=vp_i18n.t("menu_support"),
                                    command=self._open_support_dialog)

    def _build_context_menu(self):
        """Создать или обновить контекстное меню строк очереди с переводом."""
        panel = self.colors["panel"]; ink = self.colors["ink"]
        signal = self.colors["signal"]; pri_fg = self._on_signal_fg(signal)
        if hasattr(self, "ctx_menu") and self.ctx_menu:
            try: self.ctx_menu.delete(0, "end")
            except Exception: pass
        else:
            self.ctx_menu = tk.Menu(self.root, tearoff=0,
                                     bg=panel, fg=ink, activebackground=signal, activeforeground=pri_fg)
        self.ctx_menu.add_command(label=vp_i18n.t("ctx_play_here"),     command=self._play_selected)
        self.ctx_menu.add_command(label=vp_i18n.t("ctx_rerender"),     command=self._retry_selected)
        self.ctx_menu.add_command(label=vp_i18n.t("ctx_rerender_all"), command=self._retry_all_failed)
        self.ctx_menu.add_command(label=vp_i18n.t("ctx_copy_text"),  command=self._copy_selected)
        self.ctx_menu.add_command(label=vp_i18n.t("ctx_show_full"), command=self._show_text_panel_for_selected)
        self.ctx_menu.add_separator()
        self.ctx_menu.add_command(label=vp_i18n.t("ctx_save_text"),  command=self._save_text_selected)
        self.ctx_menu.add_command(label=vp_i18n.t("ctx_save_audio"),  command=self._save_audio_selected)
        self.ctx_menu.add_command(label=vp_i18n.t("ctx_save_subs"), command=self._save_subtitles_selected)
        self.ctx_menu.add_command(label=vp_i18n.t("ctx_speak_rec"), command=self._respeak_selected)
        self.ctx_menu.add_separator()
        self.ctx_menu.add_command(label=vp_i18n.t("ctx_pin"), command=self._toggle_pin)
        self.ctx_menu.add_command(label=vp_i18n.t("ctx_fav"),  command=self._toggle_favorite)
        self.ctx_menu.add_command(label=vp_i18n.t("ctx_mute_author"), command=self._toggle_mute_author)
        self.ctx_menu.add_command(label=vp_i18n.t("ctx_watch_author"), command=self._toggle_watch_author)
        self.ctx_menu.add_command(label=vp_i18n.t("ctx_rename_author"), command=self._rename_author)
        self.ctx_menu.add_command(label=vp_i18n.t("ctx_voice_author"), command=self._assign_author_voice)
        self.ctx_menu.add_command(label=vp_i18n.t("ctx_summary"), command=self._summarize_selected)
        self.ctx_menu.add_command(label=vp_i18n.t("ctx_ai_reply"), command=self._ai_reply_selected)
        self.ctx_menu.add_command(label=vp_i18n.t("ctx_export_mp3"), command=self._export_mp3)
        self.ctx_menu.add_command(label=vp_i18n.t("ctx_move_up"), command=lambda: self._move_selected(-1))
        self.ctx_menu.add_command(label=vp_i18n.t("ctx_move_down"), command=lambda: self._move_selected(+1))
        self.ctx_menu.add_separator()
        self.ctx_menu.add_command(label=vp_i18n.t("ctx_delete"), command=self._delete_selected)

    @staticmethod
    def _shade(hexc, amt):
        """Затемнить (amt<0) или осветлить (amt>0) цвет; amt в долях -1..1."""
        hexc = str(hexc).lstrip("#")
        if len(hexc) != 6:
            return "#" + hexc
        r, g, b = int(hexc[0:2], 16), int(hexc[2:4], 16), int(hexc[4:6], 16)
        if amt < 0:
            f = 1 + amt
            r, g, b = r * f, g * f, b * f
        else:
            r, g, b = r + (255 - r) * amt, g + (255 - g) * amt, b + (255 - b) * amt
        return "#%02x%02x%02x" % (max(0, min(255, int(r))), max(0, min(255, int(g))), max(0, min(255, int(b))))

    def _round_img(self, w, h, r, fill, outline=None, ow=0, bg=None):
        """Скруглённый прямоугольник как PhotoImage (супер-сэмплинг ×4 → сглаживание)."""
        from PIL import Image, ImageDraw, ImageTk
        ss = 4
        bg_col = bg or getattr(self, "col_panel", "#131615")
        im = Image.new("RGBA", (w * ss, h * ss), bg_col)
        d = ImageDraw.Draw(im)
        inset = ow * ss
        d.rounded_rectangle([inset, inset, w * ss - 1 - inset, h * ss - 1 - inset],
                            radius=r * ss, fill=fill,
                            outline=outline, width=(ow * ss if outline else 0))
        im = im.resize((w, h), Image.LANCZOS)
        ph = ImageTk.PhotoImage(im)
        self._btn_imgs.append(ph)            # держим ссылку, иначе Tk соберёт картинку
        return ph

    def _round_buttons(self, style, c, base_font):
        """Поднять кнопки до «эппловских» скруглённых через картинки PIL (9-slice).
        Тихо выходит, если Pillow недоступен или среда (macOS aqua) не поддерживает кастомные элементы."""
        try:
            from PIL import Image, ImageTk  # noqa: F401
        except Exception:
            return
        try:
            self._btn_imgs = []
            gen = self._btn_gen = getattr(self, "_btn_gen", 0) + 1
            ink, signal, panel, hairline = c["ink"], c["signal"], c["panel"], c["hairline"]
            pri_fg = self._on_signal_fg(signal)
            dark = str(self.var_theme.get()).lower().startswith("т") if hasattr(self, "var_theme") else True
            W, H, R = 24, 28, 7
            # вторичная заливка (обычные кнопки) — мягкая, с тонкой окантовкой
            if dark:
                sec_n, sec_h, sec_p = "#1c211f", "#262e2b", "#151918"
            else:
                sec_n, sec_h, sec_p = "#e9eeec", "#dce4e0", "#cfd9d4"
            n  = self._round_img(W, H, R, sec_n, outline=hairline, ow=1, bg=panel)
            hv = self._round_img(W, H, R, sec_h, outline=hairline, ow=1, bg=panel)
            pr = self._round_img(W, H, R, sec_p, outline=hairline, ow=1, bg=panel)
            sec = f"sec{gen}.button"
            style.element_create(sec, "image", n, ("pressed", pr), ("active", hv),
                                 border=R, sticky="nsew", padding=(9, 4))
            leaf = lambda el: [(el, {"sticky": "nsew", "children": [("Button.label", {"sticky": "nsew"})]})]
            style.layout("TButton", leaf(sec))
            style.configure("TButton", foreground=ink, font=base_font, anchor="center")
            style.map("TButton", foreground=[("disabled", c["mute"]), ("active", ink)])
            style.layout("Toggle.TButton", leaf(sec))
            style.configure("Toggle.TButton", foreground=ink, font=base_font, anchor="center")
            style.map("Toggle.TButton", foreground=[("disabled", c["mute"]), ("active", ink)])
            # активная таблетка (выбранный фильтр / выбранная скорость)
            pa_n = self._round_img(W, H, R, c["sel"], outline=signal, ow=1, bg=panel)
            pa_h = self._round_img(W, H, R, self._shade(c["sel"], 0.08), outline=signal, ow=1, bg=panel)
            pa_el = f"pilla{gen}.button"
            style.element_create(pa_el, "image", pa_n, ("pressed", pa_h), ("active", pa_h),
                                 border=R, sticky="nsew", padding=(9, 4))
            style.layout("PillActive.TButton", leaf(pa_el))
            style.configure("PillActive.TButton", foreground=signal if dark else ink,
                            font=self._f_semi(9), anchor="center")
            style.map("PillActive.TButton", foreground=[("active", signal if dark else ink)])
            # первичная (акцент) — мятная заливка, контрастный текст
            pn = self._round_img(W, H, R, signal, bg=panel)
            pp_h = self._round_img(W, H, R, self._shade(signal, -0.08), bg=panel)
            pp_p = self._round_img(W, H, R, self._shade(signal, -0.16), bg=panel)
            pri = f"pri{gen}.button"
            style.element_create(pri, "image", pn, ("pressed", pp_p), ("active", pp_h),
                                 border=R, sticky="nsew", padding=(12, 5))
            style.layout("Primary.TButton", leaf(pri))
            style.configure("Primary.TButton", foreground=pri_fg,
                            font=self._f_semi(10), anchor="center")
            style.map("Primary.TButton", foreground=[("active", pri_fg), ("pressed", pri_fg)])
            # иконочные кнопки: тот же скруглённый фон, но чёткий векторный icon-шрифт
            if getattr(self, "_icon_font", ""):
                ic_el = f"ic{gen}.button"
                style.element_create(ic_el, "image", n, ("pressed", pr), ("active", hv),
                                     border=R, sticky="nsew", padding=(6, 4))
                style.layout("Icon.TButton", leaf(ic_el))
                style.configure("Icon.TButton", foreground=ink, font=(self._icon_font, 11), anchor="center")
                style.map("Icon.TButton", foreground=[("disabled", c["mute"]), ("active", ink)])
                style.layout("IconPrimary.TButton", leaf(pri))
                style.configure("IconPrimary.TButton", foreground=pri_fg,
                                font=(self._icon_font, 13), anchor="center")
                style.map("IconPrimary.TButton", foreground=[("active", pri_fg), ("pressed", pri_fg)])
            # транспортная play/pause — акцентная мятная кнопка (главный фокус нижнего бара)
            tn = self._round_img(W, H, R, signal, bg=panel)
            th = self._round_img(W, H, R, self._shade(signal, -0.08), bg=panel)
            tp = self._round_img(W, H, R, self._shade(signal, -0.16), bg=panel)
            trans = f"trans{gen}.button"
            style.element_create(trans, "image", tn, ("pressed", tp), ("active", th),
                                 border=R, sticky="nsew", padding=(12, 5))
            style.layout("Transport.TButton", leaf(trans))
            _tfont = (self._icon_font, 14) if getattr(self, "_icon_font", "") else self._f_semi(11)
            style.configure("Transport.TButton", foreground=pri_fg, font=_tfont, anchor="center")
            style.map("Transport.TButton", foreground=[("active", pri_fg), ("pressed", pri_fg)])
        except Exception as e:
            log_line(f"round_buttons fallback: {e}")

    def _ic(self, name: str, primary: bool = False):
        """Вернуть (text, style) для иконочной кнопки: чёткий глиф icon-шрифта, если он есть,
        иначе эмодзи-фолбэк на обычном стиле."""
        g = ICON_GLYPHS.get(name)
        if g and getattr(self, "_icon_font", ""):
            return g[0], ("IconPrimary.TButton" if primary else "Icon.TButton")
        return (g[1] if g else "?"), ("Primary.TButton" if primary else "Toggle.TButton")

    def _icon_button(self, parent, name, command, tip="", width=3, primary=False):
        """Миниатюрная иконочная кнопка: чёткий глиф + всплывающая подсказка при наведении."""
        t, st = self._ic(name, primary=primary)
        b = ttk.Button(parent, text=t, width=width, command=command, style=st)
        if tip:
            self._tt(b, tip)
        return b

    def _ic_glyph(self, name, size=12):
        """Глиф + шрифт для СЫРОЙ tk-кнопки (мини-плеер на overrideredirect-окне, где ttk-стиль
        неудобен): чёткий icon-шрифт если он есть, иначе эмодзи на обычном UI-шрифте."""
        g = ICON_GLYPHS.get(name)
        if g and getattr(self, "_icon_font", ""):
            return g[0], (self._icon_font, size)
        return (g[1] if g else "?"), ("Segoe UI", size)

    def _apply_styles(self, c):
        style = ttk.Style()
        try: style.theme_use("clam")
        except Exception: pass
        if not hasattr(self, "_icon_font"):
            import tkinter.font as _tkfont
            try: _fams = set(_tkfont.families(self.root))
            except Exception: _fams = set()
            self._icon_font = ("Segoe Fluent Icons" if "Segoe Fluent Icons" in _fams
                               else "Segoe MDL2 Assets" if "Segoe MDL2 Assets" in _fams else "")
        # глифы для динамической кнопки play/pause (обновляется во многих местах)
        self._g_play = self._ic("play", primary=True)[0]
        self._g_pause = self._ic("pause", primary=True)[0]
        bg, panel, panel2, ink, mute, signal, err_col, hairline, sel, active = (
            c["bg"], c["panel"], c["panel2"], c["ink"], c["mute"], c["signal"],
            c["err"], c["hairline"], c["sel"], c["active"])
        pri_fg = self._on_signal_fg(signal)
        self.root.configure(bg=bg)
        base_font = self._f(10)
        semi_9 = self._f_semi(9)
        semi_10 = self._f_semi(10)
        semi_12 = self._f_semi(12)
        semi_13 = self._f_semi(13)
        style.configure(".", background=panel, foreground=ink, fieldbackground=panel, font=base_font)
        style.configure("TFrame", background=panel)
        style.configure("Bg.TFrame", background=bg)
        style.configure("Card.TFrame", background=panel2)
        style.configure("TPanedwindow", background=hairline)
        style.configure("Sash", sashthickness=3, gripcount=0, background=hairline)
        style.configure("TLabel", background=panel, foreground=ink, font=base_font)
        style.configure("Card.TLabel", background=panel2, foreground=ink, font=base_font)
        style.configure("Prompt.TLabel", background=panel2, foreground=signal, font=semi_9)
        style.configure("Dim.TLabel", background=panel, foreground=mute, font=self._f(9))
        style.configure("Badge.TLabel", background=sel, foreground=signal, font=semi_9)
        style.configure("Title.TLabel", background=panel, foreground=ink, font=semi_12)
        style.configure("Hero.TLabel", background=panel, foreground=ink, font=semi_13)
        style.configure("Now.TLabel", background=panel, foreground=ink, font=self._f(11))
        # плоский фолбэк (если PIL нет — останется он)
        style.configure("TButton", background=panel2, foreground=ink, borderwidth=0, focusthickness=0, padding=(10, 5), anchor="center", font=base_font)
        style.map("TButton", background=[("active", active), ("pressed", hairline)], foreground=[("active", ink)])
        style.configure("PillActive.TButton", background=sel, foreground=signal, borderwidth=0, padding=(10, 5), anchor="center", font=semi_9)
        style.map("PillActive.TButton", background=[("active", sel)], foreground=[("active", signal)])
        style.configure("Primary.TButton", background=signal, foreground=pri_fg, borderwidth=0, padding=(14, 6), anchor="center", font=semi_10)
        style.map("Primary.TButton", background=[("active", self._shade(signal, -0.08)), ("pressed", self._shade(signal, -0.16))], foreground=[("active", pri_fg)])
        style.configure("Transport.TButton", background=signal, foreground=pri_fg, borderwidth=0, padding=(14, 6), anchor="center", font=semi_12)
        style.map("Transport.TButton", background=[("active", self._shade(signal, -0.08)), ("pressed", self._shade(signal, -0.16))], foreground=[("active", pri_fg)])
        style.configure("Toggle.TButton", background=panel2, foreground=ink, borderwidth=0, padding=(9, 4), anchor="center")
        style.map("Toggle.TButton", background=[("active", active)], foreground=[("active", ink)])
        style.configure("TScale", background=panel, troughcolor=hairline, borderwidth=0)
        style.configure("TEntry", fieldbackground=panel2, foreground=ink, insertcolor=ink, bordercolor=hairline, borderwidth=1, padding=5)
        style.configure("TCombobox", fieldbackground=panel2, foreground=ink, background=panel2, arrowcolor=ink, bordercolor=hairline, borderwidth=1, padding=4)
        style.map("TCombobox", fieldbackground=[("readonly", panel2)], foreground=[("readonly", ink)], bordercolor=[("focus", signal)])
        style.configure("TCheckbutton", background=panel, foreground=ink, font=base_font)
        style.map("TCheckbutton", background=[("active", panel)], foreground=[("active", ink)])
        style.configure("Treeview", background=panel, foreground=ink, fieldbackground=panel,
                        bordercolor=hairline, lightcolor=panel, darkcolor=panel,
                        rowheight=38, borderwidth=1, relief="flat", font=base_font)
        style.configure("Treeview.Heading", background=panel2, foreground=mute,
                        bordercolor=hairline, lightcolor=panel2, darkcolor=panel2,
                        relief="flat", borderwidth=0, padding=(4, 4), font=semi_9)
        style.map("Treeview.Heading", background=[("active", active)])
        style.map("Treeview", background=[("selected", sel)], foreground=[("selected", ink)])
        style.configure("Vertical.TScrollbar", background=panel2, troughcolor=panel,
                        bordercolor=panel, lightcolor=panel, darkcolor=panel,
                        arrowcolor=mute, borderwidth=0, arrowsize=10, relief="flat")
        style.map("Vertical.TScrollbar", background=[("active", active)])
        style.configure("Horizontal.TScrollbar", background=panel2, troughcolor=panel,
                        bordercolor=panel, lightcolor=panel, darkcolor=panel,
                        arrowcolor=mute, borderwidth=0, arrowsize=10, relief="flat")
        # апгрейд до скруглённых «эппловских» кнопок
        self._round_buttons(style, c, base_font)

    def _apply_theme(self, theme):
        self.prefs["theme"] = theme
        c = self.colors = self._palette(theme)
        self._apply_styles(c)
        self.col_signal, self.col_hairline, self.col_panel = c["signal"], c["hairline"], c["panel"]
        self.col_ink, self.col_mute, self.col_sel = c["ink"], c["mute"], c["sel"]
        pri_fg = self._on_signal_fg(c["signal"])
        dark = str(theme).lower().startswith("т") or theme == "dark"
        try:
            if hasattr(self, "btn_theme"):
                self.btn_theme.configure(text="☀" if dark else "🌙")
            self.text_widget.configure(bg=c["panel"], fg=c["ink"], insertbackground=c["ink"])
            self.text_widget.tag_configure("speaking", background=c["sel"], foreground=c["ink"])
            self.text_widget.tag_configure("speaking_word", background=c["signal"], foreground=pri_fg)
            self.text_widget.tag_configure("entity", foreground=c["signal"], underline=True)
            self.tts_input.configure(bg=c["panel2"], fg=c["ink"], insertbackground=c["ink"],
                                     highlightbackground=c["hairline"], highlightcolor=c["signal"])
            self.seek_canvas.configure(bg=c["panel"])
            if hasattr(self, "transport_divider"):
                self.transport_divider.configure(bg=c["hairline"])
            if hasattr(self, "prompt_bar"):
                self.prompt_bar.configure(bg=c["signal"])
            self.tree.tag_configure("current", background=c["sel"])
            self.tree.tag_configure("done", foreground=c["mute"])
            self.tree.tag_configure("error", foreground=c["err"])
            self.ctx_menu.configure(bg=c["panel"], fg=c["ink"],
                                    activebackground=c["signal"], activeforeground=pri_fg)
            self._list_menu.configure(bg=c["panel"], fg=c["ink"],
                                      activebackground=c["signal"], activeforeground=pri_fg)
            self._update_filter_pills()
            self._update_rate_pills()
            self._panel_key = None
            self._update_text_panel()
            self._draw_seek()
            self._apply_window_titlebar_theme(self.root, c)
            self._restyle_update_banner()
            self._restyle_sponsor_card(c)
            for child_attr in ("_ai_win", "_support_win"):
                cw = getattr(self, child_attr, None)
                if cw is not None and cw.winfo_exists():
                    self._apply_window_titlebar_theme(cw, c)
        except Exception as e:
            log_line(f"apply_theme partial: {e}")
        self._save_prefs()

    def _on_theme_change(self, _e=None):
        self._apply_theme(self.var_theme.get())

    def _toggle_theme_quick(self):
        cur = str(self.var_theme.get())
        nxt = "Светлая" if cur.lower().startswith("т") else "Тёмная"
        self.var_theme.set(nxt)
        self._apply_theme(nxt)

    def _build_ui(self):
        self.root.title(APP_NAME)
        self._apply_modern_icon()

        # Однократный перевод старого дефолта на новую тёмно-мятную тему 2-колоночного макета
        if not self.prefs.get("ui_v2_layout_2"):
            self.prefs["ui_v2_layout_2"] = True
            self.prefs["ui_v2_layout"] = True
            self.prefs["ui_v2_theme"] = True
            self.prefs["theme"] = "Тёмная"
            self.prefs["accent"] = "#7ee2b8"
            self.prefs["show_text_panel"] = True
            self.prefs["show_queue"] = True
            self.prefs["tools_open"] = False
            self.prefs["adv_open"] = False
            self.prefs["sashes"] = [470]
            self.prefs["geometry"] = "1280x760"
            self.var_show_text.set(True)
            self.var_show_queue.set(True)

        geom = self.prefs.get("geometry") or ""
        self.root.geometry(geom if _geometry_ok(geom) else "1280x760")
        self.root.minsize(960, 560)

        self._accent_colors = {"Мята": "#7ee2b8", "Красный": "#fc3c44", "Синий": "#0a84ff",
                               "Графит": "#48484a", "Зелёный": "#34c759", "Фиолетовый": "#af52de"}
        self.var_theme = tk.StringVar(value=self.prefs.get("theme", "Тёмная"))
        self.var_sleep = tk.StringVar(value=vp_i18n.t("off"))
        self.var_accent = tk.StringVar(value=next(
            (k for k, v in self._accent_colors.items() if v == self.prefs.get("accent", "#7ee2b8")),
            "Мята"))
        c = self.colors = self._palette(self.var_theme.get())
        self._apply_styles(c)
        self._apply_window_titlebar_theme(self.root, c)
        self.root.after(120, lambda: self._apply_window_titlebar_theme(self.root, self.colors))
        if IS_MAC:
            try:
                self.root.lift()
                self.root.attributes("-topmost", True)
                self.root.after(300, lambda: self.root.attributes("-topmost", False))
                self.root.focus_force()
                subprocess.Popen([
                    "/usr/bin/osascript", "-e",
                    'tell application "System Events" to set frontmost of first process whose unix id is ' + str(os.getpid()) + ' to true'
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass
        bg, panel, panel2, ink, mute, signal, err_col, hairline, sel, active = (
            c["bg"], c["panel"], c["panel2"], c["ink"], c["mute"], c["signal"],
            c["err"], c["hairline"], c["sel"], c["active"])
        pri_fg = self._on_signal_fg(signal)
        self.col_signal, self.col_hairline, self.col_panel = signal, hairline, panel
        self.col_ink, self.col_mute, self.col_sel = ink, mute, sel
        self._dnd_badge_text = None

        # ==================== НИЖНИЙ ПЛЕЕР-БАР (на всю ширину окна) ====================
        transport_bar = ttk.Frame(self.root, padding=(14, 8))
        transport_bar.pack(side="bottom", fill="x")
        transport_bar.columnconfigure(1, weight=1)
        self.transport_divider = tk.Frame(self.root, bg=hairline, height=1, bd=0, highlightthickness=0)
        self.transport_divider.pack(side="bottom", fill="x")

        # Левая группа: ⏮ ▶/⏸ ⏭ 🔁 ⏩
        ctrl = ttk.Frame(transport_bar)
        ctrl.grid(row=0, column=0, sticky="w", padx=(0, 12))
        self.btn_prev_ctrl = self._icon_button(ctrl, "prev", self.action_prev, "Предыдущая реплика (←)", width=3)
        self.btn_prev_ctrl.pack(side="left", padx=(0, 4))
        self.btn_toggle = ttk.Button(ctrl, text=self._g_play, width=4, command=self.action_toggle,
                                     style="Transport.TButton")
        self.btn_toggle.pack(side="left", padx=2)
        self._tt(self.btn_toggle, "Пауза / продолжить (Пробел)")
        self.btn_next_ctrl = self._icon_button(ctrl, "next", self.action_next, "Следующая реплика (→)", width=3)
        self.btn_next_ctrl.pack(side="left", padx=(4, 0))
        self.btn_repeat_ctrl = self._icon_button(ctrl, "repeat", self.action_repeat, "Повторить текущую с начала (R)", width=3)
        self.btn_repeat_ctrl.pack(side="left", padx=(4, 0))
        self.btn_listenall_ctrl = self._icon_button(ctrl, "listenall", self._listen_all,
                          "Слушать всё подряд — непрослушанные по очереди", width=3)
        self.btn_listenall_ctrl.pack(side="left", padx=(4, 0))

        # Центральная группа: время + широкая волновая дорожка (waveform) + длительность
        seek_row = ttk.Frame(transport_bar)
        seek_row.grid(row=0, column=1, sticky="ew", padx=6)
        seek_row.columnconfigure(1, weight=1)
        self.lbl_pos = ttk.Label(seek_row, text="0:00", style="Dim.TLabel", width=5)
        self.lbl_pos.grid(row=0, column=0, sticky="w")
        self.seek_canvas = tk.Canvas(seek_row, height=32, bg=panel, highlightthickness=0,
                                     bd=0, cursor="hand2")
        self.seek_canvas.grid(row=0, column=1, sticky="ew", padx=8)
        self.lbl_dur = ttk.Label(seek_row, text="0:00", style="Dim.TLabel", width=5)
        self.lbl_dur.grid(row=0, column=2, sticky="e")
        self._seek_frac = 0.0
        self._wave = None; self._wave_id = None
        self._ab = [None, None]
        self.seek_canvas.bind("<Button-1>", self._seek_canvas_click)
        self.seek_canvas.bind("<B1-Motion>", self._seek_canvas_click)
        self.seek_canvas.bind("<ButtonRelease-1>", self._seek_canvas_release)
        self.seek_canvas.bind("<Configure>", lambda e: self._draw_seek())
        self._tt(self.seek_canvas, "Дорожка: кликни в любое место — переход на этот момент")

        # Правая группа: пресеты скорости [1×][1.25×][1.5×][2×] + Голос + [⋯] + [❐ мини]
        rctrl = ttk.Frame(transport_bar)
        rctrl.grid(row=0, column=2, sticky="e", padx=(12, 0))
        self._rate_preset_btns = {}
        for val in (1.0, 1.25, 1.5, 2.0):
            b = ttk.Button(rctrl, text=f"{val:g}×", width=4, style="Toggle.TButton",
                           command=lambda v=val: self._set_rate_preset(v))
            b.pack(side="left", padx=(0, 3))
            self._rate_preset_btns[val] = b

        vcb = ttk.Combobox(rctrl, textvariable=self.var_voice, state="readonly",
                           values=list(VOICES.keys()), width=18)
        vcb.pack(side="left", padx=(8, 4))
        vcb.bind("<<ComboboxSelected>>", self._on_voice_change)
        self._tt(vcb, "Голос озвучки («На компьютере» — локальный Piper, работает без интернета)")
        self.btn_preview_voice = ttk.Button(rctrl, text="▶", width=2, style="Toggle.TButton", command=self._preview_voice)
        self.btn_preview_voice.pack(side="left", padx=(0, 6))
        self._tt(self.btn_preview_voice, vp_i18n.t("listen_selected_voice"))

        self.var_adv_open = tk.BooleanVar(value=bool(self.prefs.get("adv_open", False)))
        self.btn_adv = ttk.Button(rctrl, text="⋯", width=3, style="Toggle.TButton",
                                  command=self._toggle_adv)
        self.btn_adv.pack(side="left", padx=(0, 4))
        self._tt(self.btn_adv, "Ещё: громкость, плавная скорость, режим, A-B повтор, закладки, фразы")
        self._icon_button(rctrl, "mini", self._enter_mini,
                          "Свернуть в мини-плеер поверх окон").pack(side="left")

        # Раскрываемый ряд «⋯» в нижней панели (A-B повтор, закладки, фразы, громкость, точная скорость, режим)
        abrow = self.abrow = ttk.Frame(transport_bar)
        abrow.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(8, 2))
        self.btn_a = ttk.Button(abrow, text="A", width=3, command=self._set_ab_a, style="Toggle.TButton")
        self.btn_a.pack(side="left"); self._tt(self.btn_a, "Начало A-B повтора")
        self.btn_b = ttk.Button(abrow, text="B", width=3, command=self._set_ab_b, style="Toggle.TButton")
        self.btn_b.pack(side="left", padx=(4, 0)); self._tt(self.btn_b, "Конец A-B повтора")
        self._icon_button(abrow, "close", self._clear_ab, "Сбросить A-B повтор").pack(side="left", padx=(4, 0))
        self._icon_button(abrow, "bookmark", self._add_bookmark,
                          "Добавить закладку на текущей позиции").pack(side="left", padx=(8, 0))
        self._icon_button(abrow, "sentprev", lambda: self._jump_sentence(-1),
                          "Назад на фразу (Shift+←)").pack(side="left", padx=(8, 0))
        self._icon_button(abrow, "sentnext", lambda: self._jump_sentence(+1),
                          "Вперёд на фразу (Shift+→)").pack(side="left", padx=(4, 0))

        self.lbl_tempo = ttk.Label(abrow, text=vp_i18n.t("tempo") + ":", style="Dim.TLabel")
        self.lbl_tempo.pack(side="left", padx=(14, 4))
        ttk.Scale(abrow, from_=0.5, to=2.0, variable=self.var_rate, orient="horizontal",
                  length=90, command=self._on_rate_change).pack(side="left")
        self.lbl_rate = ttk.Label(abrow, text=f"{self.var_rate.get():.2f}×", width=5, style="Dim.TLabel")
        self.lbl_rate.pack(side="left", padx=(4, 0))

        self.lbl_sound = ttk.Label(abrow, text=vp_i18n.t("sound") + ":", style="Dim.TLabel")
        self.lbl_sound.pack(side="left", padx=(12, 4))
        ttk.Scale(abrow, from_=0.0, to=1.0, variable=self.var_volume, orient="horizontal",
                  length=80, command=self._on_vol_change).pack(side="left")
        self.lbl_vol = ttk.Label(abrow, text=f"{int(self.var_volume.get()*100)}%", width=5, style="Dim.TLabel")
        self.lbl_vol.pack(side="left", padx=(4, 0))

        self.lbl_mode_title = ttk.Label(abrow, text=vp_i18n.t("mode") + ":", style="Dim.TLabel")
        self.lbl_mode_title.pack(side="left", padx=(12, 4))
        self.cmb_mode = ttk.Combobox(abrow, textvariable=self.var_mode, state="readonly", width=14,
                                     values=self._get_mode_values())
        self.cmb_mode.pack(side="left")
        self.cmb_mode.bind("<<ComboboxSelected>>", lambda e: self._save_prefs())
        self._apply_adv_state()

        # ==================== 2-КОЛОНОЧНОЕ ТЕЛО: СЛЕВА ОЧЕРЕДЬ, СПРАВА ЧИТАЛКА ====================
        self.body = ttk.PanedWindow(self.root, orient="horizontal")
        self.body.pack(fill="both", expand=True, padx=0, pady=0)
        self.body.bind("<Configure>", self._guard_sash)

        # -------------------- ЛЕВАЯ КОЛОНКА: ОЧЕРЕДЬ + СВОЙ ТЕКСТ --------------------
        self.mid_frame = mid = ttk.Frame(self.body, padding=(12, 10))
        self.body.add(mid, weight=1)
        self._mid_added = True
        mid.columnconfigure(0, weight=1)
        mid.rowconfigure(4, weight=1)

        # Шапка очереди: «Очередь» + непрослушанные + счётчик | [🤖 Нейросети] [☀/🌙] [⚙] [⋮]
        head = ttk.Frame(mid)
        head.grid(row=0, column=0, sticky="ew")
        head.columnconfigure(0, weight=1)
        htitle = ttk.Frame(head)
        htitle.grid(row=0, column=0, sticky="w")
        self.lbl_queue_title = ttk.Label(htitle, text=vp_i18n.t("queue"), style="Title.TLabel")
        self.lbl_queue_title.pack(side="left")
        self.lbl_unread = ttk.Label(htitle, text="", style="Badge.TLabel", cursor="hand2")
        self.lbl_unread.pack(side="left", padx=(8, 0))
        self.lbl_unread.bind("<Button-1>", lambda e: self._listen_all())
        self._tt(self.lbl_unread, "Непрослушанные реплики. Клик — слушать всё подряд")
        self.lbl_qcount = ttk.Label(htitle, text="0", style="Dim.TLabel")
        self.lbl_qcount.pack(side="left", padx=(8, 0))

        hbtns = ttk.Frame(head)
        hbtns.grid(row=0, column=1, sticky="e")
        dark_now = str(self.var_theme.get()).lower().startswith("т")
        self.btn_theme = ttk.Button(hbtns, text=("☀" if dark_now else "🌙"), width=3,
                                    style="Toggle.TButton", command=self._toggle_theme_quick)
        self.btn_theme.pack(side="left", padx=(0, 4))
        self._tt(self.btn_theme, "Переключить тему (Тёмная / Светлая)")

        self.btn_ai_top = ttk.Button(hbtns, text="🤖 " + vp_i18n.t("tab_ai"), style="Toggle.TButton",
                                     command=self._open_ai_dialog)
        self.btn_ai_top.pack(side="left", padx=(0, 4))
        self._tt(self.btn_ai_top, "Какие нейросети озвучивать (Antigravity, Codex, Claude Code…)")

        is_pro = self._is_supporter()
        self.btn_support = ttk.Button(hbtns, text=("★ Pro" if is_pro else "♥"), width=(5 if is_pro else 3),
                                      style=("PillActive.TButton" if is_pro else "Toggle.TButton"),
                                      command=self._open_support_dialog)
        self.btn_support.pack(side="left", padx=(0, 4))
        self._tt(self.btn_support, "О проекте VoicePlayer · Статистика прослушивания · Поддержать разработку")

        self._icon_button(hbtns, "settings", self._open_settings, "Настройки").pack(side="left", padx=(0, 4))

        self._build_list_menu()
        _mt, _ms = self._ic("more")
        self.btn_listmenu = ttk.Button(hbtns, text=_mt, width=3, style=_ms, command=self._open_list_menu)
        self.btn_listmenu.pack(side="left")
        self._tt(self.btn_listmenu, "Действия со списком: подтянуть диалоги, слушать всё, поиск, очистка")

        # Плашка неподключённых нейросетей (если есть)
        self.lbl_ai = ttk.Label(mid, text="", style="Badge.TLabel", cursor="hand2", padding=(6, 2))
        self.lbl_ai.grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.lbl_ai.bind("<Button-1>", lambda e: self._open_ai_dialog())
        self._tt(self.lbl_ai, "Найдены нейросети — нажмите, чтобы подключить авто-озвучку")
        self.lbl_ai.grid_remove()

        # Таблетки фильтров [Все] [Нейросети] [Доска] [★] + [🔄] [🔍] [▾]
        pills_bar = ttk.Frame(mid)
        pills_bar.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        self._src_pill_btns = {}
        for src_key, pill_lbl, tip_txt in (
            ("all",      vp_i18n.t("tab_all"),   "Все реплики в очереди"),
            ("ai",       vp_i18n.t("tab_ai"),    "Ответы и диалоги Antigravity, Codex, Claude Code, Cursor, Gemini"),
            ("board",    vp_i18n.t("tab_board"), "Сообщения командной доски"),
            ("favorite", "★",                    "Избранные и закреплённые реплики"),
        ):
            b = ttk.Button(pills_bar, text=pill_lbl,
                           style="Toggle.TButton", command=lambda k=src_key: self._set_source_pill(k))
            b.pack(side="left", padx=(0, 4))
            self._tt(b, tip_txt)
            self._src_pill_btns[src_key] = b

        self._tt(ttk.Button(pills_bar, text="▾", width=2, style="Toggle.TButton",
                            command=self._toggle_extra_filters),
                 "Фильтр по автору и дате").pack(side="right")
        self._icon_button(pills_bar, "search", self._toggle_search,
                          "Поиск по очереди (Ctrl+F)").pack(side="right", padx=(0, 3))
        self._icon_button(pills_bar, "refresh", lambda: self._pull_recent_ai(limit_per_source=6),
                          "Подтянуть свежие диалоги и ответы из Antigravity и Codex").pack(side="right", padx=(0, 3))

        # Скрываемая панель «Автор + Дата + Поиск» (раскрывается по 🔍 или ▾)
        sub_bars = ttk.Frame(mid)
        sub_bars.grid(row=3, column=0, sticky="ew")
        sub_bars.columnconfigure(0, weight=1)

        self.filt_frame = filt = ttk.Frame(sub_bars)
        self.lbl_filt_author = ttk.Label(filt, text=vp_i18n.t("author") + ":", style="Dim.TLabel")
        self.lbl_filt_author.pack(side="left")
        self.cmb_author = ttk.Combobox(filt, textvariable=self.var_filter_author,
                                       state="readonly", width=13, values=[vp_i18n.t("filter_all")])
        self.cmb_author.pack(side="left", padx=(4, 8))
        self.cmb_author.bind("<<ComboboxSelected>>", lambda e: self._render_list())
        self.lbl_filt_date = ttk.Label(filt, text=vp_i18n.t("date") + ":", style="Dim.TLabel")
        self.lbl_filt_date.pack(side="left")
        self.cmb_date = ttk.Combobox(filt, textvariable=self.var_filter_date,
                                     state="readonly", width=10, values=[vp_i18n.t("filter_all")])
        self.cmb_date.pack(side="left", padx=(4, 8))
        self.cmb_date.bind("<<ComboboxSelected>>", lambda e: self._render_list())
        self.btn_filt_reset = ttk.Button(filt, text=vp_i18n.t("reset"), width=8, command=self._reset_filters,
                                         style="Toggle.TButton")
        self.btn_filt_reset.pack(side="left")

        self.search_frame = ttk.Frame(sub_bars)
        self.search_entry = ttk.Entry(self.search_frame, textvariable=self.var_search)
        self.search_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self._bind_context(self.search_entry, self._show_entry_menu)
        ttk.Button(self.search_frame, text="✕", width=3,
                   command=self._toggle_search).pack(side="left")

        # Список очереди (Treeview)
        cols = ("src", "author", "date", "time", "text")
        list_wrap = ttk.Frame(mid)
        list_wrap.grid(row=4, column=0, sticky="nsew", pady=(6, 8))
        list_wrap.rowconfigure(0, weight=1)
        list_wrap.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(list_wrap, columns=cols, show="headings", selectmode="extended")
        self.tree.heading("src", text="")
        self.tree.heading("author", text=vp_i18n.t("col_source") + " ⇅", command=lambda: self._sort_by("author"))
        self.tree.heading("date", text=vp_i18n.t("col_date") + " ⇅", command=lambda: self._sort_by("date"))
        self.tree.heading("time", text=vp_i18n.t("col_time") + " ⇅", command=lambda: self._sort_by("time"))
        self.tree.heading("text", text=vp_i18n.t("col_text"), command=lambda: self._sort_by("text"))
        self.tree.column("src", width=26, anchor="center", stretch=False)
        self.tree.column("author", width=148, anchor="w", stretch=False)
        self.tree.column("date", width=74, anchor="center", stretch=False)
        self.tree.column("time", width=58, anchor="center", stretch=False)
        self.tree.column("text", width=180, anchor="w", stretch=True)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(list_wrap, orient="vertical", command=self.tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.tag_configure("current", background=sel)
        self.tree.tag_configure("done", foreground=mute)
        self.tree.tag_configure("error", foreground=err_col)
        self.tree.bind("<Double-Button-1>", lambda e: self._play_selected())
        self.tree.bind("<Return>",          lambda e: self._play_selected())
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self._bind_context(self.tree,       self._show_context_menu)
        self.tree.bind("<Delete>",          lambda e: self._delete_selected())
        self.tree.bind("<Control-KeyPress>", self._tree_ctrl)
        self.tree.bind("<MouseWheel>",      lambda e: self.root.after_idle(self._update_row_waves), add="+")
        self.tree.bind("<Configure>",       lambda e: self.root.after_idle(self._update_row_waves), add="+")

        # Нижний блок левой колонки: «Свой текст или таблица» + кнопка «Озвучить»
        input_wrap = ttk.Frame(mid)
        input_wrap.grid(row=5, column=0, sticky="ew", pady=(2, 0))
        input_wrap.columnconfigure(0, weight=1)

        in_head = ttk.Frame(input_wrap)
        in_head.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        self.lbl_custom_text = ttk.Label(in_head, text=vp_i18n.t("custom_text_title"), style="Dim.TLabel")
        self.lbl_custom_text.pack(side="left")
        self._icon_button(in_head, "broom", lambda: (self.tts_input.delete("1.0", "end"), self.tts_input.configure(height=3)),
                          "Очистить поле", width=2).pack(side="right")
        self.btn_paste_input = ttk.Button(in_head, text="📋 " + vp_i18n.t("paste"), style="Toggle.TButton",
                                         command=self._paste_into_input)
        self.btn_paste_input.pack(side="right", padx=(0, 4))
        self._tt(self.btn_paste_input, "Вставить текст или таблицу из буфера обмена")

        tts_box = ttk.Frame(input_wrap)
        tts_box.grid(row=1, column=0, sticky="ew")
        tts_box.columnconfigure(0, weight=1)
        self.tts_input = tk.Text(tts_box, width=22, height=3, wrap="word", relief="flat", bd=0,
                                 bg=panel2, fg=ink, insertbackground=ink, undo=True, autoseparators=True,
                                 highlightthickness=1, highlightbackground=hairline, highlightcolor=signal,
                                 font=("Segoe UI", 10), padx=8, pady=6)
        self.tts_input.grid(row=0, column=0, sticky="ew")
        tin_sb = ttk.Scrollbar(tts_box, orient="vertical", command=self.tts_input.yview)
        tin_sb.grid(row=0, column=1, sticky="ns")
        self.tts_input.configure(yscrollcommand=tin_sb.set)

        tts_btns = _wrap_bar(ttk.Frame(input_wrap))
        tts_btns.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        self.btn_speak_input = ttk.Button(tts_btns, text="🔊 " + vp_i18n.t("speak_btn"), style="Primary.TButton",
                                         command=self._speak_input)
        self.btn_speak_input.pack(side="left")
        self._tt(self.btn_speak_input, "Озвучить текст из поля (Ctrl+Enter)")
        self.btn_dictate = ttk.Button(tts_btns, text="🎤 " + vp_i18n.t("dictate"), command=self._toggle_dictate)
        self.btn_dictate.pack(side="left", padx=(5, 0))
        self._tt(self.btn_dictate, "Продиктовать текст голосом в это поле")
        self._tt(self.btn_dictate, "Продиктовать текст голосом в это поле")
        self._icon_button(tts_btns, "globe", self._speak_url,
                          "Ссылка (URL) в поле → озвучить статью", width=3).pack(side="left", padx=(5, 0))
        self._icon_button(tts_btns, "copy", self._copy_field,
                          "Скопировать текст из поля", width=3).pack(side="left", padx=(4, 0))
        self._tt(ttk.Button(tts_btns, text="📤", width=3, style="Toggle.TButton", command=self._send_to_board),
                 "Отправить текст из поля на доску команды").pack(side="left", padx=(4, 0))
        self._tt(ttk.Button(tts_btns, text="📨", width=3, style="Toggle.TButton", command=self._send_to_telegram),
                 "Отправить текст из поля в Telegram").pack(side="left", padx=(4, 0))

        def _on_input_key_release(_e=None):
            if not self.tts_input.get("1.0", "end").strip():
                try: self.tts_input.configure(height=3)
                except Exception: pass
        self.tts_input.bind("<KeyRelease>", _on_input_key_release)
        self.tts_input.bind("<Control-Return>", lambda e: (self._speak_input(), "break")[1])
        self.tts_input.bind("<Control-KeyPress>", self._input_ctrl)
        self.tts_input.bind("<<Paste>>", self._on_input_paste)
        self._bind_context(self.tts_input, self._show_tts_input_menu)

        # Карточка спонсора / рекомендации (для монетизации бесплатной версии)
        self.sponsor_card = tk.Frame(mid, bg=panel2, highlightthickness=1, highlightbackground=hairline, padx=10, pady=6)
        self.sponsor_card.grid(row=6, column=0, sticky="ew", pady=(6, 0))
        self.sponsor_card.columnconfigure(0, weight=1)

        sp_left = tk.Frame(self.sponsor_card, bg=panel2)
        sp_left.pack(side="left", fill="both", expand=True)

        sp_top = tk.Frame(sp_left, bg=panel2)
        sp_top.pack(fill="x")
        self.lbl_sponsor_badge = tk.Label(sp_top, text="Партнёр", bg="#222b27", fg=signal,
                                          font=self._f_semi(8), padx=5, pady=1)
        self.lbl_sponsor_badge.pack(side="left")
        self.lbl_sponsor_title = tk.Label(sp_top, text="", bg=panel2, fg=ink,
                                          font=self._f_semi(9), anchor="w")
        self.lbl_sponsor_title.pack(side="left", padx=(6, 0), fill="x", expand=True)

        self.lbl_sponsor_desc = tk.Label(sp_left, text="", bg=panel2, fg=mute,
                                         font=self._f(8), justify="left", anchor="w", wraplength=340)
        self.lbl_sponsor_desc.pack(fill="x", pady=(2, 0))

        sp_right = tk.Frame(self.sponsor_card, bg=panel2)
        sp_right.pack(side="right", padx=(8, 0))

        self.btn_sponsor_action = ttk.Button(sp_right, text="Подробнее →", style="Toggle.TButton")
        self.btn_sponsor_action.pack(side="left")

        self.btn_sponsor_close = ttk.Button(sp_right, text="✕", width=2, style="Toggle.TButton",
                                           command=self._dismiss_sponsor)
        self.btn_sponsor_close.pack(side="left", padx=(4, 0))
        self._tt(self.btn_sponsor_close, "Скрыть это предложение")

        self.sponsor_card.grid_remove()

        # -------------------- ПРАВАЯ КОЛОНКА: ПАНЕЛЬ ЧТЕНИЯ И ИНСТРУМЕНТЫ --------------------
        self.text_frame = ttk.Frame(self.body, padding=(16, 10))
        self.body.add(self.text_frame, weight=2)
        self._text_added = True
        self.text_frame.columnconfigure(0, weight=1)
        self.text_frame.rowconfigure(3, weight=1)

        # Верхняя строка: Автор · дата/время + индикатор тишины | Копировать · Сохранить mp3 · Правка · Главное · Инструменты
        text_head = ttk.Frame(self.text_frame)
        text_head.grid(row=0, column=0, sticky="ew")
        text_head.columnconfigure(0, weight=1)

        meta_left = ttk.Frame(text_head)
        meta_left.grid(row=0, column=0, sticky="w")
        self.lbl_now_author = ttk.Label(meta_left, text=vp_i18n.t("empty_hero_hint"), style="Hero.TLabel")
        self.lbl_now_author.pack(side="left")
        self.lbl_now_time = ttk.Label(meta_left, text="", style="Dim.TLabel")
        self.lbl_now_time.pack(side="left", padx=(10, 0))
        self.lbl_dnd = ttk.Label(meta_left, text="", style="Badge.TLabel", cursor="hand2")
        self.lbl_dnd.pack(side="left", padx=(10, 0))
        self.lbl_dnd.bind("<Button-1>", lambda e: self._toggle_dnd_quick())
        self._tt(self.lbl_dnd, "Тишина включена — клик, чтобы снять «не беспокоить»")
        # Скрытый совместимый лейбл тизера (весь текст теперь сразу виден в основной панели чтения)
        self.lbl_now_text = ttk.Label(self.text_frame, text="", style="Now.TLabel")

        text_btns = ttk.Frame(text_head)
        text_btns.grid(row=0, column=1, sticky="e")
        self.btn_copy_text = ttk.Button(text_btns, text=vp_i18n.t("copy"), style="Toggle.TButton",
                                        command=self._copy_full_text)
        self.btn_copy_text.pack(side="left", padx=(0, 5))
        self._tt(self.btn_copy_text, "Скопировать весь текст реплики")
        self.btn_export_mp3_btn = ttk.Button(text_btns, text=vp_i18n.t("save_mp3"), style="Toggle.TButton",
                                            command=self._export_mp3)
        self.btn_export_mp3_btn.pack(side="left", padx=(0, 5))
        self._tt(self.btn_export_mp3_btn, "Сохранить озвучку текущей реплики в mp3")
        self.btn_edit_text = ttk.Button(text_btns, text="✏ " + vp_i18n.t("edit"), style="Toggle.TButton",
                                        command=self._toggle_edit_text)
        self.btn_edit_text.pack(side="left", padx=(0, 5))
        self._tt(self.btn_edit_text, "Редактировать текст; нажми ещё раз — применить правки")
        self.btn_highlight_btn = ttk.Button(text_btns, text="✨ " + vp_i18n.t("highlight"), style="Toggle.TButton",
                                            command=self._highlight_entities)
        self.btn_highlight_btn.pack(side="left", padx=(0, 5))
        self._tt(self.btn_highlight_btn, "Подсветить главное: даты, числа, имена, задачи")
        self.var_tools_open = tk.BooleanVar(value=bool(self.prefs.get("tools_open", False)))
        tools_arrow = " ▾" if self.var_tools_open.get() else " ▸"
        self.btn_tools = ttk.Button(text_btns, text="🛠 " + vp_i18n.t("tools") + tools_arrow, command=self._toggle_tools,
                                    style="Toggle.TButton")
        self.btn_tools.pack(side="left")
        self._tt(self.btn_tools, "Загрузка документов/аудио, запись, архив, плейлисты, словарь, команды")
        self._editing_text = False

        # Выдвижной ящик «Инструменты» (по умолчанию скрыт, раскрывается кнопкой «🛠 Инструменты»)
        self.tools_box = ttk.Frame(self.text_frame, padding=(10, 8))
        self.tools_box.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        self.tools_box.columnconfigure(0, weight=1)

        load_row = _wrap_bar(ttk.Frame(self.tools_box))
        load_row.grid(row=0, column=0, sticky="ew", pady=(2, 0))
        self.btn_tool_doc = ttk.Button(load_row, text="📑 " + vp_i18n.t("tool_doc"), command=self._load_document)
        self.btn_tool_doc.pack(side="left")
        self._tt(self.btn_tool_doc, "Открыть PDF/DOCX/TXT/RTF и озвучить")
        self.btn_tool_audio = ttk.Button(load_row, text="📂 " + vp_i18n.t("tool_audio"), command=self._load_audio_file)
        self.btn_tool_audio.pack(side="left", padx=(5, 0))
        self._tt(self.btn_tool_audio, "Открыть аудиофайл и распознать речь в текст")
        self.btn_tool_folder = ttk.Button(load_row, text="📁 " + vp_i18n.t("tool_folder"), command=self._load_audio_folder)
        self.btn_tool_folder.pack(side="left", padx=(5, 0))
        self._tt(self.btn_tool_folder, "Распознать все аудиофайлы из выбранной папки")
        self.btn_tool_board = ttk.Button(load_row, text=vp_i18n.t("tool_board"), command=self._load_history)
        self.btn_tool_board.pack(side="left", padx=(5, 0))
        self._tt(self.btn_tool_board, "Подтянуть последние 20 сообщений доски")
        self.btn_tool_session = ttk.Button(load_row, text="💾 " + vp_i18n.t("tool_session"), command=self._export_session)
        self.btn_tool_session.pack(side="left", padx=(5, 0))
        self._tt(self.btn_tool_session, "Сохранить весь текст очереди одним файлом")
        self.btn_tool_cache = ttk.Button(load_row, text="📁 " + vp_i18n.t("tool_cache"), command=self._open_audio_cache)
        self.btn_tool_cache.pack(side="left", padx=(5, 0))
        self._tt(self.btn_tool_cache, "Открыть папку с готовыми mp3")

        tools_row = _wrap_bar(ttk.Frame(self.tools_box))
        tools_row.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        self.btn_record = ttk.Button(tools_row, text="🎤 " + vp_i18n.t("tool_record"), width=9, command=self._toggle_record)
        self.btn_record.pack(side="left")
        self._tt(self.btn_record, "Записать с микрофона и распознать в текст")
        self.btn_sysrec = ttk.Button(tools_row, text="🔊 " + vp_i18n.t("tool_sysaudio"), width=9, command=self._toggle_record_system)
        self.btn_sysrec.pack(side="left", padx=(5, 0))
        self._tt(self.btn_sysrec, "Записать системный звук (звонок/видео) и распознать")
        self.btn_live = ttk.Button(tools_row, text="🎙 Live", width=7, command=self._toggle_live)
        self.btn_live.pack(side="left", padx=(5, 0))
        self._tt(self.btn_live, "Распознавание в реальном времени")
        self.btn_voicecmd = ttk.Button(tools_row, text="🎧 " + vp_i18n.t("tool_commands"), width=11, command=self._toggle_voicecmd)
        self.btn_voicecmd.pack(side="left", padx=(5, 0))
        self._tt(self.btn_voicecmd, "Голосовые команды: пауза/дальше/повтори/быстрее…")
        self._tt(self.btn_voicecmd, "Голосовые команды: пауза/дальше/повтори/быстрее…")
        self._icon_button(tools_row, "archive", self._open_archive,
                          "Архив: искать по всем прошлым репликам").pack(side="left", padx=(8, 0))
        self._icon_button(tools_row, "summary", self._summarize_selected,
                          "Пересказ реплики или очереди").pack(side="left", padx=(4, 0))
        self._icon_button(tools_row, "dict", self._edit_pronounce,
                          "Словарь произношения").pack(side="left", padx=(4, 0))
        self._icon_button(tools_row, "playlistsave", self._save_playlist,
                          "Сохранить текущую очередь как плейлист").pack(side="left", padx=(4, 0))
        self._icon_button(tools_row, "playlists", self._open_playlists,
                          "Открыть сохранённый плейлист").pack(side="left", padx=(4, 0))
        self._icon_button(tools_row, "digest", self._daily_digest,
                          "Дайджест за сегодня").pack(side="left", padx=(4, 0))
        self._icon_button(tools_row, "diag", self._diagnostics,
                          "Диагностика: микрофон, движки, устройства").pack(side="left", padx=(4, 0))
        self._icon_button(tools_row, "delete", self.clear_queue,
                          "Очистить очередь (закреплённые остаются)").pack(side="left", padx=(4, 0))
        self._apply_tools_state()

        # Плашка вопроса пользователя (диалог Antigravity / Codex) — видна, когда у ответа есть prompt
        self.prompt_frame = ttk.Frame(self.text_frame, style="Card.TFrame")
        self.prompt_frame.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        self.prompt_frame.columnconfigure(1, weight=1)
        self.prompt_bar = tk.Frame(self.prompt_frame, bg=signal, width=3, bd=0, highlightthickness=0)
        self.prompt_bar.grid(row=0, column=0, sticky="ns")
        self.lbl_now_prompt = ttk.Label(self.prompt_frame, text="", style="Card.TLabel",
                                        wraplength=680, justify="left", padding=(10, 7))
        self.lbl_now_prompt.grid(row=0, column=1, sticky="ew")
        self.prompt_frame.bind("<Configure>", lambda e: self.lbl_now_prompt.configure(wraplength=max(240, e.width - 28)))
        self.prompt_frame.grid_remove()

        # Поле полного текста и таблиц
        text_wrap = ttk.Frame(self.text_frame)
        text_wrap.grid(row=3, column=0, sticky="nsew", pady=(8, 0))
        text_wrap.columnconfigure(0, weight=1)
        text_wrap.rowconfigure(0, weight=1)
        import tkinter.font as _tkfont
        self._text_font_size = int(self.prefs.get("text_font_size", 12))
        self.text_font = _tkfont.Font(family="Segoe UI", size=self._text_font_size)
        self.text_font_bold = _tkfont.Font(family="Segoe UI", size=self._text_font_size, weight="bold")
        self.text_widget = tk.Text(text_wrap, width=38, height=10, wrap="word", relief="flat",
                                    bg=panel, fg=ink, insertbackground=ink,
                                    highlightthickness=0, cursor="hand2", undo=True,
                                    font=self.text_font, padx=14, pady=12, spacing1=2, spacing3=4)
        self.text_widget.grid(row=0, column=0, sticky="nsew")
        text_sb = ttk.Scrollbar(text_wrap, orient="vertical", command=self.text_widget.yview)
        text_sb.grid(row=0, column=1, sticky="ns")
        self.text_widget.configure(yscrollcommand=text_sb.set)
        self.text_widget.tag_configure("speaking", background=sel, foreground=ink)
        self.text_widget.tag_configure("speaking_word", background=signal, foreground=pri_fg)
        self.text_widget.tag_configure("entity", foreground=signal, underline=True)
        self.text_widget.bind("<Button-1>", self._on_text_click)
        self.text_widget.bind("<KeyPress>", self._text_panel_key)
        self.text_widget.bind("<Control-KeyPress>", self._text_panel_ctrl)
        self._bind_context(self.text_widget, self._text_panel_menu)
        self.text_widget.bind("<Control-MouseWheel>",
                               lambda e: self._change_text_font(1 if e.delta > 0 else -1))
        self.text_widget.bind("<Configure>", self._on_text_panel_resize)

        # Нижняя строка статуса правой панели
        status_row = ttk.Frame(self.text_frame)
        status_row.grid(row=4, column=0, sticky="ew", pady=(6, 0))
        self.lbl_status = ttk.Label(status_row, text=vp_i18n.t("status_launching"), style="Dim.TLabel")
        self.lbl_status.pack(side="left")
        self.lbl_hint = ttk.Label(status_row, text=vp_i18n.t("bottom_hint"),
                                  style="Dim.TLabel")
        self.lbl_hint.pack(side="right")

        # контекстное меню
        self._build_context_menu()

        # горячие клавиши
        self.root.bind("<space>",  lambda e: self._space_or_passthrough(e))
        self.root.bind("<Right>",  lambda e: self.action_next())
        self.root.bind("<Left>",   lambda e: self.action_prev())
        self.root.bind("<Control-Right>", lambda e: self._seek_relative(+10))   # +10 сек
        self.root.bind("<Control-Left>",  lambda e: self._seek_relative(-10))   # −10 сек
        self.root.bind("<Shift-Right>", lambda e: (self._jump_sentence(+1) if not isinstance(e.widget, (ttk.Entry, tk.Text)) else None))
        self.root.bind("<Shift-Left>",  lambda e: (self._jump_sentence(-1) if not isinstance(e.widget, (ttk.Entry, tk.Text)) else None))
        self.root.bind("<Alt-Up>",   lambda e: self._move_selected(-1))
        self.root.bind("<Alt-Down>", lambda e: self._move_selected(+1))
        self.root.bind("<KeyPress-r>", lambda e: self.action_repeat() if not isinstance(e.widget, (ttk.Entry, tk.Text)) else None)
        self.root.bind("<Control-f>", lambda e: self._toggle_search())
        self.root.bind("<Escape>",   lambda e: self._on_escape())
        # Расширенные горячие клавиши (F1, Ctrl+,, Ctrl+M, Ctrl+T, Ctrl+L, Ctrl+I, Ctrl+D, Ctrl+E, Ctrl+S, Ctrl+1..4)
        self.root.bind("<F1>", lambda e: self._show_shortcuts_dialog())
        self.root.bind("<Control-h>", lambda e: self._show_shortcuts_dialog())
        self.root.bind("<Control-comma>", lambda e: self._open_settings())
        self.root.bind("<Control-m>", lambda e: self._toggle_dnd_quick())
        self.root.bind("<Control-t>", lambda e: self._toggle_tools())
        self.root.bind("<Control-l>", lambda e: (self.tts_input.delete("1.0", "end"), self.tts_input.configure(height=3)) if hasattr(self, "tts_input") else None)
        self.root.bind("<Control-i>", lambda e: self.tts_input.focus_set() if hasattr(self, "tts_input") else None)
        self.root.bind("<Control-d>", lambda e: self._toggle_dictate())
        self.root.bind("<Control-e>", lambda e: self._toggle_edit_text())
        self.root.bind("<Control-s>", lambda e: self._export_mp3())
        self.root.bind("<Control-Delete>", lambda e: self._delete_current_or_selected())
        self.root.bind("<Control-Shift-L>", lambda e: self._clear_queue())
        self.root.bind("<Control-u>", lambda e: threading.Thread(target=self._check_update, args=(True,), daemon=True, name="update-check").start())
        self.root.bind("<F5>", lambda e: self._pull_recent_ai(limit_per_source=8, silent=False))
        self.root.bind("<Control-Key-1>", lambda e: self._set_rate_pill(1.0))
        self.root.bind("<Control-Key-2>", lambda e: self._set_rate_pill(1.25))
        self.root.bind("<Control-Key-3>", lambda e: self._set_rate_pill(1.5))
        self.root.bind("<Control-Key-4>", lambda e: self._set_rate_pill(2.0))
        self.root.bind("<Control-equal>", lambda e: self._nudge_rate(+0.1))
        self.root.bind("<Control-plus>", lambda e: self._nudge_rate(+0.1))
        self.root.bind("<Control-minus>", lambda e: self._nudge_rate(-0.1))
        self.root.bind("<Control-Key-0>", lambda e: self._set_rate_pill(1.0))
        self.root.bind("<KeyPress-j>", lambda e: self._seek_relative(-5) if not isinstance(e.widget, (ttk.Entry, tk.Text)) else None)
        self.root.bind("<KeyPress-l>", lambda e: self._seek_relative(+5) if not isinstance(e.widget, (ttk.Entry, tk.Text)) else None)
        self.root.bind("<KeyPress-k>", lambda e: self.action_toggle() if not isinstance(e.widget, (ttk.Entry, tk.Text)) else None)
        self.root.bind("<KeyPress-m>", lambda e: self._toggle_dnd_quick() if not isinstance(e.widget, (ttk.Entry, tk.Text)) else None)
        self.root.bind("<F2>", lambda e: self._toggle_edit_text())
        self.root.bind("<Control-n>", lambda e: (self.tts_input.delete("1.0", "end"), self.tts_input.configure(height=3), self.tts_input.focus_set()) if hasattr(self, "tts_input") else None)
        self.root.bind("<Control-Shift-F>", lambda e: self.ent_search.focus_set() if hasattr(self, "ent_search") else None)
        self.root.bind("<Control-Shift-T>", lambda e: self._toggle_theme_quick())
        self.root.bind("<Control-Shift-M>", lambda e: self._toggle_mini_player())
        self.root.bind("<Control-Shift-V>", lambda e: self._preview_voice())
        self.root.bind("<Control-Shift-D>", lambda e: self._diagnostics())
        self.root.bind("<Control-Shift-K>", lambda e: self._copy_diagnostics())

        self._update_filter_pills()
        self._update_rate_pills()
        if not self.var_show_text.get():
            self._hide_text_panel()
        if not self.var_show_queue.get():
            self._hide_queue_panel()
        self._retranslate_ui()
        try:
            self.root.update_idletasks()
        except Exception:
            pass

    def _space_or_passthrough(self, e):
        if isinstance(e.widget, (ttk.Entry, tk.Text)):
            return
        self.action_toggle()
        return "break"

    def _on_escape(self):
        if self.search_frame.winfo_ismapped():
            self._toggle_search()
        if hasattr(self, "filt_frame") and self.filt_frame.winfo_ismapped():
            self.filt_frame.pack_forget()

    def _set_status(self, text: str, ok: bool = True):
        translated = vp_i18n.translate_status(text) if hasattr(vp_i18n, "translate_status") else text
        self.lbl_status.configure(text=translated)

    def _set_source_pill(self, key: str):
        self.var_source.set(key)
        self.var_source_label.set(self._src_labels.get(key, "Все источники"))
        self._update_filter_pills()
        self._save_prefs()
        self._render_list()

    def _update_filter_pills(self):
        cur = self.var_source.get()
        for k, b in getattr(self, "_src_pill_btns", {}).items():
            try:
                b.configure(style=("PillActive.TButton" if k == cur else "Toggle.TButton"))
            except Exception:
                pass

    def _update_rate_pills(self):
        mult = self._speed_mult()
        for val, b in getattr(self, "_rate_preset_btns", {}).items():
            try:
                active = abs(mult - val) < 0.03
                b.configure(style=("PillActive.TButton" if active else "Toggle.TButton"))
            except Exception:
                pass

    def _toggle_extra_filters(self):
        if not hasattr(self, "filt_frame"):
            return
        if self.filt_frame.winfo_ismapped():
            self.filt_frame.pack_forget()
        else:
            self.filt_frame.pack(fill="x", pady=(6, 0))

    def _on_rate_change(self, *_):
        # МГНОВЕННО меняем скорость прямо на играющей речи (time-stretch, тон сохраняется).
        # Тяжёлой работы тут нет: плеер лишь будит свой воркер — UI не зависает.
        mult = self._speed_mult()
        self.lbl_rate.configure(text=f"{mult:.2f}×")
        self._update_rate_pills()
        try:
            self.player.set_speed(mult)
        except Exception:
            pass
        # сохранение настройки откладываем (debounce), чтобы не писать файл на каждый пиксель
        if self._rate_after_id:
            try: self.root.after_cancel(self._rate_after_id)
            except Exception: pass
        self._rate_after_id = self.root.after(400, self._commit_rate)

    def _commit_rate(self):
        self._rate_after_id = None
        # запомнить скорость для текущего источника (если включён режим «своя скорость»)
        if self.var_rate_per_source.get() and 0 <= self.current_idx < len(self.queue_items):
            src = self.queue_items[self.current_idx].source
            self._rate_by_source[src] = round(self._speed_mult(), 3)
        self._save_prefs()

    def _on_source_change(self, _e=None):
        self.var_source.set(self._src_rev.get(self.var_source_label.get(), "all"))
        self._update_filter_pills()
        self._save_prefs()
        self._render_list()

    def _on_model_change(self, _e=None):
        self._save_prefs()
        # прогреем выбранную модель в фоне
        threading.Thread(target=self._preload_whisper, daemon=True, name="whisper-preload").start()

    def _on_sleep_change(self, _e=None):
        v = str(self.var_sleep.get()).strip()
        if any(v.lower().startswith(x) for x in ("выкл", "off", "desactivado", "关闭", "オフ", "aus", "désactivé", "0", "")):
            self._set_sleep_timer(0)
        else:
            digits = "".join(c for c in v if c.isdigit())
            try: self._set_sleep_timer(int(digits) if digits else 0)
            except Exception: self._set_sleep_timer(0)

    def _on_accent_change(self, _e=None):
        color = self._accent_colors.get(self.var_accent.get(), "#7ee2b8")
        self.col_signal = color
        self.prefs["accent"] = color
        try:
            self._apply_theme(self.var_theme.get())
        except Exception:
            pass
        self._save_prefs()

    def _preload_voice(self, voice: str):
        try:
            vp_piper.preload(voice)
        except Exception as e:
            log_line(f"voice preload {voice}: {e}")

    def _on_voice_change(self, _e=None):
        self._voice = VOICES.get(self.var_voice.get(), TTS_VOICE)
        if vp_piper.is_local(self._voice):
            threading.Thread(target=self._preload_voice, args=(self._voice,), daemon=True, name="voice-preload").start()
        self._save_prefs()
        self._set_status(f"голос: {self.var_voice.get()}", ok=True)

    def _preview_voice(self):
        self.enqueue(source="manual", author="Проба голоса", time_str=time.strftime("%H:%M"),
                     text="Привет! Это проба выбранного голоса. Так я буду читать ваши реплики.",
                     dedup=False)

    def _load_audio_folder(self):
        if not _WHISPER_OK:
            messagebox.showerror(APP_NAME, "Движок распознавания не установлен."); return
        d = filedialog.askdirectory(title="Папка с аудиофайлами")
        if not d:
            return
        exts = {".ogg", ".mp3", ".m4a", ".wav", ".flac", ".aac", ".opus", ".wma"}
        n = 0
        for p in sorted(Path(d).iterdir()):
            try:
                if p.is_file() and p.suffix.lower() in exts:
                    self._add_audio_file(p); n += 1
            except Exception:
                pass
        self._set_status(f"папка: {n} файл(ов) на распознавание", ok=(n > 0))

    def _export_session(self):
        items = [it for it in self._visible() if it.text and not it.text.startswith("(")]
        if not items:
            messagebox.showinfo(APP_NAME, "Нечего экспортировать."); return
        fn = filedialog.asksaveasfilename(title="Экспорт всей сессии", defaultextension=".txt",
                                          initialfile="session.txt",
                                          filetypes=[("Текст", "*.txt"), ("Все файлы", "*.*")])
        if not fn:
            return
        lines = []
        for it in items:
            stamp = " ".join(p for p in (it.date_str, it.time_str) if p)
            lines.append(f"[{it.author}{(' · ' + stamp) if stamp else ''}]\n{self._panel_plain_text(it)}\n")
        try:
            Path(fn).write_text("\n".join(lines), encoding="utf-8")
            self._set_status(f"сессия сохранена: {Path(fn).name} ({len(items)} реплик)", ok=True)
        except Exception as e:
            messagebox.showerror(APP_NAME, f"Не удалось сохранить:\n{e}")

    # ------------------ запись с микрофона ------------------
    def _toggle_record(self):
        if not _STREAM_OK:
            messagebox.showerror(APP_NAME, "Аудио-движок не установлен."); return
        if not self._recording:
            try:
                self._recorder.start()
                self._recording = True
                self.btn_record.configure(text="⏺ Стоп")
                self._set_status("идёт запись с микрофона… нажми ещё раз для остановки", ok=True)
            except Exception as e:
                messagebox.showerror(APP_NAME, f"Не удалось начать запись:\n{e}")
        else:
            self._recording = False
            self.btn_record.configure(text="🎤 Запись")
            path = AUDIO_DIR / f"rec_{uuid4().hex[:8]}.wav"
            try:
                ok = self._recorder.stop(path)
            except Exception as e:
                messagebox.showerror(APP_NAME, f"Ошибка записи:\n{e}"); return
            if ok:
                self._add_audio_file(path)
                self._set_status("запись остановлена → распознаю", ok=True)
            else:
                self._set_status("запись пустая", ok=False)

    def _toggle_record_system(self):
        try:
            import soundcard  # noqa
        except Exception:
            messagebox.showerror(APP_NAME, "Нет библиотеки soundcard для захвата системного звука.")
            return
        if not self._recording_sys:
            try:
                self._sysrec.start()
                self._recording_sys = True
                self.btn_sysrec.configure(text="⏺ Стоп ПК")
                self._set_status("записываю системный звук (звонок/видео)… ещё раз — стоп", ok=True)
            except Exception as e:
                messagebox.showerror(APP_NAME, f"Не удалось начать запись системного звука:\n{e}")
        else:
            self._recording_sys = False
            self.btn_sysrec.configure(text="🔊 Звук ПК")
            path = AUDIO_DIR / f"sys_{uuid4().hex[:8]}.wav"
            try:
                ok = self._sysrec.stop(path)
            except Exception as e:
                messagebox.showerror(APP_NAME, f"Ошибка записи:\n{e}"); return
            if ok:
                self._add_audio_file(path)
                self._set_status("запись системного звука → распознаю", ok=True)
            else:
                self._set_status("пусто — проверь, что звук играл; нажми ещё раз и повтори", ok=False)

    # ------------------ live-распознавание ------------------
    def _toggle_live(self):
        if not (_STREAM_OK and _WHISPER_OK):
            messagebox.showerror(APP_NAME, "Нужны аудио-движок и распознавание."); return
        if not self._live:
            try:
                import sounddevice as sd
                with self._live_lock:
                    self._live_frames = []
                self._live = True
                self.btn_live.configure(text="⏺ Стоп live")

                def cb(indata, frames, t, status):
                    if self._live:
                        with self._live_lock:
                            self._live_frames.append(indata.copy())
                self._live_stream = sd.InputStream(samplerate=16000, channels=1,
                                                   dtype="float32", callback=cb)
                self._live_stream.start()
                self._live_item = self.enqueue(source="file", author="🎙 Live",
                                               time_str=time.strftime("%H:%M"),
                                               text="(слушаю…)", dedup=False)
                if self._live_item:
                    self._live_item.is_file = True; self._live_item.transcribing = True
                    self._live_item.audio_ready = False
                threading.Thread(target=self._live_worker, daemon=True, name="live").start()
                self._set_status("live-распознавание включено — говори", ok=True)
            except Exception as e:
                self._live = False
                messagebox.showerror(APP_NAME, f"Не удалось включить live:\n{e}")
        else:
            self._live = False
            self.btn_live.configure(text="🎙 Live")
            try:
                if self._live_stream:
                    self._live_stream.stop(); self._live_stream.close()
            except Exception:
                pass
            self._live_stream = None
            # финальное распознавание + сохранение wav
            self.root.after(100, self._live_finalize)

    def _live_worker(self):
        import numpy as np
        last = 0
        while self._live:
            time.sleep(4.0)
            with self._live_lock:
                if not self._live_frames:
                    continue
                audio = np.concatenate(self._live_frames, axis=0).reshape(-1).astype(np.float32)
            if audio.shape[0] - last < 8000:   # меньше 0.5с нового — ждём
                continue
            last = audio.shape[0]
            try:
                model = _get_whisper(self._model_size)
                segs, _ = model.transcribe(audio, language=None, beam_size=1, vad_filter=True)
                txt = " ".join(s.text.strip() for s in segs).strip()
                if txt and self._live_item is not None:
                    self._live_item.text = txt
                    self.root.after(0, self._request_render)
                    if 0 <= self.current_idx < len(self.queue_items) and \
                            self.queue_items[self.current_idx] is self._live_item:
                        self.root.after(0, self._update_text_panel)
            except Exception as e:
                log_line(f"live transcribe error: {e}")

    def _live_finalize(self):
        import numpy as np
        with self._live_lock:
            frames = list(self._live_frames)
        it = self._live_item
        self._live_item = None
        if not frames or it is None:
            if it is not None:
                it.transcribing = False; it.text = it.text or "(тишина)"; self._render_list()
            return
        path = AUDIO_DIR / f"live_{uuid4().hex[:8]}.wav"
        try:
            _write_wav(frames, path, 16000)
            it.file_path = path; it.audio_path = path; it.audio_ready = True
        except Exception:
            pass
        # финальная точная расшифровка (по словам) в фоне
        def fin():
            try:
                text, marks = transcribe_file(path, self._model_size, word_level=True)
                it.text = text or it.text; it.raw_text = it.text; it.marks = marks
            except Exception as e:
                log_line(f"live finalize error: {e}")
            finally:
                it.transcribing = False
                archive_add(it)
                self.root.after(0, self._request_render)
                self.root.after(0, lambda: self._set_status("live-распознавание завершено", ok=True))
        threading.Thread(target=fin, daemon=True, name="live-fin").start()

    # ------------------ AI-саммари ------------------
    def _summarize_selected(self):
        idx = self._selected_idx()
        if idx is not None and 0 <= idx < len(self.queue_items):
            src_text = self.queue_items[idx].text; title = "Саммари реплики"
        else:
            src_text = "\n".join(it.text for it in self._visible()
                                 if it.text and not it.text.startswith("("))
            title = "Саммари очереди"
        if not src_text or len(src_text) < 40:
            messagebox.showinfo(APP_NAME, "Слишком мало текста для пересказа."); return
        self._set_status("делаю пересказ…", ok=True)
        threading.Thread(target=self._summary_worker, args=(src_text, title), daemon=True).start()

    def _summary_worker(self, text, title):
        s = ai_summary(text)
        self.root.after(0, lambda: self.enqueue(source="manual", author="📝 " + title,
                        time_str=time.strftime("%H:%M"), text=s, dedup=False))
        self.root.after(0, lambda: self._notify("Пересказ готов", title))
        self.root.after(0, lambda: self._set_status("пересказ готов и в очереди", ok=True))

    # ------------------ переименование авторов ------------------
    def _rename_author(self):
        idx = self._selected_idx()
        if idx is None or not (0 <= idx < len(self.queue_items)):
            return
        author = self.queue_items[idx].author          # оригинальное имя (ключ, не меняется)
        cur = self._author_names.get(author, "")
        win = tk.Toplevel(self.root); win.title("Переименовать автора")
        c = self.colors; win.transient(self.root); win.configure(bg=c["bg"]); win.grab_set()
        self._center_over_root(win, 380, 170)
        ttk.Label(win, text=f"Оригинальное имя: {author}", style="Dim.TLabel").pack(anchor="w", padx=14, pady=(14, 2))
        ttk.Label(win, text="Показывать как:", style="TLabel").pack(anchor="w", padx=14, pady=(6, 4))
        var = tk.StringVar(value=cur)
        ent = ttk.Entry(win, textvariable=var); ent.pack(fill="x", padx=14); ent.focus_set()
        ent.icursor("end")

        def save():
            name = var.get().strip()
            if name and name != author:
                self._author_names[author] = name
            else:
                self._author_names.pop(author, None)    # пусто или совпадает с оригиналом → сбросить
            self._save_prefs(); win.destroy()
            self._render_list(); self._update_text_panel()
            self._set_status(f"автор «{author}» → «{name or author}»", ok=True)

        def reset():
            self._author_names.pop(author, None)
            self._save_prefs(); win.destroy()
            self._render_list(); self._update_text_panel()
            self._set_status(f"имя автора «{author}» сброшено", ok=True)

        ent.bind("<Return>", lambda e: save())
        btns = ttk.Frame(win); btns.pack(fill="x", padx=14, pady=14)
        ttk.Button(btns, text="OK", style="Primary.TButton", command=save).pack(side="left")
        ttk.Button(btns, text="Сбросить", command=reset).pack(side="left", padx=(6, 0))
        ttk.Button(btns, text="Отмена", command=win.destroy).pack(side="right")

    def _edit_author_names(self):
        """Редактор всех сохранённых имён авторов (набор переживает перезагрузку)."""
        c = self.colors
        win = tk.Toplevel(self.root); win.title("Имена авторов"); win.configure(bg=c["bg"])
        self._center_over_root(win, 460, max(380, self.root.winfo_height()))
        win.minsize(360, 300)
        ttk.Label(win, text="Каждая строка: оригинал = как показывать\nНапример:  Claude/323b5065 = Семён",
                  style="Dim.TLabel").pack(anchor="w", padx=12, pady=(10, 4))
        txt = tk.Text(win, wrap="word", bg=c["panel"], fg=c["ink"], insertbackground=c["ink"],
                      font=("Segoe UI", 11), padx=8, pady=8, highlightthickness=0,
                      undo=True, autoseparators=True)

        # known = сохранённые + встречающиеся сейчас в очереди (чтобы было что переименовать)
        known = dict(self._author_names)
        for it in self.queue_items:
            known.setdefault(it.author, self._author_names.get(it.author, ""))

        def paste_clip():
            try: s = self.root.clipboard_get()
            except Exception: s = ""
            if s:
                if txt.get("insert linestart", "insert").strip():
                    txt.insert("insert", "\n")
                txt.insert("insert", s)
            txt.focus_set()

        def save():
            d = {}
            for line in txt.get("1.0", "end").splitlines():
                if "=" in line:
                    k, v = line.split("=", 1); k = k.strip(); v = v.strip()
                    if k and v and v != k:
                        d[k] = v
            self._author_names = d; self._save_prefs(); win.destroy()
            self._render_list(); self._update_text_panel()
            self._set_status(f"имена авторов сохранены ({len(d)})", ok=True)

        btns = ttk.Frame(win); btns.pack(side="bottom", fill="x", padx=12, pady=10)
        ttk.Button(btns, text="Сохранить", style="Primary.TButton", command=save).pack(side="left")
        self._tt(ttk.Button(btns, text="📋 Вставить из буфера", command=paste_clip),
                 "Вставить из буфера обмена (или Ctrl+V)").pack(side="left", padx=(6, 0))
        ttk.Button(btns, text="Отмена", command=win.destroy).pack(side="right")

        txt.pack(fill="both", expand=True, padx=12, pady=(0, 4))
        txt.insert("1.0", "\n".join(f"{k} = {v}" for k, v in known.items()))
        txt.bind("<Control-KeyPress>", self._input_ctrl)
        txt.focus_set()

    # ------------------ авто-голос по автору ------------------
    def _assign_author_voice(self):
        idx = self._selected_idx()
        if idx is None or not (0 <= idx < len(self.queue_items)):
            return
        author = self.queue_items[idx].author
        cur = next((k for k, v in VOICES.items() if v == self._author_voices.get(author)), "")
        win = tk.Toplevel(self.root); win.title("Голос для автора")
        c = self.colors; win.transient(self.root); win.configure(bg=c["bg"]); win.grab_set()
        self._center_over_root(win, 360, 150)
        ttk.Label(win, text=f"Голос для «{author}»:", style="TLabel").pack(anchor="w", padx=14, pady=(14, 6))
        var = tk.StringVar(value=cur or list(VOICES)[0])
        cb = ttk.Combobox(win, textvariable=var, state="readonly", values=["(как у всех)"] + list(VOICES.keys()))
        cb.pack(fill="x", padx=14)
        if not cur:
            var.set("(как у всех)")

        def save():
            v = var.get()
            if v == "(как у всех)":
                self._author_voices.pop(author, None)
            else:
                self._author_voices[author] = VOICES.get(v, TTS_VOICE)
            self._save_prefs(); win.destroy()
            self._set_status(f"голос для «{author}»: {v}", ok=True)
        btns = ttk.Frame(win); btns.pack(fill="x", padx=14, pady=12)
        ttk.Button(btns, text="OK", style="Primary.TButton", command=save).pack(side="left")
        ttk.Button(btns, text="Отмена", command=win.destroy).pack(side="left", padx=(6, 0))

    # ------------------ текст → mp3 (аудиокнига) ------------------
    def _export_mp3(self):
        idx = self._selected_idx()
        if idx is not None and 0 <= idx < len(self.queue_items):
            text = self.queue_items[idx].text
        else:
            text = self.tts_input.get("1.0", "end").strip()
        text = _clean_for_tts(text or "")
        if len(text) < 5:
            messagebox.showinfo(APP_NAME, "Выбери реплику или вставь текст в поле — тогда сохраню в mp3.")
            return
        fn = filedialog.asksaveasfilename(title="Сохранить озвучку в mp3", defaultextension=".mp3",
                                          initialfile="audio.mp3", filetypes=[("MP3", "*.mp3")])
        if not fn:
            return
        self._set_status("озвучиваю в mp3…", ok=True)
        threading.Thread(target=self._export_mp3_worker, args=(text, fn), daemon=True).start()

    def _export_mp3_worker(self, text, fn):
        try:
            voice = getattr(self, "_voice", TTS_VOICE)
            target = Path(fn)
            spans = _split_for_tts(text)
            parts = [target.with_suffix(f".p{i:03d}.tmp.mp3") for i in range(len(spans))]
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=TTS_PARALLEL_PARTS) as pool:
                list(pool.map(lambda i: _synthesize_with_retries(text[spans[i][0]:spans[i][1]].strip(), TTS_RATE,
                                                                 parts[i], voice, "export"), range(len(spans))))
            with open(target, "wb") as out:
                for part in parts:
                    out.write(part.read_bytes())
                    part.unlink()
            self.root.after(0, lambda: self._set_status(f"mp3 сохранён: {Path(fn).name}", ok=True))
            self.root.after(0, lambda: self._notify("Готово", "mp3 сохранён"))
        except Exception as e:
            self.root.after(0, lambda: messagebox.showerror(APP_NAME, f"Не удалось:\n{e}"))

    # ------------------ AI-черновик ответа ------------------
    def _ai_reply_selected(self):
        idx = self._selected_idx()
        if idx is None or not (0 <= idx < len(self.queue_items)):
            messagebox.showinfo(APP_NAME, "Выбери реплику, на которую ответить."); return
        text = self.queue_items[idx].text
        self._set_status("готовлю черновик ответа…", ok=True)

        def worker():
            r = ai_reply(text) or "(Claude недоступен)"
            def put():
                self._show_text_panel() if False else None
                self.tts_input.delete("1.0", "end"); self.tts_input.insert("1.0", r)
                self._set_status("черновик ответа в поле — правь и отправляй", ok=True)
            self.root.after(0, put)
        threading.Thread(target=worker, daemon=True).start()

    # ------------------ диктовка ответа голосом ------------------
    def _toggle_dictate(self):
        if not (_STREAM_OK and _WHISPER_OK):
            messagebox.showerror(APP_NAME, "Нужны аудио-движок и распознавание."); return
        if not self._recording and not self._recording_to_field:
            try:
                self._recorder.start()
                self._recording_to_field = True
                self.btn_dictate.configure(text="⏺ Стоп")
                self._set_status("диктуй ответ… ещё раз — стоп", ok=True)
            except Exception as e:
                messagebox.showerror(APP_NAME, f"Не удалось:\n{e}")
        elif self._recording_to_field:
            self._recording_to_field = False
            self.btn_dictate.configure(text="🎤→✍ Диктовать")
            path = AUDIO_DIR / f"dict_{uuid4().hex[:8]}.wav"
            try:
                ok = self._recorder.stop(path)
            except Exception as e:
                messagebox.showerror(APP_NAME, f"Ошибка:\n{e}"); return
            if not ok:
                self._set_status("пусто", ok=False); return
            self._set_status("распознаю продиктованное…", ok=True)

            def worker():
                try:
                    text, _ = transcribe_file(path, self._model_size)
                except Exception as e:
                    log_line(f"dictate transcribe error: {e}")
                    text = ""
                self.root.after(0, lambda: self._dictate_done(text))
            threading.Thread(target=worker, daemon=True).start()

    def _dictate_done(self, text):
        if text:
            cur = self.tts_input.get("1.0", "end").strip()
            self.tts_input.delete("1.0", "end")
            self.tts_input.insert("1.0", (cur + " " + text).strip() if cur else text)
            self._set_status("продиктовано — правь и отправляй", ok=True)
        else:
            self._set_status("не распознал — повтори", ok=False)

    def _field_text(self):
        return self.tts_input.get("1.0", "end").strip()

    def _send_to_board(self):
        t = self._field_text()
        if not t:
            messagebox.showinfo(APP_NAME, "Поле пустое."); return
        if not messagebox.askyesno(APP_NAME, "Отправить на доску команды от имени «Антон»?"):
            return
        if post_to_board(t, "Антон"):
            self._set_status("отправлено на доску", ok=True)
        else:
            messagebox.showerror(APP_NAME, "Не удалось отправить на доску (см. лог).")

    def _send_to_telegram(self):
        t = self._field_text()
        if not t:
            messagebox.showinfo(APP_NAME, "Поле пустое."); return
        chat = self.var_tg_chat.get().strip()
        if not chat:
            chat = self._ask_text("Telegram", "ID чата/группы (например -100123…):") or ""
            if chat:
                self.var_tg_chat.set(chat); self._save_prefs()
        ok, err = post_to_telegram(t, chat)
        if ok:
            self._set_status("отправлено в Telegram", ok=True)
        else:
            messagebox.showerror(APP_NAME, f"Telegram: {err}")

    def _copy_field(self):
        t = self._field_text()
        if t:
            self.root.clipboard_clear(); self.root.clipboard_append(t)
            self._set_status("ответ скопирован", ok=True)

    # ------------------ дайджест за день ------------------
    def _daily_digest(self):
        try:
            with _archive_lock:
                conn = sqlite3.connect(f"file:{ARCHIVE_DB}?mode=ro", uri=True, timeout=2.0)
                midnight = time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d"))
                rows = conn.execute("SELECT author, text FROM items WHERE ts>=? ORDER BY ts",
                                    (midnight,)).fetchall()
                conn.close()
        except Exception:
            rows = []
        if not rows:
            messagebox.showinfo(APP_NAME, "За сегодня в архиве пусто."); return
        text = "\n".join(f"{a}: {t}" for a, t in rows if t)
        self._set_status("делаю дайджест за день…", ok=True)
        threading.Thread(target=self._summary_worker, args=(text, "Дайджест за день"), daemon=True).start()

    # ------------------ слежение за автором ------------------
    def _toggle_watch_author(self):
        idx = self._selected_idx()
        if idx is None or not (0 <= idx < len(self.queue_items)):
            return
        a = self.queue_items[idx].author
        if a in self._watch:
            self._watch.discard(a); self._set_status(f"не слежу за «{a}»", ok=True)
        else:
            self._watch.add(a); self._set_status(f"слежу за «{a}» — уведомлю и проиграю первым", ok=True)
        self._save_prefs(); self._render_list()

    # ------------------ диагностика ------------------
    def _diagnostics(self):
        import shutil
        lines = []
        try:
            import sounddevice as sd
            lines.append(f"Вывод звука: {sd.query_devices(kind='output')['name']}")
            lines.append(f"Микрофон: {sd.query_devices(kind='input')['name']}")
        except Exception as e:
            lines.append(f"Аудио: ошибка ({e})")
        lp = find_loopback_device()
        lines.append(f"Системный звук (Стерео микшер): {'есть' if lp is not None else 'нет (нужен soundcard)'}")
        lines.append(f"Распознавание (whisper): {'OK, модель ' + self.var_model.get() if _WHISPER_OK else 'НЕТ'}")
        try:
            import fastembed  # noqa
            lines.append("Семантический поиск: OK")
        except Exception:
            lines.append("Семантический поиск: НЕТ (fastembed)")
        lines.append(f"AI (Claude CLI): {'OK' if shutil.which('claude') else 'НЕТ'}")
        lines.append(f"Системный трей: {'OK' if self._tray else 'НЕТ'}")
        lines.append(f"Telegram-токен: {'есть' if os.environ.get('TELEGRAM_BOT_TOKEN') else 'нет'}")
        messagebox.showinfo("Диагностика", "\n".join(lines))

    # ------------------ голосовые команды (hands-free) ------------------
    def _toggle_voicecmd(self):
        if not (_STREAM_OK and _WHISPER_OK):
            messagebox.showerror(APP_NAME, "Нужны аудио-движок и распознавание."); return
        if not self._voicecmd:
            try:
                import sounddevice as sd
                self._voicecmd = True
                self.btn_voicecmd.configure(text="🎧 Команды ✓")
                self._vc_frames = []

                def cb(indata, frames, t, status):
                    if self._voicecmd:
                        self._vc_frames.append(indata.copy())
                self._vc_stream = sd.InputStream(samplerate=16000, channels=1, dtype="float32", callback=cb)
                self._vc_stream.start()
                threading.Thread(target=self._vc_worker, daemon=True, name="voicecmd").start()
                self._set_status("голосовые команды: пауза / дальше / назад / повтори / быстрее / медленнее / стоп", ok=True)
            except Exception as e:
                self._voicecmd = False
                messagebox.showerror(APP_NAME, f"Не удалось:\n{e}")
        else:
            self._voicecmd = False
            self.btn_voicecmd.configure(text="🎧 Команды")
            try:
                if self._vc_stream:
                    self._vc_stream.stop(); self._vc_stream.close()
            except Exception:
                pass
            self._vc_stream = None
            self._set_status("голосовые команды выключены", ok=True)

    def _vc_worker(self):
        import numpy as np
        while self._voicecmd:
            time.sleep(1.8)
            frames = self._vc_frames; self._vc_frames = []
            if not frames:
                continue
            audio = np.concatenate(frames, axis=0).reshape(-1).astype(np.float32)
            if audio.shape[0] < 8000:
                continue
            try:
                model = _get_whisper("tiny")
                segs, _ = model.transcribe(audio, language="ru", beam_size=1, vad_filter=True)
                txt = " ".join(s.text.lower() for s in segs)
            except Exception:
                continue
            cmd = None
            if any(w in txt for w in ("пауз", "стоп пауз")): cmd = self.action_toggle
            elif "дальше" in txt or "следующ" in txt or "вперёд" in txt: cmd = self.action_next
            elif "назад" in txt or "предыдущ" in txt: cmd = self.action_prev
            elif "повтор" in txt: cmd = self.action_repeat
            elif "быстрее" in txt: cmd = lambda: self._vc_speed(+0.2)
            elif "медленн" in txt: cmd = lambda: self._vc_speed(-0.2)
            elif "стоп" in txt or "хватит" in txt: cmd = lambda: self.player.stop()
            if cmd:
                self.root.after(0, cmd)
                self.root.after(0, lambda t=txt: self._set_status(f"команда: {t.strip()[:30]}", ok=True))

    def _vc_speed(self, delta):
        v = max(0.5, min(2.0, self.var_rate.get() + delta))
        self.var_rate.set(v); self._on_rate_change()

    def _set_rate_preset(self, v: float):
        """Быстрый пресет темпа: один клик — без точного попадания по ползунку."""
        self.var_rate.set(max(0.5, min(2.0, v)))
        self._on_rate_change()
        self._set_status(f"скорость {v:g}×", ok=True)

    def _nudge_rate(self, delta: float):
        """Плавная регулировка скорости с клавиатуры (Ctrl++ / Ctrl+-)."""
        cur = round(float(self.var_rate.get()) + delta, 2)
        cur = max(0.5, min(2.0, cur))
        self.var_rate.set(cur)
        self._on_rate_change()
        self._set_status(f"{vp_i18n.t('rate')}: {cur:g}×", ok=True)

    def _apply_tools_state(self):
        """Показать/спрятать сворачиваемый ящик «Инструменты»."""
        open_ = self.var_tools_open.get()
        arr = " ▾" if open_ else " ▸"
        self.btn_tools.configure(text=f"🛠 {vp_i18n.t('tools')}{arr}")
        if open_:
            self.tools_box.grid()
        else:
            self.tools_box.grid_remove()

    def _toggle_tools(self):
        self.var_tools_open.set(not self.var_tools_open.get())
        self._apply_tools_state()
        self._save_prefs()

    def _apply_adv_state(self):
        """Показать/спрятать продвинутый ряд (A-B повтор, закладка, перемотка по фразам)."""
        if self.var_adv_open.get():
            self.abrow.grid()
        else:
            self.abrow.grid_remove()

    def _toggle_adv(self):
        self.var_adv_open.set(not self.var_adv_open.get())
        self._apply_adv_state()
        self._save_prefs()

    # ------------------ «не беспокоить» / тихие часы ------------------
    def _in_quiet_hours(self) -> bool:
        if not self.var_quiet.get():
            return False
        try:
            now = time.strftime("%H:%M")
            a, b = self._quiet_from, self._quiet_to
            if a == b:
                return False
            if a < b:
                return a <= now < b          # обычный интервал в пределах суток
            return now >= a or now < b        # интервал через полночь (22:00–08:00)
        except Exception:
            return False

    def _dnd_active(self) -> bool:
        """Тишина: вручную включённое «не беспокоить» ИЛИ попадание в тихие часы.
        Новые реплики при этом всё равно копятся и рендерятся — просто не озвучиваются сами."""
        return self.var_dnd.get() or self._in_quiet_hours()

    def _on_dnd_toggle(self):
        self._save_prefs()
        self._update_dnd_badge()
        self._set_status("не беспокоить: вкл" if self.var_dnd.get() else "не беспокоить: выкл", ok=True)

    def _toggle_dnd_quick(self):
        """Клик по бейджу «тишина»: снять ручное «не беспокоить» (тихие часы — только в Настройках)."""
        if self.var_dnd.get():
            self.var_dnd.set(False)
            self._on_dnd_toggle()
        elif self._in_quiet_hours():
            self._set_status("сейчас тихие часы — выключить можно в Настройках", ok=True)

    def _update_dnd_badge(self):
        """Показать/спрятать индикатор тишины в шапке. Дёшево: меняем виджет только при смене текста."""
        try:
            if self.var_dnd.get():
                txt = "🔕 Не беспокоить"
            elif self._in_quiet_hours():
                txt = "🔕 Тихие часы"
            else:
                txt = ""
            if txt != self._dnd_badge_text:
                self._dnd_badge_text = txt
                self.lbl_dnd.configure(text=txt)
        except Exception:
            pass

    def _commit_quiet_hours(self):
        import re
        def norm(s):
            s = (s or "").strip()
            m = re.fullmatch(r"(\d{1,2})[:.\s]?(\d{2})", s)
            if not m:
                return None
            h, mn = int(m.group(1)), int(m.group(2))
            if h > 23 or mn > 59:
                return None
            return f"{h:02d}:{mn:02d}"
        a = norm(self.var_quiet_from.get()); b = norm(self.var_quiet_to.get())
        if not a or not b:
            self._set_status("время в формате ЧЧ:ММ (например 22:00)", ok=False)
            return
        self._quiet_from, self._quiet_to = a, b
        self.var_quiet_from.set(a); self.var_quiet_to.set(b)
        self._save_prefs()
        self._update_dnd_badge()
        self._set_status(f"тихие часы: с {a} до {b}", ok=True)

    # ------------------ таймер сна (реализация ниже, в _set_sleep_timer/_sleep_fire) ------------------
    def _sleep_remaining_label(self) -> str:
        base = vp_i18n.t("menu_sleep_timer")
        if not self._sleep_deadline:
            return base
        left = int(self._sleep_deadline - time.time())
        if left <= 0:
            return base
        return f"{base} ({vp_i18n.t('left_short')}: {left // 60}:{left % 60:02d})"

    # ------------------ отметить всё прослушанным ------------------
    def _mark_all_played(self):
        n = 0
        for it in self.queue_items:
            if not it.deleted and not it.played:
                it.played = True
                n += 1
        self._render_list()
        try: self._save_session()
        except Exception: pass
        self._set_status(f"отмечено прослушанным: {n}", ok=True)

    # ------------------ память скорости по источнику ------------------
    def _apply_source_rate(self, item):
        """Если включена своя скорость для источника — подставить сохранённую скорость
        этого источника на слайдер перед запуском (бот можно слушать быстрее команды)."""
        if not self.var_rate_per_source.get():
            return
        r = self._rate_by_source.get(item.source)
        if r is None:
            return
        try:
            r = max(0.5, min(2.0, float(r)))
        except Exception:
            return
        if abs(self.var_rate.get() - r) > 0.001:
            self.var_rate.set(r)
            self.lbl_rate.configure(text=f"{r:.2f}×")

    def _highlight_entities(self):
        if not self.var_show_text.get() or getattr(self, "_panel_item", None) is None:
            return
        it = self._panel_item
        try:
            self.text_widget.tag_remove("entity", "1.0", "end")
            for a, b in extract_entities(it.text):
                self._panel_tag_span("entity", a, b)
            self._set_status("суть выделена (даты, числа, имена, задачи)", ok=True)
        except Exception:
            pass

    # ------------------ словарь произношения ------------------
    def _load_pronounce(self):
        global _PRONOUNCE
        try:
            _PRONOUNCE = json.loads(PRONOUNCE_FILE.read_text(encoding="utf-8"))
        except Exception:
            _PRONOUNCE = {"ЛМК": "эл-эм-ка", "ОДК": "о-дэ-ка", "ГПР": "гэ-пэ-эр", "КП": "ка-пэ"}
            self._save_pronounce()

    def _save_pronounce(self):
        try:
            PRONOUNCE_FILE.write_text(json.dumps(_PRONOUNCE, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
        except Exception:
            pass

    def _edit_pronounce(self):
        c = self.colors
        win = tk.Toplevel(self.root); win.title("Словарь произношения")
        win.transient(self.root); win.configure(bg=c["bg"])
        self._center_over_root(win, 460, max(420, self.root.winfo_height()))
        win.minsize(380, 320)

        ttk.Label(win, text="Каждая строка: слово = как читать\nНапример:  ЛМК = эл-эм-ка",
                  style="Dim.TLabel").pack(anchor="w", padx=12, pady=(10, 4))

        txt = tk.Text(win, wrap="word", bg=c["panel"], fg=c["ink"], insertbackground=c["ink"],
                      font=("Segoe UI", 11), padx=8, pady=8, highlightthickness=0,
                      undo=True, autoseparators=True)

        def paste_clip():
            try: s = self.root.clipboard_get()
            except Exception: s = ""
            if s:
                if txt.get("insert linestart", "insert").strip():
                    txt.insert("insert", "\n")          # с новой строки, если строка не пуста
                txt.insert("insert", s)
            txt.focus_set()

        def save():
            global _PRONOUNCE
            d = {}
            for line in txt.get("1.0", "end").splitlines():
                if "=" in line:
                    k, v = line.split("=", 1); k = k.strip(); v = v.strip()
                    if k:
                        d[k] = v
            _PRONOUNCE = d; self._save_pronounce(); win.destroy()
            self._set_status(f"словарь произношения сохранён ({len(d)} слов)", ok=True)

        # кнопки закрепляем СНИЗУ ПЕРВЫМИ — тогда они всегда видны, а текст занимает остаток
        btns = ttk.Frame(win); btns.pack(side="bottom", fill="x", padx=12, pady=10)
        ttk.Button(btns, text="Сохранить", style="Primary.TButton", command=save).pack(side="left")
        self._tt(ttk.Button(btns, text="📋 Вставить из буфера", command=paste_clip),
                 "Вставить текст из буфера обмена (или Ctrl+V)").pack(side="left", padx=(6, 0))
        ttk.Button(btns, text="Отмена", command=win.destroy).pack(side="right")

        txt.pack(fill="both", expand=True, padx=12, pady=(0, 4))
        txt.insert("1.0", "\n".join(f"{k} = {v}" for k, v in _PRONOUNCE.items()))
        txt.bind("<Control-KeyPress>", self._input_ctrl)   # Ctrl+A/C/V/X/Z (любая раскладка)
        txt.focus_set()

    # ------------------ архив (поиск/повторное открытие) ------------------
    def _open_archive(self):
        c = self.colors
        win = tk.Toplevel(self.root); win.title("Архив реплик")
        win.transient(self.root); win.configure(bg=c["bg"]); self._center_over_root(win, 760, 480)
        top = ttk.Frame(win); top.pack(fill="x", padx=10, pady=8)
        ttk.Label(top, text="Поиск:", style="Dim.TLabel").pack(side="left")
        q = tk.StringVar(); ent = ttk.Entry(top, textvariable=q)
        ent.pack(side="left", fill="x", expand=True, padx=(6, 6))
        sem = tk.BooleanVar(value=False)
        tree = ttk.Treeview(win, columns=("a", "d", "t"), show="headings", selectmode="browse")
        tree.heading("a", text="Автор"); tree.heading("d", text="Когда"); tree.heading("t", text="Текст")
        tree.column("a", width=130, stretch=False); tree.column("d", width=120, stretch=False)
        tree.column("t", width=480)
        tree.pack(fill="both", expand=True, padx=10, pady=(0, 6))
        data = []

        def fill(rows):
            for r in tree.get_children():
                tree.delete(r)
            data.clear()
            for (a, ds, ts, text, source) in rows:
                data.append(text)
                tree.insert("", "end", values=(a, f"{ds} {ts}", (text or "")[:140]))

        def refresh(*_):
            if sem.get() and q.get().strip():
                self._set_status("семантический поиск… (модель грузится при первом запуске)", ok=True)

                def worker():
                    rows = archive_semantic_search(q.get())
                    self.root.after(0, lambda: fill(rows))
                    self.root.after(0, lambda: self._set_status(f"найдено по смыслу: {len(rows)}", ok=True))
                threading.Thread(target=worker, daemon=True).start()
            else:
                fill(archive_search(q.get()))

        def reopen(_e=None):
            sel = tree.selection()
            if not sel:
                return
            i = tree.index(sel[0])
            if 0 <= i < len(data):
                self.enqueue(source="manual", author="Из архива",
                             time_str=time.strftime("%H:%M"), text=data[i], dedup=False)
                self._set_status("реплика из архива добавлена в очередь", ok=True)
        ttk.Button(top, text="Найти", command=refresh).pack(side="left")
        ttk.Checkbutton(top, text="по смыслу", variable=sem).pack(side="left", padx=(6, 0))
        ent.bind("<Return>", refresh)
        tree.bind("<Double-Button-1>", reopen)
        self._bind_context(tree, reopen)
        ttk.Label(win, text="Двойной клик по строке — добавить в очередь · «по смыслу» = семантический поиск",
                  style="Dim.TLabel").pack(anchor="w", padx=10, pady=(0, 8))
        refresh()

    # ------------------ плейлисты (именованные очереди) ------------------
    def _open_settings(self, select_tab: str = ""):
        c = self.colors
        win = tk.Toplevel(self.root); win.title(vp_i18n.t("settings"))

        tab_titles = [
            vp_i18n.t("tab_playback"),
            vp_i18n.t("tab_recognition"),
            vp_i18n.t("tab_appearance"),
            vp_i18n.t("tab_shortcuts"),
            vp_i18n.t("tab_team"),
            vp_i18n.t("tab_ai_tab"),
            vp_i18n.t("tab_updates"),
        ]
        needed_w = sum(len(t) * 9 + 32 for t in tab_titles) + 60
        win_w = max(940, min(1200, needed_w + 60))
        win_h = 580
        win.transient(self.root); win.configure(bg=c["bg"]); self._center_over_root(win, win_w, win_h)
        win.minsize(940, 560)
        win.resizable(True, True)

        style = ttk.Style()
        style.configure("TNotebook", background=c["bg"], borderwidth=0)
        style.configure("TNotebook.Tab", background=c["panel2"], foreground=c["ink"], padding=(10, 5), font=("Segoe UI", 9))
        style.map("TNotebook.Tab", background=[("selected", c["panel"])], foreground=[("selected", c["ink"])])
        nb = ttk.Notebook(win); nb.pack(fill="both", expand=True, padx=10, pady=10)

        def tab(title):
            f = ttk.Frame(nb, padding=12); nb.add(f, text=title)
            f.columnconfigure(1, weight=1); return f

        # — Воспроизведение —
        t1 = tab(vp_i18n.t("tab_playback"))
        ttk.Checkbutton(t1, text=vp_i18n.t("opt_autoplay"), variable=self.var_autoplay,
                        command=self._save_prefs).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(t1, text=vp_i18n.t("opt_autoscroll"), variable=self.var_autoscroll,
                        command=self._save_prefs).grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Checkbutton(t1, text=vp_i18n.t("opt_autoclip"), variable=self.var_autoclip,
                        command=self._on_autoclip).grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Checkbutton(t1, text=vp_i18n.t("opt_speak_author"), variable=self.var_speak_author,
                        command=self._on_speak_author).grid(row=3, column=0, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Checkbutton(t1, text=vp_i18n.t("opt_rate_per_source"), variable=self.var_rate_per_source,
                        command=self._save_prefs).grid(row=4, column=0, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Checkbutton(t1, text=vp_i18n.t("opt_newest_first"), variable=self.var_newest_first,
                        command=lambda: (self._save_prefs(), self._render_list())).grid(
            row=50, column=0, columnspan=2, sticky="w", pady=(10, 0))
        ttk.Label(t1, text=vp_i18n.t("opt_sleep_timer")).grid(row=5, column=0, sticky="w", pady=(10, 0))
        off_lbl = vp_i18n.t("off")
        m_lbl = vp_i18n.t("min_short")
        cur_slp = str(self.var_sleep.get()).strip()
        if any(cur_slp.lower().startswith(x) for x in ("выкл", "off", "desactivado", "关闭", "बंद", "オフ", "aus", "désactivé", "0", "")):
            self.var_sleep.set(off_lbl)
        else:
            digits = "".join(c for c in cur_slp if c.isdigit())
            if digits:
                self.var_sleep.set(f"{digits} {m_lbl}")
            else:
                self.var_sleep.set(off_lbl)
        slp_opts = [off_lbl, f"15 {m_lbl}", f"30 {m_lbl}", f"45 {m_lbl}", f"60 {m_lbl}", f"90 {m_lbl}"]
        slp = ttk.Combobox(t1, textvariable=self.var_sleep, state="readonly", width=12, values=slp_opts)
        slp.grid(row=5, column=1, sticky="ew", pady=(10, 0)); slp.bind("<<ComboboxSelected>>", self._on_sleep_change)
        # тихие часы: не беспокоить + интервал «с … до …»
        ttk.Checkbutton(t1, text=vp_i18n.t("opt_dnd"), variable=self.var_dnd,
                        command=self._on_dnd_toggle).grid(row=6, column=0, columnspan=2, sticky="w", pady=(10, 0))
        ttk.Checkbutton(t1, text=vp_i18n.t("opt_quiet_hours"), variable=self.var_quiet,
                        command=self._save_prefs).grid(row=7, column=0, columnspan=2, sticky="w", pady=(4, 0))
        qrow = ttk.Frame(t1); qrow.grid(row=8, column=0, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Label(qrow, text=vp_i18n.t("from")).pack(side="left")
        self.var_quiet_from = tk.StringVar(value=self._quiet_from)
        self.var_quiet_to = tk.StringVar(value=self._quiet_to)
        ttk.Entry(qrow, textvariable=self.var_quiet_from, width=7).pack(side="left", padx=4)
        ttk.Label(qrow, text=vp_i18n.t("to")).pack(side="left")
        ttk.Entry(qrow, textvariable=self.var_quiet_to, width=7).pack(side="left", padx=4)
        ttk.Button(qrow, text="OK", width=4, style="Toggle.TButton",
                   command=self._commit_quiet_hours).pack(side="left", padx=4)
        ttk.Label(t1, text=vp_i18n.t("quiet_hint"),
                  style="Dim.TLabel").grid(row=9, column=0, columnspan=2, sticky="w", pady=(4, 0))

        # — Распознавание —
        t2 = tab(vp_i18n.t("tab_recognition"))
        ttk.Label(t2, text=vp_i18n.t("model")).grid(row=0, column=0, sticky="w")
        mdl = ttk.Combobox(t2, textvariable=self.var_model, state="readonly", width=12,
                           values=["tiny", "base", "small", "medium"])
        mdl.grid(row=0, column=1, sticky="ew"); mdl.bind("<<ComboboxSelected>>", self._on_model_change)
        ttk.Checkbutton(t2, text=vp_i18n.t("translate_to_en"), variable=self.var_translate,
                        command=self._save_prefs).grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Label(t2, text=vp_i18n.t("whisper_hint"),
                  style="Dim.TLabel").grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))

        # — Оформление —
        t3 = tab(vp_i18n.t("tab_appearance"))
        ttk.Label(t3, text=vp_i18n.t("ui_language")).grid(row=0, column=0, sticky="w")
        lang_names = [f"{code} — {name}" for code, name in vp_i18n.SUPPORTED_LANGUAGES.items()]
        cur_lang_code = self.var_lang.get()
        cur_lang_display = f"{cur_lang_code} — {vp_i18n.SUPPORTED_LANGUAGES.get(cur_lang_code, 'English')}"
        self.var_lang_display = tk.StringVar(value=cur_lang_display)
        cmb_lang = ttk.Combobox(t3, textvariable=self.var_lang_display, state="readonly", width=18,
                                values=lang_names)
        cmb_lang.grid(row=0, column=1, sticky="ew")

        lbl_lang_applied = ttk.Label(t3, text="", style="Signal.TLabel")
        lbl_lang_applied.grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 6))

        def _apply_lang_action():
            val = self.var_lang_display.get()
            code = val.split(" ")[0].strip()
            self.var_lang.set(code)
            vp_i18n.set_lang(code)
            self._save_prefs()
            self._retranslate_ui()
            try:
                win.destroy()
            except Exception:
                pass
            self.root.after(20, lambda: self._open_settings(select_tab="appearance"))

        btn_lang_ok = ttk.Button(t3, text="✓ " + vp_i18n.t("apply_lang"), style="Primary.TButton",
                                 command=_apply_lang_action)
        btn_lang_ok.grid(row=0, column=2, padx=(8, 0))

        ttk.Label(t3, text=vp_i18n.t("theme")).grid(row=2, column=0, sticky="w", pady=(8, 0))
        th_dark = vp_i18n.t("theme_dark")
        th_light = vp_i18n.t("theme_light")
        cur_th = th_dark if str(self.var_theme.get()).lower().startswith(("т", "d", "o", "s")) else th_light
        self.var_theme_display = tk.StringVar(value=cur_th)
        thm = ttk.Combobox(t3, textvariable=self.var_theme_display, state="readonly", width=12,
                           values=[th_dark, th_light])
        def _on_thm_sel(_e=None):
            val = self.var_theme_display.get()
            chosen = "Тёмная" if val == th_dark else "Светлая"
            self.var_theme.set(chosen)
            self._apply_theme(chosen)
        thm.grid(row=2, column=1, sticky="ew", pady=(8, 0)); thm.bind("<<ComboboxSelected>>", _on_thm_sel)

        ttk.Label(t3, text=vp_i18n.t("accent_color")).grid(row=3, column=0, sticky="w", pady=(8, 0))
        accent_keys = [
            ("Мята", vp_i18n.t("accent_mint")),
            ("Красный", vp_i18n.t("accent_red")),
            ("Синий", vp_i18n.t("accent_blue")),
            ("Графит", vp_i18n.t("accent_graphite")),
            ("Зелёный", vp_i18n.t("accent_green")),
            ("Фиолетовый", vp_i18n.t("accent_violet")),
        ]
        cur_acc_internal = self.var_accent.get()
        cur_acc_display = next((d for k, d in accent_keys if k == cur_acc_internal), cur_acc_internal)
        self.var_accent_display = tk.StringVar(value=cur_acc_display)
        acc_values = [d for _, d in accent_keys]
        acc = ttk.Combobox(t3, textvariable=self.var_accent_display, state="readonly", width=12,
                           values=acc_values)
        def _on_acc_sel(_e=None):
            val = self.var_accent_display.get()
            orig = next((k for k, d in accent_keys if d == val), val)
            self.var_accent.set(orig)
            self._on_accent_change()
        acc.grid(row=3, column=1, sticky="ew", pady=(8, 0)); acc.bind("<<ComboboxSelected>>", _on_acc_sel)
        ttk.Label(t3, text=vp_i18n.t("font_hint"),
                  style="Dim.TLabel").grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 0))

        # — Горячие клавиши —
        t_shortcuts = tab(vp_i18n.t("tab_shortcuts"))
        ttk.Label(t_shortcuts, text=vp_i18n.t("global_shortcuts_title"), font=("Segoe UI Semibold", 9)).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))

        glob_box = ttk.Frame(t_shortcuts, style="Panel.TFrame")
        glob_box.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        glob_box.columnconfigure(1, weight=1)

        is_mac = sys.platform == "darwin"
        global_key1 = "Cmd + Shift + S" if is_mac else "Ctrl + Alt + S"
        global_key2 = "Cmd + Shift + P" if is_mac else "Ctrl + Alt + P"

        tk.Label(glob_box, text=global_key1, font=("Segoe UI Semibold", 9), bg=c["panel2"], fg=c["signal"],
                 padx=8, pady=3, relief="flat", bd=0).grid(row=0, column=0, padx=(0, 8), pady=2, sticky="w")
        ttk.Label(glob_box, text=vp_i18n.t("speak_selection_hint")).grid(row=0, column=1, sticky="w")

        tk.Label(glob_box, text=global_key2, font=("Segoe UI Semibold", 9), bg=c["panel2"], fg=c["signal"],
                 padx=8, pady=3, relief="flat", bd=0).grid(row=1, column=0, padx=(0, 8), pady=2, sticky="w")
        ttk.Label(glob_box, text=f"{vp_i18n.t('play')} / {vp_i18n.t('pause')} ({vp_i18n.t('mode')})").grid(row=1, column=1, sticky="w")

        quick_keys = [
            ("Space / K", f"{vp_i18n.t('play')} / {vp_i18n.t('pause')}"),
            ("Ctrl + Enter", vp_i18n.t("speak_btn")),
            ("→  /  ←", f"{vp_i18n.t('next')} / {vp_i18n.t('prev')}"),
            ("Ctrl + → / ←", "±10s Seek"),
            ("Ctrl + 1 / 2 / 3 / 4", "1.0× / 1.25× / 1.5× / 2.0× Speed"),
            ("Ctrl + ,", vp_i18n.t("settings")),
            ("Ctrl + M", vp_i18n.t("opt_dnd")),
            ("Ctrl + T", vp_i18n.t("tools")),
            ("Ctrl + L", vp_i18n.t("reset")),
            ("Ctrl + D", vp_i18n.t("dictate")),
            ("Ctrl + Shift + T", f"{vp_i18n.t('theme')} (Dark / Light)"),
            ("Ctrl + Shift + M", vp_i18n.t("mini_player")),
        ]
        q_frame = ttk.Frame(t_shortcuts)
        q_frame.grid(row=2, column=0, columnspan=2, sticky="nsew", pady=(0, 10))
        q_frame.columnconfigure(0, minsize=140)
        q_frame.columnconfigure(1, weight=1)

        for ri, (combo, action_name) in enumerate(quick_keys):
            r = ri // 2
            col_offset = (ri % 2) * 2
            tk.Label(q_frame, text=combo, font=("Segoe UI Semibold", 8), bg=c["panel2"], fg=c["signal"],
                     padx=6, pady=2, relief="flat", bd=0).grid(row=r, column=col_offset, padx=(0, 6), pady=2, sticky="w")
            ttk.Label(q_frame, text=action_name, font=("Segoe UI", 9)).grid(row=r, column=col_offset+1, padx=(0, 14), pady=2, sticky="w")

        ttk.Button(t_shortcuts, text=vp_i18n.t("open_shortcuts_dialog"), style="Toggle.TButton",
                   command=self._show_shortcuts_dialog).grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))

        # — Команда —
        t4 = tab(vp_i18n.t("tab_team"))
        ttk.Label(t4, text=vp_i18n.t("tg_chat_hint")).grid(row=0, column=0, sticky="w")
        ttk.Entry(t4, textvariable=self.var_tg_chat).grid(row=0, column=1, sticky="ew")
        ttk.Button(t4, text=vp_i18n.t("dict_btn"), command=self._edit_pronounce).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(12, 0))
        ttk.Button(t4, text=vp_i18n.t("authors_btn"), command=self._edit_author_names).grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Button(t4, text=vp_i18n.t("diag_btn"), command=self._diagnostics).grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))

        # — Нейросети —
        t5 = tab(vp_i18n.t("tab_ai_tab"))
        t5.columnconfigure(1, weight=1)
        self._build_ai_tab(t5)
        t6 = tab(vp_i18n.t("tab_updates"))
        self._build_update_tab(t6)
        if select_tab in ("Нейросети", "ai", vp_i18n.t("tab_ai_tab")):
            nb.select(t5)
        elif select_tab in ("Оформление", "appearance", vp_i18n.t("tab_appearance")):
            nb.select(t3)
        elif select_tab in ("Горячие клавиши", "shortcuts", "hotkeys", vp_i18n.t("tab_shortcuts")):
            nb.select(t_shortcuts)

        ttk.Button(win, text=vp_i18n.t("close"), style="Primary.TButton",
                   command=lambda: (self._save_prefs(), win.destroy())).pack(pady=(0, 10))

    def _save_playlist(self):
        items = [it for it in self._visible() if it.text and not it.text.startswith("(")]
        if not items:
            messagebox.showinfo(APP_NAME, "Очередь пуста — нечего сохранять."); return
        name = self._ask_text("Сохранить плейлист", "Название плейлиста:")
        if not name:
            return
        pl = playlists_load()
        pl[name] = [{"author": it.author, "source": it.source, "text": it.raw_text,
                     "time_str": it.time_str, "date_str": it.date_str} for it in items]
        playlists_save(pl)
        self._set_status(f"плейлист «{name}» сохранён ({len(items)} реплик)", ok=True)

    def _open_playlists(self):
        c = self.colors
        pl = playlists_load()
        win = tk.Toplevel(self.root); win.title("Плейлисты")
        win.transient(self.root); win.configure(bg=c["bg"]); self._center_over_root(win, 360, 380)
        ttk.Label(win, text="Сохранённые очереди:", style="Dim.TLabel").pack(anchor="w", padx=12, pady=(10, 4))
        lb = tk.Listbox(win, bg=c["panel"], fg=c["ink"], highlightthickness=0, bd=0,
                        font=("Segoe UI", 11), selectbackground=c["sel"], selectforeground=c["ink"])
        lb.pack(fill="both", expand=True, padx=12)
        names = list(pl.keys())
        for n in names:
            lb.insert("end", f"{n}  ({len(pl[n])})")

        def load():
            sel = lb.curselection()
            if not sel:
                return
            name = names[sel[0]]
            was = self.var_autoplay.get(); self.var_autoplay.set(False)
            try:
                for rec in pl[name]:
                    self.enqueue(source=rec.get("source") or "manual", author=rec.get("author") or "—",
                                 time_str=rec.get("time_str") or "", date_str=rec.get("date_str") or "",
                                 text=rec.get("text") or "", dedup=False)
            finally:
                self.var_autoplay.set(was)
            win.destroy()
            self._set_status(f"плейлист «{name}» загружен в очередь", ok=True)

        def delete():
            sel = lb.curselection()
            if not sel:
                return
            name = names[sel[0]]
            pl.pop(name, None); playlists_save(pl)
            names.pop(sel[0]); lb.delete(sel[0])
        btns = ttk.Frame(win); btns.pack(fill="x", padx=12, pady=10)
        ttk.Button(btns, text="Загрузить", style="Primary.TButton", command=load).pack(side="left")
        ttk.Button(btns, text="Удалить", command=delete).pack(side="left", padx=(6, 0))
        ttk.Button(btns, text="Закрыть", command=win.destroy).pack(side="left", padx=(6, 0))

    def _ask_text(self, title, prompt):
        c = self.colors
        win = tk.Toplevel(self.root); win.title(title)
        win.transient(self.root); win.grab_set(); win.configure(bg=c["bg"]); self._center_over_root(win, 340, 140)
        ttk.Label(win, text=prompt, style="TLabel").pack(anchor="w", padx=14, pady=(14, 6))
        var = tk.StringVar()
        e = ttk.Entry(win, textvariable=var); e.pack(fill="x", padx=14); e.focus_set()
        res = {"v": None}

        def ok():
            res["v"] = var.get().strip(); win.destroy()
        e.bind("<Return>", lambda ev: ok())
        btns = ttk.Frame(win); btns.pack(fill="x", padx=14, pady=12)
        ttk.Button(btns, text="OK", style="Primary.TButton", command=ok).pack(side="left")
        ttk.Button(btns, text="Отмена", command=win.destroy).pack(side="left", padx=(6, 0))
        self.root.wait_window(win)
        return res["v"]

    def _api_transcribe(self, src: str):
        try:
            if src.startswith("http"):
                import urllib.request
                ext = Path(src.split("?")[0]).suffix or ".mp3"
                dst = AUDIO_DIR / f"api_{uuid4().hex[:8]}{ext}"
                urllib.request.urlretrieve(src, dst)
                self._add_audio_file(dst)
            else:
                p = Path(src)
                if p.exists():
                    self._add_audio_file(p)
        except Exception as e:
            log_line(f"api transcribe error: {e}")

    def _on_speak_author(self):
        # копируем флаг в обычный атрибут — фоновый поток рендера читает его, не tk-переменную
        self._speak_author_on = self.var_speak_author.get()
        self._save_prefs()
        self._set_status("автор будет объявляться голосом" if self._speak_author_on
                         else "объявление автора выключено", ok=True)

    def _on_autoclip(self):
        if self.var_autoclip.get():
            try: self._last_clip = (self.root.clipboard_get() or "").strip()
            except Exception: self._last_clip = ""
            self._set_status("авто-озвучка буфера включена — копируй текст", ok=True)
        else:
            self._set_status("авто-озвучка буфера выключена", ok=True)

    def _poll_clipboard(self):
        if not self.var_autoclip.get():
            return
        try: clip = self.root.clipboard_get()
        except Exception: return
        clip = (clip or "").strip()
        if clip and clip != self._last_clip and len(clip) > 1 and not clip.startswith("http"):
            self._last_clip = clip
            self.enqueue(source="manual", author="Буфер обмена",
                         time_str=time.strftime("%H:%M"), text=clip, dedup=False)

    def _toggle_mute_author(self):
        idx = self._selected_idx()
        if idx is None or not (0 <= idx < len(self.queue_items)):
            return
        author = self.queue_items[idx].author
        if author in self._muted:
            self._muted.discard(author)
            self._set_status(f"снова озвучиваю: {author}", ok=True)
        else:
            self._muted.add(author)
            self._set_status(f"больше не озвучиваю: {author}", ok=True)
        self._save_prefs()
        self._render_list()

    def _on_vol_change(self, *_):
        v = self.var_volume.get()
        self.lbl_vol.configure(text=f"{int(v*100)}%")
        try: self.player.set_volume(v)
        except Exception: pass
        self._save_prefs()

    # ------------------ дорожка перемотки (кликабельная полоса) ------------------
    @staticmethod
    def _fmt_time(s: float) -> str:
        s = int(max(0, s)); return f"{s // 60}:{s % 60:02d}"

    def _is_item_rendering(self, item: Optional[Item]) -> bool:
        if item is None or item.deleted:
            return False
        return bool((not item.audio_ready and not item.audio_error) or item.more_parts or item.transcribing)

    def _any_rendering(self) -> bool:
        if self._rendering_item_id:
            return True
        return any(self._is_item_rendering(it) for it in self.queue_items)

    def _active_seek_item(self) -> Optional[Item]:
        if 0 <= self.current_idx < len(self.queue_items):
            return self.queue_items[self.current_idx]
        p = getattr(self, "_panel_item", None)
        if p is not None and not p.deleted:
            return p
        if self._rendering_item_id:
            for it in self.queue_items:
                if it.id == self._rendering_item_id and not it.deleted:
                    return it
        return None

    def _draw_wave_slider_strip(self, canvas: tk.Canvas, w: int, h: int, phase: float,
                                bg_col: str, hairline_col: str, signal_col: str,
                                *, active: bool = True, x_start: int = 0):
        """Волнообразная анимация-ползунок вдоль длины трека во время рендера."""
        import math
        if w - x_start <= 8:
            return
        span = w - x_start
        yc = h / 2.0
        # Базовая тонкая подложка по всей длине
        canvas.create_line(x_start + 2, yc, w - 2, yc, fill=hairline_col, width=max(1, h // 3), capstyle="round")
        # Плавное возвратно-поступательное скольжение гребня волны вдоль трека (0.08 .. 0.92)
        crest = 0.5 + 0.42 * math.sin(phase * 1.65)
        sigma = 0.24 if active else 0.18
        sig_mid = self._shade(signal_col, -0.25)
        sig_dim = self._shade(signal_col, -0.48)
        # Вертикальные микро-столбики волны + плавная синусоида по длине трека
        step = 4 if h <= 6 else 3
        pts = []
        max_amp = max(1.6, (h - 1.5) * 0.48)
        for x in range(x_start + 2, w - 2, step):
            nx = (x - x_start) / max(1.0, float(span))
            dist = (nx - crest) / sigma
            env = math.exp(-0.5 * dist * dist)
            osc = math.sin(nx * 20.0 - phase * 5.2) * 0.65 + math.cos(nx * 9.0 + phase * 3.1) * 0.35
            wy = yc + osc * max_amp * (0.25 + 0.75 * env)
            pts.extend((x, wy))
            if h >= 6 and env > 0.12:
                bh = max(1.8, (h - 2) * env * (0.38 + 0.62 * abs(osc)))
                col = signal_col if env > 0.62 else (sig_mid if env > 0.30 else sig_dim)
                canvas.create_line(x, yc - bh / 2, x, yc + bh / 2, fill=col, width=1.5 if h <= 8 else 2, capstyle="round")
        # Непрерывная бегущая волна-ползунок поверх трека
        if len(pts) >= 4:
            canvas.create_line(*pts, fill=(sig_mid if active else sig_dim), width=1.5 if h <= 6 else 2,
                               smooth=True, capstyle="round")
        # Яркий центральный волновой пакет («ползунок») вокруг гребня
        win_pts = []
        x0 = max(x_start + 2, int(x_start + (crest - sigma * 1.15) * span))
        x1 = min(w - 2, int(x_start + (crest + sigma * 1.15) * span))
        for x in range(x0, x1 + 1, 3):
            nx = (x - x_start) / max(1.0, float(span))
            dist = (nx - crest) / sigma
            env = math.exp(-0.5 * dist * dist)
            osc = math.sin(nx * 22.0 - phase * 5.2)
            wy = yc + osc * max_amp * env
            win_pts.extend((x, wy))
        if len(win_pts) >= 4:
            canvas.create_line(*win_pts, fill=(signal_col if active else sig_mid),
                               width=2.2 if h <= 6 else 2.6, smooth=True, capstyle="round")

    def _schedule_wave_anim(self):
        if self._wave_anim_after is not None:
            return
        if self._any_rendering() and not self.stop_event.is_set():
            self._wave_anim_after = self.root.after(45, self._wave_anim_tick)
        else:
            self._hide_row_waves()

    def _hide_row_waves(self):
        for cv in getattr(self, "_row_wave_canvases", []):
            try:
                cv.place_forget()
            except Exception:
                pass

    def _update_row_waves(self):
        """Отрисовать волнообразный ползунок внизу/вдоль длины каждого рендерящегося трека в списке."""
        if not hasattr(self, "tree") or not self.tree.winfo_exists():
            return
        rendering_rows = []
        cur_id = self.queue_items[self.current_idx].id if 0 <= self.current_idx < len(self.queue_items) else None
        sel_iids = set(self.tree.selection())
        tree_h = self.tree.winfo_height()
        for it in self.queue_items:
            if not self._is_item_rendering(it):
                continue
            iid = self._iid_by_item_id.get(it.id)
            if not iid:
                continue
            try:
                bbox = self.tree.bbox(iid)
            except Exception:
                bbox = None
            if not bbox or len(bbox) != 4:
                continue
            rx, ry, rw, rh = bbox
            if rw <= 24 or rh <= 6 or (ry + rh - 5) < 22 or (ry + rh) > tree_h - 1:
                continue
            active = (it.id == self._rendering_item_id) or (it.id == cur_id)
            is_sel = (iid in sel_iids) or (it.id == cur_id)
            rendering_rows.append((it, iid, rx, ry, rw, rh, active, is_sel))
            if len(rendering_rows) >= 6:
                break

        while len(self._row_wave_canvases) < len(rendering_rows):
            cv = tk.Canvas(self.tree, height=5, highlightthickness=0, bd=0, cursor="hand2")
            self._row_wave_canvases.append(cv)

        for idx, (it, iid, rx, ry, rw, rh, active, is_sel) in enumerate(rendering_rows):
            cv = self._row_wave_canvases[idx]
            row_bg = self.col_sel if is_sel else self.col_panel
            cw = max(16, rw - 8)
            ch = 7
            try:
                cv.configure(bg=row_bg)
                cv.place(x=rx + 4, y=ry + rh - ch - 1, width=cw, height=ch)
                cv.delete("all")
                self._draw_wave_slider_strip(
                    cv, cw, ch, self._wave_phase + idx * 0.7,
                    row_bg, self.col_hairline, self.col_signal, active=active
                )
                cv.bind("<Button-1>", lambda _e, row_iid=iid: (self.tree.selection_set(row_iid), self._on_tree_select()))
                cv.bind("<Double-Button-1>", lambda _e, row_iid=iid: (self.tree.selection_set(row_iid), self._play_selected()))
                self._bind_context(cv, lambda _e, row_iid=iid: self._show_context_menu_for_row(_e, row_iid))
            except Exception:
                pass

        for cv in self._row_wave_canvases[len(rendering_rows):]:
            try:
                cv.place_forget()
            except Exception:
                pass

    def _wave_anim_tick(self):
        self._wave_anim_after = None
        if self.stop_event.is_set():
            return
        if not self._any_rendering():
            self._hide_row_waves()
            self._draw_seek()
            self._update_mini()
            return
        self._wave_phase = (self._wave_phase + 0.14) % 628.0
        self._update_row_waves()
        self._draw_seek()
        if self.mini is not None and self.mini.winfo_exists():
            self._update_mini()
        self._wave_anim_after = self.root.after(45, self._wave_anim_tick)

    def _draw_seek(self, frac=None):
        c = self.seek_canvas
        if frac is None:
            frac = self._seek_frac
        frac = max(0.0, min(1.0, frac))
        w = c.winfo_width(); h = c.winfo_height()
        if w <= 2:
            return
        c.delete("all")
        y = h // 2
        seek_item = self._active_seek_item()
        item_rendering = self._is_item_rendering(seek_item)
        # Если трек ещё рендерится и воспроизведение не началось — во всю длину дорожки рисуем волновой ползунок
        if not self.player.has_track() and (item_rendering or self._any_rendering()):
            self._draw_wave_slider_strip(
                c, w, h, self._wave_phase,
                self.col_panel, self.col_hairline, self.col_signal, active=True
            )
            return
        wave = getattr(self, "_wave", None)
        fx = w * frac
        if wave is not None and len(wave) > 2:
            n = len(wave)
            for x in range(0, w, 2):
                amp = float(wave[min(n - 1, int(x / w * n))])
                bh = max(1.0, amp * (h - 4))
                col = self.col_signal if x <= fx else self.col_hairline
                c.create_line(x, y - bh / 2, x, y + bh / 2, fill=col, width=2)
        else:
            r = 3
            c.create_line(r, y, w - r, y, fill=self.col_hairline, width=5, capstyle="round")
            if fx > r:
                c.create_line(r, y, fx, y, fill=self.col_signal, width=5, capstyle="round")
        # Если первая часть длинного трека уже играет, а продолжение (more_parts) ещё рендерится —
        # вдоль нижней кромки/хвоста дорожки пускаем волновую анимацию дорендера
        if seek_item is not None and getattr(seek_item, "more_parts", False):
            tail_x = max(int(fx) + 10, int(w * 0.35))
            if tail_x < w - 16:
                self._draw_wave_slider_strip(
                    c, w, h, self._wave_phase,
                    self.col_panel, self.col_hairline, self.col_signal,
                    active=True, x_start=tail_x
                )
        # закладки (жёлтые риски)
        dur = self.player.duration() or 0
        if dur > 0:
            for bs in self._current_bookmarks():
                bx = w * (bs / dur)
                c.create_line(bx, 0, bx, h, fill="#ffcc00", width=2)
            # точки A-B
            for ab, lbl in ((self._ab[0], "A"), (self._ab[1], "B")):
                if ab is not None:
                    ax = w * (min(ab, dur) / dur)
                    c.create_line(ax, 0, ax, h, fill="#34c759", width=2)
        # бегунок
        c.create_oval(fx - 6, y - 6, fx + 6, y + 6, fill=self.col_signal, outline=self.col_panel, width=2)

    def _seek_canvas_click(self, event):
        if not self.player.has_track():
            return
        w = self.seek_canvas.winfo_width()
        frac = max(0.0, min(1.0, event.x / max(1, w)))
        self._seek_frac = frac
        self._seeking = True
        self._seek_dur = self.player.duration()
        self.lbl_pos.configure(text=self._fmt_time(frac * self._seek_dur))
        self._draw_seek(frac)

    def _seek_canvas_release(self, _event=None):
        if not self._seeking:
            return
        dur = self._seek_dur or self.player.duration()
        try:
            self.player.seek(self._seek_frac * dur)
        except Exception:
            pass
        self.user_paused = False
        self.btn_toggle.configure(text=self._g_pause)
        self._seeking = False

    def _seek_relative(self, delta: float):
        if isinstance(self.root.focus_get(), (tk.Text, ttk.Entry)):
            return
        if not self.player.has_track():
            return
        pos = self.player.position() + delta
        pos = max(0.0, min(pos, self.player.duration()))
        try:
            self.player.seek(pos)
            self.user_paused = False
            self.btn_toggle.configure(text=self._g_pause)
        except Exception:
            pass

    def _jump_sentence(self, direction: int):
        """Перемотка по предложениям внутри текущей реплики (как у Speechify). Если у реплики
        есть тайминги предложений — прыгаем к началу соседней фразы; если нет (например,
        распознанное аудио) — мягкий откат на ±8 секунд."""
        if not self.player.has_track():
            return
        item = self.queue_items[self.current_idx] if 0 <= self.current_idx < len(self.queue_items) else None
        marks = getattr(item, "marks", None) if item else None
        pos = self.player.position()
        if not marks:
            return self._seek_relative(8.0 if direction > 0 else -8.0)
        starts = sorted(m[0] for m in marks)
        if direction > 0:
            target = next((s for s in starts if s > pos + 0.05), starts[-1])
        else:
            prevs = [s for s in starts if s < pos - 1.0]    # «назад» сначала к началу текущей фразы
            target = prevs[-1] if prevs else 0.0
        try:
            self.player.seek(max(0.0, min(target, self.player.duration())))
            self.user_paused = False
            self.btn_toggle.configure(text=self._g_pause)
            self._set_status("◄ предыдущая фраза" if direction < 0 else "следующая фраза ►", ok=True)
        except Exception:
            pass
        return "break"

    # ------------------ A-B повтор и закладки ------------------
    def _set_ab_a(self):
        if self.player.has_track():
            self._ab[0] = self.player.position()
            self._set_status(f"A = {self._fmt_time(self._ab[0])}", ok=True); self._draw_seek()

    def _set_ab_b(self):
        if self.player.has_track():
            self._ab[1] = self.player.position()
            self._set_status(f"B = {self._fmt_time(self._ab[1])} (повтор A↔B)", ok=True); self._draw_seek()

    def _clear_ab(self):
        self._ab = [None, None]; self._set_status("A-B повтор сброшен", ok=True); self._draw_seek()

    def _current_bookmarks(self):
        if 0 <= self.current_idx < len(self.queue_items):
            return self.queue_items[self.current_idx].bookmarks
        return []

    def _add_bookmark(self):
        if not (0 <= self.current_idx < len(self.queue_items)) or not self.player.has_track():
            return
        it = self.queue_items[self.current_idx]
        pos = round(self.player.position(), 2)
        if all(abs(pos - b) > 0.5 for b in it.bookmarks):
            it.bookmarks.append(pos); it.bookmarks.sort()
            self._set_status(f"закладка на {self._fmt_time(pos)}", ok=True)
            self._draw_seek()

    def _update_seekbar(self):
        if self._seeking:
            return
        try:
            if self.player.has_track():
                dur = self.player.duration(); pos = self.player.position()
                if dur > 0:
                    self._seek_frac = min(1.0, pos / dur)
                    self._draw_seek(self._seek_frac)
                    self.lbl_pos.configure(text=self._fmt_time(pos))
                    self.lbl_dur.configure(text=self._fmt_time(dur))
                    self._highlight_speaking(pos)
            else:
                self._seek_frac = 0.0; self._draw_seek(0.0)
                self.lbl_pos.configure(text="0:00"); self.lbl_dur.configure(text="0:00")
        except Exception:
            pass

    def _on_text_click(self, event):
        """Клик по предложению в панели текста → перемотка к его таймкоду."""
        try:
            idx = self.text_widget.index(f"@{event.x},{event.y}")
        except Exception:
            return
        char = None
        for seg in getattr(self, "_panel_segs", []):       # в какой кусок текста попал клик
            if seg["kind"] == "text" and self.text_widget.compare(idx, ">=", seg["start"]):
                pos = min(len(self.text_widget.get(seg["start"], idx)), len(seg["display"]))
                char = vp_layout.shown_to_spoken(seg, pos)
        if char is not None:
            self._seek_to_char(char)

    def _seek_to_char(self, char: int):
        """Перемотать к предложению, в котором стоит этот символ озвучиваемого текста."""
        item = getattr(self, "_panel_item", None)
        if not item or not item.marks or char < 0:
            return
        target = None
        for (s, e, a, b) in item.marks:
            if a <= char < b:
                target = s; break
        if target is None:                       # клик в промежутке — ближайшее следующее
            for (s, e, a, b) in item.marks:
                if char < a:
                    target = s; break
        if target is None:
            return
        real_idx = self.queue_items.index(item) if item in self.queue_items else self.current_idx
        if real_idx == self.current_idx and self.player.has_track():
            try: self.player.seek(target)
            except Exception: pass
            self.user_paused = False
            self.btn_toggle.configure(text=self._g_pause)
        else:                                    # другая реплика — включить и перемотать
            self._play_index(real_idx)
            self.root.after(300, lambda t=target: self._seek_safe(t))

    # --- панель полного текста: read-only + копирование/выделение ---
    def _text_panel_key(self, e):
        """Страж: вне режима «Правка» блокируем ввод, но пропускаем навигацию/выделение."""
        if getattr(self, "_editing_text", False):
            return None                          # режим правки — печатать можно
        if e.keysym in ("Left", "Right", "Up", "Down", "Home", "End", "Prior", "Next",
                        "Tab", "Shift_L", "Shift_R", "Control_L", "Control_R", "Prior", "Next"):
            return None                          # навигация и Shift-выделение разрешены
        return "break"                           # печать/Backspace/Delete блокируем

    def _text_panel_copy(self):
        try:
            s = self.text_widget.get("sel.first", "sel.last")
        except Exception:
            s = ""
        if not s:                                # нет выделения — копируем всё тело реплики (без заголовка)
            s = self._panel_plain_text()
        if s:
            self.root.clipboard_clear(); self.root.clipboard_append(s)
            self._set_status("текст скопирован", ok=True)

    def _text_panel_select_all(self):
        base = getattr(self, "_text_body_offset", 0)
        try:
            self.text_widget.tag_remove("sel", "1.0", "end")
            self.text_widget.tag_add("sel", f"1.0+{base}c", "end-1c")   # тело без заголовка
        except Exception:
            self.text_widget.tag_add("sel", "1.0", "end-1c")
        self.text_widget.focus_set()

    def _text_panel_ctrl(self, e):
        kc = e.keycode
        if kc == 67:                              # C — копировать
            self._text_panel_copy(); return "break"
        if kc == 65:                              # A — выделить всё
            self._text_panel_select_all(); return "break"
        if getattr(self, "_editing_text", False):  # в режиме «Правка» — вставка/вырезание
            if kc == 86: e.widget.event_generate("<<Paste>>"); return "break"
            if kc == 88: e.widget.event_generate("<<Cut>>");   return "break"
            if kc == 90:
                try: e.widget.edit_undo()
                except Exception: pass
                return "break"
        return None

    def _text_panel_menu(self, event):
        c = self.colors
        menu = tk.Menu(self.root, tearoff=0, bg=c["panel"], fg=c["ink"],
                       activebackground=c["sel"], activeforeground=c["ink"], bd=0)
        menu.add_command(label="Копировать", command=self._text_panel_copy)
        if getattr(self, "_editing_text", False):
            menu.add_command(label="Вырезать", command=lambda: self.text_widget.event_generate("<<Cut>>"))
            menu.add_command(label="Вставить", command=lambda: self.text_widget.event_generate("<<Paste>>"))
        menu.add_command(label="Выделить всё", command=self._text_panel_select_all)
        try: menu.tk_popup(event.x_root, event.y_root)
        finally: menu.grab_release()
        return "break"

    def _show_tts_input_menu(self, event):
        c = self.colors
        menu = tk.Menu(self.root, tearoff=0, bg=c["panel"], fg=c["ink"],
                       activebackground=c["sel"], activeforeground=c["ink"], bd=0)
        menu.add_command(label="✂ Вырезать", command=lambda: self.tts_input.event_generate("<<Cut>>"))
        menu.add_command(label="📋 Копировать", command=lambda: self.tts_input.event_generate("<<Copy>>"))
        menu.add_command(label="📥 Вставить", command=self._paste_into_input)
        menu.add_separator()
        menu.add_command(label="Выделить всё", command=lambda: (self.tts_input.tag_add("sel", "1.0", "end"), self.tts_input.mark_set("insert", "end")))
        menu.add_command(label="Очистить", command=lambda: (self.tts_input.delete("1.0", "end"), self.tts_input.configure(height=3)))
        try: menu.tk_popup(event.x_root, event.y_root)
        finally: menu.grab_release()
        return "break"

    def _show_entry_menu(self, event):
        w = event.widget
        c = self.colors
        menu = tk.Menu(self.root, tearoff=0, bg=c["panel"], fg=c["ink"],
                       activebackground=c["sel"], activeforeground=c["ink"], bd=0)
        menu.add_command(label="✂ Вырезать", command=lambda: w.event_generate("<<Cut>>"))
        menu.add_command(label="📋 Копировать", command=lambda: w.event_generate("<<Copy>>"))
        menu.add_command(label="📥 Вставить", command=lambda: w.event_generate("<<Paste>>"))
        menu.add_separator()
        menu.add_command(label="Выделить всё", command=lambda: (w.select_range(0, "end"), w.icursor("end")))
        menu.add_command(label="Очистить", command=lambda: w.delete(0, "end"))
        try: menu.tk_popup(event.x_root, event.y_root)
        finally: menu.grab_release()
        return "break"

    def _seek_safe(self, seconds: float):
        try:
            self.player.seek(seconds)
            self.user_paused = False
            self.btn_toggle.configure(text=self._g_pause)
        except Exception:
            pass

    # ------------------ подсветка читаемого предложения ------------------
    def _highlight_speaking(self, pos: float):
        if not self.var_show_text.get() or getattr(self, "_editing_text", False):
            return
        if not (0 <= self.current_idx < len(self.queue_items)):
            return
        item = self.queue_items[self.current_idx]
        if item is not getattr(self, "_panel_item", None):
            return
        marks = getattr(item, "marks", None)
        # предложение — мягкая фоновая подсветка + автопрокрутка
        if marks:
            cur = None
            for (start, end, a, b) in marks:
                if start <= pos < end:
                    cur = (a, b); break
            if cur is not None and cur != self._hl_range:
                self._hl_range = cur
                try:
                    self.text_widget.tag_remove("speaking", "1.0", "end")
                    self._panel_tag_span("speaking", cur[0], cur[1])
                    self._panel_hl_rows(cur[0], cur[1])
                    self._panel_see(cur[0])
                except Exception:
                    pass
        # слово — яркая подсветка (follow-along, как у Speechify)
        words = getattr(item, "word_marks", None)
        if not words and marks:                      # старые/восстановленные реплики — вывести из предложений
            words = item.word_marks = _derive_word_marks(item.text, marks)
        if words:
            wcur = None
            for (start, end, a, b) in words:
                if start <= pos < end:
                    wcur = (a, b); break
            if wcur != getattr(self, "_hl_word", None):
                self._hl_word = wcur
                try:
                    self.text_widget.tag_remove("speaking_word", "1.0", "end")
                    if wcur:
                        self._panel_tag_span("speaking_word", wcur[0], wcur[1])
                    self._panel_hl_cell(wcur)
                except Exception:
                    pass

    # --- разметка панели: обычный текст + таблицы ---
    def _panel_tag_span(self, tag: str, a: int, b: int):
        """Поставить тег на отрезок озвучиваемого текста (куски с таблицами пропускаются)."""
        for seg in getattr(self, "_panel_segs", []):
            if seg["kind"] == "text" and a < seg["b"] and b > seg["a"]:
                x0 = vp_layout.spoken_to_shown(seg, max(a, seg["a"]))
                x1 = vp_layout.spoken_to_shown(seg, min(b, seg["b"]), end=True)
                self.text_widget.tag_add(tag, f"{seg['start']}+{x0}c", f"{seg['start']}+{x1}c")

    def _panel_rows(self):
        for seg in getattr(self, "_panel_segs", []):
            if seg["kind"] == "table":
                yield from seg["rows"]

    def _panel_paint_row(self, row: dict, bg: str, fg: Optional[str] = None):
        for lbl in row.get("labels", []):
            lbl.configure(bg=bg, fg=fg or self.col_ink)

    def _panel_hl_rows(self, a: int, b: int):
        """Строка таблицы, которая читается сейчас, подсвечена как предложение."""
        for row in self._panel_rows():
            if row["b"] > row["a"] and a < row["b"] and b > row["a"]:
                self._panel_paint_row(row, self.col_sel)
                row["active"] = True
            elif row.get("active"):
                self._panel_paint_row(row, self.col_panel)
                row["active"] = False

    def _panel_hl_cell(self, word):
        """Ячейка с текущим словом — яркая, как слово в обычном тексте."""
        old = getattr(self, "_hl_cell", None)
        new = None
        if word:
            for row in self._panel_rows():
                for col, a, b in row.get("spans", []):
                    if col >= 0 and word[0] < b and word[1] > a:
                        new = row["labels"][col]
        if old is new:
            return
        if old is not None and old.winfo_exists():
            old.configure(bg=self.col_sel, fg=self.col_ink)
        if new is not None:
            new.configure(bg=self.col_signal, fg=self._on_signal_fg(self.col_signal))
        self._hl_cell = new

    def _panel_see(self, char: int):
        for seg in getattr(self, "_panel_segs", []):
            if seg["a"] <= char < seg["b"]:
                if seg["kind"] == "text":
                    self.text_widget.see(f"{seg['start']}+{vp_layout.spoken_to_shown(seg, char)}c")
                else:
                    for row in seg["rows"]:
                        if row["a"] <= char < row["b"]:
                            self.text_widget.see(row["index"])
                return

    def _panel_configure_tags(self):
        """Оформление исходной вёрстки: шрифты общие, поэтому меняются вместе с размером текста."""
        import tkinter.font as _tkfont
        text = self.text_widget
        if not hasattr(self, "text_font_heading"):
            size = self._text_font_size
            self.text_font_heading = _tkfont.Font(family="Segoe UI", size=size + 2, weight="bold")
            self.text_font_italic = _tkfont.Font(family="Segoe UI", size=size, slant="italic")
            self.text_font_code = _tkfont.Font(family="Consolas", size=size)
        text.tag_configure("doc_heading", font=self.text_font_heading, spacing1=6, spacing3=3)
        text.tag_configure("doc_list", lmargin1=8, lmargin2=30, spacing3=2)
        text.tag_configure("doc_quote", lmargin1=16, lmargin2=16, font=self.text_font_italic, foreground=self.col_mute)
        text.tag_configure("doc_code", font=self.text_font_code, background=self.colors["panel2"], lmargin1=8, lmargin2=8)
        text.tag_configure("doc_rule", foreground=self.col_hairline)
        text.tag_configure("inline_bold", font=self.text_font_bold)
        text.tag_configure("inline_italic", font=self.text_font_italic)
        text.tag_configure("inline_code", font=self.text_font_code, background=self.colors["panel2"])
        for tag in ("speaking", "speaking_word"):     # подсветка чтения поверх оформления
            text.tag_raise(tag)

    def _panel_insert_text(self, seg: dict):
        """Строка исходного текста: seg["start"] — откуда считаются позиции внутри неё."""
        text = self.text_widget
        seg["start"] = text.index("end-1c")
        text.insert("end-1c", seg["display"] + "\n")
        start, display = seg["start"], seg["display"]
        if display and seg["style"] != "para":
            text.tag_add(f"doc_{seg['style']}", start, f"{start}+{len(display) + 1}c")
        for a, b, style in seg["spans"]:
            text.tag_add(f"inline_{style}", f"{start}+{a}c", f"{start}+{b}c")

    def _panel_insert_table(self, seg: dict):
        """Таблица в панели: каждая строка — отдельная вложенная рамка, чтобы прокрутка
        могла показать именно читаемую строку. Ширины колонок общие — считает layout."""
        text = self.text_widget
        seg["frames"] = []
        rows = [{"cells": seg["header"], "a": 0, "b": 0, "spans": [], "head": True}] + seg["rows"]
        width = max(len(r["cells"]) for r in rows)
        for row in rows:
            frame = tk.Frame(text, bg=self.col_hairline, cursor="hand2")
            row["labels"] = []
            for col in range(width):
                value = row["cells"][col] if col < len(row["cells"]) else ""
                lbl = tk.Label(frame, text=value, justify="left", anchor="nw", padx=7, pady=4,
                               font=self.text_font_bold if row.get("head") else self.text_font,
                               bg=self.col_panel, fg=self.col_ink, bd=0, highlightthickness=0)
                lbl.grid(row=0, column=col, sticky="nsew", padx=(1, 1 if col == width - 1 else 0),
                         pady=(1 if row.get("head") else 0, 1))
                lbl.bind("<MouseWheel>", lambda e: (text.yview_scroll(-3 if e.delta > 0 else 3, "units"), "break")[1])
                lbl.bind("<Control-MouseWheel>", lambda e: self._change_text_font(1 if e.delta > 0 else -1))
                self._bind_context(lbl, self._text_panel_menu)
                if not row.get("head"):
                    target = next((a for c, a, _b in row["spans"] if c == col), row["a"])
                    lbl.bind("<Button-1>", lambda e, ch=target: self._seek_to_char(ch))
                row["labels"].append(lbl)
            frame.rowconfigure(0, weight=1)
            row["index"] = text.index("end-1c")
            text.window_create("end-1c", window=frame, align="top")
            text.insert("end-1c", "\n")
            row["frame"] = frame
            seg["frames"].append(row)
        self._panel_widgets.extend(r["frame"] for r in rows)
        seg["width"] = width
        self._panel_layout_table(seg)

    def _panel_layout_table(self, seg: dict):
        """Ширины колонок: по содержимому. Если не влезает — ужимаем длинные колонки, но не уже
        самого длинного слова, чтобы слова не рвались посередине."""
        avail = max(160, self.text_widget.winfo_width() - 36)
        pad = 18
        need = [40] * seg["width"]
        floor = [40] * seg["width"]
        for row in seg["frames"]:
            font = self.text_font_bold if row.get("head") else self.text_font
            for col, value in enumerate(row["cells"]):
                need[col] = max(need[col], min(font.measure(value) + pad, 460))
                longest = max((font.measure(w) for w in value.split()), default=0) + pad
                floor[col] = max(floor[col], longest)
        floor = [min(f, n) for f, n in zip(floor, need)]
        if sum(need) <= avail:
            widths = need
        elif sum(floor) >= avail:                 # совсем узко: иначе таблица не поместится
            widths = [max(40, int(f * avail / sum(floor))) for f in floor]
        else:
            room = avail - sum(floor)
            stretch = sum(n - f for n, f in zip(need, floor)) or 1
            widths = [f + int((n - f) * room / stretch) for n, f in zip(need, floor)]
        for row in seg["frames"]:
            for col, lbl in enumerate(row["labels"]):
                lbl.configure(wraplength=max(24, widths[col] - pad + 2))
                row["frame"].columnconfigure(col, minsize=widths[col], weight=0)

    def _on_text_panel_resize(self, e):
        if e.width == getattr(self, "_panel_width", 0):
            return
        self._panel_width = e.width
        if getattr(self, "_panel_resize_after", None):
            self.root.after_cancel(self._panel_resize_after)
        self._panel_resize_after = self.root.after(120, self._panel_relayout)

    def _panel_relayout(self):
        self._panel_resize_after = None
        for seg in getattr(self, "_panel_segs", []):
            if seg["kind"] == "table":
                self._panel_layout_table(seg)

    @staticmethod
    def _has_source(item: Item) -> bool:
        """У распознанных файлов исходника нет: raw_text там имя файла или сам результат."""
        return item.source != "file" and bool(item.raw_text.strip())

    def _shown_text(self, item: Item) -> str:
        """Текст реплики для показа: как написан, без словаря произношения и замен для голоса."""
        if not self._has_source(item):
            return item.text
        if item.shown_cache[0] != item.raw_text:
            item.shown_cache = (item.raw_text, vp_layout.plain_text(item.raw_text) or item.text)
        return item.shown_cache[1]

    def _panel_plain_text(self, item: Optional[Item] = None) -> str:
        """Текст реплики для копирования: с таблицей, а не озвучкой «колонка: значение»."""
        item = item or getattr(self, "_panel_item", None)
        if item is None:
            return ""
        return normalize_tables(item.raw_text) if self._has_source(item) else item.text

    # ------------------ panel toggles ------------------
    def _toggle_text_panel(self):
        if self.var_show_text.get():
            self._hide_text_panel()
        else:
            self._show_text_panel()

    def _show_text_panel(self):
        self.var_show_text.set(True)
        if not getattr(self, "_text_added", False):
            try:
                self.body.add(self.text_frame, weight=2)
                self._text_added = True
            except Exception: pass
        self._update_text_panel()
        self._save_prefs()

    def _hide_text_panel(self):
        self.var_show_text.set(False)
        if getattr(self, "_text_added", False):
            try:
                self.body.forget(self.text_frame)
                self._text_added = False
            except Exception: pass
        self._save_prefs()

    def _toggle_queue_panel(self):
        if getattr(self, "_mid_added", True):
            self._hide_queue_panel()
        else:
            self._show_queue_panel()

    def _show_queue_panel(self):
        self.var_show_queue.set(True)
        if not getattr(self, "_mid_added", False):
            try:
                self.body.insert(0, self.mid_frame, weight=1)
                self._mid_added = True
            except Exception:
                try: self.body.add(self.mid_frame, weight=1); self._mid_added = True
                except Exception: pass
        self._save_prefs()

    def _hide_queue_panel(self):
        self.var_show_queue.set(False)
        if getattr(self, "_mid_added", True):
            try:
                self.body.forget(self.mid_frame)
                self._mid_added = False
            except Exception: pass
        self._save_prefs()

    def _toggle_search(self):
        if self.search_frame.winfo_ismapped():
            self.search_frame.pack_forget()
            self.var_search.set("")
            self.tree.focus_set()
        else:
            self.search_frame.pack(fill="x", pady=(6, 0))
            self.search_entry.focus_set()

    def _on_search_change(self):
        self.search_query = self.var_search.get().strip().lower()
        self._render_list()

    # ------------------ queue logic ------------------
    def enqueue(self, *, source: str, author: str, time_str: str, text: str,
                date_str: str = "", prompt: str = "", title: str = "", dedup: bool = True) -> Optional[Item]:
        clean = _clean_for_tts(text)
        if not clean: return None
        # дедуп: проверяем совпадение с последними живыми элементами очереди (защита от дубля хук + watcher)
        if dedup:
            norm_raw = text.strip()
            for prev in reversed(self.queue_items[-25:]):
                if not prev.deleted and prev.raw_text.strip() == norm_raw:
                    if prompt and not prev.prompt:
                        prev.prompt = prompt
                    if title and not prev.title:
                        prev.title = title
                    return None
        item = Item(id=str(uuid4()), source=source, author=author, time_str=time_str,
                    text=clean, raw_text=text, date_str=date_str or time.strftime("%d.%m.%Y"),
                    prompt=prompt, title=title)
        self.queue_items.append(item)
        self._q_put(item, 100)
        archive_add(item)                          # в полный искомый архив
        if author in getattr(self, "_watch", set()):
            self.root.after(0, lambda a=author: self._notify("Новое от " + a, clean[:80]))
        self.root.after(0, self._request_render)
        return item

    def _ensure_ai_hooks_connected(self):
        """При старте обновить стабильную копию хука vp_feed.py и авто-подключить Antigravity/Codex,
        если они установлены и пользователь ранее не отключал их вручную."""
        try:
            hook_home = APP_DIR if getattr(sys, "frozen", False) else vp_updater.stable_hook_home(APP_DIR)
            disabled = set(self.prefs.get("disabled_ai_tools", []))
            for tid in ("antigravity", "codex"):
                if tid in disabled:
                    continue
                spec = vp_integrations.HOOK_TOOLS.get(tid)
                if spec and (Path.home() / spec["home"]).exists() and not vp_integrations.is_connected(tid):
                    try:
                        vp_integrations.connect(tid, hook_home)
                        log_line(f"auto-connected hook for {tid}")
                    except Exception as e:
                        log_line(f"auto-connect {tid} skipped: {e}")
        except Exception as e:
            log_line(f"ensure_ai_hooks error: {e}")

    def _poll_codex_inbox(self):
        inbox = Path.home() / ".voice-player" / "codex-inbox"
        try:
            for entry in sorted(inbox.glob("*.json")):
                try:
                    payload = json.loads(entry.read_text(encoding="utf-8"))
                    self.enqueue(
                        source=payload.get("source") or "codex",
                        author=payload["author"],
                        time_str=payload["time"],
                        date_str=payload.get("date") or "",
                        text=payload["text"],
                        prompt=str(payload.get("prompt") or ""),
                        title=str(payload.get("title") or ""),
                        dedup=True,
                    )
                except (ValueError, KeyError, TypeError) as error:
                    log_line(f"Invalid Codex inbox entry {entry.name}: {error}")
                    entry.rename(entry.with_suffix(".invalid"))
                else:
                    entry.unlink()
        except OSError as error:
            log_line(f"Codex inbox: {error}")
        self.root.after(1000, self._poll_codex_inbox)

    def _watcher_poll_loop(self):
        """Фоновый опрос локальных баз и логов Antigravity и Codex (App + CLI)."""
        while not self.stop_event.is_set():
            try:
                disabled = set(self.prefs.get("disabled_ai_tools", []))
                items = self.watchers.poll()
                if items:
                    filtered = [it for it in items if it.get("source") not in disabled]
                    if filtered:
                        self._ui_after(lambda batch=filtered: self._enqueue_watched(batch))
            except Exception as e:
                log_line(f"watcher poll error: {e}")
            self.stop_event.wait(2.0)

    def _enqueue_watched(self, items: list[dict], *, autoplay_allow: bool = True) -> int:
        added = 0
        was_auto = self.var_autoplay.get()
        if not autoplay_allow:
            self.var_autoplay.set(False)
        try:
            for d in items:
                it = self.enqueue(
                    source=d.get("source") or "codex",
                    author=d.get("author") or "AI",
                    time_str=d.get("time") or time.strftime("%H:%M"),
                    date_str=d.get("date") or time.strftime("%d.%m.%Y"),
                    text=d.get("text") or "",
                    prompt=d.get("prompt") or "",
                    title=d.get("title") or "",
                    dedup=True,
                )
                if it is not None:
                    if not autoplay_allow:
                        it.played = True
                    added += 1
        finally:
            if not autoplay_allow:
                self.var_autoplay.set(was_auto)
        if added:
            self._save_session()
            self._render_list()
        return added

    def _pull_recent_ai(self, limit_per_source: int = 5, silent: bool = False):
        """Явно подтянуть последние диалоги и ответы из Antigravity, Codex и терминального Codex."""
        if not silent:
            self._set_status("подтягиваю последние диалоги из Antigravity и Codex…", ok=True)

        def worker():
            try:
                items = self.watchers.fetch_recent(limit_per_source=limit_per_source)
            except Exception as e:
                log_line(f"pull_recent_ai error: {e}")
                items = []

            def done():
                n = self._enqueue_watched(items, autoplay_allow=False)
                if n > 0:
                    self._set_status(f"подтянуто новых диалогов из Antigravity / Codex: {n}", ok=True)
                elif not silent:
                    total_ai = sum(1 for it in self.queue_items if not it.deleted and it.source in AI_SOURCES)
                    self._set_status(f"все последние диалоги уже в очереди (всего ИИ-ответов: {total_ai})", ok=True)
            self._ui_after(done)

        threading.Thread(target=worker, daemon=True, name="pull-ai").start()

    def clear_queue(self):
        try: self.player.stop()
        except Exception: pass
        # закреплённые (📌) НЕ удаляем — они остаются в очереди
        kept = [it for it in self.queue_items if it.pinned and not it.deleted]
        for it in kept:
            it.played = False
        self.queue_items = kept
        self.current_idx = -1
        self.btn_toggle.configure(text=self._g_play)
        self.lbl_now_author.configure(text="—")
        self.lbl_now_time.configure(text="")
        self.lbl_now_text.configure(
            text="Очередь очищена." + (" Закреплённые сохранены." if kept else " Ждём новых реплик…"))
        self._update_text_panel()
        self._render_list()

    def _spawn_render_workers(self, workers: int):
        for i in range(workers):
            threading.Thread(target=self._render_worker_loop, daemon=True, name=f"tts-{i}").start()

    # PRIO: 0 — играем прямо сейчас, 10 — следующая на очереди (прогрев), 50 — повтор, 100 — обычная
    def _q_put(self, item: "Item", prio: int = 100):
        with self._render_seq_lock:
            seq = self._render_seq
            self._render_seq += 1
        self.render_queue.put((prio, seq, item))

    def _prefetch_around(self, idx: int):
        """Прогреть наперёд: текущую (если не готова) — срочно, следующую годную — заранее,
        чтобы к концу текущей реплики звук уже был готов и не было паузы."""
        try:
            if 0 <= idx < len(self.queue_items):
                cur = self.queue_items[idx]
                if not cur.audio_ready and not cur.audio_error and not cur.deleted:
                    self._q_put(cur, 0)
            nxt = self._find_next(idx + 1)
            if 0 <= nxt < len(self.queue_items):
                nx = self.queue_items[nxt]
                if not nx.audio_ready and not nx.audio_error and not nx.deleted:
                    self._q_put(nx, 10)
        except Exception:
            pass

    def _render_worker_loop(self):
        while not self.stop_event.is_set():
            try: prio, seq, item = self.render_queue.get(timeout=0.5)
            except queue.Empty: continue
            # дубль из приоритетной переочереди или уже готовая/удалённая — пропускаем
            if item.deleted or (item.audio_ready and not item.more_parts):
                continue
            # Озвучка всегда в нейтральном темпе; скорость регулирует плеер на лету.
            voice = self._author_voices.get(item.author) or getattr(self, "_voice", TTS_VOICE)
            # объявление автора голосом (только доска; флаг кэшируем в обычный атрибут —
            # tk-переменные из фонового потока трогать нельзя)
            lead = ""
            if getattr(self, "_speak_author_on", False) and item.source == "board" and item.author:
                lead = self._disp_author(item.author)
            streaming = hasattr(self.player, "append_file")  # старый MCI-плеер ждёт весь файл
            self._rendering_item_id = item.id
            self._ui_after(self._schedule_wave_anim)
            try:
                render_audio_blocking(item, voice=voice, lead=lead,
                                      on_part=self._on_part_ready if streaming else None)
            finally:
                if self._rendering_item_id == item.id:
                    self._rendering_item_id = None
            if item.audio_error and not self._maybe_auto_retry(item):
                item.more_parts = False                  # повторов не будет — отыграть то, что есть
            self.root.after(0, lambda it=item: self._on_render_done(it))
            self.root.after(0, self._request_render)

    def _input_ctrl(self, e):
        """Ctrl+A/C/V/X/Z в поле ввода по keycode — независимо от раскладки клавиатуры.
        Windows VK: A=65, C=67, V=86, X=88, Z=90."""
        w = e.widget
        kc = e.keycode
        if kc == 65:                                  # A — выделить всё
            try:
                w.tag_add("sel", "1.0", "end-1c")
                w.mark_set("insert", "1.0")
            except Exception:
                pass
            return "break"
        if kc == 67:                                  # C — копировать
            w.event_generate("<<Copy>>");  return "break"
        if kc == 86:                                  # V — вставить
            w.event_generate("<<Paste>>"); return "break"
        if kc == 88:                                  # X — вырезать
            w.event_generate("<<Cut>>");   return "break"
        if kc == 90:                                  # Z — отменить
            try: w.edit_undo()
            except Exception: pass
            return "break"
        return None

    def _tree_ctrl(self, e):
        """Ctrl+A в списке — выделить все строки (по keycode, любая раскладка)."""
        if e.keycode == 65:
            kids = self.tree.get_children()
            if kids:
                self.tree.selection_set(kids)
            return "break"
        if e.keycode == 67:                           # Ctrl+C — копировать текст выделенных реплик
            idxs = self._selected_indices()
            texts = [self._panel_plain_text(self.queue_items[i]) for i in idxs if 0 <= i < len(self.queue_items)]
            if texts:
                try:
                    self.root.clipboard_clear(); self.root.clipboard_append("\n\n".join(texts))
                    self._set_status(f"скопировано реплик: {len(texts)}", ok=True)
                except Exception:
                    pass
            return "break"
        return None

    def _disp_author(self, author: str) -> str:
        """Отображаемое имя автора: псевдоним, если задан, иначе оригинал."""
        return self._author_names.get(author) or author

    def _on_render_done(self, item: "Item"):
        """Спокойный статус про сеть: одну строку при потере связи, одну при возврате —
        вместо пугающего «ошибка рендера» на каждую реплику. Главный поток."""
        try:
            if item.audio_ready:
                if self._net_offline:
                    self._net_offline = False
                    self._set_status("связь вернулась — озвучиваю накопившееся", ok=True)
                return
            err = (item.audio_error or "").lower()
            network = any(k in err for k in ("timeout", "connect", "getaddrinfo", "temporar",
                                             "ssl", "reset", "disconnected", "пустой файл", "name or service"))
            if network and not self._net_offline:
                self._net_offline = True
                self._set_status("нет связи с озвучкой — реплики ждут, озвучу как вернётся сеть", ok=False)
        except Exception:
            pass

    def _on_part_ready(self, item: Item):
        """Поток рендера: готова очередная часть. Если реплика уже играет — дописать в трек."""
        self._sync_parts(item)
        self.root.after(0, self._request_render)

    def _sync_parts(self, item: Item):
        with self._parts_lock:
            if item is not self._loaded_item:
                return
            gen = self._loaded_gen
            added = False
            for path in item.parts_done[item.parts_fed:]:
                try:
                    if not self.player.append_file(path, gen):
                        return
                except Exception as e:
                    log_line(f"append part [{item.id}]: {e}")
                    return
                item.parts_fed += 1
                added = True
        if added:
            self.root.after(0, lambda it=item: self._refresh_wave(it))

    def _refresh_wave(self, item: Item):
        if self._wave_id == item.id:
            self._wave = self.player.envelope()

    def _reset_render_state(self, item: Item):
        item.audio_error = None; item.audio_ready = False; item.audio_path = None
        item.parts_done = []; item.parts_fed = 0; item.more_parts = False
        item.render_attempts = 0                      # ручной повтор → свежий бюджет авто-повторов

    def _maybe_auto_retry(self, item: Item) -> bool:
        """Самолечение: упавшую по транзиентной ошибке (таймаут/сеть) реплику переставить
        в очередь рендера ещё раз, с нарастающей задержкой и жёстким лимитом — иначе шторм.
        Возвращает True, если повтор запланирован."""
        if item.deleted or not item.audio_error:
            return False
        err = (item.audio_error or "").lower()
        transient = any(k in err for k in ("timeout", "connect", "ssl", "temporar",
                                           "reset", "got disconnected", "пустой файл"))
        if not transient:
            return False                             # «пустой текст» и пр. — не повторяем
        if item.render_attempts >= TTS_AUTO_RETRY:
            return False                             # предохранитель
        item.render_attempts += 1
        delay = TTS_AUTO_RETRY_BASE_S * item.render_attempts
        log_line(f"auto-retry [{item.id}] через {delay}с (попытка {item.render_attempts}/{TTS_AUTO_RETRY})")

        def _requeue(it=item):
            if it.deleted or (it.audio_ready and not it.more_parts):
                return
            it.audio_error = None
            self._q_put(it, 150)                     # после новых реплик: сбойная не держит очередь
            self.root.after(0, self._request_render)
        t = threading.Timer(delay, _requeue)
        t.daemon = True
        t.start()
        return True

    def _speed_mult(self) -> float:
        """Множитель скорости со слайдера (0.5–2.0). Только из главного потока."""
        try: m = float(self.var_rate.get())
        except Exception: m = 1.0
        return max(0.5, min(2.0, m))

    def _source_allows(self, item: Item) -> bool:
        if item.deleted: return False
        if item.forced:                       # пользователь сам нажал «Озвучить» — фильтры не мешают
            return True
        if not self._source_allows_base(item):
            return False
        ALL_FILTER_VALUES = {"все", "all", "todos", "全部", "सभी", "すべて", "alle", "tous", ""}
        fa = self.var_filter_author.get()
        if fa and fa not in ALL_FILTER_VALUES and fa != vp_i18n.t("filter_all") and item.author != fa and self._disp_author(item.author) != fa: return False
        fd = self.var_filter_date.get()
        if fd and fd not in ALL_FILTER_VALUES and fd != vp_i18n.t("filter_all") and item.date_str != fd: return False
        if self.search_query:
            haystack = (item.author + " " + item.text).lower()
            if self.search_query not in haystack: return False
        return True

    def _visible(self, for_display: bool = False) -> list[Item]:
        """По умолчанию от старых к новым: так идут чтение, экспорт и плейлисты.
        for_display — порядок строк в списке на экране."""
        items = [m for m in self.queue_items if self._source_allows(m)]
        if for_display and not self.sort_col and self.var_newest_first.get():
            items.reverse()
        if self.sort_col:
            keyfn = {
                "author": lambda it: it.author.lower(),
                "date":   lambda it: it.date_str,
                "time":   lambda it: it.time_str,
                "text":   lambda it: it.text.lower(),
            }.get(self.sort_col)
            if keyfn:
                items.sort(key=keyfn, reverse=self.sort_reverse)
        # закреплённые — всегда вверху (стабильно, без потери остального порядка)
        items.sort(key=lambda it: 0 if it.pinned else 1)
        return items

    def _sort_by(self, col: str):
        if self.sort_col == col:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_col = col
            self.sort_reverse = False
        self._render_list()

    def _reset_filters(self):
        all_lbl = vp_i18n.t("filter_all")
        self.var_filter_author.set(all_lbl)
        self.var_filter_date.set(all_lbl)
        self.sort_col = None
        self.sort_reverse = False
        self._render_list()

    def _find_next(self, from_idx: int) -> int:
        for i in range(max(0, from_idx), len(self.queue_items)):
            it = self.queue_items[i]
            if not it.deleted and not it.played and it.author not in self._muted \
                    and self._source_allows(it):
                return i
        return -1

    def _find_prev(self, from_idx: int) -> int:
        for i in range(min(from_idx, len(self.queue_items) - 1), -1, -1):
            it = self.queue_items[i]
            if not it.deleted and self._source_allows(it): return i
        return -1

    def _playable(self, i) -> bool:
        it = self.queue_items[i]
        return (not it.deleted and it.author not in self._muted and self._source_allows(it))

    @staticmethod
    def _parse_mode_to_idx(mode_str: str) -> int:
        s = str(mode_str or "").strip().lower()
        if not s:
            return 0
        canonical_map = {
            "очередь": 0, "queue": 0, "cola": 0, "キュー": 0, "warteschlange": 0, "file d'attente": 0, "कतार": 0, "队列": 0,
            "повтор одной": 1, "repeat one": 1, "repetir una": 1, "1曲リピート": 1, "eine wiederholen": 1, "répéter un": 1, "एक दोहराएं": 1, "单曲循环": 1,
            "повтор очереди": 2, "repeat all": 2, "repetir todo": 2, "全曲リピート": 2, "alle wiederholen": 2, "tout répéter": 2, "सभी दोहराएं": 2, "列表循环": 2,
            "перемешать": 3, "shuffle": 3, "aleatorio": 3, "シャッフル": 3, "zufall": 3, "aléatoire": 3, "रैंडम": 3, "随机播放": 3,
        }
        for k, idx in canonical_map.items():
            if k in s:
                return idx
        return 0

    @staticmethod
    def _get_mode_values() -> list[str]:
        return [
            vp_i18n.t("mode_queue"),
            vp_i18n.t("mode_repeat_one"),
            vp_i18n.t("mode_repeat_all"),
            vp_i18n.t("mode_shuffle"),
        ]

    def _get_mode_index(self) -> int:
        if hasattr(self, "cmb_mode") and self.cmb_mode.current() >= 0:
            return self.cmb_mode.current()
        return self._parse_mode_to_idx(self.var_mode.get() if hasattr(self, "var_mode") else "")

    def _auto_next_index(self) -> int:
        mode_idx = self._get_mode_index()
        is_repeat_one = (mode_idx == 1)
        is_repeat_all = (mode_idx == 2)
        is_shuffle = (mode_idx == 3)
        if is_repeat_one and 0 <= self.current_idx < len(self.queue_items):
            self.queue_items[self.current_idx].played = False
            return self.current_idx
        if is_shuffle:
            cands = [i for i in range(len(self.queue_items))
                     if self._playable(i) and not self.queue_items[i].played and i != self.current_idx]
            if not cands:                      # все сыграны — начать круг заново
                for it in self.queue_items:
                    if self._source_allows(it): it.played = False
                cands = [i for i in range(len(self.queue_items))
                         if self._playable(i) and i != self.current_idx]
            return random.choice(cands) if cands else -1
        nxt = self._find_next(self.current_idx + 1)
        if nxt < 0 and is_repeat_all:
            for it in self.queue_items:
                if self._source_allows(it): it.played = False
            nxt = self._find_next(0)
        return nxt

    def _play_index(self, idx: int):
        if idx < 0 or idx >= len(self.queue_items): return
        item = self.queue_items[idx]
        if item.deleted or not self._source_allows(item): return
        if not item.audio_ready:
            self.current_idx = idx
            self._prefetch_around(idx)             # просили эту, а её ещё нет — рендерим без очереди
            self._render_list()
            return
        self.current_idx = idx
        if not getattr(item, "_counted_play", False):
            item._counted_play = True
            self._stats_items = int(getattr(self, "_stats_items", 0)) + 1
        item.played = True
        self.user_paused = False
        self._queue_end_notified = False
        self._prefetch_around(idx)                 # прогреть следующую, пока играет текущая
        self._apply_source_rate(item)
        try:
            self.player.set_volume(self.var_volume.get())
            with self._parts_lock:
                path = item.audio_path
                self.player.load_and_play(path, speed=self._speed_mult())
                self._loaded_item = item
                self._loaded_gen = self.player.generation() if hasattr(self.player, "generation") else 0
                # готовый целый файл — дозагружать нечего; иначе играем первую часть и дописываем
                first = item.parts_done and path == item.parts_done[0]
                item.parts_fed = 1 if first else len(item.parts_done)
            if first:
                threading.Thread(target=self._sync_parts, args=(item,), daemon=True, name="parts").start()
            if self._wave_id != item.id:           # пересчитать waveform под новый трек
                self._wave = self.player.envelope()
                self._wave_id = item.id
                if self._ab != [None, None]:
                    self._ab = [None, None]        # сброс A-B при смене трека
            if self._resume and item.raw_text == self._resume.get("text"):
                try: self.player.seek(float(self._resume.get("pos", 0)))
                except Exception: pass
                self._set_status(f"продолжаю с {self._fmt_time(self._resume.get('pos', 0))}", ok=True)
                self._resume = None                # одноразово
        except Exception as e:
            # сбой воспроизведения: пометить трек битым (иначе _tick зациклит),
            # снять плеер и без шторма перейти к следующей годной реплике
            item.audio_error = str(e)
            item.audio_ready = False
            try: self.player.stop()
            except Exception: pass
            self.btn_toggle.configure(text=self._g_play)
            self._play_fail_streak = getattr(self, "_play_fail_streak", 0) + 1
            log_line(f"play failed [{item.id}] streak={self._play_fail_streak}: {e}")
            self._render_list()
            if self._play_fail_streak >= 6:
                self.current_idx = -1
                self._set_status("аудио-движок не отвечает — перезапусти плеер", ok=False)
                return
            nxt = self._find_next(idx + 1)
            if nxt >= 0:
                self.root.after(150, lambda n=nxt: self._play_index(n))
            else:
                self.current_idx = -1   # дать новым репликам автоиграть
            return
        self._play_fail_streak = 0
        self.btn_toggle.configure(text=self._g_pause)
        self._render_list()
        self._update_text_panel()

    def _selected_idx(self) -> Optional[int]:
        sel = self.tree.selection()
        if not sel: return None
        try: return int(self.tree.item(sel[0], "values")[5])
        except (ValueError, IndexError): return None

    def _selected_indices(self) -> list:
        out = []
        for iid in self.tree.selection():
            try: out.append(int(self.tree.item(iid, "values")[5]))
            except (ValueError, IndexError): pass
        return out

    def _play_selected(self):
        idx = self._selected_idx()
        if idx is None: return
        if 0 <= idx < len(self.queue_items):
            it = self.queue_items[idx]
            if it.audio_error and not it.audio_ready:
                self._retry_render(it); return
        self._play_index(idx)

    def _retry_selected(self):
        idx = self._selected_idx()
        if idx is None: return
        if 0 <= idx < len(self.queue_items):
            self._retry_render(self.queue_items[idx])

    def _retry_render(self, item: Item):
        self._reset_render_state(item)
        log_line(f"manual retry [{item.id}] text='{item.text[:60]}'")
        self._q_put(item, 50)
        self._render_list()

    def _retry_all_failed(self):
        """Перерендерить все реплики со знаком ⚠ (упавшие)."""
        failed = [it for it in self.queue_items if it.audio_error and not it.audio_ready and not it.deleted]
        for it in failed:
            self._reset_render_state(it)
            self._q_put(it, 50)
        if failed:
            self._set_status(f"повтор {len(failed)} упавших", ok=True)
            self._render_list()
        else:
            self._set_status("упавших нет", ok=True)

    def _move_selected(self, delta: int):
        idx = self._selected_idx()
        if idx is None or not (0 <= idx < len(self.queue_items)):
            return "break"
        j = idx + delta
        while 0 <= j < len(self.queue_items) and self.queue_items[j].deleted:
            j += delta
        if not (0 <= j < len(self.queue_items)):
            return "break"
        self.queue_items[idx], self.queue_items[j] = self.queue_items[j], self.queue_items[idx]
        if self.current_idx == idx: self.current_idx = j
        elif self.current_idx == j: self.current_idx = idx
        self._render_list()
        for r in self.tree.get_children():
            try:
                if int(self.tree.item(r, "values")[5]) == j:
                    self.tree.selection_set(r); self.tree.see(r); break
            except (ValueError, IndexError):
                pass
        return "break"

    def _delete_selected(self):
        idxs = self._selected_indices()
        if not idxs:
            return
        cur_deleted = False
        for idx in idxs:
            if 0 <= idx < len(self.queue_items):
                self.queue_items[idx].deleted = True
                self.queue_items[idx].pinned = False   # удаление снимает закрепление
                if idx == self.current_idx:
                    cur_deleted = True
        if cur_deleted:
            try: self.player.stop()
            except Exception: pass
            self.btn_toggle.configure(text=self._g_play)
            self.current_idx = -1
        self._save_session()
        self._render_list()

    def _delete_played(self):
        """Удалить все прослушанные реплики (кроме закреплённых)."""
        n = 0
        for it in self.queue_items:
            if it.played and not it.deleted and not it.pinned and self._source_allows_base(it):
                it.deleted = True; n += 1
        if 0 <= self.current_idx < len(self.queue_items) and self.queue_items[self.current_idx].deleted:
            try: self.player.stop()
            except Exception: pass
            self.btn_toggle.configure(text=self._g_play)
            self.current_idx = -1
        self._set_status(f"удалено прослушанных: {n}", ok=True)
        self._render_list()

    def _source_allows_base(self, item: Item) -> bool:
        """Видна ли реплика по текущему фильтру источника (без поиска) — для массовых операций."""
        f = self.var_source.get()
        if f == "favorite":
            return bool(item.favorite)
        if f == "ai":
            return item.source in AI_SOURCES
        return f == "all" or item.source == f

    def _copy_selected(self):
        idx = self._selected_idx()
        if idx is None: return
        if 0 <= idx < len(self.queue_items):
            try:
                self.root.clipboard_clear()
                self.root.clipboard_append(self.queue_items[idx].text)
            except Exception: pass

    def _copy_full_text(self):
        item = getattr(self, "_panel_item", None)
        if item is None and 0 <= self.current_idx < len(self.queue_items):
            item = self.queue_items[self.current_idx]
        if item is not None:
            try:
                self.root.clipboard_clear()
                self.root.clipboard_append(self._panel_plain_text(item))
                self._set_status("текст скопирован в буфер обмена", ok=True)
            except Exception:
                pass

    def _show_text_panel_for_selected(self):
        idx = self._selected_idx()
        if idx is None: return
        if not self.var_show_text.get(): self._show_text_panel()
        if 0 <= idx < len(self.queue_items):
            self.current_idx = idx
            self._update_text_panel()

    def _open_audio_cache(self):
        try:
            _open_in_file_manager(AUDIO_DIR)
        except Exception as e:
            messagebox.showinfo(APP_NAME, f"Кэш: {AUDIO_DIR}\n({e})")

    def _show_context_menu(self, event):
        rowid = self.tree.identify_row(event.y)
        if rowid:
            # если клик по строке вне текущего выделения — выделить только её;
            # если по уже выделенной — сохранить мультивыбор
            if rowid not in self.tree.selection():
                self.tree.selection_set(rowid)
                self._on_tree_select()
            try: self.ctx_menu.tk_popup(event.x_root, event.y_root)
            finally: self.ctx_menu.grab_release()
        elif self.tree.selection():
            try: self.ctx_menu.tk_popup(event.x_root, event.y_root)
            finally: self.ctx_menu.grab_release()
        else:
            try: self._list_menu.tk_popup(event.x_root, event.y_root)
            finally: self._list_menu.grab_release()
        return "break"

    def _show_context_menu_for_row(self, event, rowid):
        if rowid:
            if rowid not in self.tree.selection():
                self.tree.selection_set(rowid)
                self._on_tree_select()
            try: self.ctx_menu.tk_popup(event.x_root, event.y_root)
            finally: self.ctx_menu.grab_release()
        return "break"

    def _on_tree_select(self, _event=None):
        idx = self._selected_idx()
        if idx is None or not (0 <= idx < len(self.queue_items)): return
        item = self.queue_items[idx]
        if item.audio_error:
            self.lbl_now_author.configure(text=("🤖 " if item.source in AI_SOURCES else "👥 ") + self._disp_author(item.author) + "  ⚠ ошибка рендера")
            self.lbl_now_time.configure(text=((item.date_str + " ") if item.date_str else "") + (item.time_str or ""))
            self.lbl_now_text.configure(
                text=f"{self._shown_text(item)[:300]}\n\n⚠ {item.audio_error}\n\nДвойной клик / Enter — перерендер."
            )
        if self.var_show_text.get():
            # показать выбранную в текст-панели даже если ещё не играет
            self._update_text_panel(force_item=item)
        self._update_row_waves()
        self._draw_seek()

    # ------------------ actions ------------------
    def action_toggle(self):
        if self.player.is_playing():
            self.player.pause()
            self.user_paused = True
            self.btn_toggle.configure(text=self._g_play)
            return
        if self.player.has_track() and self.player.is_paused():
            self.player.resume()
            self.user_paused = False
            self.btn_toggle.configure(text=self._g_pause)
            return
        idx = self._find_next(0)
        if idx >= 0: self._play_index(idx)

    def action_next(self):
        idx = self._find_next(self.current_idx + 1)
        if idx >= 0: self._play_index(idx)

    def action_prev(self):
        idx = self._find_prev(self.current_idx - 1)
        if idx >= 0:
            self.queue_items[idx].played = False
            self._play_index(idx)

    def _listen_all(self):
        """Хендс-фри: включить автопроигрывание и читать всё непрослушанное сверху вниз."""
        self.var_autoplay.set(True)
        self._save_prefs()
        idx = self._find_next(0)
        if idx >= 0:
            self._play_index(idx)
            self._set_status(vp_i18n.t("status_listening_all"), ok=True)
        else:
            self._set_status(vp_i18n.t("status_no_unheard"), ok=True)

    def action_repeat(self):
        if self.current_idx < 0:
            return
        # скорость живёт в плеере — достаточно переиграть с начала на текущем темпе
        self.queue_items[self.current_idx].played = True
        self._play_index(self.current_idx)

    # ------------------ render ------------------
    def _request_render(self):
        """Коалесцировать частые перерисовки списка (поток рендера/транскрибации)."""
        if self._render_after is not None:
            return
        self._render_after = self.root.after(80, self._do_render)

    def _do_render(self):
        self._render_after = None
        self._render_list()

    def _render_list(self):
        # обновить списки фильтров (автор/дата) под текущую очередь
        try:
            ALL_FILTER_VALUES = {"все", "all", "todos", "全部", "सभी", "すべて", "alle", "tous", ""}
            all_lbl = vp_i18n.t("filter_all")
            authors = sorted({self._disp_author(m.author) for m in self.queue_items if not m.deleted and m.author})
            self.cmb_author.configure(values=[all_lbl] + authors)
            cur_fa = self.var_filter_author.get().strip().lower()
            if cur_fa in ALL_FILTER_VALUES:
                self.var_filter_author.set(all_lbl)
            elif cur_fa not in [a.lower() for a in ([all_lbl] + authors)]:
                self.var_filter_author.set(all_lbl)

            dates = sorted({m.date_str for m in self.queue_items if not m.deleted and m.date_str})
            self.cmb_date.configure(values=[all_lbl] + dates)
            cur_fd = self.var_filter_date.get().strip().lower()
            if cur_fd in ALL_FILTER_VALUES:
                self.var_filter_date.set(all_lbl)
            elif cur_fd not in [d.lower() for d in ([all_lbl] + dates)]:
                self.var_filter_date.set(all_lbl)
        except Exception:
            pass
        for row in self.tree.get_children():
            self.tree.delete(row)
        self._iid_by_item_id = {}
        v = self._visible(for_display=True)
        cur_id = self.queue_items[self.current_idx].id if 0 <= self.current_idx < len(self.queue_items) else None
        cur_pos = 0
        total = len(v)
        pending_render = sum(1 for it in self.queue_items if not it.deleted and not it.audio_ready and not it.audio_error)
        cur_iid = None
        for v_pos, item in enumerate(v):
            tags: list[str] = []
            is_cur = item.id == cur_id
            if is_cur:
                tags.append("current"); cur_pos = v_pos + 1
            elif item.played:
                tags.append("done")
            if item.audio_error:
                tags.append("error")
            src_icon = ("🤖" if item.source in AI_SOURCES
                        else "👥" if item.source == "board"
                        else "✍" if item.source == "manual"
                        else "📂" if item.source == "file" else "·")
            if item.transcribing:
                ready_mark = " 🎙"
            else:
                ready_mark = " ⚠" if item.audio_error else ""
            marks = ("📌" if item.pinned else "") + ("⭐" if item.favorite else "") \
                    + ("🔇" if item.author in self._muted else "") \
                    + ("🔔" if item.author in self._watch else "") + tag_for(item.text)
            real_idx = self.queue_items.index(item)
            iid = self.tree.insert("", "end", values=(
                src_icon, (marks + " " if marks else "") + self._disp_author(item.author)[:24],
                item.date_str or "",
                (item.time_str or "") + ready_mark,
                self._shown_text(item)[:120], real_idx,
            ), tags=tuple(tags))
            self._iid_by_item_id[item.id] = iid
            if is_cur: cur_iid = iid

        # счётчик
        pending_label = f" · {pending_render}" if pending_render else ""
        self.lbl_qcount.configure(text=(f"{cur_pos} {vp_i18n.t('of')} {total}{pending_label}" if total else f"0{pending_label}"))
        # непрослушанное по источникам
        try:
            un_ai = sum(1 for m in self.queue_items
                        if not m.deleted and not m.played and m.source in AI_SOURCES)
            un_board = sum(1 for m in self.queue_items
                           if not m.deleted and not m.played and m.source == "board")
            parts = []
            if un_ai: parts.append(f"🤖 {un_ai}")
            if un_board: parts.append(f"👥 {un_board}")
            self.lbl_unread.configure(text=("  ".join(parts)))
        except Exception:
            pass

        # авто-прокрутка
        if cur_iid and self.var_autoscroll.get():
            try: self.tree.see(cur_iid)
            except Exception: pass

        # «сейчас»
        if 0 <= self.current_idx < len(self.queue_items):
            it = self.queue_items[self.current_idx]
            if not it.audio_error:
                self.lbl_now_author.configure(text=("🤖 " if it.source in AI_SOURCES else "👥 ") + self._disp_author(it.author))
                self.lbl_now_time.configure(text=((it.date_str + " ") if it.date_str else "") + (it.time_str or ""))
                flat = " ".join(self._shown_text(it).split())   # тизер в 1–2 строки (без дубля полной панели)
                preview = flat[:120] + ("…" if len(flat) > 120 else "")
                self.lbl_now_text.configure(text=preview)
        elif v:
            it = v[0]
            self.lbl_now_author.configure(text=("🤖 " if it.source in AI_SOURCES else "👥 ") + self._disp_author(it.author))
            self.lbl_now_time.configure(text=((it.date_str + " ") if it.date_str else "") + (it.time_str or ""))
            flat = " ".join(self._shown_text(it).split())
            self.lbl_now_text.configure(text=flat[:120] + ("…" if len(flat) > 120 else ""))
        else:
            self.lbl_now_author.configure(text="Готов к озвучке")
            self.lbl_now_time.configure(text="")
            self.lbl_now_text.configure(text="Ожидание новых реплик или вставки текста…")
        # обновить полный текст
        self._update_text_panel()
        self._update_mini()
        self._update_row_waves()
        self._schedule_wave_anim()

    def _update_text_panel(self, force_item: Optional[Item] = None):
        if not self.var_show_text.get(): return
        if getattr(self, "_editing_text", False): return   # не перетирать правки пользователя
        item = force_item or (self.queue_items[self.current_idx] if 0 <= self.current_idx < len(self.queue_items) else None)
        if item is None:
            sel_idx = self._selected_idx()
            if sel_idx is not None and 0 <= sel_idx < len(self.queue_items) and not self.queue_items[sel_idx].deleted:
                item = self.queue_items[sel_idx]
            else:
                vis = self._visible(for_display=True)
                if vis:
                    item = vis[0]
        self._panel_item = item
        # Плашка с запросом пользователя (для диалогов Antigravity / Codex)
        try:
            if item and getattr(item, "prompt", ""):
                prompt_flat = " ".join(item.prompt.split())
                if len(prompt_flat) > 220:
                    prompt_flat = prompt_flat[:220] + "…"
                self.lbl_now_prompt.configure(text=f"💬 {vp_i18n.t('prompt_label')}: {prompt_flat}")
                self.prompt_frame.grid()
            else:
                self.prompt_frame.grid_remove()
        except Exception:
            pass
        if item and not item.audio_error:
            try:
                self.lbl_now_author.configure(text=("🤖 " if item.source in AI_SOURCES else "👥 ") + self._disp_author(item.author))
                self.lbl_now_time.configure(text=((item.date_str + " ") if item.date_str else "") + (item.time_str or ""))
            except Exception:
                pass
        self._text_body_offset = 0
        txt = err = ""
        if item:
            # привести к новому формату (таблицы → читаемые строки) — чинит и старые реплики;
            # для уже-чистых новых функция идемпотентна и ничего не меняет
            try:
                fixed = _clean_for_tts(item.raw_text)
                if fixed and fixed != item.text:
                    item.text = fixed
                    item.marks = []; item.word_marks = []   # тайминги устарели — подсветка off до перерендера
            except Exception:
                pass
            stamp = " / ".join(p for p in (item.date_str, item.time_str) if p)
            header = f"[{self._disp_author(item.author)}" + (f" · {stamp}" if stamp else "") + "]\n"
            prefix = header + "\n"
            self._text_body_offset = len(prefix)     # с этого символа начинается тело реплики
            err = f"\n\n⚠ {item.audio_error}" if item.audio_error else ""
            txt = prefix
        key = (id(item), item.text if item else "", getattr(item, "prompt", "") if item else "", txt, err if item else "", self.col_panel, self.col_ink)
        if key == getattr(self, "_panel_key", None):  # та же реплика — не сбрасывать прокрутку и выделение
            return
        self._panel_key = key
        for w in getattr(self, "_panel_widgets", []):
            w.destroy()
        self._panel_widgets = []
        self._hl_cell = None
        self.text_widget.delete("1.0", "end")        # программная вставка работает и без state-переключений
        self.text_widget.insert("1.0", txt)
        self._panel_segs = []
        if item:
            try:
                segs = vp_layout.document_segments(item.text, item.raw_text, _clean_for_tts)
            except Exception as e:
                log_line(f"panel layout error: {e}")
                segs = vp_layout._plain_fallback(item.text)
            self._panel_segs = segs
            self._panel_configure_tags()
            for seg in segs:
                if seg["kind"] == "text":
                    self._panel_insert_text(seg)
                else:
                    self._panel_insert_table(seg)
            self.text_widget.insert("end-1c", err)
        else:
            onboarding = (
                "Очередь пока пуста.\n\n"
                "• Диалоги и ответы из Antigravity, Codex и терминального Codex подтягиваются автоматически.\n"
                "• Нажмите «🔄 Диалоги» вверху слева, чтобы подтянуть последние ответы прямо сейчас.\n"
                "• Нажмите Ctrl+Alt+S в любом окне, чтобы озвучить выделенный текст.\n"
                "• Или нажмите «+ Текст / файл» слева, чтобы вставить свой текст или аудио."
            )
            self.text_widget.insert("1.0", onboarding)
        try: self.text_widget.edit_reset()           # чистим историю undo (правка не откатывает в прошлую реплику)
        except Exception: pass
        self._hl_range = None; self._hl_word = None  # сбросить подсветку при смене реплики

    def _toggle_edit_text(self):
        if not self.var_show_text.get():
            self._show_text_panel()
        item = getattr(self, "_panel_item", None)
        if item is None:
            return
        if not self._editing_text:
            # включить правку
            self._editing_text = True
            self.btn_edit_text.configure(text="💾 Применить")
            self.text_widget.configure(state="normal")
            # правим оригинал с его разметкой, иначе правка потеряла бы вёрстку
            for w in getattr(self, "_panel_widgets", []):
                w.destroy()
            self._panel_widgets = []
            self._panel_segs = []
            self._panel_key = None
            self.text_widget.delete(f"1.0+{self._text_body_offset}c", "end")
            self.text_widget.insert("end-1c", normalize_tables(item.raw_text) if self._has_source(item) else item.text)
            self.text_widget.focus_set()
        else:
            # применить правки: тело после заголовка → item.text
            base = getattr(self, "_text_body_offset", 0)
            try:
                body = self.text_widget.get(f"1.0+{base}c", "end").strip()
            except Exception:
                body = self.text_widget.get("1.0", "end").strip()
            item.raw_text = body
            item.text = _clean_for_tts(body) or body
            item.marks = []; item.word_marks = []   # правка ломает тайминги — подсветка off до перерендера
            self._panel_key = None
            self._editing_text = False
            self.btn_edit_text.configure(text="✏ Правка")
            self._update_text_panel(force_item=item)
            self._render_list()
            self._set_status("правки применены", ok=True)

    def _change_text_font(self, delta: int):
        self._text_font_size = max(8, min(28, self._text_font_size + delta))
        try:
            self.text_font.configure(size=self._text_font_size)
            self.text_font_bold.configure(size=self._text_font_size)
            if hasattr(self, "text_font_heading"):
                self.text_font_heading.configure(size=self._text_font_size + 2)
                self.text_font_italic.configure(size=self._text_font_size)
                self.text_font_code.configure(size=self._text_font_size)
            self._panel_relayout()
        except Exception:
            pass
        self.prefs["text_font_size"] = self._text_font_size
        return "break"

    def _scroll_to_current(self):
        if 0 > self.current_idx or self.current_idx >= len(self.queue_items): return
        cur_iid = None
        for iid in self.tree.get_children():
            vals = self.tree.item(iid, "values")
            try:
                if int(vals[5]) == self.current_idx: cur_iid = iid; break
            except (ValueError, IndexError):
                continue
        if cur_iid:
            self.tree.see(cur_iid)
            self.tree.selection_set(cur_iid)

    # ------------------ мини-плеер (поверх всех окон) ------------------
    def _enter_mini(self):
        if self.mini is not None and self.mini.winfo_exists():
            return
        cc = getattr(self, "colors", None) or self._palette(self.var_theme.get())
        bg, panel, panel2, ink, mute, signal = cc["bg"], cc["panel"], cc["panel2"], cc["ink"], cc["mute"], cc["signal"]
        active = cc["active"]
        sig_fg = self._on_signal_fg(signal)
        self.root.withdraw()
        m = tk.Toplevel(self.root)
        self.mini = m
        m.title("Voice Player")
        m.overrideredirect(True)
        m.attributes("-topmost", True)
        m.configure(bg=bg)

        frame = tk.Frame(m, bg=panel, highlightthickness=1, highlightbackground=cc["hairline"])
        frame.pack(fill="both", expand=True, padx=1, pady=1)

        row_main = tk.Frame(frame, bg=panel)
        row_main.pack(fill="x", padx=10, pady=(8, 4))

        gt, ft = self._ic_glyph("play", 15)
        self.mini_toggle = tk.Button(row_main, text=gt, font=ft, bd=0, bg=signal, fg=sig_fg,
                                     activebackground=signal, activeforeground=sig_fg,
                                     width=3, cursor="hand2", command=self.action_toggle)
        self.mini_toggle.pack(side="left", padx=(0, 8), ipady=3)
        self._tt(self.mini_toggle, "Пауза / продолжить (Ctrl+Alt+Space)")

        # Сначала фиксируем правый блок кнопок (⏭ и ⤢), чтобы длинный текст никогда не выталкивал их за край
        right_btns = tk.Frame(row_main, bg=panel)
        right_btns.pack(side="right", padx=(6, 0))
        gn, fn = self._ic_glyph("next", 13)
        self._tt(tk.Button(right_btns, text=gn, font=fn, bd=0, bg=panel2, fg=ink, activebackground=active,
                 activeforeground=ink, width=3, cursor="hand2", command=self.action_next),
                 "Следующая (Ctrl+Alt+N)").pack(side="left", padx=2, ipady=2)
        gr, fr = self._ic_glyph("restore", 12)
        self._tt(tk.Button(right_btns, text=gr, font=fr, bd=0, bg=panel2, fg=ink, activebackground=active,
                 activeforeground=ink, width=3, cursor="hand2", command=self._exit_mini),
                 "Развернуть окно (Ctrl+Alt+P / двойной клик)").pack(side="left", padx=2, ipady=2)

        mid_col = tk.Frame(row_main, bg=panel)
        mid_col.pack(side="left", fill="x", expand=True)
        self.mini_author = tk.Label(mid_col, text="—", bg=panel, fg=mute,
                                    font=("Segoe UI Semibold", 8), anchor="w")
        self.mini_author.pack(fill="x")
        self.mini_text = tk.Label(mid_col, text="", bg=panel, fg=ink,
                                  font=("Segoe UI Semibold", 9), anchor="w", justify="left")
        self.mini_text.pack(fill="x")

        # тонкая прогресс-полоса (видно, сколько осталось) + клик = перемотка
        self.mini_seek = tk.Canvas(frame, height=4, bg=cc["hairline"], highlightthickness=0,
                                   bd=0, cursor="hand2")
        self.mini_seek.pack(fill="x", padx=10, pady=(2, 3))
        self.mini_seek.bind("<Button-1>", self._mini_seek_click)

        self.mini_hint = tk.Label(frame, text="Ctrl+Alt+P или 2×клик — развернуть · Ctrl+Alt+S — озвучить выделенное",
                                  bg=panel, fg=mute, font=("Segoe UI", 7), anchor="w")
        self.mini_hint.pack(fill="x", padx=10, pady=(0, 5))

        # перетаскивание за любую «не-кнопочную» область + двойной клик = развернуть
        for w in (frame, row_main, mid_col, self.mini_author, self.mini_text, self.mini_hint):
            w.bind("<Button-1>", self._mini_press)
            w.bind("<B1-Motion>", self._mini_drag)
            w.bind("<Double-Button-1>", lambda _e: self._exit_mini())

        # позиция: сохранённая или у правого края экрана
        geo = self.mini_geometry or self.prefs.get("mini_geometry", "")
        m.update_idletasks()
        if geo and "+" in geo:
            pos = geo[geo.index("+"):]
            m.geometry(f"380x98{pos}")
        else:
            sw = m.winfo_screenwidth()
            m.geometry(f"380x98+{sw - 404}+48")
        self._update_mini()

    def _mini_press(self, e):
        self._mini_dx = e.x_root - self.mini.winfo_x()
        self._mini_dy = e.y_root - self.mini.winfo_y()

    def _mini_drag(self, e):
        x = e.x_root - self._mini_dx
        y = e.y_root - self._mini_dy
        self.mini.geometry(f"+{x}+{y}")
        self.mini_geometry = self.mini.geometry()

    def _exit_mini(self):
        try:
            if self.mini is not None and self.mini.winfo_exists():
                self.mini_geometry = self.mini.geometry()
                self.mini.destroy()
        except Exception:
            pass
        self.mini = None
        self.root.deiconify()
        self.root.lift()
        self._save_prefs()

    def _update_mini(self):
        if self.mini is None or not self.mini.winfo_exists():
            return
        dur = self.player.duration() if self.player.has_track() else 0
        pos = self.player.position() if self.player.has_track() else 0
        rem_str = ""
        if dur > 0:
            rem = max(0, int(dur - pos))
            rem_str = f" · осталось {rem // 60}:{rem % 60:02d}"
        if 0 <= self.current_idx < len(self.queue_items):
            it = self.queue_items[self.current_idx]
            icon = "🤖 " if it.source in AI_SOURCES else "👥 "
            auth = self._disp_author(it.author) or "—"
            max_auth = 22 if rem_str else 34
            if len(auth) > max_auth:
                auth = auth[:max_auth - 1] + "…"
            self.mini_author.configure(text=icon + auth + rem_str)
            shown = " ".join(self._shown_text(it).split())
            self.mini_text.configure(text=shown[:34] + ("…" if len(shown) > 34 else ""))
        else:
            self.mini_author.configure(text="VoicePlayer · ожидание")
            self.mini_text.configure(text="Жду новых реплик…")
        try:
            if self.player.is_playing():
                self.mini_toggle.configure(text=self._ic_glyph("pause", 15)[0])
            else:
                self.mini_toggle.configure(text=self._ic_glyph("play", 15)[0])
        except Exception:
            pass
        # прогресс-полоса (или волновая анимация во время рендера)
        try:
            if self.mini_seek.winfo_exists():
                self.mini_seek.delete("all")
                w = self.mini_seek.winfo_width()
                cur_it = self.queue_items[self.current_idx] if 0 <= self.current_idx < len(self.queue_items) else None
                if w > 1 and not self.player.has_track() and (self._is_item_rendering(cur_it) or self._any_rendering()):
                    self._draw_wave_slider_strip(
                        self.mini_seek, w, 4, self._wave_phase,
                        self.col_panel, self.col_hairline, self.col_signal, active=True
                    )
                elif w > 1 and dur > 0:
                    frac = min(1.0, pos / dur)
                    self.mini_seek.create_rectangle(0, 0, int(w * frac), 4,
                                                    fill=self.col_signal, width=0, tags="prog")
                    if cur_it is not None and getattr(cur_it, "more_parts", False):
                        tail_x = max(int(w * frac) + 6, int(w * 0.35))
                        if tail_x < w - 10:
                            self._draw_wave_slider_strip(
                                self.mini_seek, w, 4, self._wave_phase,
                                self.col_panel, self.col_hairline, self.col_signal,
                                active=True, x_start=tail_x
                            )
        except Exception:
            pass

    def _mini_seek_click(self, e):
        """Клик по полосе мини-плеера → перемотка на эту долю реплики."""
        try:
            w = self.mini_seek.winfo_width()
            if w > 1 and self.player.has_track():
                frac = max(0.0, min(1.0, e.x / w))
                self.player.seek(frac * self.player.duration())
                self.user_paused = False
                self.btn_toggle.configure(text=self._g_pause)
                self._update_mini()
        except Exception:
            pass
        return "break"

    def _load_history(self):
        """Подгрузить последние 20 сообщений с доски (без авто-играть)."""
        try:
            if not DB_PATH.exists():
                messagebox.showinfo(APP_NAME, f"База доски не найдена:\n{DB_PATH}")
                return
            conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=2.0)
            try:
                rows = conn.execute(
                    "SELECT content, created_at FROM memory_entries "
                    "WHERE namespace='team-chat' AND status='active' "
                    "ORDER BY created_at DESC LIMIT 20",
                ).fetchall()
            finally:
                conn.close()
            rows.reverse()
            was_auto = self.var_autoplay.get()
            self.var_autoplay.set(False)
            try:
                for content, created_at in rows:
                    raw = str(content or "")
                    author, time_str, body = parse_message(raw)
                    self.enqueue(source="board", author=author,
                                 time_str=time_str or time.strftime("%H:%M", time.localtime(created_at / 1000)),
                                 text=body)
            finally:
                self.var_autoplay.set(was_auto); self._save_prefs()
        except Exception as e:
            messagebox.showerror(APP_NAME, f"Не удалось загрузить историю:\n{e}")

    # ------------------ загрузка аудиофайла + транскрибация ------------------
    def _setup_tray(self):
        if not IS_WIN:
            return
        try:
            import pystray
        except Exception as e:
            log_line(f"tray unavailable: {e}"); return
        try:
            img = self._draw_modern_app_icon(64)
        except Exception as e:
            log_line(f"tray icon draw error: {e}"); return
        menu = pystray.Menu(
            pystray.MenuItem("Показать", lambda *a: self.root.after(0, self._tray_show), default=True),
            pystray.MenuItem("Пауза / продолжить", lambda *a: self.root.after(0, self.action_toggle)),
            pystray.MenuItem("Следующая", lambda *a: self.root.after(0, self.action_next)),
            pystray.MenuItem("Выход", lambda *a: self.root.after(0, self._tray_quit)),
        )
        try:
            self._tray = pystray.Icon("voiceplayer", img, "VoicePlayer", menu)
            threading.Thread(target=self._tray.run, daemon=True, name="tray").start()
            log_line("tray icon enabled")
        except Exception as e:
            self._tray = None
            log_line(f"tray init failed: {e}")

    def _tray_show(self):
        self._show_window()

    def _show_window(self):
        """Вывести окно на передний план (трей, повторный запуск ярлыка, горячая клавиша)."""
        try:
            if self.mini is not None and self.mini.winfo_exists():
                self._exit_mini()
            self.root.deiconify(); self.root.state("normal")
            self.root.attributes("-topmost", True)   # Windows не даёт фокус фоновым окнам без этого
            self.root.lift(); self.root.focus_force()
            self.root.after(150, lambda: self.root.attributes("-topmost", False))
        except Exception:
            pass

    def _toggle_window(self):
        """Ctrl+Alt+P: показать плеер, а если он уже перед глазами — свернуть."""
        try:
            focused = self.root.focus_displayof() is not None
            if self.root.state() == "normal" and focused and self.mini is None:
                self.root.iconify()
            else:
                self._show_window()
        except Exception:
            self._show_window()

    # --- нейросети: найти установленные и подключить ---
    def _refresh_ai_badge(self):
        """Плашка в шапке: какие нейросети найдены, но ещё не подключены."""
        disabled = set(self.prefs.get("disabled_ai_tools", []))
        try:
            tools = vp_integrations.detect()
            any_connected = any(t["connected"] for t in tools)
            pending = [
                t["label"] for t in tools
                if t["installed"] and not t["connected"] and t["id"] not in disabled
                and not (t["id"] == "gemini" and any_connected)
            ]
        except Exception as e:
            log_line(f"ai detect error: {e}")
            pending = []
        if pending:
            self.lbl_ai.configure(text="🤖 Подключить: " + ", ".join(pending))
            self.lbl_ai.grid()
        else:
            self.lbl_ai.configure(text="")
            self.lbl_ai.grid_remove()

    def _show_update_banner(self, version: str):
        if not version or getattr(self, "_update_banner_shown", False):
            return
        if getattr(self, "_update_banner_widget", None) is not None:
            return
        self._update_banner_shown = True

        c = getattr(self, "colors", self._palette(self.var_theme.get() if hasattr(self, "var_theme") else "Тёмная"))
        bg = c.get("panel2", "#1c211f")
        sig = c.get("signal", "#7ee2b8")
        ink = c.get("ink", "#e8ecea")
        hair = c.get("hairline", "#232927")
        mute = c.get("mute", "#7c8782")
        pri_fg = self._on_signal_fg(sig)

        banner = tk.Frame(self.root, bg=bg, highlightthickness=1, highlightbackground=hair, padx=14, pady=8)
        self._update_banner_widget = banner
        try:
            banner.pack(side="top", fill="x", before=self.body)
        except Exception:
            banner.pack(side="top", fill="x")

        left_box = tk.Frame(banner, bg=bg)
        left_box.pack(side="left", fill="x", expand=True)

        lbl_icon = tk.Label(left_box, text="🚀", bg=bg, font=self._f(12))
        lbl_icon.pack(side="left", padx=(0, 8))

        lbl_title = tk.Label(left_box, text=f"Доступно обновление VoicePlayer v{version}",
                             bg=bg, fg=ink, font=self._f_semi(10))
        lbl_title.pack(side="left")

        lbl_desc = tk.Label(left_box, text=" — обновление скачано в фоне и готово к установке.",
                            bg=bg, fg=mute, font=self._f(9))
        lbl_desc.pack(side="left", padx=(2, 0))

        right_box = tk.Frame(banner, bg=bg)
        right_box.pack(side="right")

        btn_restart = tk.Button(
            right_box, text="Перезагрузить для обновления", bg=sig, fg=pri_fg,
            activebackground=self._shade(sig, -0.08), activeforeground=pri_fg,
            relief="flat", bd=0, padx=12, pady=5, font=self._f_semi(9), cursor="hand2",
            command=self._apply_update_now
        )
        btn_restart.pack(side="right", padx=(8, 0))

        btn_later = tk.Button(
            right_box, text="Позже", bg=bg, fg=mute,
            activebackground=c.get("active", "#262e2b"), activeforeground=ink,
            relief="flat", bd=0, padx=8, pady=5, font=self._f(9), cursor="hand2",
            command=self._dismiss_update_banner
        )
        btn_later.pack(side="right")

    def _dismiss_update_banner(self):
        w = getattr(self, "_update_banner_widget", None)
        if w is not None:
            try:
                w.destroy()
            except Exception:
                pass
            self._update_banner_widget = None

    def _restyle_update_banner(self):
        w = getattr(self, "_update_banner_widget", None)
        if w is not None and w.winfo_exists():
            v = getattr(self, "_update_ready", "")
            self._dismiss_update_banner()
            self._update_banner_shown = False
            if v:
                self._show_update_banner(v)

    def _apply_update_now(self):
        self._dismiss_update_banner()
        try:
            self._autosave_session()
        except Exception:
            pass
        self._apply_update()

    def _build_update_tab(self, tab):
        ttk.Label(tab, text=f"{vp_i18n.t('version')} {APP_VERSION}", style="Hero.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w")
        var = tk.BooleanVar(value=self._auto_update)

        def toggle():
            self._auto_update = bool(var.get())
            self._save_prefs()

        ttk.Checkbutton(tab, text=vp_i18n.t("auto_update"), variable=var, command=toggle).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(10, 0))
        ttk.Label(tab, wraplength=580, style="Dim.TLabel",
                  text=vp_i18n.t("auto_update_desc")).grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))
        btn_box = ttk.Frame(tab)
        btn_box.grid(row=3, column=0, columnspan=2, sticky="w", pady=(12, 0))
        ttk.Button(btn_box, text=vp_i18n.t("check_updates_now"),
                   command=lambda: threading.Thread(target=self._check_update, args=(True,), daemon=True,
                                                    name="update-check").start()).pack(side="left")
        if getattr(self, "_update_ready", ""):
            ttk.Button(btn_box, text=f"🚀 Перезагрузить сейчас (v{self._update_ready})", style="Primary.TButton",
                       command=self._apply_update_now).pack(side="left", padx=(10, 0))

    def _update_loop(self):
        if self.stop_event.wait(UPDATE_FIRST_S):
            return
        while True:
            if self._auto_update:
                self._check_update(False)
            if self.stop_event.wait(UPDATE_EVERY_S):
                return

    def _check_update(self, manual: bool):
        """В фоне: ошибки сети не беспокоят, ручная проверка отвечает всегда."""
        def status(text, ok=True):
            self.root.after(0, lambda: self._set_status(text, ok=ok))
        try:
            version = vp_updater.check_and_install(APP_DIR, log_line)
        except Exception as e:
            log_line(f"update: {type(e).__name__}: {e}")
            if manual:
                status(f"обновление: {e}"[:120], ok=False)
            return
        if version:
            self._update_ready = version
            status(f"новая версия {version} скачана и готова к установке", ok=True)
            self.root.after(0, lambda: self._show_update_banner(version))
        elif manual:
            if self._update_ready:
                status(f"версия {self._update_ready} уже скачана и готова к установке", ok=True)
                self.root.after(0, lambda: self._show_update_banner(self._update_ready))
            else:
                status(f"установлена последняя версия ({APP_VERSION})")

    def _user_idle_s(self) -> float:
        if not IS_WIN:
            return float("inf")
        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
        info = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return 0.0
        return ((ctypes.windll.kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000.0

    def _can_restart_now(self) -> bool:
        return (not self.player.is_playing() and not self.player.is_paused()
                and self.render_queue.empty() and self._trans_queue.empty()
                and not self.tts_input.get("1.0", "end").strip()
                and self._user_idle_s() >= UPDATE_IDLE_S)

    def _update_tick(self):
        if self.stop_event.is_set():
            return
        try:
            on_disk = vp_updater.read_version(APP_DIR)          # копию обновили на месте, например через git
            if (not self._update_ready and self._auto_update
                    and vp_updater.parse_version(on_disk) > vp_updater.parse_version(APP_VERSION)):
                self._update_ready = on_disk
            best = vp_updater.best_installed(vp_updater.parse_version(APP_VERSION))
            if best and not self._update_ready:
                self._update_ready = vp_updater.read_version(best)
            if self._update_ready and not getattr(self, "_update_banner_shown", False):
                self._show_update_banner(self._update_ready)
            if self._update_ready and self._auto_update and self._can_restart_now():
                self._apply_update()
                return
        except Exception as e:
            log_line(f"update tick: {e}")
        self.root.after(5000, self._update_tick)

    def _apply_update(self):
        log_line(f"update: перезапуск на версию {self._update_ready}")
        try:
            self._autosave_session()
        except Exception:
            pass
        hidden = self.root.state() != "normal"
        vp_updater.restart(APP_DIR, ["--background"] if hidden else [])
        self.on_close()

    def _is_supporter(self) -> bool:
        return verify_supporter_key(getattr(self, "_supporter_key", ""))

    def _format_stats_summary(self) -> str:
        secs = int(max(0.0, getattr(self, "_stats_seconds", 0.0)))
        items = int(max(0, getattr(self, "_stats_items", 0)))
        hrs, rem = divmod(secs, 3600)
        mins = rem // 60
        m_lbl = vp_i18n.t("min_short")
        if hrs > 0:
            t_str = f"{hrs}h {mins}{m_lbl}"
        elif mins > 0:
            t_str = f"{mins} {m_lbl}"
        else:
            t_str = f"{secs}s"
        return vp_i18n.t("stats_spoken_for_you", time=t_str, count=items)

    def _trigger_telemetry_ping(self):
        t = getattr(self, "telemetry", None)
        if not t:
            return
        try:
            sources = [it.source for it in getattr(self, "queue_items", [])[:20] if hasattr(it, "source")]
            t.ping_async(
                total_seconds=getattr(self, "_stats_seconds", 0.0),
                total_items=getattr(self, "_stats_items", 0),
                active_sources=sources,
                voice=getattr(self, "_voice", "piper:dmitri"),
                lang=self.var_lang.get() if hasattr(self, "var_lang") else "ru"
            )
        except Exception as e:
            log_line(f"telemetry ping: {e}")

    def _trigger_sponsor_check(self):
        """Проверить удалённый манифест спонсоров/партнёров."""
        if self._is_supporter():
            return
        t = getattr(self, "telemetry", None)
        if not t:
            return
        t.fetch_sponsor_campaign_async(
            on_sponsor_loaded=lambda data: self.root.after(0, lambda: self._apply_sponsor_campaign(data))
        )

    def _apply_sponsor_campaign(self, data: dict):
        if not data or not data.get("active") or self._is_supporter():
            return
        cid = data.get("campaign_id", "")
        dismissed = self.prefs.get("dismissed_sponsors", [])
        if cid and cid in dismissed:
            return
        if not hasattr(self, "sponsor_card"):
            return
        self._current_sponsor_data = data
        self.lbl_sponsor_badge.configure(text=data.get("badge", "Партнёр"))
        self.lbl_sponsor_title.configure(text=data.get("title", ""))
        self.lbl_sponsor_desc.configure(text=data.get("desc", ""))
        url = data.get("url", "https://infostroper-cloud.github.io/voice-player/")
        self.btn_sponsor_action.configure(
            text=data.get("btn_text", "Подробнее →"),
            command=lambda: webbrowser.open(url)
        )
        self.sponsor_card.grid()

    def _dismiss_sponsor(self):
        cid = getattr(self, "_current_sponsor_data", {}).get("campaign_id", "")
        if cid:
            dismissed = list(self.prefs.get("dismissed_sponsors", []))
            if cid not in dismissed:
                dismissed.append(cid)
            self.prefs["dismissed_sponsors"] = dismissed
            self._save_prefs()
        if hasattr(self, "sponsor_card"):
            self.sponsor_card.grid_remove()

    def _restyle_sponsor_card(self, c: dict):
        if not hasattr(self, "sponsor_card") or not self.sponsor_card.winfo_exists():
            return
        panel2 = c.get("panel2", "#1c211f")
        hairline = c.get("hairline", "#232927")
        ink = c.get("ink", "#e8ecea")
        mute = c.get("mute", "#7c8782")
        signal = c.get("signal", "#7ee2b8")
        self.sponsor_card.configure(bg=panel2, highlightbackground=hairline)
        for child in self.sponsor_card.winfo_children():
            try:
                child.configure(bg=panel2)
            except Exception:
                pass
        self.lbl_sponsor_title.configure(bg=panel2, fg=ink)
        self.lbl_sponsor_desc.configure(bg=panel2, fg=mute)
        self.lbl_sponsor_badge.configure(fg=signal)

    def _maybe_show_support_reminder(self):
        """Ненавязчивое окно в стиле ненавязчивой поддержки: не чаще раза в 7 дней после ощутимой пользы,
        только когда плеер молчит и пользователь не активировал Supporter-ключ."""
        if self._is_supporter():
            return
        if self.player.is_playing() or self._any_rendering():
            return
        if self._stats_items < 30 and self._stats_seconds < 7200:
            return
        now = time.time()
        if now - getattr(self, "_last_support_prompt_ts", 0.0) < 7 * 86400:
            return
        self._last_support_prompt_ts = now
        self._save_prefs()
        self._open_support_dialog(is_reminder=True)

    def _open_support_dialog(self, is_reminder: bool = False):
        """Окно «О проекте / Поддержать VoicePlayer» (модель Supporter + Supporter-ключ)."""
        win = getattr(self, "_support_win", None)
        if win is not None and win.winfo_exists():
            win.lift()
            win.focus_force()
            return
        cc = getattr(self, "colors", None) or self._palette(self.var_theme.get())
        w = tk.Toplevel(self.root)
        self._support_win = w
        w.title(vp_i18n.t("about_win_title"))
        w.configure(bg=cc["panel"])
        self._center_over_root(w, 560, 420)
        w.transient(self.root)

        box = ttk.Frame(w, padding=18)
        box.pack(fill="both", expand=True)
        box.columnconfigure(0, weight=1)

        title_txt = (vp_i18n.t("about_saved_time") if is_reminder
                     else f"VoicePlayer {APP_VERSION}  ·  {vp_i18n.t('app_tagline')}")
        ttk.Label(box, text=title_txt, style="Hero.TLabel").grid(row=0, column=0, sticky="w")

        # Карточка со статистикой пользы
        stat_card = ttk.Frame(box, style="Card.TFrame", padding=(12, 10))
        stat_card.grid(row=1, column=0, sticky="ew", pady=(10, 10))
        ttk.Label(stat_card, text="📊  " + self._format_stats_summary(),
                  style="Prompt.TLabel").pack(anchor="w")

        desc = vp_i18n.t("about_desc")
        ttk.Label(box, text=desc, style="TLabel", wraplength=510, justify="left").grid(
            row=2, column=0, sticky="w", pady=(0, 12))

        donate_url = os.environ.get("VP_DONATE_URL", "https://t.me/tribute/app?startapp=dTZW")
        boosty_url = "https://boosty.to/tikoulf/posts/2b4dd6a8-157f-4202-b8db-b4e019ae8a02"
        pro_key_url = "https://web.tribute.tg/p/K1s"
        tg_channel_url = "https://t.me/voiceplayer_ai"
        btn_row = ttk.Frame(box)
        btn_row.grid(row=3, column=0, sticky="ew", pady=(0, 14))
        ttk.Button(btn_row, text="♥  " + vp_i18n.t("donate_tg"), style="Primary.TButton",
                   command=lambda: webbrowser.open(donate_url)).pack(side="left", padx=(0, 6))
        ttk.Button(btn_row, text="🧡  Boosty (★ Pro)", style="Toggle.TButton",
                   command=lambda: webbrowser.open(boosty_url)).pack(side="left", padx=(0, 6))
        ttk.Button(btn_row, text="★  " + vp_i18n.t("pro_key"), style="Toggle.TButton",
                   command=lambda: webbrowser.open(pro_key_url)).pack(side="left", padx=(0, 6))
        ttk.Button(btn_row, text="✈️ " + vp_i18n.t("channel"), style="Toggle.TButton",
                   command=lambda: webbrowser.open(tg_channel_url)).pack(side="left", padx=(0, 6))
        ttk.Button(btn_row, text=vp_i18n.t("later"), style="Toggle.TButton",
                   command=w.destroy).pack(side="left")

        # Поле активации ключа Supporter
        key_frame = ttk.Frame(box)
        key_frame.grid(row=4, column=0, sticky="ew", pady=(4, 0))
        key_frame.columnconfigure(1, weight=1)
        ttk.Label(key_frame, text=vp_i18n.t("supporter_key_label") + ":", style="Dim.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 8))
        var_key = tk.StringVar(value=self._supporter_key)
        ent_key = ttk.Entry(key_frame, textvariable=var_key)
        ent_key.grid(row=0, column=1, sticky="ew", padx=(0, 8))
        lbl_msg = ttk.Label(box, text=(vp_i18n.t("supporter_active_msg") if self._is_supporter() else ""),
                            style="Dim.TLabel")
        lbl_msg.grid(row=5, column=0, sticky="w", pady=(6, 0))

        def activate():
            k = var_key.get().strip().upper()
            if verify_supporter_key(k):
                self._supporter_key = k
                self._save_prefs()
                lbl_msg.configure(text=vp_i18n.t("supporter_active_msg"))
                if hasattr(self, "btn_support"):
                    self.btn_support.configure(text="★ Pro", width=5, style="PillActive.TButton")
            else:
                lbl_msg.configure(text=vp_i18n.t("supporter_invalid_msg"))

        ttk.Button(key_frame, text=vp_i18n.t("activate_btn"), style="Toggle.TButton", command=activate).grid(row=0, column=2, sticky="e")

        # diag_row = ttk.Frame(box)
        diag_row.grid(row=6, column=0, sticky="ew", pady=(14, 0))
        ttk.Button(diag_row, text="✉ " + vp_i18n.t("support_channel"), style="Primary.TButton",
                   command=lambda: webbrowser.open("https://t.me/voiceplayer_ai")).pack(side="left", padx=(0, 6))
        ttk.Button(diag_row, text="📋 " + vp_i18n.t("copy_diag"), style="Toggle.TButton",
                   command=self._copy_diagnostic_info).pack(side="left", padx=(0, 6))
        ttk.Button(diag_row, text="📁 " + vp_i18n.t("open_logs"), style="Toggle.TButton",
                   command=self._open_logs_dir).pack(side="left")

    def _open_logs_dir(self):
        """Открыть папку со служебными и краш-логами VoicePlayer."""
        folder = Path.home() / ".voice-player"
        folder.mkdir(parents=True, exist_ok=True)
        try:
            if IS_WIN:
                os.startfile(folder)
            elif IS_MAC:
                import subprocess
                subprocess.Popen(["open", str(folder)])
            else:
                import subprocess
                subprocess.Popen(["xdg-open", str(folder)])
        except Exception as e:
            log_line(f"open_logs_dir error: {e}")

    def _copy_diagnostic_info(self):
        """Собрать и скопировать системную диагностику в буфер обмена для поддержки."""
        try:
            import platform
            diag_lines = [
                f"=== VoicePlayer Diagnostics (v{APP_VERSION}) ===",
                f"OS: {platform.platform()} | Arch: {platform.machine()}",
                f"Python: {platform.python_version()} | Lang: {vp_i18n.get_lang()}",
                f"Supporter: {'Active ★' if self._is_supporter() else 'Free'}",
                f"Voice: {self._voice} | Rate: {self._rate}",
                f"Database: {DB_PATH.exists()} ({DB_PATH})",
                "--- Last 35 Log Lines ---"
            ]
            if LOG_FILE.exists():
                lines = LOG_FILE.read_text(encoding="utf-8", errors="ignore").splitlines()[-35:]
                diag_lines.extend(lines)
            txt = "\n".join(diag_lines)
            self.root.clipboard_clear()
            self.root.clipboard_append(txt)
            messagebox.showinfo("Диагностика", "Диагностический лог скопирован в буфер обмена!\nВы можете вставить его в чат поддержки: t.me/voiceplayer_ai")
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось собрать лог: {e}")

    def _open_ai_dialog(self):
        """Открыть быстрое окно «Подключение к нейросетям» из шапки очереди."""
        win = getattr(self, "_ai_win", None)
        if win is not None and win.winfo_exists():
            win.lift()
            win.focus_force()
            return
        cc = getattr(self, "colors", None) or self._palette(self.var_theme.get())
        w = tk.Toplevel(self.root)
        self._ai_win = w
        w.title(vp_i18n.t("ai_connect_title"))
        w.configure(bg=cc["panel"])
        self._center_over_root(w, 720, 560)
        w.transient(self.root)
        self._apply_window_titlebar_theme(w, cc)
        container = ttk.Frame(w, padding=16, style="Panel.TFrame")
        container.pack(fill="both", expand=True)
        self._build_ai_tab(container)

    def _show_ai_setup_dialog(self, tool):
        w = tk.Toplevel(self.root)
        w.title(f"{tool['label']} · Инструкция")
        w.geometry("520x260")
        self._center_over_root(w, 520, 260)
        w.transient(self.root)
        c = getattr(self, "colors", {})
        panel_bg = getattr(self, "col_panel", "#131615")
        panel2_bg = c.get("panel2", "#1c211f")
        w.configure(bg=panel_bg)
        f = ttk.Frame(w, padding=20, style="Panel.TFrame")
        f.pack(fill="both", expand=True)
        ttk.Label(f, text=f"{tool.get('icon', '🤖')} {tool['label']}", font=("Segoe UI Semibold", 13)).pack(anchor="w")
        ttk.Label(f, text=tool.get('desc', ''), style="Dim.TLabel").pack(anchor="w", pady=(4, 12))

        tip = tool.get("setup_tip") or "https://github.com"
        ttk.Label(f, text="Команда установки или ссылка:", font=("Segoe UI Semibold", 9)).pack(anchor="w")

        entry = tk.Entry(f, bg=panel2_bg, fg=self.col_ink, insertbackground=self.col_ink,
                         relief="flat", highlightthickness=1, highlightbackground=self.col_hairline)
        entry.insert(0, tip)
        entry.configure(state="readonly")
        entry.pack(fill="x", pady=(4, 16), ipady=4)

        btn_bar = ttk.Frame(f, style="Panel.TFrame")
        btn_bar.pack(fill="x")

        def copy_cmd():
            self.root.clipboard_clear()
            self.root.clipboard_append(tip)
            self._set_status(f"Скопировано: {tip}", ok=True)
            w.destroy()

        def open_url():
            import webbrowser
            if tip.startswith("http"):
                webbrowser.open(tip)
            else:
                copy_cmd()
            w.destroy()

        ttk.Button(btn_bar, text="Скопировать команду", style="Primary.TButton", command=copy_cmd).pack(side="left", padx=(0, 8))
        if tip.startswith("http"):
            ttk.Button(btn_bar, text="Открыть сайт", command=open_url).pack(side="left")
        ttk.Button(btn_bar, text="Закрыть", command=w.destroy).pack(side="right")

    def _build_ai_tab(self, tab):
        for w in tab.winfo_children():
            w.destroy()
        bold_f = ("Segoe UI Semibold", 10)
        c = getattr(self, "colors", {})
        panel_bg = getattr(self, "col_panel", "#131615")
        panel2_bg = c.get("panel2", "#1c211f")
        signal_col = getattr(self, "col_signal", "#7ee2b8")
        hairline_col = getattr(self, "col_hairline", "#232927")

        cur_filter = getattr(self, "_ai_filter", "all")

        # Верхний заголовок и описание
        top_box = ttk.Frame(tab, style="Panel.TFrame")
        top_box.pack(fill="x", pady=(0, 6))

        title_row = ttk.Frame(top_box, style="Panel.TFrame")
        title_row.pack(fill="x")
        ttk.Label(title_row, text=vp_i18n.t("ai_connect_title"), font=("Segoe UI Semibold", 12)).pack(side="left")

        ttk.Button(title_row, text=vp_i18n.t("pull_recent_ai_btn"), style="Primary.TButton",
                   command=lambda: self._pull_recent_ai(limit_per_source=8, silent=False)).pack(side="right")

        ttk.Label(top_box, text=vp_i18n.t("ai_connect_desc"),
                  style="Dim.TLabel", justify="left").pack(anchor="w", pady=(2, 6))

        # Панель категорий (быстрые фильтры)
        cat_bar = ttk.Frame(top_box, style="Panel.TFrame")
        cat_bar.pack(fill="x", pady=(0, 6))

        cats = [
            ("all", "Все (23)"),
            ("agents", "Агенты"),
            ("ide", "IDE"),
            ("cli", "CLI"),
            ("local_llm", "Локальные LLM"),
            ("chat", "Чат и Поиск"),
            ("terminal", "Терминал"),
        ]

        def set_filter(cat_id):
            self._ai_filter = cat_id
            self._build_ai_tab(tab)

        for cid, clbl in cats:
            is_active = (cur_filter == cid)
            ttk.Button(cat_bar, text=clbl,
                       style="Primary.TButton" if is_active else "TButton",
                       command=lambda c=cid: set_filter(c)).pack(side="left", padx=(0, 4))

        # Скроллируемая область с карточками нейросетей
        box = ttk.Frame(tab, style="Panel.TFrame")
        box.pack(fill="both", expand=True)

        canvas = tk.Canvas(box, bg=panel_bg, highlightthickness=0, bd=0)
        sb = ttk.Scrollbar(box, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas, style="Panel.TFrame")

        inner_win = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _on_cfg(event):
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _on_canv_cfg(event):
            canvas.itemconfig(inner_win, width=event.width)

        inner.bind("<Configure>", _on_cfg)
        canvas.bind("<Configure>", _on_canv_cfg)
        canvas.configure(yscrollcommand=sb.set)

        def _on_mw(e):
            if canvas.winfo_exists():
                canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")

        canvas.bind("<Enter>", lambda _e: canvas.bind_all("<MouseWheel>", _on_mw))
        canvas.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))
        canvas.bind("<Destroy>", lambda _e: canvas.unbind_all("<MouseWheel>"))

        canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        def toggle(tool):
            tid = tool["id"]
            disabled = set(self.prefs.get("disabled_ai_tools", []))
            try:
                if tool["connected"]:
                    vp_integrations.disconnect(tid)
                    disabled.add(tid)
                    self.prefs["disabled_ai_tools"] = sorted(disabled)
                    self._save_prefs()
                    self._set_status(f"{tool['label']}: авточтение отключено", ok=True)
                else:
                    disabled.discard(tid)
                    self.prefs["disabled_ai_tools"] = sorted(disabled)
                    self._save_prefs()
                    hook_home = APP_DIR if getattr(sys, "frozen", False) else vp_updater.stable_hook_home(APP_DIR)
                    vp_integrations.connect(tid, hook_home)
                    if self.var_source.get() not in ("all", "ai"):
                        self.var_source.set("all")
                        self.var_source_label.set(self._src_labels["all"])
                    self._set_status(f"{tool['label']}: подключён и отслеживается", ok=True)
            except Exception as e:
                messagebox.showerror(APP_NAME, f"Не удалось изменить настройки {tool['label']}:\n{e}", parent=tab)
            self._build_ai_tab(tab)
            self._refresh_ai_badge()

        # Строим строки для каждого инструмента
        all_tools = vp_integrations.detect()
        filtered = [t for t in all_tools if cur_filter == "all" or t.get("category") == cur_filter]

        for row_i, tool in enumerate(filtered):
            tid = tool["id"]
            icon = tool.get("icon", "🤖")
            lbl = tool["label"]

            row_frame = tk.Frame(inner, bg=panel2_bg, highlightthickness=1,
                                 highlightbackground=hairline_col, padx=10, pady=8)
            row_frame.pack(fill="x", pady=3, padx=2)

            # Левая колонка: Иконка + Название + Описание
            left = tk.Frame(row_frame, bg=panel2_bg)
            left.pack(side="left", fill="y")

            icon_lbl = tk.Label(left, text=icon, font=("Segoe UI Emoji", 14), bg=panel2_bg, fg=signal_col)
            icon_lbl.pack(side="left", padx=(0, 8))

            name_box = tk.Frame(left, bg=panel2_bg)
            name_box.pack(side="left")
            tk.Label(name_box, text=lbl, font=bold_f, bg=panel2_bg, fg=self.col_ink).pack(anchor="w")
            desc = tool.get("desc") or ""
            if desc:
                tk.Label(name_box, text=desc, font=("Segoe UI", 8), bg=panel2_bg, fg=self.col_mute).pack(anchor="w")

            # Правая колонка: Статус + Кнопка
            right = tk.Frame(row_frame, bg=panel2_bg)
            right.pack(side="right")

            if not tool["installed"]:
                state_text = "⬛ " + vp_i18n.t("not_found")
                state_fg = self.col_mute
            elif tool["connected"]:
                if tid == "terminal":
                    state_text = "🟢 Пайп и vp-say активны"
                elif tid in ("antigravity", "codex", "cline"):
                    state_text = "🟢 " + vp_i18n.t("reads_dialogs_hooks")
                else:
                    state_text = "🟢 " + vp_i18n.t("reads_dialogs_stop")
                state_fg = signal_col
            else:
                state_text = "⚪ " + vp_i18n.t("installed_ready")
                state_fg = self.col_ink

            tk.Label(right, text=state_text, font=("Segoe UI", 9), bg=panel2_bg, fg=state_fg).pack(side="left", padx=(0, 12))

            if tool["installed"]:
                btn_txt = vp_i18n.t("disconnect") if tool["connected"] else vp_i18n.t("connect")
                btn_style = "Toggle.TButton" if tool["connected"] else "Primary.TButton"
                ttk.Button(right, text=btn_txt, style=btn_style,
                           command=lambda t=tool: toggle(t)).pack(side="left")
            else:
                ttk.Button(right, text="Инструкция",
                           command=lambda t=tool: self._show_ai_setup_dialog(t)).pack(side="left")

        # Нижняя подсказка
        foot_box = ttk.Frame(tab, style="Panel.TFrame")
        foot_box.pack(fill="x", pady=(8, 0))
        ttk.Label(foot_box, text="💡 " + vp_i18n.t("ai_selection_note"), style="Dim.TLabel",
                  wraplength=640, justify="left").pack(anchor="w")
        ttk.Label(foot_box, text="📟 Терминал: любая команда | vp-say  (или Ctrl+Alt+S для озвучки выделенного текста)",
                  style="Dim.TLabel", justify="left").pack(anchor="w", pady=(2, 0))

    def _tray_quit(self):
        try:
            if self._tray: self._tray.stop()
        except Exception:
            pass
        self.on_close()

    def _notify(self, title, msg):
        if self._dnd_active():        # «не беспокоить»/тихие часы — без всплывающих уведомлений
            return
        try:
            if self._tray:
                self._tray.notify(str(msg), str(title))
        except Exception:
            pass

    def _setup_dnd(self):
        try:
            import windnd
        except Exception as e:
            log_line(f"dnd unavailable: {e}"); return

        def on_drop(files):
            paths = []
            for f in files:
                if isinstance(f, bytes):
                    for enc in ("utf-8", "mbcs", "cp1251"):
                        try: paths.append(f.decode(enc)); break
                        except Exception: continue
                else:
                    paths.append(str(f))
            self.root.after(0, lambda: self._handle_dropped(paths))
        try:
            windnd.hook_dropfiles(self.root, func=on_drop)
            log_line("drag-and-drop enabled")
        except Exception as e:
            log_line(f"dnd hook failed: {e}")

    def _handle_dropped(self, paths):
        exts = {".ogg", ".mp3", ".m4a", ".wav", ".flac", ".aac", ".opus", ".wma"}
        n = 0
        for p in paths:
            pp = Path(p.strip())
            if pp.suffix.lower() in exts and pp.exists():
                self._add_audio_file(pp); n += 1
        if n:
            self._set_status(f"перетащено: {n} файл(ов) → распознаю", ok=True)
        elif paths:
            self._set_status("перетащи аудиофайл (.ogg/.mp3/.m4a/.wav…)", ok=False)

    def _load_document(self):
        paths = filedialog.askopenfilenames(
            title="Выбери документ для озвучки",
            filetypes=[("Документы", "*.pdf *.docx *.txt *.md *.rtf *.csv"), ("Все файлы", "*.*")])
        for p in paths:
            threading.Thread(target=self._load_document_worker, args=(Path(p),),
                             daemon=True, name="doc-load").start()

    def _load_document_worker(self, path: "Path"):
        try:
            self.root.after(0, lambda: self._set_status(f"читаю {path.name}…", ok=True))
            chunks = _chunk_text(_extract_document_text(path))
            if not chunks:
                raise ValueError("не удалось извлечь текст")
            n = len(chunks)

            def add():
                for ch in chunks:
                    self.enqueue(source="manual", author=path.stem[:40],
                                 time_str=time.strftime("%H:%M"), text=ch, dedup=False)
                self._set_status(f"{path.name}: в очередь добавлено частей — {n}", ok=True)
            self.root.after(0, add)
        except Exception as e:
            self.root.after(0, lambda: self._set_status(f"не удалось прочитать {path.name}: {e}", ok=False))

    def _load_audio_file(self):
        if not _WHISPER_OK:
            messagebox.showerror(APP_NAME, "Движок распознавания не установлен.\n"
                                 "Запусти install-deps.cmd (faster-whisper).")
            return
        paths = filedialog.askopenfilenames(
            title="Выбери аудиофайлы для транскрибации",
            filetypes=[("Аудио", "*.ogg *.mp3 *.m4a *.wav *.flac *.aac *.opus *.wma"),
                       ("Все файлы", "*.*")])
        for p in paths:
            self._add_audio_file(Path(p))

    def _add_audio_file(self, path: "Path"):
        if not path.exists():
            return
        item = Item(id=str(uuid4()), source="file", author=path.name,
                    time_str=time.strftime("%H:%M"), date_str=time.strftime("%d.%m.%Y"),
                    text="(распознаётся…)", raw_text=path.name,
                    is_file=True, file_path=path, audio_path=path,
                    audio_ready=True, transcribing=True)
        self.queue_items.append(item)
        self._trans_queue.put(item)
        self._set_status(f"распознаю «{path.name}»… (модель грузится при первом запуске)", ok=True)
        self._render_list()

    def _preload_whisper(self):
        try:
            self._ui_after(lambda: self._set_status("загружаю модель распознавания…", ok=True))
            _get_whisper(self._model_size)
            self._ui_after(lambda: self._set_status("модель распознавания готова", ok=True))
        except Exception as e:
            log_line(f"whisper preload error: {e}")

    def _ui_after(self, fn):
        """root.after из фонового потока; до запуска mainloop молча пропускаем."""
        try:
            self.root.after(0, fn)
        except RuntimeError:
            pass

    def _transcribe_worker_loop(self):
        while not self.stop_event.is_set():
            try: item = self._trans_queue.get(timeout=0.5)
            except queue.Empty: continue
            t0 = time.time()
            lang = "en" if self._translate_en else None
            try:
                text, marks = transcribe_file(item.file_path, self._model_size,
                                              language=lang, translate=self._translate_en,
                                              word_level=True)
                item.text = text or "(речь не распознана)"
                item.raw_text = item.text
                item.marks = marks
            except Exception as e:
                item.text = "(ошибка распознавания)"
                log_line(f"transcribe FAIL [{item.id}]: {e}")
            finally:
                item.transcribing = False
                dt = time.time() - t0
                nm = item.author
                archive_add(item)                  # сохранить распознанный текст в архив
                self.root.after(0, self._request_render)
                self.root.after(0, lambda d=dt: self._set_status(f"распознано за {d:.0f}с", ok=True))
                self.root.after(0, lambda n=nm: self._notify("Распознавание готово", n))

    # ------------------ сохранение текста и аудио ------------------
    def _save_text_selected(self):
        idx = self._selected_idx()
        if idx is None or not (0 <= idx < len(self.queue_items)):
            return
        it = self.queue_items[idx]
        default = (it.author.rsplit(".", 1)[0] if it.is_file else (it.author or "текст")) + ".txt"
        fn = filedialog.asksaveasfilename(title="Сохранить текст", defaultextension=".txt",
                                          initialfile=default,
                                          filetypes=[("Текст", "*.txt"), ("Все файлы", "*.*")])
        if not fn:
            return
        try:
            Path(fn).write_text(it.text, encoding="utf-8")
            self._set_status(f"текст сохранён: {Path(fn).name}", ok=True)
        except Exception as e:
            messagebox.showerror(APP_NAME, f"Не удалось сохранить текст:\n{e}")

    def _save_audio_selected(self):
        import shutil
        idx = self._selected_idx()
        if idx is None or not (0 <= idx < len(self.queue_items)):
            return
        it = self.queue_items[idx]
        src = it.file_path if it.is_file else it.audio_path
        if not src or not Path(src).exists():
            messagebox.showinfo(APP_NAME, "Аудио ещё не готово или недоступно.")
            return
        ext = Path(src).suffix or ".mp3"
        base = (it.author.rsplit(".", 1)[0] if it.is_file else (it.author or "audio"))
        fn = filedialog.asksaveasfilename(title="Сохранить аудио", defaultextension=ext,
                                          initialfile=base + ext,
                                          filetypes=[("Аудио", "*" + ext), ("Все файлы", "*.*")])
        if not fn:
            return
        try:
            shutil.copyfile(src, fn)
            self._set_status(f"аудио сохранено: {Path(fn).name}", ok=True)
        except Exception as e:
            messagebox.showerror(APP_NAME, f"Не удалось сохранить аудио:\n{e}")

    @staticmethod
    def _srt_time(s: float, comma=True) -> str:
        ms = int(round(s * 1000))
        h, ms = divmod(ms, 3600000); m, ms = divmod(ms, 60000); sec, ms = divmod(ms, 1000)
        sep = "," if comma else "."
        return f"{h:02d}:{m:02d}:{sec:02d}{sep}{ms:03d}"

    def _save_subtitles_selected(self):
        idx = self._selected_idx()
        if idx is None or not (0 <= idx < len(self.queue_items)):
            return
        it = self.queue_items[idx]
        if not it.marks:
            messagebox.showinfo(APP_NAME, "У этой реплики нет таймингов для субтитров.")
            return
        base = (it.author.rsplit(".", 1)[0] if it.is_file else (it.author or "subtitles"))
        fn = filedialog.asksaveasfilename(title="Сохранить субтитры", defaultextension=".srt",
                                          initialfile=base + ".srt",
                                          filetypes=[("SubRip", "*.srt"), ("WebVTT", "*.vtt"), ("Все файлы", "*.*")])
        if not fn:
            return
        vtt = fn.lower().endswith(".vtt")
        try:
            lines = []
            if vtt:
                lines.append("WEBVTT\n")
            for i, (s, e, a, b) in enumerate(it.marks, 1):
                frag = it.text[a:b].strip()
                if vtt:
                    lines.append(f"{self._srt_time(s, False)} --> {self._srt_time(e, False)}\n{frag}\n")
                else:
                    lines.append(f"{i}\n{self._srt_time(s)} --> {self._srt_time(e)}\n{frag}\n")
            Path(fn).write_text("\n".join(lines), encoding="utf-8")
            self._set_status(f"субтитры сохранены: {Path(fn).name}", ok=True)
        except Exception as e:
            messagebox.showerror(APP_NAME, f"Не удалось сохранить субтитры:\n{e}")

    def _respeak_selected(self):
        """Озвучить распознанный текст файла заново (чистой озвучкой edge-tts)."""
        idx = self._selected_idx()
        if idx is None or not (0 <= idx < len(self.queue_items)):
            return
        it = self.queue_items[idx]
        txt = (it.text or "").strip()
        if not txt or txt.startswith("("):
            messagebox.showinfo(APP_NAME, "Нет распознанного текста для озвучки.")
            return
        self.enqueue(source="manual", author="Озвучка: " + it.author,
                     time_str=time.strftime("%H:%M"), text=txt, dedup=False)
        self._set_status("озвучиваю распознанный текст", ok=True)

    # ------------------ таймер сна ------------------
    def _set_sleep_timer(self, minutes):
        if self._sleep_after is not None:
            try: self.root.after_cancel(self._sleep_after)
            except Exception: pass
            self._sleep_after = None
        if not minutes:
            self._sleep_deadline = 0.0
            try: self.var_sleep.set(vp_i18n.t("off"))
            except Exception: pass
            self._set_status("таймер сна выключен", ok=True)
            return
        self._sleep_deadline = time.time() + minutes * 60
        self._sleep_after = self.root.after(int(minutes * 60000), self._sleep_fire)
        self._set_status(f"таймер сна: остановлю через {minutes} мин", ok=True)

    def _sleep_fire(self):
        self._sleep_after = None
        self._sleep_deadline = 0.0
        try: self.player.stop()
        except Exception: pass
        self.user_paused = True
        self.current_idx = -1
        self.var_autoplay.set(False)        # чтобы новые реплики не запустили звук, пока спим
        try: self.var_sleep.set(vp_i18n.t("off"))
        except Exception: pass
        try: self.btn_toggle.configure(text=self._g_play)
        except Exception: pass
        self._set_status("таймер сна сработал — воспроизведение остановлено", ok=True)
        self._save_prefs()

    # ------------------ озвучить статью по ссылке ------------------
    def _speak_url(self):
        url = (self.tts_input.get("1.0", "end").strip())
        if not (url.startswith("http://") or url.startswith("https://")):
            messagebox.showinfo(APP_NAME, "Вставь ссылку (http…) в поле текста и нажми эту кнопку.")
            return
        self._set_status("загружаю статью…", ok=True)
        threading.Thread(target=self._fetch_url_worker, args=(url,), daemon=True).start()

    def _fetch_url_worker(self, url):
        import urllib.request, re, html
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            raw = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "replace")
            raw = re.sub(r"(?is)<(script|style|nav|header|footer|aside)[^>]*>.*?</\1>", " ", raw)
            raw = re.sub(r"(?is)<br\s*/?>", "\n", raw)
            raw = re.sub(r"(?is)</(p|div|h[1-6]|li)>", "\n", raw)
            text = re.sub(r"(?is)<[^>]+>", " ", raw)
            text = html.unescape(text)
            text = re.sub(r"[ \t]+", " ", text)
            text = re.sub(r"\n\s*\n+", "\n", text).strip()
            text = text[:6000]   # ограничим объём
            if not text:
                raise ValueError("пустой текст")
            self.root.after(0, lambda: self.enqueue(source="manual", author="Статья",
                            time_str=time.strftime("%H:%M"), text=text, dedup=False))
            self.root.after(0, lambda: self._set_status("статья добавлена в очередь", ok=True))
            self.root.after(0, lambda: self.tts_input.delete("1.0", "end"))
        except Exception as e:
            self.root.after(0, lambda: self._set_status(f"не удалось загрузить статью: {e}", ok=False))

    # ------------------ озвучка произвольного текста ------------------
    def _speak_input(self):
        txt = self.tts_input.get("1.0", "end").strip()
        if not txt:
            self._set_status("поле пустое — вставь текст", ok=False)
            return
        item = self.enqueue(source="manual", author=vp_i18n.t("custom_text_author"),
                            time_str=time.strftime("%H:%M"), text=txt, dedup=False)
        self._speak_now(item, "текст")
        try:
            self.tts_input.delete("1.0", "end")
            self.tts_input.configure(height=3)
        except Exception:
            pass

    def _speak_now(self, item: Optional[Item], what: str):
        """Ручной текст — вне очереди: остановить текущее, рендер с приоритетом 0 и
        сыграть сразу по готовности (_tick запустит current_idx, когда аудио появится).
        Иначе он вставал в хвост очереди и ждал всё накопленное — «кнопка не работает»."""
        if item is None:
            self._set_status(f"{what}: после очистки разметки озвучивать нечего", ok=False)
            return
        item.forced = True
        try: self.player.stop()
        except Exception: pass
        self.user_paused = False
        self._play_index(self.queue_items.index(item))
        if not item.audio_ready:
            self._set_status(f"{what}: готовлю озвучку, начнётся через несколько секунд", ok=True)

    def _paste_into_input(self):
        clip = self._clipboard_rich()
        if clip:
            self.tts_input.delete("1.0", "end")
            self.tts_input.insert("1.0", clip)
            self._after_input_paste(clip)

    def _clipboard_rich(self) -> str:
        """Текст из буфера с таблицами. Браузер и Word кладут таблицу в HTML, а в обычном
        тексте она часто рассыпается — поэтому HTML в приоритете, если таблица там есть."""
        html = _read_clipboard_html()
        if html and "<table" in html.lower():
            md = html_to_markdown(html)
            if find_tables(md):
                return md
        try:
            return normalize_tables(self.root.clipboard_get())
        except Exception:
            return ""

    def _on_input_paste(self, _e=None):
        clip = self._clipboard_rich()
        if clip:
            try: self.tts_input.delete("sel.first", "sel.last")
            except tk.TclError: pass
            self.tts_input.insert("insert", clip)
            self._after_input_paste(clip)
        return "break"

    def _after_input_paste(self, clip: str):
        tables = find_tables(clip)
        if tables:
            self.tts_input.configure(height=9)   # таблице нужно место, её видно целиком
            rows = sum(len(t["rows"]) for t in tables)
            self._set_status(f"таблица распознана: строк {rows}, колонок {len(tables[0]['header'])} — "
                             "при озвучке покажу её справа", ok=True)

    # ------------------ глобальная горячая клавиша «озвучить выделенное» ------------------
    def _start_global_hotkey(self):
        if os.name != "nt":
            return
        threading.Thread(target=self._hotkey_loop, daemon=True, name="hotkey").start()

    def _hotkey_loop(self):
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            MOD_ALT, MOD_CONTROL, MOD_NOREPEAT = 0x0001, 0x0002, 0x4000
            VK_S = 0x53
            HOTKEY_ID = 1
            ok = False
            for attempt in range(5):
                if user32.RegisterHotKey(None, HOTKEY_ID,
                                         MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, VK_S):
                    ok = True; break
                time.sleep(0.6)   # вдруг прошлый экземпляр ещё не освободил
            if not ok:
                log_line("global hotkey Ctrl+Alt+S register FAILED (занята другой программой?)")
                self.root.after(0, lambda: self._set_status(
                    "Ctrl+Alt+S занята другим софтом — поле «Озвучить» работает", ok=False))
            else:
                log_line("global hotkey Ctrl+Alt+S registered")
            # аппаратные медиа-клавиши клавиатуры (▶⏸ ⏭ ⏮ ⏹) ловим НИЗКОУРОВНЕВЫМ
            # хуком, а не RegisterHotKey: хук видит нажатие ВСЕГДА, даже если эти кнопки
            # уже «забрал» другой плеер — так наш плеер сам распознаёт их, как Spotify и пр.
            if user32.RegisterHotKey(None, 2, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, 0x50):   # P
                log_line("global hotkey Ctrl+Alt+P registered")
            self._install_media_hook(user32)
            msg = wintypes.MSG()
            while not self.stop_event.is_set():
                r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if r in (0, -1):
                    break
                if msg.message == 0x0312 and int(msg.wParam) == 1:   # WM_HOTKEY Ctrl+Alt+S
                    self.root.after(0, self._speak_selection)
                elif msg.message == 0x0312 and int(msg.wParam) == 2:  # WM_HOTKEY Ctrl+Alt+P
                    self.root.after(0, self._toggle_window)
        except Exception as e:
            log_line(f"hotkey loop error: {e}")

    def _install_media_hook(self, user32):
        """Низкоуровневый хук клавиатуры (WH_KEYBOARD_LL): ловит аппаратные медиа-кнопки
        play/pause, next, prev, stop и переключает наш плеер. Не «съедает» нажатие
        (CallNextHookEx), поэтому не ломает другие приложения и системные кнопки громкости."""
        import ctypes
        from ctypes import wintypes
        try:
            VK = {0xB3: self.action_toggle,            # Media Play/Pause
                  0xB0: self.action_next,              # Media Next Track
                  0xB1: self.action_prev,              # Media Prev Track
                  0xB2: lambda: self.player.stop()}    # Media Stop

            LRESULT = ctypes.c_ssize_t
            ULONG_PTR = ctypes.c_size_t

            class KBDLLHOOKSTRUCT(ctypes.Structure):
                _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                            ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                            ("dwExtraInfo", ULONG_PTR)]

            # колбэк хука обязан быть stdcall (WINFUNCTYPE), иначе падение при вызове
            HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
            user32.CallNextHookEx.restype = LRESULT
            user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int,
                                              wintypes.WPARAM, wintypes.LPARAM]
            user32.SetWindowsHookExW.restype = wintypes.HHOOK
            user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC,
                                                 wintypes.HINSTANCE, wintypes.DWORD]
            # ВАЖНО: без restype хэндл модуля обрежется до 32 бит → SetWindowsHookEx упадёт
            k32 = ctypes.windll.kernel32
            k32.GetModuleHandleW.restype = wintypes.HMODULE
            k32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]

            def _proc(nCode, wParam, lParam):
                try:
                    if nCode == 0 and wParam in (0x0100, 0x0104):   # WM_KEYDOWN/WM_SYSKEYDOWN
                        vk = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents.vkCode
                        act = VK.get(vk)
                        if act:
                            self.root.after(0, act)
                except Exception:
                    pass
                return user32.CallNextHookEx(None, nCode, wParam, lParam)

            self._media_hook_proc = HOOKPROC(_proc)   # держим ссылку, иначе соберёт GC
            hmod = k32.GetModuleHandleW(None)
            self._media_hook = user32.SetWindowsHookExW(13, self._media_hook_proc, hmod, 0)
            if self._media_hook:
                log_line("media keys hook installed (play/pause/next/prev)")
            else:
                err = ctypes.get_last_error() if hasattr(ctypes, "get_last_error") else 0
                log_line(f"media keys hook FAILED err={err}")
        except Exception as e:
            log_line(f"media hook error: {e}")

    def _center_over_root(self, win, w: int, h: int):
        """Открыть всплывающее окно ровно по центру над главным окном плеера
        (а не «где попало»). Размер задаём здесь же одной строкой geometry."""
        try:
            self.root.update_idletasks()
            rx, ry = self.root.winfo_x(), self.root.winfo_y()
            rw, rh = self.root.winfo_width(), self.root.winfo_height()
            x = max(0, rx + (rw - w) // 2)
            y = max(0, ry + (rh - h) // 2)
            win.geometry(f"{w}x{h}+{x}+{y}")
        except Exception:
            win.geometry(f"{w}x{h}")
        self._apply_window_titlebar_theme(win)

    def _open_list_menu(self):
        """Открыть выпадающее меню «⋮» с действиями над списком под кнопкой."""
        try:
            # подпись таймера сна с остатком времени (обновляем при каждом открытии)
            try:
                self._list_menu.entryconfigure(self._sleep_menu_index,
                                               label=self._sleep_remaining_label())
            except Exception:
                pass
            b = self.btn_listmenu
            x, y = b.winfo_rootx(), b.winfo_rooty() + b.winfo_height()
            self._list_menu.tk_popup(x, y)
        finally:
            try:
                self._list_menu.grab_release()
            except Exception:
                pass

    def _speak_selection(self):
        # копируем текущее выделение из активного окна (Ctrl+C) и озвучиваем
        self._send_ctrl_c()
        self.root.after(170, self._speak_clipboard)

    def _send_ctrl_c(self):
        try:
            import ctypes
            user32 = ctypes.windll.user32
            VK_CONTROL, VK_C, KEYEVENTF_KEYUP = 0x11, 0x43, 0x0002
            user32.keybd_event(VK_CONTROL, 0, 0, 0)
            user32.keybd_event(VK_C, 0, 0, 0)
            user32.keybd_event(VK_C, 0, KEYEVENTF_KEYUP, 0)
            user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
        except Exception as e:
            log_line(f"send ctrl+c error: {e}")

    def _speak_clipboard(self):
        txt = (self._clipboard_rich() or "").strip()
        if not txt:
            self._set_status("буфер пуст — выдели текст и снова нажми Ctrl+Alt+S", ok=False)
            return
        item = self.enqueue(source="manual", author="Выделенный текст",
                            time_str=time.strftime("%H:%M"), text=txt, dedup=False)
        self._speak_now(item, "выделенный текст (Ctrl+Alt+S)")

    def _tick(self):
        try:
            if self.current_idx >= 0:
                cur = self.queue_items[self.current_idx]
                if cur.audio_error and not cur.audio_ready:
                    # текущая не отрендерилась (сетевой сбой и т.п.) — не блокируем очередь
                    nxt = self._find_next(self.current_idx + 1)
                    if nxt >= 0 and self.queue_items[nxt].audio_ready:
                        self._play_index(nxt)
                    elif nxt < 0:
                        self.current_idx = -1   # дать новым репликам автоиграть
                elif cur.audio_ready and not self.player.has_track() and not self.user_paused:
                    # реплика готова, но ничего не загружено — играем её
                    self._play_index(self.current_idx)
                elif (self.player.has_track() and self.player.is_finished() and cur.more_parts
                      and not self.user_paused):
                    pass                        # звук догнал озвучку — ждём следующую часть
                elif self.player.has_track() and self.player.is_finished() and not self.user_paused:
                    # доиграла до конца — следующая (с учётом режима)
                    if self.var_autoplay.get():
                        nxt = self._auto_next_index()
                        if nxt >= 0:
                            self._play_index(nxt)
                        else:
                            self.player.stop()
                            self.current_idx = -1
                            self.btn_toggle.configure(text=self._g_play)
                            if not getattr(self, "_queue_end_notified", False):
                                self._queue_end_notified = True
                                self._notify("Очередь закончилась", "Все реплики проиграны")
                    else:
                        self.player.stop()
                        self.btn_toggle.configure(text=self._g_play)
            elif (self.var_autoplay.get() and self.current_idx < 0 and not self.user_paused
                  and not self._dnd_active()):
                nxt = self._find_next(0)
                if nxt >= 0 and self.queue_items[nxt].audio_ready:
                    self._play_index(nxt)
            # A-B повтор: если позиция дошла до B — прыжок на A
            a, b = self._ab
            if a is not None and b is not None and b > a and self.player.has_track():
                if self.player.position() >= b:
                    try: self.player.seek(a)
                    except Exception: pass
            self._poll_clipboard()
            if self.player.is_playing():
                self._stats_seconds = float(getattr(self, "_stats_seconds", 0.0)) + 0.3
            self._update_seekbar()
            self._update_mini()
            self._update_dnd_badge()
        except Exception as e:
            log_line(f"tick error: {e}")
        finally:
            if not self.stop_event.is_set():
                self.root.after(300, self._tick)



    def _delete_current_or_selected(self):
        """Удалить текущую или выбранную реплику из очереди."""
        try:
            sel = self.tree.selection()
            if sel:
                idx = int(sel[0])
                if 0 <= idx < len(self.queue_items):
                    del self.queue_items[idx]
                    self._render_list()
                    self._set_status("реплика удалена", ok=True)
                    return
            if self.current_idx is not None and 0 <= self.current_idx < len(self.queue_items):
                del self.queue_items[self.current_idx]
                if self.current_idx >= len(self.queue_items):
                    self.current_idx = max(0, len(self.queue_items) - 1)
                self._render_list()
                self._set_status("текущая реплика удалена", ok=True)
        except Exception as e:
            log_line(f"delete_item error: {e}")

    def _show_shortcuts_dialog(self):
        """Красивое всплывающее окно со всеми горячими клавишами VoicePlayer."""
        c = self.colors
        w = tk.Toplevel(self.root)
        w.title(vp_i18n.t("shortcuts_title"))
        w.configure(bg=c["bg"])
        self._center_over_root(w, 580, 560)
        w.transient(self.root)

        box = ttk.Frame(w, padding=18)
        box.pack(fill="both", expand=True)

        ttk.Label(box, text="⌨ " + vp_i18n.t("shortcuts_title"), font=("Segoe UI Semibold", 13)).pack(anchor="w", pady=(0, 10))

        lang = vp_i18n.get_lang()
        cat_play = vp_i18n.t("tab_playback")
        cat_ctrl = vp_i18n.t("control")
        cat_q = vp_i18n.t("queue")

        DESCS = {
            "space": {"ru": "Пауза / Продолжить", "en": "Play / Pause toggle", "es": "Pausa / Reanudar", "zh": "播放 / 暂停切换", "ja": "再生 / 一時停止の切替", "de": "Wiedergabe / Pause", "fr": "Lecture / Pause", "hi": "रोकें / जारी रखें"},
            "ctrl_enter": {"ru": "Озвучить текст из поля ввода", "en": "Speak text from input box", "es": "Leer texto del campo", "zh": "朗读输入框中的文本", "ja": "入力欄のテキストを読み上げ", "de": "Text aus Eingabefeld vorlesen", "fr": "Lire le texte du champ", "hi": "इनपुट बॉक्स से टेक्स्ट बोलें"},
            "arrows_lr": {"ru": "Следующая / Предыдущая реплика", "en": "Next / Previous item", "es": "Siguiente / Anterior elemento", "zh": "下一条 / 上一条", "ja": "次 / 前のアイテム", "de": "Nächstes / Vorheriges Element", "fr": "Élément suivant / précédent", "hi": "अगला / पिछला आइटम"},
            "ctrl_lr": {"ru": "Перемотка на ±10 сек", "en": "Seek ±10 sec", "es": "Avanzar/retroceder ±10 seg", "zh": "快进/快退 ±10秒", "ja": "シーク ±10秒", "de": "Spulen ±10 Sek", "fr": "Avance/retour ±10 sec", "hi": "±10 सेकंड आगे/पीछे"},
            "jl_seek": {"ru": "Перемотка на ±5 сек (YouTube стиль)", "en": "Seek ±5 sec (YouTube style)", "es": "Avanzar/retroceder ±5 seg (estilo YouTube)", "zh": "快进/快退 ±5秒 (YouTube风格)", "ja": "シーク ±5秒 (YouTubeスタイル)", "de": "Spulen ±5 Sek (YouTube-Stil)", "fr": "Avance/retour ±5 sec (style YouTube)", "hi": "±5 सेकंड आगे/पीछे"},
            "shift_lr": {"ru": "Переход к след. / пред. предложению", "en": "Jump to next / prev sentence", "es": "Saltar a siguiente / anterior frase", "zh": "跳转到下/上一句", "ja": "次 / 前の文にジャンプ", "de": "Zum nächsten / vorherigen Satz", "fr": "Phrase suivante / précédente", "hi": "अगले/पिछले वाक्य पर जाएं"},
            "repeat": {"ru": "Повторить текущую реплику сначала", "en": "Repeat current item from start", "es": "Repetir elemento actual", "zh": "从头重复当前条目", "ja": "現在のアイテムを最初からリピート", "de": "Aktuelles Element von vorne wiederholen", "fr": "Répéter l'élément actuel", "hi": "वर्तमान आइटम को दोहराएं"},
            "ctrl_num": {"ru": "Пресеты скорости (1.0x / 1.25x / 1.5x / 2.0x)", "en": "Speed presets (1.0x / 1.25x / 1.5x / 2.0x)", "es": "Preajustes de velocidad (1.0x / 1.25x / 1.5x / 2.0x)", "zh": "速度预设 (1.0x / 1.25x / 1.5x / 2.0x)", "ja": "速度プリセット (1.0x / 1.25x / 1.5x / 2.0x)", "de": "Geschwindigkeits-Presets", "fr": "Préréglages de vitesse", "hi": "गति प्रीसेट"},
            "ctrl_pm": {"ru": "Плавная регулировка скорости (±0.1x / сброс 1.0x)", "en": "Fine-tune speed (±0.1x / reset 1.0x)", "es": "Ajuste fino de velocidad (±0.1x / reset)", "zh": "微调速度 (±0.1x / 重置)", "ja": "速度の微調整 (±0.1x / リセット)", "de": "Feinabstimmung Geschwindigkeit (±0.1x)", "fr": "Réglage fin de la vitesse (±0.1x)", "hi": "गति को सूक्ष्म समायोजित करें"},
            "help": {"ru": "Справка по горячим клавишам", "en": "Keyboard shortcuts help", "es": "Ayuda de atajos de teclado", "zh": "快捷键帮助", "ja": "ショートカットキーのヘルプ", "de": "Hilfe zu Tastaturkurzbefehlen", "fr": "Aide sur les raccourcis", "hi": "कीबोर्ड शॉर्टकट सहायता"},
            "settings": {"ru": "Настройки программы", "en": "Settings", "es": "Configuración", "zh": "设置", "ja": "設定", "de": "Einstellungen", "fr": "Paramètres", "hi": "सेटिंग्स"},
            "dnd": {"ru": "Режим «Не беспокоить» (тишина)", "en": "Do Not Disturb (quiet mode)", "es": "Modo no molestar (silencio)", "zh": "勿扰模式 (静音)", "ja": "応答なしモード (サイレント)", "de": "Nicht stören (Ruhemodus)", "fr": "Mode Ne pas déranger", "hi": "परेशान न करें (शांत मोड)"},
            "tools": {"ru": "Панель инструментов (свернуть/развернуть)", "en": "Tools drawer (expand/collapse)", "es": "Panel de herramientas", "zh": "工具箱面板 (展开/折叠)", "ja": "ツールパネル (開閉)", "de": "Werkzeugleiste (ein-/ausklappen)", "fr": "Volet d'outils", "hi": "टूल्स पैनल"},
            "clear_field": {"ru": "Очистить поле своего текста (Ctrl+N: очистить и фокус)", "en": "Clear text field (Ctrl+N: clear & focus)", "es": "Limpiar campo de texto", "zh": "清空文本框 (Ctrl+N: 清空并聚焦)", "ja": "テキスト欄をクリア (Ctrl+N: クリアとフォーカス)", "de": "Textfeld leeren", "fr": "Effacer le champ de texte", "hi": "टेक्स्ट फ़ील्ड साफ़ करें"},
            "focus_input": {"ru": "Фокус в поле ввода текста", "en": "Focus text input box", "es": "Enfocar campo de texto", "zh": "聚焦到文本输入框", "ja": "入力欄にフォーカス", "de": "Fokus auf Eingabefeld", "fr": "Curseur dans le champ de texte", "hi": "टेक्स्ट इनपुट बॉक्स पर ध्यान केंद्रित करें"},
            "dictate": {"ru": "Голосовая диктовка микрофоном", "en": "Voice dictation with microphone", "es": "Dictado por voz con micrófono", "zh": "麦克风语音听写", "ja": "マイクによる音声入力", "de": "Sprachdiktat mit Mikrofon", "fr": "Dictée vocale au microphone", "hi": "माइक्रोफ़ोन से वॉयс डिक्टेशन"},
            "edit": {"ru": "Редактировать текст реплики", "en": "Edit reply text", "es": "Editar texto del elemento", "zh": "编辑当前文本", "ja": "テキストを編集", "de": "Text bearbeiten", "fr": "Modifier le texte", "hi": "टेक्स्ट संपादित करें"},
            "save_mp3": {"ru": "Сохранить текущую озвучку в MP3", "en": "Save current voice to MP3", "es": "Guardar voz actual en MP3", "zh": "将当前语音保存为 MP3", "ja": "現在の音声をMP3で保存", "de": "Aktuelle Sprachausgabe als MP3 speichern", "fr": "Enregistrer l'audio en MP3", "hi": "वर्तमान आवाज़ को MP3 में सहेजें"},
            "del_item": {"ru": "Удалить выбранную реплику", "en": "Delete selected item", "es": "Eliminar elemento seleccionado", "zh": "删除所选条目", "ja": "選択したアイテムを削除", "de": "Ausgewähltes Element löschen", "fr": "Supprimer l'élément sélectionné", "hi": "चयनित आइटम हटाएं"},
            "clear_q": {"ru": "Очистить всю очередь", "en": "Clear entire queue", "es": "Limpiar toda la cola", "zh": "清空整个队列", "ja": "キュー全体をクリア", "de": "Ganze Warteschlange leeren", "fr": "Vider toute la file d'attente", "hi": "पूरी कतार साफ़ करें"},
            "search": {"ru": "Поиск и фильтр по тексту", "en": "Search & filter by text", "es": "Buscar y filtrar por texto", "zh": "按文本搜索和过滤", "ja": "テキストで検索・フィルター", "de": "Nach Text suchen & filtern", "fr": "Recherche et filtre par texte", "hi": "टेक्स्ट द्वारा खोजें और फ़िल्टर करें"},
            "f5_ai": {"ru": "Подтянуть свежие диалоги ИИ", "en": "Fetch fresh AI conversations", "es": "Actualizar diálogos de IA recientes", "zh": "抓取最新 AI 对话", "ja": "最新のAI対話を取得", "de": "Neueste KI-Gespräche abrufen", "fr": "Récupérer les conversations IA", "hi": "ताज़ा AI बातचीत प्राप्त करें"},
            "escape": {"ru": "Снять выделение / Закрыть поиск", "en": "Clear selection / Close search", "es": "Deseleccionar / Cerrar búsqueda", "zh": "取消选择 / 关闭搜索", "ja": "選択解除 / 検索を閉じる", "de": "Auswahl aufheben / Suche schließen", "fr": "Désélectionner / Fermer la recherche", "hi": "चयन साफ़ करें / खोज बंद करें"}
        }

        def d(key):
            entry = DESCS.get(key, {})
            return entry.get(lang) or entry.get("en") or key

        # Список горячих клавиш: (Категория, Комбинация, Действие)
        shortcuts = [
            (cat_play, "Space  /  K", d("space")),
            (cat_play, "Ctrl + Enter", d("ctrl_enter")),
            (cat_play, "→  /  ←", d("arrows_lr")),
            (cat_play, "Ctrl + →  /  ←", d("ctrl_lr")),
            (cat_play, "J  /  L", d("jl_seek")),
            (cat_play, "Shift + →  /  ←", d("shift_lr")),
            (cat_play, "R", d("repeat")),
            (cat_play, "Ctrl + 1 / 2 / 3 / 4", d("ctrl_num")),
            (cat_play, "Ctrl + + / - / 0", d("ctrl_pm")),
            (cat_ctrl, "F1  /  Ctrl + H", d("help")),
            (cat_ctrl, "Ctrl + , (запятая)", d("settings")),
            (cat_ctrl, "Ctrl + M  /  M", d("dnd")),
            (cat_ctrl, "Ctrl + T", d("tools")),
            (cat_ctrl, "Ctrl + L  /  Ctrl + N", d("clear_field")),
            (cat_ctrl, "Ctrl + I", d("focus_input")),
            (cat_ctrl, "Ctrl + D", d("dictate")),
            (cat_ctrl, "Ctrl + E  /  F2", d("edit")),
            (cat_ctrl, "Ctrl + S", d("save_mp3")),
            (cat_q, "Ctrl + Del", d("del_item")),
            (cat_q, "Ctrl + Shift + L", d("clear_q")),
            (cat_q, "Ctrl + F  /  Ctrl + Shift + F", d("search")),
            (cat_q, "F5", d("f5_ai")),
            (cat_q, "Esc", d("escape")),
        ]

        canvas = tk.Canvas(box, bg=c["panel"], highlightthickness=0)
        sb = ttk.Scrollbar(box, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas, style="Panel.TFrame", padding=10)
        inner.columnconfigure(0, minsize=140)
        inner.columnconfigure(1, weight=1)

        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(xscrollcommand=sb.set, yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        def _on_cfg(e):
            canvas.configure(scrollregion=canvas.bbox("all"))
        inner.bind("<Configure>", _on_cfg)

        for row_i, (cat, key_combo, desc) in enumerate(shortcuts):
            k_lbl = tk.Label(inner, text=key_combo, font=("Segoe UI Semibold", 9), bg=c["panel2"], fg=c["signal"],
                             padx=8, pady=3, relief="flat", bd=0)
            k_lbl.grid(row=row_i, column=0, sticky="w", padx=(0, 10), pady=3)
            d_lbl = ttk.Label(inner, text=f"({cat})  {desc}", style="TLabel")
            d_lbl.grid(row=row_i, column=1, sticky="w", pady=3)

        ttk.Button(w, text=vp_i18n.t("close"), style="Primary.TButton", command=w.destroy).pack(pady=10)

    def on_close(self):
        global _instance_mutex
        vp_updater.mark_healthy(APP_DIR)   # штатный выход: версия рабочая, даже если окно открывали ненадолго
        self._save_prefs()
        self._save_session()          # сохранить очередь, чтобы при открытии была как была
        self.stop_event.set()
        try:
            if IS_WIN and getattr(self, "_media_hook", None):
                ctypes.windll.user32.UnhookWindowsHookEx(self._media_hook)
                self._media_hook = None
        except Exception: pass
        try:
            if self.mini is not None and self.mini.winfo_exists():
                self.mini.destroy()
        except Exception: pass
        try:
            if hasattr(self.player, "shutdown"): self.player.shutdown()
            else: self.player.stop()
        except Exception: pass
        try:
            if getattr(self, "_tray", None): self._tray.stop()
        except Exception: pass
        try:
            srv = getattr(self, "http_srv", None)
            if srv:
                try: srv.server_close()    # мгновенно освобождаем порт 3737 до остановки потока
                except Exception: pass
                threading.Thread(target=srv.shutdown, daemon=True).start()
        except Exception: pass
        try:
            if IS_WIN and _instance_mutex:
                ctypes.windll.kernel32.CloseHandle(_instance_mutex)
                _instance_mutex = None
        except Exception: pass
        self.root.destroy()


def _kill_zombie_on_port(port: int) -> None:
    """Если старый процесс завис при закрытии и держит порт 3737 (не отвечает на /api/show), прибиваем его."""
    if IS_WIN:
        try:
            out = subprocess.check_output(
                ["netstat", "-ano", "-p", "TCP"],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                text=True, timeout=3, errors="ignore"
            )
            my_pid = os.getpid()
            for line in out.splitlines():
                if f"127.0.0.1:{port} " in line and "LISTENING" in line:
                    parts = line.split()
                    pid = int(parts[-1])
                    if pid > 0 and pid != my_pid:
                        k32 = ctypes.windll.kernel32
                        h = k32.OpenProcess(1, False, pid)  # PROCESS_TERMINATE = 1
                        if h:
                            k32.TerminateProcess(h, 0)
                            k32.CloseHandle(h)
                        time.sleep(0.25)
        except Exception:
            pass
    else:
        try:
            out = subprocess.check_output(["lsof", "-ti", f"tcp:{port}"], text=True, timeout=3, errors="ignore")
            my_pid = str(os.getpid())
            for pid_str in out.split():
                if pid_str != my_pid and pid_str.isdigit():
                    try:
                        os.kill(int(pid_str), 9)
                    except Exception:
                        pass
            time.sleep(0.2)
        except Exception:
            pass


_dead_ports: set[int] = set()


def _activate_running_instance() -> bool:
    """Плеер уже запущен — показать его окно вместо второй копии."""
    import socket, urllib.error, urllib.request
    if IS_WIN:
        try:
            ctypes.windll.user32.AllowSetForegroundWindow(-1)   # разрешить запущенной копии взять фокус
        except Exception:
            pass
    for port in (HTTP_PORT, 3738, 3739, 3740):
        # Быстрая проверка TCP connect за 150 мс, чтобы не ждать закрытые порты
        try:
            with socket.create_connection((HTTP_HOST, port), timeout=0.15):
                pass
        except OSError:
            continue
        try:
            req = urllib.request.Request(f"http://{HTTP_HOST}:{port}/api/show", data=b"{}")
            urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=1.5).read()
            return True
        except urllib.error.HTTPError:
            continue                        # на порту кто-то другой или старая версия
        except (urllib.error.URLError, TimeoutError) as e:
            is_timeout = isinstance(e, TimeoutError) or isinstance(getattr(e, "reason", None), TimeoutError)
            if is_timeout:
                _kill_zombie_on_port(port)
                _dead_ports.add(port)
            continue
        except Exception:
            continue
    return False


_instance_mutex = None     # держим ссылку до конца процесса: Windows снимет его сама при выходе


def _claim_single_instance() -> bool:
    """Два запуска подряд: порт первой копии ещё не открыт, поэтому спрашиваем Windows."""
    global _instance_mutex, HTTP_PORT
    if not IS_WIN:
        return True
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    for port in (HTTP_PORT, 3738, 3739, 3740, 3741):
        if port in _dead_ports:
            continue
        m = kernel32.CreateMutexW(None, False, f"Local\\VoicePlayer-{port}")
        if ctypes.get_last_error() != 183:      # ERROR_ALREADY_EXISTS
            _instance_mutex = m
            HTTP_PORT = port
            return True
        if m:
            kernel32.CloseHandle(m)
    return False


def _self_test() -> int:
    """Проверка новой версии перед установкой: основные библиотеки и модули загружаются."""
    import edge_tts, miniaudio, numpy, sounddevice  # noqa: F401
    if vp_piper.is_available():
        import piper, lameenc  # noqa: F401
    assert TTS_VOICE and VOICES and callable(render_audio_blocking)
    return 0


def main():
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    if "--gen-key" in sys.argv:
        idx = sys.argv.index("--gen-key")
        seed = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else ""
        print(generate_supporter_key(seed))
        sys.exit(0)
    if len(sys.argv) > 2 and sys.argv[1] == "--feed":   # собранный .exe/.app работает и как хук нейросетей
        try:
            import vp_feed
        except ImportError:
            for p in (APP_DIR / "hooks", Path.home() / ".voice-player" / "hooks"):
                if p.exists() and str(p) not in sys.path:
                    sys.path.insert(0, str(p))
            import vp_feed
        sys.exit(vp_feed.main(sys.argv[1:]))
    if _activate_running_instance() or not _claim_single_instance():
        return
    install_crash_logging()
    try:
        root = tk.Tk()
        root.report_callback_exception = lambda t, v, tb: log_crash("tk-callback", t, v, tb)
        app = VoicePlayerApp(root)
        if "--background" in sys.argv:                  # обновились, пока окно было свёрнуто: не всплывать
            root.iconify()
        root.after(UPDATE_HEALTHY_S * 1000, lambda: vp_updater.mark_healthy(APP_DIR))
        root.after(5000, app._update_tick)
        root.mainloop()
        log_line("=== EXIT main (normal) ===")
    except Exception:
        log_crash("main")
    finally:
        # Гарантированно завершаем процесс на уровне ядра Windows: sounddevice/PortAudio или
        # низкоуровневый хук клавиатуры могут блокировать DLL_PROCESS_DETACH в os._exit(0).
        try:
            sys.stdout.flush(); sys.stderr.flush()
        except Exception:
            pass
        if IS_WIN:
            try:
                ctypes.windll.kernel32.TerminateProcess(ctypes.windll.kernel32.GetCurrentProcess(), 0)
            except Exception:
                pass
        os._exit(0)



if __name__ == "__main__":
    main()
