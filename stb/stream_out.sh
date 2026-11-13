#!/usr/bin/env bash
# stream_out.sh — pipe the liveview ES to an output: HDMI | RTSP (mediamtx) | UDP.
# Runs session.py locally on the STB (pc/ is deployed alongside stb/):
#   session.py 127.0.0.1 --liveview --dump -  ->  ffmpeg  ->  sink
#
# Usage:
#   ./stream_out.sh hdmi                  # decode via meson-vdec -> /dev/fb0
#   ./stream_out.sh rtsp                  # publish rtsp://<stb>:8554/dji (HEVC, VLC/ffplay)
#   ./stream_out.sh udp <pc-ip> [port]    # MPEG-TS unicast -> ffplay udp://@:9000
#   ./stream_out.sh mediamtx              # just run the bundled RTSP/WebRTC server
#
# Env: STB_HOST (default 127.0.0.1), SRC_DIR (default $HOME/dji-stb),
#      EXTRA_SESSION (extra session.py flags, e.g. "--camera-dev 0x18").

set -euo pipefail
STB_HOST="${STB_HOST:-127.0.0.1}"
SRC_DIR="${SRC_DIR:-$HOME/dji-stb}"
MODE="${1:-}"
DEST="${2:-}"
PORT="${3:-9000}"

session_pipe() {
    # liveview ES on stdout; keepalive/bootstrap run inside session.py
    exec python3 "$SRC_DIR/pc/session.py" "$STB_HOST" --liveview --wait-aircraft 30 \
         --dump - $EXTRA_SESSION
}

case "$MODE" in
    hdmi)
        # HW decode HEVC via Amlogic VPU, write to framebuffer.
        # fbdev works on Armbian console; for DRM/KMS use gstreamer kmssink instead.
        session_pipe | ffmpeg -hide_banner -loglevel warning \
            -c:v hevc_v4l2m2m -f hevc -i - \
            -pix_fmt bgra -f fbdev /dev/fb0
        ;;
    rtsp)
        # mediamtx accepts publisher push; path /dji auto-created.
        "$SRC_DIR/stb/bin/mediamtx" "$SRC_DIR/stb/mediamtx.yml" &
        MTX=$!
        trap 'kill $MTX 2>/dev/null' EXIT
        sleep 1
        session_pipe | ffmpeg -hide_banner -loglevel warning \
            -f hevc -i - -c:v copy -f rtsp rtsp://127.0.0.1:8554/dji
        ;;
    udp)
        [ -n "$DEST" ] || { echo "usage: $0 udp <dest-ip> [port]"; exit 1; }
        # MPEG-TS over UDP: lowest-latency, no server. Watch: ffplay udp://@:9000
        session_pipe | ffmpeg -hide_banner -loglevel warning \
            -f hevc -i - -c:v copy -f mpegts "udp://$DEST:$PORT?pkt_size=1316"
        ;;
    mediamtx)
        exec "$SRC_DIR/stb/bin/mediamtx" "$SRC_DIR/stb/mediamtx.yml"
        ;;
    *)
        sed -n '2,14p' "$0"; exit 1;;
esac
