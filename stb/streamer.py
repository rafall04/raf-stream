#!/usr/bin/env python3
"""Автономный стример HEVC для RC-N1 на HG680P — без телефона.

Топология:

    RC-N1 --AOA--> [dji-bridge gadget :9910] <--TCP-- [этот скрипт] --> HEVC

RC видит наш гаджет как «телефон». Дальше мы воспроизводим поток команд,
который настоящая сессия DJI Fly отправляла на command-порт LogicLink
(0x5749) — точные пакеты, заставившие RC-N1 открыть видеопорт (0x574a), —
и пишем сырой HEVC Annex-B в stdout.

Проверено на Mini 3: порт 0x574a несёт чистый HEVC без обёрток;
ffprobe видит hevc Main 1280x720.

Запуск на STB:
    python3 streamer.py | ffmpeg -i - -c copy -f rtsp rtsp://127.0.0.1:8554/dji
    python3 streamer.py --dump v.hevc            # запись в файл вместо stdout
    python3 streamer.py --host 127.0.0.1 --max-gap 0.5
"""
import argparse
import errno
import fcntl
import json
import os
import queue
import select
import socket
import struct
import sys
import threading
import time

VID_PORT = 0x574A
MAGIC = b"\x55\xcc"


class SinkWriter:
    """Пишет HEVC в FIFO или файл через очередь + поток-насос.

    RX-поток только кладёт пакеты в очередь (~6 с видео в RAM), насос
    пишет блокирующе: ни одного EAGAIN-дропа посреди GOP. Байты теряются
    только если читатель мёртв дольше ~2 с — тогда выходим на resync по
    IDR вместо того чтобы скармливать декодеру битый поток.
    """

    def __init__(self, path, log):
        self.path = path
        self.log = log
        # ~1 s video di RAM: cukup menahan restart gst, tapi latency
        # tak pernah tumbuh — kelebihan dibuang ke IDR berikutnya.
        self.q = queue.Queue(maxsize=300)
        self.synced = False
        self.dropped = 0
        threading.Thread(target=self._pump, daemon=True).start()

    def write(self, data):
        try:
            self.q.put_nowait(data)
        except queue.Full:
            self.dropped += 1
            self.synced = False

    def flush(self):
        pass

    def _pump(self):
        fd = None
        self._fd = None
        tail = b""
        while True:
            data = self.q.get()
            if not self.synced:
                # вход в поток только на AU с IDR: иначе декодер стартует
                # в середине GOP и сереет/не выводит вовсе
                seg = _find_idr_start(tail, data)
                if seg is None:
                    tail = data[-512:]
                    continue
                tail = b""
                data = seg
                self.synced = True
            else:
                tail = data[-512:]
            stall = 0
            while data:
                if fd is None:
                    try:
                        fd = os.open(self.path,
                                     os.O_WRONLY | os.O_NONBLOCK | os.O_CREAT)
                        try:
                            # Pipe kecil = plafon latency di kernel
                            # (~0.45 s pada 4.5 Mbps); sisanya antre di RAM.
                            fcntl.fcntl(fd, fcntl.F_SETPIPE_SZ, 256 * 1024)
                        except OSError:
                            pass
                        self._fd = fd
                        # Pembaca baru selalu mulai dari IDR: decoder hw
                        # abort permanen jika disuapi tengah GOP.
                        self.synced = False
                        print("[streamer] video sink attached",
                              file=self.log, flush=True)
                    except OSError:
                        fd = None
                        self._fd = None
                        time.sleep(0.05)
                        continue
                try:
                    # Soft-cap backlog kernel: jangan biarkan video basi
                    # menumpuk di pipe. F_SETPIPE_SZ tak bisa mengecilkan
                    # pipe yang masih berisi (EBUSY), jadi batasi di sisi
                    # penulis — latency di pipe tak pernah >~0.25 s.
                    pend = struct.unpack(
                        "i", fcntl.ioctl(fd, 0x541B,
                                         struct.pack("i", 0)))[0]
                    if pend > 128 * 1024:
                        stall += 1
                        if stall > 40:
                            self.dropped += 1
                            self.synced = False
                            break
                        select.select([], [fd], [], 0.05)
                        continue
                    n = os.write(fd, data)
                    data = data[n:]
                    stall = 0
                except BlockingIOError:
                    stall += 1
                    if stall > 20:
                        # читатель не сосёт ~2 с: поток внутри GOP уже
                        # потерял часть — бросаем хвост, resync на IDR
                        self.dropped += 1
                        self.synced = False
                        break
                    select.select([], [fd], [], 0.1)
                except OSError:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    fd = None
                    self._fd = None
                    break


