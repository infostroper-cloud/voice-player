"""Аудиоплееры VoicePlayer: StreamPlayer (miniaudio + audiotsm + sounddevice), MciPlayer и AfPlayer."""
import ctypes
import os
import sys
import threading
import time
from pathlib import Path
from typing import Optional
from uuid import uuid4

_winmm = ctypes.windll.winmm if os.name == "nt" else None
_logger = None


def set_logger(fn) -> None:
    global _logger
    _logger = fn


def log_line(msg: str) -> None:
    if _logger is not None:
        _logger(msg)


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
    и без изменения высоты голоса. Тяжёлый стретч идёт инкрементально (сначала 6 с,
    далее чанками по 15 с в фоновом воркере), поэтому ни UI, ни старт не тормозят."""

    _PLAY_SR = 24000
    _PLAY_CH = 1
    _HEAD_STRETCH_S = 6
    _CHUNK_STRETCH_S = 15

    def __init__(self):
        self._lock = threading.RLock()
        self._stream = None
        self._src = None          # исходный PCM (N, ch) float32
        self._sr = self._PLAY_SR
        self._ch = self._PLAY_CH
        self._out = None          # обработанный сегмент (M, ch) для текущей скорости
        self._out_pos = 0
        self._seg_src_start = 0   # позиция в исходнике, с которой начат текущий сегмент
        self._stretched_up_to = 0 # конец обработанного префикса в кадрах исходника
        self._tsm = None          # потоковый экземпляр WSOLA для бесшовной склейки чанков
        self._speed = 1.0
        self._target_speed = 1.0
        self._volume = 1.0
        self._paused = False
        self._has = False
        self._finished = False
        self._seek_req = None      # запрос перемотки (кадр исходника) для воркера
        self._gen = 0              # номер загруженного трека: дозагрузка не попадёт в чужой
        # воркер перестройки сегмента (скорость + перемотка + фоновый хвост)
        self._speed_evt = threading.Event()
        self._shutdown = False
        threading.Thread(target=self._speed_worker, daemon=True, name="stream-speed").start()

    # ---------- DSP ----------
    @staticmethod
    def _stretch(frames, speed, tsm=None, flush: bool = True):
        if abs(speed - 1.0) < 1e-3:
            if frames is None or frames.shape[0] == 0:
                return frames
            return frames.copy()
        ch = int(frames.shape[1]) if (frames is not None and frames.ndim == 2) else 1
        if tsm is None:
            if frames is None or frames.shape[0] == 0:
                return frames
            tsm = _wsola(channels=ch, speed=speed)
        writer = _ArrayWriter(channels=ch)
        if frames is not None and frames.shape[0] > 0:
            reader = _ArrayReader(frames.T.copy())     # (ch, N)
            tsm.run(reader, writer, flush=flush)
        elif flush:
            finished = False
            while not finished:
                _, finished = tsm.flush_to(writer)
            tsm.clear()
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
                total = self._src.shape[0]
                target = self._target_speed
                seek = self._seek_req
                self._seek_req = None
                sr = self._sr
                src = self._src
                gen = self._gen
                if seek is not None:
                    is_reanchor = True
                    src_start = max(0, min(int(seek), total))
                    chunk_end = (total if abs(target - 1.0) < 1e-3
                                 else min(total, src_start + sr * self._HEAD_STRETCH_S))
                    seg_start = src_start
                    tsm = _wsola(channels=src.shape[1], speed=target) if abs(target - 1.0) >= 1e-3 else None
                elif abs(target - self._speed) >= 1e-3:
                    is_reanchor = True
                    src_start = max(0, min(self._src_pos_now(), total))
                    chunk_end = (total if abs(target - 1.0) < 1e-3
                                 else min(total, src_start + sr * self._HEAD_STRETCH_S))
                    seg_start = src_start
                    tsm = _wsola(channels=src.shape[1], speed=target) if abs(target - 1.0) >= 1e-3 else None
                elif self._stretched_up_to < total:
                    is_reanchor = False
                    src_start = self._stretched_up_to
                    chunk_end = (total if abs(target - 1.0) < 1e-3
                                 else min(total, src_start + sr * self._CHUNK_STRETCH_S))
                    seg_start = self._seg_src_start
                    tsm = self._tsm
                    self._tsm = None
                    if tsm is None and abs(target - 1.0) >= 1e-3:
                        tsm = _wsola(channels=src.shape[1], speed=target)
                else:
                    continue
            flushed = (chunk_end >= total)
            try:
                chunk_out = self._stretch(src[src_start:chunk_end], target, tsm=tsm, flush=flushed)
            except Exception:
                continue
            with self._lock:
                if self._gen != gen or self._src is None:
                    continue
                if self._seek_req is not None or abs(self._target_speed - target) >= 1e-3:
                    self._speed_evt.set()
                    continue
                if is_reanchor:
                    self._out = chunk_out
                    self._out_pos = 0
                    self._seg_src_start = src_start
                    self._stretched_up_to = chunk_end
                    self._tsm = (None if flushed else tsm)
                    self._speed = target
                    self._finished = False
                    if self._stretched_up_to < self._src.shape[0]:
                        self._speed_evt.set()
                else:
                    if (abs(self._speed - target) < 1e-3
                            and self._seg_src_start == seg_start
                            and self._stretched_up_to == src_start):
                        if self._out is None or self._out.shape[0] == 0:
                            self._out = chunk_out
                        elif chunk_out is not None and chunk_out.shape[0] > 0:
                            self._out = _np.concatenate((self._out, chunk_out))
                        self._stretched_up_to = chunk_end
                        self._tsm = (None if flushed else tsm)
                        self._finished = False
                        if self._stretched_up_to < self._src.shape[0]:
                            self._speed_evt.set()
                    else:
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
                    if self._src is None or self._stretched_up_to >= self._src.shape[0]:
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
        dec = _miniaudio.decode_file(
            str(path),
            output_format=_miniaudio.SampleFormat.FLOAT32,
            nchannels=self._PLAY_CH,
            sample_rate=self._PLAY_SR,
        )
        pcm = _np.array(dec.samples, dtype=_np.float32).reshape(-1, dec.nchannels)
        speed = max(0.5, min(2.0, float(speed)))
        total = pcm.shape[0]
        if abs(speed - 1.0) < 1e-3:
            head_end = total
            tsm = None
            out0 = pcm.copy()
        else:
            head_end = min(total, dec.sample_rate * self._HEAD_STRETCH_S)
            tsm = _wsola(channels=dec.nchannels, speed=speed)
            out0 = self._stretch(pcm[:head_end], speed, tsm=tsm, flush=(head_end >= total))
        with self._lock:
            # Переиспользуем ОДИН поток на всю сессию (формат унифицирован до 24 kHz mono):
            # пересоздаём только если формат изменился — иначе просто подменяем буфер.
            need_new = (self._stream is None or self._sr != dec.sample_rate or self._ch != dec.nchannels)
            self._src = pcm; self._sr = dec.sample_rate; self._ch = dec.nchannels
            self._speed = speed; self._target_speed = speed
            self._out = out0; self._out_pos = 0; self._seg_src_start = 0
            self._stretched_up_to = head_end
            self._tsm = (None if head_end >= total else tsm)
            self._seek_req = None
            self._finished = False; self._paused = False; self._has = True
            self._gen += 1
            if need_new:
                self._open_stream()
        if head_end < total:
            self._speed_evt.set()

    def generation(self) -> int:
        with self._lock:
            return self._gen

    def append_file(self, path, gen: int) -> bool:
        """Дописать продолжение в конец играющего трека (длинная реплика озвучивается частями)."""
        dec = _miniaudio.decode_file(
            str(path),
            output_format=_miniaudio.SampleFormat.FLOAT32,
            nchannels=self._PLAY_CH,
            sample_rate=self._PLAY_SR,
        )
        pcm = _np.array(dec.samples, dtype=_np.float32).reshape(-1, dec.nchannels)
        with self._lock:
            if self._src is None or gen != self._gen or dec.sample_rate != self._sr or dec.nchannels != self._ch:
                return False
            speed = self._speed
            can_stretch_eagerly = (abs(speed - 1.0) >= 1e-3 and self._stretched_up_to == self._src.shape[0])
        extra = self._stretch(pcm, speed) if can_stretch_eagerly else None
        with self._lock:
            if self._src is None or gen != self._gen:
                return False
            old_len = self._src.shape[0]
            self._src = _np.concatenate((self._src, pcm))
            self._finished = False
            if (abs(self._speed - 1.0) < 1e-3 and abs(self._target_speed - self._speed) < 1e-3
                    and self._seek_req is None and self._stretched_up_to == old_len):
                self._out = _np.concatenate((self._out, pcm))
                self._stretched_up_to = self._src.shape[0]
            elif (extra is not None and abs(speed - self._speed) < 1e-3
                  and self._seek_req is None and self._stretched_up_to == old_len):
                self._out = _np.concatenate((self._out, extra))
                self._stretched_up_to = self._src.shape[0]
            else:                                  # хвост ещё дотягивается или скорость/позиция меняются
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
            self._src = None; self._out = None; self._tsm = None
            self._stretched_up_to = 0; self._seek_req = None
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


