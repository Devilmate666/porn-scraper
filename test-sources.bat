@echo off
cd /d "%~dp0"
python sourcetest.py > test-report.txt 2>&1
type test-report.txt
echo.
echo Saved to test-report.txt
pause