class VidServer:
    """HEVC via TCP localhost — fan-out multi-klien, stream ter-scoped
    per koneksi.

    Kelemahan fatal FIFO: objek pipe bertahan selama ada pembaca, jadi
    reader baru mewarisi byte mid-GOP basi -> decoder hw abort -> loop
    restart. TCP: koneksi baru = buffer kosong, dan payload pertama selalu
    IDR. Tiap klien punya queue+sender sendiri: klien lambat (RTSP relay,
    recorder) tak menghambat display. Backpressure per-klien dikekang
    queue (~0.5 s); kelebihan dibuang ke IDR berikutnya — latency tidak
    pernah tumbuh.
    """

    def __init__(self, host, port, log):
        self.log = log
        self.clients = []
        self.dropped = 0
        self.nals = {}
        self.on_attach = None
        # Seed AU utuh (VPS..IDR..ekor AU) yang persisten: kekeringan
        # IDR bisa bermenit-menit (miss>41000) dan cache rolling 1MB
        # hanya menjangkau ~7 detik — klien yang attach di tengah
        # kekeringan mendapat seed dari IDR terakhir BERAPAPUN umurnya.
        self.seed = bytearray()
        self._seed_new = bytearray()
        self.seed_open = False
        self._prev = b""
        # Filter AU-IDR degenerat: encoder memancarkan AU "IDR" ~2KB
        # tepat sesudah video-port reopen (tiap resubscribe). meson-vdec
        # memakannya -> dec_status 0xF -> sesi decoder mati -> display
        # restart -> resubscribe -> tiny-IDR baru = loop maut. NAL VCL
        # AU sekecil itu dibuang; head params-nya tetap diteruskan (AU
        # tanpa VCL sah), jadi stream berikutnya tetap mulus.
        self._hold = bytearray()   # byte belum diteruskan (>= pend/ekor)
        self._abs = 0              # offset stream absolut buf[0]
        self._scanned = 0          # watermark posisi yang sudah dinilai
        self._pend_fs = None       # first-slice IDR yang sedang ditahan
        self._pend_head = 0        # head AU yang memuatnya (absolut)
        self._pend_slices = 0      # NAL 0x26/0x28 di AU tertahan
        self._last_head = None
        self.hlock = threading.Lock()
        threading.Thread(target=self._accept, args=(host, port),
                         daemon=True).start()

    def write(self, data):
        buf = bytes(self._hold) + data
        sbase = self._abs
        self._abs += len(buf)
        del self._hold[:]
        # Penilaian AU hanya pada byte baru (>= _scanned); 6 byte ekor
        # ditahan agar header yang terpotong batas chunk tak terlewat.
        lo = max(0, self._scanned - sbase)
        hi = len(buf) - 6
        drops = []
        i = -1
        while True:
            i = buf.find(b"\x00\x00\x01", i + 1)
            if i < 0 or i + 3 >= len(buf) or i >= hi:
                break
            if i < lo:
                continue
            t = buf[i + 3]
            self.nals[t] = self.nals.get(t, 0) + 1
            # Header NAL asli = <type<<1|0> 0x01 — tanpa byte kedua 0x01,
            # pola 00 00 01 26 di payload/record DJI = false positive.
            fs = (t in (0x26, 0x28) and i + 4 < len(buf)
                  and buf[i + 4] == 0x01 and _is_first_slice(buf, i))
            # Head AU = SEI-suffix/VPS di awal AU, atau first-slice IDR
            # (AU bare-IDR mulai langsung di slice-nya). Tiap head
            # menutup AU sebelumnya -> menilai AU tersangka.
            if t in (0x40, 0x41, 0x50) or fs:
                h = sbase + i
                if self._pend_fs is not None:
                    # IDR asli DJI: AU >= 41KB atau 3 slice (multi-slice,
                    # slice lanjutan pakai start-code 4 byte). Degenerat
                    # pasca-reopen: <=10KB, 1 slice — tapi ada varian
                    # ~10KB yang lolos filter 16KB (abort 10:07:03) jadi
                    # dua syarat: kecil DAN single-slice = buang.
                    if (h - self._pend_head < 32768
                            and self._pend_slices < 3):
                        drops.append((self._pend_fs, h))
                        print(f"[streamer] drop deg-IDR AU "
                              f"{self._pend_fs}..{h}",
                              file=self.log, flush=True)
                    self._pend_fs = None
                    self._pend_slices = 0
                if fs:
                    self._pend_fs = h
                    self._pend_head = (self._last_head
                                       if self._last_head is not None else h)
                    self._pend_slices = 0
                self._last_head = h
            # Hitung slice IDR di dalam AU tertahan (fs sendiri dihitung
            # saat AU dibuka di atas — NAL-nya termasuk wilayah ini).
            if (self._pend_fs is not None
                    and t in (0x26, 0x28) and i + 4 < len(buf)
                    and buf[i + 4] == 0x01):
                self._pend_slices += 1
        self._scanned = sbase + hi
        end_abs = sbase + len(buf)
        # Teruskan byte sampai awal wilayah tahan; sisanya masuk _hold.
        hold_from = self._pend_fs if self._pend_fs is not None \
            else sbase + hi
        fwd_end = min(hold_from, end_abs)
        out = bytearray()
        pos = sbase
        for a, b in drops:
            a2, b2 = max(a, sbase), min(b, fwd_end)
            if a2 > pos:
                out += buf[pos - sbase:a2 - sbase]
            pos = max(pos, b2)
        if pos < fwd_end:
            out += buf[pos - sbase:fwd_end - sbase]
        self._hold += buf[fwd_end - sbase:]
        data = bytes(out)
        if not data:
            return
        idr_here = False
        # Scan ulang byte yang DITERUSKAN (overlap 8B via _prev): hanya
        # first-slice IDR yang lolos filter yang boleh memicu seed.
        fbuf = self._prev[-8:] + data
        while True:
            i = fbuf.find(b"\x00\x00\x01", i + 1)
            if i < 0 or i + 3 >= len(fbuf):
                break
            if (fbuf[i + 3] in (0x26, 0x28) and i + 4 < len(fbuf)
                    and fbuf[i + 4] == 0x01 and _is_first_slice(fbuf, i)):
                idr_here = True
        if idr_here:
            print("[streamer] IDR chunk len=%d cli=%s"
                  % (len(data),
                     ["#%d%s" % (c["id"],
                                 "S" if c["synced"] else "u")
                      for c in self.clients]),
                  file=self.log, flush=True)
            # IDR asli baru -> bangun seed segar dari VPS terdekatnya
            # (lookback via tail chunk lalu); chunk berikutnya di-append
            # sampai AU utuh (~768KB cap). Klien yang SUDAH attach saat
            # kekeringan ikut menerima seg ini langsung — jangan biarkan
            # menunggu jendela berikutnya padahal seed baru saja dibuat.
            with self.hlock:
                seg = _find_last_idr(self._prev + data)
                if seg is not None:
                    # Rekam ke buffer terpisah: self.seed hanya memuat
                    # seed yang SUDAH LENGKAP. Melayani seed setengah-
                    # rekam (IDR terpotong) = decoder error -> display
                    # exit -> "kembali ke console". Seed lama yang utuh
                    # selalu lebih baik daripada seed baru yang separo.
                    self._seed_new = bytearray(seg)
                    self.seed_open = True
                    try:
                        open("/tmp/dji_seed_ok", "w").write("1")
                    except OSError:
                        pass
                    for c_ in self.clients:
                        if not c_["synced"]:
                            try:
                                # Kirim seed sebagai item bertanda —
                                # _sender mengirimnya verbatim lalu tetap
                                # memindai first-slice IDR live (splice
                                # mid-GOP tak pernah terjadi -> v4l2 aman).
                                c_["q"].put_nowait(("seed", bytes(seg)))
                            except queue.Full:
                                pass
        elif self.seed_open:
            with self.hlock:
                # Tutup rekaman pada BATAS AU (VPS berikutnya) setelah
                # cap — memotong mid-AU meninggalkan fragment slice
                # tanpa konteks di ekor seed.
                au = (data.find(b"\x00\x00\x01\x40")
                      if len(self._seed_new) > 768 * 1024 else -1)
                if au >= 0:
                    self._seed_new += data[:au]
                    self.seed = self._seed_new
                    self.seed_open = False
                else:
                    self._seed_new += data
                    if len(self._seed_new) > 1536 * 1024:
                        self.seed = self._seed_new
                        self.seed_open = False
        self._prev = data[-65536:]
        for cl in list(self.clients):
            try:
                cl["q"].put_nowait(data)
            except queue.Full:
                self.dropped += 1
                print(f"[streamer] client #{cl['id']} q-full -> "
                      f"resync (drops={self.dropped})",
                      file=self.log, flush=True)
                # Kuras backlog lalu tandai unsynced: _sender memindai
                # first-slice IDR LIVE berikutnya dan menyambung dari
                # VPS-AU utuh — delay ~periode-IDR, tak pernah replay
                # video basi. (Reseed via seed menghasilkan video BERSIH
                # tapi berumur panjang seed (~6 s): klien lambat tak
                # pernah mengejar live edge -> loop replay = "stuck".)
                try:
                    while True:
                        cl["q"].get_nowait()
                except queue.Empty:
                    pass
                cl["synced"] = False

    def flush(self):
        pass

    def _accept(self, host, port):
        # Bind diretry tanpa batas: konflik port sesaat (instance lama
        # belum mati saat Restart=always, atau tap/debug sementara)
        # pernah membunuh thread ini permanen — service "active" tapi
        # :9990 tak pernah listen lagi sampai restart manual (terukur:
        # display respawn-loop tanpa henti saat RC tercabut).
        while True:
            try:
                srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                srv.bind((host, port))
                srv.listen(4)
                break
            except OSError:
                print(f"[streamer] vid-srv bind :{port} bentrok — "
                      "retry 2s", file=self.log, flush=True)
                try:
                    srv.close()
                except Exception:
                    pass
                time.sleep(2)
        self._next_id = 0
        while True:
            try:
                c, _ = srv.accept()
                c.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF,
                             64 * 1024)
            except OSError:
                # Koneksi mati-seketika (RST sebelum setsockopt/proses
                # klien restart): tanpa try, exception membunuh thread
                # accept SELAMANYA — semua koneksi berikutnya jadi orphan
                # ESTAB di backlog: tak ada di clients, tak dapat byte,
                # tak pernah attach. Inilah bug "synced tapi tanpa data".
                try:
                    c.close()
                except Exception:
                    pass
                continue
            # Blocking send: di localhost, kematian klien selalu mengirim
            # RST -> sendall error langsung. Timeout aktif justru membunuh
            # klien sehat yang sah stall saat init/preroll. Backpressure
            # dikekang queue penuh -> resync ke IDR — latency tak tumbuh.
            self._next_id += 1
            try:
                # 150 item x ~1.4KB DUML = ~0.4 s headroom — klien yang
                # masih init v4l2 (nego/pool ~2-3 s, soket belum dibaca)
                # pasti q-full -> resync -> buang data join-nya -> loop.
                # 4096 ≈ 5.6MB ≈ 10 s cukup untuk init + resync tanpa
                # menghapus kontinuitas.
                cl = {"id": self._next_id, "sock": c,
                      "q": queue.Queue(4096), "synced": False}
                # Seed AU utuh yang persisten: dikirim sebagai item
                # bertanda ("seed", bytes) — _sender mengirimnya verbatim
                # lalu TETAP memindai first-slice IDR live, jadi dekoder
                # melihat [seed-utuh][IDR-segar]. Menyambung langsung ke
                # live setelah seed = splice P-frame yatim = v4l2 abort.
                with self.hlock:
                    seg = bytes(self.seed) if self.seed else None
                if seg is not None:
                    cl["q"].put_nowait(("seed", seg))
                self.clients.append(cl)
                threading.Thread(target=self._sender, args=(cl,),
                                 daemon=True).start()
            except Exception:
                # Apa pun yang gagal di sini (queue OOM, thread-limit)
                # tak boleh membunuh listener — hanya klien ini rontok.
                try:
                    c.close()
                except Exception:
                    pass
                try:
                    self.clients.remove(cl)
                except Exception:
                    pass
                continue
            print("[streamer] video client attached"
                  f" id={cl['id']} ({len(self.clients)})",
                  file=self.log, flush=True)
            try:
                if self.on_attach is not None:
                    self.on_attach()
            except Exception as e:
                print(f"[streamer] on_attach exc: {e!r}",
                      file=self.log, flush=True)

    def _sender(self, cl):
        c, q = cl["sock"], cl["q"]
        tail = b""
        try:
            while True:
                data = q.get()
                if isinstance(data, tuple):
                    # Item seed: stream mandiri utuh — kirim verbatim,
                    # terus memindai first-slice IDR live berikutnya.
                    try:
                        c.sendall(data[1])
                    except (OSError, socket.timeout) as e:
                        print(f"[streamer] video client lost: {e}",
                              file=self.log, flush=True)
                        break
                    continue
                if not cl["synced"]:
                    seg = _find_idr_start(
                        tail, data, allow_any=cl.get("miss", 0) > 6)
                    if seg is None:
                        cl["miss"] = cl.get("miss", 0) + 1
                        if cl["miss"] % 200 == 1:
                            print(f"[streamer] miss={cl['miss']} "
                                  f"id={cl['id']} "
                                  f"tail={tail[-12:].hex()} "
                                  f"data={data[:16].hex()}",
                                  file=self.log, flush=True)
                        tail = data[-4096:]
                        continue
                    tail = b""
                    data = seg
                    cl["synced"] = True
                    if all(c["synced"] for c in self.clients):
                        try:
                            os.remove("/tmp/dji_idr_drought")
                        except OSError:
                            pass
                else:
                    tail = data[-4096:]
                try:
                    c.sendall(data)
                except (OSError, socket.timeout) as e:
                    print(f"[streamer] video client lost: {e}",
                          file=self.log, flush=True)
                    break
        finally:
            if cl in self.clients:
                self.clients.remove(cl)
            try:
                c.close()
            except OSError:
                pass


