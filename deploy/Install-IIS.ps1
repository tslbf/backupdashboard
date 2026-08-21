<#
.SYNOPSIS
    Stand the Backup Status Dashboard up as an IIS site, hosted by
    HttpPlatformHandler, on HTTPS/443.

.DESCRIPTION
    Run ONCE (per box) from an elevated PowerShell. It is idempotent - safe to
    re-run to change the binding, cert or identity.

    What it does:
      * verifies IIS + the HttpPlatformHandler module are present
      * ensures the venv / frontend build exist (runs redeploy.ps1 if not)
      * creates an app pool with No Managed Code, and - crucially - keeps it
        ALWAYS RUNNING with no idle timeout and no periodic recycle, so the
        in-process 08:00 collector/digest and hourly rollups fire even with no
        web traffic (HttpPlatformHandler ties the Python process to this pool)
      * creates the site with its physical path pointing at backend\ (config.py
        loads .env / the SQLite dev DB by a relative path, so CWD must be there)
      * binds HTTPS/443 to the certificate you name
      * grants the app-pool identity the file rights it needs

    Day-to-day updates are redeploy.ps1 (driven by the GitHub Actions runner);
    this script is only the one-time stand-up.

.PARAMETER RepoRoot
    Repo checkout root. Defaults to the parent of this script's folder, so if
    you cloned to C:\AIProjects\BackupsDashboard and this file is at
    ...\BackupsDashboard\deploy\Install-IIS.ps1, it resolves on its own.

.PARAMETER CertThumbprint
    Thumbprint of the TLS cert in LocalMachine\My to bind on 443. Find it with:
        Get-ChildItem Cert:\LocalMachine\My | Format-Table Subject, Thumbprint
    Omit to have the script create a self-signed cert for the hostname (fine
    for a firewalled box you reach by name; swap for a real cert later by
    re-running with -CertThumbprint).

.PARAMETER HostHeader
    Host header for the 443 binding. Empty (default) binds all hostnames on 443.

.PARAMETER ServiceAccount
    "DOMAIN\svc_backupdash" to run the pool (and therefore the app) as. This
    account is the one whose DPAPI-protected secrets in .env must have been
    created (see docs) and the one that needs SQL rights. Omit to use the
    built-in ApplicationPoolIdentity (then protect secrets with `protect
    --machine`, or the app can't decrypt them).

.PARAMETER ServiceAccountPassword
    Password for -ServiceAccount, as a SecureString. Prompted for if the
    account is given without it.

.EXAMPLE
    .\Install-IIS.ps1 -CertThumbprint AABBCC... -ServiceAccount LBFOSTERCO\svc_backupdash
#>
[CmdletBinding()]
param(
    [string]$SiteName = "BackupDashboard",
    [string]$AppPoolName = "BackupDashboard",
    [string]$RepoRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$CertThumbprint,
    [string]$HostHeader = "",
    [int]$Port = 443,
    [string]$ServiceAccount,
    [System.Security.SecureString]$ServiceAccountPassword,

    # --- Where IIS physically points -----------------------------------------
    # HttpPlatformHandler runs the app with its working directory set to the
    # site's physical path, and config.py loads .env by a RELATIVE path, so that
    # path must resolve to the backend\ folder. Two supported ways:
    #
    #   (default) repoint: set the site's physical path straight at
    #             <RepoRoot>\backend. Simplest; the site "is" the app.
    #
    #   -UseJunction: keep your existing physical path (e.g. the inetpub one you
    #             already set up) and make it a directory junction to
    #             <RepoRoot>\backend. IIS sees the inetpub path; the real content
    #             is backend\. Use this to preserve a C:\inetpub\AI-Sites\ layout.
    [switch]$UseJunction,
    [string]$JunctionPath = "C:\inetpub\AI-Sites\backups"
)

$ErrorActionPreference = "Stop"

function Assert-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $p = New-Object Security.Principal.WindowsPrincipal($id)
    if (-not $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Run this from an ELEVATED PowerShell (Run as administrator)."
    }
}

Assert-Admin

$Backend  = Join-Path $RepoRoot "backend"
$Logs     = Join-Path $RepoRoot "logs"
$WebConfig = Join-Path $Backend "web.config"

