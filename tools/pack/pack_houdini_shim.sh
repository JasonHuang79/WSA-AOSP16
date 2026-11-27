#!/usr/bin/env bash
# Pack Intel Houdini 14.0.0 NativeBridge v8 shim (`libhoudini_a16_shim.so`)
# and patched SystemApp.apk into WSA AOSP 16 system.vhdx & vendor.vhdx.
#
# Usage (Linux / WSL2 as root):
#   sudo bash pack_houdini_shim.sh \
#     --in-system /path/to/system-aosp16-gapps.vhdx \
#     --in-vendor /path/to/vendor-hwc-patched.vhdx \
#     --shim64 /path/to/libhoudini_a16_shim_64.so \
#     --shim32 /path/to/libhoudini_a16_shim_32.so \
#     --out-system /path/to/system-aosp16-houdini-shim.vhdx \
#     --out-vendor /path/to/vendor-houdini-shim.vhdx \
#     [--systemapp /path/to/SystemApp.patched.apk]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VHDX_DIR="$(cd "$SCRIPT_DIR/../vhdx" && pwd)"

IN_SYS=""
IN_VND=""
SHIM64=""
SHIM32=""
OUT_SYS=""
OUT_VND=""
SYSAPP=""
VENDOR_PAYLOAD_OFFSET=0x800000

while [[ $# -gt 0 ]]; do
  case "$1" in
    --in-system) IN_SYS="$2"; shift 2 ;;
    --in-vendor) IN_VND="$2"; shift 2 ;;
    --shim64) SHIM64="$2"; shift 2 ;;
    --shim32) SHIM32="$2"; shift 2 ;;
    --out-system) OUT_SYS="$2"; shift 2 ;;
    --out-vendor) OUT_VND="$2"; shift 2 ;;
    --systemapp) SYSAPP="$2"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done

[[ -n "$IN_SYS" && -n "$IN_VND" && -n "$SHIM64" && -n "$SHIM32" && -n "$OUT_SYS" && -n "$OUT_VND" ]] || {
  echo "Required: --in-system, --in-vendor, --shim64, --shim32, --out-system, --out-vendor" >&2
  exit 1
}

W="$(mktemp -d /tmp/wsa-houdini-shim.XXXXXX)"
trap 'rm -rf "$W"' EXIT

cp -f "$SHIM64" "$W/shim64.so"
cp -f "$SHIM32" "$W/shim32.so"
[[ -z "$SYSAPP" ]] || cp -f "$SYSAPP" "$W/SystemApp.apk"

echo "=== [1] Patch system.vhdx ==="
python3 "$VHDX_DIR/vhdx_extract.py" "$IN_SYS" "$W/sys.raw"
debugfs -R "dump /system/build.prop $W/sys.prop" "$W/sys.raw" 2>/dev/null

python3 - "$W/sys.prop" "$W/sys.prop.new" <<'PY'
import sys
lines = open(sys.argv[1], "r", encoding="utf-8").read().splitlines()
out, found = [], False
for l in lines:
    if "=" in l and not l.lstrip().startswith("#") and l.split("=", 1)[0].strip() == "ro.dalvik.vm.native.bridge":
        out.append("ro.dalvik.vm.native.bridge=libhoudini_a16_shim.so")
        found = True
        continue
    out.append(l)
if not found:
    out.append("ro.dalvik.vm.native.bridge=libhoudini_a16_shim.so")
open(sys.argv[2], "w", encoding="utf-8", newline="\n").write("\n".join(out) + "\n")
PY

cat > "$W/sys.cmds" <<EOF
rm /system/build.prop
write $W/sys.prop.new /system/build.prop
sif /system/build.prop mode 0100644
sif /system/build.prop uid 0
sif /system/build.prop gid 0
ea_set /system/build.prop security.selinux u:object_r:system_file:s0\0
EOF

if [[ -n "$SYSAPP" ]]; then
  cat >> "$W/sys.cmds" <<EOF
rm /system/system_ext/priv-app/SystemApp/SystemApp.apk
write $W/SystemApp.apk /system/system_ext/priv-app/SystemApp/SystemApp.apk
sif /system/system_ext/priv-app/SystemApp/SystemApp.apk mode 0100644
sif /system/system_ext/priv-app/SystemApp/SystemApp.apk uid 0
sif /system/system_ext/priv-app/SystemApp/SystemApp.apk gid 0
ea_set /system/system_ext/priv-app/SystemApp/SystemApp.apk security.selinux u:object_r:system_file:s0\0
EOF
fi

