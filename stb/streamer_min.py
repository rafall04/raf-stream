#!/usr/bin/env python3
"""Минимальный синтезированный HEVC-стример для RC-N1 — без replay-рецепта.

Топология та же, что у streamer.py:

    RC-N1 --AOA--> [dji-bridge :9910] <--TCP-- [этот скрипт] --> HEVC

Вместо воспроизведения захвата строим сессию с нуля по структуре,
снятой с DJI Fly + FreeFCC-USB:

  1. bootstrap: 2 кадра 00/00 -> 0x1f и -> 0x00, payload 00 00 01
  2. keepalive 2.5 c: пара 06/77 -> 0x06 и -> 0x0e, payload 01 01 00 ff ff 20 00 00
  3. link heartbeat: сырой байт 0xb0 каждую 1 с (не RCLink — в захвате так)
  4. видео-включение: 08/41 send_decode_capability -> 0x01, payload 0x02
     (в захвате port 0x574a открылся через ~1.2 с после этого кадра)
  5. добивка видео-сессии: 00/0e heartbeat -> 0x2d и 00/4f version-config -> 0x2d,
     как в хвосте захвата перед первым видео-юнитом

Никаких украденных seq/binding — все кадры свежие, CRC16/8 строится через
pc/duss.py. Если RC не открывает 0x574a, 08/41 переотправляется каждые
--retry секунд.
"""
import argparse
import itertools
import os
import socket
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "pc"))
sys.path.insert(0, os.path.join(_HERE, "pc"))
sys.path.insert(0, _HERE)
from duss import DumlPacket, wrap            # noqa: E402

from streamer import VID_PORT, SinkWriter, parse_units   # noqa: E402

_SEQ = itertools.count(149)
_tx_mu = threading.Lock()

KA_PAYLOAD = bytes.fromhex("010100ffff200000")

# Burst "masuk camera view" — dari dua capture DJI Fly (sesi A + B):
# urutan yang persis mendahului terbukanya port video 0x574a.
def enable_burst(sock):
    """Sintesis sekuens enable-video yang diamati di kedua capture."""
    seq = [
        # subscribe parameter kamera (0x01): blok 0 + 0x3cc + 0x798,
        # lalu refresh-rate 1 s — ini permintaan telemetri kamera, bukan video
        (0x00, 0x4F, 0x01, "0100000000ffffffff"),
        (0x00, 0x4F, 0x01, "01cc030000ffffffff"),
        (0x00, 0x4F, 0x01, "0198070000ffffffff"),
        (0x08, 0x41, 0x01, "02"),          # decode_capability: HEVC
        (0x00, 0x4F, 0x01, "0100000000e8030000"),
        (0x02, 0xB5, 0x01, "00000000"),   # query status kamera
        (0x00, 0xFF, 0x0E, ""),          # permintaan ke agregator 0x0e
        # kunci pembuka 0x574a: subscribe ke komponen video 0x2d blok 0-5
        (0x00, 0x4F, 0x2D, "0100000000ffffffff"),
        (0x00, 0x4F, 0x2D, "0100010000ffffffff"),
        (0x00, 0x4F, 0x2D, "0100020000ffffffff"),
        (0x00, 0x4F, 0x2D, "0100030000ffffffff"),
        (0x00, 0x4F, 0x2D, "0100040000ffffffff"),
        (0x00, 0x4F, 0x2D, "0100050000ffffffff"),
        (0x00, 0x4F, 0x2D, "040000000000000000"),  # commit subscribe
        (0x00, 0x0E, 0x2D, "000100"),   # heartbeat ke 0x2d
    ]
    for cs, ci, dst, pay in seq:
        send(sock, cs, ci, dst, bytes.fromhex(pay))
        time.sleep(0.003)   # jarak antar-frame seperti di capture (~3 ms)



def send(sock, cmd_set, cmd_id, dst, payload, src=0x02, cmd_type=0x40):
    pkt = DumlPacket(sender=src, receiver=dst, cmd_set=cmd_set, cmd_id=cmd_id,
                     payload=payload, seq=next(_SEQ) & 0xFFFF,
                     cmd_type=cmd_type)
    with _tx_mu:
        sock.sendall(wrap(pkt.encode()))


