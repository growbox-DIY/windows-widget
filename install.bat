@echo off
rem Installs Pillow and pystray, then starts the GrowBox widget (no console window).
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
  echo Python not found. Install it with:
  echo   winget install Python.Python.3.12
  echo and run this file again.
  pause
  exit /b 1
)
py -m pip install --user --upgrade pillow pystray
start "" pyw "%~dp0growbox_widget.pyw"
