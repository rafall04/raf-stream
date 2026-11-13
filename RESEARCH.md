# RESEARCH — DJI Mini 3 + RC-N1 + STB FiberHome HG680P

Playbook untuk mengganti Raspberry Pi dengan HG680P (Amlogic S905X) sebagai
USB-gadget bridge antara RC-N1 dan PC. Status: **arsitektur terbukti,
protokol byte-exact, hardware belum diuji fisik**.

```
[PC/laptop] --TCP/WiFi :9910--> [HG680P: AOA gadget] --USB--> [RC-N1 host] ))) [Mini 3]
```

## TL;DR — kenapa ini feasible

1. **RC-N1 adalah USB host**; port atas (kabel HP) bicara Android Open Accessory
   (AOA) — HP/STB jadi device. Dikonfirmasi FreeFCC-USB & forum Pi (RC-N2).
2. **HG680P-STB-02 sudah diverifikasi langsung (read-only)**: UDC `c9100000.usb`
   (dwc2, 7 EP, high-speed) terdaftar, `dr_mode=peripheral` sudah di DTB,
   `raw_gadget.ko` tersedia sebagai modul, `dummy_hcd` ada untuk self-test,
   `meson-vdec` + `hevc_v4l2m2m` untuk HW decode HEVC.
3. **DJI-Link** = implementasi referensi lengkap (Pi Zero 2 W ↔ remote DJI):
   gadget AOA via raw_gadget + bridge TCP transparan — tinggal dipakai ulang.
4. **FreeFCC-USB** = detail sesi RC-N1: bootstrap + keepalive byte-exact.

## Arsitektur

| Komponen | Di repo | Peran |
|---|---|---|
| `stb/` | gadget side | `bin/dji-bridge` (**prebuilt static aarch64** dari release DJI-Link — zero deps, langsung jalan) + `aoa_device.py`/`raw_gadget.py`/`bridge.py` (verbatim, fallback/debug) + `setup_gadget.sh` + `run_bridge.sh` + `install_services.sh` (systemd auto-start) + `build_bridge.sh` + `selftest_loopback.sh` + `capture_usbmon.sh` + `pi_src/` |
| `pc/` | laptop side | `duss.py` (codec) + `session.py` (bootstrap/keepalive/liveview/dump) + `aoa.py`/`duml.py` + `host_aoa_test.py` (exerciser — PC jadi host ala RC, untuk fase-2) |
| `refs/dji-link` | referensi | implementasi penuh: Python beta + C++ produksi (`src/pi/`, `src/core/`) + `reverse_docs/` (~50 dok hasil dekompilasi DJI Fly) |
| `refs/videobridge` | referensi | pola output ffmpeg → mediamtx → RTSP/WebRTC |
| `dji_protocol` | referensi | jalur iOS/MFi (`emulate_iphone.py`), `usb_mobile_protocol.md` |

**Deploy termudah**: `deploy.sh` → di STB cukup `bash stb/run_bridge.sh` — binary
static aarch64 tidak butuh Python sama sekali. Python path dipakai saat perlu
debug/modifikasi state machine.

## ✅ VERIFIED 2026-11-07 — sesi MITM end-to-end DJI Fly ↔ RC-N1 asli

Semua prediksi utama sudah dibuktikan di hardware nyata:

| Klaim | Status |
|---|---|
| HG680P + dwc2 sebagai AOA gadget untuk RC-N1 | ✅ `configured`, RC enumerate |
| HP mengenali gadget kita sebagai "DJI RC-N1" | ✅ DJI Fly auto-launch via `DJIAoaActivity` |
| Relay MITM dua arah phone↔RC | ✅ `Pi->RC` + `RC->Pi` mengalir bersih |
| DJI Fly penuh via MITM | ✅ semua status/tampilan termuat |
| Channel video | ✅ **`0x574a`** — prediksi `0x5750` SALAH, Mini3 pakai channel yang sama dengan WM160 |
| Codec video | ✅ **HEVC Main 1280×720 raw Annex-B** — ffprobe + decode ke PNG, gambar asli |
| Wrapper 0x6d (Lapis 6) | ❌ **TIDAK ada di jalur Android** — payload langsung ES |

### Root cause kegagalan MITM kemarin (fixed)

`mitm.py` tidak pernah `release_interface()`/`dispose_resources()` + ada
proses zombie → `claim failed: Resource busy` berulang → loop 1.5s re-handshake
→ phone "entering accessory mode" 167× di logcat (flap). DJI Fly selalu benar —
fd accessorynya kita yang rusakkan. Fix: pilih interface via `bInterfaceSubClass
0xFF` (AOA) bukan `0x42` (ADB, sekarang phone = `18d1:2d01`), release tiap akhir
sesi, kill duplikat.

### Urutan video Mini3 (dari capture nyata, 12146 unit)

```
t=0-1s   init: 00/01, 00/b7, 11/4a, 18/37, 51/0c, 03/20
t=2s+    raw byte b0 @1Hz (heartbeat link-layer, BUKAN framed)
t=54s    burst ~15 cmd → video mulai 1 detik kemudian di capture pendek
t=+289s  di sesi panjang: video mulai setelah user menavigasi UI
```

Keepalive video Mini3 = `00/99 camcap_common` (united_pub_sub_agent, byte-5
increment) + `00/0e` heartbeat_req + `b0` @1Hz. **Tidak ada `0x88`** di Mini3
(itu khusus Goggles N3 di dok samuelsadok). Stream mati ~11 detik tanpa
keepalive.

**Resep replay**: `/root/video_recipe.jsonl` (JSONL `{dt,hex}`) berisi seluruh
stream phone→rc sesi produktif — CRC valid, RC tak bisa bedakan. `streamer.py`
mereplay-nya dengan gap dikompresi (≤0.5s), tail ~5s di-loop sebagai keepalive.

Isi tail (terukur dari `mitm_capture.jsonl` 33MB, 2026-11-10): 47 baris =
`b0`×4 + `00/51`→`0x28`×19 + `11/4a`×10 + `51/0c`×6 + `00/4f`→`0x01`×6 +
`18/37`+`03/20`+`03/f9`+`03/da`+`00/32`+`00/e5`+`03/cd`+`03/f8`+`02/54`+
`02/eb`+`51/34`+`03/34`+`0d/04`+`03/3c` dst — seluruh chatter periodik
phone ikut ter-loop (b0 ~4×/loop ≫ phone 1Hz). Keepalive TIDAK hilang
pasca-replay; jangan derive ulang teori itu. Query RC yang wajib dijawab
hanya `03/d7` + `00/0e` (phone pun abaikan `03/ce`); `00/0e` dari phone
murni respons (0 request), `06/77` tidak ada di capture phone (unit
sintetis FreeFCC, bukan phone-asli).

### Arsitektur produksi (CosmoStreamer-style, tanpa HP)

```
RC-N1 ──AOA──> [dji-bridge :9910] <─TCP─ [streamer.py] ─> HEVC Annex-B stdout
   STB USB-2    gadget               replay recipe        |
                                                         v
                          ffmpeg -i - -c copy → mediamtx → rtsp :8554 / webrtc :8889
```

Deploy: `streamer.py` ada di `/root/streamer.py` + `/root/video_recipe.jsonl`.
Jalankan: `python3 /root/streamer.py --dump /tmp/v.hevc` lalu `ffmpeg -i` verify.

### Gotcha operasional (hard-won)

- `dji-bridge.service` pakai `Restart=always` — **jangan nohup bridge manual**,
  selalu via systemd (tabrakan :9910 + "UDC busy" berulang).
- RC yang sudah tercolok tidak auto-enumerate gadget baru → **bounce gadget**
  (`systemctl restart dji-bridge`) atau cabut-colok RC. dwc2 di STB tidak
  reliable detect plug ketika gadget dead.

## ✅ VERIFIED 2026-11-07 (sesi 2) — decode & display pipeline

Benchmark di STB (S905X, 4×Cortex-A53 @1.512GHz, clip uji HEVC 720p30 39s
dari capture asli, dipotong mulai VPS di byte 7905):

| Jalur decode | Hasil | Putusan |
|---|---|---|
| ffmpeg 5.1 `hevc` (SW, baseline) | 50.8s → **0.74x / 22fps** | terlalu lambat |
| + governor `performance` + `skip_loop_filter all` | 38.7s → ~1.0x | pas, nol headroom |
| `lowres=1` | diabaikan decoder | mati |
| `skip_frame` | ditolak build ini ("Invalid argument") | mati |
| `threads=4` | tanpa efek (~1.5 core saja yang terpakai) | mati |
| **ffmpeg 7.0.2 static** (`/opt/ffmpeg7`) | 37.8s → **0.98x** | NEON HEVC baru, +34% |
| `hevc_v4l2m2m` (HW, meson_vdec) | **kernel oops + hang total** | **broken** |

### Kenapa HW decode mati

