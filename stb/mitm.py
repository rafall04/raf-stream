#!/usr/bin/env python3
"""USB MITM: phone(DJI Fly) <--AOA--> [STB USB-1 host] <--relay--> [TCP :9910
-> gadget -> RC-N1]. The STB impersonates the RC toward the phone (same AOA
strings the real RC-N1 sent us), and the existing dji-bridge impersonates the
phone toward the RC. Everything both directions is logged to JSONL.

Run on the STB:  python3 /root/mitm.py
"""
import json
import os
import socket
import struct
import sys
import threading
import time

import usb.core
import usb.util

BRIDGE = ("127.0.0.1", 9910)
LOG_PATH = "/root/mitm_capture.jsonl"

# AOA accessory strings — byte-for-byte what the real RC-N1 sent our gadget.
AOA_STRINGS = [
    "DJI",                  # 0 manufacturer
    "com.dji.logiclink",    # 1 model — this is what DJI Fly listens for
    "DJI RC-N1",            # 2 description/accessory name
    "de",                   # 3 version
    "www.dji.com",          # 4 uri
    "6NYXLBW046026J",       # 5 serial
]

GOOGLE_VID = 0x18D1
AOA_PIDS = {0x2D00, 0x2D01, 0x2D02, 0x2D03, 0x2D04, 0x2D05}
# common phone VIDs we might see pre-AOA (informational only — we accept any
# non-hub, non-DJI device the user plugs in)
KNOWN_SKIP = {0x1D6B, 0x2CA3}   # linux root hub, DJI

log_fp = open(LOG_PATH, "a", buffering=1)
def log(direction, data: bytes):
    log_fp.write(json.dumps({"t": round(time.time(), 6), "dir": direction,
                             "hex": data.hex()}) + "\n")

# --- HDMI tap: rebroadcast raw HEVC payloads over UDP --------------------
# UDP — и только UDP: TCP-склад в ядре успевал накопить ~1 МБ «старого»
# видео и mpv показывал его с секундами опоздания. Датаграмма либо доходит
# сразу, либо пропадает — HEVC-декодер переживёт потерю до следующего
# keyframe. Потребитель: mpv --vo=drm udp://@127.0.0.1:8555
tap_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
TAP_ADDR = ("127.0.0.1", 8555)

def tap_send(payload: bytes):
    try:
        tap_sock.sendto(payload, TAP_ADDR)
    except OSError:
        pass

chan_counts = {}
def count_units(direction, data):
    """Track composite-unit channel types crossing the wire."""
    i = 0
    while i + 8 <= len(data):
        if data[i] == 0x55 and data[i + 1] == 0xCC:
            typ, ln = struct.unpack_from("<HI", data, i + 2)
            if ln <= 0x200000 and i + 8 + ln <= len(data):
                key = (direction, typ)
                chan_counts[key] = chan_counts.get(key, 0) + 1
                if typ != 0x5749 and chan_counts[key] <= 10:
                    print(f"[mitm] {direction} chan {typ:#06x} {ln}B",
                          flush=True)
                i += 8 + ln
                continue
        i += 1

def find_phone(timeout=0):
    """Wait for a non-hub USB device (the phone, pre- or post-AOA)."""
    t0 = time.time()
    while True:
        for dev in usb.core.find(find_all=True):
            if dev.idVendor in KNOWN_SKIP:
                continue
            return dev
        if timeout and time.time() - t0 > timeout:
            return None
        print("[mitm] waiting for phone on USB-1 ...", flush=True)
        time.sleep(2.0)

