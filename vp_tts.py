# -*- coding: utf-8 -*-
"""TTS-синтез VoicePlayer: edge-tts (in-proc hedged race + изолированный subprocess fallback),
локальные движки Piper и Kokoro, многочастный рендеринг с уступкой очереди и склейкой."""

from __future__ import annotations

import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import vp_i18n
import vp_kokoro
import vp_piper
from vp_player import _miniaudio
from vp_text import _clean_for_tts, _derive_word_marks, _map_marks_to_chars, _split_for_tts, brief_text

if TYPE_CHECKING:
    from app import Item

_log = lambda msg: None

AUDIO_DIR: Path = Path(".")
TTS_VOICE: str = "en-US-BrianMultilingualNeural"
TTS_RATE: str = "+0%"
TTS_TIMEOUT_S: int = 90
TTS_PARALLEL_PARTS: int = 2
_PIPER_OK: bool = False

_tts_tls = threading.local()


def set_logger(fn) -> None:
    """Установить функцию логирования (обычно app.log_line)."""
    global _log
    _log = fn


def log_line(msg: str) -> None:
    _log(msg)


def configure(
    *,
    audio_dir: Optional[Path] = None,
    tts_voice: Optional[str] = None,
    tts_rate: Optional[str] = None,
    tts_timeout_s: Optional[int] = None,
    tts_parallel_parts: Optional[int] = None,
    piper_ok: Optional[bool] = None,
) -> None:
    """Настроить пути и параметры TTS из app.py."""
    global AUDIO_DIR, TTS_VOICE, TTS_RATE, TTS_TIMEOUT_S, TTS_PARALLEL_PARTS, _PIPER_OK
    if audio_dir is not None:
        AUDIO_DIR = Path(audio_dir)
    if tts_voice is not None:
        TTS_VOICE = str(tts_voice)
    if tts_rate is not None:
        TTS_RATE = str(tts_rate)
    if tts_timeout_s is not None:
        TTS_TIMEOUT_S = int(tts_timeout_s)
    if tts_parallel_parts is not None:
        TTS_PARALLEL_PARTS = int(tts_parallel_parts)
    if piper_ok is not None:
        _PIPER_OK = bool(piper_ok)


