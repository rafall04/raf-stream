#!/usr/bin/env python3
"""
Session tool — talks to the AOA bridge on the STB (TCP :9910) and drives the
RC-N1/DJI session from a PC.

    [this tool] --TCP--> [stb bridge.py :9910] --AOA bulk--> [RC-N1] ))) [drone]

Implements, byte-exact, the RC-N1 session flow (FreeFCC-USB) plus the WM160
liveview recipe (DJI-Link src/core/drone.cpp):

  bootstrap : DUML 0x00/0x00  sender=0x02 type=0x40  dst 0x1F then 0x00,
              payload 00 00 01   (sent once, right after TCP connect)
  keepalive : DUML 0x06/0x77  sender=0x02 type=0x40  dst 0x06 and 0x0E,
              payload 01 01 00 FF FF 20 00 00   (every 2.5 s)
  liveview  : 0x02/0x09 -> camera(0x01)  [src u8]
              0x08/0x41 -> dm368(0x08)   [05 + 5x(codec u8, cap u32)]  codec0 cap=FFFFFFFF
              0x08/0x42 -> dm368         [fps u16 LE=30][00 00]
              0x08/0x69 -> dm368         [00 64 00]
              0x02/0xB3 -> camera        []  (request I-frame; HEVC intra-refresh needs it)

Every frame is wrapped as a composite unit: 55 CC 49 57 <len u32 LE> <DUML>.

Usage:
    python3 session.py <stb-ip>                 # bootstrap + keepalive + sniff
    python3 session.py <ip> --liveview               # also send the video-start recipe
    python3 session.py <ip> --liveview --dump v.hevc # and record the ES to a file
    ffplay -f hevc v.hevc                            # play it back

Flags: --no-bootstrap, --no-keepalive, --bare (send raw DUML, no 55 CC wrap —
diagnostic only; FreeFCC reports bare DUML is rejected by RC-N1),
--sender (0x02 app idx0 | 0x82 app idx4), --camera-dev / --dm368-dev (receiver
overrides — RC-N1-era captures show the camera at 0x18, not WM160's 0x01),
--wait-aircraft SEC (hold liveview until the drone serial appears in telemetry
— FreeFCC's waitForAircraft; drone serials look like 1581T...),
--cmd 'set:id:hexpayload[:dst]' (arbitrary DUML, repeatable; e.g. '8:78::' =
uav_dm368_set_sh_start_live_streaming), --dst (default receiver for --cmd).
"""
from __future__ import annotations
import argparse
import os
import re
import socket
import struct
import sys
import threading
import time

from duss import (DumlPacket, CompositeDemux, TYPE_DUML, TYPE_NAMES, CMD_NAMES,
                  OSD_COMMON_IDS, osd_parse, param_hash,
                  BATTERY_IDS, battery_parse, FLYC_STATES,
                  litchis_list, litchis_file, litchis_recv_parse,
                  encrypt_frame, decrypt_frame,
                  wrap, lv_parse, lv_slice)

# Drone serials match e.g. 1581T... (Mini 3 starts 1581). FreeFCC uses the same
# heuristic to decide the aircraft is actually linked through the RC before
# sending commands — telemetry only carries the serial once the drone relays.
SERIAL_RE = re.compile(rb"1581[0-9A-Z]{12,18}")

APP = 0x02          # sender: mobile app idx0. FreeFCC's sweep also tries 0x82 (idx4,
                    # the value seen in the original capture) — set --sender 0x82 if
                    # commands get no replies with the default.
DEV_CAMERA = 0x01   # WM160 camera. On the RC-N1 generation (FreeFCC captures) the
                    # camera/SVO answers at 0x18 — override with --camera-dev.
DEV_RC = 0x06
DEV_DM368 = 0x08    # WM160 video processor. Mini 3 has no DM368 (newer encoder) —
                    # the 0x08/0x4x commands may be ignored there; watch responses.
INTER_FRAME_DELAY = 0.003    # FreeFCC: ~3 ms between frames (30 ms in FCC bursts)
KEEPALIVE_PERIOD = 2.5


def _default_cmdmap() -> str | None:
    """Bundled cmds.json next to this script, else the one in the cloned ref."""
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(here, "cmds.json"),
                 os.path.join(here, os.pardir, "refs", "dji-link",
                              "dji_link_beta", "reverse_docs", "cmds.json")):
        if os.path.isfile(cand):
            return cand
    return None


