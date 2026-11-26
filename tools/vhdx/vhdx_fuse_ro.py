#!/usr/bin/env python3
"""Zero-copy read-only FUSE3 virtual raw disk mounter for dynamic/fixed VHDX.

Exposes <mountpoint>/disk.raw without copying or unpacking large VHDX files
(e.g. 128 GiB userdata.vhdx), handling dynamic VHDX chunk_ratio BAT indexing.

Usage (Linux / WSL2 with libfuse3):
    python3 vhdx_fuse_ro.py <image.vhdx> --check
    python3 vhdx_fuse_ro.py <image.vhdx> <mountpoint>
"""
import ctypes
import errno
import os
import stat
import struct
import sys
import uuid

GUID_BAT = "2dc27766-f623-4200-9d64-115e9bfd4a08"
GUID_METADATA = "8b7ca206-4790-4b9a-b8fe-575f050f886e"
GUID_FILE_PARAMS = "caa16737-fa36-4d43-b3b6-33f0aa44e76b"
GUID_VDISK_SIZE = "2fa54224-cd1b-4876-b211-5dbed83bf4b8"
GUID_LOGICAL_SECTOR = "8141bf1d-a96f-4709-ba47-f233a8faab5f"
GUID_PARENT = "a8d35f2d-b30b-454d-abf7-d3d84834ab0c"


def parse_vhdx(path):
    f = open(path, "rb")
    assert f.read(8) == b"vhdxfile", f"Not a VHDX file: {path}"
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
    f.seek(regions[GUID_METADATA][0])
    tbl = f.read(regions[GUID_METADATA][1])
    assert tbl[:8] == b"metadata"
    count = struct.unpack("<H", tbl[10:12])[0]
    meta = {}
    for i in range(count):
        e = tbl[32 + i * 32 : 64 + i * 32]
        g = str(uuid.UUID(bytes_le=e[0:16]))
        io, il = struct.unpack("<II", e[16:24])
        meta[g] = tbl[io : io + il]
    bs, fflags = struct.unpack("<II", meta[GUID_FILE_PARAMS][:8])
    vds = struct.unpack("<Q", meta[GUID_VDISK_SIZE][:8])[0]
    lss = struct.unpack("<I", meta[GUID_LOGICAL_SECTOR][:4])[0]
    assert len(meta.get(GUID_PARENT, b"")) == 0, "Differencing VHDX not supported"
    chunk_ratio = ((1 << 23) * lss) // bs
    is_fixed = bool(fflags & 1)
    boff, blen, _ = regions[GUID_BAT]
    total_entries = blen // 8
    f.seek(boff)
    bat = struct.unpack(f"<{total_entries}Q", f.read(total_entries * 8))
    return f, bs, vds, lss, chunk_ratio, is_fixed, bat


