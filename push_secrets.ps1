# Pushes values from .env to Fly.io secrets.
# Usage:  .\push_secrets.ps1
$ErrorActionPreference = "Stop"

if (-not (Test-Path ".env")) {
    Write-Host ".env not found. Run the script from C:\dev\slot-watcher" -ForegroundColor Red
    exit 1
}

$pairs = @()
foreach ($line in Get-Content ".env") {
    $clean = ($line -replace "^\uFEFF", "").Trim()
    if ($clean -match '^#') { continue }
    if ($clean -match '^[A-Za-z_][A-Za-z0-9_]*\s*=\s*\S') { $pairs += $clean }
}

if ($pairs.Count -eq 0) {
    Write-Host ".env has no non-empty values." -ForegroundColor Red
    exit 1
}

Write-Host "Sending these secrets to Fly (values hidden):" -ForegroundColor Cyan
foreach ($pair in $pairs) {
    Write-Host ("  " + $pair.Split("=")[0])
}
Write-Host ""

$flyArgs = @("secrets", "set", "--stage") + $pairs
& fly @flyArgs
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "fly secrets set failed (exit code $LASTEXITCODE)." -ForegroundColor Red
    Write-Host "Check your login: fly auth whoami" -ForegroundColor Yellow
    exit $LASTEXITCODE
}

Write-Host ""
Write-Host "Secrets staged. Deploying..." -ForegroundColor Cyan
& fly deploy
if ($LASTEXITCODE -ne 0) {
    Write-Host "fly deploy failed (exit code $LASTEXITCODE)." -ForegroundColor Red
    exit $LASTEXITCODE
}

Write-Host ""
Write-Host "Current secrets:" -ForegroundColor Green
& fly secrets list

Write-Host ""
Write-Host "Next, seed the monitors:" -ForegroundColor Cyan
Write-Host "  fly ssh console -C ""python seed_afvictoria.py"""
Write-Host "  then open https://slot-watcher.fly.dev  (login: admin)"
