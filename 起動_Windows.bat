@echo off
rem PDF Mail Sender launcher (Windows) - double-click to start
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (set PYCMD=py -3) else (set PYCMD=python)
%PYCMD% --version >nul 2>nul
if not %errorlevel%==0 (
  echo Python 3 not found. Please install from https://www.python.org/downloads/
  echo ^(Important: check "Add python.exe to PATH" during installation^)
  pause
  exit /b 1
)

if not exist .venv (
  echo First-time setup, please wait 1-2 minutes...
  %PYCMD% -m venv .venv
  .venv\Scripts\pip install --quiet -r requirements.txt
  if not %errorlevel%==0 (
    echo Setup failed. Please check your internet connection.
    rmdir /s /q .venv
    pause
    exit /b 1
  )
  echo Setup complete.
)

.venv\Scripts\python app.py
pause
