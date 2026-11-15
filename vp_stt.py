"""Распознавание речи (Whisper) и запись звука с микрофона / системы."""

import ctypes
import os
import threading
from typing import Optional

_log_fn = None


def set_logger(fn):
    global _log_fn
    _log_fn = fn


def _log(msg: str):
    if _log_fn is not None:
        try:
            _log_fn(msg)
        except Exception:
            pass


_WhisperModel = None
_WHISPER_CHECKED = False
_WHISPER_OK = False
_WHISPER_IMPORT_ERR = ""


def _ensure_whisper() -> bool:
    global _WhisperModel, _WHISPER_CHECKED, _WHISPER_OK, _WHISPER_IMPORT_ERR
    if not _WHISPER_CHECKED:
        _WHISPER_CHECKED = True
        try:
            from faster_whisper import WhisperModel as _WM
            _WhisperModel = _WM
            _WHISPER_OK = True
        except Exception as _e:
            _WHISPER_OK = False
            _WHISPER_IMPORT_ERR = str(_e)
    return _WHISPER_OK


WHISPER_MODEL_SIZE = "base"   # tiny/base/small/medium — баланс скорость/качество
_whisper_holder = {"model": None, "size": None, "lock": threading.Lock()}


def _get_whisper(size: str):
    if not _ensure_whisper():
        raise RuntimeError(f"faster-whisper не доступен: {_WHISPER_IMPORT_ERR}")
    with _whisper_holder["lock"]:
        if _whisper_holder["model"] is None or _whisper_holder["size"] != size:
            _whisper_holder["model"] = _WhisperModel(size, device="cpu", compute_type="int8")
            _whisper_holder["size"] = size
        return _whisper_holder["model"]


def transcribe_array(audio, model_size: str = WHISPER_MODEL_SIZE,
                     language: Optional[str] = None, translate: bool = False,
                     word_level: bool = False):
    """Распознать речь напрямую из 1D float32 16kHz numpy-массива (без записи WAV на диск).
    translate=True — переводит в английский. word_level=True — тайминги по словам.
    Возвращает (text, marks). Блокирующая."""
    import numpy as _np
    arr = _np.asarray(audio, dtype=_np.float32)
    if arr.ndim > 1:
        arr = arr.mean(axis=1)
    model = _get_whisper(model_size)
    segments, _info = model.transcribe(arr, language=(None if translate else language),
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
    return transcribe_array(audio, model_size=model_size, language=language,
                            translate=translate, word_level=word_level)


class MicRecorder:
    def __init__(self, samplerate=16000, channels=1):
        self.sr = samplerate
        self.ch = channels
        self._frames = []
        self._stream = None
        self._rec = False
        self.level: float = 0.0

    def start(self, device=None):
        import sounddevice as sd
        import numpy as _np
        self._frames = []
        self._rec = True
        self.level = 0.0

        def cb(indata, frames, t, status):
            if self._rec:
                self._frames.append(indata.copy())
                try:
                    peak = float(_np.abs(indata).max()) if indata.size else 0.0
                    self.level = min(1.0, max(peak * 2.2, self.level * 0.55))
                except Exception:
                    pass

        self._stream = sd.InputStream(samplerate=self.sr, channels=self.ch,
                                      dtype="float32", callback=cb, device=device)
        self._stream.start()

    def _stop_stream(self):
        self._rec = False
        self.level = 0.0
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    def stop_array(self):
        """Остановить запись и вернуть 1D float32 16kHz numpy-массив в памяти (или None, если пусто)."""
        import numpy as np
        self._stop_stream()
        if not self._frames:
            return None
        data = np.concatenate(self._frames, axis=0)
        if data.ndim > 1:
            data = data.mean(axis=1)
        return data.astype(np.float32, copy=False)

    def stop(self, path) -> bool:
        import numpy as np
        import wave
        self._stop_stream()
        if not self._frames:
            return False
        data = np.concatenate(self._frames, axis=0)
        pcm = (np.clip(data, -1, 1) * 32767).astype("<i2")
        with wave.open(str(path), "wb") as w:
            w.setnchannels(self.ch)
            w.setsampwidth(2)
            w.setframerate(self.sr)
            w.writeframes(pcm.tobytes())
        return True


def _write_wav(frames, path, sr=16000):
    import numpy as np
    import wave
    if not frames:
        return False
    data = np.concatenate(frames, axis=0)
    if data.ndim > 1:
        data = data.mean(axis=1)
    pcm = (np.clip(data, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
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
        self.sr = samplerate
        self._frames = []
        self._rec = False
        self._stream = None
        self._thread = None
        self._mode = None

    def start(self):
        self._frames = []
        self._rec = True
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
                        try:
                            ctypes.windll.ole32.CoInitializeEx(None, 0)   # MTA
                        except Exception:
                            pass
                    import soundcard as sc
                    spk = sc.default_speaker()
                    mic = sc.get_microphone(spk.id, include_loopback=True)
                    with mic.recorder(samplerate=self.sr, channels=1) as r:
                        self._ready.set()                # рекордер открыт и пишет
                        while self._rec:
                            self._frames.append(r.record(numframes=self.sr // 4))
                except Exception as e:
                    _log(f"system record error: {e}")
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
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        return _write_wav(self._frames, path, self.sr)
