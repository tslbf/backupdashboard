<#
.SYNOPSIS
    Update the deployed Backup Status Dashboard in place: pull, rebuild, restart.

.DESCRIPTION
    This is what the GitHub Actions self-hosted runner invokes on every push to
    the deploy branch (see .github/workflows/deploy.yml), and what you can run by
    hand to redeploy. It does NOT start uvicorn - under IIS the HttpPlatformHandler
    owns that process; restarting the app pool relaunches it.

    Steps, all idempotent:
      1. git fetch + hard-reset to origin/<branch>  (deploy box mirrors origin;
         .env and *.db are untracked/ignored, so they are never touched)
      2. create backend\.venv if missing, then pip install -r requirements.txt
      3. npm install + npm run build  (install first, or a new dep fails the build)
      4. app.cli init-db
      5. recycle the IIS app pool, then poll the health URL until it answers 200

    A non-zero exit fails the Actions run, so a bad build is visible in GitHub.

.PARAMETER RepoRoot
    Repo checkout root. Defaults to the parent of this script's folder.

.PARAMETER Branch
    Branch to deploy. Default: main.

.PARAMETER AppPoolName
    IIS app pool to recycle. Default: BackupDashboard.

.PARAMETER HealthUrl
    URL polled after restart to confirm the app is up. Default: the local
    /api/meta over https (cert validation is skipped for this loopback check).

.PARAMETER SkipAppPoolRecycle
    Build only; don't touch IIS. Used by Install-IIS.ps1 for the first build
    before the site exists.
#>
[CmdletBinding()]
param(
    [string]$RepoRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$Branch = "main",
    [string]$AppPoolName = "BackupDashboard",
    [string]$HealthUrl = "https://localhost/api/meta",
    [switch]$SkipAppPoolRecycle
)

$ErrorActionPreference = "Stop"
$Backend  = Join-Path $RepoRoot "backend"
$Frontend = Join-Path $RepoRoot "frontend"
$Py       = Join-Path $Backend ".venv\Scripts\python.exe"

function Step($m) { Write-Host "=== $m ===" -ForegroundColor Cyan }

# Find an interpreter good enough to build the venv (mirrors update-and-run.cmd:
# the py launcher first, because the Microsoft Store python.exe stub resolves
# on PATH but only opens the Store). Returns an exe + prefix-args array.
function Invoke-Candidate {
    param([string[]]$Cand, [Parameter(ValueFromRemainingArguments = $true)][string[]]$Rest)
    $exe = $Cand[0]
    # Guard the empty-tail case: $Cand[1..0] would count DOWN (1,0) and inject junk.
    $pre = @(); if ($Cand.Length -gt 1) { $pre = $Cand[1..($Cand.Length - 1)] }
    & $exe @pre @Rest
}
function Find-BootstrapPython {
    foreach ($cand in @(, @("py", "-3"), @("python"), @("python3"))) {
        try {
            Invoke-Candidate -Cand $cand -Rest @("-c", "import sys; sys.exit(0 if sys.version_info>=(3,11) else 1)") 2>$null
            if ($LASTEXITCODE -eq 0) { return , $cand }
        } catch { }
    }
    throw "No Python 3.11+ found (tried 'py -3', 'python', 'python3')."
}

if (-not (Test-Path $Backend)) { throw "No backend\ under $RepoRoot - is -RepoRoot right?" }

Step "[1/5] Pulling origin/$Branch"
# The runner service account is not whoever cloned the repo, so git's
# dubious-ownership guard would block every command below. Mark this checkout
# safe for the account running this script (idempotent - only adds if missing).
$safe = @($RepoRoot, ($RepoRoot -replace '\\', '/'))
$known = @(); try { $known = git config --global --get-all safe.directory 2>$null } catch { }
foreach ($p in $safe) { if ($known -notcontains $p) { git config --global --add safe.directory $p | Out-Null } }

