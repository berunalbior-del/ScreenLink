@echo off
REM 1) Put ffmpeg.exe (from https://www.gyan.dev/ffmpeg/builds/ "release essentials") in this folder
REM 2) Run this file -> dist\ScreenLink.exe
pip install pyinstaller
pyinstaller --onefile --noconsole --name ScreenLink --add-binary "ffmpeg.exe;." server.py
echo.
echo Done: dist\ScreenLink.exe
pause
