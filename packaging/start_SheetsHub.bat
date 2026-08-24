@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist "_internal\" (
  echo.
  echo ОШИБКА: рядом с SheetsHub.exe нет папки _internal.
  echo Распакуйте ВЕСЬ архив SheetsHub-Windows, а не один .exe.
  echo.
  pause
  exit /b 1
)

if not exist "SheetsHub.exe" (
  echo Не найден SheetsHub.exe в этой папке.
  pause
  exit /b 1
)

start "" "SheetsHub.exe"
