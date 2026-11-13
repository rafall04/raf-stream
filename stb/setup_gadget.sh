#!/usr/bin/env bash
# Prepare the FiberHome HG680P (Amlogic S905X, Armbian ophub) as a USB gadget.
#
# Differences vs the Pi version (refs/dji-link/dji_link_beta/pi/setup_gadget.sh):
#   - dr_mode=peripheral is already in the DTB (meson-gxl-s905x-p212) — no overlay,
#     no reboot needed.
#   - The ophub kernel ships raw_gadget.ko as a module — no out-of-tree build.
#   - UDC is c9100000.usb (dwc2, 7 EPs, high-speed) = the USB-2/OTG port.
#
# Run: sudo bash setup_gadget.sh
set -e

echo "[1/4] loading raw_gadget (dwc2 is built-in and already bound)"
modprobe raw_gadget
# the SoC exposes a usb-role-switch mux that defaults to "host" on this box —
# force the device role or no gadget (raw or legacy) can ever pull up
for role_sw in /sys/devices/platform/soc/*/usb_role/*-role-switch/role; do
    [ -w "$role_sw" ] && [ "$(cat "$role_sw")" != device ] && {
        echo device > "$role_sw"
        echo "   usb-role-switch $role_sw -> device"
    }
done
# libcomposite is not needed for the raw-gadget path, but loading it does no harm
# and is required if we fall back to a configfs test gadget.
modprobe libcomposite 2>/dev/null || true

echo "[2/4] checking the UDC"
if ls /sys/class/udc/ 2>/dev/null | grep -q .; then
    UDC=$(ls /sys/class/udc/ | head -1)
    echo "   UDC found: $UDC  (driver: $(basename "$(readlink -f /sys/class/udc/$UDC/device/driver)" 2>/dev/null || echo '?'))"
    echo "   state: $(cat /sys/class/udc/$UDC/state 2>/dev/null)"
else
    echo "!! no UDC in /sys/class/udc/ — check 'dmesg | grep dwc2' and the DTB dr_mode"
    exit 1
fi

echo "[3/4] permissions on /dev/raw-gadget"
if [ -e /dev/raw-gadget ]; then
    chmod 666 /dev/raw-gadget
    echo "   /dev/raw-gadget present, mode 666"
else
    echo "!! /dev/raw-gadget missing — 'modprobe raw_gadget' failed?"
    exit 1
fi

echo "[4/4] sanity: kernel gadget config"
zgrep -E 'USB_RAW_GADGET|USB_DWC2|USB_CONFIGFS_F_FS|USB_DUMMY_HCD' /proc/config.gz 2>/dev/null || \
    grep -E 'USB_RAW_GADGET|USB_DWC2|USB_CONFIGFS_F_FS|USB_DUMMY_HCD' "/boot/config-$(uname -r)" 2>/dev/null || true

echo
echo ">>> Ready. Physical port to use: the STB's USB-2 port (the flashing/OTG port)."
echo ">>> Start the bridge with:  python3 bridge.py --udc $UDC --udc-driver $UDC"
echo ">>> (raw_gadget's driver_name is matched against gadget->name — for dwc2"
echo ">>>  that is the platform device name, i.e. the same string as the UDC)"
