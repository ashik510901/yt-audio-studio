@echo off
setlocal
cd /d "%~dp0"
set APP_MODE=local
set PYTHONUTF8=1
if not exist ".venv\Scripts\python.exe" (
  echo Run run_local_windows.bat once first, so everything gets installed.
  pause
  exit /b 1
)
call ".venv\Scripts\activate.bat"
echo ================================================
echo  YT Audio Studio - Download Diagnostics (local)
echo ================================================
set /p URL=Paste the YouTube link and press Enter: 
echo.
echo Cookies from a browser: firefox / edge / chrome / brave  (press Enter to skip)
set /p BR=Browser: 
echo.
if "%BR%"=="" (
  python diagnose.py "%URL%"
) else (
  python diagnose.py "%URL%" --browser %BR%
)
echo.
pause
