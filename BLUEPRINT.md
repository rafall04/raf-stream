# BLUEPRINT — DJI Mini 3 → HG680P HDMI Bridge

Dokumen arsitektur, kontrak komponen, dan model kegagalan. Ditulis dari
audit kode penuh + pengukuran live, bukan tebakan. Status: terverifikasi
sampai batas yang dicatat di §Validasi.

## 1. Arsitektur

```
        ┌─────────────── RC-N1 (host USB) ───────────────┐
        │  AOA handshk → bulk IN/OUT → DUSS/DUML traffic │
        └──────────────┬─────────────────────────────────┘
                       │ dwc2 gadget (UDC c9100000.usb)
        ┌──────────────▼─────────────────────────────────┐
        │ dji-gadget → setup_gadget.sh (raw_gadget,      │
        │ usb-role-switch → device)                      │
        │ dji-bridge → bridge.py :9910                   │
        │   rx_pump multicast → semua klien TCP          │
        │   TX hanya owner ('M' marker = mitm, atau      │
        │   klien tunggal = streamer)                    │
        └──────────────┬─────────────────────────────────┘
                       │ TCP :9910 (LogicLink units)
        ┌──────────────▼─────────────────────────────────┐
        │ dji-streamer → streamer.py                     │
        │   recipe replay (verbatim→reseq tail-loop)     │
        │   keepalive pong 00/0e, rekey 03/d7, hb 00/96  │
        │   sub poll 00/99 'camcap_common' (IDR grant)   │
        │   deg-IDR filter, seed cache, renewal burst    │
        │   VidServer :9990 — fan-out per-klien queue    │
        └──────┬───────────────────┬─────────────────────┘
               │ TCP :9990          │ TCP :9990
   ┌───────────▼────────┐  ┌────────▼─────────┐  ┌──────────┐
   │ dji-display        │  │ dji-rtsp-push    │  │ dji-mitm │
   │ display.py         │  │ ffmpeg -c copy   │  │ (phone   │
   │ input-selector:    │  │ → mediamtx :8554 │  │  via USB)│
   │  sink_0 splash     │  │ /dji             │  │          │
   │  sink_1 tcp→h265   │  └──────────────────┘  └──────────┘
   │  →v4l2h265dec→     │
   │  kmssink P40 →HDMI │
   └────────────────────┘
   dji-watchdog: EP0-deadlock, reset-storm, zombie :9910,
   USB-1 rebind, process-death, CMA/thermal, log rotation,
   UDC not-attached, duml_rx-stagnan. dji-governor: perf.
```

## 2. Kontrak komponen (invariant wajib)

| Komponen | Input | Output | Invariant yang dipegang |
|---|---|---|---|
| bridge.py | bulk IN/OUT AOA | TCP :9910 | RX multicast ke SEMUA klien; TX hanya owner; `dev.send` no-op saat AOA belum ready; drain stale frames saat dev baru |
| streamer.py | TCP :9910 + recipe | HEVC :9990 + DUML TX | Seq DUML monoton (`reseq`); req-id 00/99 monoton (`reseq_sub`); TX nihil saat mitm aktif; tidak replay saat UDC ≠ configured |
| VidServer | raw payload port 0x574a | per-klien queue 4096 | Payload pertama klien = seed AU utuh; deg-IDR AU (<32KB & <3 slice) di-drop; seed hanya dari AU utuh (batas VPS) |
| display.py | TCP :9990 / file | plane40 kmssink | Master DRM dipegang seumur proses (inherited via `DJI_DRM_FD` saat execv); plane37 dimatikan atomic; kedua leg live PAR=15/16; live gate ≥40 frame; mode `auto`+`standby=loop` punya pad media sink_2 (promo idle), advance via `meos` probe+poller + swap Gst.Bin saja (leg live tak direbuild); skala/letterbox file via `render-rectangle` plane (rantai sw terlarang — >realtime) |
| watchdog.sh | sysfs/dmesg/journald | systemctl restart | Hanya aksi pada bukti (bukan `is-active`); tidak restart display saat socket sah diam |
| mitm.py | USB-1 phone | TCP :9910 owner 'M' + UDP tap :8555 | AOA strings byte-exact RC-N1; saat aktif, streamer = tap murni (TX nihil) |

## 3. State machine utama

**Streamer:**
```
wait UDC configured → connect :9910 → wait-first-bytes (8s) →
recipe replay verbatim → tail-loop reseq (+drop_sub) → RUN
   ├─ vid=0 >25s  → exit (systemd restart) — kecuali UDC down → tunggu
   ├─ video stall 45s → exit — kecuali UDC down → tunggu → stabil 8s → exit
   ├─ duml+vid=0 >15s → bail cepat (link belum settle)
   ├─ IDR flat >45s + ada klien unsynced → drought flag + renewal burst/60s
   │   → >10 mnt → exit (aktivasi ulang)
   └─ resub burst /15s, hb /1s, sub poll /5s, extra poll /2s per unsynced
```

