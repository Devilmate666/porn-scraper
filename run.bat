@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

REM ---- edit these two lines if your GitHub repo changes ----
set "OWNER=Devilmate666"
set "REPO=porn-scraper"

echo ============================================
echo  Porn Archive - GitHub + Cloudflare Deploy
echo ============================================
echo.

REM 1. git must exist before anything else uses it
where git >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Git not found. Install from https://git-scm.com/
    pause
    exit /b 1
)

REM 2. git identity, only when not configured yet
git config user.email >nul 2>&1
if errorlevel 1 (
    echo [INFO] Setting git identity...
    git config --global user.email "satanhisham@gmail.com"
    git config --global user.name "Devilmate666"
)

REM 3. make sure junk is never committed
if not exist .gitignore (
    echo [INFO] Creating .gitignore...
    > .gitignore (
        echo node_modules/
        echo static/
        echo tunnel.log
        echo __pycache__/
        echo *.pyc
        echo .wrangler/
        echo deploy.log
    )
)

REM 4. repo + remote
git rev-parse --git-dir >nul 2>&1
if errorlevel 1 (
    echo [INFO] Not a git repo. Initializing...
    git init
    git branch -M main
)
git remote get-url origin >nul 2>&1
if errorlevel 1 (
    echo [INFO] No origin remote. Adding https://github.com/%OWNER%/%REPO%.git
    git remote add origin https://github.com/%OWNER%/%REPO%.git
)

echo [0/5] Checking the project files (doctor.ps1)...
if not exist "%~dp0doctor.ps1" (
    echo [ERROR] doctor.ps1 is missing. Put it in the same folder as run.bat.
    pause
    exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0doctor.ps1" -Phase pre
if errorlevel 1 (
    echo.
    echo [ERROR] Fix the [FAIL] lines above, then run this script again. Nothing was pushed.
    pause
    exit /b 1
)
echo.

echo [1/5] Checking for changes...
git status --short
echo.
set "COMMIT_MSG="
set /p COMMIT_MSG="Enter commit message [Deploy Worker + Scraper]: "
if "!COMMIT_MSG!"=="" set "COMMIT_MSG=Deploy Worker + Scraper"
REM a double quote in the message would break the commit command
set "COMMIT_MSG=!COMMIT_MSG:"=!"

echo.
echo [2/5] Adding all files...
git add -A

echo [3/5] Committing...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0doctor.ps1" -Phase tracked
if errorlevel 1 (
    echo.
    echo [ERROR] Git would not upload some files. See the [FAIL] lines above. Nothing was pushed.
    pause
    exit /b 1
)
git commit -m "!COMMIT_MSG!"
if errorlevel 1 (
    echo [INFO] Nothing new to commit - making an empty commit so the deploy runs again.
    git commit --allow-empty -m "!COMMIT_MSG! (redeploy)"
)

echo [4/5] Syncing with GitHub...
git ls-remote --exit-code --heads origin main >nul 2>&1
if not errorlevel 1 (
    git pull --rebase origin main
    if errorlevel 1 (
        echo.
        echo [ERROR] Pull/rebase hit a conflict. Aborting the rebase so nothing is lost.
        echo         Fix the conflicting files, then run this script again.
        git rebase --abort >nul 2>&1
        pause
        exit /b 1
    )
) else (
    echo [INFO] Remote has no main branch yet - first push.
)

git push -u origin main
if errorlevel 1 (
    echo [ERROR] Push failed. Check your GitHub login and the remote URL:
    git remote get-url origin
    pause
    exit /b 1
)

echo.
echo [5/5] Pushed. The "Deploy Worker + Frontend" workflow starts now.
echo.
echo ============================================
echo  FIRST-TIME SETUP, repository secrets:
echo ============================================
echo https://github.com/%OWNER%/%REPO%/settings/secrets/actions
echo   Required:
echo     CLOUDFLARE_API_TOKEN    dash.cloudflare.com/profile/api-tokens
echo                             must also have  Account - D1 - Edit  (for the database)
echo     CLOUDFLARE_ACCOUNT_ID   dash.cloudflare.com, right sidebar
echo   Login (username + password, no email needed) - needs:
echo     AUTH_SECRET             any random 32+ characters (secret)
echo   Optional:
echo     GH_DISPATCH_TOKEN       lets the Worker start the GitHub scraper when data is stale
echo                             fine-grained token, this repo only, Actions: Read and write
echo     BACKEND_URL             your live Flask backend, see LIVE_MODE.md
echo   Optional repo variables, Settings - Variables:
echo     ALLOWED_HOSTS, WORKER_MODE, CHATURBATE_WM, CAM_PROVIDERS
echo.
echo Watch the run: https://github.com/%OWNER%/%REPO%/actions
echo   Deploy Worker + Frontend  - deploys, then fills the cache once
echo   Scrape live / Scrape full - scheduled scrapers
echo The Worker URL is printed in the deploy log, step "Deploy Worker".
echo Frontend: https://porn-archive.pages.dev
echo.
start "" "https://github.com/%OWNER%/%REPO%/actions"
echo.
echo Now waiting for the deploy and testing the live site (up to 6 minutes)...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0doctor.ps1" -Phase post
echo.
echo Done.
pause
