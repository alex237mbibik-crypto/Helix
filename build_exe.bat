@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  call run.bat
)

echo Собираю SheetsHub.exe ...
".venv\Scripts\pyinstaller.exe" --noconfirm --clean --windowed --name SheetsHub --collect-all customtkinter --add-data "config.example.yaml;." launcher.py
copy /Y config.example.yaml dist\SheetsHub\ >nul
if exist credentials.example.json copy /Y credentials.example.json dist\SheetsHub\ >nul
echo.
echo Готово: dist\SheetsHub\SheetsHub.exe
echo Рядом с exe положите config.yaml и credentials.json
pause