`meson_vdec` (staging, kernel 6.12.112-ophub) **crash di IRQ**:
`codec_hevc_process_segment` → IRQ thread `irq/48-vdec` exit → driver wedged
sampai reboot. Setelah reboot, decode stream **bersih** (x265, mulai VPS)
juga hang tanpa output — driver tidak sanggup proses apapun, bukan hanya
stream DJI. ffmpeg v4l2m2m dan `gst v4l2h265dec` sama-sama deadlock.
Hanya ada satu kernel di /boot → HW decode **bukan opsi** pada image ini.

### Kenapa mpv --vo=drm lag parah

Decode SW sendiri ~1.0x, tapi `--vo=drm` melakukan YUV→RGB **di CPU** +
scale 720p→1080p lewat swscale → total ~0.36x. A53 tidak kuat untuk
konversi+scale per-frame.

### Solusi display (terbukti 0 drop, ~180% CPU)

```
FIFO /run/dji-video.fifo
  → /opt/ffmpeg7/ffmpeg -f hevc -skip_loop_filter all -fflags nobuffer
    -flags low_delay → rawvideo NV12 (pipe)
  → gst-launch fdsrc ! rawvideoparse 1280x720 nv12 ! kmssink
    connector-id=35 plane-id=40 sync=true
  → meson_overlay_plane (terima YUV BT.601) → HDMI-A-1
```

Hardware overlay plane melakukan scale+colorspace convert — CPU cuma decode.
Video rc→fifo di-drop non-blocking kalau display belum baca (`SinkWriter`
di `streamer.py`) → backpressure tidak pernah menyentuh sesi RC.

### Mode operasi baru: `dji-mode.sh`

`standalone` = streamer+display on, mitm off (production, tanpa HP).
`mitm` = mitm+display on, streamer off (diagnostik). Watchdog mengikuti
service yang enabled: mitm/USB-1 hanya diawasi dalam mode mitm.

### Status standalone replay (belum teruji live)

- Trigger video ada di ~unit **4965** dari 13839 resep — di `--max-gap 0.5`
  (≈7/s) butuh ~12 menit; service pakai `--max-gap 0.02` → target <2 menit.
- Replay sudah terbukti menghasilkan respons RC masif (`duml_rx` ~18× tx).
- Risiko terbuka: binding e5 capture-specific bisa ditolak RC saat replay
  fresh — hanya bisa dibuktikan saat RC terpasang.

## ✅ VERIFIED 2026-11-07 (sesi 3) — trigger video + referensi eksternal

### Trigger video teridentifikasi

Satu-satunya command `08/*` dalam 60 detik sebelum `0x574a` hidup:

```
RCLink: 55 cc 49 57 | 0e 00 00 00
DUML:   55 0e 04 66 | 02 01 | 9f95 | 40 | 08 41 | 02 | 3d 0b
        header+crc8   src dst  seq  cty cs  ci  pay crc16
```

- `08/41` = **`uav_dm368_send_decode_capability_req`** (cmdset 0x08 =
  DM36x transcoder, dst=0x01 Camera) — app mendeklarasikan "bisa decode
  HEVC" (payload `0x02`), lalu port `0x574a` terbuka ~1.2 detik kemudian.
- Builder `pc/duss.py` menghasilkan frame **byte-identik** dengan capture
  (CRC8=0x66, CRC16=0x3d0b pada seq 0x959f) — sintesis terverifikasi.

### Struktur sesi (dari FreeFCC-USB + capture)

`refs/freefcc-usb` (`doesthings/FreeFCC-USB`, app Android yang bicara
langsung ke RC-N1 via AOA untuk Mini 3) mendokumentasikan:

- **Bootstrap**: `02 40 00 00 → 0x1F [00 00 01]` dan `→ 0x00 [00 00 01]`
- **Keepalive 2.5s**: `02 40 06 77 → 0x06/0x0E [01 01 00 FF FF 20 00 00]`
- **Envelope**: RCLink `55 CC 49 57 <len u32 LE> <DUML>`; route byte
  di-echo dari envelope yang diterima
- TX inter-frame 3ms; seq global monoton
- Link heartbeat mentah `0xb0` @1Hz (di capture; FreeFCC tidak pakai)

### `streamer_min.py` — sesi tersintesis (path baru)

`stb/streamer_min.py` menyusun sesi dari nol: bootstrap → keepalive
(06/77 + b0 + 00/0e+00/4f→0x2d) → `08/41` retry tiap 5s sampai video
mengalir. Service `dji-streamer-min` (disabled default). Mode switcher:

```
/root/dji-mode.sh standalone|synth|mitm
```

- standalone → dji-streamer (replay `video_recipe_trim.jsonl`, 9739 unit,
  burst di ~25-40 detik replay)
- synth → dji-streamer-min (minimal, bebas-crypto)
- mitm → dji-mitm (diagnostik saja)

### Referensi eksternal penting

- `samuelsadok/dji_protocol` — `usb_mobile_protocol.md`, `video_out_mobile.py`,
  `mb_protocol.md`: konfirmasi `VIDEO_STREAM_ID = 0x574A`; MAGIC_PACKETS
  Goggles N3 (00/99 + 00/88) — Mini3 beda (00/99 keepalive saja)
- `o-gs/dji-firmware-tools` — `comm_dat2pcap.py` referensi wire-format DUML,
  CRC8 poly 0x8C init 0x77, CRC16 poly 0x1021 init 0x3692
- `doesthings/FreeFCC-USB` — satu-satunya implementasi publik sesi AOA
  RC-N1 (bootstrap/keepalive/RCLink) — `refs/freefcc-usb`
- `wiki.dji-rev.com` known-DUML-commands + DeepWiki dissector — peta
  component ID (1=Camera/Ambarella, 3=FC, 4=Gimbal, 6=RC, 8=DM368, 9=OFDM)
- CosmoStreamer (komersial, Raspberry Pi) — membuktikan jalur ini
  feasible: RC-N1/N2/N3 → HDMI 1080p60 + telemetry→MQTT

### V4L2/meson_vdec — status riset lengkap (DIREVISI sesi 5)

Kesimpulan awal "driver broken total" **salah** — terkoreksi oleh bedah
mendalam dengan module yang dikompilasi lokal (`linux-headers` ophub ada
→ `meson-vdec.ko` bisa dibangun di STB):

- **Stream normal (x265 yuv420p/Main) decode SEMPURNA** — 318/300 frame
  EOS bersih. Kesimpulan "hang total" sebelumnya berasal dari test x265
  yang ternyata RGB/Rext (lupa `-pix_fmt yuv420p`).
