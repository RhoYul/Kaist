@echo off
rem NAS direct LAN setup - run once on the capture PC after connecting the NAS with a LAN cable
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\Setup-DirectNAS.ps1"
echo.
pause
