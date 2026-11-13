# DJI STB Bridge — Aturan Kerja

Drone-control project. **Test di darat, baling-baling dilepas.** Jangan
terbang untuk validasi.

## Checklist awal sesi (WAJIB sebelum menyentuh kode)

1. **Load skills**: `dji-protocol-reference`, `dji-bridge-debug`,
   `diagnosing-bugs`, `superpowers-systematic-debugging`,
   `superpowers-verification-before-completion` (+ `protocol-reverse-engineering`
   untuk kerja wire-level, `security-and-hardening` untuk audit input).
   Semua di `~/.agents/skills/` — invoke, jangan diingat dari konteks lama.
2. **Baseline**: `ssh root@<stb-ip> 'sh /root/smoke_test.sh'` —
   gerbang regresi L1-L5 + invariant tiap fix yang pernah commit.
   Merah sebelum ubah = masalah sudah ada, BUKAN salahmu.
3. **Sebelum commit**: `bash check.sh` (AST semua .py + `sh -n` semua .sh)
   — menangkap file rusak sebelum ter-deploy. `deploy.sh` kini
   menjalankan smoke otomatis di akhir (`SKIP_SMOKE=1` untuk bypass).
4. **Sejarah**: summary sesi sebelumnya di
   `%APPDATA%/devin/cli/summaries/history_*.md`; **REGRESSIONS.md** =
   ledger semua bug yang pernah difix (gejala→akar→commit→cek smoke);
   fakta terukur di file ini + `RESEARCH.md` + `BLUEPRINT.md`; corpus
   protokol di `refs/`.
   **Jangan menebak semantik DUML** — cek refs dulu.
5. **Bukti sebelum klaim**: tabel verifikasi di bawah — `is-active` saja
   TIDAK PERNAH cukup.

## Arsitektur satu baris

```
RC-N1 ─USB─► raw_gadget+bridge.py :9910 ─► streamer.py ─► VidServer :9990
                                              ├─► display.py → kmssink P40 (HDMI)
                                              └─► ffmpeg → mediamtx :8554 (RTSP)
```

Target: `root@<stb-ip>` (HG680P, hostname stb-01).

**Layout deploy dua-path (terukur, jangan ditebak):**
- `/root/` — copy untuk: streamer.py, display.py, mitm.py, run_display.sh,
  watchdog.sh, plane37/40_off.py, make_splash.sh, mediamtx.yml, *.jsonl,
  splash.bgrx. Unit-unit menunjuk ke sini.
- `/root/dji-stb/stb/` — clone jalan in-place untuk: **bridge.py** +
  **setup_gadget.sh** (bridge butuh sibling `aoa_device.py`/`raw_gadget.py`,
  ExecStart memakai `${HERE}` installer). Deploy bridge ke path INI, bukan
  /root/ — scp ke /root/bridge.py hanya membuat orphan (terbukti 2026-11-09:
  fix RST "terdeploy" tapi tak pernah live).
- `install_services.sh` = satu-satunya yang me-refresh copy /root/ dari
  clone. Deploy lengkap = `deploy.sh` + re-run installer.

## Deploy

```bash
# app biasa -> /root/ ; bridge + gadget -> /root/dji-stb/stb/
scp stb/<file>.py root@<stb-ip>:/root/
scp stb/bridge.py stb/setup_gadget.sh root@<stb-ip>:/root/dji-stb/stb/
ssh root@<stb-ip> 'python3 -c "import ast; ast.parse(open(\"/root/<file>.py\", encoding=\"utf-8\").read())"'
ssh root@<stb-ip> 'systemctl restart dji-<unit>'
```

Unit (source of truth = `stb/units/`): dji-gadget, dji-bridge, dji-streamer,
dji-display, dji-watchdog, dji-mediamtx, dji-rtsp-push.
Installer idempotent: `stb/install_services.sh`.

## Verifikasi (wajib sebelum klaim apa pun)

