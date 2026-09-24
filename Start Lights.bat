@echo off
title Govee Party Lights
cd /d "%~dp0"

REM --- Optional: paste the full path to rkbx_link.exe between the quotes to auto-start it ---
REM     Example: set "RKBX=C:\Users\gho13\Desktop\rkbx_link\rkbx_link.exe"
set "RKBX="

if not "%RKBX%"=="" (
  for %%F in ("%RKBX%") do start "rkbx_link" /d "%%~dpF" "%%~fF"
  timeout /t 3 /nobreak >nul
)

python govee_party.py
echo.
echo Lights stopped. Press any key to close this window.
pause >nul
