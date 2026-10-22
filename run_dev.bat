@echo off
rem Run Lorekeeper from source (no build needed). Add --browser to open it in your normal browser.
cd /d "%~dp0"
if not exist .venv (
  py -3.12 -m venv .venv 2>nul || python -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip >nul
pip install -r requirements.txt
if not exist vendor\ffmpeg\ffmpeg.exe powershell -NoProfile -ExecutionPolicy Bypass -File packaging\fetch_ffmpeg.ps1
python -m app %*
