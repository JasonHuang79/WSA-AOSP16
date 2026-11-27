#!/usr/bin/env python3
"""Patch AOSP 16 /system/bin/surfaceflinger so LayerGenericMetadata (including
org.chromium.arc.V1_0.TaskId / METADATA_TASK_ID) inherits recursively from
parent Task/Window containers down to all child layers (SurfaceView, BLAST,
VideoView, dialogs, popups).

Root cause:
  In AOSP 16 LayerSnapshotBuilder::updateMetadata (inlined in 3 paths of
  LayerSnapshotBuilder::updateSnapshot), the generic-metadata loop looks up
  keys ONLY in `requested.metadata` (the single layer's own un-inherited map),
  and only merges `parentSnapshot.layerMetadata` into `snapshot.layerMetadata`
  (+0x4e8) afterward when `args.includeMetadata` is true. Consequently, child
  SurfaceView(BLAST) layers (used by Unity games & video players) never receive
  METADATA_TASK_ID and are dropped by WSA's hwcomposer.windows.so.

Fix:
  1. In a small code cave at 0x79f900 (inside the existing R-X LOAD segment's
     alignment padding, zero section shifts), add a register-preserving helper
     that computes:
         snapshot.layerMetadata = parentSnapshot.layerMetadata;
         snapshot.layerMetadata.merge(requested.metadata, false);
     before the generic-metadata loop on all 3 paths.
  2. Redirect all 3 generic-metadata map lookups from `&requested.metadata.mMap`
     (+0x148) to `&snapshot.layerMetadata.mMap` (+0x4f0).
  3. Skip the redundant post-loop `includeMetadata` merge on all 3 paths.

Usage:
    python3 patch_sf_metadata_inherit.py \
        --input surfaceflinger \
        --output surfaceflinger.patched

    # then, on the device (root shell):
    #   adb push surfaceflinger.patched /system/bin/surfaceflinger
    #   adb shell chmod 0755 /system/bin/surfaceflinger && adb reboot

The script is fully generic: it takes any AOSP 16 x86_64 surfaceflinger binary,
verifies every patch site byte-for-byte before writing, and refuses to touch a
binary that does not match the expected baseline (no silent corruption).

Target baseline: AOSP 16 BP2A.250605.031.A2 x86_64 /system/bin/surfaceflinger.
"""
import argparse
import hashlib
import pathlib
import struct

CAVE_VMA = 0x79F900
PLT_LM_ASSIGN = 0x79CC60  # android::gui::LayerMetadata::operator=(const LayerMetadata&)
PLT_LM_MERGE = 0x79CDB0   # android::gui::LayerMetadata::merge(const LayerMetadata&, bool)


def rel32(src_next: int, dst: int) -> bytes:
    return struct.pack("<i", dst - src_next)


def call_rel32(addr: int, target: int) -> bytes:
    return b"\xe8" + rel32(addr + 5, target)


def jmp_rel32(addr: int, target: int) -> bytes:
    return b"\xe9" + rel32(addr + 5, target)


