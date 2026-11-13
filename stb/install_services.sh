#!/usr/bin/env bash
# Install the full DJI STB stack on the HG680P, idempotently.
#
#   dji-gadget     raw_gadget/UDC setup (oneshot)
#   dji-bridge     AOA<->TCP bridge on :9910
#   dji-streamer   recipe replay + keepalive -> HEVC on tcp:9990
#   dji-display    gst tcpclientsrc -> v4l2h265dec -> kmssink + fbdev splash
#   dji-watchdog   USB/EP0/socket/UDC/CMA/thermal auto-recovery
#   dji-mediamtx   RTSP server (:8554)
#   dji-rtsp-push  ffmpeg :9990 -> rtsp://127.0.0.1:8554/dji
#   dji-mitm       phone DJI Fly via USB-A host <-> bridge :9910 (opsional)
#   dji-governor   CPU governor=performance (oneshot)
#
# App files are copied to /root (paths baked into units/*). The bridge unit is
# generated with THIS directory, so the repo can live anywhere.
#
# Usage (on the STB, from the deployed stb/ dir):
#   sudo bash install_services.sh            # binary bridge if bin/dji-bridge exists
#   sudo bash install_services.sh --python   # force python bridge.py
set -euo pipefail
cd "$(dirname "$0")"
HERE="$(pwd)"
USE_PY=0
[ "${1:-}" = "--python" ] && USE_PY=1
[ -f "${HERE}/bin/dji-bridge" ] || USE_PY=1

echo "[1/7] paket sistem (fresh STB: tanpa ini display.py gagal import gi)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq || echo "    apt update gagal (offline?) — lanjut bila paket sudah ada"
apt-get install -y -qq \
    python3 python3-gi python3-usb \
    gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 \
    gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad gstreamer1.0-libav gstreamer1.0-alsa \
    ffmpeg gdb curl ca-certificates \
    || echo "    sebagian paket gagal — verifikasi manual"
for b in python3 gst-launch-1.0 ffprobe ffmpeg; do
    command -v "$b" >/dev/null || { echo "GAGAL: $b tidak tersedia"; exit 1; }
done
python3 -c "import gi; gi.require_version('Gst','1.0'); from gi.repository import Gst" \
    || { echo "GAGAL: python3-gi/gir gstreamer tidak ter-load"; exit 1; }

echo "[2/7] module autoload (raw_gadget at boot; dwc2 is builtin)"
cat > /etc/modules-load.d/dji-stb.conf <<'EOF'
# USB gadget stack for the DJI AOA bridge (HG680P / S905X)
raw_gadget
libcomposite
EOF

echo "[3/7] udev rule: /dev/raw-gadget world-accessible + gadget settle"
cat > /etc/udev/rules.d/99-dji-rawgadget.rules <<'EOF'
ACTION=="add", SUBSYSTEM=="misc", KERNEL=="raw-gadget", MODE="0666"
EOF
udevadm control --reload 2>/dev/null || true

echo "[4/7] app files -> /root"
install -m 0755 run_display.sh watchdog.sh plane40_off.py plane37_off.py \
    make_splash.sh smoke_test.sh /root/
install -m 0644 streamer.py display.py mitm.py mediamtx.yml \
    renewal.jsonl video_recipe_trim.jsonl /root/
# Binary mediamtx TIDAK di-versioning (63MB). Prioritas: copy lokal
# bin/mediamtx -> yang terpasang -> unduh release resmi ARM64.
if [ -f bin/mediamtx ]; then
    install -m 0755 bin/mediamtx /usr/local/bin/mediamtx