**Display:**
```
acquire master → atomic-off 37 → splash(sink_0) → attach :9990
  → parse→decode→probe count
  → gate 40 frame → sel pad video → LIVE
  ├─ EOS/ERROR show>0 → reexec (master diwariskan → nol flash)
  ├─ EOS/ERROR show=0 → exit normal (wrapper nilai → fallback avdec)
  ├─ bytes mengalir + frame mati → dead++ >45 → os._exit (wedge)
  ├─ bytes diam + UDC=configured → idle++ >15mnt → exit
  ├─ bytes diam + UDC≠configured → tahan splash tak terbatas
  └─ socket CLOSE-WAIT sendiri → re-attach

standby=loop (auto): pad media(sink_2) aktif saat idle — file →
  decode→mq(dlm Gst.Bin)→ghost→sel→kmssink render-rectangle; EOS video
  → flag meos (event di-DROP di ghost) → poller → swap bin saja (pad
  sel persisten; leg live/socket/gate tak disentuh); live → active-pad
  video + BLOCK ghost + vol=0 + sync=false + rect full; kembali →
  pending advance bila file habis di belakang. Live-src mati saat
  standby → ditelan + retry timer /proc/net/tcp :2706 st=0A
mode=loop (non-auto): pipeline tersendiri sync=true, decoder hw
  (video0 bebas), rebuild per file — seek v4l2 terlarang
```

## 4. Matriks kegagalan → pemilik recovery

| Gejala | Bukti | Pemulihan | Pemilik |
|---|---|---|---|
| RC cabut | UDC≠configured | streamer wait, display splash hold | streamer+display |
| RC colok → link kotor | duml_rx=0, enum storm | watchdog restart bridge (reset-storm rule) | watchdog |
| EP0 deadlock | dmesg 'already queued' | restart bridge + dmesg -c | watchdog |
| raw_gadget basi | configured tapi duml_rx stagnan 88s | restart bridge | watchdog rule 6 |
| :9910 zombie CLOSE-WAIT | `ss close-wait sport :9910` | restart bridge | watchdog |
| bridge drop streamer | 'bridge closed' | systemd restart streamer | systemd |
| replay sia-sia rx=0 | 15s diam total | exit → retry systemd | streamer |
| :9990 socket yatim | /proc/net/tcp CLOSE-WAIT inode sendiri | re-attach paksa | display |
| decoder wedge | bytes mengalir, dead>45 | os._exit → respawn | display |
| deg-IDR burst | AU IDR <32KB <3 slice | drop di VidServer | streamer |
| IDR drought | nal 26x/28x flat >45s + unsynced | renewal burst; wait; >10mnt exit | streamer |
| v4l2 abort berulang | dec_status 0xF ×2 tanpa frame | lock avdec sesi ini | run_display.sh |
| kmssink can_scale mati | crtc-pos 1280x720+320+180 | PAR=15/16 kedua leg (preventif) | display config |
| console flash | exit → fbcon restore | DJI_DRM_FD execv + fb0 nol | display+wrapper |
| green konvergensi | frame pertama mid-GOP | gate 40 frame | display |
| CMA <32MB | /proc/meminfo | restart display | watchdog |
| mitm_capture >64MB | stat file | rotasi 32MB | watchdog |
| USB-1 kosong (mitm) | lsusb kosong 45s | rebind xhci | watchdog |
| disk penuh | df 92%→76% | artifacts ~1GB diarsip ke laptop 2026-11-09; watchdog merotasi mitm_capture.jsonl >64MB | watchdog+operator |
| EOS agregat bin tak pernah dipost | alsasink EOS ~10x/file, bus diam | flag `meos` di probe ekor + poller GLib | display |
| leg file "beku" ~60 frame | frame ~200ms late → QoS-drop semua | render-rectangle plane; rantai sw dilarang | display |
| :9990 tak listen saat RC tercabut | streamer bind mati permanen (EADDRINUSE) → display respawn-loop | bind retry + telan ERROR tcpclientsrc + retry timer | streamer+display |
| bin swap deadlock/nama bentrok | set_state(NULL) di main-loop; nama duplikat | teardown thread + nama unik + EOS DROP di ghost | display |

## 5. Budget latensi (diukur + dihitung)

