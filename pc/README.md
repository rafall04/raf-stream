# pc/ — tools sisi laptop/PC (TCP ke bridge di STB)

Jalan di mana saja yang bisa reach `stb:9910` — laptop dev, atau di STB
sendiri (`session.py 127.0.0.1`).

- `duss.py` — codec self-contained: DUSS composite mux (`55 CC | type | len |`),
  DUML frame + CRC8(0x77)/CRC16(0x3692), parser header liveview `0x6d`.
- `session.py` — sesi lengkap ke RC-N1: bootstrap (FreeFCC), keepalive 2.5 dtk,
  resep liveview WM160, demux + log semua channel, dump ES ke file.
- `aoa.py` + `duml.py` — library host-side AOA + DUML (verbatim DJI-Link,
  butuh pyusb).
- `host_aoa_test.py` — exerciser: mesin ini jadi USB host ala RC-N1 terhadap
  gadget STB (fase-2, sebelum colok RC asli; juga bisa jalan di STB sendiri
  lewat `dummy_hcd` loopback — lihat `stb/selftest_loopback.sh`).

## Contoh alur

```bash
# fase 4 — sniff: apa yang RC-N1 kirim begitu pipe terbuka?
python3 session.py <ip-stb>

# fase 5 — coba resep liveview WM160 + rekam elementary stream
python3 session.py <ip-stb> --liveview --wait-aircraft 20 --dump out.hevc

# real-time: ES ke stdout → pipa ke ffplay (log tetap di stderr)
python3 session.py <ip-stb> --liveview --dump - | ffplay -f hevc -i -

# mainkan hasilnya (butuh VPS/SPS/PPS — request I-frame sudah ada di resep)
ffplay -f hevc out.hevc
# atau di STB pakai HW decoder VPU:
ffmpeg -c:v hevc_v4l2m2m -f hevc -i out.hevc -f null -
```

Yang diamati di log: `chan 0x5749` = DUML (decode otomatis), `0x574A/0x574D` =
video (auto-reassembly by frm_idx), channel lain di-print hex mentah — itulah
cara mengetahui channel mana yang Mini 3 pakai kalau beda dari WM160.

`--bare` mengirim DUML tanpa wrap `55 CC`, `--sender 0x82` memakai alamat app
idx4, `--camera-dev 0x18` receiver camera era RC-N1 — ketiganya adalah knob
sweep kalau liveview tidak merespons (FreeFCC membuktikan kombinasi
sender×framing×alamat berbeda antar firmware).

## Flag lengkap session.py

| Flag | Fungsi |
|---|---|
| `--no-bootstrap` / `--no-keepalive` | matikan handshake sesi RC-N1 |
| `--bare` | DUML mentah tanpa wrap `55 CC` (diagnostik; RC-N1 normalnya menolak) |
| `--sender 0x82` | alamat app idx4 (nilai capture asli FreeFCC) |
| `--camera-dev` / `--dm368-dev` | override receiver resep liveview (era RC-N1: camera=`0x18`) |
| `--camera-source` / `--fps` | parameter resep liveview |
| `--wait-aircraft SEC` | tahan `--liveview` sampai serial drone muncul (waitForAircraft) |
| `--liveview` | kirim resep start video WM160 |
| `--dump FILE` / `--dump -` | rekam ES ke file / stdout (pipa ke ffplay) |
| `--probe` | sweep `get_version` ke 14 alamat kandidat → peta komponen Mini 3 |
| `--cmd 'set:id:hex[:dst]'` | kirim DUML arbitrer, repeatable (`'8:78::'` = start_live_streaming) |
| `--dst` | receiver default untuk `--cmd` |
| `--cmdmap [FILE]` | decode nama 436 perintah `uav_*` (default `pc/cmds.json`) |
| `--param-info/read/write NAME[:HEX]` | flyc param by-hash `0xF7/0xF8/0xF9` — transport KeyValue asli |
| `--encrypt` | SIMPLE-encrypt semua TX (`0x43`); RX terenkripsi auto-decrypt |
| `--record FILE` | rekam raw wire dua arah ke JSONL `{t,dir,hex}` — capture replayable |
| `--media-probe` | masuk MEDIA_DOWNLOAD (`0x02/0x10 [3]`) + probe litchis LIST |
| `--media-mode N` | byte work-mode untuk probe (2=PLAYBACK, 3=MEDIA_DOWNLOAD) |
| `--litchis-list START COUNT` | kirim litchis LIST serializer-exact (FileSendPack) |

## Diagnostik yang sudah siap

- Push OSD klasik `0x03/0x43`/`0x09/0x01` → auto-decode lat/lon/alt/sats/mode (1×/dtk)
- Frame `0x43`/`0xC3` terenkripsi → auto-decrypt sebelum decode
- Serial drone `1581*` → banner `AIRCRAFT LINKED` (gate untuk `--wait-aircraft`)
- Setiap channel tak dikenal → hex dump mentah (sinar untuk channel baru Mini 3)
