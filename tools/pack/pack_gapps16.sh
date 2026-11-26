#!/usr/bin/env bash
# Inject official Android 16 (API 36) x86_64 Google Play / GApps (7 packages + permissions/sysconfig)
# extracted from Android SDK `system-images/android-36/google_apis_playstore/x86_64/system.img`
# into an existing AOSP 16 WSA system.vhdx.
#
# Usage (Linux / WSL2 as root):
#   sudo bash pack_gapps16.sh \
#     --in-system /path/to/system-aosp16.vhdx \
#     --sdk-image /path/to/android-36/google_apis_playstore/x86_64/system.img \
#     --out-system /path/to/system-aosp16-gapps.vhdx \
#     [--userapp /path/to/UserApp.patched.apk]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VHDX_DIR="$(cd "$SCRIPT_DIR/../vhdx" && pwd)"

IN_SYS=""
SDK_IMG=""
OUT_SYS=""
USERAPP=""
# Sector offsets for Android 16 (BP2A) google_apis_playstore x86_64 system.img super partition.
# Override via CLI flags if your SDK image revision uses different dynamic partition offsets.
P2_OFFSET=4096
PRODUCT_START=1773568
PRODUCT_SECTORS=2750520
SYSEXT_START=1525760
SYSEXT_SECTORS=247024

while [[ $# -gt 0 ]]; do
  case "$1" in
    --in-system) IN_SYS="$2"; shift 2 ;;
    --sdk-image) SDK_IMG="$2"; shift 2 ;;
    --out-system) OUT_SYS="$2"; shift 2 ;;
    --userapp) USERAPP="$2"; shift 2 ;;
    --p2-offset) P2_OFFSET="$2"; shift 2 ;;
    --product-start) PRODUCT_START="$2"; shift 2 ;;
    --product-sectors) PRODUCT_SECTORS="$2"; shift 2 ;;
    --sysext-start) SYSEXT_START="$2"; shift 2 ;;
    --sysext-sectors) SYSEXT_SECTORS="$2"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done

[[ -n "$IN_SYS" && -n "$SDK_IMG" && -n "$OUT_SYS" ]] || {
  echo "Required: --in-system, --sdk-image, --out-system" >&2
  exit 1
}

W="$(mktemp -d /tmp/wsa-gapps16.XXXXXX)"
RAW="$W/system.raw"
mkdir -p "$W/mnt_p" "$W/mnt_e"

LOOP_P="$(losetup -f --show -r -o $(( (P2_OFFSET + PRODUCT_START) * 512 )) --sizelimit $(( PRODUCT_SECTORS * 512 )) "$SDK_IMG")"
LOOP_E="$(losetup -f --show -r -o $(( (P2_OFFSET + SYSEXT_START) * 512 )) --sizelimit $(( SYSEXT_SECTORS * 512 )) "$SDK_IMG")"

cleanup() {
  umount "$W/mnt_p" 2>/dev/null || true
  umount "$W/mnt_e" 2>/dev/null || true
  losetup -d "$LOOP_P" 2>/dev/null || true
  losetup -d "$LOOP_E" 2>/dev/null || true
  rm -rf "$W"
}
trap cleanup EXIT

mount -o ro "$LOOP_P" "$W/mnt_p"
mount -o ro "$LOOP_E" "$W/mnt_e"

echo "=== [1] Extract input system.vhdx -> raw ext4 ==="
python3 "$VHDX_DIR/vhdx_extract.py" "$IN_SYS" "$RAW"

echo "=== [2] Generate debugfs injection script ==="
: > "$W/dbg.cmds"

add_dir() {
  local d="$1"
  cat >> "$W/dbg.cmds" <<EOF
mkdir $d
sif $d mode 040755
sif $d uid 0
sif $d gid 0
ea_set $d security.selinux u:object_r:system_file:s0\0
EOF
}