| Tahap | Latensi | Catatan |
|---|---|---|
| encode→radio→RC | ~200-400ms | inheren DJI, tak bisa diubah |
| VidServer queue | ≤~50ms | queue drain per live write |
| h265parse | ~33ms | 1 frame boundary |
| v4l2 DPB | ~33-66ms | 1-2 frame referensi |
| kmssink commit | 0-16ms | processing-deadline 8ms (dari 15) |
| **total STB** | **~50-100ms** | vs zero-copy lama ~200ms+ |
| join baru klien | ~1-2s | CRA/mid-GOP splice (dulu: menit, IDR window) |
| replug→LIVE | ~15-20s | handshake RC ~10s + replay + join + gate — lantai protokol |

## 6. Deployment & clone

```
./deploy.sh root@<ip>        # push stb/ + pc/, chmod +x
ssh root@<ip>
cd ~/dji-stb/stb && sudo bash install_services.sh   # 9 unit + recipe + mediamtx
```

File wajib runtime (di-ship repo): `display.py streamer.py run_display.sh
bridge.py aoa_device.py raw_gadget.py setup_gadget.sh watchdog.sh
plane37_off.py plane40_off.py mitm.py mediamtx.yml make_splash.sh
video_recipe_trim.jsonl renewal.jsonl bin/mediamtx` + `units/*.service`.

Catatan: `units/dji-bridge.service` + `dji-gadget.service` di repo =
snapshot referensi; installer **meng-generate ulang** keduanya dengan path
`${HERE}` — file units/ bukan source-of-truth untuk dua unit itu.

`.gitattributes` = `eol=lf` untuk sh/py/service/jsonl — clone di Windows
wajib ini (CRLF merusak script STB).

## 7. Observability — sinyal kebenaran

| Cek | Perintah | Sehat |
|---|---|---|
| USB | `cat /sys/class/udc/*/state` | `configured` |
| Link | journal streamer | `duml_rx` naik |
| Video | journal streamer | `video=pkts` naik |
| Join | `cli=[#N Sq0]` | S=sync q~0 |
| Decode | display.log | `show=` naik `live=True` |
| Plane | `cat /sys/kernel/debug/dri/0/state` | P40 fb terisi, P37 fb=0 |
| RTSP | `ffprobe rtsp://127.0.0.1:8554/dji` | hevc 1280x720 |
| State file | `/tmp/dji_idr_drought` | absen saat semua synced |
| | `/tmp/dji_seed_ok` | ada sesudah IDR pertama |
| | `/tmp/dji_mitm_active` | <45s mtime saat mitm |

## 8. Batas diketahui & TODO jujur

- **Disk 92%** — ~1GB dev-artifacts di /root (usb_watch.log 717M,
  mitm gzip 118M, *.hevc/bin captures). Putuskan hapus/arsip — eMMC tak
  akan selamat setahun tanpa ini. Rotasi display.log+mitm_capture sudah
  otomatis di watchdog.
- **mitm.py** = path sekunder ter-deploy tapi belum teruji phone-sungguhan
  (belum ada sesi DJI Fly end-to-end).
- **Jendela IDR siklikal** — klien baru saat drought & tanpa seed: join
  via CRA/mid-GOP (terverifikasi), tapi frame awal bisa 1-2 frame
  ter-drop graceful (decoder discard, tidak hijau karena gate).
- **Latency formal belum diukur timestamped** — stopwatch-photo test
  disarankan (banding vs DJI Fly).
- **v4l2 multi-open** — jangan pernah uji gst-launch saat display aktif;
  /dev/video0 satu konsumen.
- plane40_off.py pakai legacy SETPLANE (yang sebelumnya terukur no-op
  untuk plane37). Ia tidak diverifikasi berfungsi — kmssink menimpa
  plane40 saat PLAYING anyway; dibiarkan apa adanya (harmless).
- Splash = satu-satunya tempat watermark layak: leg video zero-copy
  dmabuf tak bisa diberi overlay tanpa copy ke CPU.

## 9. Rencana validasi (ground, props off)

- [ ] Colok RC → UDC configured <5s → LIVE ≤20s → gambar bersih (tanpa
      hijau/console) → RAF MULTIMEDIA tampil saat connecting
- [ ] Cabut saat LIVE → splash kembali <10s → colok → LIVE ≤20s
- [ ] `systemctl restart dji-streamer` → display re-attach ≤15s
- [ ] `systemctl restart dji-display` → join ≤3s (seed/CRA)
- [ ] Restart bridge → enum sukses, duml_rx pulih <30s
- [ ] ffprobe RTSP → hevc 1280x720
- [ ] >1 jam soak: drop=0, show naik ~30fps, CmaFree stabil, no abort baru
- [ ] Latensi: foto stopwatch HP vs layar TV — catat glass-to-glass
