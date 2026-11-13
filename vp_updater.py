"""Автообновление плеера из GitHub Releases. Только стандартная библиотека: модуль работает
до загрузки остального приложения и при первой установке.

Новая версия ставится рядом, в ~/.voice-player/versions/<версия>/. Рабочая копия кода
(git, OneDrive) не перезаписывается: при запуске любая копия передаёт управление самой новой
установленной версии, если та новее её самой. Версия, не дожившая до отметки «работает» за
MAX_ATTEMPTS запуска, считается сбойной и пропускается: это откат.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

REPO = "infostroper-cloud/pro"
PUBLIC_REPO = os.environ.get("VP_PUBLIC_REPO", "infostroper-cloud/voice-player")
TAG_PREFIX = "voice-player-v"
MAX_ATTEMPTS = 2
KEEP_VERSIONS = 3
PIP_TIMEOUT_S = 900
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def home_dir() -> Path:
    return Path.home() / ".voice-player"


def versions_dir() -> Path:
    return home_dir() / "versions"


def parse_version(text: str) -> tuple:
    m = re.fullmatch(r"\s*(\d+)\.(\d+)\.(\d+)\s*", text or "")
    return tuple(int(x) for x in m.groups()) if m else ()


def read_version(app_dir: Path) -> str:
    try:
        return (Path(app_dir) / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "0.0.0"


def console_python() -> str:
    """pythonw не имеет вывода: pip и самопроверку запускаем через python."""
    py = Path(sys.executable)
    if py.name.lower() == "pythonw.exe" and py.with_name("python.exe").exists():
        return str(py.with_name("python.exe"))
    return str(py)


def gui_python() -> str:
    py = Path(sys.executable)
    if py.name.lower() == "python.exe" and py.with_name("pythonw.exe").exists():
        return str(py.with_name("pythonw.exe"))
    return str(py)


# ---------------------------- выбор версии и откат ----------------------------

def is_usable(d: Path) -> bool:
    # неполная папка (например, туда скопировали только app.py) не должна перехватывать запуск
    if not (d / "app.py").exists() or not (d / "VERSION").exists() or not parse_version(d.name):
        return False
    if (d / ".healthy").exists():
        return True
    return _attempts(d) < MAX_ATTEMPTS


def _attempts(d: Path) -> int:
    try:
        return int((d / ".attempts").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0


def best_installed(newer_than: tuple = ()) -> Path | None:
    """Самая новая исправная установленная версия новее newer_than."""
    best, best_v = None, newer_than
    if versions_dir().is_dir():
        for d in versions_dir().iterdir():
            v = parse_version(d.name)
            if v and v > best_v and is_usable(d):
                best, best_v = d, v
    return best


def _note_start(app_dir: Path) -> None:
    if app_dir.parent == versions_dir() and not (app_dir / ".healthy").exists():
        (app_dir / ".attempts").write_text(str(_attempts(app_dir) + 1), encoding="utf-8")


def mark_healthy(app_dir: Path) -> None:
    """Вызывается, когда окно поработало: эту версию больше не откатываем."""
    if Path(app_dir).parent == versions_dir():
        try:
            (Path(app_dir) / ".healthy").write_text("ok", encoding="utf-8")
        except OSError:
            pass


def _wait_for_exit(pid: int, timeout_s: float = 15.0) -> None:
    """Перезапуск: новая копия ждёт, пока старая отпустит порт и единственный экземпляр."""
    end = time.time() + timeout_s
    if os.name == "nt":
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x100000, False, pid)              # SYNCHRONIZE
        if h:
            k.WaitForSingleObject(h, int(timeout_s * 1000))
            k.CloseHandle(h)
        return
    while time.time() < end:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.2)


def bootstrap(app_dir: Path, argv: list = None) -> None:
    """В самом начале запуска: при необходимости передать управление более новой версии."""
    argv = sys.argv if argv is None else argv
    if getattr(sys, "frozen", False) or os.environ.get("VP_NO_UPDATE") or "--self-test" in argv or "--feed" in argv:
        return
    wait_pid = os.environ.pop("VP_RESTART_WAIT", "")
    if wait_pid.isdigit():
        _wait_for_exit(int(wait_pid))
    try:
        target = best_installed(parse_version(read_version(app_dir)))
        if target is None:
            return
        _note_start(target)        # считает запускающая сторона: сломанная версия сама уже ничего не запишет
        env = dict(os.environ)
        env.setdefault("VP_ORIGIN_DIR", str(app_dir))
        subprocess.Popen([gui_python(), str(target / "app.py")] + list(argv[1:]), env=env, cwd=str(target),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=True, creationflags=NO_WINDOW)
    except Exception:
        return                     # что-то не так с установленными версиями: работаем из своей папки
    os._exit(0)


def restart(app_dir: Path, args: list = ()) -> None:
    """Запустить новую копию; она дождётся выхода этой. Выйти должен вызывающий."""
    env = dict(os.environ, VP_RESTART_WAIT=str(os.getpid()))
    if getattr(sys, "frozen", False):
        upd_dir = home_dir() / "updates"
        installers = sorted(upd_dir.glob("VoicePlayer-Setup-*.exe")) if upd_dir.is_dir() else []
        if installers:
            subprocess.Popen([str(installers[-1]), "/SILENT", "/CLOSEAPPLICATIONS"], env=env,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             close_fds=True, creationflags=NO_WINDOW)
            return
        subprocess.Popen([sys.executable] + list(args), env=env, cwd=str(app_dir),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=True, creationflags=NO_WINDOW)
        return
    target = best_installed(parse_version(read_version(app_dir))) or Path(app_dir)
    _note_start(target)
    subprocess.Popen([gui_python(), str(target / "app.py")] + list(args), env=env, cwd=str(target),
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     close_fds=True, creationflags=NO_WINDOW)


# ---------------------------- GitHub ----------------------------

def _token() -> str:
    """Доступ к закрытому репозиторию: тот же, что уже есть на компьютере у gh или git."""
    env_token = os.environ.get("VP_UPDATE_TOKEN")
    if env_token:
        return env_token
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
    try:
        r = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=15,
                           env=env, creationflags=NO_WINDOW)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        r = subprocess.run(["git", "credential", "fill"], input="protocol=https\nhost=github.com\n\n",
                           capture_output=True, text=True, timeout=20, env=env, creationflags=NO_WINDOW)
        m = re.search(r"^password=(.+)$", r.stdout, re.M) if r.returncode == 0 else None
        if m:
            return m.group(1).strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _get(url: str, token: str, accept: str, timeout: int = 30) -> bytes:
    """Ключ уходит только на api.github.com: файл отдаётся с другого адреса по перенаправлению.
    Для публичного репозитория релизов работает и без токена (token='')."""
    headers = {"Accept": accept, "User-Agent": "VoicePlayer-updater"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(urllib.request.Request(url, headers=headers), timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        location = e.headers.get("Location") if e.code in (301, 302, 303, 307, 308) else None
        if not location:
            raise
    req = urllib.request.Request(location, headers={"User-Agent": "VoicePlayer-updater"})
    with urllib.request.urlopen(req, timeout=max(timeout, 300)) as r:
        return r.read()


def latest_release(token: str = "", repo: str = "") -> dict | None:
    """В репозитории могут быть выпуски других проектов: берём только свои, по метке.
    Если токена нет (внешний пользователь), проверяем публичный репозиторий релизов."""
    target_repo = repo or (REPO if token else PUBLIC_REPO)
    try:
        raw = _get(f"https://api.github.com/repos/{target_repo}/releases?per_page=50", token, "application/vnd.github+json")
    except urllib.error.HTTPError as e:
        if token and not repo and PUBLIC_REPO and PUBLIC_REPO != REPO and e.code in (401, 403, 404):
            raw = _get(f"https://api.github.com/repos/{PUBLIC_REPO}/releases?per_page=50", "", "application/vnd.github+json")
        else:
            raise
    best, best_v = None, ()
    for rel in json.loads(raw.decode("utf-8")):
        tag = rel.get("tag_name") or ""
        if rel.get("draft") or rel.get("prerelease") or not tag.startswith(TAG_PREFIX):
            continue
        v = parse_version(tag[len(TAG_PREFIX):])
        if v and v > best_v:
            best, best_v = rel, v
    return best


# ---------------------------- установка версии ----------------------------

def _safe_extract(zip_path: Path, dest: Path) -> None:
    root = dest.resolve()
    with zipfile.ZipFile(zip_path) as z:
        for name in z.namelist():
            target = (dest / name).resolve()
            if target != root and root not in target.parents:
                raise RuntimeError(f"недопустимый путь в архиве: {name}")
        z.extractall(dest)


def _install_requirements(new_dir: Path, current_dir: Path | None) -> None:
    new_req = (new_dir / "requirements.txt").read_bytes()
    if current_dir is not None:
        try:
            if (Path(current_dir) / "requirements.txt").read_bytes() == new_req:
                return
        except OSError:
            pass
    r = subprocess.run([console_python(), "-m", "pip", "install", "--disable-pip-version-check",
                        "-r", str(new_dir / "requirements.txt")],
                       capture_output=True, text=True, timeout=PIP_TIMEOUT_S, creationflags=NO_WINDOW)
    if r.returncode != 0:
        raise RuntimeError("библиотеки не установились: " + (r.stderr.strip().splitlines() or ["?"])[-1][:200])


def _self_test(new_dir: Path) -> None:
    env = dict(os.environ, VP_NO_UPDATE="1")
    r = subprocess.run([console_python(), str(new_dir / "app.py"), "--self-test"], cwd=str(new_dir), env=env,
                       capture_output=True, text=True, timeout=120, creationflags=NO_WINDOW)
    if r.returncode != 0:
        raise RuntimeError("самопроверка не прошла: " + (r.stderr.strip().splitlines() or ["?"])[-1][:200])


def _prune(keep: tuple = ()) -> None:
    dirs = sorted((d for d in versions_dir().iterdir() if parse_version(d.name)),
                  key=lambda d: parse_version(d.name), reverse=True)
    for d in dirs[KEEP_VERSIONS:]:
        if d.name not in keep:
            shutil.rmtree(d, ignore_errors=True)


def install_zip(zip_path: Path, current_dir: Path | None = None) -> Path:
    """Распаковать, поставить библиотеки, проверить запуск, и только потом показать версию плееру."""
    versions_dir().mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".tmp-", dir=versions_dir()))
    try:
        _safe_extract(zip_path, tmp)
        version = read_version(tmp)
        if not parse_version(version):
            raise RuntimeError("в архиве нет номера версии")
        _install_requirements(tmp, current_dir)
        _self_test(tmp)
        final = versions_dir() / version
        if final.exists():
            shutil.rmtree(final)
        tmp.replace(final)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    install_launcher()
    if os.name == "nt":
        register_windows_uninstaller(version)
    keep = (Path(current_dir).name,) if current_dir is not None else ()
    _prune(keep)
    return final


def check_and_install(app_dir: Path, log=lambda _m: None) -> str:
    """Вернуть номер версии, готовой к запуску, или пустую строку. Ошибки сети не критичны."""
    own = parse_version(read_version(app_dir))
    token = _token()
    try:
        rel = latest_release(token)
    except urllib.error.HTTPError as e:
        if not token and e.code == 404:
            return ""
        raise
    if rel is None:
        return ""
    version = rel["tag_name"][len(TAG_PREFIX):]
    v = parse_version(version)
    if v <= own:
        return ""
    assets = {a["name"]: a["url"] for a in rel.get("assets") or []}
    if getattr(sys, "frozen", False):
        exe_name = f"VoicePlayer-Setup-{version}.exe"
        if exe_name in assets:
            upd_dir = home_dir() / "updates"
            upd_dir.mkdir(parents=True, exist_ok=True)
            dest_exe = upd_dir / exe_name
            if dest_exe.exists() and dest_exe.stat().st_size > 0:
                return version
            log(f"update: скачиваю установщик {version}")
            data = _get(assets[exe_name], token, "application/octet-stream")
            if exe_name + ".sha256" in assets:
                expected = _get(assets[exe_name + ".sha256"], token, "application/octet-stream").decode("ascii").split()[0]
                if hashlib.sha256(data).hexdigest() != expected.lower():
                    raise RuntimeError("контрольная сумма установщика не совпала")
            dest_exe.write_bytes(data)
            log(f"update: установщик {version} готов")
            return version
    final = versions_dir() / version
    if final.exists():
        return version if is_usable(final) else ""        # сбойную не ставим заново
    zip_name = f"voice-player-{version}.zip"
    if zip_name not in assets or zip_name + ".sha256" not in assets:
        raise RuntimeError(f"в выпуске {version} нет файлов обновления")
    log(f"update: скачиваю {version}")
    data = _get(assets[zip_name], token, "application/octet-stream")
    expected = _get(assets[zip_name + ".sha256"], token, "application/octet-stream").decode("ascii").split()[0]
    if hashlib.sha256(data).hexdigest() != expected.lower():
        raise RuntimeError("контрольная сумма архива не совпала")
    with tempfile.TemporaryDirectory() as td:
        zp = Path(td) / zip_name
        zp.write_bytes(data)
        install_zip(zp, Path(app_dir))
    log(f"update: версия {version} установлена")
    return version


# ---------------------------- постоянные пути ----------------------------

def stable_hook_home(app_dir: Path) -> Path:
    """Хуки нейросетей должны пережить обновление: их скрипт лежит вне папок версий."""
    target = home_dir() / "hooks"
    target.mkdir(parents=True, exist_ok=True)
    src_feed = Path(app_dir) / "hooks" / "vp_feed.py"
    if src_feed.exists():
        try:
            shutil.copy2(src_feed, target / "vp_feed.py")
        except OSError:
            pass
    return home_dir()


LAUNCHER = '''import pathlib, subprocess, sys
root = pathlib.Path.home() / ".voice-player" / "versions"
dirs = sorted((d for d in root.iterdir() if d.name[:1].isdigit() and (d / "vp_updater.py").exists()),
              key=lambda d: [int(x) for x in d.name.split(".")])
sys.path.insert(0, str(dirs[-1]))
import vp_updater
target = vp_updater.best_installed()
vp_updater._note_start(target)
subprocess.Popen([vp_updater.gui_python(), str(target / "app.py")], cwd=str(target))
'''


def install_launcher() -> Path:
    home_dir().mkdir(parents=True, exist_ok=True)
    py = home_dir() / "launch.pyw"
    py.write_text(LAUNCHER, encoding="utf-8")
    if os.name == "nt":
        (home_dir() / "launch.cmd").write_text(f'@start "" "{gui_python()}" "%~dp0launch.pyw"\r\n', encoding="ascii")
    else:
        sh = home_dir() / "launch.sh"
        sh.write_text(f'#!/usr/bin/env bash\nexec "{gui_python()}" "{py}" "$@"\n', encoding="utf-8")
        try:
            sh.chmod(0o755)
        except OSError:
            pass
    return py


UNINSTALL_CMD = """@echo off
setlocal
cd /d "%~dp0"
set "PYW="
if exist "%LOCALAPPDATA%\\Programs\\Python\\Python312\\pythonw.exe" set "PYW=%LOCALAPPDATA%\\Programs\\Python\\Python312\\pythonw.exe"
if not defined PYW (
    for /f "tokens=*" %%i in ('where pythonw.exe 2^>nul') do if not defined PYW set "PYW=%%i"
)
if not defined PYW (
    for /f "tokens=*" %%i in ('where python.exe 2^>nul') do if not defined PYW set "PYW=%%i"
)
if defined PYW if exist "%~dp0uninstall.pyw" (
    start "" "%PYW%" "%~dp0uninstall.pyw" %*
    exit /b 0
)
set "ARG="
if /i "%~1"=="/quiet" set "ARG=-Quiet"
if /i "%~1"=="/silent" set "ARG=-Quiet"
if /i "%~1"=="--quiet" set "ARG=-Quiet"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall.ps1" %ARG%
exit /b 0
"""

UNINSTALL_PS1 = """param([switch]$Quiet)