elif [ ! -x /usr/local/bin/mediamtx ]; then
    MTX_VER=v1.21.2
    MTX_URL="https://github.com/bluenviron/mediamtx/releases/download/${MTX_VER}/mediamtx_${MTX_VER}_linux_arm64.tar.gz"
    echo "    mengunduh mediamtx ${MTX_VER} dari release resmi..."
    curl -fsSL "$MTX_URL" -o /tmp/mtx.tgz && \
        tar -xzf /tmp/mtx.tgz -C /tmp mediamtx && \
        install -m 0755 /tmp/mediamtx /usr/local/bin/mediamtx && \
        rm -f /tmp/mtx.tgz /tmp/mediamtx
fi
mkdir -p /root/loops /var/log/dji-link
[ -f /root/video_recipe_trim.jsonl ] || \
    echo "    !! /root/video_recipe_trim.jsonl missing — streamer will idle"
[ -x /usr/local/bin/mediamtx ] || \
    echo "    !! mediamtx binary missing — RTSP path will not start"

echo "[5/7] gadget setup oneshot + dji-bridge (:9910)"
cat > /etc/systemd/system/dji-gadget.service <<EOF
[Unit]
Description=HG680P USB gadget setup (raw_gadget, UDC check)
DefaultDependencies=no
After=systemd-modules-load.service
Before=dji-bridge.service

[Service]
Type=oneshot
ExecStart=/bin/bash ${HERE}/setup_gadget.sh
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF

if [ "$USE_PY" = "1" ]; then
    EXEC="/usr/bin/python3 ${HERE}/bridge.py"
else
    chmod +x "${HERE}/bin/dji-bridge"
    EXEC="${HERE}/bin/dji-bridge"
fi
cat > /etc/systemd/system/dji-bridge.service <<EOF
[Unit]
Description=DJI AOA<->TCP bridge (HG680P gadget side, :9910)
After=dji-gadget.service
Requires=dji-gadget.service
StartLimitIntervalSec=0

[Service]
Nice=-15
ExecStart=${EXEC}
WorkingDirectory=${HERE}
Restart=always
RestartSec=2
User=root
Environment=AOA_DEBUG=1

[Install]
WantedBy=multi-user.target
EOF

echo "[6/7] remaining units from units/"
for u in dji-streamer dji-display dji-watchdog dji-mediamtx dji-rtsp-push \
         dji-mitm dji-governor; do
    install -m 0644 "units/${u}.service" /etc/systemd/system/
done

echo "[7/7] splash + enable"
[ -f /root/splash.bgrx ] || /root/make_splash.sh || \
    echo "    !! splash generation failed — display falls back to black"

systemctl daemon-reload
for u in dji-gadget dji-bridge dji-streamer dji-display dji-watchdog \
         dji-mediamtx dji-rtsp-push dji-mitm dji-governor; do
    systemctl enable --now "${u}.service"
done
# enable --now hanya MENYALAKAN unit mati — proses yang sudah jalan
# memegang kode exec-time: skrip shell/python terbaca sekali saat
# start, binary pun berganti hanya via restart (terukur 2026-11-12:
# watchdog 2 hari menjalankan rotasi `mv` basi walau file sudah
# copy-truncate → fd display.log yatim → log hilang). try-restart
# memaksa kode segar pada unit aktif; unit mati tak dibangunkan.
# dji-gadget dilewati — oneshot RemainAfterExit, tak ada proses basi.
for u in dji-bridge dji-streamer dji-display dji-watchdog \
         dji-rtsp-push dji-mitm; do
    systemctl try-restart "${u}.service" 2>/dev/null || true
done
# dji-plane-off is retired: it disabled primary plane 37 which the fbdev
# splash needs. Remove leftovers from older installs.
systemctl disable --now dji-plane-off.service 2>/dev/null || true
rm -f /etc/systemd/system/dji-plane-off.service

echo
systemctl --no-pager is-active dji-bridge dji-streamer dji-display \
    dji-watchdog dji-mediamtx dji-rtsp-push || true
echo
echo ">>> done. Ports: bridge :9910 | video :9990 | RTSP :8554/dji"
echo ">>> watch: rtsp://<stb-ip>:8554/dji in VLC/ffplay"
