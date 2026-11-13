#!/usr/bin/python3
# Matikan primary plane 37 (fbcon/OSD) via DRM atomic commit.
#
# Kenapa atomic, bukan legacy SETPLANE: driver meson atomic-native —
# legacy SETPLANE mengembalikan sukses tapi no-op (fb tetap terpasang).
# Dan DROP_MASTER sebelum close: close sebagai master terakhir memicu
# drm_client_restore -> plane 37 diaktifkan kembali seketika.
#
# Dipanggil run_display.sh di jeda restart saat master DRM bebas.
import ctypes
import fcntl
import os
import struct
import sys
import time

DRM_IOCTL_SET_CLIENT_CAP = 0x4010640D   # _IOW(d,0x0d, 16B)
DRM_CLIENT_CAP_ATOMIC = 3
DRM_IOCTL_OBJ_GETPROPS = 0xC01C64B9     # _IOWR(d,0xB9, 28B)
DRM_IOCTL_GETPROPERTY = 0xC04064AA      # _IOWR(d,0xAA, 64B)
DRM_IOCTL_ATOMIC = 0xC03864BC           # _IOWR(d,0xBC, 56B)
DRM_IOCTL_DROP_MASTER = 0x641F
DRM_MODE_OBJECT_PLANE = 0xEEEEEEEE
ATOMIC_ALLOW_MODESET = 0x0400


def plane_props(fd, plane_id):
    """Return {prop_name: prop_id} untuk plane_id."""
    # pass 1: tanya jumlah properti
    r = fcntl.ioctl(fd, DRM_IOCTL_OBJ_GETPROPS,
                    struct.pack("QQIII", 0, 0, 0, plane_id,
                                DRM_MODE_OBJECT_PLANE), True)
    n = struct.unpack_from("I", r, 16)[0]
    ids = (ctypes.c_uint32 * n)()   # prop ids u32; values tetap u64
    vals = (ctypes.c_uint64 * n)()
    fcntl.ioctl(fd, DRM_IOCTL_OBJ_GETPROPS,
                struct.pack("QQIII", ctypes.addressof(ids),
                            ctypes.addressof(vals), n, plane_id,
                            DRM_MODE_OBJECT_PLANE), True)
    out = {}
    for pid in ids:
        p = fcntl.ioctl(fd, DRM_IOCTL_GETPROPERTY,
                        struct.pack("QQQI", 0, 0, pid, 0)
                        + b"\x00" * 40, True)
        name = p[24:56].split(b"\x00")[0].decode()
        out[name] = pid
    return out


def disable_plane(fd, plane_id, props):
    ids = (ctypes.c_uint32 * 1)(plane_id)
    cnt = (ctypes.c_uint32 * 1)(2)
    pids = (ctypes.c_uint32 * 2)(props["FB_ID"], props["CRTC_ID"])
    vals = (ctypes.c_uint64 * 2)(0, 0)
    a = struct.pack("IIQQQQQQ",
                    ATOMIC_ALLOW_MODESET, 1,
                    ctypes.addressof(ids), ctypes.addressof(cnt),
                    ctypes.addressof(pids), ctypes.addressof(vals),
                    0, 0)
    fcntl.ioctl(fd, DRM_IOCTL_ATOMIC, a)


def main():
    deadline = time.time() + 3.0
    while True:
        fd = -1
        try:
            fd = os.open("/dev/dri/card0", os.O_RDWR)
            fcntl.ioctl(fd, DRM_IOCTL_SET_CLIENT_CAP,
                        struct.pack("QQ", DRM_CLIENT_CAP_ATOMIC, 1))
            props = plane_props(fd, 37)
            disable_plane(fd, 37, props)
            fcntl.ioctl(fd, DRM_IOCTL_DROP_MASTER, 0)
            os.close(fd)
            sys.stderr.write("plane37 off (atomic)\n")
            return
        except OSError as e:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if time.time() >= deadline:
                sys.stderr.write(f"plane37 off failed: {e}\n")
                return
            time.sleep(0.2)


if __name__ == "__main__":
    main()
