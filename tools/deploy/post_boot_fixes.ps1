<#
.SYNOPSIS
    Apply post-boot AOSP 16 runtime fixes on WSA via ADB:
      1. Disable fuse-bpf (`persist.sys.fuse.bpf.override=false`) so A16 vold bind-mounts
         /Android/data & /Android/obb cleanly on WSA's A13 kernel (eliminates FuseDaemon rejections).
      2. Disable Launcher3 SecondaryDisplayLauncher so `wsa://com.android.launcher3` launches
         QuickstepLauncher directly without an activity chooser dialog.
.EXAMPLE
    .\post_boot_fixes.ps1 -Serial "127.0.0.1:58526"
#>
param(
    [string]$AdbPath = "adb",
    [string]$Serial = "127.0.0.1:58526"
)

$ErrorActionPreference = "Stop"

Write-Host "Connecting to $Serial ..."
& $AdbPath connect $Serial
& $AdbPath -s $Serial root
Start-Sleep -Seconds 2
& $AdbPath connect $Serial

Write-Host "Setting persist.sys.fuse.bpf.override=false ..."
& $AdbPath -s $Serial shell "setprop persist.sys.fuse.bpf.override false"
$val = (& $AdbPath -s $Serial shell "getprop persist.sys.fuse.bpf.override") -join ""
Write-Host "  persist.sys.fuse.bpf.override = $($val.Trim())"

Write-Host "Disabling SecondaryDisplayLauncher ..."
& $AdbPath -s $Serial shell "pm disable com.android.launcher3/com.android.launcher3.secondarydisplay.SecondaryDisplayLauncher"

Write-Host ""
Write-Host "Done! Please shut down and restart WSA once so vold applies fuse-bpf=false during boot."
Write-Host "After reboot, verify: adb -s $Serial shell getprop ro.fuse.bpf.is_running   # expected: false"