if (-not (Test-Path $Backend))   { throw "No backend\ under $RepoRoot - is -RepoRoot right?" }
if (-not (Test-Path $WebConfig)) { throw "No backend\web.config - pull the latest branch first." }

Write-Host "=== [1/7] Checking IIS and HttpPlatformHandler ===" -ForegroundColor Cyan
Import-Module WebAdministration -ErrorAction Stop
# HttpPlatformHandler registers a global module by this name once installed.
$hph = (Get-WebGlobalModule -ErrorAction SilentlyContinue | Where-Object Name -eq "httpPlatformHandler")
if (-not $hph) {
    throw @"
HttpPlatformHandler is not installed. It is a separate download, not part of
IIS itself. Install it, then re-run:
  https://www.iis.net/downloads/microsoft/httpplatformhandler
(Or via Web Platform Installer / your package mirror. It is a ~1MB MSI.)
"@
}
Write-Host "  ok"

Write-Host "=== [2/7] Ensuring venv + frontend build exist ===" -ForegroundColor Cyan
if (-not (Test-Path (Join-Path $Backend ".venv\Scripts\python.exe")) -or
    -not (Test-Path (Join-Path $RepoRoot "frontend\dist\index.html"))) {
    Write-Host "  Building via redeploy.ps1 (first-time build)..."
    & (Join-Path $PSScriptRoot "redeploy.ps1") -RepoRoot $RepoRoot -SkipAppPoolRecycle
} else {
    Write-Host "  present"
}
New-Item -ItemType Directory -Force -Path $Logs | Out-Null

Write-Host "=== [3/7] App pool ($AppPoolName) ===" -ForegroundColor Cyan
if (-not (Test-Path "IIS:\AppPools\$AppPoolName")) {
    New-WebAppPool -Name $AppPoolName | Out-Null
}
# No managed code - this pool never runs .NET, only spawns python.
Set-ItemProperty "IIS:\AppPools\$AppPoolName" -Name managedRuntimeVersion -Value ""
# Keep the worker (and thus the Python process and its APScheduler) alive
# without traffic. These three are what make an 8am scheduled collect reliable.
Set-ItemProperty "IIS:\AppPools\$AppPoolName" -Name startMode -Value "AlwaysRunning"
Set-ItemProperty "IIS:\AppPools\$AppPoolName" -Name processModel.idleTimeout -Value ([TimeSpan]::Zero)
Set-ItemProperty "IIS:\AppPools\$AppPoolName" -Name recycling.periodicRestart.time -Value ([TimeSpan]::Zero)

if ($ServiceAccount) {
    if (-not $ServiceAccountPassword) {
        $ServiceAccountPassword = Read-Host -AsSecureString "Password for $ServiceAccount"
    }
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [Runtime.InteropServices.Marshal]::SecureStringToBSTR($ServiceAccountPassword))
    Set-ItemProperty "IIS:\AppPools\$AppPoolName" -Name processModel.identityType -Value SpecificUser
    Set-ItemProperty "IIS:\AppPools\$AppPoolName" -Name processModel.userName -Value $ServiceAccount
    Set-ItemProperty "IIS:\AppPools\$AppPoolName" -Name processModel.password -Value $plain
    $identityRef = $ServiceAccount
    Write-Host "  identity: $ServiceAccount"
} else {
    Set-ItemProperty "IIS:\AppPools\$AppPoolName" -Name processModel.identityType -Value ApplicationPoolIdentity
    $identityRef = "IIS AppPool\$AppPoolName"
    Write-Host "  identity: ApplicationPoolIdentity ($identityRef)" -ForegroundColor Yellow
    Write-Host "  NOTE: DPAPI secrets in .env must be created with 'protect --machine'" -ForegroundColor Yellow
    Write-Host "        for this identity to decrypt them." -ForegroundColor Yellow
}

