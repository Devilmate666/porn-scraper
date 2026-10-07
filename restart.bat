@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0restart-server.ps1"
if errorlevel 1 (
  echo Server restart stopped because the existing server could not be identified safely.
  pause
  exit /b 1
)
call "%~dp0run.bat"