def main():
    if len(sys.argv) != 3:
        raise SystemExit("Usage: python3 vhdx_fuse_ro.py <image.vhdx> <mountpoint|--check>")

    src_path, mnt_path = sys.argv[1], sys.argv[2]
    f_vhdx, bs, vds, lss, chunk_ratio, is_fixed, bat = parse_vhdx(src_path)
    fd = f_vhdx.fileno()

    def read_vhdx(offset, size):
        if offset >= vds:
            return b""
        if offset + size > vds:
            size = vds - offset
        out = bytearray()
        pos, rem = offset, size
        while rem > 0:
            bi = pos // bs
            bo = pos % bs
            take = min(rem, bs - bo)
            bat_idx = bi if is_fixed else (bi + (bi // chunk_ratio))
            e = bat[bat_idx] if bat_idx < len(bat) else 0
            st = e & 7
            if st == 6:
                foff = (e & 0xFFFFFFFFFFF00000) + bo
                chunk = os.pread(fd, take, foff)
                if len(chunk) < take:
                    chunk += b"\x00" * (take - len(chunk))
                out.extend(chunk)
            else:
                out.extend(b"\x00" * take)
            pos += take
            rem -= take
        return bytes(out)

    if mnt_path == "--check":
        sb = read_vhdx(1024, 1024)
        magic = struct.unpack("<H", sb[0x38:0x3A])[0]
        print(f"bs={bs} vds={vds} lss={lss} chunk_ratio={chunk_ratio} fixed={is_fixed} ext_magic={hex(magic)}")
        return 0

    libfuse = ctypes.CDLL("libfuse3.so.3")

    class Timespec(ctypes.Structure):
        _fields_ = [("tv_sec", ctypes.c_long), ("tv_nsec", ctypes.c_long)]

    class Stat(ctypes.Structure):
        _fields_ = [
            ("st_dev", ctypes.c_ulong),
            ("st_ino", ctypes.c_ulong),
            ("st_nlink", ctypes.c_ulong),
            ("st_mode", ctypes.c_uint),
            ("st_uid", ctypes.c_uint),
            ("st_gid", ctypes.c_uint),
            ("__pad0", ctypes.c_int),
            ("st_rdev", ctypes.c_ulong),
            ("st_size", ctypes.c_long),
            ("st_blksize", ctypes.c_long),
            ("st_blocks", ctypes.c_long),
            ("st_atim", Timespec),
            ("st_mtim", Timespec),
            ("st_ctim", Timespec),
            ("__unused", ctypes.c_long * 3),
        ]

    GETATTR_FN = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_char_p, ctypes.POINTER(Stat), ctypes.c_void_p)
    FILLDIR_FN = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_char_p, ctypes.POINTER(Stat), ctypes.c_long, ctypes.c_int)
    READDIR_FN = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p, FILLDIR_FN, ctypes.c_long, ctypes.c_void_p, ctypes.c_int)
    OPEN_FN = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p)
    READ_FN = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_long, ctypes.c_void_p)

    class FuseOps(ctypes.Structure):
        _fields_ = [
            ("getattr", GETATTR_FN),
            ("f1", ctypes.c_void_p * 11),
            ("open", OPEN_FN),
            ("read", READ_FN),
            ("f2", ctypes.c_void_p * 10),
            ("readdir", READDIR_FN),
            ("f3", ctypes.c_void_p * 17),
        ]

    @GETATTR_FN
    def cb_getattr(path, st_ptr, fi):
        ctypes.memset(st_ptr, 0, ctypes.sizeof(Stat))
        st = st_ptr.contents
        if path == b"/":
            st.st_mode = stat.S_IFDIR | 0o555
            st.st_nlink = 2
            return 0
        if path == b"/disk.raw":
            st.st_mode = stat.S_IFREG | 0o444
            st.st_nlink = 1
            st.st_size = vds
            return 0
        return -errno.ENOENT

    @READDIR_FN
    def cb_readdir(path, buf, filler, offset, fi, flags):
        if path != b"/":
            return -errno.ENOENT
        filler(buf, b".", None, 0, 0)
        filler(buf, b"..", None, 0, 0)
        filler(buf, b"disk.raw", None, 0, 0)
        return 0

    @OPEN_FN
    def cb_open(path, fi):
        return 0 if path == b"/disk.raw" else -errno.ENOENT

    @READ_FN
    def cb_read(path, buf, size, offset, fi):
        if path != b"/disk.raw":
            return -errno.ENOENT
        data = read_vhdx(offset, size)
        ctypes.memmove(buf, data, len(data))
        return len(data)

    ops = FuseOps()
    ops.getattr = cb_getattr
    ops.readdir = cb_readdir
    ops.open = cb_open
    ops.read = cb_read

    argv = [b"vhdx_fuse_ro", mnt_path.encode(), b"-f", b"-s", b"-o", b"ro,allow_other"]
    c_argv = (ctypes.c_char_p * len(argv))(*argv)
    libfuse.fuse_main_real.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_char_p), ctypes.POINTER(FuseOps), ctypes.c_size_t, ctypes.c_void_p]
    libfuse.fuse_main_real.restype = ctypes.c_int
    return libfuse.fuse_main_real(len(argv), c_argv, ctypes.byref(ops), ctypes.sizeof(FuseOps), None)


if __name__ == "__main__":
    sys.exit(main())
