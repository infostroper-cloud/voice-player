"""Локальная нейросетевая озвучка через Kokoro-82M (StyleTTS2 ONNX, уровень Speechify).

Работает полностью на этом компьютере через onnxruntime, без зависимости от серверов Microsoft.
Модель (~138 МБ) и словарь ударений RUAccent автоматически скачиваются при первом выборе голоса.
"""
import importlib.util
import json
import os
import re
import threading
from functools import lru_cache
from pathlib import Path

import vp_piper

VOICE_PREFIX = "kokoro:"
KOKORO_DIR = Path.home() / ".voice-player" / "kokoro"
HF_REPO = "zaakirio/kokoro-ru"
SAMPLE_RATE = 24000
SENTENCE_PAUSE_S = 0.20
MP3_BITRATE = 128
MAX_IPA_LEN = 480

VOICE_LABELS = {
    "★ Света HD — локальный (жен, Kokoro)": VOICE_PREFIX + "sveta",
    "★ Маша HD — локальный (жен, Kokoro)": VOICE_PREFIX + "masha",
    "★ Дима HD — локальный (муж, Kokoro)": VOICE_PREFIX + "dima",
}

_VOICE_FILES = {
    "sveta": ("onnx/model_quantized.onnx", "voices/sveta.bin"),
    "masha": ("onnx/model_quantized.onnx", "voices/masha.bin"),
    "dima": ("onnx/model_dima_quantized.onnx", "voices/dima.bin"),
}

_lock = vp_piper._lock
_g2p = None
_sessions = {}
_styles = {}


def is_local(voice: str) -> bool:
    return bool(voice) and voice.startswith(VOICE_PREFIX)


def is_available() -> bool:
    return all(
        importlib.util.find_spec(m) is not None
        for m in ("onnxruntime", "numpy", "piper", "lameenc", "ruaccent", "huggingface_hub")
    )


def _ensure_assets(name: str) -> tuple:
    from huggingface_hub import hf_hub_download

    if name not in _VOICE_FILES:
        name = "sveta"
    onnx_rel, style_rel = _VOICE_FILES[name]
    KOKORO_DIR.mkdir(parents=True, exist_ok=True)

    for fname in ("ru_g2p.py", "tokenizer.json", "kokoro-config.json"):
        target = KOKORO_DIR / fname
        if not target.is_file() or target.stat().st_size == 0:
            hf_hub_download(repo_id=HF_REPO, filename=fname, local_dir=str(KOKORO_DIR))

    onnx_path = KOKORO_DIR / onnx_rel
    if not onnx_path.is_file() or onnx_path.stat().st_size == 0:
        hf_hub_download(repo_id=HF_REPO, filename=onnx_rel, local_dir=str(KOKORO_DIR))

    style_path = KOKORO_DIR / style_rel
    if not style_path.is_file() or style_path.stat().st_size == 0:
        hf_hub_download(repo_id=HF_REPO, filename=style_rel, local_dir=str(KOKORO_DIR))

    return onnx_path, style_path


class _KokoroRuG2P:
    """RUAccent + ru_g2p + встроенный espeakbridge из piper (без misaki/phonemizer)."""

    def __init__(self, base_dir: Path):
        import importlib.util as ilu
        from ruaccent import RUAccent

        spec = ilu.spec_from_file_location("_kokoro_ru_g2p", str(base_dir / "ru_g2p.py"))
        mod = ilu.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self._mod = mod

        self._accent = RUAccent()
        self._accent.load(omograph_model_size="turbo3.1", use_dictionary=True, tiny_mode=True)
        mod._patch_token_type_ids(self._accent)

        tok_data = json.loads((base_dir / "tokenizer.json").read_text(encoding="utf-8"))
        self._vocab = tok_data["model"]["vocab"]
        self._cached_ids = lru_cache(maxsize=2048)(self._text_to_ids_uncached)

    def _phonemize_espeak(self, marked: str) -> str:
        from piper import espeakbridge

        vp_piper.ensure_espeak("ru")
        parts = []
        for ph, term, _eos in espeakbridge.get_phonemes(marked):
            ph = re.sub(r"\([^)]+\)", "", ph)
            parts.append(ph + term)
        return " ".join(p.strip() for p in parts if p.strip())

    def _text_to_ids_uncached(self, text: str) -> tuple:
        spoken = vp_piper.speakable(text)
        bracketed = self._mod.RuG2P._brackets(spoken)
        marked = self._mod._plus_to_acute(self._accent.process_all(bracketed))
        marked = self._mod.respell(marked.lower())
        ps = self._phonemize_espeak(marked)
        for old, new in self._mod.NORMALIZE.items():
            ps = ps.replace(old, new)
        ps = self._mod.lengthen_sch(self._mod.reduce_vowels(ps))
        ps = re.sub(r"\s+", " ", ps).strip()
        return tuple(self._vocab[c] for c in ps if c in self._vocab)

    def text_to_ids(self, text: str) -> list:
        return list(self._cached_ids(text))


