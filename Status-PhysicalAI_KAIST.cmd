@echo off
rem PhysicalAI KAIST Platform 상태 ? 서버가 켜져 있는지 · 카메라 연결 수
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\Server.ps1" status
if errorlevel 1 (echo. & pause & exit /b 1)
pause