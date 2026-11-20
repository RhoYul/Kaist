@echo off
rem PhysicalAI KAIST Platform 켜기 ? export 서버(8766) · 촬영 PC 서버(8765)를 창 없이 백그라운드로 켠다. 이 창은 3초 뒤 닫힌다.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\Server.ps1" start
if errorlevel 1 (echo. & pause & exit /b 1)
timeout /t 3 >nul