def build_cave(base: int):
    buf = bytearray()

    def cur():
        return base + len(buf)

    # 1) merge_meta_helper(rdi=snapshot, rsi=parentSnapshot, rdx=req_meta)
    #    Preserves ALL caller-saved + callee-saved registers!
    merge_helper = cur()
    # Save 11 regs (+ retaddr = 12*8 = 96B -> 16-byte aligned!)
    buf += bytes([
        0x53,                   # push %rbx
        0x41, 0x56,             # push %r14
        0x50,                   # push %rax
        0x51,                   # push %rcx
        0x52,                   # push %rdx
        0x56,                   # push %rsi
        0x57,                   # push %rdi
        0x41, 0x50,             # push %r8
        0x41, 0x51,             # push %r9
        0x41, 0x52,             # push %r10
        0x41, 0x53,             # push %r11
        0x48, 0x85, 0xF6,       # test %rsi, %rsi
        0x74, 0x26,             # jz .done (patched below)
    ])
    jz_off = len(buf) - 1
    # rbx = &snapshot->layerMetadata (rdi + 0x4e8)
    buf += b"\x48\x8d\x9f\xe8\x04\x00\x00"  # leaq 0x4e8(%rdi), %rbx
    # r14 = req_meta (rdx)
    buf += b"\x49\x89\xd6"                  # movq %rdx, %r14
    # rsi = &parentSnapshot->layerMetadata (rsi + 0x4e8)
    buf += b"\x48\x81\xc6\xe8\x04\x00\x00"  # addq $0x4e8, %rsi
    # rdi = rbx
    buf += b"\x48\x89\xdf"                  # movq %rbx, %rdi
    buf += call_rel32(cur(), PLT_LM_ASSIGN)
    # rdi = rbx, rsi = r14, edx = 0
    buf += b"\x48\x89\xdf"                  # movq %rbx, %rdi
    buf += b"\x4c\x89\xf6"                  # movq %r14, %rsi
    buf += b"\x31\xd2"                      # xorl %edx, %edx
    buf += call_rel32(cur(), PLT_LM_MERGE)
    done_off = len(buf)
    buf[jz_off] = done_off - (jz_off + 1)
    buf += bytes([
        0x41, 0x5B,             # pop %r11
        0x41, 0x5A,             # pop %r10
        0x41, 0x59,             # pop %r9
        0x41, 0x58,             # pop %r8
        0x5F,                   # pop %rdi
        0x5E,                   # pop %rsi
        0x5A,                   # pop %rdx
        0x59,                   # pop %rcx
        0x58,                   # pop %rax
        0x41, 0x5E,             # pop %r14
        0x5B,                   # pop %rbx
        0xC3,                   # retq
    ])

    # 2) path1_tramp: called from 0x1a3702
    #    Original: leaq 0x1d8(%r12), %rbx
    #    Args: r12=snapshot, r11=parentSnapshot, -0x170(%rbp)=req_meta
    path1_tramp = cur()
    buf += b"\x49\x8d\x9c\x24\xd8\x01\x00\x00"  # leaq 0x1d8(%r12), %rbx
    buf += b"\x57\x56\x52"                      # push rdi, rsi, rdx (3 + ret = 4*8 = 32B aligned)
    buf += b"\x4c\x89\xe7"                      # movq %r12, %rdi
    buf += b"\x4c\x89\xde"                      # movq %r11, %rsi
    buf += b"\x48\x8b\x95\x90\xfe\xff\xff"      # movq -0x170(%rbp), %rdx
    buf += call_rel32(cur(), merge_helper)
    buf += b"\x5a\x5e\x5f\xc3"                  # pop rdx, rsi, rdi; retq

    # 3) path2_tramp: called from 0x1a3872
    #    Original: leaq 0x1d8(%r12), %r14
    #    Args: r12=snapshot, rbx=parentSnapshot, -0x158(%rbp)=req_meta
    path2_tramp = cur()
    buf += b"\x4d\x8d\xb4\x24\xd8\x01\x00\x00"  # leaq 0x1d8(%r12), %r14
    buf += b"\x57\x56\x52"                      # push rdi, rsi, rdx
    buf += b"\x4c\x89\xe7"                      # movq %r12, %rdi
    buf += b"\x48\x89\xde"                      # movq %rbx, %rsi
    buf += b"\x48\x8b\x95\xa8\xfe\xff\xff"      # movq -0x158(%rbp), %rdx
    buf += call_rel32(cur(), merge_helper)
    buf += b"\x5a\x5e\x5f\xc3"                  # pop rdx, rsi, rdi; retq

    # 4) path2_map_tramp: called from 0x1a5caa (replaces `addq $0x148, -0x150(%rbp)`)
    #    Stores &snapshot->layerMetadata.mMap (r12 + 0x4f0) into -0x150(%rbp)
    path2_map_tramp = cur()
    buf += b"\x50"                              # push %rax
    buf += b"\x49\x8d\x84\x24\xf0\x04\x00\x00"  # leaq 0x4f0(%r12), %rax
    buf += b"\x48\x89\x85\xb0\xfe\xff\xff"      # movq %rax, -0x150(%rbp)
    buf += b"\x58\xc3"                          # pop %rax; retq

    # 5) path3_tramp: called from 0x1a548a
    #    Original: leaq 0x1d8(%r12), %r14
    #    Args: r12=snapshot, -0x148(%rbp)=parentSnapshot, -0x170(%rbp)=req_meta
    path3_tramp = cur()
    buf += b"\x4d\x8d\xb4\x24\xd8\x01\x00\x00"  # leaq 0x1d8(%r12), %r14
    buf += b"\x57\x56\x52"                      # push rdi, rsi, rdx
    buf += b"\x4c\x89\xe7"                      # movq %r12, %rdi
    buf += b"\x48\x8b\xb5\xb8\xfe\xff\xff"      # movq -0x148(%rbp), %rsi
    buf += b"\x48\x8b\x95\x90\xfe\xff\xff"      # movq -0x170(%rbp), %rdx
    buf += call_rel32(cur(), merge_helper)
    buf += b"\x5a\x5e\x5f\xc3"                  # pop rdx, rsi, rdi; retq

    return bytes(buf), {
        "merge_helper": merge_helper,
        "path1_tramp": path1_tramp,
        "path2_tramp": path2_tramp,
        "path2_map_tramp": path2_map_tramp,
        "path3_tramp": path3_tramp,
    }


def patch_bytes(data: bytearray, off: int, expected: bytes, replacement: bytes, label: str):
    assert len(expected) == len(replacement), f"{label}: length mismatch"
    got = bytes(data[off : off + len(expected)])
    assert got == expected, f"{label} @ 0x{off:x} mismatch:\n  got={got.hex()}\n  exp={expected.hex()}"
    data[off : off + len(expected)] = replacement
    print(f"  [PATCHED] {label} @ 0x{off:x} ({len(replacement)}B)")


