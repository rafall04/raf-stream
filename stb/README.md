# stb/ — bundle deploy ke HG680P (sisi USB gadget)

File `aoa_device.py`, `raw_gadget.py`, `bridge.py` adalah **salinan verbatim**
dari `refs/dji-link/dji_link_beta/pi/` (DJI-Link, Apache-2.0) — proven & sudah
terbang di hardware. `bin/dji-bridge` + `bin/dji-netctl` adalah **binary
static aarch64** dari release `dji-link-pi.tar.gz` v1.0.0 — jalan tanpa
Python/library apa pun. `pi_src/` = source C++-nya kalau perlu rebuild
(`build_bridge.sh`).

## Install penuh dari clone (satu perintah)

```bash
# dari root repo — push + apt deps + unit + enable + smoke otomatis:
./deploy.sh root@<ip-stb>

# manual (di STB): cd ~/dji-stb/stb && sudo bash install_services.sh
# SKIP_INSTALL=1 / SKIP_SMOKE=1 untuk memecah tahap
```

Sudah termasuk: recipe `video_recipe_trim.jsonl` (replay handshake),
`renewal.jsonl` (re-subscribe burst jendela IDR), `bin/mediamtx` →
`/usr/local/bin`, `mitm.py` (jalur phone DJI Fly), `plane37_off.py`,
9 unit systemd (`units/` = source of truth). Setelah install:
HDMI menampilkan splash `RAF MULTIMEDIA` + indikator `USB/LINK/VID/DEC`,
RTSP di `rtsp://<stb>:8554/dji`.

## Deploy manual / debug

```bash
sudo bash run_bridge.sh              # setup + binary dji-bridge (produksi)
sudo bash run_bridge.sh --python     # atau python bridge.py (debug)
```

Tanpa kabel/RC sama sekali (uji state machine AOA end-to-end di STB):
```bash
sudo bash selftest_loopback.sh           # terminal A — gadget di dummy_hcd
python3 ../pc/host_aoa_test.py --ping    # terminal B — host virtual
```

Port fisik yang dipakai: **USB-2** (port flashing/OTG). Kabel USB-A male ↔
USB-C male dari port itu ke port **atas** RC-N1 (port cradle/HP).

## Verifikasi berjenjang

1. `bash setup_gadget.sh` → harus ada `UDC found: c9100000.usb` +
   `/dev/raw-gadget present` (juga menulis `device` ke usb-role-switch)
2. `python3 bridge.py` → log `gadget running — waiting for USB events`
   (run_bridge.sh meneruskan `--udc-driver <udc>` otomatis — raw_gadget
   mencocokkannya dengan `gadget->name`, yang untuk dwc2 = nama platform
   device, BUKAN nama driver `dwc2`)
   - Tanpa kabel/host: diam di sini (normal — `state=not attached`)
   - Self-test tanpa RC: `modprobe dummy_hcd` lalu bind ke UDC dummy
3. Kabel ke **PC/laptop** dulu → PC harus lihat `18d1:4ee1`; kalau host-nya
   mengirim AOA → re-enumerate `18d1:2d00`
4. Baru colok ke RC-N1 → log menunjukkan GET_PROTOCOL → strings RC
   (`DJI / ??? / serial / fw`) → START → re-enumerate → bulk aktif
5. Dari PC: `python3 pc/session.py <ip-stb>` → bootstrap + keepalive + sniff

## Output video di STB (`stream_out.sh`)

Pipeline: `pc/session.py 127.0.0.1 --liveview --dump -` → ffmpeg → sink.
`pc/` ikut ter-deploy — semua jalan lokal di STB, tanpa laptop.

```bash
./stream_out.sh hdmi              # decode VPU meson-vdec → /dev/fb0
./stream_out.sh udp 100.x.y.z     # MPEG-TS → ffplay udp://@:9000 di laptop
./stream_out.sh rtsp              # publish rtsp://<stb>:8554/dji (HEVC, VLC)
./stream_out.sh mediamtx          # server RTSP/WebRTC saja (bin statis 66MB)
```

Catatan: RTSP/WebRTC ke browser butuh H.264 — stream drone itu **HEVC**, jadi
viewer paling jujur = VLC/ffplay via RTSP atau UDP mentah. Transcode H.264
di S905X bisa di-coba (`-c:v libx264 -preset ultrafast`) tapi CPU-nya tipis.

## Opsional: WiFi AP + netctl

`bin/dji-netctl` (HTTP API :9911) mengelola WiFi/AP ala Pi — berguna kalau STB
mau jadi kotak mandiri (PC connect ke AP-nya). Untuk bench work **ethernet
saja cukup** (STB-02 sudah online) — service AP tidak dipasang default.
`install_services.sh` hanya memasang `dji-gadget` + `dji-bridge`.

## Catatan hardware (terverifikasi di STB-02, kernel 6.12.112-ophub)

- UDC `c9100000.usb` = dwc2, 7 endpoint, high-speed, `dr_mode=peripheral` di DTB
- `raw_gadget.ko` ada sebagai modul (tidak perlu compile)
- `CONFIG_USB_MON=y` — usbmon siap kalau perlu capture
- `dummy_hcd` tersedia untuk loopback test tanpa kabel
- Port USB-1 tetap host (xhci terpisah) — bebas untuk storage/keyboard
- Root fs eMMC hanya ~2 GB bebas — jangan tulis capture besar di situ
