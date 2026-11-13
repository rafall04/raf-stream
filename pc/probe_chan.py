"""Final channel/route sweep: same video-gate candidates on the aux DUML
channel 0x7530, RC-local commands (0x06 set_usb_mode/app_work_stage), and
non-DUML channels 0x574F/0x5759."""
import socket, struct, sys, time, threading
sys.path.insert(0, ".")
from duss import wrap
from duml import DumlPacket

HOST, PORT = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1", 9910
_seq = [300]

def duml(cmd_set, cmd_id, payload, dst, sender=0x02, cmd_type=0x40):
    _seq[0] += 1
    p = DumlPacket(sender=sender, receiver=dst, seq=_seq[0],
                   cmd_type=cmd_type, cmd_set=cmd_set, cmd_id=cmd_id,
                   payload=payload)
    return p.encode()

stop = threading.Event()
hits = []

def rx_loop(sock):
    buf = b""
    while not stop.is_set():
        try:
            d = sock.recv(65536)
        except socket.timeout:
            continue
        if not d:
            break
        buf += d
        while len(buf) >= 8:
            typ, ln = struct.unpack_from("<HI", buf, 2)
            if buf[0] != 0x55 or buf[1] != 0xCC or ln > 0x200000:
                buf = buf[1:]
                continue
            if len(buf) < 8 + ln:
                break
            unit, buf = buf[8:8 + ln], buf[8 + ln:]
            if typ != 0x5749:
                print(f"[VIDEO?] chan {typ:#06x} {len(unit)}B: {unit[:48].hex()}",
                      flush=True)
                hits.append(typ)
            else:
                try:
                    p = DumlPacket.decode(unit)
                    if p.cmd_type & 0x80:
                        print(f"[rx] {p}", flush=True)
                except Exception:
                    pass

APPREG = bytes.fromhex("17000023004150500000000000000258a6")
CAMCAP = bytes.fromhex(
    "02020000d507000000000013000d0063616d6361705f636f6d6d6f6e00000000d093")
ENABLE = bytes.fromhex("00040200000000000000")

# (channel, duml_bytes) — send each frame on 0x5749 and 0x7530 where noted
PROBES = [
    # --- registration/magic packets over the AUX DUML channel 0x7530 ---
    (0x7530, duml(0x00, 0x99, CAMCAP, 0x28)),
    (0x7530, duml(0x00, 0x88, APPREG, 0x28)),
    (0x7530, duml(0x00, 0x88, APPREG, 0x89)),
    (0x7530, duml(0x00, 0x88, APPREG, 0x0E)),
    (0x7530, duml(0x09, 0xA8, ENABLE, 0x89)),
    (0x7530, duml(0x09, 0xA8, ENABLE, 0x0E)),
    # --- RC-local video gating (0x06 cmdset) on 0x5749 ---
    (0x5749, duml(0x06, 0x3A, bytes([1]), 0x06)),           # set_usb_mode=1
    (0x5749, duml(0x06, 0x3A, bytes([2]), 0x06)),           # mode=2
    (0x5749, duml(0x06, 0x8C, bytes([1]), 0x06)),           # app_work_stage=1
    (0x5749, duml(0x06, 0x8C, bytes([2]), 0x06)),           # stage=2 (fpv?)
    (0x5749, duml(0x06, 0x8C, bytes([1, 1]), 0x06)),
    (0x5749, duml(0x06, 0xF1, bytes([1]), 0x06)),           # app_to_pc_control
    (0x5749, duml(0x06, 0x3A, bytes([1]), 0x2D)),           # usb_mode on rcs231
    (0x5749, duml(0x06, 0x8C, bytes([1]), 0x2D)),
    (0x5749, duml(0x06, 0x8C, bytes([1]), 0x1F)),
    # --- 0x5759 LB-payload channel: try a DUML inside it ---
    (0x5759, duml(0x00, 0x88, APPREG, 0x0E)),
    (0x5759, duml(0x00, 0x88, APPREG, 0x28)),
    # --- 0x574F accepted channel ---
    (0x574F, duml(0x00, 0x88, APPREG, 0x0E)),
]

def main():
    s = socket.create_connection((HOST, PORT), 5)
    s.settimeout(0.5)
    threading.Thread(target=rx_loop, args=(s,), daemon=True).start()
    time.sleep(1.0)
    for chan, frame in PROBES:
        s.sendall(wrap(frame, chan))
        try:
            p = DumlPacket.decode(frame)
            print(f"[tx] chan={chan:#06x} {p.cmd_set:#04x}/{p.cmd_id:#04x}"
                  f"->{p.receiver:#04x} pl={p.payload.hex()}", flush=True)
        except Exception:
            print(f"[tx] chan={chan:#06x} raw", flush=True)
        time.sleep(0.35)
    print("[probe] done — sniff 30s", flush=True)
    time.sleep(30)
    stop.set()
    print(f"[probe] non-0x5749 hits: {hits}", flush=True)

if __name__ == "__main__":
    main()