def main():
    ap = argparse.ArgumentParser(description="Patch AOSP 16 surfaceflinger for recursive METADATA_TASK_ID inheritance")
    ap.add_argument("--input", required=True, type=pathlib.Path)
    ap.add_argument("--output", required=True, type=pathlib.Path)
    args = ap.parse_args()

    data = bytearray(args.input.read_bytes())

    # 1. Verify ELF64 header & expand R-X PT_LOAD (vma=0x198000, filesz=0x6078c0) by 0x200 to cover 0x79f900..0x79fac0
    assert data[:4] == b"\x7fELF" and data[4] == 2, "Not ELF64"
    e_phoff = struct.unpack_from("<Q", data, 32)[0]
    e_phentsize, e_phnum = struct.unpack_from("<HH", data, 54)
    expanded = False
    for i in range(e_phnum):
        ph = e_phoff + i * e_phentsize
        p_type, p_flags, p_offset, p_vaddr, p_paddr, p_filesz, p_memsz, p_align = struct.unpack_from(
            "<IIQQQQQQ", data, ph
        )
        if p_type == 1 and p_vaddr == 0x198000 and p_filesz == 0x6078C0 and p_memsz == 0x6078C0:
            new_sz = 0x607AC0  # covers up to 0x79fac0 (< 0x7a0000 page boundary!)
            struct.pack_into("<QQ", data, ph + 32, new_sz, new_sz)
            expanded = True
            print(f"  [PT_LOAD] Expanded R-X segment filesz/memsz: 0x{p_filesz:x} -> 0x{new_sz:x}")
            break
    assert expanded, "Expected R-X PT_LOAD (0x198000, 0x6078c0) not found!"

    # 2. Verify cave at 0x79f900..0x79fac0 is all zeros and write helper + trampolines
    cave_bytes, syms = build_cave(CAVE_VMA)
    assert len(cave_bytes) <= 0x1C0, f"Cave overflow: {len(cave_bytes)}B"
    assert all(b == 0 for b in data[CAVE_VMA : CAVE_VMA + len(cave_bytes)]), "Cave region not zero!"
    data[CAVE_VMA : CAVE_VMA + len(cave_bytes)] = cave_bytes
    print(f"  [CAVE] Wrote {len(cave_bytes)}B at 0x{CAVE_VMA:x}: " + ", ".join(f"{k}=0x{v:x}" for k, v in syms.items()))

    # 3. Path 1 patches
    patch_bytes(
        data, 0x1A3702,
        bytes.fromhex("498d9c24d8010000"),
        call_rel32(0x1A3702, syms["path1_tramp"]) + b"\x90" * 3,
        "Path1 pre-merge hook",
    )
    patch_bytes(
        data, 0x1A5E70,
        bytes.fromhex("488b85b0feffff480548010000"),
        bytes.fromhex("498d8424f0040000") + b"\x90" * 5,
        "Path1 lookup snapshot->layerMetadata.mMap (+0x4f0)",
    )
    patch_bytes(
        data, 0x1A373E,
        bytes.fromhex("0f84d9260000"),
        b"\x90" * 6,
        "Path1 skip redundant post-loop merge",
    )

    # 4. Path 2 patches
    patch_bytes(
        data, 0x1A3872,
        bytes.fromhex("4d8db424d8010000"),
        call_rel32(0x1A3872, syms["path2_tramp"]) + b"\x90" * 3,
        "Path2 pre-merge hook",
    )
    patch_bytes(
        data, 0x1A5CAA,
        bytes.fromhex("488185b0feffff48010000"),
        call_rel32(0x1A5CAA, syms["path2_map_tramp"]) + b"\x90" * 6,
        "Path2 lookup snapshot->layerMetadata.mMap (+0x4f0)",
    )
    patch_bytes(
        data, 0x1A38A4,
        bytes.fromhex("0f85dceaffff"),
        jmp_rel32(0x1A38A4, 0x1A2386) + b"\x90",
        "Path2 skip redundant post-loop merge",
    )

    # 5. Path 3 patches
    patch_bytes(
        data, 0x1A548A,
        bytes.fromhex("4d8db424d8010000"),
        call_rel32(0x1A548A, syms["path3_tramp"]) + b"\x90" * 3,
        "Path3 pre-merge hook",
    )
    patch_bytes(
        data, 0x1A657C,
        bytes.fromhex("488b85b0feffff480548010000"),
        bytes.fromhex("488d83f0040000") + b"\x90" * 6,
        "Path3 lookup snapshot->layerMetadata.mMap (+0x4f0)",
    )
    patch_bytes(
        data, 0x1A54E3,
        bytes.fromhex("0f85d9c9ffff"),
        jmp_rel32(0x1A54E3, 0x1A1EC2) + b"\x90",
        "Path3 skip redundant post-loop merge",
    )

    args.output.write_bytes(data)
    md5 = hashlib.md5(data).hexdigest()
    print(f"[OK] Wrote {args.output} ({len(data):,} B, md5={md5})")


if __name__ == "__main__":
    main()
