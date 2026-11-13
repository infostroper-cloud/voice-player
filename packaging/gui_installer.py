"""Классический многостраничный графический установщик VoicePlayer-Setup-<ver>.exe.

Шаги мастера установки:
1. Лицензионное соглашение (EULA / MIT License) с переключателями принятия условий.
2. Выбор папки установки с кнопкой «Обзор...» и расчётом свободного места на диске.
3. Выбор дополнительных задач: ярлык на Рабочем столе, в меню «Пуск», автозапуск.
4. Процесс установки: проверка Python, распаковка, настройка библиотек, регистрация uninstaller.
5. Завершение мастера: чекбокс «Запустить VoicePlayer» и кнопка «Завершить».

Поддерживает также тихий режим (/SILENT или /VERYSILENT).
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
from tkinter import ttk, filedialog, messagebox

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
PY_INSTALLER_URL = "https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe"

APP_VERSION_DEFAULT = "1.2.5"

LICENSE_TEXT = """ЛИЦЕНЗИОННОЕ СОГЛАШЕНИЕ С КОНЕЧНЫМ ПОЛЬЗОВАТЕЛЕМ (EULA)
для программного обеспечения «VoicePlayer» (версия 1.2.x и последующие)

ВАЖНО: Внимательно ознакомьтесь с условиями настоящего Соглашения перед началом установки. Установка, копирование или иное использование Программы означает ваше полное и безоговорочное согласие со всеми положениями настоящего Лицензионного соглашения.

1. ПРЕДМЕТ СОГЛАШЕНИЯ И ПРЕДОСТАВЛЕНИЕ ЛИЦЕНЗИИ
1.1. Настоящее Лицензионное соглашение является юридическим документом, заключаемым между вами (физическим или юридическим лицом) и разработчиком программного обеспечения VoicePlayer.
1.2. VoicePlayer является свободным программным обеспечением с открытым исходным кодом, распространяемым по международной лицензии MIT License.
1.3. Пользователю предоставляется бесплатная, неисключительная, бессрочная лицензия на использование Программы на неограниченном количестве вычислительных устройств для любых личных, образовательных, профессиональных и коммерческих целей без каких-либо роялти и скрытых платежей.

2. КОНФИДЕНЦИАЛЬНОСТЬ, ПРИВАТНОСТЬ И БЕЗОПАСНОСТЬ ДАННЫХ (LOCAL-FIRST)
2.1. Разработчик придерживается политики абсолютной защиты конфиденциальности пользователей (Privacy-by-Design):
    • Все алгоритмы синтеза речи (локальный движок Piper TTS) и локального распознавания (Whisper) выполняются непосредственно на оборудовании пользователя без отправки текстовых данных на внешние серверы.
    • Программа НЕ осуществляет сбор, передачу или хранение содержимого диалогов с нейросетями, буфера обмена, озвучиваемых файлов, персональных документов или голосовых записей.
    • Программа использует сетевые подключения исключительно для:
        а) загрузки опциональных облачных голосов (Microsoft Edge TTS) по прямому выбору пользователя;
        б) автоматической проверки наличия обновлений программы;
        в) передачи обезличенных статистических агрегатов (номер версии, операционная система, суммарное время работы) без идентификации личности.

3. ИНТЕГРАЦИИ СО СТОРОННИМИ ПЛАТФОРМАМИ И СЕРВИСАМИ ИИ
3.1. VoicePlayer предоставляет возможности автоматического сопряжения с внешними инструментами разработки и искусственного интеллекта (Antigravity, Codex, Claude Code, Cursor и др.).
3.2. Разработчик VoicePlayer не несёт ответственности за политику конфиденциальности, работоспособность, тарификацию или изменения в API сторонних сервисов и программных платформ.

4. ПРАВА НА ПОЛЬЗОВАТЕЛЬСКИЙ КОНТЕНТ
4.1. Все права на тексты, файлы, аудиозаписи и синтезированную речь, созданные или обработанные с помощью Программы, безоговорочно принадлежат Пользователю.
4.2. Разработчик не претендует на авторские или смежные права в отношении результатов работы Пользователя в Программе.

5. ОБНОВЛЕНИЯ ПРОГРАММНОГО ОБЕСПЕЧЕНИЯ
5.1. Программа включает механизм автоматического обновления для обеспечения стабильности, безопасности и расширения функционала.
5.2. Пользователь вправе отключить автоматические обновления в любое время через меню настроек Программы.