# Resolve the physical path IIS will use (see the -UseJunction note above).
if ($UseJunction) {
    $needLink = $true
    if (Test-Path $JunctionPath) {
        $item = Get-Item $JunctionPath -Force
        $tgt = ($item.Target | Select-Object -First 1)
        if ($item.LinkType -eq "Junction" -and $tgt -eq $Backend) {
            $needLink = $false
        } elseif ((Get-ChildItem $JunctionPath -Force | Measure-Object).Count -eq 0) {
            Remove-Item $JunctionPath -Force
        } else {
            throw "$JunctionPath exists, is not the expected junction, and is not empty. Move its contents aside, then re-run."
        }
    }
    if ($needLink) {
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $JunctionPath) | Out-Null
        & cmd /c mklink /J "$JunctionPath" "$Backend" | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "mklink /J $JunctionPath -> $Backend failed." }
    }
    $PhysPath = $JunctionPath
} else {
    $PhysPath = $Backend
}

Write-Host "=== [4/7] Site ($SiteName) -> $PhysPath ===" -ForegroundColor Cyan
$siteExists = Test-Path "IIS:\Sites\$SiteName"
if ($siteExists) {
    # Adopt the site you already made: only repoint it at the app and pool.
    Set-ItemProperty "IIS:\Sites\$SiteName" -Name physicalPath -Value $PhysPath
    Set-ItemProperty "IIS:\Sites\$SiteName" -Name applicationPool -Value $AppPoolName
    Write-Host "  adopted existing site"
} else {
    New-Website -Name $SiteName -PhysicalPath $PhysPath -ApplicationPool $AppPoolName `
        -Port $Port -Ssl -HostHeader $HostHeader | Out-Null
    Write-Host "  created site"
}
# preload so IIS starts the worker (and the app) at boot, not on first request.
Set-ItemProperty "IIS:\Sites\$SiteName" -Name applicationDefaults.preloadEnabled -Value $true -ErrorAction SilentlyContinue

Write-Host "=== [5/7] TLS certificate on $Port ===" -ForegroundColor Cyan
$existingHttps = Get-WebBinding -Name $SiteName -Protocol "https" -ErrorAction SilentlyContinue
if ($existingHttps -and -not $CertThumbprint) {
    # You already bound a cert for the hostname; leave it. Pass -CertThumbprint
    # only if you want to replace it.
    Write-Host "  existing https binding kept (pass -CertThumbprint to replace)"
} else {
    if (-not $CertThumbprint) {
        $dns = if ($HostHeader) { $HostHeader } else { $env:COMPUTERNAME }
        Write-Host "  No -CertThumbprint given; creating a self-signed cert for '$dns'." -ForegroundColor Yellow
        $cert = New-SelfSignedCertificate -DnsName $dns -CertStoreLocation "Cert:\LocalMachine\My"
        $CertThumbprint = $cert.Thumbprint
    }
    $binding = $existingHttps
    if (-not $binding) {
        New-WebBinding -Name $SiteName -Protocol "https" -Port $Port -HostHeader $HostHeader | Out-Null
        $binding = Get-WebBinding -Name $SiteName -Protocol "https"
    }
    $binding.AddSslCertificate($CertThumbprint, "My")
    Write-Host "  bound cert $CertThumbprint"
}

Write-Host "=== [6/7] File permissions for $identityRef ===" -ForegroundColor Cyan
# Read/execute on the tree; write on logs\ (stdout log) and backend\ (SQLite
# dev DB, if used). SQL Server prod needs no write here - grant that in SQL.
icacls "$RepoRoot" /grant "${identityRef}:(OI)(CI)RX" /T /C /Q | Out-Null
icacls "$Logs"     /grant "${identityRef}:(OI)(CI)M"  /T /C /Q | Out-Null
icacls "$Backend"  /grant "${identityRef}:(OI)(CI)M"  /C /Q | Out-Null
Write-Host "  granted"

Write-Host "=== [7/7] Starting ===" -ForegroundColor Cyan
Restart-WebAppPool -Name $AppPoolName
Start-Website -Name $SiteName -ErrorAction SilentlyContinue

$scheme = "https"
$hostForUrl = if ($HostHeader) { $HostHeader } else { "localhost" }
Write-Host ""
Write-Host "Done. Give it a few seconds to boot, then:" -ForegroundColor Green
Write-Host "    $scheme`://$hostForUrl/  (health: $scheme`://$hostForUrl/api/meta)"
Write-Host ""
Write-Host "If it 502s: check $Logs\httpplatform.log - that's uvicorn's stdout."
Write-Host "IM002 there means ODBC Driver 18 is missing or APP_DB_URL is the placeholder."
