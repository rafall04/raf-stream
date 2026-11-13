#!/bin/sh
# HDMI-конвейер для HG680P:
#
#   HEVC (TCP 127.0.0.1:9990 от streamer VidServer)
#   -> h265parse -> v4l2h265dec (meson-vdec, hardware ~5% CPU)
#   -> input-selector <-> splash videotestsrc+text
#   -> kmssink -> meson_overlay_plane 40 -> HDMI (hw-scale ke mode aktif)
#
# Plane 37 (fbcon, zpos=1 immutable) SELALU di atas plane 40. display.py
# mematikannya via atomic commit memakai fd master yang dioper ke kmssink
# (fd property) — master tak pernah dilepas -> drm_client_restore tidak
# pernah jalan -> plane 37 tetap mati selama sesi. Saat proses exit,
# restore berjalan dan console/splash fbdev tampil di jeda.
#
# vtcon1 di-unbind: fbcon berhenti menggambar -> tidak ada commit internal
# yang bisa mengaktifkan plane 37 lagi.
echo 0 > /sys/devices/virtual/vtconsole/vtcon1/bind 2>/dev/null
# fb masih memuat teks console basi — drm_client_restore di celah
# respawn menampilkannya. Nol-kan sekali: restore menampilkan hitam.
dd if=/dev/zero of=/dev/fb0 bs=1M 2>/dev/null

ZF=0
while :; do
  # Auto-fallback decoder: v4l2 = default (latency ~0.3s, CPU bebas).
  # Deteksi abort yang BENAR: jumlah "Aborting decoding session" diukur
  # per-SESI (delta), dan hanya dihitung bila sesi v4l2 gagal total
  # (show=0). Abort saat EOS-drain — efek samping normal socket mati
  # ("N frames left undrained after CMD_STOP") — ikut menambah counter
  # dmesg tapi sesinya sudah menampilkan frame nyata -> bukan korupsi.
  # Dua sesi v4l2 gagal-total berurutan -> kunci avdec untuk boot ini.
  if [ ! -f /tmp/dji_no_v4l2 ]; then
    A0=$(dmesg 2>/dev/null | grep -c "Aborting decoding session")
  fi
  # v4l2 = default selalu (bukan lagi digate seed_ok): deg-IDR filter di
  # streamer menghapus racun abort satu-satunya, dan meson-vdec terbukti
  # graceful pada join CRA/mid-GOP (ref hilang -> drop, bukan abort).
  # avdec sekarang terbukti wedge deterministik pasca-join (show=6 beku
  # + rxq penuh) — menjadikannya default saat tanpa seed_ok = loop macet.
  DEC=avdec_h265
  [ -f /tmp/dji_no_v4l2 ] || DEC=v4l2h265dec
  DJI_DECODER=$DEC /usr/bin/python3 /root/display.py \
    >>/root/display.log 2>&1
  if [ "$DEC" = v4l2h265dec ] && [ ! -f /tmp/dji_no_v4l2 ]; then
    A1=$(dmesg 2>/dev/null | grep -c "Aborting decoding session")
    SHOW=$(tail -80 /root/display.log | awk '
      /PLAYING/ { s = 0 }
      /show=[0-9]+/ { match($0, /show=[0-9]+/)
                      s = substr($0, RSTART + 5, RLENGTH - 5) }
      END { print s + 0 }')
    if [ $((A1 - A0)) -ge 1 ] && [ "${SHOW:-0}" -eq 0 ]; then
      ZF=$((ZF + 1))
    else
      ZF=0
    fi
    if [ "$ZF" -ge 2 ]; then
      echo "[run_display] $ZF sesi v4l2 abort tanpa frame -> kunci avdec" \
        >>/root/display.log
      touch /tmp/dji_no_v4l2
    fi
  fi
  # Exit = DRM master lepas -> drm_client_restore mengaktifkan plane 37
  # (fb console basi) seketika. Matikan lagi SECEPATNYA di celah ini:
  # plane37_off acquire->off->drop->close dalam ~100ms, memangkas flash
  # console dari ~3 s jadi sub-detik; sisa jeda layar hitam, bukan console.
  /usr/bin/python3 /root/plane37_off.py >>/root/display.log 2>&1
  # /dev/video0 mengizinkan multi-open: sesi baru yang dibuka saat
  # firmware masih menurunkan sesi lama -> dec_status 0xF -> abort ->
  # respawn -> tabrakan lagi (storm). Tunggu device benar-benar bebas.
  i=0
  while fuser /dev/video0 >/dev/null 2>&1 && [ $i -lt 40 ]; do
    sleep 0.5; i=$((i+1))
  done
  sleep 2
done