6. ДЕИНСТАЛЛЯЦИЯ И УДАЛЕНИЕ
6.1. Программа корректно регистрируется в операционной системе Windows и может быть полностью и без остатка удалена штатными средствами («Параметры Windows → Приложения → Установленные приложения» либо «Панель управления → Программы и компоненты»).

7. ОТКАЗ ОТ ГАРАНТИЙ И ОГРАНИЧЕНИЕ ОТВЕТСТВЕННОСТИ (MIT LICENSE)
7.1. ПРОГРАММА ПРЕДОСТАВЛЯЕТСЯ НА УСЛОВИЯХ «КАК ЕСТЬ» («AS IS»), БЕЗ КАКИХ-ЛИБО ЯВНЫХ ИЛИ ПОДРАЗУМЕВАЕМЫХ ГАРАНТИЙ, ВКЛЮЧАЯ, НО НЕ ОГРАНИЧИВАЯСЬ, ГАРАНТИЯМИ ТОВАРНОЙ ПРИГОДНОСТИ, СООТВЕТСТВИЯ ОПРЕДЕЛЕННЫМ ЦЕЛЯМ И ОТСУТСТВИЯ НАРУШЕНИЙ.
7.2. НИ ПРИ КАКИХ ОБСТОЯТЕЛЬСТВАХ АВТОРЫ ИЛИ ПРАВООБЛАДАТЕЛИ НЕ НЕСУТ ОТВЕТСТВЕННОСТИ ПО ИСКАМ О ВОЗМЕЩЕНИИ УЩЕРБА, УБЫТКОВ ИЛИ ИНЫХ ТРЕБОВАНИЙ, ВОЗНИКШИХ В СВЯЗИ С ИСПОЛЬЗОВАНИЕМ ПРОГРАММЫ.

----------------------------------------------------------------------
OFFICIAL MIT LICENSE TEXT:

Copyright (c) 2026 Anton Sorokin and VoicePlayer Contributors.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""


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
    root_zips = sorted(Path(__file__).resolve().parents[1].glob("voice-player-*.zip"))
    if root_zips:
        return root_zips[-1]
    # Резервная загрузка архива релиза с GitHub, если exe запущен автономно
    try:
        import tempfile, urllib.request
        target_zip = Path(tempfile.gettempdir()) / f"voice-player-{APP_VERSION_DEFAULT}.zip"
        if not target_zip.is_file() or target_zip.stat().st_size < 10000:
            url = f"https://github.com/infostroper-cloud/voice-player/releases/download/voice-player-v{APP_VERSION_DEFAULT}/voice-player-{APP_VERSION_DEFAULT}.zip"
            urllib.request.urlretrieve(url, str(target_zip))
        if target_zip.is_file() and target_zip.stat().st_size > 10000:
            return target_zip
    except Exception:
        pass
    raise FileNotFoundError("Внутри установщика не найден архив voice-player-*.zip")


def _find_system_python() -> str | None:
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


def _get_free_space_gb(path_str: str) -> float:
    try:
        p = Path(path_str)
        while not p.exists() and p != p.parent:
            p = p.parent
        usage = shutil.disk_usage(str(p))
        return usage.free / (1024 ** 3)
    except Exception:
        return 0.0


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


def run_install(
    dest_dir: Path,
    desktop_shortcut: bool,
    start_menu_shortcut: bool,
    auto_start: bool,
    status_cb,
    start_app: bool = False
) -> str:
    zp = _bundled_zip()
    py = _ensure_python(status_cb)
    dest_dir.mkdir(parents=True, exist_ok=True)

    status_cb(f"Распаковываю VoicePlayer в {dest_dir.name}...")
    with tempfile.TemporaryDirectory() as td:
        with zipfile.ZipFile(zp) as z:
            z.extract("vp_updater.py", td)
        updater_py = Path(td) / "vp_updater.py"

        cmd = [py, str(updater_py), "--install", str(zp), "--dest", str(dest_dir)]
        if not desktop_shortcut:
            cmd.append("--no-desktop")
        if not start_menu_shortcut:
            cmd.append("--no-startmenu")
        if auto_start:
            cmd.append("--autostart")

        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900, creationflags=NO_WINDOW)
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "ошибка установки").strip().splitlines()[-1]
            raise RuntimeError(err[:240])

    launch_cmd = dest_dir / "launch.cmd"
    launch_pyw = dest_dir / "launch.pyw"
    pyw = str(Path(py).with_name("pythonw.exe")) if Path(py).with_name("pythonw.exe").is_file() else py

    if start_app:
        status_cb("Запускаю VoicePlayer...")
        if launch_cmd.is_file():
            subprocess.Popen([str(launch_cmd)], creationflags=NO_WINDOW)
        elif launch_pyw.is_file():
            subprocess.Popen([pyw, str(launch_pyw)], creationflags=NO_WINDOW)

    return zp.stem.replace("voice-player-", "")


