# doctor.ps1 - called by run.bat. Puts the login files where they belong, checks everything, and tests the live Worker.
#   -Phase pre      before commit : find / copy / verify the files
#   -Phase tracked  after git add : make sure git really will upload them
#   -Phase post     after push    : wait for the deploy and ask the live Worker what is still missing
param([ValidateSet('pre', 'tracked', 'post')][string]$Phase = 'pre')
$ErrorActionPreference = 'Continue'
Set-Location $PSScriptRoot
$script:fail = 0

function Ok($m)   { Write-Host "  [ OK ] $m" -ForegroundColor Green }
function Info($m) { Write-Host "         $m" -ForegroundColor Gray }
function Warn($m) { Write-Host "  [WARN] $m" -ForegroundColor Yellow }
function Bad($m)  { Write-Host "  [FAIL] $m" -ForegroundColor Red; $script:fail++ }
function ReadText($p) {
    if (Test-Path -LiteralPath $p -PathType Leaf) { return [IO.File]::ReadAllText((Resolve-Path -LiteralPath $p).Path) }
    return $null
}

# path (relative to this folder) + a text that only the NEW version of the file contains
$targets = @(
    @{ Path = 'auth.ts';                      Marker = 'handleAuth' },
    @{ Path = 'types.ts';                     Marker = 'AUTH_SECRET' },
    @{ Path = 'src\index.ts';                 Marker = 'handleAuth' },
    @{ Path = 'wrangler.toml';                Marker = 'd1_databases' },
    @{ Path = 'index.html';                   Marker = 'acctBtn' },
    @{ Path = 'migrations\0001_auth.sql';     Marker = 'password_hash' },
    @{ Path = 'migrations\0002_alter_users.sql'; Marker = 'already_migrated' },
    @{ Path = 'migrations\0003_clear_rate.sql';    Marker = 'DELETE FROM rate' },
    @{ Path = '.github\scripts\d1_id.sh';     Marker = 'D1_NAME' },
    @{ Path = '.github\workflows\deploy.yml'; Marker = 'd1_id.sh' }
)

function Find-Candidate($leaf, $marker) {
    $base = [IO.Path]::GetFileNameWithoutExtension($leaf)
    $ext = [IO.Path]::GetExtension($leaf)
    $found = @()
    $found += Get-ChildItem -Path $PSScriptRoot -Recurse -File -Filter "$base*$ext" -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -notmatch '\\(node_modules|\.git|\.wrangler|static)\\' }
    foreach ($d in @('Downloads', 'Desktop')) {
        $dir = Join-Path $env:USERPROFILE $d
        if (Test-Path $dir) { $found += Get-ChildItem -Path $dir -File -Filter "$base*$ext" -ErrorAction SilentlyContinue }
    }
    foreach ($f in ($found | Sort-Object LastWriteTime -Descending)) {
        $t = ReadText $f.FullName
        if ($t -and $t.Contains($marker)) { return $f }
    }
    return $null
}

function Show-Runs($owner, $repo) {
    try {
        $r = Invoke-RestMethod -Uri "https://api.github.com/repos/$owner/$repo/actions/runs?per_page=4" -Headers @{ 'User-Agent' = 'doctor' } -TimeoutSec 15
        $head = (git rev-parse HEAD 2>$null)
        Info "Latest GitHub Actions runs (your commit is $($head.Substring(0, 7))):"
        foreach ($x in $r.workflow_runs) {
            $same = if ($head -and $x.head_sha -eq $head) { 'THIS commit' } else { 'older commit ' + $x.head_sha.Substring(0, 7) }
            $res = if ($x.conclusion) { $x.conclusion } else { $x.status }
            Info ("  {0,-40} {1,-12} {2}" -f $x.name, $res, $same)
        }
    } catch {
        Info "(could not read the run list - open the Actions page yourself)"
    }
}

function Get-RepoParts {
    $url = (git remote get-url origin 2>$null)
    if ($url -match 'github\.com[:/]+([^/]+)/([^/.]+)') { return @($Matches[1], $Matches[2]) }
    return @($null, $null)
}

