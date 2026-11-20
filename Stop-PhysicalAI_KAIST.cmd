@echo off
rem PhysicalAI KAIST Platform 끄기 ? 카메라를 정상적으로 닫고 두 서버를 끈다. 촬영 · 전송 중이면 끄지 않는다.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\Server.ps1" stop
if errorlevel 1 (echo. & pause & exit /b 1)
timeout /t 3 >nul