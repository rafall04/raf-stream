#!/usr/bin/env python3
# HDMI display pipeline untuk HG680P — satu pipeline, dua sumber:
#
#   [sink_0] videotestsrc+textoverlay "MENYAMBUNG"   ─┐
#   [sink_1] tcpclientsrc:9990 -> h265parse           ├ input-selector
#              -> v4l2h265dec                        ─┘ -> kmssink(plane 40)
#
# Kenapa input-selector: plane 37 (fbcon, zpos=1) SELALU di atas plane 40
# (video, zpos=0, immutable) — selama 37 enabled dengan fb opaque, video
# tidak pernah terlihat. Maka plane 37 dimatikan permanen oleh
# plane37_off.py di run_display.sh, dan splash/status dirender oleh gst
# di plane 40 juga — pergantian splash<->video = set_property active-pad,
# instan, tanpa restart, tanpa DRM master churn.
#
# Probe decoder tetap membuang buffer NV12 nol (bug "Too old frames"
# gst-v4l2 -> hijau) dan menghitung frame valid.
import ctypes
import faulthandler
import fcntl
import gc
import json
import os
import struct
import subprocess
import sys
import threading
import time
import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib

Gst.init(None)
# Service embedded: hang harus self-report — SIGABRT men-dump semua
# stack ke stderr (journal) daripada tebakan wchan.
faulthandler.enable()

IMG_EXT = {".jpg", ".jpeg", ".png", ".webp"}
MEDIA_EXT = {".mp4", ".mkv", ".mov", ".ts", ".webm",
             ".hevc", ".h264", ".h265"} | IMG_EXT


def media_file_ext(name):
    return os.path.splitext(name or "")[1].lower() in MEDIA_EXT


def _is_img(path):
    return os.path.splitext(path or "")[1].lower() in IMG_EXT


