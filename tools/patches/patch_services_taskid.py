#!/usr/bin/env python3
"""Patch AOSP 16 services.jar so app pixel SurfaceControls carry METADATA_TASK_ID (3).

In AOSP 16, SurfaceFlinger does not propagate a parent Task container's METADATA_TASK_ID
down to child window pixel layers. This 30-byte in-place DEX patch in
WindowStateAnimator.createSurfaceLocked (classes3.dex @ 0x156340) calls
SurfaceControl.Builder.setMetadata(3, task.mTaskId) when creating each window surface.

Usage:
    python3 patch_services_taskid.py --input services.in.jar --output services.out.jar [--dexdump /path/to/dexdump]
"""
import argparse
import hashlib
import pathlib
import struct
import subprocess
import tempfile
import zipfile
import zlib

PATCH_OFF = 0x156340
ORIG_HEX = "54eafa1552aa260f126d6e302507d80a0c081a0aeb1e6e201e07a8000c08"
NEW_HEX = "6e106c3801000c0a380a0b0052aa8c10123d6e302507d80a0c0800000000"


def fix_dex(buf: bytearray) -> bytes:
    buf[12:32] = hashlib.sha1(buf[32:]).digest()
    buf[8:12] = struct.pack("<I", zlib.adler32(buf[12:]) & 0xFFFFFFFF)
    return bytes(buf)


def main():
    ap = argparse.ArgumentParser(description="Inject METADATA_TASK_ID into WindowStateAnimator.createSurfaceLocked")
    ap.add_argument("--input", required=True, type=pathlib.Path, help="Input services.jar")
    ap.add_argument("--output", required=True, type=pathlib.Path, help="Output patched services.jar")
    ap.add_argument("--dexdump", type=pathlib.Path, default=None, help="Optional path to dexdump for verification")
    args = ap.parse_args()

    orig = bytes.fromhex(ORIG_HEX)
    new = bytes.fromhex(NEW_HEX)
    assert len(orig) == 30 and len(new) == 30

    with zipfile.ZipFile(args.input, "r") as zin, zipfile.ZipFile(args.output, "w") as zout:
        for info in zin.infolist():
            data = zin.read(info.filename)
            if info.filename == "classes3.dex":
                buf = bytearray(data)
                actual = bytes(buf[PATCH_OFF : PATCH_OFF + 30])
                assert actual == orig, f"Byte mismatch at 0x{PATCH_OFF:x}: {actual.hex()} != {ORIG_HEX}"
                buf[PATCH_OFF : PATCH_OFF + 30] = new
                data = fix_dex(buf)
                if args.dexdump:
                    with tempfile.NamedTemporaryFile(suffix=".dex", delete=False) as tf:
                        tf.write(data)
                        tmp_path = pathlib.Path(tf.name)
                    try:
                        for flag in ("-f", "-d"):
                            r = subprocess.run([str(args.dexdump), flag, str(tmp_path)], capture_output=True, text=True)
                            assert r.returncode == 0, f"dexdump {flag} failed: {r.stderr[:400]}"
                    finally:
                        tmp_path.unlink(missing_ok=True)
                    print("[OK] dexdump -f & -d verified")
            ni = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            ni.compress_type = info.compress_type
            ni.external_attr = info.external_attr
            zout.writestr(ni, data)

    md5 = hashlib.md5(args.output.read_bytes()).hexdigest()
    print(f"[OK] Saved {args.output} ({args.output.stat().st_size:,} B, md5={md5})")


if __name__ == "__main__":
    main()
