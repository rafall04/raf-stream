#!/usr/bin/python3
# Matikan overlay plane 40: framebuffer dmabuf yang di-scanout ikut
# dibebaskan saat pipeline mati -> plane menampilkan memori mati (hijau)
# sampai frame pertama tiba. Dipanggil run_display.sh di jeda restart.
#
# Penting: DROP_MASTER sebelum close — close sebagai master terakhir
# memicu drm_client_restore yang mengaktifkan KEMBALI plane 37 (fbcon)
# tepat setelah plane37_off mematikannya. Retry menunggu master bebas.
import os, fcntl, struct, sys, time

_SETPLANE = 0xC03064B7   # drm_mode_set_plane = 48 B
_DROP_MASTER = 0x641F    # DRM_IOCTL_DROP_MASTER

deadline = time.time() + 3.0
err = None
while True:
    fd = None
    try:
        fd = os.open("/dev/dri/card0", os.O_RDWR)
        fcntl.ioctl(fd, _SETPLANE,
                    struct.pack("12I", 40, 43, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0))
        fcntl.ioctl(fd, _DROP_MASTER, 0)
        err = None
        break
    except OSError as e:
        err = e
        if time.time() >= deadline:
            break
        time.sleep(0.2)
    finally:
        if fd is not None:
            os.close(fd)
if err is not None:
    sys.stderr.write(f"plane40 off failed: {err}\n")
else:
    sys.stderr.write("plane40 off\n")