def media_dims(path):
    """(w, h) stream video pertama via ffprobe; None bila gagal."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height",
             "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=8)
        w, h = r.stdout.strip().split(",")[:2]
        return int(w), int(h)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def fit_box(w, h):
    """Skala-fit ke frame 1920x1080 preserve aspect + border tengah.
    Return (tw, th, left, right, top, bottom)."""
    if w <= 0 or h <= 0:
        return 1920, 1080, 0, 0, 0, 0
    s = min(1920.0 / w, 1080.0 / h)
    tw = max(2, int(w * s) // 2 * 2)
    th = max(2, int(h * s) // 2 * 2)
    # Border kiri/atas dibulatkan ke genap — videobox di format
    # subsampled (I420/NV12 tak didukung videobox) menolak offset
    # ganjil; sisa piksel diberi ke kanan/bawah.
    l = ((1920 - tw) // 2) & ~1
    t = ((1080 - th) // 2) & ~1
    return tw, th, l, 1920 - tw - l, t, 1080 - th - t


def _sock_stats():
    """Return (close_wait, rx_queue) socket sendiri ke :9990.

    /proc/net/tcp: port remote hex (:2706=9990), st=08 = CLOSE_WAIT
    (server mengirim FIN tapi EOS gstreamer tak pernah tiba — connect
    tepat di celah proses streamer sekarat = socket yatim).
    Cocokkan via inode fd sendiri — jangan menyentuh socket klien lain
    (ffmpeg :9990 yang CLOSE_WAIT sendiri bukan urusan kita).

    rx_queue juga membedakan dua penyebab 'bytes diam':
      rxq>0  = data menunggu tapi tak terbaca = WEDGE hilir
               (decoder/sink tersendat -> backpressure penuh)
      rxq=0  = upstream memang tak mengirim = starvation sah
               (RC tercabut / tunggu IDR) -> tahan splash
    """
    try:
        inos = set()
        for f in os.listdir("/proc/self/fd"):
            try:
                l = os.readlink(f"/proc/self/fd/{f}")
            except OSError:
                continue
            if l.startswith("socket:["):
                inos.add(l[8:-1])
        for line in open("/proc/net/tcp"):
            f = line.split()
            if (len(f) > 9 and f[2].endswith(":2706")
                    and f[9] in inos):
                return f[3] == "08", int(f[4].split(":")[1], 16)
    except OSError:
        pass
    return False, 0


_SETPLANE = 0xC03064B7   # drm_mode_set_plane = 48 B
_SET_CLIENT_CAP = 0x4010640D   # _IOW(d,0x0d, {u64,u64})
_OBJ_GETPROPS = 0xC01C64B9     # _IOWR(d,0xB9, 28B)
_GETPROPERTY = 0xC04064AA      # _IOWR(d,0xAA, 64B)
_ATOMIC = 0xC03864BC           # _IOWR(d,0xBC, 56B)
_OBJ_PLANE = 0xEEEEEEEE


def _plane_props(fd, plane_id):
    r = fcntl.ioctl(fd, _OBJ_GETPROPS,
                    struct.pack("QQIII", 0, 0, 0, plane_id, _OBJ_PLANE), True)
    n = struct.unpack_from("I", r, 16)[0]
    ids = (ctypes.c_uint32 * n)()
    vals = (ctypes.c_uint64 * n)()
    fcntl.ioctl(fd, _OBJ_GETPROPS,
                struct.pack("QQIII", ctypes.addressof(ids),
                            ctypes.addressof(vals), n, plane_id,
                            _OBJ_PLANE), True)
    out = {}
    for pid in ids:
        p = fcntl.ioctl(fd, _GETPROPERTY,
                        struct.pack("QQQI", 0, 0, pid, 0) + b"\x00" * 40,
                        True)
        out[p[24:56].split(b"\x00")[0].decode()] = pid
    return out


def _atomic_off(fd, plane_id, props):
    ids = (ctypes.c_uint32 * 1)(plane_id)
    cnt = (ctypes.c_uint32 * 1)(2)
    pids = (ctypes.c_uint32 * 2)(props["FB_ID"], props["CRTC_ID"])
    vals = (ctypes.c_uint64 * 2)(0, 0)
    fcntl.ioctl(fd, _ATOMIC, struct.pack(
        "IIQQQQQQ", 0x0400, 1,                          # ALLOW_MODESET
        ctypes.addressof(ids), ctypes.addressof(cnt),
        ctypes.addressof(pids), ctypes.addressof(vals), 0, 0))


def acquire_master():
    """Buka /dev/dri/card0 sebagai DRM master, lalu matikan plane 37.

    Jalur warisan: proses ini hasil os.execv — fd master proses lama
    diwariskan lewat DJI_DRM_FD. Master TIDAK pernah lepas -> plane37
    tetap mati -> tak ada flash console di celah respawn.

    plane 37 (fbcon, zpos=1 immutable) selalu di atas video plane 40 —
    selama ia enabled dengan fb opaque, video tidak pernah terlihat.
    Pemegang master TIDAK boleh melepas/close master: setiap release
    memicu drm_client_restore -> fbdev mengaktifkan kembali plane 37
    seketika. Maka fd ini dipegang seumur proses dan dioper ke kmssink
    lewat properti `fd` — kmssink memakai fd yang sudah master.
    """
    efd = os.environ.pop("DJI_DRM_FD", None)
    if efd is not None:
        try:
            fd = int(efd)
        except ValueError:
            fd = -1
        try:
            props = _plane_props(fd, 37)
            _atomic_off(fd, 37, props)
            print(f"[display] master diwarisi (fd={fd}), plane37 off",
                  flush=True)
            # Beri firmware v4l2 waktu menurunkan sesi lama — setara
            # fuser-wait + sleep di run_display.sh yang kita lewati.
            time.sleep(2)
            return fd
        except OSError:
            if fd >= 0:
                os.close(fd)
    deadline = time.time() + 6.0
    while True:
        fd = os.open("/dev/dri/card0", os.O_RDWR)
        try:
            fcntl.ioctl(fd, _SET_CLIENT_CAP, struct.pack("QQ", 3, 1))
            if os.environ.get("DJI_NO_PLANEOFF"):
                print("[display] master acquired, plane37 OFF skipped "
                      "(DJI_NO_PLANEOFF)", flush=True)
                return fd
            props = _plane_props(fd, 37)
            _atomic_off(fd, 37, props)
            print("[display] master acquired, plane37 off (atomic)",
                  flush=True)
            return fd
        except OSError:
            # master lama (kmssink sesi sebelumnya) mungkin masih teardown
            os.close(fd)
            if time.time() >= deadline:
                print("[display] WARN: master/atomic gagal, lanjut polos",
                      flush=True)
                return None
            time.sleep(0.2)


CFG_PATH = "/root/display_cfg.json"
_show_ind = True   # di-set ulang oleh build() dari cfg


def _load_cfg():
    # Tampilan splash bisa diubah lewat /root/display_cfg.json; file
    # ini ditulis ulang tiap save. Default {} = tampilan bawaan.
    try:
        return json.load(open(CFG_PATH))
    except (OSError, ValueError):
        return {}


def _cfg_color(cfg, key, default):
    # Terima "aarrggbb" atau "#rrggbb"; keluaran int 0xAARRGGBB.
    s = str(cfg.get(key, "")).lstrip("#")
    try:
        return int(s, 16) if len(s) == 8 else 0xFF000000 | int(s, 16)
    except ValueError:
        return default


def _loop_ordered(cand):
    # Urutan playlist: loop_order eksplisit lebih dulu, file baru sisa
    # sorted() di belakang; loop_off disaring keluar. Re-read cfg tiap
    # panggil — toggle/urutan dari UI berlaku di siklus playlist
    # berikutnya tanpa restart display.
    cfg = _load_cfg()
    off = set(cfg.get("loop_off") or [])
    cand = [f for f in cand if f not in off]
    seen = set()
    head = []
    for f in cfg.get("loop_order") or []:
        if f in cand and f not in seen:
            seen.add(f)
            head.append(f)
    # UI menolak mematikan file aktif terakhir; bila lolos juga,
    # playlist kosong itu rusak — jatuh ke daftar penuh, jangan
    # kosong (media_advance IndexError/leg hitam).
    return head + [f for f in sorted(cand) if f not in seen] or sorted(cand)


def build(drm_fd=None, media_path=None):
    global _show_ind
    p = Gst.Pipeline.new("dji-display")

    def E(name, props=None):
        e = Gst.ElementFactory.make(name, None)
        if e is None:
            raise RuntimeError("element missing: " + name)
        for k, v in (props or {}).items():
            e.set_property(k, v)
        p.add(e)
        return e

    cfg = _load_cfg()
    clean = cfg.get("clean_mode", False)
    _show_ind = cfg.get("indicators", True) and not clean
    accent = _cfg_color(cfg, "accent", 0xffff8800)
    T = (lambda k, d: "" if clean else cfg.get(k, d))
    mode = cfg.get("mode", "auto")
    if mode not in ("auto", "loop", "smpte"):
        mode = "auto"
    loop_path = os.path.join(
        "/root/loops", os.path.basename(cfg.get("loop_file", "")))
    if mode == "loop" and not os.path.isfile(loop_path):
        print("[display] loop tanpa file -> auto", flush=True)
        mode = "auto"

    # sync-streams=false WAJIB: dengan default true, buffer di pad video
    # non-aktif ditahan untuk sinkronisasi PTS -> leg berhenti mengalir
    # saat CONNECTING -> socket penuh -> sendall block -> q meluap ->
    # klien unsynced -> loop maut. false = drop murni, leg selalu drain.
    sel = E("input-selector", {"sync-streams": False})
    # can_scale kmssink MATI PERMANEN begitu satu drmModeSetPlane gagal
    # (retry tanpa scale). Leg splash 1920x1080 PAR=1/1 -> hitungan
    # 2048x1080 -> commit downscale -> overlay meson menolak -> semua
    # frame berikutnya 1:1 di tengah = video "kecil". Maka kedua leg
    # WAJIB ber-caps PAR=15/16: hitungan = 1920x1080 persis (1:1, tak
    # ada commit terlarang) dan leg video 720p -> upscale -> fullscreen.
    ks_props = {"connector-id": 35, "plane-id": 40, "sync": False,
                "show-preroll-frame": False, "skip-vsync": True,
                "can-scale": True,
                # default 15ms menahan buffer terlalu lama menjelang
                # vsync; 8ms masih di bawah periode frame 30/60fps.
                "processing-deadline": 8_000_000}
    if drm_fd is not None:
        ks_props["fd"] = drm_fd     # fd master kita -> commit kmssink jalan
    sink = E("kmssink", ks_props)
    sel.link(sink)

    # sink_0 — splash "connecting". NV12 720p agar negosiasi dengan
    # kmssink sama seperti leg video (meson overlay tetap hw-scale ke
    # mode aktif 1080p).
    vts = E("videotestsrc", {"is-live": True, "pattern": 2})  # black
    txt0 = E("textoverlay", {"text": T("title", "RAF MULTIMEDIA"),
                             "valignment": 3, "halignment": 1, "ypos": 0.10,
                             "font-desc": "Sans Bold 26",
                             "color": accent})
    # Ukuran/posisi diverifikasi dengan render PNG aktual (textoverlay
    # membesarkan font-desc ~2.2x dan ypos menambatkan TENGAH blok teks).
    txt1 = E("textoverlay", {"text": T("subtitle", "MENYAMBUNG VIDEO…"),
                             "valignment": 3, "halignment": 1, "ypos": 0.34,
                             "font-desc": "Sans 22", "color": 0xffffffff})
    txtc = E("textoverlay", {"text": "",
                             "valignment": 3, "halignment": 1, "ypos": 0.52,
                             "font-desc": "Monospace 16",
                             "color": accent})
    txt2 = E("textoverlay", {"text": T("footer", "DJI Mini 3 - STB Display"),
                             "valignment": 3, "halignment": 1, "ypos": 0.88,
                             "font-desc": "Sans 12", "color": 0xff888888})
    cvt = E("videoconvert")
    # framerate=2/1: splash tetap animasi (counter menyambung) tapi
    # leg ini memproses 1080p TIAP frame lewat textoverlay+videoconvert
    # bahkan saat input-selector membuangnya di LIVE — terukur ~70%
    # CPU display.py. 2fps memangkas beban itu ~15x.
    # PAR=15/16 wajib: lihat catatan can_scale di atas.
    cf = E("capsfilter", {"caps": Gst.Caps.from_string(
        "video/x-raw,format=NV12,width=1920,height=1080,"
        "framerate=2/1,pixel-aspect-ratio=15/16")})
    vts.link(txt0)
    txt0.link(txt1)
    txt1.link(txtc)
    txtc.link(txt2)
    txt2.link(cvt)
    cvt.link(cf)
    cf.link(sel)
    pad_splash = cf.get_static_pad("src").get_peer()

    # Leg live hanya di mode auto: di mode loop/smpte decoder v4l2
    # menganggur tetap MEMEGANG /dev/video0 (single-instance) -> decode
    # file promo gagal "allocate required memory" (terukur).
    pad_video = src = tail = dec = None
    if mode == "auto":
        # sink_1 — video asli. videoscale 720p->1080p + capsfilter: caps
        # leg ini identik dengan leg splash -> switch active-pad tanpa
        # renegosiasi. nearest-neighbour = termurah di CPU (~stretch
        # 1.5x, artefaknya nyaris tak terlihat untuk FPV). Src 1280x720
        # penuh — 675 tadi BUKAN crop SPS (videotestsrc polos pun kena)
        # melainkan hitungan aspect kmssink dari EDID — jadi seluruh 720
        # baris = gambar asli, 16:9->16:9 = fullscreen tanpa distorsi.
        src = E("tcpclientsrc", {"host": "127.0.0.1", "port": 9990})
        parse = E("h265parse")
    # Decoder dipilih run_display.sh via env DJI_DECODER:
    #  - v4l2h265dec (hw): latency ~0.3s, CPU bebas — TAPI abort total
    #    (dmesg dec_status 0xF) bila start mid-GOP/missing ref. Aman
    #    sekarang karena klien selalu mulai dari seed IDR utuh.
    #  - avdec_h265 (sw): tak pernah abort (frame rusak -> artefak ->
    #    lanjut) tapi ~1.5 core — dipakai otomatis saat hw abort berulang.
        dec_name = os.environ.get("DJI_DECODER", "avdec_h265")
        # kmssink (fd+EDID aktif) menuntut PAR mode-nya — TV ini 15/16;
        # caps event ber-PAR lain ditolak mentah (not-negotiated ->
        # pipeline mati). capssetter menimpa caps event sebelum sel.
        pset2 = E("capssetter", {"caps": Gst.Caps.from_string(
            "video/x-raw,pixel-aspect-ratio=15/16")})
        if dec_name == "v4l2h265dec":
            # Jalur zero-copy: decoder ekspor dmabuf -> capssetter
            # (event saja) -> kmssink import + plane40 scale 720->1080
            # di HW. Menghapus videoconvert/crop/scale CPU (~3-6 frame
            # antrian + ~15-40ms/frame) = sumber delay terbesar yang
            # masih kita kontrol. render-rectangle memaksa dst penuh
            # 1920x1080 (tanpa ini kmssink letterbox krn PAR 15/16).
            dec = E("v4l2h265dec", {"discard-corrupted-frames": True,
                                   "capture-io-mode": "dmabuf"})
            src.link(parse)
            parse.link(dec)
            dec.link(pset2)
            tail = pset2
        else:
            # lowres=1: decode 640x360 ~3x lebih cepat dari 720p penuh —
            # avdec di S905X4 hanya sanggup ~17fps full-res vs stream
            # 25fps -> antrian selalu penuh -> splice -> abu menyebar.
            # Dengan headroom, klien selalu di live edge.
            dec = E("avdec_h265", {"lowres": 1, "max-threads": 4})
            cvt2 = E("videoconvert", {"dither": 0,
                                      "chroma-resampler": 0})
            pset = E("capssetter", {"caps": Gst.Caps.from_string(
                "video/x-raw,pixel-aspect-ratio=1/1")})
            # avdec meng-embed gambar 720 dalam buffer 738-baris — sisa
            # = padding mati (Y=0 -> hijau). Crop 48 menutup kasus itu.
            crop = E("videocrop", {"bottom": 48})
            scl = E("videoscale", {"method": 0, "n-threads": 4})
            cf2 = E("capsfilter", {"caps": Gst.Caps.from_string(
                "video/x-raw,format=NV12,width=1920,height=1080")})
            src.link(parse)
            parse.link(dec)
            dec.link(pset)
            pset.link(cvt2)
            cvt2.link(crop)
            crop.link(scl)
            scl.link(cf2)
            cf2.link(pset2)
            tail = pset2
        print(f"[display] decoder={dec_name}", flush=True)
        # Queue leaky=downstream memutuskan decoder dari sel->lock:
        # chain di input-selector berjalan di bawah kunci GLOBAL
        # selector — pad aktif memegangnya selama gst_pad_push ->
        # kmssink render, dan bila render hang (wait-for-flip tanpa
        # vblank) SEMUA pad ikut macet: buffer decoder terhenti -> pool
        # kering di ~36 -> socket penuh -> resync storm. Dengan queue,
        # thread src queue yang macet — decoder tetap drain (leak buang
        # frame basi), socket terus terbaca, streamer tak resync; saat
        # sel pulih video lanjut langsung. Probe liveness di output.
        qvid = E("queue", {"max-size-buffers": 2, "leaky": 1})
        pset2.link(qvid)
        qvid.link(sel)
        pad_video = qvid.get_static_pad("src").get_peer()
        tail = qvid

    # Leg media standby (mode auto + standby=loop) sebagai bin
    # swappable (_build_mbin) yang bermuara ke pad sel persisten:
    # pad ini milik sel dan bertahan di semua advance — bin diganti,
    # pad tidak. Queue blocking di dalam bin: saat pad tak aktif leg
    # membeku hemat CPU dan posisi file tertahan.
    pad_media = mbin = mqsrc = mqblock = vol = None
    media_sinks = set()
    mleg = {}
    if mode == "auto" and media_path:
        pad_media = sel.request_pad_simple("sink_%u")
        mbin, ghost, mleg = _build_mbin(p, cfg, media_path)
        vol = mleg["vol"]
        media_sinks = mleg["sinks"]
        ghost.link_full(pad_media, Gst.PadLinkCheck.HIERARCHY)
        mqsrc = mqblock = ghost
        print(f"[display] standby media="
              f"{os.path.basename(media_path)}", flush=True)

    return {"p": p, "sel": sel, "dec": dec, "pad_splash": pad_splash,
            "pad_video": pad_video, "pad_media": pad_media,
            "txtc": txtc, "src": src, "tail": tail, "sink": sink,
            "vol": vol, "msinks": media_sinks, "mqsrc": mqsrc,
            "mqblock": mqblock, "mbin": mbin,
            "mdec": mleg.get("dec"), "mpa": mleg.get("pa_id")}


def _fit_rect(path):
    # render-rectangle '<x,y,w,h>': plane menskala isi frame ke rect —
    # letterbox border tanpa videobox (nol CPU). Properti bertipe
    # GstValueArray — diset lewat util_set_object_arg sintaks '<>'.
    dims = media_dims(path)
    if not dims:
        return "<0,0,1920,1080>"
    tw, th, bl, br, bt, bb = fit_box(*dims)
    return f"<{bl},{bt},{tw},{th}>"


_RECT_FULL = "<0,0,1920,1080>"


def _set_rect(el, rect):
    Gst.util_set_object_arg(el, "render-rectangle", rect)


def _skip_v4l2(db, pad, caps, factories):
    # Decoder SW saja: v4l2h264dec membuang frame undrained + state rusak
    # pasca-seek ("eos sent too early" = loop beku). Bonus: /dev/video0
    # tetap bebas untuk leg live v4l2h265dec — syarat mode standby.
    keep = [f for f in factories if not f.get_name().startswith("v4l2")]
    return keep or factories


def _media_leg(p, E, cfg, path, link_tail, allow_hw=False):
    """Leg file promo: filesrc -> decodebin -> pad dinamis.

    link_tail(pad) merangkai ekor video ke tujuan akhir — kmssink
    langsung (pipeline media tersendiri) atau queue -> input-selector
    (standby hybrid). allow_hw=False memaksa decoder SW (dipakai saat
    /dev/video0 sudah dipegang leg live). Return dict dec/vol/sinks:
    vol = elemen volume untuk mute saat live; sinks = sink milik leg
    (alsasink + fakesink) untuk membedakan EOS bus media vs live.
    """
    fsrc = E("filesrc", {"location": path})
    ldec = E("decodebin")
    fsrc.link(ldec)
    if not allow_hw:
        # Decoder SW saja: v4l2h264dec membuang frame undrained + state
        # rusak pasca-seek. Bonus: /dev/video0 bebas untuk leg live.
        ldec.connect("autoplug-sort", _skip_v4l2)

    lchain = {}
    leg = {"vol": None, "sinks": set()}
    # Sink leg mewarisi base_time pipeline (konstan sejak start),
    # sedangkan PTS file mulai ~0 -> tiap buffer dianggap terlambat
    # senilai uptime pipeline -> drain instan (audio EOS 0.38dtk,
    # video EOS 1.5dtk, group-EOS -> advance prematur — terukur).
    # ts-offset menggeser basis sync sink ke "sekarang": frame
    # terpacu clock normal lagi. kmssink punya hitungan sendiri di
    # _mcount dari frame pertama; ini untuk sink dalam bin.
    # Dihitung malas di _loop_pad: clock/base_time bin baru sah hanya
    # setelah ikut pipeline (saat pad-added) — di sini masih None.
    def _set_off(sn):
        if "off" not in leg:
            try:
                leg["off"] = int(
                    p.get_clock().get_time() - p.get_base_time())
            except Exception:
                leg["off"] = 0
        try:
            sn.set_property("ts-offset", leg["off"])
            # qos=false: QoS sink ini menjalar ke decoder audio bin
            # -> skip semua frame sisa -> EOS ~0.3dtk (terukur di
            # decodebin2). Leg bebas QoS; pacing tetap dari sync+offset.
            sn.set_property("qos", False)
        except Exception:
            pass

    def _loop_pad(db, pad):
        caps = pad.query_caps(None)
        nm = (caps.get_structure(0).get_name()
              if caps and caps.get_size() else "")
        try:
            if nm.startswith("video/") and "video" not in lchain:
                # TANPA rantai sw: NV12/I420 decoder diterima kmssink
                # langsung — skala+letterbox dikerjakan plane lewat
                # render-rectangle. Rantai sw lama (~10MB/frame:
                # scale->I420->vbox 1920x1080->NV12) melebihi realtime
                # S905X (~200ms/frame) -> QoS-drop semua frame ->
                # "beku" padahal stream hidup (terukur gst-launch).
                # Decoder menempadkan buffer ke alignment codec
                # (h264: 360->384, 1080->1088) — src rect kmssink
                # jadi melampaui fb -> drmModeSetPlane ENOSPC ->
                # restart palsu loop layar-hitam (terukur). Probe
                # mengukur stride frame pertama (size = stride*rows
                # *1.5 utk 4:2:0 planar) lalu memasang crop meta lewat
                # videocrop sebelum buffer yang sama diproses —
                # zero-copy: kmssink membaca crop meta untuk src.
                crop = E("videocrop")
                crop.sync_state_with_parent()
                src = pad
                if _is_img(path):
                    # Still image: decodebin memancarkan SATU buffer
                    # lalu EOS — imagefreeze memutarnya sebagai stream
                    # terus-menerus sehingga leg hidup seperti video.
                    # Advance bukan lewat EOS tapi timer photo_secs
                    # (_arm_photo menulis meos, jalur yang sama).
                    imgf = E("imagefreeze")
                    vrate = E("videorate")
                    for e in (imgf, vrate):
                        e.sync_state_with_parent()
                    # kmssink::calculate_display_ratio menerapkan PAR
                    # panel (15/16, dari EDID) ke satu sisi: AR caps
                    # <=16/9 -> sisi menyusut (video: 720->675, muat
                    # fb -> lolos); AR >16/9 -> sisi MELEBAR melampaui
                    # fb (foto: 1024->1092) -> SetPlane ENOSPC ->
                    # reexec (terukur). Paksa AR caps =16/9 lewat
                    # videoscale; imagefreeze memutar buffer hasilnya
                    # jadi biaya skala hanya satu frame.
                    dims = media_dims(path)
                    need_scale = dims and dims[0] * 9 > dims[1] * 16
                    if need_scale:
                        tw = max(2, dims[0] & ~1)
                        th = max(2, (tw * 9 // 16) & ~1)
                        vconv = E("videoconvert")
                        vscale = E("videoscale")
                        for e in (vconv, vscale):
                            e.sync_state_with_parent()
                        if not pad.link_full(
                                vconv.get_static_pad("sink"),
                                Gst.PadLinkCheck.HIERARCHY) == \
                                Gst.PadLinkReturn.OK:
                            raise RuntimeError("loop img-vconv link")
                        vconv.link(vscale)
                        # PAR=1/1 WAJIB eksplisit: videoscale
                        # menulis PAR hasil preserve-DAR (576/571)
                        # yang tetap memicu jalur pelebaran
                        # calculate_display_ratio -> ENOSPC (terukur).
                        scaps = Gst.Caps.from_string(
                            "video/x-raw,format=I420,"
                            f"width={tw},height={th},"
                            "pixel-aspect-ratio=1/1")
                        vscale.link_filtered(imgf, scaps)
                    else:
                        # Dimensi ganjil ditolak fb 4:2:0 di kmssink
                        # (terukur 1024x571) — dan crop HARUS sebelum
                        # imagefreeze: freeze mendorong ulang buffer
                        # jpegdec mentah, caps negosiasi hilir tak
                        # mengubah layout memori yang ditolak AddFB.
                        try:
                            # get_current_caps = caps tetap;
                            # query_caps di pad-added masih RANGE.
                            cc = pad.get_current_caps() or caps
                            s = cc.get_structure(0)
                            iw = s.get_int("width")[1]
                            ih = s.get_int("height")[1]
                        except Exception:
                            iw = ih = 0
                        if iw % 2 or ih % 2:
                            ev = E("videocrop")
                            ev.set_property("right", iw % 2)
                            ev.set_property("bottom", ih % 2)
                            ev.sync_state_with_parent()
                            if not pad.link_full(
                                    ev.get_static_pad("sink"),
                                    Gst.PadLinkCheck.HIERARCHY) == \
                                    Gst.PadLinkReturn.OK:
                                raise RuntimeError("loop evencrop link")
                            if not ev.get_static_pad("src").link_full(
                                    imgf.get_static_pad("sink"),
                                    Gst.PadLinkCheck.HIERARCHY) == \
                                    Gst.PadLinkReturn.OK:
                                raise RuntimeError("evencrop-src link")
                        elif not pad.link_full(
                                imgf.get_static_pad("sink"),
                                Gst.PadLinkCheck.HIERARCHY) == \
                                Gst.PadLinkReturn.OK:
                            raise RuntimeError("loop imagefreeze link")
                    imgf.link(vrate)
                    src = vrate.get_static_pad("src")
                    # EOS jpegdec tak boleh keluar leg: eventnya basi
                    # tersangkut di antrian tail bersama lalu ter-flush
                    # ~1dtk ke leg pengganti -> probe meos membaca
                    # advance prematur, leg video terpotong (terukur).
                    # Akhir leg foto diatur timer photo_secs, bukan EOS.
                    src.add_probe(
                        Gst.PadProbeType.EVENT_DOWNSTREAM,
                        lambda _p, i_: (
                            Gst.PadProbeReturn.DROP
                            if i_.get_event() is not None and
                            i_.get_event().type == Gst.EventType.EOS
                            else Gst.PadProbeReturn.OK))
                pid = []

                def _autocrop(cp, info):
                    if pid:
                        cp.remove_probe(pid[0])
                    try:
                        # Caps di sink crop (post-evencrop/freeze) —
                        # bukan pad decodebin yang masih pra-normalisasi
                        # (1024x571 vs 1024x570); selisih 1px membuat
                        # hitungan stride/crop meleset -> SetPlane ENOSPC.
                        s = cp.get_current_caps().get_structure(0)
                        w = s.get_int("width")[1]
                        h = s.get_int("height")[1]
                        size = info.get_buffer().get_size()
                        for rows in ((h + 15) // 16 * 16, h):
                            if rows and size * 2 % (3 * rows) == 0:
                                st = size * 2 // (3 * rows)
                                if w <= st <= w + 512:
                                    crop.set_property("right", st - w)
                                    crop.set_property("bottom",
                                                      rows - h)
                                break
                    except Exception:
                        pass
                    return Gst.PadProbeReturn.OK
                pid.append(crop.get_static_pad("sink").add_probe(
                    Gst.PadProbeType.BUFFER, _autocrop))
                if not src.link_full(
                        crop.get_static_pad("sink"),
                        Gst.PadLinkCheck.HIERARCHY) == \
                        Gst.PadLinkReturn.OK:
                    raise RuntimeError("loop videocrop link")
                lchain["video"] = link_tail(
                    crop.get_static_pad("src"))
                if not lchain["video"]:
                    print("[display] loop video link gagal", flush=True)
                    raise RuntimeError("loop video chain link")
            elif nm.startswith("audio/") and "audio" not in lchain \
                    and cfg.get("loop_audio", True) \
                    and not os.path.exists("/tmp/dji_loop_no_audio"):
                acv = E("audioconvert")
                ars = E("audioresample")
                # volume = mute gate standby: audio promo dibungkamkan
                # saat live, dipulihkan saat kembali standby.
                vol_ = E("volume")
                asn = E("alsasink", {"device": "hw:0,0"})
                _set_off(asn)
                acv.link(ars)
                ars.link(vol_)
                vol_.link(asn)
                for e in (acv, ars, vol_, asn):
                    e.sync_state_with_parent()
                if pad.link_full(acv.get_static_pad("sink"),
                                 Gst.PadLinkCheck.HIERARCHY) == \
                        Gst.PadLinkReturn.OK:
                    lchain["audio"] = True
                    leg["vol"] = vol_
                    leg["sinks"].add(asn)
                else:
                    for e in (asn, vol_, ars, acv):
                        e.set_state(Gst.State.NULL)
                        p.remove(e)
            # Pad yang tak dipakai tetap WAJIB di-link — pad yatim
            # membuat multiqueue decodebin penuh -> qtdemux
            # "Internal data stream error" -> seluruh leg beku
            # (terukur: audio off lewat marker -> semua macet).
            if not pad.is_linked():
                fsn = E("fakesink", {"sync": True})
                _set_off(fsn)
                fsn.sync_state_with_parent()
                pad.link_full(fsn.get_static_pad("sink"),
                              Gst.PadLinkCheck.HIERARCHY)
                leg["sinks"].add(fsn)
        except Exception as ex:
            print(f"[display] loop_pad exc: {ex!r}", flush=True)

    leg["dec"] = ldec
    # Siklus ref tak terputus gc: bin -> decodebin -> handler
    # pad-added -> closure E -> b (=bin). Handler C-side menahan
    # closure selamanya -> refcount bin = 1 abadi -> SELURUH bin
    # + anak-anak bocor per swap (~1-2MB, terukur leaks-tracer).
    # Putus edge-nya saat autoplug selesai.
    pa_id = [ldec.connect("pad-added", _loop_pad)]

    def _nmp(db):
        if pa_id[0]:
            db.disconnect(pa_id[0])
            pa_id[0] = 0
    ldec.connect("no-more-pads", _nmp)
    leg["pa_id"] = pa_id
    return leg


def _build_mbin(p, cfg, path, gen=0):
    """Leg media standby sebagai bin swappable di pipeline selector.

    filesrc+decodebin+audio/fakesink+queue ekor hidup dalam satu bin;
    ghost src = satu-satunya titik keluar video ke pad sel persisten.
    Advance playlist hanya menukar bin ini — leg live, kmssink, gate
    dan socket :9990 TIDAK ikut direbuild (terukur: rebuild penuh per
    file membunuh spin-up v4l2h265dec -> gate 40 frame tak pernah
    tercapai saat promo pendek, plus open/close /dev/video0 per file =
    storm teardown yang dilarang). Return (bin, ghost, vol, sinks).
    Nama harus unik: teardown async berarti bin lama masih bisa
    menempel di pipeline saat yang baru di-add — nama sama = add
    ditolak diam-diam (terukur: leg mati, mshow beku).
    """
    b = Gst.Bin.new(f"mbin{gen}")

    def E(name, props=None):
        e = Gst.ElementFactory.make(name, None)
        if e is None:
            raise RuntimeError("element missing: " + name)
        for k, v in (props or {}).items():
            e.set_property(k, v)
        b.add(e)
        return e

    # Queue ekor (blocking): tempat leg membeku saat live dan titik
    # liveness/EOS — probe dipasang di ghost-nya, bukan di sini.
    mq = E("queue", {"max-size-buffers": 2})
    leg = _media_leg(
        b, E, cfg, path,
        lambda pd: pd.link_full(
            mq.get_static_pad("sink"),
            Gst.PadLinkCheck.HIERARCHY) ==
        Gst.PadLinkReturn.OK)
    ghost = Gst.GhostPad.new("src", mq.get_static_pad("src"))
    b.add_pad(ghost)
    if not p.add(b):
        print("[display] mbin add gagal — nama bentrok?", flush=True)
    return b, ghost, leg


def build_media(drm_fd, mode, cfg, path=None):
    """Pipeline tersendiri untuk mode loop/smpte.

    Berbeda dari build(): sink sync=TRUE — file/pola dipacu oleh clock
    pipeline (PTS). Di pipeline selector utama sink sync=false untuk
    latensi live sehingga leg file akan meledak secepat decode (~4x
    speed, terukur). Pipeline sendiri = pacing benar + EOS bus -> seek
    kanonik untuk loop.
    """
    p = Gst.Pipeline.new("dji-media")

    def E(name, props=None):
        e = Gst.ElementFactory.make(name, None)
        if e is None:
            raise RuntimeError("element missing: " + name)
        for k, v in (props or {}).items():
            e.set_property(k, v)
        p.add(e)
        return e

    ks_props = {"connector-id": 35, "plane-id": 40,
                "show-preroll-frame": False, "can-scale": True,
                "processing-deadline": 8_000_000}
    if drm_fd is not None:
        ks_props["fd"] = drm_fd
    sink = E("kmssink", ks_props)

    if mode == "smpte":
        # Pola bar uji proyektor (keystone/geometri/warna).
        stv = E("videotestsrc", {"is-live": True, "pattern": "smpte75"})
        scf = E("capsfilter", {"caps": Gst.Caps.from_string(
            "video/x-raw,format=NV12,width=1920,height=1080,"
            "pixel-aspect-ratio=15/16")})
        stv.link(scf)
        scf.link(sink)
        return p, sink, None

    # mode == "loop": filesrc -> decodebin -> video+audio -> sink.
    # Skala+letterbox = render-rectangle plane; decoder hw bebas
    # dipakai (tak ada leg live yang memegang /dev/video0).
    if path is None:
        path = os.path.join(
            "/root/loops", os.path.basename(cfg.get("loop_file", "")))
    _set_rect(sink, _fit_rect(path))
    leg = _media_leg(
        p, E, cfg, path,
        lambda pd: pd.link_full(
            sink.get_static_pad("sink"),
            Gst.PadLinkCheck.HIERARCHY) ==
        Gst.PadLinkReturn.OK, allow_hw=True)
    print(f"[display] mode=loop file={os.path.basename(path)}",
          flush=True)
    return p, sink, leg["dec"]


def main():
    drm_fd = acquire_master()   # matikan plane37 + oper master ke kmssink
    # gate = jumlah frame yang harus terdecode sebelum splash dilepas ke
    # video: frame konvergensi pasca-join/resync (referensi belum utuh)
    # tampil hijau — 40 frame ~1.3 dtk menutup seluruh fase itu.
    st = {"drop": 0, "show": 0, "n": 0, "nomap": 0, "live": False,
          "gate": 40}
    _cfg0 = _load_cfg()
    mode = _cfg0.get("mode", "auto")
    if mode not in ("auto", "loop", "smpte"):
        mode = "auto"
    loop_path = os.path.join(
        "/root/loops", os.path.basename(_cfg0.get("loop_file", "")))
    if mode == "loop" and not os.path.isfile(loop_path):
        print("[display] loop tanpa file -> auto", flush=True)
        mode = "auto"

    # Daftar media: dipakai mode loop (pipeline tersendiri) maupun
    # standby loop di mode auto (leg ke-3 di input-selector).
    mfiles = []
    if mode == "loop":
        mfiles = [loop_path]
    elif mode == "auto" and _cfg0.get("standby") == "loop":
        mfiles = [loop_path] if os.path.isfile(loop_path) else []
    if _cfg0.get("loop_all"):
        # loop_all berarti "putar semua yang ada di direktori" —
        # jangan digerbang loop_file: nama yang kedaluwarsa/hilang
        # tak boleh mematikan seluruh playlist.
        try:
            cand = _loop_ordered(f for f in os.listdir("/root/loops")
                                 if media_file_ext(f))
            if cand:
                mfiles = [os.path.join("/root/loops", f) for f in cand]
        except OSError:
            pass
    st["mfiles"] = mfiles
    standby_loop = (mode == "auto" and
                    _cfg0.get("standby") == "loop" and bool(mfiles))
    if mfiles:
        print(f"[display] media mode={mode} standby={standby_loop} "
              f"files={len(mfiles)}", flush=True)

    # R = rak elemen pipeline hidup. _boot_pipeline mengisi ulang isinya
    # saat advance playlist; closure membaca lewat R[...] sehingga elemen
    # baru otomatis terlihat tanpa nonlocal berantai.
    R = {}
    p = None

    def set_live(live):
        if live == st["live"]:
            return
        st["live"] = live
        # Selector hanya digeser di mode auto — media pipeline memegang
        # sink sendiri dan flag live murni untuk watchdog tick().
        if mode == "auto" and R.get("sel") is not None:
            R["sel"].set_property(
                "active-pad", R["pad_video"] if live else
                (R["pad_media"] or R["pad_splash"]))
            if standby_loop:
                if live:
                    # BLOCK pad masuk queue media: leg membeku persis di
                    # posisi file (decodebin ikut stall -> audio pad
                    # berhenti mengalir sendiri). volume=0 membungkam
                    # sisa buffer audio yang sudah telanjur lolos.
                    if R.get("mqblock") is not None:
                        st["mblock"] = R["mqblock"].add_probe(
                            Gst.PadProbeType.BLOCK |
                            Gst.PadProbeType.BUFFER,
                            lambda p_, i_, s_: Gst.PadProbeReturn.OK, st)
                    if R.get("vol") is not None:
                        R["vol"].set_property("volume", 0.0)
                    R["sink"].set_property("sync", False)
                    R["sink"].set_property("ts-offset", 0)
                    # Live selalu layar penuh — rect letterbox milik
                    # file promo tak boleh ikut ke leg video.
                    _set_rect(R["sink"], _RECT_FULL)
                else:
                    mb = st.pop("mblock", None)
                    if mb is not None and R.get("mqblock") is not None:
                        R["mqblock"].remove_probe(mb)
                    if R.get("vol") is not None:
                        R["vol"].set_property("volume", 1.0)
                    R["sink"].set_property("sync", True)
                    _set_rect(R["sink"],
                              st.get("mrect", _RECT_FULL))
                    # Anchor running-time ulang untuk lanjutan leg
                    # mid-stream (probe lama sudah terpakai/terbuang).
                    _arm_anchor()
                    if st.pop("media_pending", None):
                        _media_advance()
        print(f"[display] state -> {'LIVE' if live else 'CONNECTING'}",
              flush=True)

    def probe(pad, info, st_):
        # Exception di sini membunuh streaming task secara senyap —
        # jangan pernah biarkan lolos.
        try:
            return _probe(info.get_buffer(), st_)
        except Exception as e:
            if not st_.get("err"):
                st_["err"] = 1
                print(f"[display] probe exc: {e!r}", flush=True)
            return Gst.PadProbeReturn.OK

    def _probe(buf, st_):
        if buf is None:
            return Gst.PadProbeReturn.OK
        nmem = buf.n_memory()
        if st_["n"] < 5:
            print(f"[display] buf#{st_['n']} nmem={nmem} sz={buf.get_size()}",
                  flush=True)
        st_["n"] += 1
        # Flag bitstream: CORRUPTED (0x100) = decoder menandai frame
        # rusak (referensi hilang / NAL terpotong); DISCONT (0x40) =
        # diskontinuitas — jendela resync/CRA tempat glitch lahir.
        fl = buf.flags if hasattr(buf, "flags") else 0
        try:
            fl = int(fl)
        except (TypeError, ValueError):
            fl = 0
        if fl & 0x140 and st_["n"] - st_.get("fl_log", 0) > 10:
            st_["fl_log"] = st_["n"]
            print(f"[display] buf#{st_['n']} flags=0x{fl:x}"
                  + (" CORRUPTED" if fl & 0x100 else "")
                  + (" DISCONT" if fl & 0x40 else ""), flush=True)
        # Setelah LIVE, buffer sudah terbukti sehat — memetakan 1.4MB tiap
        # frame memakan ~1 core dan menimbulkan backlog socket. Sampling
        # 1/30 tetap menangkap regresi tanpa membebani hot path.
        if st_["live"] and st_["n"] % 30:
            st_["show"] += 1
            return Gst.PadProbeReturn.OK
        blank_y = dead_uv = False
        ok, mi = buf.get_memory(0).map(Gst.MapFlags.READ)
        if ok:
            try:
                blank_y = not any(mi.data[:4096])
                if nmem == 1:
                    # NV12 kontigu: UV = sepertiga terakhir. Chroma nyata ~128,
                    # tidak mungkin seluruhnya nol -> nol = chroma mati/hijau.
                    dead_uv = not any(mi.data[int(len(mi.data) * 0.6):])
                    # Glitch "pita hijau bawah": ukur tinggi band — hitung
                    # baris Y nol berurutan dari bawah pada buffer FINAL
                    # 1080p (stride 1920 — persis yang terlihat di layar).
                    y_len = int(len(mi.data) * 2 / 3)
                    stride = 1920
                    rows = 0
                    pos = y_len
                    while pos - stride >= 0 and not any(
                            mi.data[pos - stride:pos]):
                        pos -= stride
                        rows += 1
                    if (not blank_y and rows
                            and st_["n"] - st_.get("gb_log", 0) > 5):
                        st_["gb_log"] = st_["n"]
                        print(f"[display] greenband buf#{st_['n']} "
                              f"rows={rows} flags=0x{fl:x}", flush=True)
            finally:
                buf.get_memory(0).unmap(mi)
        else:
            st_["nomap"] += 1
        if nmem >= 2 and not dead_uv:
            ok, mi = buf.get_memory(1).map(Gst.MapFlags.READ)
            if ok:
                try:
                    dead_uv = not any(mi.data[:4096])
                finally:
                    buf.get_memory(1).unmap(mi)
        if blank_y or dead_uv:
            st_["drop"] += 1
            return Gst.PadProbeReturn.DROP
        st_["show"] += 1
        set_live(True)
        return Gst.PadProbeReturn.OK

    # Probe di buffer FINAL (post crop+scale) — yang dihitung = baris
    # mati yang benar-benar terlihat di layar, bukan output decoder.
    # DIPATURKI OFF: callback Python per-buffer (map/unmap 3MB + scan)
    # lewat GIL — terukur memangkas laju ~25fps -> ~17fps = antrian
    # streamer selalu penuh -> splice -> abu. Monitoring opsional via
    # DJI_PROBE=1 bila perlu diagnosa lagi.
    def _is_green(buf):
        """True bila frame bermuatan Y kosong / chroma mati (hijau).

        Sampel hemat: Y-head 4KB + UV-tail — cukup untuk detektor, tanpa
        memindai 1.4MB/frame (probe penuh = -8fps, resync storm).
        """
        nmem = buf.n_memory()
        ok, mi = buf.get_memory(0).map(Gst.MapFlags.READ)
        if not ok:
            return False
        try:
            if not any(mi.data[:4096]):
                return True                     # Y plane kosong
            if nmem == 1:
                # NV12 kontigu: UV = sepertiga terakhir, chroma ~128.
                return not any(mi.data[int(len(mi.data) * 0.6):])
        finally:
            buf.get_memory(0).unmap(mi)
        if nmem >= 2:
            ok, mi = buf.get_memory(1).map(Gst.MapFlags.READ)
            if ok:
                try:
                    return not any(mi.data[:4096])
                finally:
                    buf.get_memory(1).unmap(mi)
        return False

    def _count_probe(pad, info, st_):
        """Hitung frame + pertahanan hijau murah.

        Green content masih bisa lolos gate hitungan murni (frame
        konvergensi pasca-resync menghitung 'show' seperti frame sah).
        Strategi: saat belum live SEMUA frame discan (gate = 40 frame
        BERSIH); saat live, sampling 1/15 + selalu saat flag
        CORRUPTED/DISCONT terbit — satu frame hijau terdeteksi ->
        drop + re-arm gate: layar kembali splash ~1.3s, bukan hijau.
        """
        try:
            st_["n"] = st_.get("n", 0) + 1
            buf = info.get_buffer()
            try:
                fl = int(buf.flags if hasattr(buf, "flags") else 0)
            except (TypeError, ValueError):
                fl = 0
            # LARANGAN KERAS: jangan map() buffer v4l2 — map/unmap pada
            # memory dmabuf decoder merusak re-queue; pool capture (~36
            # buf) kering -> decoder mati persis di buffer ke-36 (avdec
            # ~6). Scan konten hanya untuk memory sistem (avdec).
            hw = st_.setdefault(
                "hw", os.environ.get("DJI_DECODER") == "v4l2h265dec")
            bad = bool(fl & 0x100)      # CORRUPTED — decoder menandai
            if not bad and not hw:
                suspect = ((not st_["live"]) or fl & 0x40
                           or st_["n"] % 15 == 0)
                if suspect:
                    bad = _is_green(buf)
            if bad:
                st_["drop"] += 1
                if st_["live"]:
                    set_live(False)
                    st_["gate"] = st_["show"] + 40
                    print(f"[display] green frame buf#{st_['n']} "
                          f"flags=0x{fl:x} — re-gate", flush=True)
                return Gst.PadProbeReturn.DROP
            st_["show"] += 1
            # Gate adaptif (fallbackswitch-style hysteresis): rejoin yang
            # kembali <8dtk dari splash = gangguan transisi grant/subscriber
            # — referensi GOP masih hangat, konvergensi pendek, 10 frame
            # bersih cukup. Outage panjang/join dingin tetap 40 penuh.
            if (not st_["live"] and st_.get("splash_t") and
                    time.time() - st_["splash_t"] < 8.0 and
                    st_["gate"] > st_["show"] + 10):
                st_["gate"] = st_["show"] + 10
            if st_["show"] >= st_["gate"]:
                st_.pop("splash_t", None)
                set_live(True)
        except Exception as e:
            # Probe tak boleh membunuh rantai: exception di callback
            # mematikan streaming thread = wedge total (terjadi).
            print(f"[display] count_probe exc: {e!r}", flush=True)
        return Gst.PadProbeReturn.OK
    def _media_probe(pad, info, st_):
        # Buffer masuk kmssink = frame benar-benar dirender — patokan
        # liveness pipeline media (loop/smpte).
        st_["show"] += 1
        st_["live"] = True
        return Gst.PadProbeReturn.OK

    def _eos_mark(leg, gen_=0):
        # EOS bus tak menyebut leg asal (semua bermuara di kmssink yang
        # sama) — flag diset di probe ekor tiap leg, dibaca handler EOS.
        def _pr(pad, info, st_):
            ev = info.get_event()
            if ev is not None and ev.type == Gst.EventType.EOS:
                st_["eos_leg"] = leg
                if leg == "media":
                    # EOS video media terukur lewat sini; EOS agregat
                    # bin tak pernah dipost di leg ini (alsasink EOS
                    # spam ~10x, sink lain lengkap, bus diam — terukur).
                    # Gen dibekukan saat probe dipasang: EOS pipeline
                    # lama pasca-rebuild tak boleh memicu advance ganda.
                    st_["meos"] = gen_
                    # DROP: EOS yang lolos ke pad sel menempelkan status
                    # EOS permanen di pad persisten itu — buffer bin
                    # pengganti ditolak (mshow beku, terukur). Advance
                    # diurus poller meos; tak ada konsumen EOS hilir.
                    return Gst.PadProbeReturn.DROP
            return Gst.PadProbeReturn.OK
        return _pr

    def _mcount(pad, info, st_):
        # mq.src hanya mengalir saat pad media aktif = frame benar-benar
        # sampai ke selector — patokan liveness leg standby.
        st_["mshow"] = st_.get("mshow", 0) + 1
        return Gst.PadProbeReturn.OK

    def _arm_anchor():
        # Anchor running-time leg media: satu-shot BLOCK probe di ghost
        # src — menahan buffer PERTAMA leg tepat sebelum lolos ke sel,
        # memasang ts-offset = rt - pts + margin pada kmssink. Tanpa
        # ini sink men-drop frame awal dengan offset basi leg sebelumnya
        # ("buffer late, dropping") -> queue kering -> decodebin drain
        # ~2-9x realtime -> EOS prematur (terukur ~1.5-4.5dtk pd file
        # 8dtk). Dipasang ulang tiap bin baru dan tiap balik dari live:
        # probe lama mati bersama ghost lama; re-arm menangani lanjutan
        # mid-stream (pts sudah jalan -> off=rt-pts tetap tepat).
        mq_ = R.get("mqsrc")
        if mq_ is None:
            return
        pid_ = [0]

        def _a(cp, info, st_):
            try:
                b_ = info.get_buffer()
                if b_ is not None and b_.pts != Gst.CLOCK_TIME_NONE:
                    rt_ = int(R["p"].get_clock().get_time()
                              - R["p"].get_base_time())
                    off_ = max(0, rt_ - b_.pts) + 300 * Gst.MSECOND
                    R["sink"].set_property("ts-offset", off_)
                    print(f"[display] ts-offset={off_ // Gst.MSECOND}ms",
                          flush=True)
                cp.remove_probe(pid_[0])
            except Exception as e:
                print(f"[display] anchor exc: {e!r}", flush=True)
            return Gst.PadProbeReturn.OK
        pid_[0] = mq_.add_probe(
            Gst.PadProbeType.BLOCK | Gst.PadProbeType.BUFFER, _a, st)

    def _attach_probes():
        tl = R.get("tail")
        if tl is not None:
            if os.environ.get("DJI_PROBE"):
                tl.get_static_pad("src").add_probe(
                    Gst.PadProbeType.BUFFER, probe, st)
            else:
                tl.get_static_pad("src").add_probe(
                    Gst.PadProbeType.BUFFER, _count_probe, st)
            tl.get_static_pad("src").add_probe(
                Gst.PadProbeType.EVENT_DOWNSTREAM, _eos_mark("live"), st)
        sc = R.get("src")
        if sc is not None:
            # Byte-counter di sumber: show flat + byte mengalir = decoder
            # wedge (restart sah); show flat + byte diam = socket idle
            # menunggu IDR (bukan wedge — tahan splash).
            def _bytes_probe(pad, info, st_):
                st_["bytes_in"] = st_.get("bytes_in", 0) + \
                    info.get_buffer().get_size()
                return Gst.PadProbeReturn.OK

            sc.get_static_pad("src").add_probe(
                Gst.PadProbeType.BUFFER, _bytes_probe, st)
        _attach_media_probes()

    def _qos_block(pad, info, st_):
        # Event QOS dari sink hilir dilarang masuk bin media: running
        # time leg baru mulai ~0 vs clock pipeline berumur panjang —
        # sink menilai frame "hopelessly late" dan QoS menjalar ke
        # avdec -> decoder skip SELURUH sisa frame ("Dropping frame
        # due to QoS" ~190x terukur) -> hanya ~2 buffer keluar -> EOS
        # prematur ~1.5dtk -> video terpotong sebelum habis.
        ev = info.get_event()
        if ev is not None and ev.type == Gst.EventType.QOS:
            return Gst.PadProbeReturn.DROP
        return Gst.PadProbeReturn.OK

    def _attach_media_probes():
        # Probe ghost src bin media aktif: liveness (_mcount) + sinyal
        # habis-file (meos bercap generasi). Dipanggil saat boot dan
        # tiap swap bin — ghost baru tak mewarisi probe bin lama.
        mq = R.get("mqsrc")
        if mq is not None:
            mq.add_probe(Gst.PadProbeType.BUFFER, _mcount, st)
            mq.add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM,
                         _eos_mark("media", st.get("agen", 0)), st)
            mq.add_probe(Gst.PadProbeType.EVENT_UPSTREAM,
                         _qos_block, st)
            _arm_anchor()




    def reexec():
        # Ganti image proses tanpa exit: fd master diwariskan -> plane37
        # tak pernah di-restore -> nol flash console. Hanya dipakai bila
        # sesi sudah menampilkan frame (show>0): kegagalan sebelum frame
        # pertama harus exit normal supaya fallback avdec di
        # run_display.sh bisa menilai sesi ini gagal.
        print("[display] reexec — respawn tanpa lepas master", flush=True)
        if drm_fd is not None:
            os.set_inheritable(drm_fd, True)
            os.environ["DJI_DRM_FD"] = str(drm_fd)
        # Tutup semua fd selain stdio+drm: libv4l2 membuka /dev/video0
        # TANPA CLOEXEC sehingga fd decoder lama diwarisi execv —
        # konteks+buffer CMA-nya tetap dipegang kernel -> instance baru
        # "Failed to allocate required memory" -> reexec -> +1 fd =
        # spiral mati (terukur: 5 fd video0 per proses, 2 non-CLOEXEC).
        # Soket :9990 ikut tertutup -> streamer resubscribe = join segar.
        for f in os.listdir("/proc/self/fd"):
            try:
                fd = int(f)
            except ValueError:
                continue
            if fd <= 2 or fd == drm_fd:
                continue
            try:
                os.close(fd)
            except OSError:
                pass
        os.execv(sys.executable, [sys.executable] + sys.argv)

    loop = GLib.MainLoop()

    def on_msg(bus_, msg):
        t = msg.type
        src_el = msg.src
        if src_el is not None and R.get("p") is not None:
            # Pesan bus elemen mati yang masih melayang (pipeline/bin
            # lama pasca-swap) — jangan diproses dua kali. Jalan
            # ancestor, bukan get_parent(): elemen dalam mbin
            # ber-parent bin, bukan pipeline, tapi tetap sah.
            anc = src_el
            while anc is not None and anc is not R["p"]:
                anc = anc.get_parent()
            if anc is None:
                return
        if t == Gst.MessageType.ERROR:
            err, _ = msg.parse_error()
            src = src_el.get_name() if src_el is not None else "?"
            print(f"[display] ERROR({src}):", err.message, flush=True)
            if mode == "auto" and not st["live"] and \
                    src_el is R.get("src"):
                # :9990 tak listen (streamer mati/UDC menunggu RC):
                # leg live rontok sendirian, promo/splash tetap jalan —
                # retry lewat _live_retry saat port kembali, bukan
                # respawn-loop yang mengedipkan layar tiap ~2 dtk.
                st["live_dead"] = True
                return
            if (mode == "loop" or standby_loop) and \
                    "alsa" in src.lower():
                # Device audio gagal -> build berikut tanpa leg audio
                # (file /tmp hilang saat reboot = auto-retry).
                try:
                    open("/tmp/dji_loop_no_audio", "w").write("1")
                except OSError:
                    pass
            reexec() if st["show"] else loop.quit()
        elif t == Gst.MessageType.EOS:
            if mode == "loop":
                _loop_advance()
                return
            via_video = st.pop("eos_leg", None) == "media"
            # EOS sink milik leg media diidentifikasi lewat pemilik;
            # dead_sinks = tombstone satu generasi untuk EOS bus yang
            # masih in-flight dari bin yang baru dilepas (di dalam bin
            # EOS anak dimakan bin untuk agregat — jarang tiba, tapi
            # salah klasifikasi = restart palsu). Selain itu = live.
            leg = ("media" if (via_video or
                               src_el in R.get("msinks", ()) or
                               src_el in st.get("dead_sinks", ()) or
                               (standby_loop and not st["live"] and
                                src_el is R.get("sink")))
                   else "live")
            if standby_loop and leg == "media":
                if st["live"]:
                    # File habis "di belakang" saat live (audio/fakesink
                    # tetap mengalir) — advance ditunda sampai standby.
                    st["media_pending"] = True
                    return
                if via_video:
                    # EOS lewat ekor video (probe mq.src) = file habis.
                    _media_advance()
                # Tanpa flag video: EOS agregat tak dapat dipercaya di
                # leg ini — advance diurus poller meos.
                return
            print("[display] EOS", flush=True)
            reexec() if st["show"] else loop.quit()
        elif t == Gst.MessageType.WARNING:
            err, _ = msg.parse_warning()
            print("[display] WARN:", err.message, flush=True)
        elif t == Gst.MessageType.ASYNC_DONE:
            print("[display] ASYNC_DONE", flush=True)

    def _boot_pipeline(idx=0):
        # Bangun ulang pipeline selector utuh: leg live + bin media
        # standby (file idx) + leg splash. Hanya untuk cold-start dan
        # eskalasi wedge — advance playlist memakai swap bin
        # (_media_advance) agar leg live tak ikut mati.
        nonlocal p
        st["midx"] = idx % len(mfiles) if mfiles else 0
        mpath = mfiles[st["midx"]] if standby_loop else None
        old = R.get("p")
        if old is not None:
            try:
                old.set_state(Gst.State.NULL)
            except Exception:
                pass
        R.clear()
        R.update(build(drm_fd, mpath))
        p = R["p"]
        # Generasi dinaikkan SEBELUM probe dipasang — marker meos
        # membekukan gen ini sebagai bukti EOS milik pipeline terkini.
        st["agen"] = st.get("agen", 0) + 1
        _attach_probes()
        R["sel"].set_property(
            "active-pad", R["pad_media"] or R["pad_splash"])
        if standby_loop:
            # sync=true + anchor probe (_arm_anchor lewat
            # _attach_probes): leg media dipacu clock dengan basis
            # digeser ts-offset tepat sebelum frame pertama lolos.
            R["sink"].set_property("sync", True)
            st["mrect"] = _fit_rect(mpath)
            _set_rect(R["sink"], st["mrect"])
            if R.get("vol") is not None:
                R["vol"].set_property("volume", 1.0)
            _arm_photo(mpath)
        b2 = p.get_bus()
        b2.add_signal_watch()
        b2.connect("message", on_msg)
        st.pop("eos_leg", None)
        st.pop("mblock", None)
        st.pop("meos", None)
        st["live_dead"] = False
        p.set_state(Gst.State.PLAYING)
        print(f"[display] pads splash={R['pad_splash'].get_name()} "
              f"video={R['pad_video'].get_name()} "
              f"media={R['pad_media'].get_name() if R['pad_media'] else '-'}",
              flush=True)

    def _arm_photo(path):
        # imagefreeze tak pernah EOS — timer ini sinyal habis-foto
        # pengganti: menulis meos dengan gen bersenjata sehingga poller
        # meos memakai jalur advance yang sama dengan EOS video
        # (media_pending saat live ikut gratis). gen basi yang lolos
        # setelah advance lain otomatis tak cocok di _mpoll.
        # Timer dihitung dari frame pertama yang lolos ghost (= foto
        # benar-benar tampil), BUKAN dari build: preroll leg foto ~1.7dtk
        # (terukur) sehingga timer sejak build membuat foto 5dtk hanya
        # tampil ~3.5dtk. Probe dipasang pemanggil sebelum ghost link.
        tid = st.pop("img_timer", None)
        if tid:
            GLib.source_remove(tid)
        if not path or not _is_img(path):
            return
        try:
            secs = int(_load_cfg().get("photo_secs") or 5)
        except (ValueError, TypeError):
            secs = 5
        secs = min(3600, max(1, secs))
        gen = st["agen"]

        def _fire():
            st["img_timer"] = None
            st["meos"] = gen
            return False

        mq_ = R.get("mqsrc")
        if mq_ is None:
            st["img_timer"] = GLib.timeout_add(int(secs * 1000), _fire)
            return
        pid_ = [0]

        def _arm_first(cp, info, st_):
            try:
                # timeout_add (ms), bukan _seconds: timer resolusi detik
                # GLib boleh fire ~1dtk lebih awal demi coalescing.
                st["img_timer"] = GLib.timeout_add(
                    int(secs * 1000), _fire)
            except Exception as e:
                print(f"[display] img_timer exc: {e!r}", flush=True)
            try:
                cp.remove_probe(pid_[0])
            except Exception:
                pass
            return Gst.PadProbeReturn.OK
        pid_[0] = mq_.add_probe(Gst.PadProbeType.BUFFER, _arm_first, st)

    def _mfiles_rescan():
        # mfiles dimuat sekali saat build — tanpa ini file hasil
        # upload UI tak masuk rotasi sampai display restart. Dipanggil
        # saat playlist menyelesaikan satu putaran.
        try:
            cand = _loop_ordered(f for f in os.listdir("/root/loops")
                                 if media_file_ext(f))
            if cand:
                mfiles[:] = [os.path.join("/root/loops", f) for f in cand]
                st["mfiles"] = mfiles
        except OSError:
            pass

    def _media_advance():
        # Swap bin media saja: leg live, decoder v4l2, socket :9990 dan
        # gate tetap hidup — rebuild penuh per file terukur membunuh
        # spin-up decoder (gate tak pernah lolos pada file pendek).
        st["loops"] = st.get("loops", 0) + 1
        nxt = st.get("midx", 0) + 1
        if nxt >= len(mfiles):
            _mfiles_rescan()
            nxt = 0
        path = mfiles[nxt]
        st["midx"] = nxt
        print(f"[display] standby loop #{st['loops']} -> "
              f"{os.path.basename(path)}", flush=True)
        # Catatan terukur: seek_simple FLUSH|SEGMENT pada bin yang
        # sama (loop 1 file tanpa rebuild) ret=True tapi pipeline tak
        # pernah mengalir lagi di gst ini — pos beku, leg audio ikut
        # EOS. Jangan dicoba ulang; swap bin adalah jalur yang benar.

        # Sink bin lama jadi tombstone agar EOS bus in-flight tak
        # diklasifikasi live; probe block ikut mati bersama bin.
        st["dead_sinks"] = R.get("msinks", set())
        st.pop("mblock", None)
        old = R.get("mbin")
        pp = R.get("p")
        if old is not None:
            gs = old.get_static_pad("src")
            if gs is not None and gs.is_linked():
                gs.unlink(gs.get_peer())
            # set_state(NULL) pada bin PLAYING dari main-loop macet
            # menunggu task internal yang masih terparkir di chain
            # (terukur: hang >25dtk, stack faulthandler). Teardown
            # pindah ke thread sendiri — bin sudah ter-unlink sehingga
            # selesai kapan pun tanpa efek pada leg lain.
            def _teardown(b, pipe, dec, paid):
                # File korup yang tak pernah memancarkan no-more-pads
                # menyisakan handler pad-added: siklus closure->bin
                # menahan seluruh bin hidup (leak terukur). Putus di
                # sini sebagai belt terakhir.
                try:
                    if dec is not None and paid and paid[0]:
                        dec.disconnect(paid[0])
                        paid[0] = 0
                except Exception:
                    pass
                try:
                    b.set_state(Gst.State.NULL)
                except Exception:
                    pass
                try:
                    pipe.remove(b)
                except Exception:
                    pass
            threading.Thread(target=_teardown,
                             args=(old, pp, R.get("mdec"),
                                   R.get("mpa")),
                             daemon=True).start()
        # Generasi dinaikkan SEBELUM probe bin baru dipasang — marker
        # meos membekukannya sebagai bukti EOS milik bin terkini.
        st["agen"] = st.get("agen", 0) + 1
        mbin, ghost, mleg = _build_mbin(
            R["p"], _load_cfg(), path, st["agen"])
        R.update(mbin=mbin, mqsrc=ghost, mqblock=ghost,
                 vol=mleg["vol"], msinks=mleg["sinks"],
                 mdec=mleg.get("dec"), mpa=mleg.get("pa_id"))
        # _arm_photo SEBELUM link: probe-nya menunggu buffer pertama
        # leg — harus terpasang sebelum ada jalan data melewatinya.
        _arm_photo(path)
        ghost.link_full(R["pad_media"], Gst.PadLinkCheck.HIERARCHY)
        _attach_media_probes()
        mbin.sync_state_with_parent()
        st["mrect"] = _fit_rect(path)
        if not st["live"]:
            _set_rect(R["sink"], st["mrect"])
            if R["vol"] is not None:
                R["vol"].set_property("volume", 1.0)
        # Anchor running-time leg baru sudah terpasang di ghost-nya
        # lewat _attach_media_probes -> _arm_anchor (satu-shot, aktif
        # tepat sebelum buffer pertama lolos — tanpa jendela offset
        # basi).

    def _loop_advance():
        # Playlist mode loop: rebuild per file (bukan seek —
        # v4l2h264dec merusak ~70 frame + state pasca-seek ACCURATE,
        # terukur "eos sent too early" -> loop beku di putaran kedua).
        st["loops"] = st.get("loops", 0) + 1
        mf = st.get("mfiles") or [""]
        nxt = st.get("midx", 0) + 1
        if nxt >= len(mf):
            _mfiles_rescan()
            mf = st.get("mfiles") or [""]
            nxt = 0
        print(f"[display] loop #{st['loops']} -> "
              f"{os.path.basename(mf[nxt])}", flush=True)
        _start_media(nxt)

    def _start_media(idx):
        # Pipeline media dibongkar-pasang per file (playlist) — elemen
        # demux/decode lama dibuang bersama pipeline, tak ada leak.
        nonlocal p
        if p is not None:
            try:
                p.set_state(Gst.State.NULL)
            except Exception:
                pass
        path = st["mfiles"][idx % len(st["mfiles"])]
        p, tail, _md = build_media(drm_fd, mode, _cfg0, path)
        R["p"] = p       # guard parent pesan bus lama di on_msg
        st["agen"] = st.get("agen", 0) + 1
        tail.get_static_pad("sink").add_probe(
            Gst.PadProbeType.BUFFER, _media_probe, st)
        # EOS video di pad sink = file habis — jalan alternatif bila
        # EOS agregat bin macet seperti di leg standby (terukur).
        tail.get_static_pad("sink").add_probe(
            Gst.PadProbeType.EVENT_DOWNSTREAM,
            _eos_mark("media", st["agen"]), st)
        b2 = p.get_bus()
        b2.add_signal_watch()
        b2.connect("message", on_msg)
        st["midx"] = idx % len(st["mfiles"])
        _arm_photo(path)
        p.set_state(Gst.State.PLAYING)
        print(f"[display] media#{st['midx']} "
              f"{os.path.basename(path) if path else mode}", flush=True)

    def tick():
        # Frame berhenti -> kembali ke splash (tidak exit: decoder tetap
        # menghisap socket, streamer boleh restart lalu video balik).
        # Mati total >90 dtk -> exit, run_display.sh menyambung ulang.
        shown = st["show"]
        print(f"[display] show={shown} drop={st['drop']} "
              f"live={st['live']}"
              + (f" mshow={st.get('mshow', 0)}" if standby_loop else ""),
              flush=True)
        if shown == st["last"]:
            if st["live"]:
                set_live(False)
                # Re-arm: butuh 40 frame BARU sebelum LIVE lagi — frame
                # pertama pasca-stall adalah konvergensi hijau lagi.
                # splash_t menandai outage untuk gate adaptif di probe.
                st["gate"] = shown + 40
                st["splash_t"] = time.time()
            if standby_loop and not st["live"]:
                # Standby memutar media: kesehatan = frame leg media maju.
                # Socket 9990 boleh diam berjam-jam saat RC tercabut —
                # logika wedge/idle di bawah tak relevan di sini.
                ms = st.get("mshow", 0)
                if ms != st.get("mlast", -1):
                    st["dead"] = st["idle"] = 0
                    st["mlast"] = ms
                    st["mresc"] = 0
                else:
                    st["mstall"] = st.get("mstall", 0) + 1
                    if st["mstall"] > 2:
                        st["mstall"] = 0
                        st["mresc"] = st.get("mresc", 0) + 1
                        if st["mresc"] >= 3:
                            # Advance 3x berturut tak memulihkan —
                            # wedge lebih dalam dari leg media.
                            print("[display] standby stall — rebuild "
                                  "pipeline", flush=True)
                            st["mresc"] = 0
                            _boot_pipeline(st.get("midx", 0))
                        else:
                            # File korup macet di titik sama — rebuild
                            # file yang sama = loop macet; ke file
                            # berikut menyembuhkan diri.
                            print("[display] standby stall — file "
                                  "berikut", flush=True)
                            _media_advance()
                st["last"] = shown
                return True
            if mode != "auto":
                # Loop/pattern tak punya socket 9990 untuk didiagnosa —
                # frame diam berarti leg macet/decode gagal -> restart.
                st["dead"] = st.get("dead", 0) + 1
                if st["dead"] > 3:
                    print("[display] leg non-auto stall — hard exit",
                          flush=True)
                    sys.stdout.flush()
                    if st["show"]:
                        reexec()
                    os._exit(0)
                st["last"] = shown
                return True
            # Dua hitungan terpisah:
            #  dead = frame mati SAAT bytes mengalir = wedge decoder
            #         sungguhan -> hard-exit ~45-60 dtk.
            #  idle = upstream kelaparan (replug/replay, link mati,
            #         menunggu IDR) = BUKAN wedge — exit cuma melepas
            #         master -> flash console. Tahan splash; plafon
            #         ~15 menit untuk socket half-open yang EOS-nya
            #         tak pernah sampai.
            try:
                udc = "configured"
                for f in os.listdir("/sys/class/udc"):
                    udc = open(
                        f"/sys/class/udc/{f}/state").read().strip()
                    break
            except OSError:
                pass
            flowing = st.get("bytes_in", 0) != st.get("bytes_last", -1)
            st["bytes_last"] = st.get("bytes_in", 0)
            cw, rxq = _sock_stats()
            if not flowing and cw:
                print("[display] socket 9990 CLOSE-WAIT — streamer "
                      "mati di belakang kita, re-attach", flush=True)
                if st["show"]:
                    reexec()
                sys.stdout.flush()
                os._exit(0)
            # Wedge hilir menyamar: decoder/sink tersendat -> push macet
            # -> socket tak dibaca -> rxq penuh + bytes diam = BUKAN
            # starvation. Tanpa cek rxq, kondisi ini ditahan splash
            # selamanya (terjadi: avdec macet pasca-join CRA, show=6).
            if not flowing and rxq > 0:
                st["idle"] = 0
                st["dead"] += 1
                if st["dead"] > 3:
                    print(f"[display] wedge hilir rxq={rxq}B "
                          "bytes=idle — hard exit", flush=True)
                    sys.stdout.flush()
                    if st["show"]:
                        reexec()
                    os._exit(0)
                st["last"] = shown
                return True
            drought = False
            try:
                drought = (time.time() - os.stat(
                    "/tmp/dji_idr_drought").st_mtime) < 90
            except OSError:
                pass
            if udc != "configured" or drought or not flowing:
                st["dead"] = 0
                st["idle"] = st.get("idle", 0) + 1
                if st["idle"] == 4 or st["idle"] % 20 == 0:
                    print(f"[display] dead — "
                          f"{'drought' if drought else 'UDC=' + udc}"
                          f" bytes={'flow' if flowing else 'idle'}"
                          " — tahan splash", flush=True)
                # Plafon socket half-open hanya masuk akal saat link
                # HIDUP: saat RC tercabut, socket ESTAB ke streamer
                # yang menunggu UDC = menunggu sah — exit cuma flash.
                if st["idle"] > 60 and udc == "configured":
                    print("[display] idle >15m — hard exit", flush=True)
                    sys.stdout.flush()
                    os._exit(0)
            else:
                st["idle"] = 0
                st["dead"] += 1
                if st["dead"] > 3:  # ~45-60 dtk wedge sungguhan
                    # Decoder/KMS wedged: teardown normal (set_state NULL
                    # / EOS) ikut hang di v4l2 yang macet — proses jadi
                    # zombie permanen. os._exit menjamin restart.
                    print("[display] dead >45s — wedge, hard exit "
                          "(bytes=flow)", flush=True)
                    sys.stdout.flush()
                    os._exit(0)
        else:
            st["dead"] = st["idle"] = 0
        st["last"] = shown
        return True

    def _mpoll():
        # EOS bus agregat tak pernah tiba di leg standby (terukur) —
        # satu-satunya sinyal habis-file yang sah adalah flag meos
        # dari probe mq.src. Poll di main loop: advance/pending aman.
        g = st.pop("meos", None)
        if g is not None:
            if standby_loop:
                if st["live"]:
                    st["media_pending"] = True
                elif g == st.get("agen"):
                    _media_advance()
            elif mode == "loop" and g == st.get("agen"):
                _loop_advance()
        return True

    def _live_retry():
        # Leg live rontok (socket ditolak) saat standby: promo tak
        # pernah berhenti — yang direbuild hanya saat :9990 listen
        # lagi. /proc/net/tcp lokal :2706 st=0A = LISTEN, tanpa efek
        # samping koneksi percobaan ke VidServer.
        if mode == "auto" and st.get("live_dead") and not st["live"]:
            try:
                up = any(
                    l.split()[1].endswith(":2706") and
                    l.split()[3] == "0A"
                    for l in open("/proc/net/tcp").readlines()[1:])
            except OSError:
                up = False
            if up:
                print("[display] :9990 listen lagi — rebuild leg",
                      flush=True)
                _boot_pipeline(st.get("midx", 0))
        return True

    st["last"], st["dead"] = 0, 0
    t_boot = time.time()
    def _gc_tick():
        # GstObject wrapper + closure membentuk siklus yang hanya
        # tertagih gc generasi; tanpa pemicu berkala RSS merayap
        # per swap bin (terukur ~1-2MB/swap di S905X).
        gc.collect()
        return True
    GLib.timeout_add(250, _mpoll)
    GLib.timeout_add_seconds(5, _live_retry)
    GLib.timeout_add_seconds(15, tick)
    GLib.timeout_add_seconds(60, _gc_tick)

    def sec_tick():
        if mode != "auto" or st["live"] or not _show_ind or \
                R.get("txtc") is None:
            return True
        # Spinner braille + indikator per-lapis: USB = UDC configured;
        # LINK = duml_rx maju; VID = paket video maju; DEC = decoder
        # menelurkan frame. Satu pandangan menjawab "lapis mana yang
        # mati" tanpa SSH.
        spin = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[int(time.time() * 8) % 10]
        # Indikator per-lapis: USB = UDC configured; LINK = duml_rx maju;
        # VID = paket video maju; DEC = decoder menelurkan frame. Satu
        # pandangan menjawab "lapis mana yang mati" tanpa SSH.
        usb = link = vid = False
        try:
            for f in os.listdir("/sys/class/udc"):
                usb = open(f"/sys/class/udc/{f}/state"
                           ).read().strip() == "configured"
                break
        except OSError:
            pass
        try:
            kv = dict(p.split("=") for p in
                      open("/tmp/dji_link_state").read().split())
            rx, v, t = int(kv["rx"]), int(kv["vid"]), int(kv["t"])
            fresh = time.time() - t < 45
            prev = st.get("link_prev", (0, 0))
            st["link_prev"] = (rx, v)
            link = fresh and rx > 0 and rx > prev[0]
            vid = fresh and v > 0 and v > prev[1]
            if not st.get("link_prev_seen"):
                st["link_prev_seen"] = True
                link = vid = False    # sampel pertama belum bisa dibanding
        except (OSError, KeyError, ValueError):
            pass
        dec = st["live"]
        s = (f"{spin} USB{'●' if usb else '○'} LINK{'●' if link else '○'} "
             f"VID{'●' if vid else '○'} DEC{'●' if dec else '○'}  "
             f"{int(time.time() - t_boot)}s")
        R["txtc"].set_property("text", s)
        return True

    GLib.timeout_add_seconds(1, sec_tick)

    if mode == "auto":
        _boot_pipeline(0)
        print("[display] PLAYING", flush=True)
    else:
        _start_media(0)
    try:
        loop.run()
    except KeyboardInterrupt:
        pass
    p.set_state(Gst.State.NULL)
    # CATATAN: plane 40 tidak bisa dimatikan dari proses ini saat exit —
    # kmssink masih memegang DRM master sampai fd-nya tertutup saat proses
    # berakhir. Pembersihan fb basi dilakukan plane40_off.py dari
    # run_display.sh setelah proses ini benar-benar mati.


if __name__ == "__main__":
    main()
