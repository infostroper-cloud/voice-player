"""Автономный графический установщик VoicePlayer-Setup-<ver>.exe (собирается через PyInstaller --onefile).

Что делает:
1. Внутри .exe уже вшит актуальный архив voice-player-<ver>.zip и иконка icon.ico.
2. Показывает аккуратное окно в фирменном изумрудно-мятном стиле (или ставит тихо при флаге /SILENT).
3. Находит установленный Python 3.10+, а если на компьютере пользователя Python ещё не стоит —
   автоматически скачивает и тихо ставит официальный Python 3.12 с python.org.
4. Распаковывает VoicePlayer в ~/.voice-player/versions/<ver>/, ставит зависимости, проверяет --self-test,
   создаёт ярлыки на Рабочем столе и в меню «Пуск» и запускает плеер!
"""
from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
import urllib.request
import zipfile
from pathlib import Path
from tkinter import ttk, messagebox

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
PY_INSTALLER_URL = "https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe"


def _res_dir() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def _bundled_zip() -> Path:
    d = _res_dir()
    zips = sorted(d.glob("voice-player-*.zip"))
    if zips:
        return zips[-1]
    out_zips = sorted((Path(__file__).resolve().parent / "Output").glob("voice-player-*.zip"))
    if out_zips:
        return out_zips[-1]
    raise FileNotFoundError("Внутри установщика не найден архив voice-player-*.zip")


def _find_system_python() -> str | None:
    """Найти рабочий системный python.exe (>= 3.10) вне текущего PyInstaller-процесса."""
    candidates = []
    for cmd in ("python", "python3"):
        p = shutil.which(cmd)
        if p and "WindowsApps" not in p:
            candidates.append(p)
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Python"
    if local.is_dir():
        for d in sorted(local.glob("Python3*"), reverse=True):
            if (d / "python.exe").is_file():
                candidates.append(str(d / "python.exe"))
    local_core = Path(os.environ.get("LOCALAPPDATA", "")) / "Python"
    if local_core.is_dir():
        for d in sorted(local_core.glob("pythoncore-*"), reverse=True):
            if (d / "python.exe").is_file():
                candidates.append(str(d / "python.exe"))
    for c in candidates:
        try:
            r = subprocess.run([c, "-c", "import sys; assert sys.version_info >= (3, 10); print(sys.executable)"],
                               capture_output=True, text=True, timeout=10, creationflags=NO_WINDOW)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip().splitlines()[-1]
        except Exception:
            continue
    return None


def _ensure_python(status_cb) -> str:
    py = _find_system_python()
    if py:
        return py
    status_cb("Устанавливаю среду Python 3.12 (один раз при первой установке)...")
    with tempfile.TemporaryDirectory() as td:
        exe = Path(td) / "python-setup.exe"
        urllib.request.urlretrieve(PY_INSTALLER_URL, str(exe))
        subprocess.run([str(exe), "/quiet", "InstallAllUsers=0", "PrependPath=1", "Include_test=0", "Include_tcltk=1"],
                       check=True, timeout=600, creationflags=NO_WINDOW)
    py = _find_system_python()
    if not py:
        raise RuntimeError("Не удалось автоматически установить Python 3.12. Установите Python с python.org.")
    return py


def run_install(status_cb, start_app: bool = True) -> str:
    zp = _bundled_zip()
    py = _ensure_python(status_cb)
    status_cb("Распаковываю VoicePlayer и проверяю аудио-библиотеки...")
    with tempfile.TemporaryDirectory() as td:
        with zipfile.ZipFile(zp) as z:
            z.extract("vp_updater.py", td)
        updater_py = Path(td) / "vp_updater.py"
        r = subprocess.run([py, str(updater_py), "--install", str(zp)],
                           capture_output=True, text=True, timeout=900, creationflags=NO_WINDOW)
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "ошибка установки").strip().splitlines()[-1]
            raise RuntimeError(err[:240])
        try:
            sys.path.insert(0, td)
            import vp_updater
            vp_updater.register_windows_uninstaller(zp.stem.replace("voice-player-", ""))
        except Exception:
            pass
    launch_pyw = Path.home() / ".voice-player" / "launch.pyw"
    pyw = str(Path(py).with_name("pythonw.exe")) if Path(py).with_name("pythonw.exe").is_file() else py
    if start_app and launch_pyw.is_file():
        status_cb("Запускаю VoicePlayer...")
        subprocess.Popen([pyw, str(launch_pyw)], creationflags=NO_WINDOW)
    return zp.stem.replace("voice-player-", "")