| Klaim | Bukti yang diperlukan |
|---|---|
| USB link hidup | `cat /sys/class/udc/c9100000.usb/state` = `configured` |
| Data RC mengalir | `journalctl -u dji-streamer -o cat` → `duml_rx` naik, `video=pkts` naik |
| IDR diproduksi | `nal=…26xN` naik (bukan beku) |
| Klien sehat | `cli=[#N Sq0]` (S=sync, q=queue — harus ~0) |
| Display live | `/root/display.log` → `show=` naik, `live=True`, `drop=0` |
| RTSP serve | `ffprobe rtsp://127.0.0.1:8554/dji` → `hevc,1280,720` |

`systemctl is-active` TIDAK pernah cukup — service bisa aktif tapi wedged.

## Diagnosa

Ikuti skill `dji-bridge-debug` (di ~/.agents/skills): gejala → lapis →
perintah pasti L1..L5. Jangan restart sebelum bukti diambil — restart
menghapus bukti.

## Fakta stream (terukur, jangan ditebak ulang)

- Jendela IDR: ~200 dtk sesudah aktivasi/resubscribe, lalu encoder murni
  open-GOP — `26x` beku itu SAH, bukan macet. Titik join selain IDR:
  CRA `0x2a` first-slice (~tiap detik) atau AU head mana pun (mid-GOP).
  meson-vdec membuang frame tanpa ref (`Couldn't find ref. frame`) —
  BUKAN abort. Klien :9990 join ~1-2 dtk kapan pun.
- Racun v4l2 = AU "IDR" degenerat pasca-resubscribe (≤16KB single-slice).
  Filter: drop AU IDR <32KB DAN <3 slice. IDR asli DJI ≥41KB multi-slice.
- `/dev/video0` multi-open: gst-launch uji + display.py berjalan bersamaan
  = `dec_status 0xF`/`buffer pool failed` PALSU. Uji hanya saat
  `fuser /dev/video0` kosong. Jangan stop display synced untuk uji —
  respawn-nya butuh join ulang.
- kmssink menuntut `PAR=15/16` (turunan EDID panel ini). Stamp lewat
  `capssetter` sesudah capsfilter; videoscale menghitung PAR dari DAR
  input dan bisa menimpa nilai hulu. `processing-deadline=8ms` diset
  (default 15ms menahan buffer terlalu lama). `can_scale` MATI permanen
  bila satu drmModeSetPlane gagal — jangan pernah biarkan commit
  downscale lolos (itu yang membuat video "kecil").
- `kmssink::calculate_display_ratio` menerapkan PAR panel (15/16 EDID)
  ke SATU sisi: AR caps ≤16/9 → sisi menyusut (video 720→675, src ≤
  fb → lolos); AR caps >16/9 → sisi MELEBAR melampaui fb (foto
  1024→1092) → `drmModeSetPlane ENOSPC` → reexec. Leg foto WAJIB
  caps AR ≤16/9 DAN PAR=1/1: videoscale menulis PAR preserve-DAR
  (576/571) yang tetap memicu pelebaran — stamp eksplisit di capsfilter.
  Leg foto = jpegdec → [videoscale 16:9 (AR>16/9) | evencrop (dim
  ganjil)] → imagefreeze → videorate → autocrop — imagefreeze memutar
  buffer hasil jadi biaya transform hanya 1 frame. Advance via timer
  `photo_secs` yang menulis st["meos"] (imagefreeze tak pernah EOS).
  `pad.query_caps` di pad-added decodebin masih RANGE — baca
  `pad.get_current_caps()` untuk dimensi tetap.
- Socket yatim :9990: klien connect ke streamer yang mati seketika →
  TCP CLOSE-WAIT tanpa EOS gstreamer → display menunggu di socket mati
  selamanya. Deteksi sendiri via `/proc/net/tcp` (st=08, port remote
  hex `:2706`=9990 — bukan 270E) + inode fd milik proses.
- Wedge HILIR menyamar sebagai upstream-starve: decoder/sink tersendat
  → push macet → `bytes_in` beku padahal data menunggu. Diskriminator:
  `rx_queue` socket sendiri >0 = wedge (data ada, tak terbaca) →
  hard-exit; =0 = starvation sah → tahan splash.