- **Stream DJI**: decode jalan ~14-25 frame lalu firmware `gxl_hevc.bin`
  raise `dec_status=0x0F` di byte **932419** — deterministik, selalu di
  NAL yang sama (slice `saddr=14` poc=3 sesudah IDR#2), bukan kekurangan
  input (vififo masih berisi 546KB) dan bukan buffer (pool 17 capture).
  `0x0F` tidak ada di enum vendor (0xe→0x12) = fault code firmware saat
  CABAC decode slice itu. Semua respon gagal membangunkan: ACTION_DONE
  (0xff), DISCARD_NAL (0xf0), NAL_SEARCH_CTL recovery. Bisect file mulai
  IDR#2 → crash di byte ekuivalen (data-spesifik, bukan state).
- Perbaikan driver yang TERBUKTI membantu (dipakai di `meson-vdec-patched.ko`,
  `/root/meson-vdec-patched.ko`):
  - throttle `pend+ready-3` asli kolaps ke ~1 pada stream yang menahan
    ref (DJI) → diganti window vififo 512KB (esparser work hanya
    re-schedule via completion, jadi window tak boleh terlalu kecil)
  - `prepare_new_frame`: dst kosong → `wait_event` di `cap_wq`
    (threaded IRQ boleh sleep) + `wake_up` di capture `buf_queue`
  - OUTPUT queue min 24 buffer (memutus deadlock gst: chain terblokir
    acquire saat dec_loop menunggu capture DQBUF)
  - `src_buffer_size` 1MB → 64KB (aliran input lebih halus)
  - `0x0F` → log "firmware wedge" + abort bersih (bukan hang)
- Akar wedge kemungkinan di luar jangkauan driver: crash internal
  firmware pada konten slice DJI spesifik — butuh firmware/ucode
  sources atau vendor driver utuh untuk diagnosis lanjut.
- Rekomendasi produksi: **tetap SW decode** (ff7 + scale + kmssink,
  terbukti 0 drop ~200% CPU). HW decode hanya layak lewat image/kernel
  lain (LibreELEC AMLGX 6.16) atau STB beda.
- `hg680p.dtb` ada di komunitas (LibreELEC HG680P) — bisa memperbaiki
  EDID/DDC + pin yang benar vs P212 generic saat ini.
- PERINGATAN: jangan probe register `/dev/mem` mentah — crash+reboot
  (sudah kejadian sekali).

## ✅ VERIFIED 2026-11-07 (sesi 4) — fullscreen 1080p + smoothness

### Overlay plane TIDAK bisa scale

`meson_overlay_plane` (plane-id 40, GXL) fixed: src==dst, posisi (0,0).
`render-rectangle` apa pun → `drmModeSetPlane EACCES`. Bahkan 1920x1080
native ditolak **selama ada proses lain pegang DRM master** — zombie
gst-launch dari service yang distop harus dibunuh dulu (penyebab error
"misterius" saat tes paralel).

### Solusi fullscreen 1080p — TERUJI

```
ff7 decode 720p → swscale fast_bilinear (NEON) → NV12 1920x1080
→ kmssink plane 40 = fullscreen
```

- Decode+scale realtime: **0.993x**, ffmpeg ~200% CPU (dari 400%)
- 40 detik end-to-end tanpa satu drop/error
- `fdsrc do-timestamp=true` + `queue leaky max-size-buffers=2` +
  `sync=true max-lateness=20ms` — pacing halus, latency bounded
- `dji-governor.service` (baru): governor `performance` persisten
  (hilang saat reboot, sekarang oneshot systemd)

### 1080p sejati dari drone — TIDAK mungkin via RC-N1

Link O2 RC-N1 di-cap **720p30** oleh DJI (ini spek radio, bukan software).
DJI Fly di capture TIDAK mengirim satupun command negosiasi resolusi
(`08/78`, `08/69`, `02/18` semua absen) — stream memang hardcoded 720p.
`08/41` payload `0x02` = deklarasi codec (HEVC), bukan resolusi.
Output HDMI 1080p60 = upscale, seperti CosmoStreamer (yang juga cuma
mendapat 720p dari link lalu upscale output).

### Update `08/41` — dikirim berulang

Capture baru (mitm_capture.jsonl, sesi phone nyata): `08/41` dikirim
4× (t=46.0, 88.1, 88.3, 95.3), RC jawab `00 01 02`, video `0x574a`
hidup ~1.5s setelah kiriman terakhir (t=96.8) — pas saat user masuk
camera view. Replay/sintesis harus kirim `08/41` berulang (tiap ~5s),
bukan sekali.
- `mitm.py` perlu write-timeout ≥1s + queue besar — DJI Fly baca lebih lambat
  dari rate RC; timeout 200ms menyebabkan drop besar-besaran.

## ✅ VERIFIED 2026-11-07 (sesi 5) — trigger LENGKAP + simulasi E2E

### Sekuens enable-video definitif (identik di sesi A + B)

Cross-diff dua capture (`video_recipe` vs `mitm_capture.jsonl`, 87/89
command common): video `0x574a` terbuka persis setelah burst
"masuk camera view" — BUKAN `08/41` sendirian (t=46 di-ACK, video nihil):

```
00/4f -> 0x01  0100000000ffffffff   subscribe param kamera
00/4f -> 0x01  01cc030000ffffffff   param 0x3cc
00/4f -> 0x01  0198070000ffffffff   param 0x798
08/41 -> 0x01  02                   decode_capability (ACK 000102)
00/4f -> 0x01  0100000000e8030000   blok0 @ 1000 ms
02/b5 -> 0x01  00000000             query status
00/ff -> 0x0e  (kosong)
00/4f -> 0x2d  01000N0000ffffffff   N=0..5 — subscribe komponen VIDEO
00/4f -> 0x2d  040000000000000000   commit
```

VIDEO pertama @96.76s = **~0.1s setelah subscribe 0x2d blok-1**.
Pola berulang persis di t=2110/2226 (re-entry camera view) →
`00/4f`→`0x2d` adalah pembuka port yang sebenarnya.
Selama video jalan, phone refresh `00/4f`→`0x01` @~0.5-2s (subscription TTL).

### Payload session-bound vs replayable

Hanya 18/87 command berbeda antar-sesi: mayoritas timestamp
(`ea070b07`=2026-11-07 di `00/4a`, `02/54`, `03/20`, `03/da`) + counter.
**`11/15`+`11/43` = token crypto FC** (high-entropy, beda total per sesi,
ke dst=03/92) — tapi jalur video lewat `0x2d` = komponen LOKAL RC, tak
menyentuh crypto FC. FreeFCC/CosmoStreamer juga jalan tanpa crypto itu.

### Format video 0x574a — TERBUKTI

- Payload = HEVC Annex-B murni, chunked ~2040B per unit, tanpa sub-header
- Unit pertama 287B = init block; VPS/SPS/PPS diulang tiap GOP
- Decode dari IDR: **gambar bersih sempurna** (terverifikasi visual)
- Error `POC/cu_qp_delta` ffmpeg = artefak join mid-GOP saja, pulih sendiri
- ~4.5 Mbps, 1280x720 Main@30

### rc_sim.py — simulator RC dari capture nyata

`/root/rc_sim.py` (port 9920): phase-1 putar telemetri RX rekaman, gate
video pada trigger client (`08/41` + `00/4f`→`0x2d`), phase-2 putar video
rekaman. **KEDUA path lolos**: `streamer.py` (replay) & `streamer_min.py`
(sintesis) sama-sama memicu ARMED → video → FIFO → ffmpeg 200% CPU →
kmssink → HDMI. E2E software-chain tervalidasi penuh tanpa hardware.

### Bug yang diperbaiki

- `SinkWriter`: EAGAIN → tutup+buka ulang FIFO tiap ~1.6s + potong paket
  → decoder banjir error. Fix: NAL-gate (tunggu VPS/IDR), drop paket
  utuh saat FIFO penuh, fd tetap terbuka.
- `streamer.py`: tidak ada retry kalau video gagal → `--vid-timeout 90`
  exit → systemd Restart=always mengulang sesi.
- `streamer_min.py`: enable burst kini LENGKAP (0x2d blok 0-5 + commit +
  refresh subscribe 00/4f tiap 2s).
- `pgrep -f`/`pkill -f` via SSH membunuh shell sendiri (pattern ada di
  cmdline) — pakai bracket `rc_[s]im` dan pisahkan kill/launch.
- **`raw_gadget` EP0 write deadlock (akar "kadang zonk")**: kalau RC me-reset
  bus di tengah control transfer, `ep0_write` blocked selamanya (worker di
  `raw_ioctl`) → semua enumerasi berikutnya gagal → RC retry ~3 detik tanpa
  hasil. Gejala: `configured` stale, `rc>phone`=0, dmesg `enqueue_setup
  already queued???` berulang. Fix: restart bridge (worker baru langsung
  jawab enumerasi berikutnya). TODO permanen: watchdog "SETUP queued tapi
  tak diproses" → auto bounce fase gadget di `aoa_device.py`.
- **Jangan logging video `0x574a` ke JSONL mitm** — ~40 unit/s × 4KB hex
  sinkron ke eMMC = lag menumpuk. `mitm.py` sekarang skip logging video +
  throttle print channel (10 pertama saja).
- `pkill -f mitm.py` lewat SSH membunuh shell ssh sendiri (cmdline match) —
  pakai `/root/restart_mitm.sh`.

## Protokol — byte-exact

### Lapis 1: AOA handshake (RC = host, kita = "phone")
```
enumerate sebagai 18d1:4EE1 ("phone")
RC: GET_PROTOCOL (vendor req 51)   → jawab u16 = 2
RC: SEND_STRING ×N (req 52)        → log! RC kirim identitasnya
    (filter DJI: manufacturer="DJI", model ∈ {T600, com.dji.logiclink, WM160, AG410})
RC: START (req 53)                 → tutup fd (= detach) → tunggu 0.5s
re-enumerate sebagai 18d1:2D00, 1 iface, bulk 0x81(IN)/0x01(OUT) @512B, 100mA
```
Device strings yang kita presentasikan: `("Pi","DJI-Link","BETA0001")` — bebas,
RC tidak memfilter string kita (dia mengirim stringnya sendiri).

### Lapis 2: DUSS composite mux (di atas bulk)
```
55 CC | type u16 LE | len u32 LE | payload[len]
0x5749 = DUML   0x7530 = DUML aux (parser sama)
routing video (dari AoaRawChannelHandler, tervalidasi smali):
  0x574A = video primer jalur LB2/OcuSync-lite  → WM160 (StreamDataObserver
           juga log 0x574B+0x574E sebagai aux LB2)
  0x5750 = video SDR/LTE → SdrLteVideoController.m(true) — kandidat primer
           Mini 3 (OcuSync 2 = SDR), didampingi 0x574B/0x574E di cabang SDR
  0x574C/0x574D/0x5758 = video-class (WM160: 0x574D juga dipakai)
0x5759 = LB payload (non-video)   0x574F = diterima tapi tidak di-route
Semua video-class membawa header liveview 0x6d yang sama per doc — tapi itu
WM160-verified; format payload path SDR belum terkonfirmasi.
```
"Route bytes" `49 57` = type itu sendiri (LE). Parser: resync ke `55 CC`,
tolak len > 0x200000, buffer unit yang kepotong antar read.

### Lapis 3: DUML (payload channel 0x5749)
```
55 | len(10b)+ver(6b) LE | crc8(seed 0x77, poly refl 0x8C) |
sender | receiver | seq u16 LE | cmd_type | cmd_set | cmd_id | payload |
crc16(seed 0x3692, poly refl 0x8408) LE
sender/receiver byte = (index<<5)|type
sender: app=0x02 (WAJIB — 0x0a bikin FC lock motor) atau 0x82 (app idx4,
   nilai capture asli — FreeFCC sweep keduanya)
receiver WM160: RC=0x06 FC=0x03 camera=0x01 gimbal=0x04 dm368=0x08 battery=0x0D
receiver era RC-N1 (FreeFCC/iOS): camera/SVO=0x12/0x18/0x92, WIFI=0x07,
   LB_MCU_SKY=0x09, LB_68013_SKY idx3=0x6F, WLM=0xEE, broadcast=0x1F
cmd_type: 0x40=ACK | 0x20=ACK_BEFORE_EXEC | 0x06=NO_ACK | bit0-2=encrypt
```

### Lapis 3b: enkripsi SIMPLE (0x43) — sudah terpecahkan offline

Cipher untuk frame config FC (semua cmd_set 0x03 saat link terenkripsi):
keystream-XOR atas region `frame[9:len-2]`, key statis 21B `"xO$3(-2@#ld*viAQ~ixFE"`
(+NUL di index 21), nonce = seq frame sendiri. **Tanpa handshake/key-exchange** —
flag enkripsi di app cuma mirror apa yang peer kirim. Implement: `simple_filter` /
`encrypt_frame` / `decrypt_frame` di `pc/duss.py` (diverifikasi byte-exact vs
worked example native). Kirim terenkripsi: `--encrypt`; RX auto-decrypt.
**Default: plaintext `0x40` dulu** — paksa enkripsi di link polos = frame didrop FC.

### Lapis 3c: param KeyValue = hash-param `0x03/0xF7-0xFA`

Abstraksi "key" MSDK-v5 (Mini 3 memakai KeyValue di app) **tidak punya perintah
DUML sendiri** — native SDK me-resolve nama key → nama param flyc → hash
`h=(b+(h<<8)) mod (2^32-5)` (GBK bytes, tanpa NUL) → perintah by-hash:
`0xF7` info(type/size), `0xF8` read, `0xF9` write, `0xFA` reset.
Payload: `[hash u32 LE][value LE]` concat, tanpa count. Tervalidasi byte-exact
vs capture `fcc.json` (`g_config.flying_limit.max_height=500` → `8a237103f401`).
Tool: `--param-info/-read/-write` + `param_hash()` di `pc/duss.py`.
Tabel 687 param: `refs/.../flyc_param_infos.json`.
`0x03/0xF0` = param-info **by-index** → mengembalikan nama param — bisa
enumerasi peta nama↔hash langsung dari FC tanpa menebak (kandidat
`--probe`-lanjutan di Mini 3).
Param penting yang sudah dipetakan: `fc_dark_need_gps_0`=0 → unlock takeoff
gelap/no-GPS (motor cause 147); `max_height_0`/`max_radius_0`/
`go_home_altitude_0` via `0xF9` — atau TANPA hash via `0x03/0x2D`;
`serial_api_cfg.*` = clamp virtual stick.
Aktivasi: sekali pakai (`0x00/0x32` cloud + `0x03/0x62` report ke FC,
44B payload) — unit yang sudah diaktivasi DJI Fly tidak butuh apa-apa.

### Lapis 4: sesi RC-N1 (dari FreeFCC-USB)
```
bootstrap (sekali, setelah pipe terbuka):
  DUML 0x00/0x00 sender=0x02 type=0x40 dst=0x1F payload 00 00 01
  DUML 0x00/0x00 sender=0x02 type=0x40 dst=0x00 payload 00 00 01
keepalive (tiap 2.5s, dua frame):
  DUML 0x06/0x77 dst=0x06 payload 01 01 00 FF FF 20 00 00
  DUML 0x06/0x77 dst=0x0E payload 01 01 00 FF FF 20 00 00
seq global di semua frame; delay ~3ms antar frame; AOA eksklusif (DJI Fly harus tertutup)
```

### Lapis 5: resep liveview (WM160, dari DJI-Link src/core/drone.cpp — PRODUKSI)
```
0x02/0x09 → camera(0x01) : [src u8=0]
0x08/0x41 → dm368(0x08)  : 05 + 5×[codec u8, cap u32 LE]  (codec0 cap=FFFFFFFF)
0x08/0x42 → dm368        : fps u16 LE (=30) + 00 00
0x08/0x69 → dm368        : 00 64 00   (bandwidth)
0x02/0xB3 → camera       : kosong      (request I-frame — WAJIB, HEVC intra-refresh)
opsional: 0x02/0xAB → camera : [h265?1:0, 00]  (paksa codec H.264 kalau mau)
```
**Registry `uav_*` lengkap** (`full_table.txt` 449 entri, `cmds.json` 436
machine-readable): seluruh resep liveview ada di registry generik
(`uav_camera_set_liveview_source`, `uav_dm368_send_decode_capability`, dst) →
bukan spesifik WM160. Temuan: `0x08/0x78 uav_dm368_set_sh_start_live_streaming`
= perintah "start live streaming" eksplisit — kandidat eksperimen pertama kalau
Mini 3 diam setelah resep WM160 (`--cmd '8:78::'`). Keepalive `0x06/0x77`
**tidak ada** di registry uav → murni perintah link-layer RC-N1.
`pc/session.py` punya `--cmd 'set:id:hexpayload[:dst]'` (repeatable) untuk
kirim DUML arbitrer + `CMD_NAMES` decode nama di log RX.
Catatan: client DJI-Link WM160 **tidak** mengirim bootstrap/keepalive — remote
WM160 tidak mewajibkannya. RC-N1 (FreeFCC) mewajibkan. Tool kita kirim keduanya
(aman untuk kedua kasus). Keepalive pertama dikirim **satu periode setelah**
link terbuka (bukan di t=0).

### Lapis 4b: seluk-beluk sesi dari FreeFCC/iOS (RC-N1 family)
- **Route bytes di-echo**: parser app mengingat route pada envelope terakhir
  yang berisi command frame dan mengembalikannya. `49 57` = default yang benar.
- **Sweep sender×framing**: app mencoba `0x82/RCLink`, `0x02/RCLink`, `0x82/raw`,
  `0x02/raw` dan menghitung respons per (cmd_set,cmd_id). Jika perintah tidak
  dijawab, coba `--sender 0x82` dan/atau `--bare`.
- **Response matching** per (cmd_set<<8|cmd_id), bukan seq — RC mengirim
  telemetri tak diminta dengan seq sendiri.
- **waitForAircraft**: drone baru muncul di telemetri setelah RC re-link;
  tunggu sampai ~20 dtk. Tanpa drone ter-link, frame berhenti di RC.
- **Service-mode window**: `set 0x10 id 0x58 dst 0x12 payload 03 01 00`
  (AUTOTEST enter) membuka jendela untuk param writes; `... exit` menutupnya.
  Sebelum burst param: `set 0x03 id 0xDF dst 0x03 payload 01 00 00 00`
  (assistant unlock, cmd_type 0x40).
- **Video envelope terkonfirmasi di MFi**: `55 CC 4A 57 …` (~4 KB/unit) pada
  Lito 1/Mini 4 Pro — type `0x574A` konsisten lintas generasi.
- **Camera work mode**: `0x02/0x10 set_camera_working_mode` memisahkan
  liveview/record vs playback/download (media). Liveview butuh mode
  record/liveview (default); kalau kamera masuk playback, video berhenti.
- **"Stream comes on its own"**: tidak ada perintah "start push" terpisah di
  WM160 — begitu decoder caps/fps/bandwidth diset, video mengalir. Untuk Mini 3
  kemungkinan malah sudah mengalir sejak drone link; resep 0x08/* adalah
  konfigurasi encoder — diawasi di sniff.

### Lapis 6: video payload (channel 0x574A)
```
[16B header][slice ES]   header magic 0x6d:
  b1 bit0=is_i | b2-5 frm_idx u32 | w6 low20=pkt_len | b0x0A fps |
  w11 bit0=frm_end | b1-5 slice_idx | b9-15 pkt_idx | b16-25 frm_len_KB
reassembly: group by frm_idx → urut (slice_idx,pkt_idx) → selesai saat frm_end
codec WM160 = HEVC (bukan H264); tanpa keyframe mandiri → wajib 0x02/0xB3
atau cache+reinject VPS/SPS/PPS
```

### Lapis 7: media — download file dari SD (jalur DUML yang sama)

Tidak ada socket tersembunyi — list/download mengalir lewat `0x5749` biasa.
Ada **3 keluarga protokol file**; jangan dicampur:

```
KELUARGA A — litchis FileChannel (DEX-confirmed, tapi LEGACY/dibekukan):
  mux di DUML COMMON 0x00: 0x26 RequestFile (app→cam) / 0x27 GetPushFile (push)
  inner header 10B: [0x0A|ver<<6][cmd<<5|type][len u16][sess u16][off u32]
  recv side asymmetry: word@2 → pkt_idx=>>12, len=&0xFFF
  CmdId: List=0 File=1 Stream=2 Num=3 ; CmdType: REQ=0 DATA=1 ACK=2 PUSH=3
      ABORT=4 DEL=5 PAUSE=6 RESUME=7 ; SubType: ORG=0 THM=1 SCR=2
  LIST body (7B):  u32 startIndex|storage<<30, u16 count, u8 subtype
  FILE body (16B): u32 fileIndex, u16 subIdx, u8 subtype, u8 grade,
                   u32 offset, u32 length
  ACK body:        u32 next, u8 n, n×(u32 off, u32 len)
  ABORT body:      u32 reason (Force=1)
  → builder+parser byte-exact ada di pc/duss.py (litchis_*)

KELUARGA B — legacy handshake 0x22/0x24:
  0x00/0x22 RequestSendFiles, payload 1B FILE_SELECT_MODE (CURRENT=0)
  → kamera push 0x24 GetPushFiles. Class ada, tapi WM160 membalas 0xE0
    (INVALID_CMD) → kemungkinan mati untuk WM160. CAPTURE-PENDING.

KELUARGA C — modern native crossplayback (AKTIF di app):
  fetchMediaFiles*(JNI) → libcrossplayback .so — framing wire BELUM terlihat
  di DEX. Record list = MediaFile.fromBytes (layout sekuensial besar:
  fileSize u64 SETELAH nama string; thumbnail di-carve dari file asli via
  nailInfo 4×u64 offset/size; terminator = flag isPageLastFile per record).
  Defaults SDK: storage=EXTERN1, type=ORIGIN, index=1, count=-1, allList=TRUE.
```

- **MASUK mode** (DEX-confirmed): `0x02/0x10` payload `[03]` = MEDIA_DOWNLOAD
  (`[02]` = PLAYBACK biasa). Wajib sebelum list/download; liveview berhenti
  dalam mode playback.
- **0x00/0x20 BUKAN perintah file** — di app dia SetNewestVersions (wire 0x61);
  kamera WM160 membalas 0xE0 INVALID_CMD karena cmd_id 0x20 memang tidak ada.
  Kesalahan lama: menganggap 0x20 = get_file_list.
- 0xE0 = INVALID_CMD (cmd tak dikenal), 0xE4 = NOT_SUPPORT_CURRENT_STATE
  (state salah) — beda diagnosis.
- Probe siap: `python3 session.py <ip> --media-probe --litchis-list 0 8
  --record cap.jsonl` — `--record` menyimpan raw wire dua arah (JSONL
  {t,dir,hex}) untuk diseksi offline.
- Gap capture-pending: framing native crossplayback, payload DELETE (native
  only — tidak ada di DEX sama sekali), konfirmasi 0x26 litchis diterima WM162.

### Lapis 8: gimbal & RC (kontrol lanjutan)

Gimbal (receiver 0x04): `0x04/0x0C` speed `[yaw·10][roll·10][pitch·10][81 00]`
@10Hz; `0x04/0x14` absolute angle; `0x04/0x4C` recenter `[0xFE,0x01]`;
`0x04/0x08` auto-calibration.
RC channel (0x06): `0x06/0x01` channel params (3B records name/value/dir),
`0x06/0x04` hardware params (2B records), `0x06/0x19` stick mode
(Japan1/America2/China3/Custom4), `0x06/0x20` CE/FCC power (CE=0/FCC=1),
`0x06/0x35` gimbal-dial axis, `0x06/0x2F` pairing, `0x06/0x03` stick
calibration trigger (`0x06/0xF8` progress, 8-seg A–H).

Push streams yang terlihat gratis di sniff:
- `0x06/0x05` **live stick push**: aileron/elevator/throttle/rudder u16 @
  0/2/4/6 (DJI convention 364..1024..1684), wheel byte @10 `&0x3e`, mode @11
  `&3`, buttons @12, bandwidth @13. Offset dial gimbal beda per RC model
  (kandidat @8/@13/@16/@20/@22/@24) — capture RC-N1 menentukan.
- `0x06/0x1E` RC battery: u32 mV @0 + u8 % @4.
- `0x06/0x51` custom buttons C1/C2/5D: bit flags byte 0.

### Lapis 8b: flight control / virtual stick (FLIGHT_GATING, siap pakai)

```
Urutan otoritas (VERIFIED FLYING di WM160, MASTER_REPORT §3):
  0x03/0x80 [01]        OPEN_GROUND_STATION — gate kontrol SEBENARNYA di WM160
                      (0x49/0x80 itu SDKAgent v5 — beda generasi; [02]=close,
                      bukan [00] — bug "stuck on APP" kalau salah)
  0x03/0x2A [07]        START_MOTOR  / [01] AUTO_FLY takeoff
  stick @20Hz           0x03/0x8E DataFlycJoystick — VERIFIED FLYING:
     payload 17B: [flag][pitch f32][roll f32][throttle f32][yaw f32]
     *** urutan wire WM160 = pitch,roll,throttle,yaw (MSDK menukar
         yaw↔throttle dan roll↔pitch vs doPack generik!) ***
     flag = (RollPitch<<6)|(Vert<<4)|(Yaw<<3)|(Coord<<1)|adv
          0x4A = velocity+BODY (heading-relative, natural pilot)
          0x48 = GROUND ; 0x48+zeros = hover sempurna (triple-confirmed)
     watchdog FC: frame berhenti → hover (kirim netral saat idle)
     konfirmasi diterima: FLYC_STATE→Joystick(17) di OSD + SDKCtrlDevice
     →APP(1) (u8 @0x34 OSD common)
  0x03/0x2A [02] land | [06] GOHOME | [0x0C] cancel-RTH | [0x1E] ForceLand
  0x03/0xFE [01 00]     motor force-disable (panic)
  0x03/0x80 [02]        CLOSE_GROUND_STATION — kembalikan kontrol ke RC
```
Alternatif stick untuk generasi lain (HW menentukan — Mini 3 = MSDK v5,
kemungkinan TLV/KeyValue yang berbeda dari WM160 v4-only):
  0x01/0x0A special-TLV 4×11-bit (path RC emulation, native libGroudStation)
  0x01/0x02 virtual_rc_joystick 13B
  0x49/0x80 control auth + 0x19/0x40|0x41|0x46 arbitration (era v5)
Lainnya: `0x03/0x31` set home `[type][lat f64 rad][lon f64 rad][ivl]`
(**lat dulu** — kebalikan urutan OSD!); `0x03/0x2D` set limit
`[mode 1=High/2=Far/3=Low][u16 m]` — alt/distance TANPA hash; `0x03/0x2E`
read-back `[mode]`; `0x03/0x3B/0x3C` failsafe action; `0x19/0x40|0x41|0x46`
control arbitration; `0x00/0x32` activate_device (sekali, butuh akun+internet).

### Lapis 9: telemetri push yang sudah terdecode

- OSD `0x03/0x43` / `0x09/0x01`: lat/lon f64 radian, alt s16×0.1, vel 3×s16,
  att 3×s16, flyc_state @0x1e &0x7f (enum **non-dense**: 18 unused,
  Cinematic=19, SPORT=31, TRIPOD_GPS=38 — `FLYC_STATES` di duss.py),
  status dword @0x20 (gps_level >>18&F, motor >>3, air &0xE,
  gohome >>5&7), **sats u8@0x24** (bytecode-verified size=1; u16 salah),
  motor_cause u8@0x26 &0x7f, batt% u32@0x28, **VPS s8@0x29** (size=1;
  s16 salah — menelan flyTime), fly_time u16@0x2A **÷10 detik** —
  `osd_parse()` di pc/duss.py
- Battery `0x0D/0x02`: voltage u32 mV, current **s32** mA (neg=discharge),
  full/remain mAh, temp s16×0.1, cells u8, pct u8 — `battery_parse()`
- Diagnostik takeoff = motor_cause @0x26 `&0x7f` (96 kode; hanya sebagian
  bisa dibersihkan via perintah — sisanya PHYS/WAIT)

## Status verifikasi hardware — STB-02 (`stb-01`, kernel 6.12.112-ophub)

| Item | Status |
|---|---|
| UDC `c9100000.usb` (dwc2) | ✅ terdaftar, `dr_mode=peripheral` di DTB, state `not attached` = normal tanpa host |
| `raw_gadget` | ✅ modul tersedia (`/lib/modules/.../raw_gadget.ko`) |
| configfs + FunctionFS + semua fungsi gadget | ✅ `=m` lengkap |
| `dummy_hcd` | ✅ — loopback test tanpa kabel |
| `usbmon` | ✅ `CONFIG_USB_MON=y` |
| HW decode | ✅ `meson-vdec` loaded, `/dev/video0`, ffmpeg `hevc_v4l2m2m` |
| ffmpeg / python3 / git / gcc | ✅ 5.1.9 / 3.11.2 / ada / ada |
| port fisik | USB-2 = OTG/flashing (device); USB-1 = host (xhci terpisah) |
| eMMC bebas | ⚠️ ~2.1 GB — jangan simpan capture di rootfs |
| enumeration dwc2→host | ❓ **belum diuji fisik** (gate test pertama) |

### Verified langsung di STB-02 (2026-11-06)

- **Loopback AOA penuh PASS**: `18d1:4ee1` enumerate → `GET_PROTOCOL→2` →
  strings+START → re-enumerate `18d1:2d00` → bulk ep `0x81/0x01` → ping DUML
  24B lewat bulk pipe ke TCP :9910. (`selftest_loopback.sh` +
  `host_aoa_test.py --ping`, dummy_hcd)
- **UDC asli bound**: `gadget running`, `event: CONNECT`, TCP :9910 connect
  OK dari laptop via tailnet.
- `g_serial` bound → hardware UDC sehat.

### Gotchas kernel (hard-won, sekarang di script)

1. **`driver_name` raw_gadget dibandingkan dengan `gadget->name`**, BUKAN
   nama driver sysfs. dwc2 menamai gadget-nya dengan nama platform device →
   argumen benar = nama UDC sendiri:
   `--udc c9100000.usb --udc-driver c9100000.usb`. Passing `dwc2` (nama
   driver) → `failed to start: -19` (ENODEV di `gadget_bind`'s strcmp).
   Pengecualian: `dummy_udc.0` pakai gadget name `dummy_udc`.
   `run_bridge.sh` resolve otomatis.
2. **usb-role-switch** `d0078080.usb-role-switch/role` default `host` walau
   `dr_mode=peripheral` — `setup_gadget.sh` menulis `device` otomatis.

Update ophub issue #3701: klaim "enumeration gagal di B860H" **dicabut
penulisnya** — rig test-nya ternyata tidak punya host eksternal. Bug EP0 SETUP
latent tetap "plausible, not proven". Rekomendasi mereka = `dr_mode` di DT saat
boot — persis kondisi HG680P. Probabilitas sukses: tinggi.

## Delta Mini 3 vs WM160 (Mavic Mini 1)

| Area | WM160 | Mini 3 + RC-N1 | Confidence |
|---|---|---|---|
| end-to-end video | ✅ HW-verified | **komersial terbukti**: CosmoStreamer jual Mini 3+RC-N1 via USB-C Pi → HEVC ~1080p60 | **tinggi** |
| AOA handshake | ✅ HW-verified | sama (app DJI Fly identik) | tinggi |
| DUSS mux | ✅ | sama | tinggi |
| bootstrap/keepalive | tidak dipakai | diwajibkan (FreeFCC RC-N1) | tinggi |
| channel video | 0x574A (LB2) | **0x5750 (SDR)** + 0x574B/0x574E aux — O2=SDR, sniff menjawab | sedang |
| codec | HEVC | HEVC (O2 era); bisa dipaksa H.264 via 0x02/0xAB | tinggi |
| perintah liveview | resep di atas | ID kemungkinan sama; **receiver beda** — camera kemungkinan 0x18, encoder dm368 pengganti | sedang |
| deviceId | 59 | WM162 (o-gs wiki) — lewat transparan | tinggi |
| alamat komponen | camera=0x01 | camera/SVO=0x18/0x12/0x92 (capture FreeFCC) | sedang |

Unknowns yang hanya terjawab hardware:
1. enumeration dwc2 → host nyata
2. string identitas RC-N1 untuk Mini 3 (auto-terlog oleh `aoa_device.py`)
3. payload liveview Mini 3 (coba resep WM160; gagal → capture)
4. channel video Mini 3 — prediksi `0x5750` (SDR) atau `0x574A` jika tetap LB2
   (sniff `type_counts` menjawab)
5. format push telemetri: klasik `0x03/0x43` OSD (sudah ada decoder `--`-auto
   di session.py) atau bentuk baru (KeyValue → tetap frame DUML di kabel,
   nama terlihat via `--cmdmap`)
6. alamat komponen Mini 3: `--probe` men-sweep get_version ke semua kandidat

## Rencana eksekusi — fase saat STB baru datang

```
F0  identitas : /proc/device-tree/compatible, /sys/class/udc, FDTFILE
F1  setup     : bash stb/run_bridge.sh  → setup_gadget + dji-bridge binary
    self-test : sudo bash stb/selftest_loopback.sh (dummy_hcd, TANPA kabel)
                + python3 pc/host_aoa_test.py --ping  (di shell lain)
F2  enum fisik: port USB-2 → laptop; gadget harus muncul 18d1:4ee1 →
                python3 pc/host_aoa_test.py --ping  (laptop = host ala RC)
F3  RC-N1     : bridge jalan → colok port ATAS RC →
                log: GET_PROTOCOL → strings RC → START → bulk aktif
F4  sniff     : python3 pc/session.py <ip-stb> --cmdmap --probe
                → bootstrap + keepalive otomatis; catat type_counts channel;
                  serial drone auto-terdeteksi (1581*) = aircraft LINKED;
                  --probe = sweep get_version → peta alamat komponen Mini 3;
                  --cmdmap = decode 436 nama perintah uav_* di log;
                  push OSD 0x03/0x43 auto-decode (lat/lon/alt/sats/mode)
F5  video     : python3 pc/session.py <ip-stb> --liveview --wait-aircraft 20 \
                  --dump out.hevc
                sweep kalau diam: --sender 0x82 → --camera-dev 0x18 →
                --cmd '8:78::' (uav_dm368_set_sh_start_live_streaming) → --bare
                → ffplay -f hevc out.hevc   (atau hevc_v4l2m2m di STB)
F6  output    : ffmpeg → HDMI (ffplay/kmssink) ATAU mediamtx → RTSP/WebRTC
```

Fallback bila liveview tidak muncul:
1. **Sweep sender**: `session.py --liveview --sender 0x82` lalu `--bare`
   (FreeFCC membuktikan firmware menentukan kombinasi yang diterima).
2. **Sweep receiver**: `--camera-dev 0x18` (alamat camera era RC-N1).
3. **Capture DJI Fly Android**: `frida/dumpduml.js` di emulator rooted
   (hook `DataBase.getSendData/setRecData` — satu hook menangkap SEMUA frame
   + nama class semantik). Untuk capture *liveview-start* pakai
   `dumpduml.js` penuh — `dumpduml_lite.js` justru **memfilter cmd_set 0x08**
   (video) karena spam. Alternatif tanpa root: field debug `j` di
   `AoaRawDataReceiver` men-dump pipe mentah ke `/sdcard/aoa_recv.bin`.
4. **usbmon di STB**: `stb/capture_usbmon.sh` — lihat URB bulk/control mentah.
5. **Analyzer**: Cynthion/Facedancer di kabel antara STB dan RC.
6. **Jalur MFi (plan B besar)**: RC-N1/N3 juga bicara MFi ke iPhone via
   `com.dji.logiclink` > `com.dji.protocol` > `com.dji.common` > `com.dji.fly`
   > `com.dji.video` (ranking eksplisit dari DJI-FCC-Unlock-iOS, terverifikasi
   di RC-N3). Jika AOA gagal total, port `dji_protocol/emulate_iphone.py` —
   iOS path tidak butuh gadget-mode AOA, cukup device biasa yang merespons
   handshake MFi di interface vendor.

## Referensi utama

- `refs/dji-link` — implementasi lengkap (Apache-2.0). Kunci:
  `dji_link_beta/pi/*` (gadget+bridge), `dji_link_beta/reverse_docs/*`,
  `src/core/drone.cpp` (resep liveview), `src/pi/aoa_device.cpp` (edge cases)
- `doesthings/FreeFCC-USB` — bootstrap/keepalive RC-N1, accessory_filter
- `andreapianidev/DJI-FCC-Unlock-iOS` — jalur MFi/logiclink di RC-N1
- `o-gs/dji-firmware-tools` — DUML dissector + CRC
- `ophub/amlogic-s9xxx-armbian` #3701 — status dwc2 S905X
- `xairy/raw-gadget` — UAPI `/dev/raw-gadget`
- `cosmostreamer.com` — bukti komersial Mini 3+RC-N1 video HEVC lewat AOA
  (pipeline identik lintas generasi DJI Fly — indikator kuat path/format sama)
- CosmoStreamer wiki — bukti komersial Mini 3 + RC-N1 + Pi 4 gadget

## RE-arsenal — WSL2 + Ghidra 11.4.2 (2026-11-07, SETUP DONE)

REA MCP di Windows mentok (Ghidra P0=PE-only, JEB/jadx butuh lisensi/jar).
Solusi: **WSL2 Debian 13** — Ghidra bebas analisis ELF arm64.

- Lokasi: `/opt/ghidra`, `JAVA_HOME_OVERRIDE` di `support/launch.properties`
  (butuh `openjdk-21-jdk-headless`, BUKAN jre — Ghidra wajib javac)
- Project dir: `/opt/ghproj` (libuav_video_jni+libilink2), `/opt/ghproj_kv`
  (libsdk_key_value), `/opt/ghproj_dj` (libdatajar — 219MB, masih jalan bg)
- Libs: `refs/djifly/*.so` + `/tmp/djilibs/` (key_value, datajar, ilink_network)
- Scripts: `refs/ghidra-scripts/DumpFuncs.java` (decompile by name-regex),
  `DumpStringRefs.java` (string→xref→decompile)
- Run: `JAVA_HOME=... analyzeHeadless <projdir> <proj> -process <lib> -noanalysis
  -scriptPath /mnt/c/.../refs/ghidra-scripts -postScript <Script>.java <args>`

### Temuan DJI Fly (dari binary, bukan tebakan)

- `libsdk_key_value.so` **TIDAK stripped** — 844 tipe `uav::sdk::*Msg` dengan
  `Serialization`/`Deserialization`/`ToJson`. Peta penuh KeyManager→wire:
  - `AppDecodeAbilityListMsg` = `08/41` (struct 8B: codec_id u32 + flags u32)
  - `CameraFpvControlMsg`/`LiveViewSourceCameraMsg`/`LiveViewModeMsg` = kontrol FPV
  - `RcSoftSwitchModeMsg`+`SoftSwitchMode` = key flight-mode (relevan ke C++ dji-link!)
  - `MGHMSSubscribeComponentMsg` = subscribe komponen → kandidat `00/4f`→`0x2d`
- `libuav_video_jni.so` — **ffmpeg embedded** (demuxer/decoder H265 sendiri;
  dynsym sengaja corrupt/obfuscated). Pipeline: `net_recv → parser_pull/output
  → decoder → render`, plus `UAVVideoAudioAlignment` A/V sync dan
  `UAVVideoDispatcher::addVideoStreamObserver(productId,channelId)` via
  `uav::sdk::set_video_observer`.
- `libilink_network.so` — link layer: `AesGcmEncrypt/Decrypt`,
  `Adler32FragmentEncrypt`, `EncryptionSET`, tapi juga `NotEncryptCGITask` →
  **enkripsi per-channel opsional**. Video `0x574a` cleartext di USB (terbukti
  decode) — crypto hanya jalur kontrol `11/xx`, bukan syarat video.
- `libilink2.so` — iLink protobuf stack (auth/token `DartAuthManager`),
  bukan pembuka kanal video.

### Implikasi live test

- Tidak ada bukti crypto-gate untuk video; `0x2d` subscribe tetap hipotesis
  utama pembuka kanal (FreeFCC juga tanpa crypto).
- `libdatajar` bila selesai: cari mapping KeyManager→DUML (fungsi yang
  men-translate `set(key)` jadi frame `00/4f`) untuk finalisasi sintesis.

### Lanjutan dig libdatajar (sebelum live test)

- Kotlin metadata strings (key names, class names) **tidak ke-xref** oleh
  Ghidra — mereka konstanta metadata, bukan referensi code. Untuk mapping
  key→DUML harus cari dari sisi fungsi (konstanta cmdset/cmdid), bukan string.
- `ComponentVideoSigned`/`ComponentVideoUnsigned` — dua varian komponen
  video (signed = kanal terautentikasi). `0x2d` kemungkinan salah satunya —
  belum dikonfirmasi yang mana. Kalau subscribe capture kita ke varian
  Signed, synth butuh auth; kalau Unsigned, polos cukup.
- `keyConnectingVideoByDrone`→`keyConnectCompleteVideoByDrone`→
  `keyAuthenticationVideoByDrone` — state machine handshake video dengan
  tahap autentikasi (kemungkinan untuk jalur drone↔RC via link penuh,
  bukan phone↔RC lokal).
- FreeFCC-USB membuktikan RC-N1 menerima sesi DUML arbitrer di kanal
  command tanpa auth (cmdset 0x03/0x51 langsung dijawab).

## ✅ VERIFIED 2026-11-07 (sesi 6) — video hidup permanen + HDMI low-latency

### Keepalive video = request-response handshake (BUKAN subscription refresh)

Mekanisme yang selama ini dicari — penyebab video selalu mati ~3-4 menit:

```
RC (0x2d) -> phone : 00/0e attr=0x40 seq=N          tiap ~0.3 s
phone -> RC (0x2d) : 00/0e attr=0x80 seq=N pl=000100  <10 ms
```

- Query juga datang dari komponen 0x89 dengan bentuk identik — jawab sama.
- `03/d7` dari 0x03 = re-key handshake (~3x per sesi): respons =
  attr=0x80, seq sama, payload `req[:2] + 0000 + req[4:8]` (echo counter).
- `a9->02 03/ce` attr=0x40 TIDAK dijawab oleh phone asli — biarkan.
- Resend frame apa pun dengan seq basi diabaikan RC — jawaban harus
  membawa seq query saat ini.
- Implementasi: `make_pong()` di `stb/streamer.py`.

### Parameter CRC DUML (terverifikasi)

- **CRC16** ekor: reflected poly `0x8408`, init `0x3692`, atas `duml[:-2]`.
- **CRC8** header byte[3]: reflected poly `0x8C` (0x31 MSB), init `0x77`,
  atas `duml[0:3]` — cocok 29945/30001 frame di mitm_capture.

### Arsitektur video display (final)

```
RC-N1 -AOA-> bridge.py :9910 -TCP-> streamer.py -TCP :9990-> gst -> HDMI
```

- `VidServer` (streamer) listen :9990; tiap koneksi baru = stream mulai
  dari IDR — mustahil ada byte mid-GOP basi (bug fatal FIFO: objek pipe
  bertahan menyerahkan byte basi ke reader baru -> decoder abort-loop).
- Queue 150 payload (~0.5 s) + SO_SNDBUF 64KB (~0.11 s): latency cap
  ~0.6 s; kelebihan dibuang ke IDR berikutnya (bukan korupsi GOP).
- gst: `tcpclientsrc ! h265parse ! v4l2h265dec ! kmssink
  connector-id=35 plane-id=40 sync=false` — decode HW meson-vdec ~6%
  CPU (avdec ~143%).
- HDMI native 1280x720 (sumber memang 720p; TV yang upscale).

### Catatan kegagalan yang dipelajari

- `00/4f`->0x2d burst resend + `00/96`->0x2d heartbeat + seq+CRC segar:
  SEMUA tidak menahan video — renewal sebenarnya adalah pong `00/0e`.
- FIFO shared-pipe: F_SETPIPE_SZ mengecil gagal EBUSY bila pipe berisi.
- meson-vdec abort pada missing-ref (lebih galak dari libav) — tapi
  stabil permanen selama stream selalu mulai di IDR.
- Watchdog: gst hidup-tapi-macet dideteksi via /proc/PID/io rchar.

### Polish pass — splash, watchdog, RTSP (verified on STB)

- **Splash connecting**: fbdev `/dev/fb0` (1920x1080 BGRx, R@16 G@8 B@0)
  ditulis `display.py` saat start + refresh tiap 2 s selama `show==0`.
  Konten persisten -> otomatis tampil lagi di tiap jeda restart ketika
  `plane40_off.py` mematikan overlay plane 40 (di proses terpisah — satu
  proses tak bisa pegang master sekaligus men-displannya).
  `/root/make_splash.sh` = videotestsrc+2xtextoverlay -> BGRx raw.
  Raw DRM dumb-fb+SETPLANE dicoba dan ditolak ENOSPC oleh driver meson —
  fbdev adalah jalur yang benar.
- **Fan-out VidServer**: per-client queue(150)+sender thread. Klien baru
  tak lagi menendang display; RTSP publisher/recorder boleh menempel di
  :9990 yang sama, masing-masing mulai dari IDR, backpressure terisolasi.
- **RTSP**: mediamtx v1.21.2 + `dji-rtsp-push` (ffmpeg `-f hevc -i
  tcp://127.0.0.1:9990 -c copy -f rtsp`) -> `rtsp://<stb>:8554/dji`.
- **reseq_record**: semua unit tail-loop kini seq monoton + CRC16 segar
  (bukan verbatim beku); sesi video >6 menit kontinu terverifikasi.
- **Watchdog+**: stall-check kini memburu `display.py` (bukan gst-launch
  mati), + monitor CmaFree<96M -> restart display, + thermal>80C -> stop
  display sampai dingin, + rotasi display.log.
- `dji-plane-off.service` DIHAPUS (men-displan 37 = membunuh splash).
- `install_services.sh` kini idempotent untuk 7 unit penuh; unit aktual
  tersalin ke `stb/units/` sebagai source of truth.

## 2026-11-07 — tiga akar "connecting selamanya" + IDR-on-demand sejati

### 1) Plane 37 (fbcon, zpos=1 immutable) menutupi video selamanya
- `drm_client_dev_restore` berjalan tepat setelah SETPLANE/DROP_MASTER/close
  -> fbdev langsung mengaktifkan kembali plane 37. Atomic disable "sukses"
  pun tertimpa restore seketika (dmesg: `drm_atomic_commit` lalu
  `drm_client_dev_restore] fbdev: ret=0`).
- Fix final: display.py `acquire_master()` membuka card0 -> master ->
  `SET_CLIENT_CAP(atomic)` -> atomic off plane37 -> **fd ditahan seumur
  proses dan dioper ke kmssink lewat property `fd`** (kmssink mendukung
  properti fd = pakai fd yang sudah master). Master tak pernah dilepas ->
  restore tak pernah jalan -> plane37 mati selama sesi; exit -> restore
  normal.
- `vtcon1` di-unbind di run_display.sh (fbcon berhenti menggambar/commit).

### 2) Klien :9990 kelaparan — DJI memang IDR-on-demand
- Histogram NAL: `26x` beku 507 selama >30 menit sementara video 700MB
  tetap mengalir. IDR hanya bergelombang setelah subscribe lalu sepi.
- 02/b3 'Request IFrame' (doc WM160/dissector) TIDAK dipakai phone (nol
  unit */b3 di recipe) dan tidak dijawab RC. Replay handshake langka
  (08/41, 02/eb, 02/b5, 00/4f) juga tidak memicu IDR.
