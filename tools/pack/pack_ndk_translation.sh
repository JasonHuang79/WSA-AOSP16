#!/usr/bin/env bash
# Replace cross-generation A13 Intel Houdini with native Android 16 (API 36) libndk_translation (Berberis).
#
# Solves the AOSP 16 ART `ClassLinker::RegisterNative` `CHECK(native_method != nullptr)` SIGABRT
# triggered by A13 libhoudini.so when running arm64-v8a applications on AOSP 16.
#
# Usage (Linux / WSL2 as root):
#   sudo bash pack_ndk_translation.sh \
#     --in-system /path/to/system-aosp16.vhdx \
#     --in-vendor /path/to/vendor.vhdx \
#     --sdk-image /path/to/android-36/google_apis_playstore/x86_64/system.img \
#     --out-system /path/to/system-aosp16-ndk.vhdx \
#     --out-vendor /path/to/vendor-ndk.vhdx
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VHDX_DIR="$(cd "$SCRIPT_DIR/../vhdx" && pwd)"

IN_SYS=""
IN_VND=""
SDK_IMG=""
OUT_SYS=""
OUT_VND=""
P2_OFFSET=4096
SYSTEM_START=2048
SYSTEM_SECTORS=1416432
# WSA vendor.vhdx uses an 8 MiB block fixed/template layout with ext4 payload at offset 0x800000
VENDOR_PAYLOAD_OFFSET=0x800000

while [[ $# -gt 0 ]]; do
  case "$1" in
    --in-system) IN_SYS="$2"; shift 2 ;;
    --in-vendor) IN_VND="$2"; shift 2 ;;
    --sdk-image) SDK_IMG="$2"; shift 2 ;;
    --out-system) OUT_SYS="$2"; shift 2 ;;
    --out-vendor) OUT_VND="$2"; shift 2 ;;
    --p2-offset) P2_OFFSET="$2"; shift 2 ;;
    --system-start) SYSTEM_START="$2"; shift 2 ;;
    --system-sectors) SYSTEM_SECTORS="$2"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done

[[ -n "$IN_SYS" && -n "$IN_VND" && -n "$SDK_IMG" && -n "$OUT_SYS" && -n "$OUT_VND" ]] || {
  echo "Required: --in-system, --in-vendor, --sdk-image, --out-system, --out-vendor" >&2
  exit 1
}

W="$(mktemp -d /tmp/wsa-ndk16.XXXXXX)"
SYS_RAW="$W/sys.raw"
VND_RAW="$W/vnd.raw"
mkdir -p "$W/mnt_s"

LOOP_S="$(losetup -f --show -r -o $(( (P2_OFFSET + SYSTEM_START) * 512 )) --sizelimit $(( SYSTEM_SECTORS * 512 )) "$SDK_IMG")"
cleanup() {
  umount "$W/mnt_s" 2>/dev/null || true
  losetup -d "$LOOP_S" 2>/dev/null || true
  rm -rf "$W"
}
trap cleanup EXIT
mount -o ro "$LOOP_S" "$W/mnt_s"

echo "=== [1] Extract input system.vhdx & vendor.vhdx ==="
python3 "$VHDX_DIR/vhdx_extract.py" "$IN_SYS" "$SYS_RAW"
python3 "$VHDX_DIR/vhdx_extract.py" "$IN_VND" "$VND_RAW"

echo "=== [2] Patch system/build.prop & prepare 64-bit ndk_translation.rc ==="
debugfs -R "dump /system/build.prop $W/sys_build.prop" "$SYS_RAW" 2>/dev/null
python3 - "$W/sys_build.prop" "$W/sys_build.prop.new" <<'PY'
import sys
lines = open(sys.argv[1], "r", encoding="utf-8").read().splitlines()
out, seen = [], set()
repl = {
    "ro.dalvik.vm.native.bridge": "libndk_translation.so",
    "ro.system.product.cpu.abilist": "x86_64,x86,arm64-v8a",
    "ro.system.product.cpu.abilist32": "x86",
    "ro.system.product.cpu.abilist64": "x86_64,arm64-v8a",
    "ro.product.cpu.abilist": "x86_64,x86,arm64-v8a",
    "ro.product.cpu.abilist32": "x86",
    "ro.product.cpu.abilist64": "x86_64,arm64-v8a",
}
for l in lines:
    if "=" in l and not l.lstrip().startswith("#"):
        k = l.split("=", 1)[0].strip()
        if k in repl:
            out.append(f"{k}={repl[k]}")
            seen.add(k)
            continue
        if k == "ro.dalvik.vm.isa.arm":
            out.append("# " + l)
            continue
    out.append(l)