def _is_first_slice(buf, i):
    """True jika NAL di offset i adalah slice PERTAMA suatu picture.

    IDR DJI = multi-slice: tiap slice berheader 00 00 01 26 01 identik,
    hanya slice pertama yang punya first_slice_segment_in_pic_flag=1
    (bit7 byte RBSP pertama, buf[i+5]). Join di slice tengah = header
    slice hilang -> decoder tak bisa rekonstruksi picture -> abu.
    """
    return i + 5 < len(buf) and bool(buf[i + 5] & 0x80)


def _find_last_idr(buf):
    """Titik mulai AU yang mengandung IDR TERAKHIR dalam buffer — dipakai
    untuk seed klien baru. Valid hanya first-slice IDR (bit7 RBSP) yang
    VPS-nya dekat (<2 KB = AU head lengkap): IDR DJI datang berpasangan
    dan yang kedua TANPA param set sendiri (dist ~85 KB) — menyambung di
    sana mengembalikan puluhan KB P-frame yatim = meson-vdec abort."""
    pos = -1
    i = -1
    while True:
        i = buf.find(b"\x00\x00\x01", i + 1)
        if i < 0 or i + 4 >= len(buf):
            break
        if (buf[i + 3] in (0x26, 0x28) and buf[i + 4] == 0x01
                and _is_first_slice(buf, i)):
            v = max(buf.rfind(b"\x00\x00\x01\x40", 0, i),
                    buf.rfind(b"\x00\x00\x01\x41", 0, i))
            if v >= 0 and i - v < 2048:
                pos = v
    return buf[pos:] if pos >= 0 else None