- Wedge display di N-buf (36 v4l2, ~7 avdec = kedalaman pool decoder):
  chain input-selector di bawah kunci GLOBAL `sel->lock`; pad aktif
  memegangnya selama push->kmssink render; render hang (wait-for-flip,
  kandidat vblank mati pasca plane37-off) -> SEMUA pad block -> pool
  decoder kering -> socket penuh -> resync storm. Fix: `queue
  leaky=downstream max=2` antara tail dan sel — decoder drain, streamer
  tak resync, sel pulih = lanjut instan. Probe liveness pasca-queue.
- EOS prematur leg standby (~1.5-4.5dtk pd file 8dtk, cuma ~2 buffer
  lolos ghost): bin media baru join pipeline berumur -> running_time
  buffer ~0 vs clock ~15dtk -> kmssink menilai "hopelessly late" pakai
  ts-offset BASI leg sebelumnya -> drop instan + QoS upstream -> avdec
  skip ~190 frame ("Dropping frame due to QoS" terukur) -> drain -> EOS.
  Leg boot kebal (clock≈0). Fix 3 lapis, semua terukur: (1) `_arm_anchor`
  = BLOCK probe satu-shot di ghost src memasang `ts-offset = rt - pts +
  300ms` tepat SEBELUM buffer pertama leg lolos — offset basi tak pernah
  menjangkau frame leg baru; (2) `_qos_block` men-drop event QOS di
  ghost agar tak menjalar ke decoder; (3) sink dalam bin (alsasink/
  fakesink) dapat `ts-offset = rt` saat pad-added + `qos=false` — audio
  drain 0.38dtk sembuh. JANGAN hitung offset dari frame mid-stream
  (pola _mcount lama): drain sudah telanjur -> offset salah permanen.
  ts-offset basetransform/sink bersifat aditif ("negative for earlier
  sync" = positif menunda render). EOS jpegdec leg foto wajib di-drop di
  dalam bin — event basi ter-flush ~1dtk ke leg berikut -> advance
  prematur.
- avdec_h265 WEDGE deterministik pasca-join (show=6 beku, rxq penuh,
  tcpclientsrc futex) — jangan jadikan default. v4l2h265dec = default
  SELALU (tanpa gate seed_ok): deg-IDR filter menghapus racun abort
  satu-satunya; `nal=26x` dihitung PRE-filter sehingga seed_ok tak
  pernah terbentuk saat semua IDR tersaring.
- Streamer menunggu `UDC=configured` SEBELUM connect+replay (tanpa ini:
  churn NRestarts 100-an saat RC tercabut). `stalled` saat UDC down =
  tunggu link, bukan exit. Bail `duml+vid=0 >15s` = link belum settle.
- Disk eMMC 6GB pernah 92%: watchdog merotasi display.log (>512KB) dan
  mitm_capture.jsonl (>64MB→32MB) otomatis; dev-artifacts lama di /root
  (usb_watch.log, *.hevc, *.bin) tanggung jawab operator.
- BLUEPRINT.md = dokumen master: kontrak komponen, state machine,
  failure matrix, budget latensi.
- Display exit -> master lepas -> `drm_client_restore` mengaktifkan
  plane37 + teks console BASI di fb (vtcon unbind tidak menghapus isi
  fb). Mitigasi: `dd` zeros ke fb0 sekali di run_display.sh (restore ->
  hitam), dan `os.execv` saat EOS/ERROR dengan fd master diwariskan via
  env `DJI_DRM_FD` — master tak pernah lepas, nol flash. execv hanya
  bila show>0 agar kegagalan pra-frame tetap exit normal (fallback
  avdec run_display.sh menilai sesi gagal).
- Gate LIVE: `_count_probe` melepas splash hanya setelah `gate`=40
  frame — frame konvergensi pasca-join/resync tampil hijau ~1 dtk.
  Re-arm `gate=show+40` tiap turun ke splash. Counter terpisah: `dead`
  = frame mati saat bytes mengalir (wedge -> hard-exit), `idle` =
  upstream kelaparan (bukan wedge -> tahan splash, plafon ~15 menit).
  Hard-exit pada bytes=idle = false-positive yang membuang lap
  pemulihan penuh.
- **Socket yatim**: tcpclientsrc connect di celah streamer sekarat ->
  ESTAB lalu CLOSE_WAIT saat server mati — EOS gstreamer TIDAK selalu
  tiba; klien menunggu data di socket mati selamanya. `_sock_dead()`
  mengecek `/proc/net/tcp` st=08 remote :270E milik inode fd sendiri.
- **Churn restart streamer saat RC tercabut**: bridge tetap accept
  :9910 -> replay ke void -> drop -> ulangi (174x/boot terukur).
  Streamer menunggu `UDC==configured` tanpa replay; `rx=0 >15dtk`
  -> bail. Tiap `attach -> burst resubscribe` me-reset jendela grant —
  attach berturut-turut menahan IDR (deg-IDR burst per attach).
- Per-client queue `queue.Queue(4096)` ~5.6MB ~10dtk: 150 item lama
  (~0.4dtk) overflow saat init v4l2 -> `q-full -> resync` storm.
- `--max-gap 0.005`: recipe 9739 unit replay ~49dtk (dari ~194dtk
  @0.02); port video terbuka ~11dtk setelah connect karena unit
  subscribe berada di awal recipe.
- Mode tampilan: `auto` (selector splash/video + pad ke-3 `media`
  bila `standby=loop`), `loop` (pipeline tersendiri sync=true),
  `smpte` (bar uji proyektor). Advance playlist via flag `meos` di
  probe ekor video + poller GLib 250ms — JANGAN andalkan EOS agregat
  bin: alsasink menerima event EOS ~10x/file tapi pesan EOS bin
  tak pernah dipost (terukur, penyebab belum dipahami — mungkin
  clock GstAudioSinkClock yang tak menyelesaikan drain).
- Rantai sw untuk file playback TERLARANG: videoscale->I420->videobox
  ->NV12 @1920x1080 = ~10MB/frame, >realtime S905X -> frame ~200ms
  late -> QoS-drop semua -> `show` beku ~60 padahal stream hidup
  (terukur). Benar: decode -> kmssink langsung (sink template
  menerima I420+NV12), skala+letterbox via properti
  `render-rectangle` di plane — nol CPU.
- `render-rectangle` bertipe GstValueArray write-only — diset lewat
  `Gst.util_set_object_arg(el, "render-rectangle", "<x,y,w,h>")`;
  `set_property` + `append_value` keduanya gagal di binding ini.
- v4l2h264dec boleh dipakai file playback di mode NON-auto (leg live
  tak ada -> /dev/video0 bebas); seek tetap terlarang ("eos sent too
  early" -> state rusak) -> playlist SELALU rebuild-per-file.
- vblank 60Hz terbukti hidup via counter IRQ `meson` di
  /proc/interrupts (+120/2dtk saat render) — wedge flip BUKAN
  penyebab freeze file playback.
- Advance playlist standby = swap `Gst.Bin` leg media (ghost ->
  pad sel persisten `request_pad_simple`), BUKAN rebuild pipeline:
  rebuild per file membunuh spin-up v4l2h265dec -> gate 40 frame tak
  pernah tercapai pada file pendek (terukur show=1) + open/close
  /dev/video0 berulang = storm. Tiga jebakan terukur: (1) nama bin
  wajib unik per generasi — teardown async berarti bin lama masih
  menempel saat add baru -> "not unique, not adding" diam-diam ->
  leg mati; (2) `set_state(NULL)` bin PLAYING dari main-loop deadlock
  menunggu task internal (hang >25dtk, stack faulthandler) ->
  teardown di thread sendiri; (3) EOS event di ghost src di-DROP —
  pad sel persisten yang menelan EOS menolak buffer bin pengganti.
- ERROR tcpclientsrc saat standby (streamer/UDC menunggu RC) ditelan
  + retry via timer mengecek /proc/net/tcp lokal :2706 st=0A (LISTEN)
  — respawn-loop mengedipkan promo tiap ~2dtk; leg file tetap jalan.
- streamer: bind VidServer diretry tak terbatas (thread _accept mati
  permanen pada EADDRINUSE saat Restart=always -> "active" tanpa
  listener, :9990 hilang selamanya); setup per-klien dibungkus try —
  satu klien rontok tak boleh membunuh listener.
- `faulthandler.enable()` aktif di display.py: SIGABRT men-dump semua
  stack ke display.log — cara diagnosa hang produksi.
- Uji live tanpa RC: saji stream HEVC wpp=0 ke :9990 (meson-vdec
  menolak x265 default: "Could not read from resource" — WPP tak
  didukung); transisi standby->gate->LIVE->EOS->resume standby
  terverifikasi sintetis.
- Leak bin swap (~1-2MB/advance, terukur `GST_TRACERS=leaks`):
  siklus ref bin->decodebin->handler pad-added->closure `E`->`b`
  menahan refcount bin=1 abadi (handler sinyal sisi-C tak terlihat
  gc) -> SELURUH bin + anak bocor tiap generasi. Fix wajib:
  disconnect handler `pad-added` pada sinyal `no-more-pads` (+
  belt `dec.disconnect(pa_id)` di teardown untuk file korup).
  `gc.collect()` berkala tetap dipasang — siklus baru tertagih gc.
- `os.execv` mewarisi fd non-CLOEXEC: libv4l2 membuka /dev/video0
  TANPA cloexec (flags `02`) -> tiap reexec konteks decoder lama
  tetap memegang buffer CMA di kernel -> `Failed to allocate
  required memory` -> reexec -> +1 fd = spiral mati layar-hitam
  (terukur: 5 fd video0, 2 non-CLOEXEC). Fix: tutup SEMUA fd selain
  stdio+drm_fd sebelum execv (soket :9990 ikut tertutup = join
  segar, bukan bug).
- Decoder menempadkan buffer (avdec h264 360->384 stride; h264
  umum 1080->1088): src rect kmssink melampaui fb -> drmModeSetPlane
  ENOSPC -> "general resource error" -> spiral reexec (terukur
  file 360x640; videotestsrc berstride pas TAK memicu — uji semu).
  Fix: `videocrop` + probe stride frame pertama di sink-pad-nya
  (size=stride*rows*1.5 utk 4:2:0) — meta-only, zero frame hilang.
  aspectratiocrop GAGAL menolong — membaca caps (sudah benar),
  bukan stride buffer.
- Mode tampilan `cfg["mode"]` ∈ {auto, loop, smpte} via UI: loop =
  pipeline terpisah `build_media()` (filesrc→decodebin→kmssink,
  `sync=true`) — JANGAN gabung leg file ke selector utama: sink
  `sync=false` membuat file meledak ~4x speed (terukur). EOS bus →
  `seek(0)` = loop. Decoder DIPAKSA SW via `autoplug-sort` skip
  `v4l2*`: v4l2h264dec membuang ~70 frame undrained + state rusak
  pasca-seek ("eos sent too early" → loop beku). Leg live hanya
  dibangun di mode auto — v4l2 single-instance: decoder idle tetap
  memegang /dev/video0 → decode file gagal "allocate required memory".
  Audio loop → `alsasink hw:0,0` (paket `gstreamer1.0-alsa`; error →
  tandai `/tmp/dji_loop_no_audio`, build berikut skip). smpte =
  `videotestsrc pattern=smpte75` untuk kalibrasi proyektor.
- Upload loop file: letakkan langsung di `/root/loops/` (scp) —
  ext whitelist mp4/mkv/mov/ts/webm/hevc/h264/h265, cap ~700MB + cek
  disk. Hapus file dari daftar = mode reset auto.
- Playlist `loop_all`: urutan `sorted()` file di `/root/loops/`, EOS →
  pipeline dibangun ulang untuk file berikut (`_start_media`), `media#N`
  di log menandai transisi. Letterbox `fit_box()`: border SELALU genap
  (I420 subsampled menolak offset ganjil). videobox: TIDAK mendukung
  NV12 — letterbox di I420 lalu `videoconvert`→NV12; border videobox
  NEGATIF menambah tepi (positif = CROP — luput negasi = lebar output
  terjepit ke 1 → NOFORMAT hilir).
- Rantai dinamis pad-added (terukur): `element.link()`/`pad.link()`
  di konteks dinamis gagal `NOFORMAT` palsu (query_caps pad belum
  negotiated → EMPTY) → pakai `pad.link_full(HIERARCHY)`; elemen
  `sync_state_with_parent()` SEBELUM link pad decodebin (kebalikannya:
  sticky event gagal → qtdemux "Internal data stream error" permanen);
  pad decodebin yang tak dipakai WAJIB di-link `fakesink` — pad yatim
  memenuhi multiqueue → demux beku. Urutan rantai file: `scl` dulu baru
  `conv` — conv di depan memaksa transform I420 lintas dimensi yang
  gagal not-negotiated.
- `/tmp/dji_loop_no_audio`: alsasink gagal-buka menulis marker ini →
  pad audio jatuh ke fakesink (masih drain, tak wedge). Marker tertulis
  juga saat ALSA hanya sibuk sesaat (proses lain memegang pcmC0D0p) →
  `rm` marker + restart display untuk mengembalikan suara. Verifikasi:
  `fuser /dev/snd/pcmC0D0p`.
- Uji display manual (`python3 display.py`) WAJIB `systemctl stop
  dji-watchdog dji-display` dulu — watchdog membangkitkan display pada
  pgrep `run_display.sh` kosong; dua pemegang master = "master/atomic
  gagal" + ALSA sibuk palsu.
- Watchdog rebind xhci: dibatasi 3x beruntun + cooldown 10 mnt + CAP
  total 6 per periode-kosong — cabang cooldown me-refresh timer sehingga
  tanpa cap rebind jalan tiap ~10mnt selamanya (terukur n=14). Counter
  reset otomatis saat device ter-enumerasi → plug berikutnya diproteksi.
- Watchdog rule "UDC not attached": hanya restart bridge bila UDC
  pernah `configured` sesi ini (flag /tmp/wdg_udc_cfg) — tanpa itu ia
  churn tiap ~90dtk selama RC tercabut dan membunuh :9910 + display.
  Flag WAJIB di-clear setelah sekali fire — sebelumnya bertahan selamanya
  → restart tiap ~135dtk tanpa henti saat RC memang lepas; colok RC di
  jendela restart = handshake mati setengah jalan ("miss"). Satu bounce
  per detach; `configured` berikutnya re-arm.
- Watchdog rule CLOSE-WAIT :9910 WAJIB persistence 3 tick (~24dtk):
  transient satu-tick lahir sah dari tiap streamer exit (bail 15s →
  systemd respawn) — restart instan di sana sama saja membunuh
  handshake = churn ke-3. Zombie asli bertahan >24dtk.
- dji-rtsp-push menunggu byte-pertama di :9990 sebelum exec ffmpeg —
  VidServer accept walau no-video, ffmpeg telanjang crash-loop 4579x.
- Ghost-reap VidServer dipanggil dari UDC-wait loop juga — selama RC
  tercabut klien yatim tumbuh tak terbatas (id>258 terukur).
- `/root/sess_trace.jsonl` = wire trace sesi streamer (`--trace`, default
  on): format identik mitm_capture ({t,dir,hex}), video jadi marker
  kompak {dir:"vid",n}. Truncate per proses, self-cap 32MB — tak perlu
  rotasi. Sesi gagal → diff vs `mitm_capture.jsonl` (phone sehat).
  Log menandai `first rx @Xs` / `first video @Xs` per sesi.
- Replay recipe WAJIB lewat `reseq_record` di SEMUA pass — verbatim
  hanya sah untuk colok pertama. Dedup RC melacak seq yang pernah
  terlihat DI SESI USB, dan sesi itu bertahan lintas restart streamer
  (bridge+UDC tetap `configured`). Replay verbatim mengirim seq phone
  identik tiap restart → restart ke-2+ dalam satu colok = replay
  didedup total → sesi tak pernah terbuka → spiral stall/restart
  sampai cabut-colok (akar "miss" terukur). Seq segar sah: aktivasi RC
  memang menerima seq apa pun di awal sesi. `drop_sub` hanya di pass
  ekor — pass head membawa subscribe awal yang wajib mendarat.
- Cold-boot terverifikasi 2026-11-09: power-on → stack penuh hidup ~4dtk,
  NRestarts=0, plane37 off, splash aktif, Tailscale+SSH ~70dtk. UDC-wait
  mendeteksi bridge-restart via MSG_PEEK EOF (socket.timeout=OSError —
  except-order kritis).

## WiFi onboard (RTL8189FTV) — terverifikasi 2026-11-09

- Chip SDIO `024c:f179` di mmc0 sudah ter-enumerasi DTB — driver yang
  benar = `8189fs` (vendor Realtek 8189FS, path RTL8188F), BUKAN 8189es.
- `jwrdegoede/rtl8189ES_linux` master TIDAK bisa: master menghapus file
  `hal/*/rtl8188f` (dir phydm kosong) walau tabel ID masih mencantumkan
  F179. Snapshot yang benar = `maomomo-eth/b860av1.1t-armbian`
  `driver/rtl8189fs/` (pinned commit 75a566a sebelum penghapusan) +
  `driver/patches/linux-6.12.patch`.
- Patch tambahan di STB (tidak ada di repo itu): `set_monitor_channel`
  op cfg80211 di 6.12 menambah param `struct net_device *ndev` —
  signature diguard `KERNEL_VERSION(6,12,0)` di ioctl_cfg80211.c.
- Headers gcc12 vs kernel gcc15: `auto.conf` menghapus
  `CONFIG_CC_HAS_MIN_FUNCTION_ALIGNMENT=y` → kbuild fallback ke
  `-falign-functions=4`. Jika headers ter-reinstall, patch ini hilang.
- Build: `make ARCH=arm64 KSRC=/lib/modules/$(uname -r)/build -j4`
  → `8189fs.ko` → install ke `/lib/modules/*/kernel/drivers/net/wireless/`
  + `depmod -a` + `/etc/modules-load.d/8189fs.conf`. Terverifikasi:
  bind `/sys/bus/sdio/drivers/rtl8189fs`, `wlan0` naik, scan menemukan
  AP nyata, mode AP didukung.
- Hotspot (NetworkManager, butuh `dnsmasq-base` untuk ipv4 shared):
  koneksi `dji-admin`, SSID+PSK bebas (`nmcli con add ...` / `con mod`),
  IP STB `10.42.0.1/24` + dnsmasq DHCP/DNS internal. autoconnect=yes →
  naik tiap boot. Ganti SSID/password me-restart AP ~10 dtk (klien
  terputus). Validasi umum: ssid 1-32 char, psk 8-63 char.

## Larangan keras (pelajaran pahit)

- Unit replay DUML: SELALU `reseq()` (seq@14 + CRC16). Unit `00/99`:
  `reseq_sub()` — req-id batin harus monoton (dedup agent).
- `seq_ref`/`ctr_ref` streamer DIPERSISTKAN ke `/tmp/dji_stream_{seq,ctr}`
  — JANGAN pernah reset ke konstanta di awal sesi: dedup RC menolak seq
  berulang dalam sesi USB yang sama; restart proses ≠ sesi USB baru.
- Jangan lepas DRM master setelah plane37 dimatikan —
  `drm_client_dev_restore` langsung mengembalikannya.
- Jangan tambah komentar boilerplate; ikuti gaya komentar yang ada.
- `02/b3` TIDAK dipakai phone di Mini 3 — jangan kirim sebagai "Request IFrame".
- Diagnosa enumerasi mati: `new device is full-speed` (dwc2 ENUMDONE
  peripheral) berulang tanpa `new address` = host reset lolos tapi paket
  SETUP EP0 tak pernah sampai (DAINT=0, RXFIFO kosong) → penyebab FISIK
  (kabel/data line/port longgar), BUKAN software. Bisect penentu remote:
  gadget configfs dummy di UDC sama — gagal identik → hardware.
  (2026-11-10: reboot+module reload+rebind gagal; RC reset ~9x/dtk.)
- Watchdog reset-storm: RC baru colok me-retry enumerate ~9x/dtk yang
  memenuhi dmesg — restart prematur membunuh EP0 mid-flight = loop
  abadi. Detector menuntut 3 tick + bridge_age>=60dtk.
