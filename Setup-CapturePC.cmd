@echo off
rem 촬영 PC 설치 ? 카메라 4대가 연결된 PC 에서 한 번 실행 (Python 확인 · 설정 · 방화벽 · 자동 실행 · 서버 켜기)
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\Setup-CapturePC.ps1"
echo.
pause