add_file() {
  local src="$1" dst="$2"
  [[ -f "$src" ]] || { echo "MISSING SRC: $src" >&2; exit 1; }
  cat >> "$W/dbg.cmds" <<EOF
rm $dst
write $src $dst
sif $dst mode 0100644
sif $dst uid 0
sif $dst gid 0
ea_set $dst security.selinux u:object_r:system_file:s0\0
EOF
}

if [[ -n "$USERAPP" ]]; then
  cp "$USERAPP" "$W/UserApp.apk"
  add_file "$W/UserApp.apk" "/system/system_ext/priv-app/UserApp/UserApp.apk"
fi

# Native x86_64 API 36 Google priv-apps (skipping mismatched emulator oat/ artifacts)
for app in PrebuiltGmsCore Phonesky GoogleOneTimeInitializer PartnerSetupPrebuilt ConfigUpdater; do
  add_dir "/system/product/priv-app/$app"
  add_file "$W/mnt_p/priv-app/$app/$app.apk" "/system/product/priv-app/$app/$app.apk"
done

for app in GoogleServicesFramework GoogleFeedback; do
  add_dir "/system/system_ext/priv-app/$app"
  add_file "$W/mnt_e/priv-app/$app/$app.apk" "/system/system_ext/priv-app/$app/$app.apk"
done

for f in privapp-permissions-google-p.xml privapp-permissions-sdk-google.xml split-permissions-google.xml; do
  add_file "$W/mnt_p/etc/permissions/$f" "/system/product/etc/permissions/$f"
done

for f in google.xml google_build.xml google-hiddenapi-package-whitelist.xml google-staged-installer-whitelist.xml disable-zero-touch.xml; do
  add_file "$W/mnt_p/etc/sysconfig/$f" "/system/product/etc/sysconfig/$f"
done

add_dir "/system/product/etc/default-permissions"
for f in default-permissions.xml default-permissions-sdk-google.xml; do
  add_file "$W/mnt_p/etc/default-permissions/$f" "/system/product/etc/default-permissions/$f"
done

add_file "$W/mnt_e/etc/permissions/privapp-permissions-google-se.xml" "/system/system_ext/etc/permissions/privapp-permissions-google-se.xml"

echo "=== [3] Inject into ext4 via debugfs ==="
debugfs -w -f "$W/dbg.cmds" "$RAW" 2>&1 | grep -vE '^(debugfs|rm: File not found|mkdir: Ext2 file already exists|Allocated inode)' || true

echo "=== [4] Readback MD5 verification ==="
verify_md5() {
  local src="$1" dst="$2" label="$3"
  debugfs -R "dump -p $dst $W/chk_tmp" "$RAW" 2>/dev/null
  local a b
  a="$(md5sum "$src" | awk '{print $1}')"
  b="$(md5sum "$W/chk_tmp" | awk '{print $1}')"
  echo "  $label: src=$a dst=$b"
  [[ "$a" == "$b" ]] || { echo "MD5 MISMATCH for $label!" >&2; exit 1; }
}
verify_md5 "$W/mnt_p/priv-app/PrebuiltGmsCore/PrebuiltGmsCore.apk" "/system/product/priv-app/PrebuiltGmsCore/PrebuiltGmsCore.apk" "PrebuiltGmsCore.apk"
verify_md5 "$W/mnt_p/priv-app/Phonesky/Phonesky.apk" "/system/product/priv-app/Phonesky/Phonesky.apk" "Phonesky.apk"
verify_md5 "$W/mnt_e/priv-app/GoogleServicesFramework/GoogleServicesFramework.apk" "/system/system_ext/priv-app/GoogleServicesFramework/GoogleServicesFramework.apk" "GoogleServicesFramework.apk"

echo "=== [5] Randomize UUID & e2fsck ==="
tune2fs -U random "$RAW"
e2fsck -fn "$RAW" || true

echo "=== [6] Pack output VHDX with --verify ==="
rm -f "$OUT_SYS"
python3 "$VHDX_DIR/raw2vhdx.py" --raw "$RAW" --out "$OUT_SYS" --verify
md5sum "$OUT_SYS"
