#!/usr/bin/env python3
"""Read-only VHDX -> raw disk image extractor (supports dynamic & fixed VHDX).

Parses Region Table, Metadata, and BAT (handles FULLY_PRESENT and
PARTIALLY_PRESENT blocks with sector bitmaps; zero blocks emit zero bytes).

Usage:
    python3 vhdx_extract.py <src.vhdx> <dst.raw>
"""
import struct
import sys
import uuid

GUID_BAT = "2dc27766-f623-4200-9d64-115e9bfd4a08"
GUID_METADATA = "8b7ca206-4790-4b9a-b8fe-575f050f886e"
GUID_FILE_PARAMS = "caa16737-fa36-4d43-b3b6-33f0aa44e76b"
GUID_VDISK_SIZE = "2fa54224-cd1b-4876-b211-5dbed83bf4b8"
GUID_PARENT = "a8d35f2d-b30b-454d-abf7-d3d84834ab0c"


def parse(path):
    f = open(path, "rb")
    if f.read(8) != b"vhdxfile":
        raise SystemExit(f"Not a VHDX file: {path}")
    regions = {}
    for base in (0x30000, 0x40000):
        f.seek(base)
        sig, ck, count, rsvd = struct.unpack("<4sIII", f.read(16))
        if sig != b"regi":
            continue
        for _ in range(count):
            e = f.read(32)
            g = str(uuid.UUID(bytes_le=e[0:16]))
            off, ln, req = struct.unpack("<QII", e[16:32])
            regions[g] = (off, ln, req)
        break
    if not regions:
        raise SystemExit("Region table not found")
    f.seek(regions[GUID_METADATA][0])
    tbl = f.read(regions[GUID_METADATA][1])
    if tbl[0:8] != b"metadata":
        raise SystemExit("Invalid metadata signature")
    count = struct.unpack("<H", tbl[10:12])[0]
    meta = {}
    for i in range(count):
        e = tbl[32 + i * 32 : 64 + i * 32]
        g = str(uuid.UUID(bytes_le=e[0:16]))
        io, il = struct.unpack("<II", e[16:24])
        meta[g] = tbl[io : io + il]
    bs = struct.unpack("<I", meta[GUID_FILE_PARAMS][0:4])[0]
    vds = struct.unpack("<Q", meta[GUID_VDISK_SIZE][0:8])[0]
    parent = len(meta.get(GUID_PARENT, b"")) > 0
    boff, blen, _ = regions[GUID_BAT]
    nb = (vds + bs - 1) // bs
    f.seek(boff)
    bat = struct.unpack(f"<{nb}Q", f.read(nb * 8))
    return f, bs, vds, bat, parent


def extract(src, dst):
    f, bs, vds, bat, parent = parse(src)
    if parent:
        raise SystemExit("Differencing VHDX (with parent) is not supported")
    zeros = partial = 0
    with open(dst, "wb") as o:
        for e in bat:
            st = e & 7
            if st == 6:
                f.seek(e & 0xFFFFFFFFFFF00000)
                d = f.read(bs)
                if len(d) < bs:
                    d += b"\x00" * (bs - len(d))
            elif st == 7:
                off = e & 0xFFFFFFFFFFF00000
                f.seek(off + bs)
                bitmap = f.read(bs // (4 * 1024))
                f.seek(off)
                data = bytearray(f.read(bs))
                for b in range(len(bitmap) * 8):
                    if not (bitmap[b // 8] >> (b % 8)) & 1:
                        s = b * 4096
                        data[s : s + 4096] = b"\x00" * 4096
                d = bytes(data)
                partial += 1
            else:
                zeros += 1
                d = b"\x00" * bs
            o.write(d)
        o.truncate(vds)
    f.close()
    print(f"raw={dst} bs={bs} vdisk={vds} bat={len(bat)} zeros={zeros} partial={partial}")
    return dst


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("Usage: python3 vhdx_extract.py <src.vhdx> <dst.raw>")
    extract(sys.argv[1], sys.argv[2])
