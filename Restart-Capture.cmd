@echo off
rem 카메라가 잘 안 나올 때 ? 촬영 PC 서버만 정상 종료(카메라 닫기) 후 다시 켠다.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\Server.ps1" restart -Role capture
if errorlevel 1 (echo. & pause & exit /b 1)
timeout /t 3 >nul