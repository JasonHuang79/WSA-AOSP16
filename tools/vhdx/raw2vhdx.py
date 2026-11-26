#!/usr/bin/env python3
"""Convert a raw ext4 image to a dynamic VHDX (16 MiB blocks) with optional readback MD5 verification.

Layout matches stock WSA system.vhdx:
  - 0x00000  vhdxfile signature + creator (UTF-16LE)
  - 0x10000  File Header #1 (4 KiB, CRC32C)
  - 0x20000  File Header #2 (4 KiB, CRC32C)
  - 0x30000  Region Table #1 (64 KiB, CRC32C)
  - 0x40000  Region Table #2 (64 KiB, CRC32C)
  - 0x100000 Log region (1 MiB, unused/zeroed)
  - 0x200000 BAT (1 MiB aligned)
  - 0x300000 Metadata (1 MiB aligned)
  - 0x400000 Payload blocks (16 MiB each; all-zero blocks stored as PAYLOAD_BLOCK_ZERO)

Usage:
    python3 raw2vhdx.py --raw system.raw --out system.vhdx [--verify]
"""
import argparse
import hashlib
import os
import struct
import sys
import uuid

BLOCK_SIZE = 16 * 1024 * 1024
SECTOR = 512
LOG_START = 0x100000
LOG_LEN = 1 * 1024 * 1024
BAT_START = 0x200000
META_START = 0x300000
DATA_START = 0x400000
MIB = 1024 * 1024

GUID_BAT = "2dc27766-f623-4200-9d64-115e9bfd4a08"
GUID_METADATA = "8b7ca206-4790-4b9a-b8fe-575f050f886e"
GUID_FILE_PARAMS = "caa16737-fa36-4d43-b3b6-33f0aa44e76b"
GUID_VDISK_SIZE = "2fa54224-cd1b-4876-b211-5dbed83bf4b8"
GUID_PAGE83 = "beca12ab-b2e6-4523-93ef-c309e000c746"
GUID_LOGICAL_SECTOR = "8141bf1d-a96f-4709-ba47-f233a8faab5f"
GUID_PHYSICAL_SECTOR = "cda348c7-445d-4471-9cc9-e9885251c556"

_CRC_TABLE = []


def _init_crc_table():
    poly = 0x82F63B78
    for i in range(256):
        c = i
        for _ in range(8):
            c = (c >> 1) ^ poly if c & 1 else c >> 1
        _CRC_TABLE.append(c)


def crc32c(data, crc=0xFFFFFFFF):
    if not _CRC_TABLE:
        _init_crc_table()
    c = crc
    tbl = _CRC_TABLE
    for b in data:
        c = tbl[(c ^ b) & 0xFF] ^ (c >> 8)
    return c ^ 0xFFFFFFFF


def guid_bytes(s):
    return uuid.UUID(s).bytes_le


def build_header(seq, file_guid, data_guid):
    buf = bytearray(4096)
    struct.pack_into("<4s", buf, 0, b"head")
    struct.pack_into("<I", buf, 4, 0)
    struct.pack_into("<Q", buf, 8, seq)
    buf[16:32] = file_guid
    buf[32:48] = data_guid
    buf[48:64] = b"\x00" * 16
    struct.pack_into("<H", buf, 64, 0)
    struct.pack_into("<H", buf, 66, 1)
    struct.pack_into("<I", buf, 68, LOG_LEN)
    struct.pack_into("<Q", buf, 72, LOG_START)
    struct.pack_into("<I", buf, 4, crc32c(bytes(buf)))
    return bytes(buf)


def build_region_table(entries):
    buf = bytearray(64 * 1024)
    struct.pack_into("<4s", buf, 0, b"regi")
    struct.pack_into("<I", buf, 4, 0)
    struct.pack_into("<I", buf, 8, len(entries))
    struct.pack_into("<I", buf, 12, 0)
    for i, (g, off, ln, req) in enumerate(entries):
        base = 16 + i * 32
        buf[base : base + 16] = guid_bytes(g)
        struct.pack_into("<Q", buf, base + 16, off)
        struct.pack_into("<I", buf, base + 24, ln)
        struct.pack_into("<I", buf, base + 28, req)
    struct.pack_into("<I", buf, 4, crc32c(bytes(buf)))
    return bytes(buf)


