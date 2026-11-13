#!/usr/bin/env bash
# usbmon capture helper — record raw USB traffic on the gadget side.
# CONFIG_USB_MON=y is built into the ophub kernel, so this works without install.
#
# Usage:
#   bash capture_usbmon.sh            # capture ALL buses -> /tmp/usbmon.txt
#   bash capture_usbmon.sh 2          # only bus 2 (find ours: lsusb -t)
#   tcpdump-like:  cat /sys/kernel/debug/usb/usbmon/0u
#
# Stop with Ctrl-C. Convert to pcap later with 'text2pcap' or just grep the text.
set -e
BUS="${1:-0}u"

if [ ! -d /sys/kernel/debug/usb/usbmon ]; then
    echo ">> mounting debugfs + loading usbmon"
    mount -t debugfs none /sys/kernel/debug 2>/dev/null || true
    modprobe usbmon 2>/dev/null || true
fi

OUT="/tmp/usbmon_$(date +%H%M%S).txt"
echo ">> capturing bus $BUS -> $OUT   (Ctrl-C to stop)"
echo ">> our gadget will appear as a DEVICE on this bus; S/C/Ei/Eo = URB types,"
echo ">> Bo/Bi = bulk out/in data (the DUSS composite stream lives there)"
cat "/sys/kernel/debug/usb/usbmon/$BUS" | tee "$OUT"
