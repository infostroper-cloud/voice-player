"""Собрать архив обновления из зафиксированного в git состояния и выпустить его.

Сборка:   python packaging/release.py build [--out папка]
Выпуск:   python packaging/release.py publish   (архив уходит в GitHub Releases, плееры подхватят сами)

В архив попадают только файлы из белого списка и только из коммита HEAD: незафиксированные
правки, секреты и данные в выпуск попасть не могут.
"""
import hashlib
import io
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = "infostroper-cloud/pro"
PUBLIC_REPO = "infostroper-cloud/voice-player"
ROOT_FILES = {"VERSION", "requirements.txt", "icon.ico", "LICENSE", "uninstall.sh"}
GH = shutil.which("gh") or r"C:\Program Files\GitHub CLI\gh.EXE"


def _git(*args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True).stdout


def wanted(path: str) -> bool:
    if "/" not in path:
        return path in ROOT_FILES or path.endswith(".py")
    return path.startswith("hooks/") and path.endswith(".py")


def build(out_dir: Path) -> tuple:
    if _git("status", "--porcelain", "--", ".").strip():
        sys.exit("Есть незафиксированные правки в voice-player: сначала коммит.")
    version = _git("show", "HEAD:./VERSION").decode().strip()
    names = [n for n in _git("ls-tree", "-r", "--name-only", "HEAD", ".").decode("utf-8").splitlines() if wanted(n)]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for n in names:
            z.writestr(n, _git("show", f"HEAD:./{n}"))
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"voice-player-{version}.zip"
    zip_path.write_bytes(buf.getvalue())
    sha_path = Path(str(zip_path) + ".sha256")
    sha_path.write_text(f"{hashlib.sha256(buf.getvalue()).hexdigest()}  {zip_path.name}\n", encoding="ascii")
    print(f"{zip_path}: {len(names)} файлов, {len(buf.getvalue()) // 1024} КБ")
    return version, zip_path, sha_path


def publish() -> None:
    with tempfile.TemporaryDirectory() as td:
        version, zip_path, sha_path = build(Path(td))
        commit = _git("rev-parse", "HEAD").decode().strip()
        subprocess.run([GH, "release", "create", f"voice-player-v{version}", str(zip_path), str(sha_path),
                        "--repo", REPO, "--target", commit, "--title", f"VoicePlayer {version}",
                        "--notes", f"Сборка из {commit[:7]}", "--latest=false"], check=True)
        # Также публикуем в публичный репозиторий витрины/обновлений (для внешних пользователей без gh auth)
        installer_zip = ROOT / "packaging" / "Output" / f"VoicePlayer-Installer-{version}.zip"
        pub_files = [str(zip_path), str(sha_path)] + ([str(installer_zip)] if installer_zip.is_file() else [])
        subprocess.run([GH, "release", "create", f"voice-player-v{version}", *pub_files,
                        "--repo", PUBLIC_REPO, "--title", f"VoicePlayer {version}",
                        "--notes", f"Автообновление и установщик VoicePlayer {version}", "--latest"], check=False)
    print(f"Выпущена версия {version}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "publish":
        publish()
    elif len(sys.argv) > 1 and sys.argv[1] == "build":
        build(Path(sys.argv[3]) if len(sys.argv) > 3 else ROOT / "packaging" / "Output")
    else:
        print(__doc__)
