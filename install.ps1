# VoicePlayer Windows Installer - Zero-SmartScreen
# https://github.com/infostroper-cloud/voice-player

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$ErrorActionPreference = "Stop"

$Version = "1.2.4"
$Repo = "infostroper-cloud/voice-player"
$InstallDir = Join-Path $env:LOCALAPPDATA "Programs\VoicePlayer"
$ZipUrl = "https://github.com/$Repo/releases/download/voice-player-v$Version/voice-player-$Version.zip"

Write-Host ""
Write-Host "  ========================================================" -ForegroundColor DarkGreen
Write-Host "    VoicePlayer $Version — Установка на Windows" -ForegroundColor Green
Write-Host "    Озвучка ИИ и голосовой ввод (Local TTS & Speech)" -ForegroundColor Gray
Write-Host "  ========================================================" -ForegroundColor DarkGreen
Write-Host ""

# 1. Поиск рабочего Python >= 3.10
function Find-Python {
    foreach ($cmd in @("python.exe", "python3.exe", "py.exe")) {
        try {
            $cmds = @(Get-Command $cmd -ErrorAction SilentlyContinue)
            foreach ($c in $cmds) {
                $p = $c.Source
                if ($p -and $p -notlike "*WindowsApps*" -and (Test-Path $p)) {
                    $ver = & $p -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
                    if ($ver -and [float]$ver -ge 3.10) { return $p }
                }
            }
        } catch {}
    }
    $searchDirs = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Python"),
        (Join-Path $env:LOCALAPPDATA "Python"),
        (Join-Path $env:ProgramFiles "Python*")
    )
    foreach ($base in $searchDirs) {
        if (Test-Path $base) {
            $pyFiles = @(Get-ChildItem -Path $base -Filter "python.exe" -Recurse -ErrorAction SilentlyContinue)
            foreach ($f in $pyFiles) {
                try {
                    $p = $f.FullName
                    if ($p -notlike "*Scripts*" -and (Test-Path $p)) {
                        $ver = & $p -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
                        if ($ver -and [float]$ver -ge 3.10) { return $p }
                    }
                } catch {}
            }
        }
    }
    return $null
}

Write-Host "[1/5] Проверка среды Python..." -ForegroundColor Cyan
$PythonExe = Find-Python
if ($PythonExe -is [array]) {
    $PythonExe = $PythonExe[0]
}

if (-not $PythonExe) {
    Write-Host "      Python 3.10+ не найден. Загружаю официальный Python 3.12..." -ForegroundColor Yellow
    $PyInstallerUrl = "https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe"
    $TempPy = Join-Path $env:TEMP "python-3.12.8-amd64.exe"
    Invoke-WebRequest -Uri $PyInstallerUrl -OutFile $TempPy -UseBasicParsing
    Unblock-File $TempPy -ErrorAction SilentlyContinue
    Write-Host "      Устанавливаю Python (тихий режим)..." -ForegroundColor Yellow
    Start-Process -FilePath $TempPy -ArgumentList "/quiet InstallAllUsers=0 PrependPath=1 Include_test=0 Include_tcltk=1" -Wait
    Remove-Item $TempPy -Force -ErrorAction SilentlyContinue
    $PythonExe = Find-Python
    if ($PythonExe -is [array]) {
        $PythonExe = $PythonExe[0]
    }
}

if (-not $PythonExe) {
    Write-Error "Не удалось найти или установить Python 3.10+. Установите Python с python.org и повторите попытку."
    exit 1
}
Write-Host "      Найден Python: $PythonExe" -ForegroundColor Green

# 2. Подготовка каталога установки
Write-Host "[2/5] Подготовка папки установки..." -ForegroundColor Cyan
if (-not (Test-Path $InstallDir)) {
    New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
}
Write-Host "      Папка: $InstallDir" -ForegroundColor Gray

# 3. Скачивание актуального архива
Write-Host "[3/5] Скачивание VoicePlayer $Version..." -ForegroundColor Cyan
$TempZip = Join-Path $env:TEMP "voice-player-$Version.zip"
try {
    Invoke-WebRequest -Uri $ZipUrl -OutFile $TempZip -UseBasicParsing
} catch {
    Write-Host "      Прямая ссылка не ответила, пробую API релизов GitHub..." -ForegroundColor Yellow
    $ApiUrl = "https://api.github.com/repos/$Repo/releases/tags/voice-player-v$Version"
    $Rel = Invoke-RestMethod -Uri $ApiUrl -UseBasicParsing
    $Asset = $Rel.assets | Where-Object { $_.name -like "voice-player-*.zip" } | Select-Object -First 1
    if (-not $Asset) { throw "Архив релиза не найден в $Repo" }
    Invoke-WebRequest -Uri $Asset.browser_download_url -OutFile $TempZip -UseBasicParsing
}
Unblock-File $TempZip -ErrorAction SilentlyContinue

# 4. Распаковка и запуск установщика vp_updater
Write-Host "[4/5] Распаковка и проверка зависимостей..." -ForegroundColor Cyan
$TempExtract = Join-Path $env:TEMP "vp-setup-$Version"
if (Test-Path $TempExtract) { Remove-Item $TempExtract -Recurse -Force }
Expand-Archive -Path $TempZip -DestinationPath $TempExtract -Force
Get-ChildItem -Path $TempExtract -Recurse | Unblock-File -ErrorAction SilentlyContinue

$Updater = Join-Path $TempExtract "vp_updater.py"
& "$PythonExe" "$Updater" --install "$TempZip" --dest "$InstallDir"

Remove-Item $TempZip -Force -ErrorAction SilentlyContinue
Remove-Item $TempExtract -Recurse -Force -ErrorAction SilentlyContinue

# 5. Разблокировка и запуск
Write-Host "[5/5] Разблокировка файлов и регистрация в Windows..." -ForegroundColor Cyan
Get-ChildItem -Path $InstallDir -Recurse | Unblock-File -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "✓ Установка успешно завершена!" -ForegroundColor Green
Write-Host "  Ярлыки созданы на Рабочем столе и в меню «Пуск»." -ForegroundColor Gray
Write-Host "  Запускаю VoicePlayer..." -ForegroundColor Cyan

$LaunchCmd = Join-Path $InstallDir "launch.cmd"
if (Test-Path $LaunchCmd) {
    Start-Process -FilePath "cmd.exe" -ArgumentList "/c `"$LaunchCmd`"" -WindowStyle Hidden
} else {
    $LaunchPyw = Join-Path $InstallDir "launch.pyw"
    $Pyw = Join-Path (Split-Path $PythonExe) "pythonw.exe"
    if (-not (Test-Path $Pyw)) { $Pyw = $PythonExe }
    Start-Process -FilePath $Pyw -ArgumentList "`"$LaunchPyw`"" -WindowStyle Hidden
}
