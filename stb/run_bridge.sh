#!/usr/bin/env bash
# Convenience runner: setup gadget (idempotent) + start the AOA<->TCP bridge.
# Prefers the prebuilt static aarch64 binary (bin/dji-bridge); falls back to
# the Python implementation (bridge.py) which is easier to hack on.
# Usage: sudo bash run_bridge.sh [--udc c9100000.usb] [--python]
set -e
cd "$(dirname "$0")"
USE_PY=0
for a in "$@"; do [ "$a" = "--python" ] && USE_PY=1; done

bash setup_gadget.sh
echo

# raw_gadget's driver_name is compared against gadget->name inside the
# kernel's gadget_bind — NOT the sysfs driver. dwc2 names its gadget after
# the platform device ("c9100000.usb"), while dummy_hcd uses "dummy_udc".
# So the right value is the UDC name itself, except for the dummy emulator.
args=("$@")
if ! printf '%s\n' "${args[@]}" | grep -q '^--udc-driver$'; then
    UDC=$(ls /sys/class/udc/ | grep -v dummy | head -1)
    DRV=$(basename "$(readlink /sys/class/udc/$UDC/device/driver)" 2>/dev/null)
    [ "$DRV" = "dummy_udc" ] && GNAME="dummy_udc" || GNAME="$UDC"
    [ -n "$UDC" ] && args+=(--udc "$UDC" --udc-driver "$GNAME")
fi

if [ "$USE_PY" = "0" ] && [ -x bin/dji-bridge ]; then
    echo ">> starting prebuilt dji-bridge (C++, static aarch64)"
    exec ./bin/dji-bridge "${args[@]}"
fi
echo ">> starting python bridge.py"
exec python3 bridge.py $(printf '%s\n' "${args[@]}" | grep -v '^--python$')
