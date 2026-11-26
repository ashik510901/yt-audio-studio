@echo off
setlocal
cd /d "%~dp0"
set APP_MODE=local
set PYTHONUTF8=1
echo ================================================
echo  YT Audio Studio - Local Windows Mode
echo ================================================

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo Python was not found.
  echo Install Python 3.11 or newer from https://www.python.org/downloads/
  echo and tick "Add python.exe to PATH", then run this file again.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment...
  %PY% -m venv .venv
  if errorlevel 1 (
    echo Failed to create the virtual environment.
    pause
    exit /b 1
  )
)
call ".venv\Scripts\activate.bat"

echo Installing / updating packages ...
python -m pip install --upgrade pip >nul 2>nul
pip install -r requirements.txt
if errorlevel 1 (
  echo Package installation failed. Check your internet connection.
  pause
  exit /b 1
)

where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo FFmpeg was not found. Trying to install it with winget...
  winget install -e --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements
  echo.
  echo If FFmpeg was just installed, CLOSE this window and run this file again.
  pause
  exit /b 1
)

echo Starting the app. A browser tab will open. Close this window to stop the app.
streamlit run streamlit_app.py
pause