- **`00/4f` itu `DataCommonGetCfgFile`** — baca config file (offset
  bertambah 0->0x3cc->0x798 di capture), bukan open-video-port!

### 3) Mekanisme subscribe sebenarnya: `00/99 -> 0x28` pub/sub agent
- Recipe: 284 unit `00/99 -> rcv 0x28` payload `02 02 00 00 <ctr16>
  00.. 13 00 0d 00 "camcap_common"` — `uav_general_united_pub_sub_agent_req`
  (per DJI-Link DUML_COMMANDS_FULL). Phone poll ~1 Hz dengan request-id
  u16 monoton naik (0x9618..0x1032).
- Replay verbatim diabaikan sebagai dup app-level — `reseq_sub()` kini
  menaikkan outer seq (offset 14) + inner request-id (offset 23) + CRC16.
- Loop streamer: poll `00/99` tiap ~1 s (seperti phone) + ekstra tiap 2 s
  selama ada klien unsynced -> agent memancarkan burst IDR untuk
  "subscriber baru" -> klien mid-gap sync ~1-2 detik (uji: 808KB/8s,
  26x 198->201).
- Instrumen klien per-id: `cli=[#NSq150|#Nuq0]` di baris stats, miss log
  membawa id klien, `sub poll -> id=[...]` saat poll ekstra terpicu.

