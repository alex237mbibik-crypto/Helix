@echo off
chcp 65001 >nul
cd /d "%~dp0"

where py >nul 2>&1
if %errorlevel%==0 (
  set PY=py -3
) else (
  set PY=python
)

if not exist ".venv\Scripts\python.exe" (
  echo Создаю виртуальное окружение...
  %PY% -m venv .venv
  if errorlevel 1 (
    echo Не найден Python 3. Установите с https://www.python.org/downloads/
    echo При установке отметьте "Add python.exe to PATH".
    pause
    exit /b 1
  )
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
)

".venv\Scripts\python.exe" -m sheets_hub
if errorlevel 1 pause
