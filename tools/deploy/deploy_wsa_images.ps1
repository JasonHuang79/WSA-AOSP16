<#
.SYNOPSIS
    Safely deploy patched AOSP 16 system.vhdx and/or vendor.vhdx into an installed WSA directory.
.EXAMPLE
    .\deploy_wsa_images.ps1 -WsaDir "D:\WSA" -SystemVhdx ".\system-aosp16.vhdx" -VendorVhdx ".\vendor-aosp16.vhdx"
#>
param(
    [Parameter(Mandatory = $true)]
    [string]$WsaDir,
    [string]$SystemVhdx,
    [string]$VendorVhdx,
    [string]$BackupSuffix = ".bak"
)

$ErrorActionPreference = "Stop"

$wsaAlias = Join-Path $env:LOCALAPPDATA "Microsoft\WindowsApps\MicrosoftCorporationII.WindowsSubsystemForAndroid_8wekyb3d8bbwe\WsaClient.exe"

Write-Host "=== [1] Stopping WSA ==="
if (Test-Path $wsaAlias) {
    Start-Process -FilePath $wsaAlias -ArgumentList "/shutdown" -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 4
}
Get-Process WsaClient, vmmemWSA, WsaService, WsaSettings -ErrorAction SilentlyContinue |
    Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 3
if (Get-Process WsaClient, vmmemWSA -ErrorAction SilentlyContinue) {
    throw "WSA is still running; please shut it down from WSA Settings first."
}

function Deploy-Image([string]$Src, [string]$Dst) {
    if (-not (Test-Path $Src)) { throw "Source image not found: $Src" }
    $bak = "$Dst$BackupSuffix"
    if ((Test-Path $Dst) -and -not (Test-Path $bak)) {
        Write-Host "  Backing up $Dst -> $bak"
        Copy-Item $Dst $bak -Force
    }
    $srcHash = (Get-FileHash -Algorithm MD5 $Src).Hash.ToLower()
    Write-Host "  Copying $Src -> $Dst"
    Copy-Item $Src $Dst -Force
    $dstHash = (Get-FileHash -Algorithm MD5 $Dst).Hash.ToLower()
    if ($srcHash -ne $dstHash) {
        throw "MD5 mismatch after copy! src=$srcHash dst=$dstHash"
    }
    Write-Host "  [OK] MD5 verified: $dstHash"
}

Write-Host "=== [2] Deploying images ==="
if ($SystemVhdx) { Deploy-Image $SystemVhdx (Join-Path $WsaDir "system.vhdx") }
if ($VendorVhdx) { Deploy-Image $VendorVhdx (Join-Path $WsaDir "vendor.vhdx") }

Write-Host "=== [3] Deployment complete ==="
Write-Host "Launch WSA from the Windows Start Menu or via: WsaClient.exe /launch wsa://com.android.settings"