class Session:
    def __init__(self, args):
        self.args = args
        self.sock: socket.socket | None = None
        self.seq = 0
        self.tx_mu = threading.Lock()
        self.stop = threading.Event()
        # --dump - sends the ES to stdout; every log print goes to stderr by
        # swapping sys.stdout before anything else runs (print() follows it).
        if args.dump == "-":
            self.dump_fp = sys.stdout.buffer
            sys.stdout = sys.stderr
        elif args.dump:
            self.dump_fp = open(args.dump, "wb")
        else:
            self.dump_fp = None
        self.video_pkts = 0
        self.video_bytes = 0
        self.frames_done = 0
        self._frame_parts: dict[int, dict[tuple, bytes]] = {}
        self._stats_t = time.time()
        self.aircraft_serial: bytes | None = None
        self.senders_seen: set[int] = set()
        self._osd_n = 0
        self._bat_n = 0
        self.rec_fp = open(args.record, "w") if args.record else None
        self.cmd_names = dict(CMD_NAMES)
        if args.cmdmap:
            try:
                import json
                table = json.load(open(args.cmdmap))
                for e in table:
                    self.cmd_names.setdefault((e["cmd_set"], e["cmd_id"]),
                                              e["req"].removesuffix("_req"))
            except Exception as e:
                print(f"[session] cmdmap load failed: {e}", flush=True)

    def _rec(self, direction: str, data: bytes):
        """Raw-wire recorder: JSONL {t,dir,hex} — replayable ground truth for
        offline demux/dissector work; captures whatever the RC really sent."""
        if self.rec_fp:
            import json
            self.rec_fp.write(json.dumps(
                {"t": round(time.time(), 6), "dir": direction,
                 "hex": data.hex()}) + "\n")

    # ---------------- TX ----------------
    def _next_seq(self) -> int:
        s = self.seq
        self.seq = (self.seq + 1) & 0xFFFF
        return s

    def send_duml(self, cmd_set: int, cmd_id: int, payload: bytes,
                  receiver: int, cmd_type: int = 0x40, note: str = ""):
        pkt = DumlPacket(sender=self.args.sender, receiver=receiver, cmd_set=cmd_set,
                         cmd_id=cmd_id, payload=payload,
                         seq=self._next_seq(), cmd_type=cmd_type)
        frame = pkt.encode()
        if self.args.encrypt:
            frame = encrypt_frame(frame)
        data = frame if self.args.bare else wrap(frame, TYPE_DUML)
        with self.tx_mu:
            try:
                self.sock.sendall(data)
                self._rec("tx", data)
            except OSError as e:
                print(f"[tx] send failed: {e}", flush=True)
                self.stop.set()
                return
        print(f"[tx] {note or 'cmd'} {pkt}", flush=True)
        time.sleep(INTER_FRAME_DELAY)

    def bootstrap(self):
        print("[session] bootstrap (dst 0x1F + 0x00, cmd 00/00, payload 00 00 01)")
        self.send_duml(0x00, 0x00, bytes.fromhex("000001"), 0x1F, note="boot1")
        self.send_duml(0x00, 0x00, bytes.fromhex("000001"), 0x00, note="boot2")

    def keepalive_loop(self):
        while not self.stop.wait(KEEPALIVE_PERIOD):
            self.send_duml(0x06, 0x77, bytes.fromhex("01 01 00 FF FF 20 00 00"),
                           DEV_RC, note="keepalive rc")
            self.send_duml(0x06, 0x77, bytes.fromhex("01 01 00 FF FF 20 00 00"),
                           0x0E, note="keepalive 0x0e")

    def start_liveview(self):
        src = self.args.camera_source & 0xFF
        cam = self.args.camera_dev
        enc = self.args.dm368_dev
        print(f"[session] liveview recipe (WM160), camera_source={src} "
              f"cam={cam:#04x} enc={enc:#04x}")
        self.send_duml(0x02, 0x09, bytes([src]), cam, note="lv source")
        cap = bytearray([5])
        for codec in range(5):
            cap.append(codec)
            cap += struct.pack("<I", 0xFFFFFFFF if codec == 0 else 0)
        self.send_duml(0x08, 0x41, bytes(cap), enc, note="lv caps")
        fps = struct.pack("<H", self.args.fps) + b"\x00\x00"
        self.send_duml(0x08, 0x42, fps, enc, note="lv fps")
        self.send_duml(0x08, 0x69, bytes([0, 100, 0]), enc, note="lv bw")
        self.send_duml(0x02, 0xB3, b"", cam, note="request I-frame")

    def send_param(self, spec: str, cmd_id: int, note: str):
        """spec = 'param.name' or 'param.name:hexvalue' — 0x03/0xF7-F9 hashed param ops."""
        name, _, val = spec.partition(":")
        h = param_hash(name)
        payload = struct.pack("<I", h) + (bytes.fromhex(val) if val else b"")
        self.send_duml(0x03, cmd_id, payload, 0x03,
                       note=f"{note} {name} h={h:#010x}")

    def probe_devices(self):
        """get_version sweep — replies reveal which component addresses are alive."""
        # broadcast + WM160 addresses + RC-N1-era addresses (FreeFCC captures)
        for dst in (0x1F, 0x03, 0x01, 0x04, 0x0B, 0x06, 0x08, 0x12, 0x18, 0x07,
                    0x09, 0x6F, 0x92, 0xEE):
            self.send_duml(0x00, 0x01, b"", dst, note=f"probe get_version->{dst:#04x}")

    def send_media_probe(self):
        """DEX-confirmed media probes: enter MEDIA_DOWNLOAD, then a
        serializer-exact litchis LIST (FileSendPack header + DataRequestList).
        Legacy pipeline — modern crossplayback framing is capture-pending, so
        every rx unit still gets recorded via --record."""
        self.send_duml(0x02, 0x10, bytes([self.args.media_mode]),
                       self.args.camera_dev, note="media-enter")
        if self.args.litchis_list is not None:
            start, count = self.args.litchis_list
            self.send_duml(0x00, 0x26, litchis_list(start, count),
                           self.args.camera_dev, note="litchis-list")

    def send_custom(self, spec: str):
        """--cmd 'set:id:hexpayload[:dst]' — arbitrary DUML for on-hardware experiments."""
        parts = spec.split(":")
        if len(parts) < 3:
            raise ValueError(f"--cmd format is set:id:payload[:dst], got {spec!r}")

        def _num(s: str) -> int:
            # DUML docs write set/id/dst in hex (0x41, 0xB3); bare values are hex too
            return int(s, 0) if s.lower().startswith("0x") else int(s, 16)

        cmd_set, cmd_id = _num(parts[0]), _num(parts[1])
        payload = bytes.fromhex(parts[2]) if parts[2] else b""
        dst = _num(parts[3]) if len(parts) > 3 and parts[3] else self.args.dst
        self.send_duml(cmd_set, cmd_id, payload, dst,
                       note=f"custom {cmd_set:#04x}/{cmd_id:#04x}->{dst:#04x}")

    # ---------------- RX ----------------
    def on_unit(self, typ: int, payload: bytes):
        name = TYPE_NAMES.get(typ, "?")
        if typ not in TYPE_NAMES or name.endswith("?"):
            print(f"[rx] chan {typ:#06x} ({name}) {len(payload)}B: {payload[:48].hex()}",
                  flush=True)

    def on_duml(self, payload: bytes):
        m = SERIAL_RE.search(payload)
        if m and self.aircraft_serial != m.group():
            self.aircraft_serial = m.group()
            print(f"[session] AIRCRAFT LINKED — serial {self.aircraft_serial.decode()}",
                  flush=True)
        try:
            if len(payload) > 10 and (payload[8] & 0x07) == 0x03:
                payload = decrypt_frame(payload)   # SIMPLE-encrypted reply
            p = DumlPacket.decode(payload)
            self.senders_seen.add(p.sender)
            name = self.cmd_names.get((p.cmd_set, p.cmd_id), "")
            if (p.cmd_set, p.cmd_id) in OSD_COMMON_IDS:
                self._osd_n += 1
                if self._osd_n % 10 == 1:   # ~10 Hz push; print ~1/s
                    o = osd_parse(p.payload)
                    if o:
                        mode = FLYC_STATES.get(o['flyc_state'], f"?{o['flyc_state']}")
                        print(f"[osd] lat={o['lat']:.6f} lon={o['lon']:.6f} "
                              f"h={o['height_m']:.1f}m sats={o['gps_sats']} "
                              f"bat={o['battery_pct']}% mode={mode} "
                              f"air={o['in_air']} cause={o['motor_cause']}", flush=True)
            elif (p.cmd_set, p.cmd_id) in BATTERY_IDS:
                self._bat_n += 1
                if self._bat_n % 10 == 1:
                    b = battery_parse(p.payload)
                    if b:
                        print(f"[bat] {b['voltage_mv']/1000:.2f}V {b['pct']}% "
                              f"{b['cap_remain_mah']}/{b['cap_full_mah']}mAh "
                              f"{b['temp_c']:.1f}C {b['current_ma']}mA", flush=True)
            elif (p.cmd_set, p.cmd_id) in ((0x00, 0x26), (0x00, 0x27)):
                lh = litchis_recv_parse(p.payload)
                if lh:
                    print(f"[rx-litchis] cmd={lh['cmd']} type={lh['type']} "
                          f"pkt={lh['pkt_idx']} len={lh['length']} "
                          f"off={lh['offset']} sess={lh['session']} "
                          f"body={lh['body'][:32].hex()}", flush=True)
                else:
                    print(f"[rx-duml] {p} {name}".rstrip(), flush=True)
            else:
                print(f"[rx-duml] {p} {name}".rstrip(), flush=True)
        except Exception as e:
            print(f"[rx-duml] undecodable {len(payload)}B ({e}): {payload[:48].hex()}",
                  flush=True)

    def on_video(self, payload: bytes):
        h = lv_parse(payload)
        self.video_pkts += 1
        if h is None:
            if self.video_pkts <= 5:
                print(f"[rx-video] non-0x6d payload {len(payload)}B: {payload[:32].hex()}",
                      flush=True)
            return
        s = lv_slice(payload)
        if s:
            self.video_bytes += len(s)
            if self.dump_fp:
                self.dump_fp.write(s)
        # reassemble by frm_idx -> (slice_idx, pkt_idx); frame done on frm_end
        fi = h["frm_idx"] & 0xFFFF
        self._frame_parts.setdefault(fi, {})[(h["slice_idx"], h["pkt_idx"])] = s or b""
        if h["frm_end"]:
            self.frames_done += 1
            self._frame_parts.pop(fi, None)
            if self.frames_done == 1:
                print(f"[rx-video] first frame complete (frm_idx={fi}, fmt={h['video_fmt']}, "
                      f"fps={h['fps']})", flush=True)

    def stats_tick(self):
        while not self.stop.wait(10):
            d = self._demux
            counts = ", ".join(f"{TYPE_NAMES.get(t, hex(t))}={n}"
                               for t, n in sorted(d.type_counts.items()))
            air = self.aircraft_serial.decode() if self.aircraft_serial else "no"
            snd = ",".join(hex(s) for s in sorted(self.senders_seen))
            print(f"[stats] units={d.units} buf={len(d.buf)} | {counts} | "
                  f"video pkts={self.video_pkts} es={self.video_bytes}B "
                  f"frames={self.frames_done} | aircraft={air} senders={snd}",
                  flush=True)

    # ---------------- run ----------------
    def run(self):
        self.sock = socket.create_connection((self.args.host, self.args.port))
        self.sock.settimeout(1.0)
        print(f"[session] connected {self.args.host}:{self.args.port}", flush=True)

        self._demux = CompositeDemux(on_duml=self.on_duml,
                                     on_video=self.on_video,
                                     on_unit=self.on_unit)

        if not self.args.no_bootstrap:
            self.bootstrap()
        if not self.args.no_keepalive:
            threading.Thread(target=self.keepalive_loop, daemon=True,
                             name="keepalive").start()
        if self.args.probe:
            threading.Timer(1.0, self.probe_devices).start()
        for name in self.args.param_info or []:
            self.send_param(name, 0xF7, "param-info")
        for name in self.args.param_read or []:
            self.send_param(name, 0xF8, "param-read")
        for spec in self.args.param_write or []:
            self.send_param(spec, 0xF9, "param-write")
        for spec in self.args.cmd or []:
            try:
                self.send_custom(spec)
            except ValueError as e:
                print(f"[session] {e}", flush=True)
        if self.args.media_probe:
            threading.Timer(1.5, self.send_media_probe).start()
        if self.args.liveview:
            time.sleep(self.args.liveview_delay)
            if self.args.wait_aircraft:
                deadline = time.time() + self.args.wait_aircraft
                while self.aircraft_serial is None and time.time() < deadline \
                        and not self.stop.is_set():
                    time.sleep(0.5)
                if self.aircraft_serial is None:
                    print("[session] no aircraft telemetry in "
                          f"{self.args.wait_aircraft}s — sending liveview anyway",
                          flush=True)
            self.start_liveview()
        threading.Thread(target=self.stats_tick, daemon=True, name="stats").start()

        try:
            while not self.stop.is_set():
                try:
                    data = self.sock.recv(65536)
                except socket.timeout:
                    continue
                if not data:
                    print("[session] TCP closed by bridge", flush=True)
                    break
                self._rec("rx", data)
                self._demux.feed(data)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop.set()
            if self.rec_fp:
                self.rec_fp.close()
            if self.dump_fp:
                self.dump_fp.close()
                print(f"[session] wrote {self.video_bytes} ES bytes to {self.args.dump}")
            print("[session] done", flush=True)


