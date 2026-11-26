#!/usr/bin/env python3
"""Patch WSA SystemApp.apk for AOSP 16 compatibility (P1-P6 NOPs) and re-sign with AOSP platform key.

Patches applied to classes.dex:
  - P1 @ 0x124d14: KeyboardSetup.waitForKeyboardReady (6B NOP)
  - P2 @ 0x0a1dec: AmazonRedirectionHandler.registerReceiver (6B NOP)
  - P3 @ 0x0b45a4: AppsRedirectionHandler.registerReceiver (6B NOP)
  - P4 @ 0x0e1146: SettingsRedirectionHandler.setDevMode (6B NOP)
  - P5 @ 0x09f0d0: NetworkManager.updateNetwork (6B NOP)
  - P6 @ 0x0df780: PermissionUsage.startWatchingNoted (6B NOP; avoids A16 6-arg OnOpNotedListener AbstractMethodError)

Usage:
    python3 patch_systemapp.py \
      --input SystemApp.apk --output SystemApp.patched.apk \
      --build-tools /path/to/android-sdk/build-tools/36.1.0 \
      --key platform.pk8 --cert platform.x509.pem
"""
import argparse
import hashlib
import os
import pathlib
import subprocess
import tempfile
import zipfile
import zlib

PATCHES = [
    ("P1 KeyboardSetup.waitForKeyboardReady", 0x124D14, "7010434f0400"),
    ("P2 AmazonRedirectionHandler.registerReceiver", 0x0A1DEC, "6e306c002100"),
    ("P3 AppsRedirectionHandler.registerReceiver", 0x0B45A4, "6e306c002100"),
    ("P4 SettingsRedirectionHandler.setDevMode", 0x0E1146, "6e40bf001243"),
    ("P5 NetworkManager.updateNetwork", 0x09F0D0, "6e5a130150a9"),
    ("P6 PermissionUsage.startWatchingNoted (A16)", 0x0DF780, "6e3042002005"),
]


def tool_exe(bt: pathlib.Path, name: str) -> str:
    if os.name == "nt":
        for ext in (".exe", ".bat"):
            p = bt / f"{name}{ext}"
            if p.exists():
                return str(p)
    return str(bt / name)


def main():
    ap = argparse.ArgumentParser(description="Patch WSA SystemApp.apk for AOSP 16")
    ap.add_argument("--input", required=True, type=pathlib.Path)
    ap.add_argument("--output", required=True, type=pathlib.Path)
    ap.add_argument("--build-tools", required=True, type=pathlib.Path)
    ap.add_argument("--key", required=True, type=pathlib.Path)
    ap.add_argument("--cert", required=True, type=pathlib.Path)
    args = ap.parse_args()

    with zipfile.ZipFile(args.input, "r") as zin:
        dex = bytearray(zin.read("classes.dex"))
        entries = [(info, zin.read(info.filename)) for info in zin.infolist()]

    for label, off, exp_hex in PATCHES:
        exp = bytes.fromhex(exp_hex)
        got = bytes(dex[off : off + 6])
        assert got == exp, f"{label} @ 0x{off:x} mismatch: got={got.hex()} exp={exp_hex}"
        dex[off : off + 6] = b"\x00" * 6
        print(f"  [PATCHED] {label} @ 0x{off:x}")

    dex[12:32] = hashlib.sha1(dex[32:]).digest()
    dex[8:12] = (zlib.adler32(dex[12:]) & 0xFFFFFFFF).to_bytes(4, "little")

    with tempfile.TemporaryDirectory() as td:
        work = pathlib.Path(td)
        dex_out = work / "classes.dex"
        dex_out.write_bytes(dex)
        r = subprocess.run([tool_exe(args.build_tools, "dexdump"), "-d", str(dex_out)], capture_output=True, text=True, errors="replace")
        assert r.returncode == 0, f"dexdump failed: {r.stderr[:400]}"

        unsigned_apk = work / "unsigned.apk"
        aligned_apk = work / "aligned.apk"
        with zipfile.ZipFile(unsigned_apk, "w") as zout:
            for info, payload in entries:
                fn = info.filename
                if fn.startswith("META-INF/") and (
                    fn.endswith((".SF", ".RSA", ".DSA", ".EC")) or fn == "META-INF/MANIFEST.MF"
                ):
                    continue
                ni = zipfile.ZipInfo(fn, date_time=info.date_time)
                ni.compress_type = info.compress_type
                ni.external_attr = info.external_attr
                zout.writestr(ni, bytes(dex) if fn == "classes.dex" else payload)

        subprocess.run([tool_exe(args.build_tools, "zipalign"), "-f", "-p", "4", str(unsigned_apk), str(aligned_apk)], check=True)
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
                str(aligned_apk),
            ],
            check=True,
        )
        subprocess.run([tool_exe(args.build_tools, "apksigner"), "verify", str(args.output)], check=True)

    md5 = hashlib.md5(args.output.read_bytes()).hexdigest()
    print(f"[OK] Signed {args.output} ({args.output.stat().st_size:,} B, md5={md5})")


if __name__ == "__main__":
    main()