def build_metadata(vdisk_size, block_size):
    region = bytearray(MIB)
    struct.pack_into("<8s", region, 0, b"metadata")
    struct.pack_into("<H", region, 8, 0)
    struct.pack_into("<H", region, 10, 5)
    base = 0x10000
    fp = struct.pack("<II", block_size, 0)
    vs = struct.pack("<Q", vdisk_size)
    p83 = uuid.uuid5(uuid.NAMESPACE_OID, f"aosp16-patched-{vdisk_size}").bytes_le
    ls = struct.pack("<I", SECTOR)
    ps = struct.pack("<I", SECTOR)
    items = [
        (GUID_FILE_PARAMS, fp, False, False, True),
        (GUID_VDISK_SIZE, vs, False, True, True),
        (GUID_PAGE83, p83, False, False, False),
        (GUID_LOGICAL_SECTOR, ls, False, True, True),
        (GUID_PHYSICAL_SECTOR, ps, False, True, True),
    ]
    off = base
    for i, (g, blob, is_user, is_vdisk, is_req) in enumerate(items):
        e = 32 + i * 32
        region[e : e + 16] = guid_bytes(g)
        struct.pack_into("<I", region, e + 16, off)
        struct.pack_into("<I", region, e + 20, len(blob))
        flags = (1 if is_user else 0) | (2 if is_vdisk else 0) | (4 if is_req else 0)
        struct.pack_into("<I", region, e + 24, flags)
        struct.pack_into("<I", region, e + 28, 0)
        region[off : off + len(blob)] = blob
        off += len(blob)
    return bytes(region)