def _find_idr_start(hist, data, allow_any=False):
    """Titik join untuk klien unsynced dalam payload chunk.

    Prioritas: first-slice IDR (0x26/0x28) — clean-start — lalu
    first-slice CRA (0x2a): titik akses acak open-GOP yang dirancang,
    muncul ~tiap detik saat jendela IDR tertutup. meson-vdec membuang
    frame tanpa referensi ("Couldn't find ref. frame") alih-alih abort
    — join open-GOP aman di hardware (terbukti splice uji EOS bersih).
    allow_any: sudah ~6 chunk tanpa RA -> splice di AU head berikutnya
    (VPS): join mid-GOP, decoder konvergen <1 s via refresh berikutnya.
    """
    buf = hist + data
    i, cra, head = -1, None, None
    while True:
        i = buf.find(b"\x00\x00\x01", i + 1)
        if i < 0 or i + 4 >= len(buf):
            break
        t = buf[i + 3]
        if t in (0x40, 0x41) and head is None:
            head = i
        if buf[i + 4] != 0x01 or not _is_first_slice(buf, i):
            continue
        v = max(buf.rfind(b"\x00\x00\x01\x40", 0, i),
                buf.rfind(b"\x00\x00\x01\x41", 0, i))
        at = buf[v:] if v >= 0 and i - v < 2048 else buf[i:]
        if t in (0x26, 0x28):
            return at
        if t == 0x2a and cra is None:
            cra = at
    if cra is not None:
        return cra
    if allow_any and head is not None:
        return buf[head:]
    return None


def parse_units(buf: bytearray):
    """Выдать (port, payload) для каждого завершённого LogicLink-юнита."""
    while True:
        i = buf.find(MAGIC)
        if i < 0:
            buf.clear()
            return
        if i:
            del buf[:i]
        if len(buf) < 8:
            return
        ln = struct.unpack_from("<I", buf, 4)[0]
        if ln > 0x200000:                 # неправдоподобно — ресинк за magic
            del buf[:2]
            continue
        if len(buf) < 8 + ln:
            return
        yield (buf[2] | (buf[3] << 8), bytes(buf[8:8 + ln]),
               bytes(buf[:8 + ln]))
        del buf[:8 + ln]


class TraceWriter:
    """Rekam wire tx/rx sesi ke JSONL — format identik mitm_capture.jsonl
    ({t,dir,hex} per unit ter-frame) supaya sesi GAGAL bisa di-diff
    byte-per-byte vs capture phone yang sehat. Unit video direkam sebagai
    marker kompak {t,dir:"vid",n} — payload 4Mbps akan memenuhi eMMC.
    Self-cap 32MB + truncate per sesi: tak bisa membesar tak terbatas."""
    CAP = 32 * 1024 * 1024

    def __init__(self, path):
        self.lock = threading.Lock()
        self.dead = False
        try:
            self.f = open(path, "w", buffering=1, encoding="utf-8")
        except OSError:
            self.dead = True
            self.f = None

    def _w(self, rec):
        if self.dead:
            return
        try:
            with self.lock:
                self.f.write(rec + "\n")
                if self.f.tell() > self.CAP:
                    self.dead = True
                    self.f.write('{"trace":"capped at 32MB"}\n')
                    self.f.close()
        except OSError:
            self.dead = True

    def tx(self, data: bytes):
        self._w('{"t":%.6f,"dir":"tx","hex":"%s"}' % (time.time(), data.hex()))

    def rx(self, port: int, raw: bytes):
        if port == VID_PORT:
            self._w('{"t":%.6f,"dir":"vid","n":%d}' % (time.time(), len(raw)))
        else:
            self._w('{"t":%.6f,"dir":"rx","hex":"%s"}' % (time.time(), raw.hex()))

    def close(self):
        self.dead = True


def dji_crc16(data):
    """CRC16 DUML: reflected poly 0x8408, init 0x3692 (verified on capture)."""
    c = 0x3692
    for b in data:
        c ^= b
        for _ in range(8):
            c = (c >> 1) ^ 0x8408 if c & 1 else c >> 1
    return c & 0xffff


def dji_crc8(data):
    """CRC8 header DUML (byte[3]): reflected poly 0x8c, init 0x77 —
    diverifikasi 29945/30001 frame pada mitm_capture."""
    c = 0x77
    for b in data:
        c ^= b
        for _ in range(8):
            c = (c >> 1) ^ 0x8C if c & 1 else c >> 1
    return c & 0xff


def make_pong(d):
    """Bangun respons DUML untuk query RC (dst=0x02, attr=0x40).

    Di sesi asli phone menjawab dua query periodik selama video:
    - 00/0e dari komponen video (0x2d) dan 0x89 — keepalive ~0.3s:
      respons = attr 0x80, seq SAMA, payload konstan 00 01 00.
    - 03/d7 dari 0x03 — re-key handshake: respons = attr 0x80, seq sama,
      payload req[:2] + 0000 + counter req[4:8].
    Query a9->02 03/ce sengaja tidak dijawab (phone asli pun diam).
    """
    cs, ci, pl = d[9], d[10], d[11:-2]
    if (cs, ci) == (0x00, 0x0e):
        resp_pl = b"\x00\x01\x00"
    elif (cs, ci) == (0x03, 0xd7):
        resp_pl = pl[:2] + b"\x00\x00" + (pl[4:8] if len(pl) >= 8 else b"")
    else:
        return None
    resp = bytearray([0x55, 13 + len(resp_pl), 0x04, 0, d[5], d[4]])
    resp += d[6:8]
    resp += bytes([0x80, cs, ci])
    resp += resp_pl
    resp[3] = dji_crc8(resp[:3])
    resp += struct.pack("<H", dji_crc16(resp))
    return bytes(resp)


def make_keyreqs():
    """(deprecated) digantikan poll 00/99 'camcap_common' — lihat collect_burst."""
    return []


SEQ_FILE = "/tmp/dji_stream_seq"
CTR_FILE = "/tmp/dji_stream_ctr"


def _persist_load(path, default):
    try:
        return int(open(path).read().strip(), 0)
    except (OSError, ValueError):
        return default


def _persist_save(path, value):
    # Atomic tmp+rename seperti .dji_link_state — pembaca tak pernah
    # melihat tulis separuh.
    try:
        with open(path + ".tmp", "w") as f:
            f.write(str(value))
        os.replace(path + ".tmp", path)
    except OSError:
        pass


def reseq(unit, seq):
    """Seq baru untuk юнита: duml[6:8] -> unit[14:16], CRC16 di ekor DUML.

    RC menjatuhkan resend dengan seq yang sudah pernah ia lihat di sesi ini —
    burst verbatim tidak menahan subscribe. Seq harus naik + CRC dihitung ulang.
    """
    u = bytearray(unit)
    struct.pack_into("<H", u, 14, seq & 0xffff)
    struct.pack_into("<H", u, len(u) - 2, dji_crc16(u[8:-2]))
    return bytes(u)


def reseq_sub(unit, seq, ctr):
    """Reseq + counter poll baru untuk unit 00/99 united_pub_sub_agent.

    Payload unit menyimpan request-id u16 di pl[4:6] -> offset unit 23:25;
    RC/agent mengabaikan poll dengan id yang sudah pernah dilihat, maka
    id harus monoton naik seperti pada capture (0x9618..0x1032).
    """
    u = bytearray(unit)
    struct.pack_into("<H", u, 14, seq & 0xffff)
    struct.pack_into("<H", u, 23, ctr & 0xffff)
    struct.pack_into("<H", u, len(u) - 2, dji_crc16(u[8:-2]))
    return bytes(u)