def keepalive_thread(sock, stop):
    """Фоновые keepalive: 06/77 пара 2.5 с, b0 байт 1 с, добивка 0x2d 5 с."""
    t_ka = t_b0 = t_2d = 0.0
    while not stop.is_set():
        now = time.monotonic()
        try:
            if now - t_ka >= 2.5:
                for dst in (0x06, 0x0E):
                    send(sock, 0x06, 0x77, dst, KA_PAYLOAD)
                t_ka = now
            if now - t_b0 >= 1.0:
                with _tx_mu:
                    sock.sendall(b"\xb0")
                t_b0 = now
            if now - t_2d >= 2.0:
                # refresh subscribe 0x01/0x2d — di capture phone mengulang
                # 00/4f tiap 0.5-2 s selama video jalan (subscription TTL)
                send(sock, 0x00, 0x4F, 0x01,
                     bytes.fromhex("0100000000e8030000"))
                send(sock, 0x00, 0x4F, 0x2D,
                     bytes.fromhex("0100000000ffffffff"))
                send(sock, 0x00, 0x0E, 0x2D, bytes.fromhex("000100"))
                t_2d = now
        except OSError:
            return
        stop.wait(0.1)


def main():
    ap = argparse.ArgumentParser(description="Синтезированный HEVC-стример "
                                           "Mini 3 через AOA-мост STB")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9910)
    ap.add_argument("--dump", metavar="FILE", default="/run/dji-video.fifo",
                    help="куда писать HEVC (FIFO/файл); '-' = stdout")
    ap.add_argument("--capability", type=int, default=0x02,
                    help="байт decode_capability для 08/41 (наблюдали 0x02)")
    ap.add_argument("--retry", type=float, default=5.0,
                    help="переотправка 08/41, если нет видео, секунд")
    args = ap.parse_args()

    log = sys.stderr
    out = (sys.stdout.buffer if args.dump == "-"
           else SinkWriter(args.dump, log))
    stats = {"vid": 0, "vid_bytes": 0, "duml": 0}

    sock = socket.create_connection((args.host, args.port), timeout=10)
    sock.settimeout(None)
    print("[streamer-min] connected, waiting for RC", file=log, flush=True)

    # RC сам шлёт телеметрию после конфигурирования гаджета — ждём её.
    first = sock.recv(65536)
    print("[streamer-min] RC alive — bootstrap", file=log, flush=True)
    sock.settimeout(1.0)

    send(sock, 0x00, 0x00, 0x1F, bytes.fromhex("000001"))
    send(sock, 0x00, 0x00, 0x00, bytes.fromhex("000001"))

    stop = threading.Event()
    ka = threading.Thread(target=keepalive_thread, args=(sock, stop),
                          daemon=True)
    ka.start()

    buf = bytearray(first)
    t_stat = t_enable = time.monotonic()
    enabled_once = False
    try:
        while True:
            try:
                data = sock.recv(65536)
            except socket.timeout:
                data = None
            if data == b"":
                print("[streamer-min] bridge closed", file=log, flush=True)
                break
            if data:
                buf += data
                for port, payload, _raw in parse_units(buf):
                    if port == VID_PORT:
                        stats["vid"] += 1
                        stats["vid_bytes"] += len(payload)
                        out.write(payload)
                        out.flush()
                    else:
                        stats["duml"] += 1
            now = time.monotonic()
            # Переотправляем decode_capability пока видео не пошло —
            # в захвате stream открылся ~1.2 с после кадра.
            if stats["vid"] == 0 and now - t_enable >= args.retry:
                enable_burst(sock)
                if not enabled_once:
                    print("[streamer-min] sent enable burst (08/41+%s)"
                          % "0x2d-subscribe", file=log, flush=True)
                    enabled_once = True
                t_enable = now
            if now - t_stat > 15:
                print(f"[streamer-min] vid={stats['vid']}pkts "
                      f"{stats['vid_bytes']/1e6:.1f}MB "
                      f"duml={stats['duml']}", file=log, flush=True)
                t_stat = now
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
    print(f"[streamer-min] done, video {stats['vid_bytes']/1e6:.1f}MB",
          file=log, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