if (-not $Quiet) {
    Add-Type -AssemblyName System.Windows.Forms
    $res = [System.Windows.Forms.MessageBox]::Show(
        "Вы действительно хотите удалить VoicePlayer с вашего компьютера?",
        "Удаление VoicePlayer",
        [System.Windows.Forms.MessageBoxButtons]::YesNo,
        [System.Windows.Forms.MessageBoxIcon]::Question
    )
    if ($res -ne [System.Windows.Forms.DialogResult]::Yes) {
        exit 0
    }
}

# 1. Завершить запущенные процессы VoicePlayer
Get-Process | Where-Object { $_.MainWindowTitle -like "*VoicePlayer*" } | Stop-Process -Force -ErrorAction SilentlyContinue
Get-WmiObject Win32_Process | Where-Object { $_.CommandLine -like "*voice-player*" -and $_.ProcessId -ne $PID } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

# 2. Удалить ярлыки с Рабочего стола и меню Пуск
foreach ($d in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))) {
    $lnk = Join-Path $d "VoicePlayer.lnk"
    if (Test-Path $lnk) { Remove-Item $lnk -Force -ErrorAction SilentlyContinue }
}

# 3. Удалить запись из «Установленных приложений» Windows
Remove-Item -Path "HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\VoicePlayer" -Recurse -Force -ErrorAction SilentlyContinue

