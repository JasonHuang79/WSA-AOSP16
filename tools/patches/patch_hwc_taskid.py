#!/usr/bin/env python3
"""Patch WSA vendor hwcomposer.windows.so (x86_64) for AOSP 16 SurfaceFlinger TaskId passthrough.

Background:
  AOSP 16 SurfaceFlinger forwards METADATA_TASK_ID using the HWC 2.4 GenericMetadata key
  "org.chromium.arc.V1_0.TaskId" (28 bytes) only after querying getLayerGenericMetadataKey().
  Stock WSA 2407 hwcomposer.windows.so reports 0 generic metadata keys and only recognizes
  "com.microsoft.wsa.windowtype" (also 28 bytes), causing every layer's mTaskId to stay 0.

Patches applied (exact byte-verified against WSA 2407.40000.4.0 x64 hwcomposer.windows.so):
  1. .rodata @ 0xdeaa: replace "com.microsoft.wsa.windowtype" -> "org.chromium.arc.V1_0.TaskId" (28B)
  2. Layer::setLayerGenericMetadata @ 0x2954b & 0x2955c: target Layer::mTaskId (+0x8c) instead of +0x90
  3. @ 0x29700 (unused Layer::isValidWindowType, 48B): write HWC2_PFN_GET_LAYER_GENERIC_METADATA_KEY
     returning "org.chromium.arc.V1_0.TaskId" for keyIndex == 0
  4. Device::getFunction(70) @ 0x195b6: redirect returned function pointer to 0x29700

Usage:
    python3 patch_hwc_taskid.py --input hwcomposer.windows.so --output hwcomposer.windows.patched.so
"""
import argparse
import hashlib
import pathlib
import struct


def va_ro(va: int) -> int:
    return va


def va_tx(va: int) -> int:
    return va - 0x1000


def patch_hwcomposer(src_path: pathlib.Path, out_path: pathlib.Path) -> None:
    buf = bytearray(src_path.read_bytes())

    # [Patch 1] 28-byte in-place string replacement
    old_str = b"com.microsoft.wsa.windowtype"
    new_str = b"org.chromium.arc.V1_0.TaskId"
    assert len(old_str) == 28 and len(new_str) == 28
    off_str = va_ro(0xDEAA)
    assert bytes(buf[off_str : off_str + 28]) == old_str, (
        f"Unexpected .rodata string at 0x{off_str:x}: {buf[off_str:off_str+28]!r}"
    )
    buf[off_str : off_str + 28] = new_str
    print(f"[OK] Patch 1: .rodata @ 0x{off_str:x}: {old_str.decode()} -> {new_str.decode()}")

    # [Patch 2] Redirect 28-byte metadata branch write offset from +0x90 (mWindowType) to +0x8c (mTaskId)
    off_cmp = va_tx(0x2954B)
    off_mov = va_tx(0x2955C)
    assert bytes(buf[off_cmp : off_cmp + 7]) == bytes.fromhex("41398f90000000"), "Unexpected cmp opcode"
    assert bytes(buf[off_mov : off_mov + 7]) == bytes.fromhex("41898f90000000"), "Unexpected mov opcode"
    buf[off_cmp + 3] = 0x8C
    buf[off_mov + 3] = 0x8C
    print("[OK] Patch 2: setLayerGenericMetadata @ 0x2954b & 0x2955c: +0x90 -> +0x8c (mTaskId)")

    # [Patch 3] Emit getLayerGenericMetadataKey() implementation @ 0x29700 (48 bytes)
    off_fn = va_tx(0x29700)
    assert bytes(buf[off_fn : off_fn + 6]) == bytes.fromhex("8d4fffb00181"), "Unexpected prologue at 0x29700"
    disp_str = 0xDEAA - 0x29722
    code = bytearray()
    code += bytes.fromhex("4d85c0")                                # test r8, r8
    code += bytes.fromhex("7404")                                  # je +4
    code += bytes.fromhex("41c60000")                              # mov byte ptr [r8], 0
    code += bytes.fromhex("31c0")                                  # xor eax, eax
    code += bytes.fromhex("85f6")                                  # test esi, esi
    code += bytes.fromhex("751a")                                  # jne ret_len
    code += bytes.fromhex("b01c")                                  # mov al, 28
    code += bytes.fromhex("4885c9")                                # test rcx, rcx
    code += bytes.fromhex("7413")                                  # je ret_len
    code += bytes.fromhex("3b02")                                  # cmp eax, dword ptr [rdx]
    code += bytes.fromhex("0f4702")                                # cmova eax, dword ptr [rdx]
    code += bytes.fromhex("488d35") + struct.pack("<i", disp_str)  # lea rsi, [rip + disp_str]
    code += bytes.fromhex("4889cf")                                # mov rdi, rcx
    code += bytes.fromhex("89c1")                                  # mov ecx, eax
    code += bytes.fromhex("f3a4")                                  # rep movsb
    code += bytes.fromhex("8902")                                  # mov dword ptr [rdx], eax
    code += bytes.fromhex("c3")                                    # ret
    code += bytes.fromhex("cccccccc")                              # int3 pad
    assert len(code) == 48
    buf[off_fn : off_fn + 48] = code
    print("[OK] Patch 3: Wrote getLayerGenericMetadataKey @ 0x29700")

    # [Patch 4] Redirect Device::getFunction(70) @ 0x195b6 -> 0x29700
    off_lea = va_tx(0x195B6)
    assert bytes(buf[off_lea : off_lea + 7]) == bytes.fromhex("4c8d35d3000000"), "Unexpected lea at 0x195b6"
    disp_fn = 0x29700 - 0x195BD
    buf[off_lea : off_lea + 7] = bytes.fromhex("4c8d35") + struct.pack("<i", disp_fn)
    print(f"[OK] Patch 4: Device::getFunction(70) @ 0x195b6 -> 0x29700 (disp={hex(disp_fn)})")

    out_path.write_bytes(bytes(buf))
    print(f"[OK] Saved {out_path} ({out_path.stat().st_size:,} B, md5={hashlib.md5(bytes(buf)).hexdigest()})")


def main():
    ap = argparse.ArgumentParser(description="Patch WSA hwcomposer.windows.so for AOSP 16 TaskId passthrough")
    ap.add_argument("--input", required=True, type=pathlib.Path, help="Stock WSA hwcomposer.windows.so")
    ap.add_argument("--output", required=True, type=pathlib.Path, help="Output patched .so")
    args = ap.parse_args()
    patch_hwcomposer(args.input, args.output)


if __name__ == "__main__":
    main()
