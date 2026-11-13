#!/usr/bin/env python3
"""
Host-side AOA exerciser — plays the role of the RC-N1 (USB host) against our
STB gadget. This is the phase-2 gate test: proves the whole AOA handshake on a
plain laptop BEFORE the remote controller is involved.

Run on a machine whose USB host port is cabled to the STB's USB-2 port —
a laptop with pyusb+libusb, or the STB itself using dummy_hcd (loopback,
no cable at all:  modprobe dummy_hcd, then bind the gadget to the dummy UDC
from another shell — see stb/README.md).

    python3 host_aoa_test.py             # find 18d1:4ee1, run the full handshake
    python3 host_aoa_test.py --vid 18d1 --pid 4ee1
    python3 host_aoa_test.py --ping      # also send a wrapped DUML and listen 3s

What it does:
  1. finds the phase-1 "phone" (default 18d1:4ee1 — our gadget)
  2. GET_PROTOCOL(51) -> expect >= 1
  3. SEND_STRING x6 with the DJI identity (manufacturer=DJI, model=com.dji.logiclink)
  4. START(53) -> the STB re-enumerates as 18d1:2d00
  5. claims bulk IN/OUT and optionally round-trips a DUML frame

Needs pyusb:  pip install pyusb   (Linux: also libusb-1.0 + udev perms or sudo;
Windows: libusbK/WinUSB via Zadig on the STB device; macOS: brew install libusb)
"""
from __future__ import annotations
import argparse
import sys
import time

import aoa
from duss import DumlPacket, wrap, TYPE_DUML, CompositeDemux, TYPE_NAMES


def main():
    ap = argparse.ArgumentParser(description="exercise the STB AOA gadget as a USB host")
    ap.add_argument("--vid", default="18d1", help="phase-1 VID (hex)")
    ap.add_argument("--pid", default="4ee1", help="phase-1 PID (hex)")
    ap.add_argument("--model", default="com.dji.logiclink",
                    help="accessory model string to present")
    ap.add_argument("--ping", action="store_true",
                    help="after handshake, send a wrapped DUML ping and listen 3s")
    args = ap.parse_args()
    vid, pid = int(args.vid, 16), int(args.pid, 16)

    import usb.core
    dev = usb.core.find(idVendor=vid, idProduct=pid)
    if dev is None:
        print(f"!! {vid:04x}:{pid:04x} not found — is the STB gadget running "
              f"(bridge.py) and the cable in the OTG port?")
        print("   other devices on the bus:")
        for d in aoa.find_candidate_devices():
            print(f"   {d.idVendor:04x}:{d.idProduct:04x} "
                  f"{getattr(d, 'product', None) or ''}")
        return 1
    print(f"[1] phase-1 device found: {vid:04x}:{pid:04x}")

    proto = aoa.get_protocol(dev)
    print(f"[2] GET_PROTOCOL -> {proto} "
          f"({'AOA capable' if proto >= 1 else 'NOT AOA — wrong device?'})")
    if proto < 1:
        return 1

    ident = dict(aoa.DJI_IDENTITY)
    ident[1] = args.model
    print(f"[3] sending identity strings (model={args.model!r}) + START")
    aoa.switch_to_accessory(dev, ident)

    print("[4] waiting for re-enumeration as accessory (18d1:2d0x)...")
    try:
        adev, ep_in, ep_out = aoa.open_accessory()
    except aoa.AoaError as e:
        print(f"!! {e}")
        return 1
    print(f"    accessory up: {adev.idVendor:04x}:{adev.idProduct:04x} "
          f"ep_in=0x{ep_in.bEndpointAddress:02x} ep_out=0x{ep_out.bEndpointAddress:02x}")

    if not args.ping:
        print("[done] handshake OK — the gadget side works. "
              "Re-run with --ping to test the bulk pipe.")
        return 0

    # send a wrapped DUML GetVersion-style ping; the bridge forwards it to whatever
    # TCP client is attached (session.py) — and log anything that comes back.
    pkt = DumlPacket(sender=0x02, receiver=0x1F, cmd_set=0x00, cmd_id=0x00,
                     payload=b"\x00\x00\x01", seq=0, cmd_type=0x40)
    ep_out.write(wrap(pkt.encode(), TYPE_DUML))
    print("[5] sent bootstrap-style DUML; listening 3s on bulk IN...")
    demux = CompositeDemux(
        on_duml=lambda p: print(f"    DUML in: {p.hex()}"),
        on_unit=lambda t, p: print(f"    chan {TYPE_NAMES.get(t, hex(t))}: "
                                 f"{len(p)}B {p[:32].hex()}"))
    t_end = time.time() + 3
    while time.time() < t_end:
        try:
            data = ep_in.read(65536, timeout=500)
            if data:
                demux.feed(bytes(data))
        except Exception:
            pass
    print("[done] bulk test finished")
    return 0


if __name__ == "__main__":
    sys.exit(main())