def find_aoa(timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        for dev in usb.core.find(find_all=True):
            if dev.idVendor == GOOGLE_VID and dev.idProduct in AOA_PIDS:
                return dev
        time.sleep(0.5)
    return None

def aoa_handshake(dev):
    """Act as the accessory host: tell the phone we're the RC-N1."""
    proto = dev.ctrl_transfer(0xC0, 51, 0, 0, 2, timeout=1000)
    print(f"[mitm] AOA protocol version: {proto.tolist()}", flush=True)
    for i, s in enumerate(AOA_STRINGS):
        dev.ctrl_transfer(0x40, 52, 0, i, s.encode(), timeout=1000)
        print(f"[mitm] string[{i}] = {s!r}", flush=True)
    dev.ctrl_transfer(0x40, 53, 0, 0, None, timeout=1000)
    print("[mitm] AOA START sent — phone should re-enumerate", flush=True)

def claim_accessory(dev):
    """Return (ep_in, ep_out) on the accessory bulk interface."""
    # The kernel already ran SET_CONFIGURATION during enumeration; re-setting
    # it resets the Android accessory gadget mid-registration, so only issue
    # the request when the device is not configured yet.
    try:
        cfg = dev.get_active_configuration()
    except usb.core.USBError:
        dev.set_configuration()
        cfg = dev.get_active_configuration()
    # With ADB enabled the phone enumerates as 18d1:2d01 with two vendor
    # interfaces; the AOA one has bInterfaceSubClass 0xff (ADB uses 0x42).
    intf = None
    for i in cfg:
        if i.bInterfaceClass == 0xFF and i.bInterfaceSubClass == 0xFF:
            intf = i
            break
    if intf is None:
        intf = cfg[(0, 0)]
    ep_in = usb.util.find_descriptor(
        intf, custom_match=lambda e:
        usb.util.endpoint_direction(e.bEndpointAddress) ==
        usb.util.ENDPOINT_IN)
    ep_out = usb.util.find_descriptor(
        intf, custom_match=lambda e:
        usb.util.endpoint_direction(e.bEndpointAddress) ==
        usb.util.ENDPOINT_OUT)
    try:
        if dev.is_kernel_driver_active(intf.bInterfaceNumber):
            dev.detach_kernel_driver(intf.bInterfaceNumber)
    except (usb.core.USBError, NotImplementedError):
        pass
    usb.util.claim_interface(dev, intf)
    return ep_in, ep_out, intf.bInterfaceNumber

def phone_to_rc(dev, ep_in, sock, dead, app_open):
    while not dead.is_set():
        try:
            data = dev.read(ep_in, 65536, timeout=500)
        except usb.core.USBTimeoutError:
            continue
        except usb.core.USBError as e:
            print(f"[mitm] phone read error: {e}", flush=True)
            dead.set()
            return
        if not data:
            continue
        if not app_open.is_set():
            app_open.set()
            print("[mitm] *** phone spoke — app is open, enabling writes ***",
                  flush=True)
        b = bytes(data)
        log("phone>rc", b)
        count_units("phone>rc", b)
        try:
            sock.sendall(b)
        except OSError:
            dead.set()
            return

def rc_to_phone(dev, ep_out, sock, dead, app_open):
    """Forward bridge->phone, re-framed to whole composite units.

    The RC sends one composite unit per bulk transfer; the phone's parser
    may rely on those transfer boundaries. TCP recv() chunks arbitrarily,
    so re-assemble units and write each in a single dev.write() call.
    A bounded queue absorbs stalls while no app drains the endpoint.
    """
    sock.settimeout(0.5)
    pending = bytearray()          # unparsed bytes from the bridge
    ctlq = []                      # control units — никогда не роняем
    vidq = []                      # video units — мелкая очередь, ~0.4 с
    VID_CAP = 150                  # ~150 юнитов: старше — мёртвый груз задержки
    dropped = 0
    while not dead.is_set():
        try:
            data = sock.recv(65536)
        except socket.timeout:
            data = None
        except OSError:
            dead.set()
            return
        if data == b"":                # orderly EOF — bridge went away
            dead.set()
            return
        if data:
            pending += data
        # cut complete composite units out of the byte stream
        while True:
            i = pending.find(b"\x55\xcc")
            if i < 0:
                pending.clear()
                break
            if i:
                del pending[:i]
            if len(pending) < 8:
                break
            ln = struct.unpack_from("<I", pending, 4)[0]
            if ln > 0x200000:          # implausible length -> resync
                del pending[:2]
                continue
            if len(pending) < 8 + ln:
                break                  # unit still arriving
            unit = bytes(pending[:8 + ln])
            del pending[:8 + ln]
            if len(unit) >= 4 and (unit[2] | unit[3] << 8) == 0x574A:
                tap_send(unit[8:])
                vidq.append(unit)
                if len(vidq) > VID_CAP:
                    dropped += 1       # телефону — только самое свежее видео
                    vidq.pop(0)
            else:
                # Управляющая рамка — потеря одной клинит session-init
                # приложения (застревает на логотипе дрона). Никаких дропов.
                log("rc>phone", unit)
                ctlq.append(unit)
        if dropped and dropped % 500 < 20:
            print(f"[mitm] vid backlog full ({dropped} video dropped)",
                  flush=True)
        # Пакуем до 16 КБ юнитов в ОДИН bulk-write: синхронный dev.write
        # стоит ~3-6 мс на вызов — по одному юниту (150/с) телефон не
        # поспевает за RC и очередь растёт вечно. Композитные юниты
        # самоограничены (55cc+port+len), парсеру границы трансфера не нужны.
        if ctlq or vidq:
            chunk = bytearray()
            ctl_used = []
            while ctlq and len(chunk) < 16384:
                u = ctlq.pop(0)
                chunk += u
                ctl_used.append(u)
                count_units("rc>phone", u)
            while vidq and len(chunk) < 16384:
                u = vidq.pop(0)
                chunk += u
                count_units("rc>phone", u)
            try:
                dev.write(ep_out, bytes(chunk), timeout=2000)
            except usb.core.USBTimeoutError:
                # Управляющие рамки возвращаем в голову — приложение их ждёт.
                # Видео-часть чанка теряется, следующий кадр важнее.
                ctlq = ctl_used + ctlq
            except usb.core.USBError as e:
                print(f"[mitm] phone write error: {e}", flush=True)
                dead.set()
                return

def stats(dead):
    while not dead.wait(15):
        try:
            os.utime("/tmp/dji_mitm_active", None)   # keepalive owner-flag
        except OSError:
            pass
        tot = {}
        for (d, t), n in sorted(chan_counts.items()):
            tot[d] = tot.get(d, 0) + n
        print(f"[mitm] units so far {tot} | non-5749: "
              f"{ {k: v for k, v in chan_counts.items() if k[1] != 0x5749} }",
              flush=True)

def session_once():
    dev = find_phone()
    print(f"[mitm] device {dev.idVendor:04x}:{dev.idProduct:04x} "
          f"bus {dev.bus} addr {dev.address}", flush=True)
    if not (dev.idVendor == GOOGLE_VID and dev.idProduct in AOA_PIDS):
        try:
            aoa_handshake(dev)
        except usb.core.USBError as e:
            print(f"[mitm] handshake failed: {e}", flush=True)
            return
        dev = find_aoa()
        if dev is None:
            print("[mitm] no accessory device after START", flush=True)
            return
        print(f"[mitm] accessory enumerated "
              f"{dev.idVendor:04x}:{dev.idProduct:04x} addr {dev.address}",
              flush=True)
    else:
        print("[mitm] phone already in accessory mode", flush=True)
    try:
        ep_in, ep_out, intf_num = claim_accessory(dev)
    except usb.core.USBError as e:
        print(f"[mitm] claim failed: {e}", flush=True)
        usb.util.dispose_resources(dev)
        time.sleep(5.0)   # don't spin — a busy claim needs the holder to exit
        return
    print(f"[mitm] claimed if0: ep_in={ep_in.bEndpointAddress:#04x} "
          f"ep_out={ep_out.bEndpointAddress:#04x} "
          f"mps={ep_in.wMaxPacketSize}", flush=True)

    sock = None
    t0 = time.time()
    while time.time() - t0 < 20:
        try:
            sock = socket.create_connection(BRIDGE, timeout=2)
            break
        except OSError:
            time.sleep(0.5)
    if sock is None:
        print("[mitm] bridge connect failed (20s)", flush=True)
        return
    try:
        sock.sendall(b"M")       # klaim owner TX — streamer jadi tap-only
    except OSError:
        print("[mitm] owner claim failed", flush=True)
        return
    # Flag untuk streamer: TX bridge milik mitm — pause replay/poll.
    try:
        open("/tmp/dji_mitm_active", "w").write(str(int(time.time())))
    except OSError:
        pass
    print("[mitm] relay up — RC <-> phone (TX owner). "
          "Open DJI Fly on the phone now.", flush=True)
    dead = threading.Event()
    app_open = threading.Event()
    threading.Thread(target=phone_to_rc, args=(dev, ep_in, sock, dead, app_open),
                     daemon=True).start()
    threading.Thread(target=rc_to_phone, args=(dev, ep_out, sock, dead, app_open),
                     daemon=True).start()
    threading.Thread(target=stats, args=(dead,), daemon=True).start()
    while not dead.is_set():
        time.sleep(0.5)
    sock.close()
    try:
        os.remove("/tmp/dji_mitm_active")
    except OSError:
        pass
    # Release the interface claim and the libusb handle — a lingering claim
    # makes every subsequent session fail claim_interface with EBUSY, which
    # then spins the outer loop and retriggers AOA handshakes on the phone.
    try:
        usb.util.release_interface(dev, intf_num)
    except usb.core.USBError:
        pass
    usb.util.dispose_resources(dev)
    print("[mitm] session ended — waiting for re-plug", flush=True)

def main():
    print("[mitm] DJI RC-N1 <-> DJI Fly MITM, logging to", LOG_PATH, flush=True)
    print("[mitm] HDMI tap: UDP 127.0.0.1:8555", flush=True)
    try:
        while True:
            session_once()
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    print("[mitm] stopped", flush=True)
    return 0

if __name__ == "__main__":
    sys.exit(main())
