@echo off
setlocal
cd /d "%~dp0"
echo ============================================
echo  Fix GitHub sync - keep MY index.html
echo ============================================
echo.

rem Make sure no half-finished rebase/merge is hanging around
git rebase --abort >nul 2>&1
git merge --abort >nul 2>&1

rem Safety backup of your current file
copy /y index.html index_backup_before_sync.html >nul
echo [OK] Backup saved: index_backup_before_sync.html

rem Commit anything that is not committed yet
git add -A
git commit -m "Local changes (card tags + combined tag feed)" >nul 2>&1

echo.
echo [1/3] Fetching GitHub...
git fetch origin
if errorlevel 1 goto :fail

echo.
echo [2/3] Merging GitHub history, keeping YOUR files as they are...
git merge -s ours origin/main -m "Merge origin/main - keep local index.html"
if errorlevel 1 goto :fail

echo.
echo [3/3] Pushing to GitHub...
git push origin main
if errorlevel 1 goto :fail

echo.
echo ============================================
echo  DONE. GitHub now has your index.html and
echo  the deploy will start. run.bat works again.
echo ============================================
pause
exit /b 0

:fail
echo.
echo [ERROR] Something failed - see the message above.
echo Your file is safe: index_backup_before_sync.html
pause
exit /b 1