git -C $RepoRoot rev-parse --is-inside-work-tree > $null 2>&1
if ($LASTEXITCODE -ne 0) { throw "$RepoRoot is not a git checkout." }
git -C $RepoRoot fetch --prune origin
if ($LASTEXITCODE -ne 0) { throw "git fetch failed." }
$before = (git -C $RepoRoot rev-parse HEAD)
# Hard reset: a deploy box mirrors origin exactly. .env (gitignored) and the
# SQLite DB (*.db, gitignored) are untracked, so reset --hard leaves them be.
git -C $RepoRoot reset --hard "origin/$Branch"
if ($LASTEXITCODE -ne 0) { throw "git reset failed." }
$after = (git -C $RepoRoot rev-parse HEAD)
Write-Host "  $before -> $after"

Step "[2/5] Python environment"
if (-not (Test-Path $Py)) {
    Write-Host "  No venv - creating..."
    $boot = Find-BootstrapPython
    Invoke-Candidate -Cand $boot -Rest @("-m", "venv", (Join-Path $Backend ".venv"))
    if ($LASTEXITCODE -ne 0) { throw "venv creation failed." }
}
# python -m pip, not bare pip: bare pip hits Access Denied under some policies.
& $Py -m pip install -q -r (Join-Path $Backend "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "pip install failed." }
Write-Host "  ok"

Step "[3/5] Frontend build"
Push-Location $Frontend
try {
    & npm install --no-audit --no-fund
    if ($LASTEXITCODE -ne 0) { throw "npm install failed." }
    & npm run build
    if ($LASTEXITCODE -ne 0) { throw "npm run build failed." }
} finally { Pop-Location }
Write-Host "  ok"

Step "[4/5] Database (init-db)"
# Non-fatal on purpose: the app calls init_db() in its startup lifespan
# (see main.py), so the schema is created/migrated when the site boots. Running
# it here is just a pre-flight, and it connects as the RUNNER's account - which
# may not have SQL rights even when the app-pool account does. A failure here
# must not sink the deploy; the health check below is the real gate.
Push-Location $Backend
try {
    & $Py -m app.cli init-db
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  WARNING: init-db failed (exit $LASTEXITCODE) - continuing." -ForegroundColor Yellow
        Write-Host "  The app initializes its own schema on startup; if the site is" -ForegroundColor Yellow
        Write-Host "  healthy below this is harmless. If it 502s, grant this runner's" -ForegroundColor Yellow
        Write-Host "  account SQL access (docs\sql\grant-access.sql)." -ForegroundColor Yellow
    } else {
        Write-Host "  ok"
    }
} finally { Pop-Location }

if ($SkipAppPoolRecycle) {
    Write-Host "  (skipping app pool recycle / health check per -SkipAppPoolRecycle)"
    Write-Host "Build complete." -ForegroundColor Green
    return
}

Step "[5/5] Recycle app pool + health check"
Import-Module WebAdministration -ErrorAction Stop
Restart-WebAppPool -Name $AppPoolName

# Loopback health check. Skip cert validation - a self-signed dev cert or a
# hostname mismatch on localhost is not what this check is about; a 200 from
# /api/meta means the app booted and reached its database.
if (-not ([System.Management.Automation.PSTypeName]'NoValidation').Type) {
    Add-Type @"
using System.Net;
using System.Security.Cryptography.X509Certificates;
public class NoValidation {
    public static bool Check(object s, X509Certificate c, X509Chain ch, System.Net.Security.SslPolicyErrors e) { return true; }
}
"@
}
[System.Net.ServicePointManager]::ServerCertificateValidationCallback = [NoValidation]::Check
[System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12

$ok = $false
foreach ($attempt in 1..30) {
    try {
        $resp = Invoke-WebRequest -Uri $HealthUrl -UseBasicParsing -TimeoutSec 5
        if ($resp.StatusCode -eq 200) { $ok = $true; break }
    } catch { }
    Start-Sleep -Seconds 2
}
if (-not $ok) {
    throw "Health check failed: $HealthUrl never returned 200 (60s). Check logs\httpplatform.log."
}
Write-Host "  healthy: $HealthUrl" -ForegroundColor Green
Write-Host ""
Write-Host "Deployed $after on $Branch." -ForegroundColor Green