# =====================================================================================================
if ($Phase -eq 'pre') {
    Write-Host ""
    Write-Host "=== Checking that every login file is in the right place ===" -ForegroundColor Cyan

    foreach ($t in $targets) {
        $cur = ReadText $t.Path
        if ($cur -and $cur.Contains($t.Marker)) { Ok $t.Path; continue }
        $leaf = Split-Path $t.Path -Leaf
        $c = Find-Candidate $leaf $t.Marker
        if ($c) {
            $dir = Split-Path $t.Path -Parent
            if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
            Copy-Item -LiteralPath $c.FullName -Destination $t.Path -Force
            Ok "$($t.Path)  (fixed: copied the new version from $($c.FullName))"
        } elseif ($cur) {
            Bad "$($t.Path) is still the OLD version and the new one was not found anywhere. Download it again and put it there."
        } else {
            Bad "$($t.Path) is missing and no copy was found. Download it again and put it there."
        }
    }

    # stray copies in the wrong folder are harmless but confusing
    foreach ($stray in @('index.ts', '0001_auth.sql', 'd1_id.sh', 'deploy.yml')) {
        if (Test-Path -LiteralPath $stray -PathType Leaf) { Warn "stray file in the project root: $stray (the real one lives in a sub-folder). You can delete it." }
    }

    # content checks
    $idx = ReadText 'src\index.ts'
    if ($idx) {
        if ($idx.Contains('"../auth"')) { Ok "src\index.ts imports the auth module" } else { Bad 'src\index.ts does not import "../auth" (old file)' }
        if ($idx -match 'Access-Control-Allow-Headers"\s*:\s*"Content-Type,\s*Authorization"') { Ok "CORS allows the Authorization header" } else { Bad "src\index.ts CORS does not allow Authorization (old file)" }
    }
    $wr = ReadText 'wrangler.toml'
    if ($wr) {
        if ($wr -match 'main\s*=\s*"src/index\.ts"') { Ok "wrangler.toml main = src/index.ts" } else { Bad 'wrangler.toml: main must be "src/index.ts"' }
        if ($wr -match 'binding\s*=\s*"DB"') { Ok "wrangler.toml has the D1 binding DB" } else { Bad "wrangler.toml has no D1 binding named DB" }
    }
    $dy = ReadText '.github\workflows\deploy.yml'
    if ($dy) {
        if ($dy.Contains('migrations/0001_auth.sql')) { Ok "deploy.yml runs the database migration" } else { Bad "deploy.yml does not run migrations/0001_auth.sql (old file)" }
        if ($dy.Contains('AUTH_SECRET')) { Ok "deploy.yml hands AUTH_SECRET to the Worker" } else { Bad "deploy.yml does not hand over the login secrets (old file)" }
    }
    $ix = ReadText 'index.html'
    if ($ix -and $ix.Contains('/api/auth/config')) { Ok "index.html asks the Worker if login is enabled" } elseif ($ix) { Bad "index.html has no login code (old file)" }

    # shell scripts must use LF line endings or GitHub's Linux runner cannot run them
    foreach ($sh in @('.github\scripts\d1_id.sh', '.github\scripts\kv_ids.sh')) {
        if (Test-Path -LiteralPath $sh) {
            $b = [IO.File]::ReadAllBytes((Resolve-Path -LiteralPath $sh).Path)
            $s = [Text.Encoding]::UTF8.GetString($b)
            if ($s.Contains("`r`n")) {
                [IO.File]::WriteAllBytes((Resolve-Path -LiteralPath $sh).Path, [Text.Encoding]::UTF8.GetBytes($s.Replace("`r`n", "`n")))
                Ok "$sh  (fixed: Windows line endings converted to Linux)"
            }
        }
    }
    if (-not (Test-Path '.github\scripts\kv_ids.sh')) { Bad ".github\scripts\kv_ids.sh is missing (your old script, the deploy needs it)" }

    # .gitattributes keeps scripts/workflows in LF
    if (-not (Test-Path '.gitattributes')) {
        [IO.File]::WriteAllText((Join-Path $PSScriptRoot '.gitattributes'), "*.sh text eol=lf`n*.yml text eol=lf`n*.yaml text eol=lf`n*.py text eol=lf`n*.ts text eol=lf`n")
        Ok ".gitattributes created"
    }

    Write-Host ""
    if ($script:fail -gt 0) { Write-Host "  $($script:fail) problem(s) above. Nothing was pushed." -ForegroundColor Red; exit 1 }
    Write-Host "  All files are in place." -ForegroundColor Green
    exit 0
}

