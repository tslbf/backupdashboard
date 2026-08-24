<#
.SYNOPSIS
    Install and register the GitHub Actions self-hosted runner on this Windows
    box, as a Windows service, labelled for the deploy workflow.

.DESCRIPTION
    Run ONCE from an elevated PowerShell. It downloads the runner, configures it
    against the repository with the registration token you provide, adds the
    'backupdashboard' label the workflow targets, and installs it as an
    auto-start service so deploys work whether or not anyone is logged in.

    GET THE TOKEN FIRST (it expires in ~1 hour):
        GitHub repo > Settings > Actions > Runners > New self-hosted runner
        Copy the value after `--token` in the "Configure" section.

.PARAMETER Token
    The registration token from the page above. Required.

.PARAMETER RepoUrl
    Repository URL. Default: https://github.com/tslbf/backupdashboard

.PARAMETER RunnerDir
    Where to install the runner. Default: C:\actions-runner

.PARAMETER Version
    Runner version (without the leading 'v'). Default resolves the latest
    release tag from GitHub at run time; pass a value to pin it.

.PARAMETER RunAsAccount
    Optional "DOMAIN\svc" to run the runner service as. Omit to use the default
    (NT AUTHORITY\NETWORK SERVICE). The account needs rights to run redeploy.ps1
    and to recycle the IIS app pool (i.e. it must be a local admin, or be
    granted app-pool recycle rights), so a dedicated admin service account is
    the usual choice.

.EXAMPLE
    .\Install-Runner.ps1 -Token AXXXX...  -RunAsAccount LBFOSTERCO\svc_ghrunner
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Token,
    [string]$RepoUrl = "https://github.com/tslbf/backupdashboard",
    [string]$RunnerDir = "C:\actions-runner",
    [string]$Version,
    [string]$RunAsAccount,
    [System.Security.SecureString]$RunAsPassword,
    [string]$Labels = "backupdashboard"
)

$ErrorActionPreference = "Stop"

$id = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this from an ELEVATED PowerShell (Run as administrator)."
}

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

if (-not $Version) {
    Write-Host "Resolving latest runner version..." -ForegroundColor Cyan
    $rel = Invoke-RestMethod "https://api.github.com/repos/actions/runner/releases/latest" `
        -Headers @{ "User-Agent" = "backupdashboard-setup" }
    $Version = $rel.tag_name.TrimStart("v")
    Write-Host "  latest is $Version"
}

New-Item -ItemType Directory -Force -Path $RunnerDir | Out-Null
Set-Location $RunnerDir

$zip = "actions-runner-win-x64-$Version.zip"
if (-not (Test-Path (Join-Path $RunnerDir "config.cmd"))) {
    Write-Host "Downloading $zip ..." -ForegroundColor Cyan
    Invoke-WebRequest -Uri "https://github.com/actions/runner/releases/download/v$Version/$zip" `
        -OutFile (Join-Path $RunnerDir $zip)
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [System.IO.Compression.ZipFile]::ExtractToDirectory((Join-Path $RunnerDir $zip), $RunnerDir)
    Remove-Item (Join-Path $RunnerDir $zip)
} else {
    Write-Host "Runner already extracted in $RunnerDir - reconfiguring." -ForegroundColor Yellow
}

# config.cmd registers the runner and, with --runasservice, installs the service.
$cfgArgs = @(
    "--unattended",
    "--url", $RepoUrl,
    "--token", $Token,
    "--name", "$env:COMPUTERNAME-backupdash",
    "--labels", $Labels,
    "--runasservice",
    "--replace"
)
if ($RunAsAccount) {
    if (-not $RunAsPassword) { $RunAsPassword = Read-Host -AsSecureString "Password for $RunAsAccount" }
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [Runtime.InteropServices.Marshal]::SecureStringToBSTR($RunAsPassword))
    $cfgArgs += @("--windowslogonaccount", $RunAsAccount, "--windowslogonpassword", $plain)
}

Write-Host "Configuring runner against $RepoUrl (labels: $Labels)..." -ForegroundColor Cyan
& (Join-Path $RunnerDir "config.cmd") @cfgArgs
if ($LASTEXITCODE -ne 0) { throw "Runner configuration failed (token expired? it lasts ~1h)." }

Write-Host ""
Write-Host "Runner installed as a service and listening for jobs." -ForegroundColor Green
Write-Host "It shows as 'Idle' under repo Settings > Actions > Runners."
Write-Host "Push to main (or use 'Run workflow') to trigger a deploy."