def _get_tts_loop():
    """Один переиспользуемый SelectorEventLoop на поток.

    Почему так: `asyncio.run()` создаёт и закрывает НОВЫЙ event loop на каждый вызов.
    На Windows дефолтный loop — ProactorEventLoop, и его повторное создание/закрытие
    в рабочем потоке со временем течёт IOCP-хэндлами -> новые сетевые соединения
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


def _synthesize_inproc(text: str, rate: str, mp3_path: Path, voice: Optional[str] = None) -> tuple:
    """Озвучка edge-tts ВНУТРИ процесса с параллельной hedged-гонкой при сбоях WebSocket."""
    import asyncio
    import edge_tts

    target_voice = voice or TTS_VOICE

    async def _fetch_once(conn_to: float, recv_to: float) -> tuple:
        comm = edge_tts.Communicate(
            text,
            target_voice,
            rate=rate,
            connect_timeout=conn_to,
            receive_timeout=recv_to,
        )
        audio = bytearray()
        sents = []
        words = []
        async for chunk in comm.stream():
            t = chunk.get("type")
            if t == "audio":
                audio.extend(chunk["data"])
            elif t in ("SentenceBoundary", "WordBoundary"):
                start = chunk["offset"] / 1e7
                end = start + chunk["duration"] / 1e7
                (sents if t == "SentenceBoundary" else words).append(
                    (start, end, str(chunk.get("text") or ""))
                )
        if not audio:
            raise RuntimeError("NoAudioReceived: empty stream")
        return bytes(audio), sents, words

    async def _run():
        last_err = None
        for branches, conn_to, recv_to in ((1, 8, 15), (2, 10, 20)):
            if branches == 1:
                try:
                    data, sents, words = await _fetch_once(conn_to, recv_to)
                    Path(mp3_path).write_bytes(data)
                    return sents, words
                except Exception as e:
                    last_err = e
                    continue
            tasks = [asyncio.create_task(_fetch_once(conn_to, recv_to)) for _ in range(branches)]
            try:
                for fut in asyncio.as_completed(tasks):
                    try:
                        data, sents, words = await fut
                        Path(mp3_path).write_bytes(data)
                        return sents, words
                    except Exception as e:
                        last_err = e
            finally:
                pending = [t for t in tasks if not t.done()]
                for t in pending:
                    t.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
        raise last_err or RuntimeError("NoAudioReceived")

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


def _synthesize(text: str, rate: str, mp3_path: Path, voice: Optional[str] = None) -> tuple:
    """Озвучить text. Основной путь для Edge TTS — внутри процесса с переиспользуемым
    SelectorEventLoop (_synthesize_inproc), без задержки на холодный старт python.exe (~0.5с на часть).
    Если внутрипроцессный вызов падает — фолбэк в изолированный дочерний процесс.
    Локальный голос Piper всегда работает внутри процесса."""
    voice = voice or TTS_VOICE
    if vp_kokoro.is_local(voice):
        return vp_kokoro.synthesize(text, voice, mp3_path), []
    mp3_path.parent.mkdir(parents=True, exist_ok=True)
    if vp_piper.is_local(voice):
        return vp_piper.synthesize(text, voice, mp3_path), []
    try:
        return _synthesize_inproc(text, rate, mp3_path, voice)
    except Exception as e:
        if getattr(sys, "frozen", False) or type(e).__name__ == "NoAudioReceived":
            raise
        log_line(f"tts inproc fallback ({voice}): {type(e).__name__}: {e}"[:200])

    import json
    import subprocess

    txt_path = mp3_path.with_suffix(".in.txt")
    marks_path = mp3_path.with_suffix(".marks.json")
    err_path = mp3_path.with_suffix(".err.txt")
    done_path = mp3_path.with_suffix(".done")
    proc = None
    try:
        for p in (marks_path, done_path):
            try:
                p.unlink()
            except Exception:
                pass
        txt_path.write_text(text, encoding="utf-8")
        _t0 = time.time()
        # БЕЗ каналов (pipes): stdin/stdout в DEVNULL, ошибки в файл, данные в файлы.
        # Не ждём корректного выхода дочернего процесса (он по неясной причине иногда
        # зависает на выходе именно когда порождён из этого долгоживущего процесса) —
        # опрашиваем файл-сигнал .done, который ребёнок пишет последним.
        errf = open(err_path, "wb")
        try:
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    _TTS_CHILD_CODE,
                    str(txt_path),
                    str(mp3_path),
                    str(marks_path),
                    voice or TTS_VOICE,
                    rate,
                    str(done_path),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=errf,
                close_fds=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            deadline = _t0 + min(TTS_TIMEOUT_S, 30)
            while time.time() < deadline:
                if done_path.exists():
                    break  # готово (звук + тайминги записаны)
                if proc.poll() is not None:
                    break  # процесс завершился (успех/ошибка)
                time.sleep(0.15)
        finally:
            errf.close()
        done = done_path.exists()
        # прибиваем дочерний процесс (если ещё жив — например, завис на выходе)
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass
        if not done:
            err_txt = ""
            try:
                err_txt = err_path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                pass
            last = (err_txt.strip().splitlines() or ["TimeoutError: рендер не завершился"])[-1]
            raise RuntimeError(last[:200])
        if os.environ.get("VP_TTS_DEBUG"):
            log_line(f"  tts subprocess за {time.time()-_t0:.1f}с voice={voice}")
        sents = []
        words = []
        if marks_path.exists():
            data = json.loads(marks_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):  # новый формат {sents, words}
                for row in data.get("sents", []):
                    sents.append((row[0], row[1], row[2]))
                for row in data.get("words", []):
                    words.append((row[0], row[1], row[2]))
            else:  # старый формат — список предложений
                for row in data:
                    sents.append((row[0], row[1], row[2]))
        return sents, words
    finally:
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass
        for p in (txt_path, marks_path, err_path, done_path):
            try:
                p.unlink()
            except Exception:
                pass


def _audio_duration(path: Path, fallback: float) -> float:
    if _miniaudio is None:
        return fallback
    try:
        return float(_miniaudio.get_file_info(str(path)).duration)
    except Exception:
        return fallback


def _find_local_fallback_voice(text: str = "") -> str:
    """Найти реально установленную на диске модель Piper для офлайн-фолбэка."""
    if not _PIPER_OK or not vp_piper.VOICES_DIR.is_dir():
        return ""
    if vp_piper.is_local(TTS_VOICE):
        m = vp_piper.VOICES_DIR / (TTS_VOICE.removeprefix(vp_piper.VOICE_PREFIX) + ".onnx")
        if m.is_file() and m.with_suffix(".onnx.json").is_file():
            return TTS_VOICE
    installed = sorted(
        p.stem
        for p in vp_piper.VOICES_DIR.glob("*.onnx")
        if p.with_suffix(".onnx.json").is_file()
    )
    if not installed:
        return ""
    has_cyr = any("а" <= ch.lower() <= "я" or ch.lower() == "ё" for ch in (text or ""))
    pref_pfx = ("ru_",) if has_cyr else ("en_", "ru_")
    for pfx in pref_pfx:
        for stem in installed:
            if stem.startswith(pfx):
                return vp_piper.VOICE_PREFIX + stem
    return vp_piper.VOICE_PREFIX + installed[0]


def _has_local_fallback_voice(voice: str) -> bool:
    return False


def _synthesize_with_retries(
    text: str,
    rate: str,
    mp3_file: Path,
    voice: str,
    item_id: str,
    cancel_evt: Optional[threading.Event] = None,
    synthesize_fn=None,
) -> tuple:
    """Одна часть: (sents, words), либо RuntimeError после всех попыток."""
    synth = synthesize_fn or _synthesize
    last_err = "render failed"
    is_local = vp_piper.is_local(voice) or vp_kokoro.is_local(voice)
    max_retries = 0 if is_local else 1
    for attempt in range(1, max_retries + 2):
        if cancel_evt is not None and cancel_evt.is_set():
            raise RuntimeError("cancelled")
        try:
            sents, words = synth(text, rate, mp3_file, voice)
            if mp3_file.exists() and mp3_file.stat().st_size > 0:
                if attempt > 1:
                    log_line(f"render OK [{item_id}] attempt={attempt} text='{text[:60]}'")
                return sents, words
            last_err = "озвучка вернула пустой файл"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"[:300]
            try:
                if mp3_file.exists() and mp3_file.stat().st_size == 0:
                    mp3_file.unlink()
            except OSError:
                pass
        if cancel_evt is not None and cancel_evt.is_set():
            raise RuntimeError("cancelled")
        log_line(f"render FAIL [{item_id}] {mp3_file.stem} attempt={attempt}/{max_retries+1}: {last_err}")
        if attempt <= max_retries:
            delay = 0.6
            if cancel_evt is not None:
                if cancel_evt.wait(delay):
                    raise RuntimeError("cancelled")
            else:
                time.sleep(delay)
    try:
        if mp3_file.exists() and mp3_file.stat().st_size == 0:
            mp3_file.unlink()
    except OSError:
        pass
    raise RuntimeError(last_err)


def _cleanup_rendered_parts(item: "Item", keep_path: Path):
    """Удалить временные файлы кусков (.pXXX.mp3) и оставить в parts_done только целый файл."""
    keep_p = Path(keep_path)
    for p in getattr(item, "parts_done", ()):
        try:
            pp = Path(p)
            if pp != keep_p:
                pp.unlink(missing_ok=True)
        except Exception:
            pass
    if keep_p.exists():
        item.parts_done = [keep_p]
        item.parts_fed = 1


def render_audio_blocking(
    item: "Item",
    rate: Optional[str] = None,
    voice: Optional[str] = None,
    lead: str = "",
    on_part=None,
    should_yield=None,
    *,
    audio_dir: Optional[Path] = None,
    synthesize_fn=None,
    brief: bool = False,
    auto_voice_lang: bool = False,
) -> None:
    """Озвучить реплику. Длинную — частями: каждая часть короткая и не упирается в таймаут,
    on_part вызывается по порядку, как только готова очередная часть (можно играть, не дожидаясь
    всего). Упала часть — готовые остаются, повтор продолжит с места сбоя."""
    if rate is None:
        rate = TTS_RATE
    if voice is None:
        voice = TTS_VOICE
    target_audio_dir = Path(audio_dir) if audio_dir is not None else AUDIO_DIR

    body = _clean_for_tts(item.text)
    if brief and len(body) > 420:
        body = brief_text(body)
    if not body or not any(ch.isalnum() for ch in body):
        item.audio_error = "пустой текст"
        log_line(f"render skip [{item.id}]: empty text")
        return

    # Автоопределение языка текста: срабатывает если голос не задан,
    # либо если включён auto_voice_lang и язык текста не совпадает с языком выбранного голоса.
    if not item.parts_done:
        detected = vp_i18n.detect_text_language(body)
        if not voice:
            if detected in vp_i18n.DEFAULT_VOICES_BY_LANG:
                voice = vp_i18n.DEFAULT_VOICES_BY_LANG[detected]
            else:
                voice = TTS_VOICE
        elif auto_voice_lang and detected in vp_i18n.DEFAULT_VOICES_BY_LANG and "Multilingual" not in voice:
            vlow = voice.lower()
            if vlow.startswith("piper:ru_") or vlow.startswith("ru-"):
                v_lang = "ru"
            elif vlow.startswith("kokoro:") or vlow.startswith("en-"):
                v_lang = "en"
            else:
                v_lang = vlow.split("-", 1)[0]
            if v_lang != detected:
                voice = vp_i18n.DEFAULT_VOICES_BY_LANG[detected]

    spans = [(a, b) for a, b in _split_for_tts(body) if any(ch.isalnum() for ch in body[a:b])]
    if not spans:
        item.audio_error = "пустой текст"
        log_line(f"render skip [{item.id}]: no speakable text")
        return
    mp3_file = target_audio_dir / f"{item.id}.mp3"
    if not item.parts_done:
        item.marks = []
        item.word_marks = []
        item.rendered_voice = voice
    else:
        voice = item.rendered_voice or voice  # голоса пишут звук в разном формате, склеить их нельзя
    done = len(item.parts_done)
    t_offset = sum(_audio_duration(p, 0.0) for p in item.parts_done)
    item.more_parts = len(spans) > 1

    def part_file(i: int) -> Path:
        return mp3_file if len(spans) == 1 else target_audio_dir / f"{item.id}.p{i:03d}.mp3"

    def part_text(i: int) -> str:
        a, b = spans[i]
        chunk = body[a:b].strip()
        # lead (имя автора голосом) звучит перед телом; тайминги маппим по телу, лид в них не попадёт
        return (lead.rstrip(". ") + ".\n" + chunk) if (lead and i == 0) else chunk

    def _commit_part(i: int, sents, words) -> None:
        nonlocal t_offset
        a, b = spans[i]
        shift = lambda marks: [(s + t_offset, e + t_offset, ca + a, cb + a) for s, e, ca, cb in marks]
        chunk = body[a:b]
        item.marks = item.marks + shift(_map_marks_to_chars(chunk, sents))
        new_words = (
            _map_marks_to_chars(chunk, words)
            if words
            else _derive_word_marks(chunk, _map_marks_to_chars(chunk, sents))
        )
        item.word_marks = item.word_marks + shift(new_words)
        t_offset += _audio_duration(part_file(i), sents[-1][1] if sents else 0.0)
        item.parts_done = item.parts_done + [part_file(i)]
        item.audio_error = None
        item.rendered_rate = rate
        if i == 0:
            item.audio_path = part_file(0)
        if on_part is not None:
            item.audio_ready = True  # уже можно играть — остальное догрузится
            on_part(item)

    cancel_evt = threading.Event()
    is_local_engine = vp_piper.is_local(voice) or vp_kokoro.is_local(voice)
    if is_local_engine:
        try:
            for i in range(done, len(spans)):
                sents, words = _synthesize_with_retries(
                    part_text(i), rate, part_file(i), voice, item.id, cancel_evt, synthesize_fn=synthesize_fn
                )
                _commit_part(i, sents, words)
                if i + 1 < len(spans) and should_yield is not None and should_yield(item):
                    return  # уступаем место срочным/новым репликам; дорендерим позже
        except Exception as e:
            cancel_evt.set()
            item.audio_error = str(e)[:300] or "render failed"
            if on_part is None or not item.parts_done:
                item.audio_ready = False
    else:
        with ThreadPoolExecutor(max_workers=TTS_PARALLEL_PARTS, thread_name_prefix="tts-part") as pool:
            try:
                for batch_start in range(done, len(spans), TTS_PARALLEL_PARTS):
                    batch_end = min(batch_start + TTS_PARALLEL_PARTS, len(spans))
                    futures = {
                        i: pool.submit(
                            _synthesize_with_retries,
                            part_text(i),
                            rate,
                            part_file(i),
                            voice,
                            item.id,
                            cancel_evt,
                            synthesize_fn=synthesize_fn,
                        )
                        for i in range(batch_start, batch_end)
                    }
                    for i in range(batch_start, batch_end):
                        sents, words = futures[i].result()
                        _commit_part(i, sents, words)
                    if batch_end < len(spans) and should_yield is not None and should_yield(item):
                        return  # уступаем место срочным/новым репликам; дорендерим позже
            except Exception as e:
                cancel_evt.set()
                item.audio_error = str(e)[:300] or "render failed"
                if on_part is None or not item.parts_done:
                    item.audio_ready = False
    if item.audio_error:
        return
    if len(spans) > 1:  # склеить в один файл: перемотка, сохранение, запуск заново
        try:
            headers = [Path(p).read_bytes()[:4] == b"RIFF" for p in item.parts_done]
            is_wav = all(headers)
            is_mixed = any(headers) and not is_wav
            if not is_mixed:
                tmp = mp3_file.with_suffix(".tmp")
                if is_wav:
                    import wave

                    with wave.open(str(tmp), "wb") as w_out:
                        params_set = False
                        for p in item.parts_done:
                            with wave.open(str(p), "rb") as w_in:
                                if not params_set:
                                    w_out.setparams(w_in.getparams())
                                    params_set = True
                                w_out.writeframes(w_in.readframes(w_in.getnframes()))
                else:
                    with open(tmp, "wb") as out:
                        for p in item.parts_done:
                            out.write(Path(p).read_bytes())
                tmp.replace(mp3_file)
                item.audio_path = mp3_file
                if on_part is None:
                    _cleanup_rendered_parts(item, mp3_file)
        except Exception as e:
            log_line(f"render concat [{item.id}]: {e}")
    else:
        item.audio_path = mp3_file
    item.audio_ready = True
    item.more_parts = False
    log_line(f"render OK [{item.id}] voice={voice} parts={len(spans)}")
