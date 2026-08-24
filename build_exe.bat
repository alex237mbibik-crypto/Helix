@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  call run.bat
)

echo Собираю SheetsHub.exe ...
".venv\Scripts\pyinstaller.exe" --noconfirm --clean --windowed --onedir --name SheetsHub --noupx --collect-all customtkinter --collect-all certifi --collect-all gspread --collect-all google_auth_oauthlib --hidden-import=certifi --hidden-import=google_auth_oauthlib --hidden-import=google_auth_oauthlib.flow --add-data "config.example.yaml;." launcher.py
copy /Y config.example.yaml dist\SheetsHub\ >nul
if exist credentials.example.json copy /Y credentials.example.json dist\SheetsHub\ >nul
if exist packaging\bundled_credentials.json copy /Y packaging\bundled_credentials.json dist\SheetsHub\credentials.json >nul
if exist packaging\HOW_TO_RUN.txt copy /Y packaging\HOW_TO_RUN.txt dist\SheetsHub\ >nul
if exist packaging\start_SheetsHub.bat copy /Y packaging\start_SheetsHub.bat dist\SheetsHub\ >nul
echo.
echo Готово: dist\SheetsHub\SheetsHub.exe
echo Нужна вся папка dist\SheetsHub вместе с _internal
if exist dist\SheetsHub\credentials.json echo credentials.json уже лежит в папке сборки
echo После входа появится token.json
pause
