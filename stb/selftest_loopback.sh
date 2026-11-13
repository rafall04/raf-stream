#!/usr/bin/env bash
# Self-test WITHOUT any cable or remote: dummy_hcd creates a virtual
# host<->device pair inside the STB. The gadget binds to the dummy UDC and the
# STB's own kernel sees a virtual USB host — pyusb on the same box can then run
# host_aoa_test.py against it. This validates the entire AOA state machine and
# the dwc2-independent code paths end-to-end.
#
# Needs: modprobe dummy_hcd raw_gadget; python3 + pyusb on the STB.
# Usage:
#   sudo bash selftest_loopback.sh          (terminal A — the gadget side)
#   python3 ../pc/host_aoa_test.py --ping   (terminal B — the host side)
set -e
cd "$(dirname "$0")"

echo "[1] loading modules"
modprobe raw_gadget
modprobe dummy_hcd num=1

echo "[2] UDCs now present:"
ls -la /sys/class/udc/
UDC=$(ls /sys/class/udc/ | grep -i dummy | head -1)
if [ -z "$UDC" ]; then
    echo "!! no dummy UDC — 'modprobe dummy_hcd' failed?"
    exit 1
fi
echo "   dummy UDC = $UDC (gadget binds here; the kernel ALSO exposes a"
echo "   virtual host controller for it — appears as a new usb bus in lsusb)"

echo "[3] starting the AOA gadget on the dummy UDC"
# raw_gadget's driver_name is matched against gadget->name — "dummy_udc" for
# the emulated UDC (not the device name "dummy_udc.0")
DRIVER="dummy_udc"
echo "   UDC driver = $DRIVER"
echo "   >>> in another shell:  python3 ../pc/host_aoa_test.py --ping"
echo "   (the test sends GET_PROTOCOL/STRINGS/START exactly like the RC will)"
exec python3 bridge.py --udc "$UDC" --udc-driver "$DRIVER"