def reseq_record(rec, seq_ref, ctr_ref, drop_sub=False):
    """Reseq tiap unit DUML dalam satu record (record bisa multi-unit:
    magic 55cc4957 + len + duml berulang). Prefix non-unit dipertahankan.

    Unit 00/99 (pub/sub poll) di-replay dengan request-id batin BARU via
    reseq_sub: agent men-dedup req-id <= yang pernah dilihat, sehingga id
    basi dari recipe mematikan mekanisme IDR-on-demand. ctr diambil
    max(ctr+1, captured+1) secara sirkular -> stream keluar selalu
    monoton-naik walau recipe membawa id lebih tinggi dari seed kita.
    """
    magic = b"\x55\xcc\x49\x57"
    offs = []
    i = rec.find(magic)
    while i >= 0:
        offs.append(i)
        i = rec.find(magic, i + 1)
    if not offs:
        return rec
    out = bytearray(rec[:offs[0]])
    offs.append(len(rec))
    for j in range(len(offs) - 1):
        u = rec[offs[j]:offs[j + 1]]
        if len(u) >= 16 and struct.unpack_from("<I", u, 4)[0] + 8 == len(u):
            seq_ref[0] = (seq_ref[0] + 1) & 0xffff
            if (len(u) > 18 and u[8] == 0x55 and u[13] == 0x28
                    and u[17] == 0x00 and u[18] == 0x99):
                # Grant IDR diukur ~490 per aktivasi — poll 00/99 replay
                # tail-loop adalah pembakar utama (~1.4/dtk). drop_sub
                # membuangnya di pass ekor: budget bertahan ~40+ menit.
                if drop_sub:
                    continue
                cap = struct.unpack_from("<H", u, 23)[0]
                if (cap - ctr_ref[0]) % 0x10000 < 0x8000:
                    ctr_ref[0] = cap
                ctr_ref[0] = (ctr_ref[0] + 1) & 0xffff
                u = reseq_sub(u, seq_ref[0], ctr_ref[0])
            else:
                u = reseq(u, seq_ref[0])
        out += u
    return bytes(out)


def collect_burst(recipe):
    """Юниты для периодического ресенда.

    Возвращает (subs, hb, sub):
    - subs: 00/4f -> 0x2d — чтение cfg-файла (legacy-бурст, оставляем);
    - hb:   00/96 -> 0x2d — heartbeat компонента видео (~0.8 s);
    - sub:  00/99 -> 0x28 — united_pub_sub_agent poll топика
      "camcap_common" (~1 Гц у телефона). Именно новый poll с новым
      внутренним counter заставляет энкодер отдать IDR новому
      подписчику — replay с тем же counter игнорируется как dup.
    """
    subs = []
    hb = sub = None
    # Unit aktivasi head-only (sekali per sesi di capture, tak pernah di
    # tail-loop): dugaan renewal langganan — kalau subscribe ber-TTL,
    # re-fire ini secara periodik seharusnya menghidupkan IDR lagi.
    act = {}
    act_want = {(0x2d, 0x00, 0x51), (0x2d, 0x00, 0xe5), (0x2d, 0x00, 0x01),
                (0x01, 0x02, 0xb5), (0x28, 0x00, 0x4a)}
    for r in recipe:
        b = bytes.fromhex(r["hex"])
        i = 0
        while True:
            i = b.find(b"\x55\xcc\x49\x57", i)
            if i < 0 or i + 8 > len(b):
                break
            ln = struct.unpack_from("<I", b, i + 4)[0]
            pl = b[i + 8:i + 8 + ln]
            if len(pl) > 10 and pl[0] == 0x55:
                key = (pl[5], pl[9], pl[10])
                if key in act_want and key not in act:
                    act[key] = b[i:i + 8 + ln]
                if pl[5] == 0x2d and pl[9] == 0x00:
                    if pl[10] == 0x4f:
                        subs.append(b[i:i + 8 + ln])
                    elif pl[10] == 0x96 and hb is None:
                        hb = b[i:i + 8 + ln]
                elif (pl[5] == 0x28 and pl[9] == 0x00
                      and pl[10] == 0x99 and sub is None):
                    sub = b[i:i + 8 + ln]
            i += 8 + ln
    subs.extend(act.values())
    return subs, hb, sub


_mitm_cache = [0.0, False]

def _mitm_active():
    """True bila mitm.py memegang owner TX bridge (phone DJI Fly terpasang).
    Flag file di-touch mitm tiap ~15 dtk; segar <45 dtk = aktif. Saat aktif,
    streamer jadi tap murni: tetap terima video untuk :9990 tapi NIHIL kirim
    — replay/poll/pong milik mitm sekarang."""
    now = time.time()
    if now - _mitm_cache[0] < 2.0:
        return _mitm_cache[1]
    _mitm_cache[0] = now
    try:
        _mitm_cache[1] = now - os.stat("/tmp/dji_mitm_active").st_mtime < 45
    except OSError:
        _mitm_cache[1] = False
    return _mitm_cache[1]


def _udc_state():
    try:
        for f in os.listdir("/sys/class/udc"):
            return open(f"/sys/class/udc/{f}/state").read().strip()
    except OSError:
        pass
    return "configured"    # tanpa sysfs: anggap hidup — perilaku lama


def _renewal_thread(recs, sock, send_lock, seq_ref, ctr_ref, trace=None):
    """Putar burst renewal subscribe dengan pacing asli (dt dipertahankan,
    digap-maks 0.1 s — jangan dibanjiri sekaligus: flood 900 unit pernah
    membunuh sesi). Tiap record di-reseq_record: seq/req-id selalu segar.
    """
    last = 0.0
    for dt, rec in recs:
        w = min(dt - last, 0.1)
        last = dt
        if w > 0:
            time.sleep(w)
        try:
            with send_lock:
                u = reseq_record(rec, seq_ref, ctr_ref)
                sock.sendall(u)
                if trace is not None:
                    trace.tx(u)
        except OSError:
            return


def sender_thread(sock, recipe, args, stats, send_lock, seq_ref, ctr_ref,
                  trace=None):
    """Воспроизвести записанный поток phone->rc.

    ВСЕ проходы — через reseq_record: RC dedup menjatuhkan seq yang pernah
    ia lihat DI SESI USB — dan sesi itu bertahan lintas restart streamer
    (bridge+UDC tetap configured). Replay verbatim mengirim seq phone yang
    identik tiap restart -> restart ke-2+ dalam satu colok = seluruh
    replay didedup -> sesi tak pernah terbuka -> spiral stall/restart
    sampai cabut-colok (inilah "miss" yang terukur). Seq segar juga sah:
    aktivasi RC memang menerima seq apa pun di awal sesi. drop_sub hanya
    di pass ekor — pass head membawa subscribe awal yang wajib mendarat.
    """
    head = recipe
    tail = [r for r in recipe if r["dt"] > recipe[-1]["dt"] - 5.0]
    last_t = 0.0
    first_pass = True
    while True:
        for r in head:
            wait = r["dt"] - last_t
            last_t = r["dt"]
            if wait > args.max_gap:
                wait = args.max_gap
            if wait > 0:
                time.sleep(wait)
            try:
                with send_lock:
                    unit = reseq_record(bytes.fromhex(r["hex"]),
                                        seq_ref, ctr_ref,
                                        drop_sub=not first_pass)
                    sock.sendall(unit)
                    if trace is not None:
                        trace.tx(unit)
            except OSError:
                return
            stats["tx"] += 1
        head = tail
        first_pass = False