## 2026-11-08 — zero-copy HDMI, can_scale mati permanen, CRA-join, socket yatim

### 1) Jalur display zero-copy (dmabuf)
- Rantai lama CPU (videoconvert+crop+scale 720->1080) memakan ~75-125%
  CPU + antrian frame = delay. Kini: `v4l2h265dec capture-io-mode=dmabuf`
  -> `capssetter PAR=15/16` -> `input-selector` -> `kmssink`; plane40
  me-scale 720->1080 di hardware. Terukur CPU display.py ~10.5%, fb
  kernel dialokasikan `v4l2h265dec0:sr` (bukti dmabuf aktif).
- Leg splash dibatasi `framerate=2/1`: videotestsrc+3xtextoverlay
  memproses 1080p terus walau dibuang selector (~70% CPU sesi).

### 2) "Video kecil" = can_scale mati permanen (gstkmssink.c ~1667)
- `drmModeSetPlane` gagal sekali -> `self->can_scale=FALSE` SELAMANYA
  -> semua frame berikutnya ditampilkan 1:1 di tengah
  (`crtc-pos=1280x720+320+180`, kernel bukti langsung).
- Pemicu: leg splash 1080p PAR=1/1 -> hitungan aspect 2048x1080 ->
  commit DOWNSCALE -> meson overlay menolak. Upscale didukung,
  downscale tidak.
