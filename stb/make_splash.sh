#!/bin/sh
# Buat /root/splash.bgrx (BGRx 1920x1080) — layar "connecting" display.py.
#
#   ./make_splash.sh            -> splash teks gelap bawaan
#   ./make_splash.sh logo.png   -> dari PNG (didukung pngdec)
#
# fb0 = XR24: byte order B,G,R,X -> format gst "BGRx".
# NB: gst-launch di build ini mewajibkan caps dibingkai '! caps !'.
if [ -n "$1" ]; then
  gst-launch-1.0 -q filesrc location="$1" ! pngdec ! videoconvert ! \
    videoscale ! video/x-raw,format=BGRx,width=1920,height=1080 ! \
    filesink location=/root/splash.bgrx
else
  gst-launch-1.0 -q videotestsrc num-buffers=1 pattern=black ! \
    video/x-raw,width=1920,height=1080 ! \
    textoverlay text="MENYAMBUNG VIDEO..." halignment=center \
      valignment=center font-desc="Sans Bold 56" color=0xffe8e8e8 \
      shaded-background=yes ! \
    textoverlay text="DJI Mini 3 - STB Display" halignment=center \
      valignment=bottom ypad=120 font-desc="Sans 36" color=0xff8090a0 ! \
    videoconvert ! video/x-raw,format=BGRx ! \
    filesink location=/root/splash.bgrx
fi
echo "splash -> /root/splash.bgrx ($(stat -c%s /root/splash.bgrx) bytes)"
