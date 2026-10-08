@echo off
chcp 65001 >nul
setlocal
title Sheets Hub — установка / обновление

rem Ставит или обновляет программу в %%LOCALAPPDATA%%\SheetsHub\app
rem и запускает её. Можно положить ярлык на этот файл на все ПК
rem или кинуть сам .bat в общую папку.
rem config.yaml, credentials.json и token.json на ПК не затираются.

set "REPO=alex237mbibik-crypto/Helix"
set "INSTALL=%LOCALAPPDATA%\SheetsHub\app"
set "ZIP=%TEMP%\SheetsHub-Windows.zip"
set "STAGE=%TEMP%\SheetsHub-stage"

echo.
echo Sheets Hub: проверка обновления с GitHub...
echo Установка: %INSTALL%
echo.

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop';" ^
  "$headers=@{ 'User-Agent'='SheetsHub-Updater'; 'Accept'='application/vnd.github+json' };" ^
  "$rel=Invoke-RestMethod -Uri 'https://api.github.com/repos/%REPO%/releases/latest' -Headers $headers;" ^
  "$asset=$rel.assets | Where-Object { $_.name -eq 'SheetsHub-Windows.zip' } | Select-Object -First 1;" ^
  "if (-not $asset) { $asset=$rel.assets | Where-Object { $_.name -like '*.zip' } | Select-Object -First 1 };" ^
  "if (-not $asset) { throw 'В релизе нет zip. Сначала дождитесь GitHub Actions / Release.' };" ^
  "Write-Host ('Скачиваю ' + $rel.tag_name + ' ...');" ^
  "Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $env:ZIP -UseBasicParsing;" ^
  "if (Test-Path $env:STAGE) { Remove-Item -Recurse -Force $env:STAGE };" ^
  "New-Item -ItemType Directory -Path $env:STAGE | Out-Null;" ^
  "Expand-Archive -Path $env:ZIP -DestinationPath $env:STAGE -Force;" ^
  "$src=$env:STAGE;" ^
  "if (-not (Test-Path (Join-Path $src 'SheetsHub.exe'))) {" ^
  "  $inner=Get-ChildItem $src -Directory | Select-Object -First 1;" ^
  "  if ($inner) { $src=$inner.FullName }" ^
  "};" ^
  "if (-not (Test-Path (Join-Path $src 'SheetsHub.exe'))) { throw 'В архиве нет SheetsHub.exe' };" ^
  "New-Item -ItemType Directory -Path $env:INSTALL -Force | Out-Null;" ^
  "foreach ($f in @('config.yaml','credentials.json','token.json')) { $p=Join-Path $src $f; if (Test-Path $p) { Remove-Item $p -Force } };" ^
  "robocopy $src $env:INSTALL /E /R:2 /W:1 /XF credentials.json token.json config.yaml /XD webview_data SheetsHub_data updates | Out-Null;" ^
  "if ($LASTEXITCODE -ge 8) { throw 'Не удалось скопировать файлы программы' };" ^
  "Write-Host 'Готово. Локальный config.yaml не трогали.'"

if errorlevel 1 (
  echo.
  echo Не удалось скачать обновление. Нужен интернет и опубликованный GitHub Release.
  echo Проверьте Actions → Build Windows.
  pause
  exit /b 1
)

if not exist "%INSTALL%\SheetsHub.exe" (
  echo Не найден SheetsHub.exe после установки.
  pause
  exit /b 1
)

start "" "%INSTALL%\SheetsHub.exe"
endlocal
exit /b 0
