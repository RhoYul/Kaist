@echo off
rem Frame drop diagnosis (capture PC). Stops the capture server (cameras), runs tests A/D/B/C (~2 min), starts it again.
rem Result: logs\diag-frames-<time>.txt
cd /d "%~dp0"
chcp 65001 >nul
set "PY=%~dp0python\python.exe"
if not exist "%PY%" set "PY=%USERPROFILE%\Desktop\ui_install\python\python.exe"
if not exist "%PY%" set "PY=python"
echo == 1/3 Stop capture server (release cameras)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\Server.ps1" stop -Role capture
if errorlevel 1 (echo Capture server is busy. Try again after recording/QC/NAS finishes. & pause & exit /b 1)
echo == 2/3 Diagnosis running - do not touch cameras/USB for about 2 minutes
"%PY%" "%~dp0tools\diag_frames.py" %*
echo == 3/3 Start capture server again
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\Server.ps1" start -Role capture
echo.
echo Send the newest logs\diag-frames-*.txt file to the control PC.
pause
