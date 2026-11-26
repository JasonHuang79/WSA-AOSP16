#!/usr/bin/env python3
"""Patch WSA UserApp.apk for AOSP 16 compatibility and native launcher shortcut support.

Patches applied to classes3.dex:
  1. @ 0x1a4240: set default of `persist.sys.disable_cache_cleanup` from false (1201) to true (1211),
     avoiding A16 SecurityException on killAllBackgroundProcesses during /launch.
  2. @ 0x1d7224: in IntentRedirectionHandler.handleLaunchPackage, when getLaunchIntentForPackage(pkg)
     returns null (e.g. Launcher3 which has CATEGORY_HOME rather than CATEGORY_LAUNCHER), fall back to
     creating `new Intent(Intent.ACTION_MAIN).setPackage(pkg)` so `WsaClient.exe /launch wsa://com.android.launcher3`
     opens the native Android desktop directly from the Windows Start Menu.

Usage:
    python3 patch_userapp.py \
      --input UserApp.apk --output UserApp.patched.apk \
      --build-tools /path/to/android-sdk/build-tools/36.1.0 \
      --key platform.pk8 --cert platform.x509.pem
"""
import argparse
import hashlib
import os
import pathlib
import struct
import subprocess
import tempfile
import zipfile
import zlib

P1_OFF = 0x1A4240
P1_ORIG = bytes([0x12, 0x01])
P1_NEW = bytes([0x12, 0x11])

P2_OFF = 0x1D7224
P2_ORIG = bytes.fromhex(
    "62023c1a2203a00c70107a7403001a0567316e20867453000c036e20867403000c036e10967403000c037120810332000f04"
)
P2_NEW = bytes.fromhex(
    "22014d00"      # new-instance v1, Intent
    "1a029a67"      # const-string v2, "android.intent.action.MAIN"
    "702010012100"  # invoke-direct {v1, v2}, Intent.<init>(String)
    "6e2031010100"  # invoke-virtual {v1, v0}, Intent.setPackage(String)
    "280f"          # goto +0x0f
) + (b"\x00\x00" * 14)


def tool_exe(bt: pathlib.Path, name: str) -> str:
    if os.name == "nt":
        for ext in (".exe", ".bat"):
            p = bt / f"{name}{ext}"
            if p.exists():
                return str(p)
    return str(bt / name)


def fix_dex(buf: bytearray) -> bytes:
    buf[12:32] = hashlib.sha1(buf[32:]).digest()
    buf[8:12] = struct.pack("<I", zlib.adler32(buf[12:]) & 0xFFFFFFFF)
    return bytes(buf)


def main():
    ap = argparse.ArgumentParser(description="Patch WSA UserApp.apk for AOSP 16")
    ap.add_argument("--input", required=True, type=pathlib.Path)
    ap.add_argument("--output", required=True, type=pathlib.Path)
    ap.add_argument("--build-tools", required=True, type=pathlib.Path)
    ap.add_argument("--key", required=True, type=pathlib.Path)
    ap.add_argument("--cert", required=True, type=pathlib.Path)
    args = ap.parse_args()

    assert len(P2_ORIG) == 50 and len(P2_NEW) == 50

    with tempfile.TemporaryDirectory() as td:
        work = pathlib.Path(td)
        unsigned = work / "unsigned.apk"
        aligned = work / "aligned.apk"
        tmp_dex = work / "classes3.dex"

        with zipfile.ZipFile(args.input, "r") as zin, zipfile.ZipFile(unsigned, "w") as zout:
            for info in zin.infolist():
                fn = info.filename
                if fn.startswith("META-INF/") and (
                    fn.endswith((".SF", ".RSA", ".DSA", ".EC")) or fn == "META-INF/MANIFEST.MF"
                ):
                    continue
                data = zin.read(fn)
                if fn == "classes3.dex":
                    buf = bytearray(data)
                    assert bytes(buf[P1_OFF : P1_OFF + 2]) == P1_ORIG, "P1 byte mismatch"
                    assert bytes(buf[P2_OFF : P2_OFF + 50]) == P2_ORIG, "P2 byte mismatch"
                    buf[P1_OFF : P1_OFF + 2] = P1_NEW
                    buf[P2_OFF : P2_OFF + 50] = P2_NEW
                    data = fix_dex(buf)
                    tmp_dex.write_bytes(data)
                    r = subprocess.run(
                        [tool_exe(args.build_tools, "dexdump"), "-d", "-f", str(tmp_dex)],
                        capture_output=True, text=True, errors="replace",
                    )
                    assert r.returncode == 0, f"dexdump failed: {r.stderr[:400]}"
                    assert "1d7224: 2201 4d00" in r.stdout and "android.intent.action.MAIN" in r.stdout
                    print("[OK] Patched UserApp classes3.dex (cache cleanup disable + ACTION_MAIN fallback)")
                ni = zipfile.ZipInfo(fn, date_time=info.date_time)
                ni.compress_type = info.compress_type
                ni.external_attr = info.external_attr
                zout.writestr(ni, data)

        subprocess.run([tool_exe(args.build_tools, "zipalign"), "-f", "-p", "4", str(unsigned), str(aligned)], check=True)
        subprocess.run(
            [
                tool_exe(args.build_tools, "apksigner"), "sign",
                "--key", str(args.key),
                "--cert", str(args.cert),
                "--min-sdk-version", "33",
                "--v1-signing-enabled", "true",
                "--v2-signing-enabled", "true",
                "--v3-signing-enabled", "true",
                "--out", str(args.output),
                str(aligned),
            ],
            check=True,
        )
        subprocess.run([tool_exe(args.build_tools, "apksigner"), "verify", str(args.output)], check=True)

    md5 = hashlib.md5(args.output.read_bytes()).hexdigest()
    print(f"[OK] Signed {args.output} ({args.output.stat().st_size:,} B, md5={md5})")


if __name__ == "__main__":
    main()