- Fix: kedua leg ber-caps PAR=15/16 -> hitungan persis 1920x1080 (1:1,
  tak ada commit terlarang) -> can_scale hidup -> video upscale.
  `plane-properties` lowercase diabaikan (Failed to set 'crtc_w');
  `render-rectangle` hanya kanvas letterbox — bukan solusi.

### 3) Join tanpa IDR — open-GOP protokol
- Setelah ~200 dtk grant, encoder murni open-GOP: `26x` beku, CRA
  (`0x2a`) ~tiap detik. Klien synced tetap decode; klien baru menunggu
  IDR selamanya ("menyambung" menit-an) — BUKAN stream rusak.
- Uji eksklusif v4l2: splice di CRA first-slice / AU head mana pun ->
  meson-vdec log `Couldn't find ref. frame` = drop graceful, NOL abort.
  avdec konvergen <1 dtk (frame PNG bersih).
- `_sender` kini: seed -> scan IDR (0x26/0x28), CRA first-slice (0x2a
  flag i+5&0x80), dan fallback AU-head setelah ~6 chunk (~1 dtk).
- KONTRA-indikasi: AU "IDR" degenerat pasca-resubscribe (span<32KB,
  <3 slice) = racun `dec_status 0xF` — filter `span<32768 and
  slice_count<3` (varian ~10-16KB lolos filter 16KB lama).