def _get_engine(voice: str):
    global _g2p
    import numpy as np
    import onnxruntime as ort

    name = voice[len(VOICE_PREFIX):] if is_local(voice) else "sveta"
    if name not in _VOICE_FILES:
        name = "sveta"
    onnx_path, style_path = _ensure_assets(name)

    if _g2p is None:
        _g2p = _KokoroRuG2P(KOKORO_DIR)

    onnx_key = str(onnx_path)
    if onnx_key not in _sessions:
        _sessions.clear()  # держим в памяти одну ONNX-модель (~138 МБ)
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = min(4, max(2, (os.cpu_count() or 4) // 2))
        _sessions[onnx_key] = ort.InferenceSession(
            onnx_key, sess_options=opts, providers=["CPUExecutionProvider"]
        )

    if name not in _styles:
        _styles[name] = np.fromfile(str(style_path), dtype=np.float32).reshape(-1, 256)

    return _g2p, _sessions[onnx_key], _styles[name]


def preload(voice: str) -> None:
    """Скачать и загрузить модель Kokoro заранее."""
    with _lock:
        _get_engine(voice)


def _split_ids(ids: list, limit: int = MAX_IPA_LEN) -> list:
    if len(ids) <= limit:
        return [ids] if ids else []
    return [ids[i:i + limit] for i in range(0, len(ids), limit)]


def synthesize(text: str, voice: str, mp3_path: Path) -> list:
    """Синтезировать речь в mp3 и вернуть тайминги предложений [(start, end, sentence)]."""
    import lameenc
    import numpy as np

    marks, pcm = [], bytearray()
    t = 0.0
    for sentence in vp_piper.split_sentences(text):
        with _lock:
            g2p, sess, style_pack = _get_engine(voice)
            input_names = {i.name for i in sess.get_inputs()}
            speed_arr = np.array([1.0], dtype=np.float32)
            ids = g2p.text_to_ids(sentence)
            if not ids:
                continue
            start = t
            for chunk_ids in _split_ids(ids):
                idx = min(max(len(chunk_ids) - 1, 0), len(style_pack) - 1)
                feed = {
                    "input_ids": np.array([[0, *chunk_ids, 0]], dtype=np.int64),
                    "style": style_pack[idx][None],
                }
                if "speed" in input_names:
                    feed["speed"] = speed_arr
                audio = np.asarray(sess.run(None, feed)[0], dtype=np.float32).reshape(-1)
                if audio.size == 0:
                    continue
                fade = min(72, audio.size // 4)
                if fade > 1:
                    ramp = np.linspace(0.0, 1.0, fade, dtype=np.float32)
                    audio = audio.copy()
                    audio[:fade] *= ramp
                    audio[-fade:] *= ramp[::-1]
                audio_i16 = np.clip(audio * 32767.0, -32768.0, 32767.0).astype("<i2")
                pcm += audio_i16.tobytes()
                t += audio_i16.size / SAMPLE_RATE
            if t > start:
                marks.append((start, t, sentence))
                gap = int(SENTENCE_PAUSE_S * SAMPLE_RATE)
                pcm += b"\0\0" * gap
                t += gap / SAMPLE_RATE

    if not pcm:
        raise RuntimeError("в тексте нечего озвучить")

    enc = lameenc.Encoder()
    enc.set_bit_rate(MP3_BITRATE)
    enc.set_in_sample_rate(SAMPLE_RATE)
    enc.set_channels(1)
    enc.set_quality(2)
    tmp = mp3_path.with_name(mp3_path.name + ".tmp")
    tmp.write_bytes(bytes(enc.encode(bytes(pcm))) + bytes(enc.flush()))
    tmp.replace(mp3_path)
    return marks
