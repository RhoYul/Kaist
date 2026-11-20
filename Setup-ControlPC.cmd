@echo off
rem 관제 PC 설치 ? 관리자 노트북에서 한 번 실행 (이 PC 에서만 도는 관제 화면 http://localhost:8767)
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\Setup-ControlPC.ps1"
echo.
pause