def _apply_dark_titlebar(win: tk.Tk):
    if os.name != "nt":
        return
    try:
        win.update_idletasks()
        wid = win.winfo_id()
        hwnd = ctypes.windll.user32.GetParent(wid) or wid
        dwmapi = ctypes.windll.dwmapi
        one = ctypes.c_int(1)
        for attr in (20, 19):
            dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(one), ctypes.sizeof(one))
        cap = ctypes.c_uint(0x00151613)
        txt = ctypes.c_uint(0x00EAECE8)
        dwmapi.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(cap), ctypes.sizeof(cap))
        dwmapi.DwmSetWindowAttribute(hwnd, 36, ctypes.byref(txt), ctypes.sizeof(txt))
    except Exception:
        pass


def main():
    if "/SILENT" in [a.upper() for a in sys.argv[1:]]:
        run_install(lambda _m: None, start_app=True)
        return

    root = tk.Tk()
    root.title("Установка VoicePlayer")
    root.geometry("500x260")
    root.resizable(False, False)
    root.configure(bg="#131615")
    ico = _res_dir() / "icon.ico"
    if ico.is_file():
        try:
            root.iconbitmap(default=str(ico))
        except Exception:
            pass
    _apply_dark_titlebar(root)

    # Центрируем окно
    root.update_idletasks()
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"500x260+{(sw - 500) // 2}+{(sh - 260) // 2}")

    frame = tk.Frame(root, bg="#131615", padx=28, pady=24)
    frame.pack(fill="both", expand=True)

    tk.Label(frame, text="VoicePlayer — Голосовой плеер ИИ", bg="#131615", fg="#e8ecea",
             font=("Segoe UI Semibold", 14), anchor="w").pack(fill="x")
    tk.Label(frame, text="Автоозвучка диалогов из Antigravity, Codex, Claude Code и Cursor\nлокальным нейроголосом без интернета и подписок.",
             bg="#131615", fg="#7c8782", font=("Segoe UI", 9), justify="left", anchor="w").pack(fill="x", pady=(6, 18))

    lbl_status = tk.Label(frame, text="Готов к установке (ярлык появится на Рабочем столе)",
                          bg="#131615", fg="#7ee2b8", font=("Segoe UI Semibold", 10), anchor="w")
    lbl_status.pack(fill="x", pady=(0, 10))

    bar = ttk.Progressbar(frame, mode="indeterminate", length=440)
    bar.pack(fill="x", pady=(0, 18))

    btn_row = tk.Frame(frame, bg="#131615")
    btn_row.pack(fill="x")

    def set_status(msg: str):
        root.after(0, lambda: lbl_status.configure(text=msg))

    def start_install():
        btn_install.configure(state="disabled", text="Установка...")
        bar.start(12)

        def worker():
            try:
                ver = run_install(set_status, start_app=True)
                root.after(0, lambda: on_done(ver))
            except Exception as e:
                root.after(0, lambda err=str(e): on_err(err))

        threading.Thread(target=worker, daemon=True).start()

    def on_done(ver: str):
        bar.stop()
        lbl_status.configure(text=f"✓ VoicePlayer {ver} установлен и запущен!")
        btn_install.configure(state="normal", text="Готово (Закрыть)", command=root.destroy)
        root.after(1800, root.destroy)

    def on_err(err: str):
        bar.stop()
        lbl_status.configure(text="⚠ Ошибка установки", fg="#ff6b6b")
        btn_install.configure(state="normal", text="Повторить")
        messagebox.showerror("Установка VoicePlayer", err)

    btn_install = tk.Button(
        btn_row, text="Установить и запустить VoicePlayer", command=start_install,
        bg="#7ee2b8", fg="#0d0f0e", activebackground="#6ad6a9", activeforeground="#0d0f0e",
        font=("Segoe UI Semibold", 10), bd=0, padx=18, pady=8, cursor="hand2"
    )
    btn_install.pack(side="left")

    tk.Button(
        btn_row, text="Отмена", command=root.destroy,
        bg="#1c211f", fg="#e8ecea", activebackground="#262e2b", activeforeground="#e8ecea",
        font=("Segoe UI", 10), bd=0, padx=14, pady=8, cursor="hand2"
    ).pack(side="right")

    root.mainloop()


if __name__ == "__main__":
    main()
