# REGRESSIONS — ledger bug yang pernah difix

Aturan pakai (baca di AGENTS.md checklist):
- **Bug kambuh?** → `sh /root/smoke_test.sh` → merah = invariant rusak,
  lihat commit di tabel ini → `git show <hash>` atau `git log --grep <kata-kunci>` (hash bisa berubah bila histori di-rewrite).
- **Bug baru difix?** → tambah baris: gejala | akar | commit | cek smoke.
  Fix tanpa invariant = bug yang akan kambuh lagi.
- **Trace kapan fix hilang**: `git log -S'pola-kode' --oneline -- stb/<file>`
  (pickaxe: muncul/hilangnya pola, bukan sekadar diff).

| Gejala | Akar terukur | Fix | Cek di smoke_test |
|---|---|---|---|
| Replay tak membuka sesi pada restart ke-2+ (miss berulang) | RC dedup seq per sesi USB; pass verbatim kirim seq identik | `78a49a6` reseq semua pass | `L3 reseq di semua pass` (grep kode) |
| Unit sesekali di-drop RC | seq_ref race 2 thread → duplikat | `64b8dba` alokasi di send_lock | code review / trace diff |
| Seq reset 0xA000 tiap restart → dedup | counter tak persist | `e4ab55c` `/tmp/dji_stream_seq` | `L3 seq/ctr persist` |
| Bridge restart tiap ~135dtk saat RC lepas | `wdg_udc_cfg` tak pernah clear | `863df01` | `L2 bridge restart ≤4/10m` |
| Rebind xhci tiap ~10mnt selamanya | cooldown me-refresh timer | `863df01` cap 6 | `L2` + log watchdog |
| CLOSE-WAIT transient → restart bridge | rule-2 tanpa persistence | `7d12403` 3 tick | `L4 CLOSE-WAIT ≤3` |
| Foto tampil ~3.5dtk saat diset 5 | timer dari build bin, preroll 1.7dtk | `bffebda` timer dari frame pertama | `L5 leg foto ≈ photo_secs` |
| Foto ~4dtk saat diset 5 | `timeout_add_seconds` coalescing | `ae8c723` timer ms | sama |
| Video file terpotong 1.5-4dtk | running_time leg baru ~0 vs clock → kmssink drop + QoS | `_arm_anchor`+`_qos_block`+offset sink | leg video di display.log |
| POST /restart-* bunuh sesi live | guard hanya di JS | `3d80fd2` `_live_now` 409 | `API live-guard ada` |
| UI/streamer/bridge tak punya self-check | tak ada recorder sesi | `e958a9b` `sess_trace.jsonl` | `L3 sess_trace ada` |
| ui.py/mediamtx mati diam-diam | tak dipantau watchdog | `e8bb4b8` pgrep-check | `svc dji-ui/mediamtx` |
| Klien :9990 unsynced selamanya | tak ada paksa-IDR tanpa restart | `c8585c5` `/api/resub` pulse | tombol "paksa IDR" di UI |

| `int(Content-Length)` crash thread UI + body tak berbatas | unguarded ValueError + slowloris | `abfe39b` guard 413/400 | `bash check.sh` AST |
| mitm claim gagal pada sesi ADB | `release_interface(dev,0)` bukan intf | `abfe39b` intf_num | smoke mitm (live) |
| Deploy tanpa verifikasi | tak ada gerbang pasca-deploy | `deploy.sh` auto-smoke | deploy itu sendiri |
| Commit membawa file rusak | tak ada gerbang syntax lokal | `check.sh` AST+sh -n | jalan sebelum commit |

## Invariant yang belum punya cek (daftar utang audit)

- `ep_stop_xfr timeout` di dmesg saat disconnect (dwc2) — belum dipetakan
  ke cek; kandidat: `dmesg | tail` di smoke lambat-path.
- seq monoton di `sess_trace` wire-level (sekarang cek grep kode;
  parser komposit penuh belum ditulis).
| Log display "macet" padahal pipeline sehat — semua log hilang | watchdog `mv` file baru ke atas log → inode lama ter-unlink → fd `append:` proses menunjuk inode mati → output tertulis ke file yatim | `stb/watchdog.sh` copy-truncate (`cat > file`, pola sama seperti mitm_capture) | `cat /proc/PID/fd/1` → `(deleted)`; inode baru menerima tulisan |
| Log display "macet" LAGI walau watchdog copy-truncate ter-deploy | Proses watchdog start 10-Nov — SEBELUM fix 05:12; skrip shell dieksekusi dari memori exec-time, bukan file — rotasi `mv` lama tetap jalan → inode putus → fd yatim. Fix file ≠ fix proses terinterpretasi | restart `dji-watchdog` + `dji-display-rs` (reattach fd); smoke kini menguji `readlink /proc/PID/fd/1` → `(deleted)` = FAIL | `L5b fd log utuh` |
| `/var/log/dji-watchdog.log` selalu 0 byte — diagnosa restart buta | cron `armbian-truncate-logs` (*/15) mengosongkan semua /var/log | `LOG=/root/dji-watchdog.log` — /root tak disentuh cron | `tail /root/dji-watchdog.log` isi nyata |
| Main-loop dji-display wedged, proses "aktif" — pgrep tak menangkap | proses hidup tapi GLib loop beku = kelas insiden stuck-foto | fast_tick menulis `/tmp/dji_display_hb` tiap detik; watchdog me-restart bila file basi >20dtk & proses >30dtk; smoke cek kesegaran file | `L5b heartbeat rs segar` |