class ClassicInstallerWizard:
    """Классический визард установщика Windows."""

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Установка VoicePlayer")
        # Увеличенный комфортный размер для правильного отображения на любых экранах и масштабах
        self.root.geometry("620x510")
        self.root.minsize(580, 480)
        self.root.resizable(True, True)
        self.root.configure(bg="#131615")

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass

        if sys.platform == "win32":
            try:
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("VoicePlayer.Setup.Installer")
            except Exception:
                pass

        ico_candidates = [
            _res_dir() / "icon.ico",
            Path(getattr(sys, "_MEIPASS", "")) / "icon.ico",
            Path(__file__).resolve().parent / "icon.ico",
            Path(__file__).resolve().parents[1] / "icon.ico",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "VoicePlayer" / "icon.ico",
            Path.home() / ".voice-player" / "icon.ico",
        ]
        self.installer_ico = next((c for c in ico_candidates if c and c.is_file()), None)
        if self.installer_ico:
            try:
                self.root.iconbitmap(default=str(self.installer_ico))
            except Exception:
                try:
                    self.root.iconbitmap(str(self.installer_ico))
                except Exception:
                    pass
            try:
                from PIL import Image, ImageTk
                img = Image.open(str(self.installer_ico))
                self._icon_img = ImageTk.PhotoImage(img.resize((32, 32)))
                self.root.iconphoto(True, self._icon_img)
            except Exception:
                pass

        _apply_dark_titlebar(self.root)
        self.root.after(30, self._apply_taskbar_icon)
        self.root.after(120, self._apply_taskbar_icon)
        self.root.bind("<Map>", lambda e: self._apply_taskbar_icon())

        # Центрирование
        self.root.update_idletasks()
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.root.geometry(f"620x510+{(sw - 620) // 2}+{(sh - 510) // 2}")

        self.current_page = 0
        default_folder = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Programs" / "VoicePlayer"
        self.var_dest = tk.StringVar(value=str(default_folder))
        self.var_license = tk.StringVar(value="disagree")
        self.var_desktop = tk.BooleanVar(value=True)
        self.var_startmenu = tk.BooleanVar(value=True)
        self.var_autostart = tk.BooleanVar(value=False)
        self.var_launch_now = tk.BooleanVar(value=True)

        self._installed_ver = APP_VERSION_DEFAULT
        try:
            zp = _bundled_zip()
            self._installed_ver = zp.stem.replace("voice-player-", "")
        except Exception:
            pass

        self._build_layout()
        self._show_page(0)

    def _apply_taskbar_icon(self):
        """Гарантированно установить иконку программы на панели задач Windows (WM_SETICON и GCLP_HICON)."""
        if sys.platform != "win32" or not getattr(self, "installer_ico", None):
            return
        try:
            import ctypes
            user32 = ctypes.windll.user32
            wid = self.root.winfo_id()
            hwnd = user32.GetAncestor(wid, 2) or wid
            IMAGE_ICON = 1
            LR_LOADFROMFILE = 0x0010
            hicon_big = user32.LoadImageW(None, str(self.installer_ico), IMAGE_ICON, 32, 32, LR_LOADFROMFILE)
            hicon_small = user32.LoadImageW(None, str(self.installer_ico), IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
            WM_SETICON = 0x0080
            if hicon_big:
                user32.SendMessageW(hwnd, WM_SETICON, 1, hicon_big)
                try:
                    set_class = getattr(user32, "SetClassLongPtrW", getattr(user32, "SetClassLongW", None))
                    if set_class:
                        set_class(hwnd, -14, hicon_big)  # GCLP_HICON
                except Exception:
                    pass
            if hicon_small:
                user32.SendMessageW(hwnd, WM_SETICON, 0, hicon_small)
                try:
                    set_class = getattr(user32, "SetClassLongPtrW", getattr(user32, "SetClassLongW", None))
                    if set_class:
                        set_class(hwnd, -34, hicon_small)  # GCLP_HICONSM
                except Exception:
                    pass
        except Exception:
            pass

    def _build_layout(self):
        # 1. Верхняя плашка шапки (Header Banner)
        self.header_frame = tk.Frame(self.root, bg="#181d1b", height=68, highlightthickness=0)
        self.header_frame.pack(side="top", fill="x")
        self.header_frame.pack_propagate(False)

        header_inner = tk.Frame(self.header_frame, bg="#181d1b", padx=20, pady=10)
        header_inner.pack(fill="both", expand=True)

        header_text_box = tk.Frame(header_inner, bg="#181d1b")
        header_text_box.pack(side="left", fill="both", expand=True)

        self.lbl_head_title = tk.Label(header_text_box, text="Лицензионное соглашение", bg="#181d1b",
                                       fg="#e8ecea", font=("Segoe UI Semibold", 11), anchor="w")
        self.lbl_head_title.pack(fill="x")

        self.lbl_head_sub = tk.Label(header_text_box, text="Пожалуйста, ознакомьтесь с условиями перед продолжением.",
                                     bg="#181d1b", fg="#7c8782", font=("Segoe UI", 9), anchor="w")
        self.lbl_head_sub.pack(fill="x", pady=(2, 0))

        lbl_badge = tk.Label(header_inner, text="🔊 VoicePlayer", bg="#222b27", fg="#7ee2b8",
                             font=("Segoe UI Semibold", 9), padx=8, pady=4)
        lbl_badge.pack(side="right")

        tk.Frame(self.root, bg="#262e2b", height=1).pack(side="top", fill="x")

        # 2. Нижняя панель навигации (Bottom Bar)
        tk.Frame(self.root, bg="#262e2b", height=1).pack(side="bottom", fill="x")
        self.nav_frame = tk.Frame(self.root, bg="#131615", padx=20, pady=14)
        self.nav_frame.pack(side="bottom", fill="x")

        self.lbl_ver_badge = tk.Label(self.nav_frame, text=f"v{self._installed_ver} · Open Source", bg="#131615",
                                      fg="#6b7571", font=("Segoe UI", 9))
        self.lbl_ver_badge.pack(side="left")

        self.btn_cancel = tk.Button(
            self.nav_frame, text="Отмена", command=self._confirm_cancel,
            bg="#1c211f", fg="#e8ecea", activebackground="#2a322f", activeforeground="#e8ecea",
            font=("Segoe UI", 9), bd=0, padx=16, pady=7, cursor="hand2"
        )
        self.btn_cancel.pack(side="right", padx=(8, 0))

        self.btn_next = tk.Button(
            self.nav_frame, text="Далее >", command=self._go_next,
            bg="#202624", fg="#59635f", activebackground="#6ad6a9", activeforeground="#0d0f0e",
            font=("Segoe UI Semibold", 9), bd=0, padx=20, pady=7, cursor="hand2"
        )
        self.btn_next.pack(side="right", padx=(6, 0))

        self.btn_back = tk.Button(
            self.nav_frame, text="< Назад", command=self._go_back,
            bg="#1c211f", fg="#e8ecea", activebackground="#2a322f", activeforeground="#e8ecea",
            font=("Segoe UI", 9), bd=0, padx=16, pady=7, cursor="hand2"
        )
        self.btn_back.pack(side="right")

        # 3. Основная область страниц (Body Container)
        self.body_container = tk.Frame(self.root, bg="#131615", padx=20, pady=12)
        self.body_container.pack(side="top", fill="both", expand=True)

        self._build_page_license()
        self._build_page_dest()
        self._build_page_tasks()
        self._build_page_install()
        self._build_page_finish()

    # ------------------- Страница 0: Лицензионное соглашение -------------------
    def _build_page_license(self):
        self.page_license = tk.Frame(self.body_container, bg="#131615")

        tk.Label(self.page_license, text="Для продолжения установки необходимо ознакомиться и принять лицензионное соглашение:",
                 bg="#131615", fg="#d1d8d5", font=("Segoe UI", 9), anchor="w").pack(side="top", fill="x", pady=(0, 6))

        # ВАЖНО: Блок выбора согласия упаковываем к НИЗУ страницы FIRST, чтобы он гарантированно не вытеснялся текстом
        self.agree_box = tk.Frame(
            self.page_license, bg="#181d1b", highlightthickness=1,
            highlightbackground="#2a322f", padx=14, pady=8
        )
        self.agree_box.pack(side="bottom", fill="x", pady=(8, 0))

        self.r1 = tk.Radiobutton(
            self.agree_box, text="Я принимаю условия соглашения", variable=self.var_license, value="agree",
            command=self._on_license_toggle, bg="#181d1b", fg="#e8ecea", selectcolor="#1c211f",
            activebackground="#181d1b", activeforeground="#7ee2b8", font=("Segoe UI Semibold", 9), cursor="hand2"
        )
        self.r1.pack(anchor="w", pady=(0, 2))

        self.r2 = tk.Radiobutton(
            self.agree_box, text="Я не принимаю условия соглашения", variable=self.var_license, value="disagree",
            command=self._on_license_toggle, bg="#181d1b", fg="#7c8782", selectcolor="#1c211f",
            activebackground="#181d1b", activeforeground="#7c8782", font=("Segoe UI", 9), cursor="hand2"
        )
        self.r2.pack(anchor="w")

        # Текстовое поле с прокруткой (занимает всё оставшееся пространство сверху)
        text_wrap = tk.Frame(self.page_license, bg="#1c211f", highlightthickness=1, highlightbackground="#262e2b")
        text_wrap.pack(side="top", fill="both", expand=True)

        sb = ttk.Scrollbar(text_wrap, orient="vertical")
        sb.pack(side="right", fill="y")

        self.txt_license = tk.Text(
            text_wrap, bg="#1c211f", fg="#d1d8d5", insertbackground="#d1d8d5",
            font=("Segoe UI", 8), wrap="word", bd=0, padx=12, pady=10, height=11, yscrollcommand=sb.set
        )
        self.txt_license.pack(side="left", fill="both", expand=True)
        sb.configure(command=self.txt_license.yview)

        self.txt_license.insert("1.0", LICENSE_TEXT)
        self.txt_license.configure(state="disabled")

    def _on_license_toggle(self):
        if self.var_license.get() == "agree":
            self.agree_box.configure(highlightbackground="#7ee2b8")
            self.r1.configure(fg="#7ee2b8")
            self.btn_next.configure(state="normal", bg="#7ee2b8", fg="#0d0f0e")
        else:
            self.agree_box.configure(highlightbackground="#2a322f")
            self.r1.configure(fg="#e8ecea")
            self.btn_next.configure(state="disabled", bg="#202624", fg="#59635f")

    # ------------------- Страница 1: Выбор папки установки -------------------
    def _build_page_dest(self):
        self.page_dest = tk.Frame(self.body_container, bg="#131615")

        tk.Label(self.page_dest, text="Программа установит VoicePlayer в следующую папку.",
                 bg="#131615", fg="#e8ecea", font=("Segoe UI", 9), anchor="w").pack(fill="x")
        tk.Label(self.page_dest, text="Чтобы выбрать другую папку, нажмите «Обзор...»:",
                 bg="#131615", fg="#7c8782", font=("Segoe UI", 9), anchor="w").pack(fill="x", pady=(2, 12))

        row_dir = tk.Frame(self.page_dest, bg="#131615")
        row_dir.pack(fill="x", pady=(0, 16))

        self.ent_dest = ttk.Entry(row_dir, textvariable=self.var_dest, font=("Segoe UI", 9))
        self.ent_dest.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.var_dest.trace_add("write", lambda *_: self._update_free_space())

        btn_browse = tk.Button(
            row_dir, text="Обзор...", command=self._browse_folder,
            bg="#1c211f", fg="#e8ecea", activebackground="#262e2b", activeforeground="#e8ecea",
            font=("Segoe UI", 9), bd=0, padx=14, pady=5, cursor="hand2"
        )
        btn_browse.pack(side="right")

        info_card = tk.Frame(self.page_dest, bg="#181d1b", highlightthickness=1, highlightbackground="#262e2b",
                             padx=16, pady=14)
        info_card.pack(fill="x", pady=(4, 0))

        tk.Label(info_card, text="Требуется свободного места на диске: ~45 МБ",
                 bg="#181d1b", fg="#7c8782", font=("Segoe UI", 9), anchor="w").pack(fill="x")

        self.lbl_free_space = tk.Label(info_card, text="Доступно на диске: расчет...",
                                       bg="#181d1b", fg="#7ee2b8", font=("Segoe UI Semibold", 9), anchor="w")
        self.lbl_free_space.pack(fill="x", pady=(4, 0))

    def _browse_folder(self):
        init_dir = self.var_dest.get()
        if not Path(init_dir).exists():
            init_dir = str(Path.home())
        chosen = filedialog.askdirectory(initialdir=init_dir, title="Выберите папку для установки VoicePlayer")
        if chosen:
            self.var_dest.set(str(Path(chosen) / "VoicePlayer" if not chosen.lower().endswith("voiceplayer") else chosen))

    def _update_free_space(self):
        path_str = self.var_dest.get().strip()
        if path_str:
            free_gb = _get_free_space_gb(path_str)
            self.lbl_free_space.configure(text=f"Доступно на диске: {free_gb:.1f} ГБ")

    # ------------------- Страница 2: Дополнительные задачи -------------------
    def _build_page_tasks(self):
        self.page_tasks = tk.Frame(self.body_container, bg="#131615")

        tk.Label(self.page_tasks, text="Выберите дополнительные ярлыки и параметры запуска программы:",
                 bg="#131615", fg="#e8ecea", font=("Segoe UI", 9), anchor="w").pack(fill="x", pady=(0, 10))

        card = tk.Frame(self.page_tasks, bg="#181d1b", highlightthickness=1, highlightbackground="#262e2b",
                        padx=16, pady=14)
        card.pack(fill="x", pady=(0, 12))

        tk.Label(card, text="Ярлыки приложения:", bg="#181d1b", fg="#7ee2b8",
                 font=("Segoe UI Semibold", 9), anchor="w").pack(fill="x", pady=(0, 6))

        cb_desk = tk.Checkbutton(
            card, text="Создать значок на Рабочем столе", variable=self.var_desktop,
            bg="#181d1b", fg="#e8ecea", selectcolor="#1c211f", activebackground="#181d1b",
            activeforeground="#7ee2b8", font=("Segoe UI", 9), cursor="hand2"
        )
        cb_desk.pack(anchor="w", pady=(0, 4))

        cb_start = tk.Checkbutton(
            card, text="Создать ярлык в меню «Пуск»", variable=self.var_startmenu,
            bg="#181d1b", fg="#e8ecea", selectcolor="#1c211f", activebackground="#181d1b",
            activeforeground="#7ee2b8", font=("Segoe UI", 9), cursor="hand2"
        )
        cb_start.pack(anchor="w")

        card2 = tk.Frame(self.page_tasks, bg="#181d1b", highlightthickness=1, highlightbackground="#262e2b",
                         padx=16, pady=14)
        card2.pack(fill="x")

        tk.Label(card2, text="Параметры запуска программы:", bg="#181d1b", fg="#7ee2b8",
                 font=("Segoe UI Semibold", 9), anchor="w").pack(fill="x", pady=(0, 6))

        cb_launch = tk.Checkbutton(
            card2, text="Запустить VoicePlayer сразу после завершения установки", variable=self.var_launch_now,
            bg="#181d1b", fg="#e8ecea", selectcolor="#1c211f", activebackground="#181d1b",
            activeforeground="#7ee2b8", font=("Segoe UI", 9), cursor="hand2"
        )
        cb_launch.pack(anchor="w", pady=(0, 4))

        cb_auto = tk.Checkbutton(
            card2, text="Запускать VoicePlayer при входе в Windows (автозапуск)", variable=self.var_autostart,
            bg="#181d1b", fg="#e8ecea", selectcolor="#1c211f", activebackground="#181d1b",
            activeforeground="#7ee2b8", font=("Segoe UI", 9), cursor="hand2"
        )
        cb_auto.pack(anchor="w")

    # ------------------- Страница 3: Процесс установки -------------------
    def _build_page_install(self):
        self.page_install = tk.Frame(self.body_container, bg="#131615")

        self.lbl_install_step = tk.Label(self.page_install, text="Подготовка к установке...",
                                         bg="#131615", fg="#e8ecea", font=("Segoe UI Semibold", 10), anchor="w")
        self.lbl_install_step.pack(fill="x", pady=(20, 8))

        self.install_bar = ttk.Progressbar(self.page_install, mode="indeterminate", length=540)
        self.install_bar.pack(fill="x", pady=(0, 14))

        self.lbl_install_detail = tk.Label(
            self.page_install, text="Пожалуйста, подождите, пока мастер завершит установку файлов программы.",
            bg="#131615", fg="#7c8782", font=("Segoe UI", 9), justify="left", anchor="w"
        )
        self.lbl_install_detail.pack(fill="x")

    # ------------------- Страница 4: Завершение мастера -------------------
    def _build_page_finish(self):
        self.page_finish = tk.Frame(self.body_container, bg="#131615")

        tk.Label(self.page_finish, text="✓ Установка успешно завершена!",
                 bg="#131615", fg="#7ee2b8", font=("Segoe UI Semibold", 14), anchor="w").pack(fill="x", pady=(12, 6))

        self.lbl_finish_desc = tk.Label(
            self.page_finish, text="VoicePlayer готов к работе.\nВсе необходимые библиотеки и компоненты синтеза речи настроены.",
            bg="#131615", fg="#cfd5d2", font=("Segoe UI", 9), justify="left", anchor="w"
        )
        self.lbl_finish_desc.pack(fill="x", pady=(0, 16))

        card = tk.Frame(self.page_finish, bg="#181d1b", highlightthickness=1, highlightbackground="#262e2b",
                        padx=16, pady=12)
        card.pack(fill="x", pady=(0, 16))

        self.lbl_finish_path = tk.Label(card, text="Папка программы: ...", bg="#181d1b", fg="#7c8782",
                                        font=("Segoe UI", 9), anchor="w")
        self.lbl_finish_path.pack(fill="x")

        cb_launch = tk.Checkbutton(
            self.page_finish, text="Запустить VoicePlayer сейчас", variable=self.var_launch_now,
            bg="#131615", fg="#e8ecea", selectcolor="#1c211f", activebackground="#131615",
            activeforeground="#7ee2b8", font=("Segoe UI Semibold", 10), cursor="hand2"
        )
        cb_launch.pack(anchor="w")

    # ------------------- Управление страницами -------------------
    def _show_page(self, index: int):
        self.current_page = index
        for p in (self.page_license, self.page_dest, self.page_tasks, self.page_install, self.page_finish):
            p.pack_forget()

        if index == 0:
            self.lbl_head_title.configure(text="Лицензионное соглашение")
            self.lbl_head_sub.configure(text="Пожалуйста, ознакомьтесь с условиями соглашения перед продолжением.")
            self.page_license.pack(fill="both", expand=True)
            self.btn_back.configure(state="disabled")
            self.btn_next.configure(text="Далее >")
            self._on_license_toggle()
            self.btn_cancel.configure(state="normal")

        elif index == 1:
            self.lbl_head_title.configure(text="Выбор папки установки")
            self.lbl_head_sub.configure(text="В какую папку вы хотите установить VoicePlayer?")
            self.page_dest.pack(fill="both", expand=True)
            self.btn_back.configure(state="normal")
            self.btn_next.configure(state="normal", text="Далее >", bg="#7ee2b8", fg="#0d0f0e")
            self._update_free_space()
            self.btn_cancel.configure(state="normal")

        elif index == 2:
            self.lbl_head_title.configure(text="Выберите дополнительные задачи")
            self.lbl_head_sub.configure(text="Какие ярлыки и параметры запуска должен настроить установщик?")
            self.page_tasks.pack(fill="both", expand=True)
            self.btn_back.configure(state="normal")
            self.btn_next.configure(state="normal", text="Установить", bg="#7ee2b8", fg="#0d0f0e")
            self.btn_cancel.configure(state="normal")

        elif index == 3:
            self.lbl_head_title.configure(text="Установка VoicePlayer")
            self.lbl_head_sub.configure(text="Пожалуйста, подождите, пока идет процесс установки...")
            self.page_install.pack(fill="both", expand=True)
            self.btn_back.configure(state="disabled")
            self.btn_next.configure(state="disabled")
            self.btn_cancel.configure(state="disabled")
            self._start_installation()

        elif index == 4:
            self.lbl_head_title.configure(text="Завершение мастера установки")
            self.lbl_head_sub.configure(text="VoicePlayer успешно установлен на ваш компьютер.")
            self.lbl_finish_path.configure(text=f"Папка программы: {self.var_dest.get()}")
            self.page_finish.pack(fill="both", expand=True)
            self.btn_back.pack_forget()
            self.btn_cancel.pack_forget()
            self.btn_next.configure(state="normal", text="Завершить", bg="#7ee2b8", fg="#0d0f0e")

    def _go_next(self):
        if self.current_page == 0:
            if self.var_license.get() != "agree":
                return
            self._show_page(1)
        elif self.current_page == 1:
            dest = self.var_dest.get().strip()
            if not dest:
                messagebox.showwarning("Папка установки", "Пожалуйста, укажите папку для установки VoicePlayer.")
                return
            try:
                p = Path(dest)
                p.mkdir(parents=True, exist_ok=True)
                test_file = p / ".test_write.tmp"
                test_file.write_text("ok", encoding="ascii")
                test_file.unlink(missing_ok=True)
            except Exception as e:
                messagebox.showerror(
                    "Ошибка доступа",
                    f"Не удалось создать файлы в выбранной папке:\n{e}\n\n"
                    "Пожалуйста, выберите другую папку (например, в вашей пользовательской директории)."
                )
                return
            self._show_page(2)
        elif self.current_page == 2:
            self._show_page(3)
        elif self.current_page == 4:
            self._finish_and_launch()

    def _go_back(self):
        if self.current_page in (1, 2):
            self._show_page(self.current_page - 1)

    def _confirm_cancel(self):
        if messagebox.askyesno("Выход из установки", "Прервать установку VoicePlayer?"):
            self.root.destroy()

    def _start_installation(self):
        self.install_bar.start(12)
        dest = Path(self.var_dest.get().strip())
        desktop = self.var_desktop.get()
        startmenu = self.var_startmenu.get()
        autostart = self.var_autostart.get()

        def set_status(msg: str):
            self.root.after(0, lambda: self.lbl_install_step.configure(text=msg))

        def worker():
            try:
                ver = run_install(
                    dest_dir=dest,
                    desktop_shortcut=desktop,
                    start_menu_shortcut=startmenu,
                    auto_start=autostart,
                    status_cb=set_status,
                    start_app=False
                )
                self._installed_ver = ver
                self.root.after(0, self._on_install_done)
            except Exception as e:
                self.root.after(0, lambda err=str(e): self._on_install_error(err))

        threading.Thread(target=worker, daemon=True).start()

    def _on_install_done(self):
        self.install_bar.stop()
        self._show_page(4)

    def _on_install_error(self, err: str):
        self.install_bar.stop()
        self.lbl_install_step.configure(text="⚠ Ошибка при установке", fg="#ff6b6b")
        self.lbl_install_detail.configure(text=f"Произошла ошибка:\n{err}")
        self.btn_back.configure(state="normal")
        self.btn_cancel.configure(state="normal")
        messagebox.showerror("Ошибка установки VoicePlayer", f"Не удалось завершить установку:\n{err}", parent=self.root)

    def _finish_and_launch(self):
        if self.var_launch_now.get():
            dest = Path(self.var_dest.get().strip())
            launch_pyw = dest / "launch.pyw"
            launch_cmd = dest / "launch.cmd"
            try:
                py = _find_system_python() or sys.executable
                pyw = str(Path(py).with_name("pythonw.exe")) if Path(py).with_name("pythonw.exe").is_file() else py
                if launch_pyw.is_file():
                    subprocess.Popen([pyw, str(launch_pyw)], cwd=str(dest),
                                     creationflags=0x00000008 | 0x00000200)
                elif launch_cmd.is_file():
                    try:
                        os.startfile(str(launch_cmd))
                    except Exception:
                        subprocess.Popen(["cmd.exe", "/c", str(launch_cmd)], cwd=str(dest),
                                         creationflags=0x00000008 | 0x00000200)
            except Exception:
                pass
        self.root.destroy()


def main():
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("VoicePlayer.Setup.Installer")
        except Exception:
            pass
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            try: ctypes.windll.user32.SetProcessDPIAware()
            except Exception: pass
    args_upper = [a.upper() for a in sys.argv[1:]]
    if "/SILENT" in args_upper or "/VERYSILENT" in args_upper:
        default_folder = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Programs" / "VoicePlayer"
        for a in sys.argv[1:]:
            if a.upper().startswith("/DIR="):
                default_folder = Path(a[5:].strip('"'))
        run_install(
            dest_dir=default_folder,
            desktop_shortcut=True,
            start_menu_shortcut=True,
            auto_start=False,
            status_cb=lambda _m: None,
            start_app=True
        )
        return

    root = tk.Tk()
    ClassicInstallerWizard(root)
    root.mainloop()


if __name__ == "__main__":
    main()