# =====================================================================================================
if ($Phase -eq 'tracked') {
    Write-Host ""
    Write-Host "=== Checking that git will upload the login files ===" -ForegroundColor Cyan
    foreach ($t in $targets) {
        $p = $t.Path.Replace('\', '/')
        git ls-files --error-unmatch -- $p 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { Ok "$p is tracked" }
        else {
            $why = (git check-ignore -v -- $p 2>$null)
            if ($why) { Bad "$p is IGNORED by git: $why" } else { Bad "$p is not tracked by git" }
        }
    }
    if ($script:fail -gt 0) { exit 1 }
    exit 0
}

# =====================================================================================================
if ($Phase -eq 'post') {
    Write-Host ""
    Write-Host "=== Waiting for the deploy, then asking the live Worker ===" -ForegroundColor Cyan
    $worker = 'https://porn-archive-api.satanhisham.workers.dev'
    $pages = 'https://porn-archive.pages.dev'
    $html = ReadText 'index.html'
    if ($html -match "const v = '(https://[^']+)'") { $worker = $Matches[1].TrimEnd('/') }
    $parts = Get-RepoParts
    $owner = $parts[0]; $repo = $parts[1]
    Info "Worker: $worker"
    if ($owner) { Info "Repo:   $owner/$repo" }

    $cfg = $null; $code = 0; $err = ''
    for ($i = 1; $i -le 36; $i++) {
        try {
            $cfg = Invoke-RestMethod -Method Post -Uri "$worker/api/auth/config" -ContentType 'application/json' -Body '{}' -TimeoutSec 20
            break
        } catch {
            $code = 0
            try { $code = [int]$_.Exception.Response.StatusCode } catch { }
            $err = $_.Exception.Message
        }
        Write-Host ("  ...still the old Worker or not deployed yet (try {0}/36, HTTP {1}). Next check in 10 s" -f $i, $code) -ForegroundColor DarkGray
        Start-Sleep -Seconds 10
    }
    Write-Host ""

    if (-not $cfg) {
        Bad "The Worker still answers $code on /api/auth/config, so the NEW Worker code is not live."
        Info "Your local files were checked and are correct, so the deploy is what failed or has not finished."
        if ($owner) { Show-Runs $owner $repo }
        Info "1) Open GitHub > Actions > 'Deploy Worker + Frontend'. Is the newest run green AND for your newest commit?"
        Info "2) Open the run > step 'Deploy Worker'. Send me the last 20 lines of that step."
        Info "3) Cloudflare > Workers & Pages > porn-archive-api > Deployments: is there a deployment from the last few minutes?"
        exit 1
    }

    Ok "The new Worker code is live."
    if ($cfg.enabled) {
        Ok "LOGIN IS ENABLED on the server."
        try {
            $pg = Invoke-WebRequest -Uri ("$pages/?nocache=" + (Get-Random)) -UseBasicParsing -TimeoutSec 20
            if ($pg.Content.Contains('acctBtn')) { Ok "The website already shows the Sign in button. Press Ctrl+F5 once." }
            else { Warn "The website is still the old page (Pages is still publishing). Wait a minute, then press Ctrl+F5." }
        } catch { Warn "Could not read $pages ($($_.Exception.Message))" }
        exit 0
    }

    Warn "The Worker is new, but login is still switched off. Missing:"
    foreach ($m in $cfg.missing) {
        switch ($m) {
            'DB'        { Bad "DB - the database is not connected. Cloudflare API token needs the permission  Account > D1 > Edit  (dash.cloudflare.com/profile/api-tokens, edit your token). Then run run.bat again." }
            'AUTH_SECRET' { Bad "AUTH_SECRET - GitHub repo > Settings > Secrets and variables > Actions > Secrets > New repository secret. Name: AUTH_SECRET, value: any 32+ random characters." }
            default     { Bad $m }
        }
    }
    Write-Host ""
    Info "After adding them, run run.bat again (it always redeploys, and the deploy hands the secrets to the Worker)."
    exit 1
}
