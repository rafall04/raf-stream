#!/usr/bin/env python3
"""
Bridge on the Pi Zero: AOA channel to the remote controller  <->  TCP to the laptop.

    [Laptop: keyboard/Drone API] --TCP--> [bridge.py on the Pi] --USB(AOA)--> [Remote controller] ))) [Drone]

The laptop sends ready-made DUML frames over TCP; we hand them to the remote controller via bulk IN;
everything the remote controller sends (bulk OUT) we push back into TCP. We do not parse the bytes — a transparent
transport (DUML parsing lives on the laptop, in drone.py).

Run on the Pi (after setup_gadget.sh):
    sudo python3 bridge.py --udc 20980000.usb
Default port is 9910.
"""

from __future__ import annotations
import argparse
import faulthandler
import logging
import os
import queue
import socket
import sys
import threading
import time
import traceback

from aoa_device import AoaDevice

UDC_SYSFS = "/sys/class/udc"
LOG_DIR = "/var/log/dji-link"
LOG_FILE = os.path.join(LOG_DIR, "bridge.log")

_log_fp = None


class Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for s in self.streams:
            try:
                s.write(data)
                s.flush()
            except Exception:
                pass

    def flush(self):
        for s in self.streams:
            try:
                s.flush()
            except Exception:
                pass

    def fileno(self):
        for s in self.streams:
            try:
                return s.fileno()
            except Exception:
                pass
        raise OSError("no fileno")


def setup_logging(path: str = LOG_FILE):
    """Send every stdout/stderr line to systemd and to a persistent file."""
    global _log_fp
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _log_fp = open(path, "a", buffering=1)
    except OSError:
        _log_fp = open("bridge.log", "a", buffering=1)
        path = os.path.abspath("bridge.log")

    sys.stdout = Tee(sys.__stdout__, _log_fp)
    sys.stderr = Tee(sys.__stderr__, _log_fp)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(threadName)s %(message)s",
        stream=sys.stderr,
    )
    try:
        faulthandler.enable(file=_log_fp)
    except Exception:
        pass
    logging.info("bridge starting pid=%s log=%s", os.getpid(), path)


def install_crash_handlers():
    def excepthook(tp, value, tb):
        logging.critical("uncaught exception", exc_info=(tp, value, tb))

    sys.excepthook = excepthook

    if hasattr(threading, "excepthook"):
        def thread_hook(args):
            logging.critical(
                "uncaught thread exception in %s",
                args.thread.name if args.thread else "<unknown>",
                exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
            )

        threading.excepthook = thread_hook


def detect_udc() -> str:
    """Return the single available UDC name.

    The name is board-specific (Pi Zero 1: 20980000.usb, Pi Zero 2 W: 3f980000.usb),
    so autodetect instead of hardcoding a default.
    """
    try:
        udcs = sorted(os.listdir(UDC_SYSFS))
    except OSError:
        udcs = []
    if not udcs:
        raise RuntimeError(
            f"no UDC found in {UDC_SYSFS} — run setup_gadget.sh and reboot "
            "(the dwc2 overlay must be active in peripheral mode)"
        )
    if len(udcs) > 1:
        print(f"[bridge] several UDCs {udcs}, using {udcs[0]} (override with --udc)")
    return udcs[0]


class BridgeState:
    def __init__(self):
        self._lock = threading.Lock()
        self._dev: AoaDevice | None = None
        self.status = "AOA not started yet"
        self.stop = threading.Event()

    def set_dev(self, dev: AoaDevice | None, status: str):
        with self._lock:
            self._dev = dev
            self.status = status
        logging.info(status)

    def dev(self) -> AoaDevice | None:
        with self._lock:
            return self._dev

    def ready_dev(self) -> AoaDevice | None:
        with self._lock:
            dev = self._dev
        return dev if dev is not None and dev.ready() else None


def start_aoa_worker(state: BridgeState, udc_arg: str | None, udc_driver_arg: str | None):
    def work():
        while not state.stop.is_set():
            dev = None
            try:
                udc = udc_arg or detect_udc()
                udc_driver = udc_driver_arg or udc
                logging.info("using UDC %s (driver %s)", udc, udc_driver)
                dev = AoaDevice(udc_driver, udc)
                state.set_dev(dev, f"AOA worker running on UDC {udc}")
                dev.run_forever()
                state.set_dev(None, "AOA worker stopped; retrying")
            except Exception:
                state.set_dev(None, "AOA worker crashed; retrying in 2s")
                logging.error("AOA worker exception:\n%s", traceback.format_exc())
                time.sleep(2)
            finally:
                if dev is not None:
                    try:
                        dev.stop()
                    except Exception:
                        logging.error("dev.stop failed:\n%s", traceback.format_exc())

    t = threading.Thread(target=work, name="aoa-worker", daemon=True)
    t.start()
    return t


