# Starts tunnel + server, then publishes the page to Cloudflare Pages with the new tunnel link baked in.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$Project = "porn-archive"                       # your Cloudflare Pages project name
$PagesUrl = "https://$Project.pages.dev"

pip install -r requirements.txt | Out-Null

$log = Join-Path $PSScriptRoot "tunnel.log"
Remove-Item $log -ErrorAction SilentlyContinue
Start-Process cloudflared -ArgumentList "tunnel --url http://localhost:8080 --logfile `"$log`"" -WindowStyle Minimized

Write-Host "Waiting for tunnel link..."
$url = $null
for ($i = 0; $i -lt 90 -and -not $url; $i++) {
    Start-Sleep 1
    try {
        $txt = Get-Content $log -Raw -ErrorAction Stop
        if ($txt -match 'https://[a-z0-9-]+\.trycloudflare\.com') { $url = $Matches[0] }
    } catch {}
}
if (-not $url) { Write-Host "Could not find tunnel link. Is cloudflared installed?"; Read-Host "Press Enter"; exit 1 }
Write-Host "Tunnel: $url"

New-Item -ItemType Directory static -Force | Out-Null
$html = [IO.File]::ReadAllText((Join-Path $PSScriptRoot "index.html"))
$html = $html.Replace("__API_BASE__", $url)
[IO.File]::WriteAllText((Join-Path $PSScriptRoot "static\index.html"), $html, (New-Object Text.UTF8Encoding $false))

Write-Host "Publishing page to Cloudflare Pages..."
npx --yes wrangler pages deploy static --project-name=$Project --branch=main --commit-dirty=true
Write-Host ""
Write-Host "=== READY ===  Open: $PagesUrl   (keep this window open)"

$env:ALLOWED_ORIGINS = $PagesUrl
python app.py