### 4) Queue resync storm saat decoder init
- `queue.Queue(150)` ~210KB ~0.4 dtk headroom. Display init v4l2 (nego/
  pool ~2-3 dtk) tak membaca socket -> q penuh -> resync -> buang data
  join -> decoder kelaparan -> dead -> ulangi. Kini 4096 (~5.6MB ~10dtk):
  `q-full` = 0 di sesi baru.

### 5) Kronologi "console flash" + socket yatim (replug)
- Exit display -> master lepas -> `drm_client_restore` -> plane37 aktif
  -> **teks console BASI di fb tampil** (unbind vtcon TIDAK menghapus
  isi fb). Fix: `dd` zeros ke fb0 sekali (restore->hitam) + `os.execv`
  saat EOS/ERROR dgn fd master diwariskan via `DJI_DRM_FD` — master tak
  pernah lepas, nol restore. execv hanya bila show>0 (kegagalan
  pra-frame harus exit normal -> fallback avdec run_display.sh).
- **Orphan socket (akar "30dtk tak tersambung")**: tcpclientsrc connect
  tepat saat proses streamer sekarat -> TCP ESTAB -> server mati ->
  CLOSE_WAIT — tapi EOS gst tak pernah tiba; display menunggu data di
  socket mati selamanya (tampak "tahan splash, bytes=idle"). Deteksi:
  `/proc/net/tcp` st=08 remote :270E milik inode fd sendiri -> reattach.