debugfs -w -f "$W/sys.cmds" "$W/sys.raw" 2>&1 | grep -vE '^(debugfs|Allocated inode)' || true
tune2fs -U random "$W/sys.raw"
e2fsck -fn "$W/sys.raw" || true
rm -f "$OUT_SYS"
python3 "$VHDX_DIR/raw2vhdx.py" --raw "$W/sys.raw" --out "$OUT_SYS" --verify
rm -f "$W/sys.raw"

echo "=== [2] Patch vendor.vhdx ==="
python3 "$VHDX_DIR/vhdx_extract.py" "$IN_VND" "$W/vnd.raw"
debugfs -R "dump /build.prop $W/vnd.prop" "$W/vnd.raw" 2>/dev/null

python3 - "$W/vnd.prop" "$W/vnd.prop.new" <<'PY'
import sys
lines = open(sys.argv[1], "r", encoding="utf-8").read().splitlines()
out, found = [], False
for l in lines:
    if "=" in l and not l.lstrip().startswith("#") and l.split("=", 1)[0].strip() == "ro.dalvik.vm.native.bridge":
        out.append("ro.dalvik.vm.native.bridge=libhoudini_a16_shim.so")
        found = True
        continue
    out.append(l)
if not found:
    out.append("ro.dalvik.vm.native.bridge=libhoudini_a16_shim.so")
open(sys.argv[2], "w", encoding="utf-8", newline="\n").write("\n".join(out) + "\n")
PY

cat > "$W/vnd.cmds" <<EOF
rm /lib64/libhoudini_a16_shim.so
write $W/shim64.so /lib64/libhoudini_a16_shim.so
sif /lib64/libhoudini_a16_shim.so mode 0100644
sif /lib64/libhoudini_a16_shim.so uid 0
sif /lib64/libhoudini_a16_shim.so gid 0
ea_set /lib64/libhoudini_a16_shim.so security.selinux u:object_r:same_process_hal_file:s0\0
rm /lib/libhoudini_a16_shim.so
write $W/shim32.so /lib/libhoudini_a16_shim.so
sif /lib/libhoudini_a16_shim.so mode 0100644
sif /lib/libhoudini_a16_shim.so uid 0
sif /lib/libhoudini_a16_shim.so gid 0
ea_set /lib/libhoudini_a16_shim.so security.selinux u:object_r:same_process_hal_file:s0\0
rm /build.prop
write $W/vnd.prop.new /build.prop
sif /build.prop mode 0100644
sif /build.prop uid 0
sif /build.prop gid 0
ea_set /build.prop security.selinux u:object_r:vendor_file:s0\0
EOF

debugfs -w -f "$W/vnd.cmds" "$W/vnd.raw" 2>&1 | grep -vE '^(debugfs|rm: File not found|Allocated inode)' || true
e2fsck -fn "$W/vnd.raw" || true

cp -f "$IN_VND" "$W/vendor-out.vhdx"
python3 - "$W/vnd.raw" "$W/vendor-out.vhdx" "$VENDOR_PAYLOAD_OFFSET" <<'PY'
import os, sys
raw, vv, off = sys.argv[1], sys.argv[2], int(sys.argv[3], 16)
rs, vs = os.path.getsize(raw), os.path.getsize(vv)
assert off + rs <= vs, f"vendor raw ({rs}) exceeds VHDX template payload space"
with open(raw, "rb") as f, open(vv, "r+b") as g:
    g.seek(off)
    left = rs
    while left:
        chunk = f.read(min(1 << 20, left))
        if not chunk:
            break
        g.write(chunk)
        left -= len(chunk)
PY

python3 "$VHDX_DIR/vhdx_extract.py" "$W/vendor-out.vhdx" "$W/vnd-verify.raw"
[[ "$(md5sum "$W/vnd.raw" | awk '{print $1}')" == "$(md5sum "$W/vnd-verify.raw" | awk '{print $1}')" ]] || {
  echo "vendor.vhdx readback MD5 mismatch!" >&2
  exit 1
}
cp -f "$W/vendor-out.vhdx" "$OUT_VND"
md5sum "$OUT_SYS" "$OUT_VND"