def main():
    ap = argparse.ArgumentParser(description="RC-N1 session tool over the STB AOA bridge")
    ap.add_argument("host")
    ap.add_argument("--port", type=int, default=9910)
    ap.add_argument("--no-bootstrap", action="store_true")
    ap.add_argument("--no-keepalive", action="store_true")
    ap.add_argument("--liveview", action="store_true",
                    help="send the WM160 liveview-start recipe")
    ap.add_argument("--liveview-delay", type=float, default=1.0,
                    help="seconds after connect before the liveview recipe")
    ap.add_argument("--wait-aircraft", type=float, default=0, metavar="SEC",
                    help="wait up to SEC for the drone serial in telemetry "
                         "before sending liveview (FreeFCC waitForAircraft)")
    ap.add_argument("--camera-source", type=int, default=0)
    ap.add_argument("--camera-dev", type=lambda s: int(s, 0), default=DEV_CAMERA,
                    help="liveview receiver for the 0x02/0x09+0xB3 commands "
                         "(WM160=0x01; RC-N1-era captures show 0x18)")
    ap.add_argument("--dm368-dev", type=lambda s: int(s, 0), default=DEV_DM368,
                    help="liveview receiver for the 0x08/0x4x commands (WM160=0x08)")
    ap.add_argument("--sender", type=lambda s: int(s, 0), default=APP,
                    help="sender address for our DUML (0x02=app idx0, 0x82=app idx4)")
    ap.add_argument("--dst", type=lambda s: int(s, 0), default=DEV_RC,
                    help="default receiver for --cmd when no :dst given")
    ap.add_argument("--cmd", action="append", metavar="SET:ID:HEX[:DST]",
                    help="send an arbitrary DUML (repeatable). Examples: "
                         "'8:78::'  (uav_dm368_set_sh_start_live_streaming), "
                         "'2:10:00:18' (camera work mode to RC-N1-era camera)")
    ap.add_argument("--record", metavar="FILE",
                    help="record raw wire bytes both directions as JSONL "
                         "{t,dir,hex} — replayable capture for offline analysis")
    ap.add_argument("--media-probe", action="store_true",
                    help="send DEX-confirmed media probes: camera work-mode "
                         "MEDIA_DOWNLOAD then a litchis LIST request")
    ap.add_argument("--media-mode", type=lambda s: int(s, 0), default=3,
                    help="camera work-mode byte for --media-probe "
                         "(DEX: 2=PLAYBACK, 3=MEDIA_DOWNLOAD; default 3)")
    ap.add_argument("--litchis-list", metavar="START:COUNT", nargs=2,
                    type=lambda s: int(s, 0),
                    help="with --media-probe: also send a litchis LIST request "
                         "at index START for COUNT records")
    ap.add_argument("--probe", action="store_true",
                    help="get_version sweep over common receiver addresses (who answers)")
    ap.add_argument("--param-info", action="append", metavar="NAME",
                    help="0x03/0xF7 query a flyc param's type/size by name hash")
    ap.add_argument("--param-read", action="append", metavar="NAME",
                    help="0x03/0xF8 read a flyc param value by name hash")
    ap.add_argument("--param-write", action="append", metavar="NAME:HEXVAL",
                    help="0x03/0xF9 write a flyc param (hashed name + LE value); "
                         "plaintext on an unencrypted link — do NOT force encryption")
    ap.add_argument("--cmdmap", metavar="CMDS_JSON", nargs="?", const=_default_cmdmap(),
                    help="load a uav_* name table (refs/dji-link cmds.json) to decode "
                         "RX command names; bare --cmdmap uses the bundled copy")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--dump", metavar="FILE",
                    help="write the video elementary stream (slices) to FILE")
    ap.add_argument("--bare", action="store_true",
                    help="send raw DUML without the 55 CC composite wrap (diagnostic)")
    ap.add_argument("--encrypt", action="store_true",
                    help="SIMPLE-encrypt all outgoing DUML (cmd_type 0x43). Only if "
                         "the link negotiates encryption — plaintext works otherwise")
    args = ap.parse_args()
    Session(args).run()


if __name__ == "__main__":
    main()