- Hard-exit `bytes=idle` lama = false-positive: upstream kelaparan ≠
  wedge decoder. Counter dipisah: `dead` (bytes flow, frame mati ->
  exit ~45-60dtk), `idle` (kelaparan -> tahan, plafon ~15 menit).
- Gate LIVE: splash dilepas hanya setelah 40 frame (~1.3dtk) — frame
  konvergensi hijau pasca join/resync tertutup; re-arm `gate=show+40`
  tiap turun ke splash.

### 6) Streamer churn saat RC tercabut
- Terukur `NRestarts=174` dalam satu boot: UDC not-attached tapi bridge
  tetap accept :9910 -> replay 9739 unit (~10s CPU) ke void -> drop ->
  restart. Fix: streamer menunggu `UDC==configured` TANPA replay —
  instance yang menunggu langsung handshake saat RC dicolok (pickup
  instan, bukan selesaikan replay sia-sia dulu).
- Lap terbuang: replay 26dtk dengan `duml_rx=0` sampai "bridge dropped
  us" (connect di celah AOA belum settle). Bail: rx total=0 selama
  >15dtk -> exit -> retry.
- Recipe 9739 unit = 202 dtk capture; `--max-gap 0.02` -> replay ~194
  dtk; `0.005` -> ~49 dtk (subscription units terkirim lebih awal;
  port video terbuka ~11 dtk setelah connect).
- Setiap `attach -> burst resubscribe` me-reset jendela grant —
  churn attach berturut-turut menahan IDR (deg-IDR burst pasca tiap
  attach terlihat di log).