def _reap_ghosts(out, log):
    # Socket mati di sisi unsynced tak pernah ketemu send-gagal — reap via
    # MSG_PEEK. Dipanggil dari stat-tick DAN UDC-wait: tanpa yang kedua,
    # klien menumpuk tak terbatas selama RC tercabut (id>250 terukur).
    for c in list(getattr(out, "clients", [])):
        s = c.get("sock")
        if s is None:
            continue
        try:
            dead = (s.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) == b"")
        except BlockingIOError:
            dead = False
        except OSError:
            dead = True
        if dead:
            try:
                s.close()
            except OSError:
                pass
            try:
                out.clients.remove(c)
            except ValueError:
                pass
            print(f"[streamer] reaped ghost id={c['id']}", file=log,
                  flush=True)


def main():
    ap = argparse.ArgumentParser(description="HEVC-стример Mini 3 через "
                                           "AOA-мост STB")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9910)
    ap.add_argument("--recipe", default="/root/video_recipe.jsonl",
                    help="JSONL {dt,hex} рецепт воспроизведения (захват DJI Fly)")
    ap.add_argument("--dump", metavar="FILE",
                    help="писать HEVC в FILE вместо stdout")
    ap.add_argument("--serve", type=int, metavar="PORT",
                    help="serve HEVC via TCP 127.0.0.1:PORT — koneksi "
                         "baru selalu mulai dari IDR")
    ap.add_argument("--max-gap", type=float, default=0.5,
                    help="предел сжатия пауз между пакетами, секунды")
    ap.add_argument("--vid-timeout", type=float, default=90,
                    help="если видео 0x574a не появилось за N сек — выход "
                         "(watchdog перезапустит сессию с нуля)")
    ap.add_argument("--trace", metavar="FILE",
                    default="/root/sess_trace.jsonl",
                    help="rekam wire tx/rx ke JSONL (format mitm_capture) — "
                         "sesi gagal bisa di-diff vs capture phone. "
                         "'' menonaktifkan")
    args = ap.parse_args()

    recipe = [json.loads(l) for l in open(args.recipe)]
    burst, hb, sub = collect_burst(recipe)
    # Renewal subscribe asli — diekstrak dari mitm_capture 38 menit:
    # saat phone resume setelah gap ~10 menit, ia mengirim burst kompak
    # ~150 unit dalam ~4 s (00/4f, 02/54, 00/4a, 00/99, 03/f9, 07/30,
    # 00/e5, 02/eb, 08/41, 11/4a, ee/05, …) — membuka jendela grant IDR
    # baru (~200 s) TANPA memutus sesi. Ini jawaban plafon ~490: jendela
    # grant per aktivasi, bukan TTL — dan renewal-nya burst ini.
    renewal_recs = []
    try:
        for l in open("/root/renewal.jsonl"):
            r = json.loads(l)
            renewal_recs.append((r["dt"], bytes.fromhex(r["hex"])))
    except OSError:
        pass

    log = sys.stderr
    trace = TraceWriter(args.trace) if args.trace else None
    resub = threading.Event()
    if args.serve:
        out = VidServer("127.0.0.1", args.serve, log)
        # Klien baru -> burst subscribe SEGERA: IDR DJI datang bergelombang
        # (open-GOP di antaranya), klien yang attach saat jeda IDR bisa
        # menunggu tak terbatas. Re-subscribe memaksa RC memancarkan IDR.
        out.on_attach = resub.set
    elif args.dump:
        out = SinkWriter(args.dump, log)
    else:
        out = sys.stdout.buffer

    stats = {"tx": 0, "vid": 0, "vid_bytes": 0, "duml": 0}
    sock = socket.create_connection((args.host, args.port), timeout=10)
    sock.settimeout(1.0)
    print(f"[streamer] connected {args.host}:{args.port}, "
          f"recipe {len(recipe)} units", file=log, flush=True)

    # Jangan replay ke link yang belum ada: UDC "not attached" = RC
    # tercabut, tapi bridge tetap accept :9910 — replay 9739 unit (~10s
    # CPU) terbuang ke void lalu drop, restart, ulangi (174 siklus
    # terukur dalam satu boot). Tunggu configured tanpa batas: saat RC
    # dicolok, instance yang menunggu inilah yang langsung handshake —
    # bukan yang sedang sibuk replay ke link mati.
    wait_log = 0
    while True:
        try:
            udc = "configured"
            for f in os.listdir("/sys/class/udc"):
                udc = open(f"/sys/class/udc/{f}/state").read().strip()
                break
        except OSError:
            udc = "?"
        if udc == "configured":
            break
        # Bridge restart saat menunggu -> socket ini EOF; replay nanti
        # akan tertuju ke socket mati. Keluar untuk attach segar.
        # NB: socket.timeout == OSError — harus didahulukan (mode
        # settimeout(1.0) select-timeout bahkan pada recv DONTWAIT).
        try:
            if sock.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) == b"":
                print("[streamer] bridge socket EOF saat UDC-wait — "
                      "reconnect", file=log, flush=True)
                return
        except (BlockingIOError, socket.timeout):
            pass
        except OSError:
            print("[streamer] bridge socket mati saat UDC-wait — "
                  "reconnect", file=log, flush=True)
            return
        if time.time() - wait_log > 30:
            _reap_ghosts(out, log)
            print("[streamer] menunggu UDC configured (RC tercabut?)",
                  file=log, flush=True)
            wait_log = time.time()
        time.sleep(1)

    # Ждём первых байт от RC — RC рассылает телеметрию сам, как только
    # гаджет сконфигурирован. Tapi tanpa batas: kalau RC sedang idle/suspend,
    # recv(None) menunggu selamanya padahal replay justru aktivasinya —
    # deadlock hening. Batasi 8 dtk: lewat itu replay jalan apa adanya.
    sock.settimeout(8.0)
    try:
        first = sock.recv(65536)
        if first == b"":
            # EOF: bridge mati di celah UDC-wait->recv — replay ke socket
            # yatim = lap sia-sia. Keluar, respawn menyambung segar.
            print("[streamer] bridge EOF — reconnect", file=log,
                  flush=True)
            return
        print("[streamer] RC alive — replaying session", file=log,
              flush=True)
    except socket.timeout:
        first = b""
        print("[streamer] RC silent — replay anyway (activation)",
              file=log, flush=True)
    sock.settimeout(1.0)

    send_lock = threading.Lock()
    # Seq/req-id DIPERSISTKAN lintas restart proses: dedup RC menolak seq
    # yang pernah ia lihat di sesi USB yang sama — seq yang reset ke
    # 0xA000 tiap restart (terukur 27x/hari) membuat replay sesi ke-2 dst
    # di-drop -> subscribe gagal -> stall -> restart -> makin parah.
    # +0x400 margin: save terakhir bisa tertinggal ~15s dari bump aktual;
    # RC hanya menolak seq berulang, nilai besar selalu sah.
    seq_ref = [(_persist_load(SEQ_FILE, 0xA000) + 0x400) & 0xffff]
    ctr_ref = [(_persist_load(CTR_FILE, 0x2000) + 0x400) & 0xffff]
    tx = threading.Thread(target=sender_thread,
                          args=(sock, recipe, args, stats, send_lock, seq_ref,
                                ctr_ref, trace),
                          daemon=True)
    tx.start()

    buf = bytearray(first)
    t_stat = t_start = time.time()
    last_vid = t_start
    first_vid_t = None
    last_idr, last_idr_t, last_react, drought_renew = 0, t_start, 0, 0
    link_down, cfg_since = False, 0.0
    next_burst = t_start + 20
    next_hb = t_start
    next_sub = t_start
    next_keyreq = t_start
    mitm_seen = False
    try:
        while True:
            mitm = _mitm_active()
            if mitm and not mitm_seen:
                mitm_seen = True
                print("[streamer] mitm owner — replay pause, tap video saja",
                      file=log, flush=True)
            elif not mitm:
                mitm_seen = False
            try:
                data = sock.recv(65536)
            except socket.timeout:
                data = None
            except OSError:
                print("[streamer] bridge dropped us", file=log, flush=True)
                break
            if data == b"":
                print("[streamer] bridge closed", file=log, flush=True)
                break
            if data:
                buf += data
                for port, payload, raw in parse_units(buf):
                    if trace is not None:
                        trace.rx(port, raw)
                    if port == VID_PORT:
                        stats["vid"] += 1
                        stats["vid_bytes"] += len(payload)
                        last_vid = time.time()
                        if first_vid_t is None:
                            first_vid_t = last_vid
                            print(f"[streamer] first video @"
                                  f"{first_vid_t - t_start:.2f}s",
                                  file=log, flush=True)
                        out.write(payload)
                        out.flush()
                    else:
                        stats["duml"] += 1
                        if stats["duml"] == 1:
                            print(f"[streamer] first rx @"
                                  f"{time.time() - t_start:.2f}s",
                                  file=log, flush=True)
                        # Query RC yang wajib dijawab (00/0e keepalive 0x2d/0x89,
                        # 03/d7 re-key): tanpa jawaban ber-seq sama, RC menutup
                        # port video ~3 menit — penyebab freeze berkala.
                        if (not mitm and len(payload) >= 13
                                and payload[0] == 0x55
                                and payload[5] == 0x02 and payload[8] == 0x40):
                            pong = make_pong(payload)
                            if pong:
                                try:
                                    with send_lock:
                                        sock.sendall(b"\x55\xcc\x49\x57"
                                                     + struct.pack("<I", len(pong))
                                                     + pong)
                                        if trace is not None:
                                            trace.tx(b"\x55\xcc\x49\x57"
                                                     + struct.pack("<I", len(pong))
                                                     + pong)
                                except OSError:
                                    break
                        # Логируем первые ответы RC: если сессия отвергнута,
                        # по cmdset/cmdid видно, что именно RC ответил.
                        if stats["duml"] <= 64 and len(payload) > 12:
                            print(f"[streamer] rx port={port:#06x} "
                                  f"{payload[9]:02x}/{payload[10]:02x}",
                                  file=log, flush=True)
            now = time.time()
            if resub.is_set():
                resub.clear()
                if now < next_burst:
                    next_burst = now
                    print("[streamer] attach -> burst resubscribe",
                          file=log, flush=True)
            if not mitm and now >= next_burst and burst:
                try:
                    for u in burst:
                        with send_lock:
                            seq_ref[0] = (seq_ref[0] + 1) & 0xffff
                            u2 = reseq(u, seq_ref[0])
                            sock.sendall(u2)
                            if trace is not None:
                                trace.tx(u2)
                except OSError:
                    break
                next_burst = now + 15
            # heartbeat видеокомпонента — только когда 0x574a уже открыт
            if not mitm and hb is not None and stats["vid"] and now >= next_hb:
                try:
                    with send_lock:
                        seq_ref[0] = (seq_ref[0] + 1) & 0xffff
                        u2 = reseq(hb, seq_ref[0])
                        sock.sendall(u2)
                        if trace is not None:
                            trace.tx(u2)
                except OSError:
                    break
                next_hb = now + 1.0
            # Poll pub/sub "camcap_common" ~1 Гц seperti phone asli. Selain
            # menahan subscribe, setiap poll dengan request-id BARU membuat
            # agent mengirim burst IDR untuk subscriber baru — inilah
            # mekanisme IDR-on-demand yang sebenarnya (bukan 02/b3).
            if not mitm and sub is not None and stats["vid"] and now >= next_sub:
                try:
                    with send_lock:
                        seq_ref[0] = (seq_ref[0] + 1) & 0xffff
                        ctr_ref[0] = (ctr_ref[0] + 1) & 0xffff
                        u2 = reseq_sub(sub, seq_ref[0], ctr_ref[0])
                        sock.sendall(u2)
                        if trace is not None:
                            trace.tx(u2)
                except OSError:
                    break
                # Eksperimen plafon ~490 grant: poll 5 s (bukan 1 s seperti
                # phone). Jika plafon bergeser ke ~5x waktu => batas
                # per-hitungan grant; jika tetap ~200 s => TTL langganan.
                # Attach/desync tetap ditangani poll ekstra 2 s di bawah.
                next_sub = now + 5.0
            # Klien :9990 belum sinkron -> poll ekstra tiap 2 detik supaya
            # IDR burst datang lebih cepat daripada menunggu tick 1s.
            if not mitm and sub is not None and stats["vid"] and now >= next_keyreq:
                unsynced = [c["id"] for c in out.clients if not c["synced"]]
                if unsynced:
                    try:
                        with send_lock:
                            seq_ref[0] = (seq_ref[0] + 1) & 0xffff
                            ctr_ref[0] = (ctr_ref[0] + 1) & 0xffff
                            u2 = reseq_sub(sub, seq_ref[0],
                                           ctr_ref[0])
                            sock.sendall(u2)
                            if trace is not None:
                                trace.tx(u2)
                    except OSError:
                        break
                    print(f"[streamer] sub poll -> id={unsynced}",
                          file=log, flush=True)
                    next_keyreq = now + 2.0
            # Подписка 0x2d живёт ограниченное время: если RC открыл порт и
            # потом замолчал — выходим, watchdog перезапустит рецепт и
            # enable-бурст уйдёт повторно.
            stalled = stats["vid"] > 0 and now - last_vid > 45
            if (stats["vid"] == 0 and now - t_start > args.vid_timeout
                    or stalled):
                # Link fisik mati (RC off/kabel kendor): tak ada restart
                # yang menolong — replay pun di-drop bridge sampai AOA
                # ready. Tetap hidup dan tunggu UDC configured; baru exit
                # sesudah link stabil ~8 s supaya restart+replay mendarat
                # di link yang benar-benar hidup — tanpa churn 100-an s.
                st_ = _udc_state()
                if st_ != "configured":
                    cfg_since = 0.0
                    if not link_down:
                        link_down = True
                        print(f"[streamer] link down (UDC={st_}) — "
                              "tunggu host tanpa restart-churn",
                              file=log, flush=True)
                else:
                    cfg_since = cfg_since or now
                    if link_down and now - cfg_since > 8:
                        print("[streamer] link pulih — restart untuk "
                              "replay segar", file=log, flush=True)
                        break
                    if not link_down:
                        why = "video stalled" if stalled else "no video"
                        print(f"[streamer] {why} — restart via watchdog",
                              file=log, flush=True)
                        break
            if time.time() - t_stat > 15:
                _reap_ghosts(out, log)
                drop = getattr(out, "dropped", 0)
                qs = getattr(getattr(out, "q", None), "qsize", lambda: -1)()
                fifo = -1
                fd = getattr(out, "_fd", None)
                if fd is not None:
                    try:
                        fifo = struct.unpack(
                            "i", fcntl.ioctl(fd, 0x541B,
                                             struct.pack("i", 0)))[0]
                    except OSError:
                        pass
                nals = getattr(out, "nals", None)
                nstr = (" nal=" + ",".join(f"{k:02x}x{v}"
                        for k, v in sorted(nals.items()))) if nals else ""
                cli = " ".join(
                    f"#{c['id']}{'S' if c['synced'] else 'u'}"
                    f"q{c['q'].qsize()}"
                    for c in getattr(out, "clients", []))
                print(f"[streamer] tx={stats['tx']} duml_rx={stats['duml']} "
                      f"video={stats['vid']}pkts "
                      f"{stats['vid_bytes']/1e6:.1f}MB drop={drop} "
                      f"qs={qs} fifo={fifo//1024}KB cli=[{cli}]{nstr}",
                      file=log, flush=True)
                # State file untuk indikator per-lapis di splash display:
                # LINK = duml_rx maju, VID = video pkts maju. Atomic via
                # tmp+rename supaya pembaca tak pernah lihat tulis separuh.
                try:
                    with open("/tmp/.dji_link_state", "w") as f:
                        f.write(f"rx={stats['duml']} vid={stats['vid']} "
                                f"idr={(nals.get(0x26, 0) + nals.get(0x28, 0)) if nals else 0} "
                                f"t={int(time.time())}\n")
                    os.replace("/tmp/.dji_link_state",
                               "/tmp/dji_link_state")
                except OSError:
                    pass
                _persist_save(SEQ_FILE, seq_ref[0])
                _persist_save(CTR_FILE, ctr_ref[0])
                t_stat = time.time()
                # Diam total >15 dtk sejak start = link dua arah belum
                # settle (connect di celah bridge/AOA sekarat): replay
                # terbuang sampai drop ~26 dtk terukur. Bail cepat —
                # retry systemd mendarat di link yang sudah hidup.
                if (stats["duml"] + stats["vid"] == 0
                        and time.time() - t_start > 15):
                    print("[streamer] RC diam total 15s — exit, retry",
                          file=log, flush=True)
                    break
                # TTL grant IDR ~200 s per aktivasi (diukur: 483-498 IDR
                # selalu di ~200 detik tak peduli laju poll — bukan count-
                # cap). Sesudahnya encoder murni open-GOP (CRA 2a): klien
                # yang SUDAH synced tetap decode normal — CRA butuh state
                # referensi kontinu, bukan IDR. Restart hanya saat ada
                # klien unsynced yang menunggu IDR.
                now2 = time.time()
                idr_n = nals.get(0x26, 0) + nals.get(0x28, 0) if nals else 0
                if idr_n != last_idr:
                    last_idr, last_idr_t = idr_n, now2
                elif not mitm and stats["vid"] and nals and now2 - last_idr_t > 45:
                    # Jendela grant IDR SIKLIKAL — terukur menutup ~90-200 s
                    # lalu membuka lagi sendiri (26x246 -> 344 tanpa aksi).
                    # Kekeringan itu keadaan SAH: klien synced tetap decode
                    # CRA, klien unsynced memang menunggu — restart justru
                    # memaksa join mid-GOP -> decoder abort -> loop maut.
                    # Cukup tandai drought + tunggu; batas darurat 10 menit.
                    need = any(not c["synced"]
                               for c in getattr(out, "clients", []))
                    if need:
                        try:
                            open("/tmp/.dji_idr_drought", "w").write(
                                str(int(now2)))
                            os.replace("/tmp/.dji_idr_drought",
                                       "/tmp/dji_idr_drought")
                        except OSError:
                            pass
                        if last_react == 0:
                            last_react = now2
                            drought_renew = 0
                            print(f"[streamer] IDR flat @{idr_n} — "
                                  "drought: renewal burst + tunggu "
                                  "jendela", file=log, flush=True)
                        # Jendela grant tak terbuka oleh poll saja — yang
                        # membukanya di capture = burst subscribe kompak
                        # (~150 unit, pola phone-resume). renewal_recs
                        # sudah dimuat tapi tak pernah dikirim — inilah
                        # pemakaiannya. Ulang tiap 60 s selama drought.
                        if renewal_recs and now2 - drought_renew > 60:
                            drought_renew = now2
                            print(f"[streamer] drought {int(now2-last_react)}s"
                                  " — renewal burst", file=log, flush=True)
                            threading.Thread(
                                target=_renewal_thread,
                                args=(renewal_recs, sock, send_lock,
                                      seq_ref, ctr_ref, trace),
                                daemon=True).start()
                        # Batas darurat: drought >10 mnt = sesi perlu
                        # diaktivasi ulang dari nol (restart via exit).
                        if now2 - last_react > 600:
                            print(f"[streamer] IDR drought >10 mnt @{idr_n}"
                                  " — restart aktivasi", file=log,
                                  flush=True)
                            break
                        last_idr_t = now2
                    else:
                        try:
                            os.remove("/tmp/dji_idr_drought")
                        except OSError:
                            pass
                        last_idr_t = now2
    except KeyboardInterrupt:
        pass
    _persist_save(SEQ_FILE, seq_ref[0])
    _persist_save(CTR_FILE, ctr_ref[0])
    print(f"[streamer] done, video {stats['vid_bytes']/1e6:.1f}MB",
          file=log, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