for k, v in [
    ("ro.dalvik.vm.native.bridge", "libndk_translation.so"),
    ("ro.dalvik.vm.isa.arm64", "x86_64"),
    ("ro.enable.native.bridge.exec", "1"),
]:
    if k not in seen:
        out.append(f"{k}={v}")
open(sys.argv[2], "w", encoding="utf-8", newline="\n").write("\n".join(out) + "\n")
PY

cat > "$W/ndk_translation.rc" <<'EOF'
# Enable native bridge for arm64 target executables (Android 16 libndk_translation / Berberis)
on early-init && property:ro.enable.native.bridge.exec=1
    mount binfmt_misc binfmt_misc /proc/sys/fs/binfmt_misc

on property:ro.enable.native.bridge.exec=1 && property:ro.dalvik.vm.isa.arm64=x86_64
    copy /system/etc/binfmt_misc/arm64_exe /proc/sys/fs/binfmt_misc/register
    copy /system/etc/binfmt_misc/arm64_dyn /proc/sys/fs/binfmt_misc/register
EOF

echo "=== [3] Inject Berberis/libndk_translation into system.raw ==="
: > "$W/sys_dbg.cmds"
add_dir() {
  cat >> "$W/sys_dbg.cmds" <<EOF
mkdir $1
sif $1 mode $2
sif $1 uid $3
sif $1 gid $4
ea_set $1 security.selinux $5\0
EOF
}
add_file() {
  [[ -f "$1" ]] || { echo "MISSING: $1" >&2; exit 1; }
  cat >> "$W/sys_dbg.cmds" <<EOF
rm $2
write $1 $2
sif $2 mode $3
sif $2 uid $4
sif $2 gid $5
ea_set $2 security.selinux $6\0
EOF
}

# Remove legacy Houdini symlink at /system/lib64/arm64 before creating real directory
echo "rm /system/lib64/arm64" >> "$W/sys_dbg.cmds"
add_dir "/system/lib64/arm64" 040755 0 0 "u:object_r:system_lib_file:s0"
for f in "$W/mnt_s/system/lib64/arm64/"*.so; do
  add_file "$f" "/system/lib64/arm64/$(basename "$f")" 0100644 0 0 "u:object_r:system_lib_file:s0"
done

for f in "$W/mnt_s/system/lib64/libndk_translation.so" \
         "$W/mnt_s/system/lib64/libberberis_exec_region.so" \
         "$W/mnt_s/system/lib64/libndk_translation_proxy_"*.so; do
  add_file "$f" "/system/lib64/$(basename "$f")" 0100644 0 0 "u:object_r:system_lib_file:s0"
done

add_dir "/system/bin/arm64" 040751 0 2000 "u:object_r:system_file:s0"
add_file "$W/mnt_s/system/bin/arm64/linker64" "/system/bin/arm64/linker64" 0100755 0 2000 "u:object_r:system_file:s0"
add_file "$W/mnt_s/system/bin/arm64/app_process64" "/system/bin/arm64/app_process64" 0100755 0 2000 "u:object_r:system_file:s0"
add_file "$W/mnt_s/system/bin/ndk_translation_program_runner_binfmt_misc_arm64" "/system/bin/ndk_translation_program_runner_binfmt_misc_arm64" 0100755 0 2000 "u:object_r:system_file:s0"

