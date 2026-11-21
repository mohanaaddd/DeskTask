@echo off
setlocal
cd /d "%~dp0"
echo === Building DeskTask.exe ===
where python >nul 2>&1
if errorlevel 1 (
  echo Python is not installed. Get it from python.org and tick "Add python.exe to PATH".
  pause & exit /b 1
)
taskkill /f /im DeskTask.exe >nul 2>&1
python -m pip install --upgrade pip pyinstaller pystray pillow
if errorlevel 1 ( echo Install failed. & pause & exit /b 1 )
python -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name DeskTask --icon icon.ico ^
  --hidden-import pystray._win32 DeskTask.py
if errorlevel 1 ( echo Build failed. & pause & exit /b 1 )
echo.
echo Done. Your app is: dist\DeskTask.exe
pause