# 4. Удалить файлы
$vpHome = Join-Path $env:USERPROFILE ".voice-player"
Start-Process -FilePath "cmd.exe" -ArgumentList "/c timeout /t 1 /nobreak >nul & rd /s /q `"$vpHome`"" -WindowStyle Hidden

if (-not $Quiet) {
    [System.Windows.Forms.MessageBox]::Show(
        "VoicePlayer успешно удален с вашего компьютера.",
        "Удаление VoicePlayer",
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::Information
    )
}
"""

UNINSTALL_PYW = """import os
import shutil
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

def main():
    args = [a.lower() for a in sys.argv[1:]]
    quiet = any(a in args for a in ("/quiet", "/silent", "--quiet", "--silent"))
    home = Path.home() / ".voice-player"

    if not quiet:
        root = tk.Tk()
        root.title("Удаление VoicePlayer")
        root.geometry("480x250")
        root.resizable(False, False)
        root.configure(bg="#131615")

        ico = home / "icon.ico"
        if ico.is_file():
            try: root.iconbitmap(default=str(ico))
            except Exception: pass

        root.update_idletasks()
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry(f"480x250+{(sw - 480) // 2}+{(sh - 250) // 2}")

        f = tk.Frame(root, bg="#131615", padx=24, pady=22)
        f.pack(fill="both", expand=True)

        tk.Label(f, text="Удаление VoicePlayer", bg="#131615", fg="#e8ecea",
                 font=("Segoe UI Semibold", 13), anchor="w").pack(fill="x")
        tk.Label(f, text="Вы действительно хотите удалить VoicePlayer с вашего компьютера?",
                 bg="#131615", fg="#7c8782", font=("Segoe UI", 10), justify="left", anchor="w").pack(fill="x", pady=(8, 14))

        var_purge = tk.BooleanVar(value=True)
        cb = tk.Checkbutton(f, text="Удалить сохранённую историю, аудиозаписи и настройки",
                            variable=var_purge, bg="#131615", fg="#e8ecea", selectcolor="#1c211f",
                            activebackground="#131615", activeforeground="#e8ecea", font=("Segoe UI", 9))
        cb.pack(anchor="w", pady=(0, 20))

        btn_box = tk.Frame(f, bg="#131615")
        btn_box.pack(fill="x")

        def do_uninstall():
            btn_del.configure(state="disabled", text="Удаление...")
            root.update()
            perform_uninstall(purge_user_data=var_purge.get(), home=home)
            root.destroy()
            try:
                messagebox.showinfo("Удаление VoicePlayer", "VoicePlayer успешно удален с вашего компьютера.")
            except Exception:
                pass
            sys.exit(0)

        btn_del = tk.Button(btn_box, text="Удалить VoicePlayer", command=do_uninstall,
                            bg="#fc3c44", fg="#ffffff", activebackground="#d93239", activeforeground="#ffffff",
                            font=("Segoe UI Semibold", 9), bd=0, padx=14, pady=6, cursor="hand2")
        btn_del.pack(side="left")

        tk.Button(btn_box, text="Отмена", command=root.destroy,
                  bg="#1c211f", fg="#e8ecea", activebackground="#262e2b", activeforeground="#e8ecea",
                  font=("Segoe UI", 9), bd=0, padx=12, pady=6, cursor="hand2").pack(side="right")

        root.mainloop()
    else:
        perform_uninstall(purge_user_data=True, home=home)

def perform_uninstall(purge_user_data: bool, home: Path):
    try:
        subprocess.run(["taskkill", "/F", "/FI", "WINDOWTITLE eq VoicePlayer*"],
                       capture_output=True, timeout=5, creationflags=NO_WINDOW)
    except Exception:
        pass
    try:
        ps_kill = "Get-WmiObject Win32_Process | Where-Object { $_.CommandLine -like '*voice-player*' -and $_.ProcessId -ne $PID } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
        subprocess.run(["powershell", "-NoProfile", "-Command", ps_kill],
                       capture_output=True, timeout=10, creationflags=NO_WINDOW)
    except Exception:
        pass

    ps_lnk = '''
    foreach ($d in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))) {
        $l = Join-Path $d 'VoicePlayer.lnk'
        if (Test-Path $l) { Remove-Item $l -Force -ErrorAction SilentlyContinue }
    }
    '''
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps_lnk],
                       capture_output=True, timeout=10, creationflags=NO_WINDOW)
    except Exception:
        pass

    try:
        import winreg
        key_path = r"Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\VoicePlayer"
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path)
    except Exception:
        pass

    if purge_user_data:
        cmd = f'timeout /t 1 /nobreak >nul & rd /s /q "{home}"'
        subprocess.Popen(["cmd.exe", "/c", cmd], creationflags=NO_WINDOW)
    else:
        for sub in ("versions", "hooks", "updates"):
            p = home / sub
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
        for fn in ("launch.cmd", "launch.pyw", "launch.sh", "icon.ico", "icon.png"):
            p = home / fn
            if p.is_file():
                try: p.unlink()
                except Exception: pass

if __name__ == "__main__":
    main()
"""


def register_windows_uninstaller(version: str = "") -> None:
    """Зарегистрировать VoicePlayer в «Параметры -> Приложения -> Установленные приложения»
    и в «Панели управления -> Программы и компоненты» Windows."""
    if os.name != "nt":
        return
    try:
        import winreg
        v = version
        if not v:
            best = best_installed()
            v = read_version(best) if best else "1.2.0"
        h = home_dir()
        h.mkdir(parents=True, exist_ok=True)

        (h / "uninstall.cmd").write_text(UNINSTALL_CMD, encoding="ascii", errors="ignore")
        (h / "uninstall.ps1").write_text(UNINSTALL_PS1, encoding="utf-8")
        (h / "uninstall.pyw").write_text(UNINSTALL_PYW, encoding="utf-8")

        ico = h / "icon.ico"
        if not ico.is_file():
            best = best_installed()
            if best and (best / "icon.ico").is_file():
                try:
                    shutil.copy2(best / "icon.ico", ico)
                except Exception:
                    pass

        uninstall_cmd = h / "uninstall.cmd"
        size_kb = 28000
        try:
            total_b = sum(f.stat().st_size for f in h.rglob("*") if f.is_file())
            if total_b > 0:
                size_kb = int(total_b // 1024)
        except Exception:
            pass

        key_path = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\VoicePlayer"
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as k:
            winreg.SetValueEx(k, "DisplayName", 0, winreg.REG_SZ, "VoicePlayer")
            winreg.SetValueEx(k, "DisplayVersion", 0, winreg.REG_SZ, v)
            winreg.SetValueEx(k, "Publisher", 0, winreg.REG_SZ, "VoicePlayer Team")
            winreg.SetValueEx(k, "DisplayIcon", 0, winreg.REG_SZ, f'"{ico}",0' if ico.is_file() else str(ico))
            winreg.SetValueEx(k, "InstallLocation", 0, winreg.REG_SZ, str(h))
            winreg.SetValueEx(k, "InstallDate", 0, winreg.REG_SZ, time.strftime("%Y%m%d"))
            winreg.SetValueEx(k, "UninstallString", 0, winreg.REG_SZ, f'cmd.exe /c ""{uninstall_cmd}""')
            winreg.SetValueEx(k, "QuietUninstallString", 0, winreg.REG_SZ, f'cmd.exe /c ""{uninstall_cmd}"" /quiet')
            winreg.SetValueEx(k, "EstimatedSize", 0, winreg.REG_DWORD, size_kb)
            winreg.SetValueEx(k, "HelpLink", 0, winreg.REG_SZ, "https://infostroper-cloud.github.io/voice-player/")
            winreg.SetValueEx(k, "URLInfoAbout", 0, winreg.REG_SZ, "https://infostroper-cloud.github.io/voice-player/")
            winreg.SetValueEx(k, "NoModify", 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(k, "NoRepair", 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(k, "Comments", 0, winreg.REG_SZ, "VoicePlayer — Озвучка ИИ и голосовой ввод")
    except Exception:
        pass


def create_shortcuts() -> None:
    """Ярлык указывает на %USERPROFILE%: один и тот же файл на рабочем столе OneDrive
    работает на каждом компьютере, где плеер установлен."""
    if os.name != "nt":
        return
    best = best_installed()
    if best and (best / "icon.ico").is_file():
        try:
            shutil.copy2(best / "icon.ico", home_dir() / "icon.ico")
        except Exception:
            pass
    script = (
        "$s = New-Object -ComObject WScript.Shell;"
        "foreach ($d in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))) {"
        "$l = $s.CreateShortcut((Join-Path $d 'VoicePlayer.lnk'));"
        "$l.TargetPath = '%USERPROFILE%\\.voice-player\\launch.cmd'; $l.WindowStyle = 7;"
        "$l.IconLocation = '%USERPROFILE%\\.voice-player\\icon.ico'; $l.Save() }"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", script], check=True, timeout=60, creationflags=NO_WINDOW)
    register_windows_uninstaller()


def main(argv: list) -> int:
    """Первая установка: python vp_updater.py --install архив.zip [--no-shortcuts]"""
    if len(argv) < 3 or argv[1] != "--install":
        print(main.__doc__)
        return 2
    final = install_zip(Path(argv[2]))
    if "--no-shortcuts" not in argv:
        create_shortcuts()
    print(f"Установлена версия {final.name}: {final}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
