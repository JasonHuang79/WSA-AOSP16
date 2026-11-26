<#
.SYNOPSIS
    Create a Windows Start Menu shortcut that launches the native AOSP 16 Launcher3 desktop directly.
    Requires UserApp patched with `tools/patches/patch_userapp.py` (ACTION_MAIN fallback).
.EXAMPLE
    .\create_desktop_shortcut.ps1
    .\create_desktop_shortcut.ps1 -ShortcutName "WSA Android Desktop"
#>
param(
    [string]$ShortcutName = "适用于 Android™ 的 Windows 子系统的桌面",
    [string]$WsaInstallDir = ""
)

$ErrorActionPreference = "Stop"

$wsaAlias = Join-Path $env:LOCALAPPDATA "Microsoft\WindowsApps\MicrosoftCorporationII.WindowsSubsystemForAndroid_8wekyb3d8bbwe\WsaClient.exe"
if (-not (Test-Path $wsaAlias)) {
    throw "WSA AppExecutionAlias not found at: $wsaAlias"
}

# Try to reuse WSA's built-in icon
$iconPath = ""
if ($WsaInstallDir -and (Test-Path (Join-Path $WsaInstallDir "Assets\app.ico"))) {
    $iconPath = Join-Path $WsaInstallDir "Assets\app.ico"
} else {
    $pkg = Get-AppxPackage -Name "MicrosoftCorporationII.WindowsSubsystemForAndroid" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($pkg) {
        $cand = Join-Path $pkg.InstallLocation "Assets\app.ico"
        if (Test-Path $cand) { $iconPath = $cand }
    }
}

$startMenu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
$lnkPath = Join-Path $startMenu "$ShortcutName.lnk"

$shell = New-Object -ComObject WScript.Shell
$sc = $shell.CreateShortcut($lnkPath)
$sc.TargetPath = $wsaAlias
$sc.Arguments = "/launch wsa://com.android.launcher3"
$sc.WorkingDirectory = Split-Path $wsaAlias
if ($iconPath) {
    $sc.IconLocation = "$iconPath,0"
}
$sc.Description = "Launch native AOSP 16 Android Desktop in WSA"
$sc.Save()

Write-Host "[OK] Created Start Menu shortcut: $lnkPath"
if ($iconPath) { Write-Host "     Icon: $iconPath" }