add_dir "/system/etc/binfmt_misc" 040755 0 0 "u:object_r:system_file:s0"
add_file "$W/mnt_s/system/etc/binfmt_misc/arm64_exe" "/system/etc/binfmt_misc/arm64_exe" 0100644 0 0 "u:object_r:system_file:s0"
add_file "$W/mnt_s/system/etc/binfmt_misc/arm64_dyn" "/system/etc/binfmt_misc/arm64_dyn" 0100644 0 0 "u:object_r:system_file:s0"
add_file "$W/ndk_translation.rc" "/system/etc/init/ndk_translation.rc" 0100644 0 0 "u:object_r:system_file:s0"
add_file "$W/mnt_s/system/etc/ld.config.arm64.txt" "/system/etc/ld.config.arm64.txt" 0100644 0 0 "u:object_r:system_linker_config_file:s0"
add_file "$W/sys_build.prop.new" "/system/build.prop" 0100644 0 0 "u:object_r:system_file:s0"

debugfs -w -f "$W/sys_dbg.cmds" "$SYS_RAW" 2>&1 | grep -vE '^(debugfs|rm: File not found|mkdir: Ext2 file already exists|Allocated inode)' || true

echo "=== [4] Patch vendor/build.prop to disable A13 Houdini ==="
debugfs -R "dump /build.prop $W/vnd_build.prop" "$VND_RAW" 2>/dev/null
python3 - "$W/vnd_build.prop" "$W/vnd_build.prop.new" <<'PY'
import sys
lines = open(sys.argv[1], "r", encoding="utf-8").read().splitlines()
out = []
repl = {
    "ro.dalvik.vm.native.bridge": "libndk_translation.so",
    "ro.vendor.enable.native.bridge.exec": "0",
    "ro.vendor.enable.native.bridge.exec64": "0",
    "ro.vendor.product.cpu.abilist": "x86_64,x86,arm64-v8a",
    "ro.vendor.product.cpu.abilist32": "x86",
    "ro.vendor.product.cpu.abilist64": "x86_64,arm64-v8a",
}
for l in lines:
    if "=" in l and not l.lstrip().startswith("#"):
        k = l.split("=", 1)[0].strip()
        if k in repl:
            out.append(f"{k}={repl[k]}")
            continue
        if k == "ro.dalvik.vm.isa.arm":
            out.append("# " + l)
            continue
    out.append(l)
open(sys.argv[2], "w", encoding="utf-8", newline="\n").write("\n".join(out) + "\n")
PY

debugfs -w "$VND_RAW" <<EOF 2>&1 | grep -vE '^(debugfs|Allocated inode)' || true
rm /build.prop
write $W/vnd_build.prop.new /build.prop
sif /build.prop mode 0100644
sif /build.prop uid 0
sif /build.prop gid 0
ea_set /build.prop security.selinux u:object_r:vendor_file:s0\0
EOF

echo "=== [5] Re-UUID, e2fsck & pack system/vendor VHDX ==="
tune2fs -U random "$SYS_RAW"
e2fsck -fn "$SYS_RAW" || true
e2fsck -fn "$VND_RAW" || true

rm -f "$OUT_SYS" "$OUT_VND"
python3 "$VHDX_DIR/raw2vhdx.py" --raw "$SYS_RAW" --out "$OUT_SYS" --verify

# Repack vendor.vhdx preserving the 8 MiB VHDX template layout
cp -f "$IN_VND" "$W/vendor-out.vhdx"
python3 - "$VND_RAW" "$W/vendor-out.vhdx" "$VENDOR_PAYLOAD_OFFSET" <<'PY'
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

python3 "$VHDX_DIR/vhdx_extract.py" "$W/vendor-out.vhdx" "$W/vendor-verify.raw"
[[ "$(md5sum "$VND_RAW" | awk '{print $1}')" == "$(md5sum "$W/vendor-verify.raw" | awk '{print $1}')" ]] || {
  echo "vendor.vhdx readback MD5 mismatch!" >&2
  exit 1
}
cp -f "$W/vendor-out.vhdx" "$OUT_VND"
md5sum "$OUT_SYS" "$OUT_VND"
