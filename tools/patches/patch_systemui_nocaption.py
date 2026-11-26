#!/usr/bin/env python3
"""Disable AOSP 16 freeform CaptionWindowDecorViewModel in SystemUI.apk (eliminates double title bar on WSA).

Patches classes2.dex @ 0x573ca4 (CaptionWindowDecorViewModel.shouldShowWindowDecor$1):
  orig: 52 60 16 00 ... (iget v0, v6, RunningTaskInfo.displayId)
  new : 12 00 0f 00     (const/4 v0, 0 ; return v0)

Usage:
    python3 patch_systemui_nocaption.py \
      --input SystemUI.in.apk --output SystemUI.out.apk \
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

OFF = 0x573CA4
ORIG = bytes.fromhex("52601600545111aa")
PATCH = bytes.fromhex("12000f00")


def tool_exe(bt: pathlib.Path, name: str) -> str:
    if os.name == "nt":
        for ext in (".exe", ".bat"):
            p = bt / f"{name}{ext}"
            if p.exists():
                return str(p)
    return str(bt / name)


def main():
    ap = argparse.ArgumentParser(description="Remove AOSP 16 duplicate freeform caption bar in SystemUI.apk")
    ap.add_argument("--input", required=True, type=pathlib.Path)
    ap.add_argument("--output", required=True, type=pathlib.Path)
    ap.add_argument("--build-tools", required=True, type=pathlib.Path)
    ap.add_argument("--key", required=True, type=pathlib.Path, help="AOSP platform.pk8")
    ap.add_argument("--cert", required=True, type=pathlib.Path, help="AOSP platform.x509.pem")
    args = ap.parse_args()

    with zipfile.ZipFile(args.input, "r") as zin:
        c2 = bytearray(zin.read("classes2.dex"))

    actual = bytes(c2[OFF : OFF + len(ORIG)])
    assert actual == ORIG, f"Byte mismatch at 0x{OFF:x}: {actual.hex()} != {ORIG.hex()}"
    c2[OFF : OFF + len(PATCH)] = PATCH
    c2[12:32] = hashlib.sha1(c2[32:]).digest()
    c2[8:12] = (zlib.adler32(c2[12:]) & 0xFFFFFFFF).to_bytes(4, "little")

    with tempfile.TemporaryDirectory() as td:
        work = pathlib.Path(td)
        c2_path = work / "classes2.dex"
        c2_path.write_bytes(c2)

        r = subprocess.run(
            [tool_exe(args.build_tools, "dexdump"), "-d", str(c2_path)],
            capture_output=True, text=True, errors="replace", check=True,
        )
        idx = r.stdout.find("CaptionWindowDecorViewModel.shouldShowWindowDecor$1:")
        assert idx != -1, "Target method not found in dexdump output"
        snippet = "\n".join(r.stdout[idx : idx + 500].splitlines()[:8])
        assert "0000: const/4 v0, #int 0" in snippet and "0001: return v0" in snippet
        print("[OK] Verified patched CaptionWindowDecorViewModel.shouldShowWindowDecor$1")

        unsigned_apk = work / "unsigned.apk"
        aligned_apk = work / "aligned.apk"
        with zipfile.ZipFile(args.input, "r") as zin, zipfile.ZipFile(unsigned_apk, "w") as zout:
            for info in zin.infolist():
                fn = info.filename
                if fn.startswith("META-INF/") and (
                    fn.endswith((".SF", ".RSA", ".DSA", ".EC")) or fn == "META-INF/MANIFEST.MF"
                ):
                    continue
                ni = zipfile.ZipInfo(fn, date_time=info.date_time)
                ni.compress_type = info.compress_type
                ni.external_attr = info.external_attr
                zout.writestr(ni, bytes(c2) if fn == "classes2.dex" else zin.read(fn))

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