def make_vhdx(raw_path, out_path, log=print):
    size = os.path.getsize(raw_path)
    if size % BLOCK_SIZE != 0:
        raise SystemExit(f"Raw size {size} is not a multiple of {BLOCK_SIZE}")
    nblocks = size // BLOCK_SIZE

    zero = b"\x00" * BLOCK_SIZE
    states = []
    with open(raw_path, "rb") as f:
        for i in range(nblocks):
            d = f.read(BLOCK_SIZE)
            if len(d) != BLOCK_SIZE:
                raise SystemExit(f"Short read at block {i}")
            states.append(2 if d == zero else 6)
    present = sum(1 for s in states if s == 6)
    log(f"  blocks={nblocks} zero={nblocks - present} present={present}")

    bat_bytes = nblocks * 8
    bat_region_len = ((bat_bytes + MIB - 1) // MIB) * MIB

    bat = []
    data_off = DATA_START
    assign = {}
    for i, st in enumerate(states):
        if st == 6:
            assign[i] = data_off
            bat.append(data_off | 6)
            data_off += BLOCK_SIZE
        else:
            bat.append(2)

    file_guid = uuid.uuid5(uuid.NAMESPACE_OID, "wsa-aosp16-file").bytes_le
    data_guid = uuid.uuid5(uuid.NAMESPACE_OID, f"wsa-aosp16-raw-{size}").bytes_le

    with open(out_path, "wb") as o:
        lead = bytearray(64 * 1024)
        lead[0:8] = b"vhdxfile"
        creator = "wsa-aosp16 raw2vhdx".encode("utf-16-le")
        lead[8 : 8 + len(creator)] = creator
        o.write(lead)
        o.seek(LOG_START)
        o.write(b"\x00" * LOG_LEN)
        o.seek(0x10000)
        o.write(build_header(1, file_guid, data_guid))
        o.seek(0x20000)
        o.write(build_header(2, file_guid, data_guid))
        entries = [
            (GUID_BAT, BAT_START, bat_region_len, 0),
            (GUID_METADATA, META_START, MIB, 0),
        ]
        o.seek(0x30000)
        o.write(build_region_table(entries))
        o.seek(0x40000)
        o.write(build_region_table(entries))
        o.seek(BAT_START)
        o.write(struct.pack(f"<{nblocks}Q", *bat))
        o.seek(META_START)
        o.write(build_metadata(size, BLOCK_SIZE))
        with open(raw_path, "rb") as f:
            for i in range(nblocks):
                if states[i] != 6:
                    continue
                f.seek(i * BLOCK_SIZE)
                d = f.read(BLOCK_SIZE)
                if len(d) != BLOCK_SIZE:
                    raise SystemExit(f"Short read at payload block {i}")
                o.seek(assign[i])
                o.write(d)
        o.truncate(data_off)
    log(f"  wrote {out_path} ({os.path.getsize(out_path):,} bytes)")
    return {"block_size": BLOCK_SIZE, "vdisk_size": size, "blocks": nblocks, "present": present}


def verify_vhdx(vhdx_path, raw_path, log=print):
    with open(vhdx_path, "rb") as f:
        if f.read(8) != b"vhdxfile":
            raise SystemExit("Not a VHDX file")
        f.seek(0x30000)
        rt = f.read(64 * 1024)
        assert rt[0:4] == b"regi"
        stored_ck = struct.unpack("<I", rt[4:8])[0]
        chk = bytearray(rt)
        chk[4:8] = b"\x00" * 4
        if crc32c(bytes(chk)) != stored_ck:
            raise SystemExit("Region table CRC32C mismatch")
        count = struct.unpack("<I", rt[8:12])[0]
        regions = {}
        for i in range(count):
            base = 16 + i * 32
            g = str(uuid.UUID(bytes_le=rt[base : base + 16]))
            off, ln = struct.unpack("<QI", rt[base + 16 : base + 28])
            regions[g] = (off, ln)
        f.seek(0x10000)
        h = bytearray(f.read(4096))
        hck = struct.unpack("<I", h[4:8])[0]
        h[4:8] = b"\x00" * 4
        if crc32c(bytes(h)) != hck or h[0:4] != b"head":
            raise SystemExit("File header CRC32C or signature invalid")
        moff, mlen = regions[GUID_METADATA]
        f.seek(moff)
        meta = f.read(mlen)
        assert meta[0:8] == b"metadata"
        mcount = struct.unpack("<H", meta[10:12])[0]
        items = {}
        for i in range(mcount):
            e = 32 + i * 32
            g = str(uuid.UUID(bytes_le=meta[e : e + 16]))
            io, il = struct.unpack("<II", meta[e + 16 : e + 24])
            items[g] = meta[io : io + il]
        bs = struct.unpack("<I", items[GUID_FILE_PARAMS][0:4])[0]
        vds = struct.unpack("<Q", items[GUID_VDISK_SIZE][0:8])[0]
        boff, _ = regions[GUID_BAT]
        nb = vds // bs
        f.seek(boff)
        bat = struct.unpack(f"<{nb}Q", f.read(nb * 8))

        h_out, h_ref = hashlib.md5(), hashlib.md5()
        zero = b"\x00" * bs
        with open(raw_path, "rb") as r:
            for i, e in enumerate(bat):
                st = e & 7
                if st == 6:
                    f.seek(e & 0xFFFFFFFFFFF00000)
                    d = f.read(bs)
                elif st == 2:
                    d = zero
                else:
                    raise SystemExit(f"Unsupported block state {st} at block {i}")
                h_out.update(d)
                h_ref.update(r.read(bs))
    md5_vhdx, md5_raw = h_out.hexdigest(), h_ref.hexdigest()
    ok = md5_vhdx == md5_raw
    log(f"  verify MD5: vhdx={md5_vhdx} raw={md5_raw} match={ok}")
    return ok


def main():
    ap = argparse.ArgumentParser(description="Pack raw ext4 image into dynamic VHDX")
    ap.add_argument("--raw", required=True, help="Input raw image path")
    ap.add_argument("--out", required=True, help="Output VHDX path")
    ap.add_argument("--verify", action="store_true", default=True, help="Verify readback MD5 (default: on)")
    ap.add_argument("--no-verify", dest="verify", action="store_false")
    a = ap.parse_args()
    make_vhdx(a.raw, a.out)
    if a.verify and not verify_vhdx(a.out, a.raw):
        raise SystemExit("VHDX verification failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