def serve(state: BridgeState, host: str, port: int):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(4)
    print(f"[bridge] listening for laptop on {host}:{port}")

    # Multi-client: RX RC dimulticast ke SEMUA klien (streamer :9990 tetap
    # kebagian video saat MITM aktif). TX hanya dari klien owner — mitm.py
    # mengklaim dengan byte pertama 'M'; klien generic (streamer) mendapat
    # TX hanya saat sendirian — persis perilaku single-client lama.
    clients: list[dict] = []
    clients_lock = threading.Lock()
    owner = {"c": None}

    def rx_pump():
        active: AoaDevice | None = None
        while True:
            dev = state.ready_dev()
            if dev is None:
                if active is not None:
                    print("[bridge] AOA went away; keeping TCP clients connected")
                    active = None
                time.sleep(0.25)
                continue
            if dev is not active:
                active = dev
                drained = 0
                while not dev.rx_queue.empty():
                    try:
                        dev.rx_queue.get_nowait()
                        drained += 1
                    except Exception:
                        break
                print(f"[bridge] AOA available; drained {drained} stale frames")
            try:
                data = dev.rx_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            except Exception:
                logging.error("rx_queue read failed:\n%s", traceback.format_exc())
                time.sleep(0.5)
                continue
            with clients_lock:
                for c in clients:
                    if c["dead"]:
                        continue
                    try:
                        if c["tx"].full():
                            c["tx"].get_nowait()   # klien lambat: buang yg basi
                            c["dropped"] += 1
                        c["tx"].put_nowait(data)
                    except Exception:
                        c["dead"] = True

    threading.Thread(target=rx_pump, daemon=True).start()

    def reap():
        with clients_lock:
            for c in list(clients):
                if c["dead"]:
                    if owner["c"] is c:
                        owner["c"] = None
                        print("[bridge] MITM owner left — TX kembali ke generic")
                    try:
                        c["conn"].close()
                    except OSError:
                        pass
                    clients.remove(c)
                    print(f"[bridge] {c['addr']} disconnected "
                          f"(rx-dropped={c['dropped']})")

    def client_writer(c):
        while not c["dead"]:
            try:
                data = c["tx"].get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                c["conn"].sendall(data)
            except OSError:
                c["dead"] = True
                return

    def client_reader(c):
        conn = c["conn"]
        warned = False
        try:
            while not c["dead"]:
                data = conn.recv(4096)
                if not data:
                    break
                with clients_lock:
                    can_tx = (c is owner["c"]) or (
                        owner["c"] is None and len(clients) == 1)
                if not can_tx:
                    if not warned:
                        print(f"[bridge] {c['addr']} TX di-drop — "
                              "bukan owner", flush=True)
                        warned = True
                    continue
                dev = state.ready_dev()
                if dev is None:
                    if not warned:
                        print("[bridge] dropping laptop frames until AOA is ready")
                        warned = True
                    continue
                warned = False
                dev.send(data)
        except OSError:
            pass
        finally:
            c["dead"] = True
            reap()

    while True:
        conn, addr = srv.accept()
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        is_mitm = False
        try:
            conn.settimeout(0.3)
            if conn.recv(1, socket.MSG_PEEK) == b"M":
                conn.recv(1)            # konsumsi marker klaim owner
                is_mitm = True
        except socket.timeout:
            pass
        except OSError:
            conn.close()                # RST seketika — jangan bunuh accept loop
            continue
        finally:
            try:
                conn.settimeout(None)
            except OSError:
                pass                    # conn sudah close di jalur RST
        c = {"conn": conn, "addr": addr, "tx": queue.Queue(8192),
             "dead": False, "dropped": 0}
        with clients_lock:
            clients.append(c)
            if is_mitm:
                if owner["c"] is not None and owner["c"] is not c:
                    owner["c"]["dead"] = True
                owner["c"] = c
                print(f"[bridge] MITM owner connected {addr}")
            else:
                print(f"[bridge] connected {addr} ({len(clients)} clients)")
        reap()
        threading.Thread(target=client_writer, args=(c,), daemon=True).start()
        threading.Thread(target=client_reader, args=(c,), daemon=True).start()


def main():
    setup_logging()
    install_crash_handlers()
    ap = argparse.ArgumentParser(description="Pi AOA<->TCP bridge")
    ap.add_argument("--udc", default=None,
                    help="UDC name (see /sys/class/udc/); autodetected if omitted")
    ap.add_argument("--udc-driver", default=None,
                    help="UDC driver name (defaults to the same as --udc)")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=9910)
    ap.add_argument("--model", default="com.dji.logiclink",
                    help="expected model from the remote controller (for logs)")
    args = ap.parse_args()

    state = BridgeState()
    start_aoa_worker(state, args.udc, args.udc_driver)
    try:
        serve(state, args.host, args.port)
    except KeyboardInterrupt:
        print("\n[bridge] exit")
    except Exception:
        logging.critical("bridge main crashed:\n%s", traceback.format_exc())
        raise
    finally:
        state.stop.set()
        dev = state.dev()
        if dev is not None:
            dev.stop()


if __name__ == "__main__":
    main()
