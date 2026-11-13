"""
DUSS composite mux + DUML codec — self-contained, for the PC-side session tool.

Wire format (verified from DJI Fly reversing + FreeFCC-USB + DJI-Link):
    55 CC | type u16 LE | length u32 LE | payload[length]
    type 0x5749 = DUML channel (payload is a DUML frame starting with 0x55)
    type 0x574A / 0x574D = video (payload starts with 0x6d, 16-byte liveview header)

DUML v1 frame:
    55 | len(10b)+ver(6b) LE | crc8(seed 0x77) | sender | receiver | seq LE |
    cmd_type | cmd_set | cmd_id | payload | crc16(seed 0x3692) LE

Adapted from refs/dji-link (Apache-2.0): dji_link_beta/duml.py + composite.py.
"""
from __future__ import annotations
import math
import struct
from dataclasses import dataclass

SOF0, SOF1 = 0x55, 0xCC
TYPE_DUML = 0x5749
TYPE_DUML_AUX = 0x7530         # aux DUML channel — same parser, per the app
TYPE_VIDEO = (0x574A, 0x574B, 0x574C, 0x574D, 0x574E, 0x5750, 0x5758)
# 11 known channels in the app: DUML 0x5749/0x7530, video-class 0x574A-0x5758,
# LB payload 0x5759 (non-video), accepted-but-unrouted 0x574F.

# channel names seen in DJI Fly reversing; unknown types still get counted/logged
TYPE_NAMES = {
    0x5749: "DUML", 0x7530: "DUML_AUX",
    0x574A: "VIDEO_A", 0x574B: "VIDEO_B", 0x574C: "VIDEO_C", 0x574D: "VIDEO_D",
    0x574E: "VIDEO_E", 0x5750: "VIDEO_SDR", 0x5758: "VIDEO_X",
    0x5759: "LB_PAYLOAD", 0x574F: "chan_F",
}

# uav_* registry names (full_table.txt / cmds.json) + link-layer names from FreeFCC
CMD_NAMES = {
    (0x00, 0x00): "general.ping",          # also the RC-N1 bootstrap frame
    (0x00, 0x01): "general.get_version",
    (0x00, 0x0E): "general.heartbeat",
    (0x00, 0x32): "general.activate_device",
    (0x02, 0x02): "camera.record_video",
    (0x02, 0x09): "camera.set_liveview_source",
    (0x02, 0x10): "camera.set_work_mode",   # liveview vs playback
    (0x02, 0xAB): "camera.set_video_coding",
    (0x02, 0xB3): "camera.request_i_frame",
    (0x02, 0x4C): "camera.set_video_out_para",  # liveview resolution/format
    (0x02, 0x4D): "camera.get_video_out_para",
    (0x01, 0x02): "action.virtual_rc_joystick", # virtual stick (flight control)
    (0x04, 0x01): "gimbal.motion_control",
    (0x04, 0x0A): "gimbal.set_angle",
    (0x04, 0x0C): "gimbal.ctrl_speed",
    (0x04, 0x14): "gimbal.set_angle_ex",
    (0x04, 0x44): "gimbal.work_mode",
    (0x06, 0x01): "rc.get_channel_params",
    (0x06, 0x05): "rc.stick_push",
    (0x06, 0x1E): "rc.battery_push",
    (0x06, 0x2F): "rc.pair_frequency",
    (0x06, 0x51): "rc.button_push",
    (0x06, 0x77): "rc.link_keepalive",      # RC-N1-era link layer, not in uav registry
    (0x06, 0xF1): "rc.app_to_pc_control",
    (0x07, 0x18): "wifi.set_country_code",
    (0x08, 0x32): "dm368.sdr_data_report",
    (0x08, 0x41): "dm368.decode_capability",
    (0x08, 0x42): "dm368.decode_framerate",
    (0x08, 0x69): "dm368.liveview_bandwidth",
    (0x08, 0x78): "dm368.start_live_streaming",
    (0x10, 0x58): "test.autotest_enter",    # opens the service-mode window (FreeFCC)
    (0x03, 0x43): "fc.osd_common_push",    # DataOsdGetPushCommon (WM160-era)
    (0x03, 0x44): "fc.osd_home_push",
    (0x09, 0x01): "osd.common_push",       # OSD-set alias of the same model
    (0x09, 0x02): "osd.home_push",
    (0x03, 0xDF): "fc.assistant_unlock",
    (0x03, 0xF7): "fc.param_info",
    (0x03, 0xF8): "fc.param_read",
    (0x03, 0xF9): "fc.write_param",
    (0x03, 0xFA): "fc.param_reset",
    (0x51, 0x02): "wlm.link_mode_switch",
    (0x51, 0x1A): "wlm.service_mode_switch",
    (0x0D, 0x02): "battery.dynamic_push",    # voltage/current/capacity/temp
    (0x0D, 0x03): "battery.cell_push",
    (0x03, 0x2A): "fc.function_control",     # takeoff/land/rth/motors
    (0x03, 0x2D): "fc.set_limits",           # max altitude/distance
    (0x03, 0x2E): "fc.get_limits",
    (0x03, 0x31): "fc.set_home_point",       # [type][lat f64 rad][lon f64 rad][ivl]
    (0x03, 0x3B): "fc.set_failsafe",
    (0x03, 0x3C): "fc.get_failsafe",
    (0x03, 0x62): "fc.set_active_result",
    (0x03, 0x80): "fc.ground_station_onoff", # virtual-stick enable gate
    (0x03, 0x8E): "fc.joystick_float",       # [flag][roll,pitch,yaw,thr f32]
    (0x03, 0xFE): "fc.motor_force_disable",
    (0x01, 0x02): "special.virtual_joystick",
    (0x01, 0x0A): "special.stick_tlv",       # RC-emulation stick path
    (0x19, 0x40): "fc.lock_control",
    (0x19, 0x41): "fc.preempt_control",
    (0x19, 0x46): "fc.task_occupy_control",
    (0x49, 0x80): "sdk.control_auth",        # 1=take / 0=release
    (0x00, 0x32): "common.activate_device",
    (0x04, 0x08): "gimbal.auto_calibration",
    (0x06, 0x03): "rc.calibrate_channels",
    (0x0A, 0x27): "vision.common_ctrl",
}

MAGIC = 0x55
CRC8_SEED = 0x77
CRC16_SEED = 0x3692


def _gen_table(poly: int, width: int) -> list[int]:
    mask = (1 << width) - 1
    table = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = ((c >> 1) ^ poly) if (c & 1) else (c >> 1)
            c &= mask
        table.append(c)
    return table


_CRC8_TAB = _gen_table(0x8C, 8)      # refl(0x31)
_CRC16_TAB = _gen_table(0x8408, 16)  # refl(0x1021)


def crc8(data: bytes, seed: int = CRC8_SEED) -> int:
    c = seed
    for b in data:
        c = _CRC8_TAB[(c ^ b) & 0xFF]
    return c & 0xFF


def crc16(data: bytes, seed: int = CRC16_SEED) -> int:
    c = seed
    for b in data:
        c = ((c >> 8) ^ _CRC16_TAB[(c ^ b) & 0xFF]) & 0xFFFF
    return c & 0xFFFF


# DUML "SIMPLE" cipher (cmd_type 0x43) — keystream-XOR keyed by the frame's own
# seq. Static 21-byte key + NUL, reversed from libGroudStation.so
# (DUML_ENCRYPTION.md; encrypt == decrypt). Region = frame[9:len-2].
_SIMPLE_KEY = bytes.fromhex("784f2433282d3240236c642a766941517e69784645") + b"\x00"


def simple_filter(buf: bytes, seq: int) -> bytes:
    out = bytearray(len(buf))
    keyidx = 1
    slo, shi = seq & 0xFF, (seq >> 8) & 0xFF
    for i, b in enumerate(buf):
        if keyidx >= 22:
            keyidx = 0
        out[i] = (_SIMPLE_KEY[keyidx] ^ b ^ (shi if (i & 1) else slo)) & 0xFF
        keyidx = ((i + 1) & 0xF) ^ (keyidx + 1)
    return bytes(out)


def encrypt_frame(frame: bytes) -> bytes:
    """Plaintext DUML frame (cmd_type 0x40, valid CRCs) → SIMPLE 0x43 frame."""
    f = bytearray(frame)
    seq = f[6] | (f[7] << 8)
    f[9:len(f) - 2] = simple_filter(bytes(f[9:len(f) - 2]), seq)
    f[8] |= 0x03
    crc = crc16(bytes(f[:len(f) - 2]))
    f[-2], f[-1] = crc & 0xFF, (crc >> 8) & 0xFF
    return bytes(f)


def decrypt_frame(frame: bytes) -> bytes:
    """SIMPLE-encrypted DUML frame → plaintext (valid CRCs restored)."""
    f = bytearray(frame)
    seq = f[6] | (f[7] << 8)
    f[9:len(f) - 2] = simple_filter(bytes(f[9:len(f) - 2]), seq)
    f[8] &= ~0x07
    crc = crc16(bytes(f[:len(f) - 2]))
    f[-2], f[-1] = crc & 0xFF, (crc >> 8) & 0xFF
    return bytes(f)


def param_hash(name: str | bytes) -> int:
    """DJI FC param-name hash (hashFromString): h=(b+(h<<8)) mod (2^32-5).
    Used by 0x03/0xF7/0xF8/0xF9/0xFA — the KeyValue config transport."""
    b = name.encode("gbk", errors="replace") if isinstance(name, str) else name
    h = 0
    for byte in b:
        h = (byte + (h << 8)) % 0xFFFFFFFB
    return h


def wrap(payload: bytes, typ: int = TYPE_DUML) -> bytes:
    """Wrap a DUML frame into a composite/RCLink unit for the AOA bulk pipe."""
    return bytes([SOF0, SOF1]) + struct.pack("<H", typ) + struct.pack("<I", len(payload)) + payload


class DumlError(Exception):
    pass


@dataclass
class DumlPacket:
    sender: int
    receiver: int
    cmd_set: int
    cmd_id: int
    payload: bytes = b""
    seq: int = 0
    cmd_type: int = 0x00      # 0x40 = ACK required
    version: int = 1          # real DJI frames use version=1

    def encode(self) -> bytes:
        body = bytes([
            self.sender & 0xFF, self.receiver & 0xFF,
            self.seq & 0xFF, (self.seq >> 8) & 0xFF,
            self.cmd_type & 0xFF, self.cmd_set & 0xFF, self.cmd_id & 0xFF,
        ]) + bytes(self.payload)
        total = 1 + 2 + 1 + len(body) + 2
        if total > 0x3FF:
            raise DumlError(f"frame too long: {total}")
        len_ver = (total & 0x3FF) | ((self.version & 0x3F) << 10)
        header = bytes([MAGIC, len_ver & 0xFF, (len_ver >> 8) & 0xFF])
        header += bytes([crc8(header)])
        frame = header + body
        return frame + crc16(frame).to_bytes(2, "little")

    @classmethod
    def decode(cls, frame: bytes) -> "DumlPacket":
        if len(frame) < 13:
            raise DumlError(f"short frame: {len(frame)}")
        if frame[0] != MAGIC:
            raise DumlError(f"bad magic: {frame[0]:#04x}")
        len_ver = frame[1] | (frame[2] << 8)
        total = len_ver & 0x3FF
        if crc8(frame[0:3]) != frame[3]:
            raise DumlError("CRC8 mismatch")
        if total != len(frame):
            raise DumlError(f"len {total} != actual {len(frame)}")
        if crc16(frame[:-2]) != int.from_bytes(frame[-2:], "little"):
            raise DumlError("CRC16 mismatch")
        return cls(sender=frame[4], receiver=frame[5],
                   seq=frame[6] | (frame[7] << 8),
                   cmd_type=frame[8], cmd_set=frame[9], cmd_id=frame[10],
                   payload=frame[11:-2], version=(len_ver >> 10) & 0x3F)

    def __str__(self) -> str:
        return (f"DUML seq={self.seq} {self.sender:#04x}->{self.receiver:#04x} "
                f"set={self.cmd_set:#04x} id={self.cmd_id:#04x} "
                f"type={self.cmd_type:#04x} len={len(self.payload)} "
                f"data={self.payload[:64].hex()}")


class CompositeDemux:
    """Stream -> composite units. Callbacks: on_duml(payload), on_video(payload),
    on_unit(typ, payload) for every unit including unrouted types."""

    def __init__(self, on_duml=None, on_video=None, on_unit=None, max_unit=0x200000):
        self.buf = bytearray()
        self.on_duml, self.on_video, self.on_unit = on_duml, on_video, on_unit
        self.max_unit = max_unit
        self.units = 0
        self.type_counts: dict[int, int] = {}

    def feed(self, data: bytes) -> None:
        self.buf += data
        while True:
            if len(self.buf) < 8:
                return
            if not (self.buf[0] == SOF0 and self.buf[1] == SOF1):
                idx = self.buf.find(bytes([SOF0, SOF1]), 1)
                if idx < 0:
                    keep = 1 if self.buf[-1] == SOF0 else 0
                    del self.buf[:len(self.buf) - keep]
                    return
                del self.buf[:idx]
                continue
            typ = struct.unpack_from("<H", self.buf, 2)[0]
            length = struct.unpack_from("<I", self.buf, 4)[0]
            if length > self.max_unit:
                del self.buf[0]
                continue
            total = 8 + length
            if len(self.buf) < total:
                return
            payload = bytes(self.buf[8:total])
            del self.buf[:total]
            self.units += 1
            self.type_counts[typ] = self.type_counts.get(typ, 0) + 1
            if self.on_unit:
                self.on_unit(typ, payload)
            if typ in (TYPE_DUML, TYPE_DUML_AUX) and self.on_duml:
                self.on_duml(payload)
            elif typ in TYPE_VIDEO and self.on_video:
                self.on_video(payload)


# ---- 16-byte liveview packet header (magic 0x6d, WM160 format) ----
LV_MAGIC = 0x6D
LV_HDR_LEN = 16


def lv_parse(b: bytes) -> dict | None:
    """Parse a liveview unit payload ([16B hdr][slice]). None if not liveview."""
    if len(b) < LV_HDR_LEN or b[0] != LV_MAGIC:
        return None
    f1 = b[1]
    w6 = struct.unpack_from("<I", b, 6)[0]
    w11 = struct.unpack_from("<I", b, 0x0B)[0]
    return {
        "is_i": f1 & 1, "video_chan": (f1 >> 1) & 3, "video_fmt": (f1 >> 3) & 3,
        "frm_idx": struct.unpack_from("<I", b, 2)[0],
        "pkt_len": w6 & 0xFFFFF, "ssfn": (w6 >> 20) & 0xFFF,
        "fps": b[0x0A],
        "frm_end": w11 & 1, "slice_idx": (w11 >> 1) & 0x1F,
        "pkt_is_last": (w11 >> 8) & 1, "pkt_idx": (w11 >> 9) & 0x7F,
        "frm_len_kb": (w11 >> 16) & 0x3FF,
    }


def lv_slice(payload: bytes) -> bytes | None:
    """Extract the elementary-stream slice bytes from a video unit payload."""
    h = lv_parse(payload)
    if h is None or h["pkt_len"] == 0:
        return None
    return payload[LV_HDR_LEN:LV_HDR_LEN + h["pkt_len"]]


# OSD common push arrives as 0x03/0x43 (FLYC) or 0x09/0x01 (OSD-set alias);
# home push as 0x03/0x44 or 0x09/0x02. Offsets from DataOsdGetPushCommon
# (OSD_TELEMETRY_RESEARCH_2026.md — verified against app smali + MSDK jar).
OSD_COMMON_IDS = {(0x03, 0x43), (0x09, 0x01)}
OSD_HOME_IDS = {(0x03, 0x44), (0x09, 0x02)}


def osd_parse(p: bytes) -> dict | None:
    """Decode a DataOsdGetPushCommon payload. None if too short to be one."""
    if len(p) < 0x30:
        return None
    status = struct.unpack_from("<I", p, 0x20)[0]
    lon, lat = struct.unpack_from("<dd", p, 0)
    return {
        "lon": math.degrees(lon), "lat": math.degrees(lat),
        "height_m": struct.unpack_from("<h", p, 0x10)[0] * 0.1,
        "v_speed": (struct.unpack_from("<hhh", p, 0x12)),
        "att": (struct.unpack_from("<hhh", p, 0x18)),
        "flyc_state": p[0x1e] & 0x7F,
        "rc_state": (p[0x1e] >> 7) & 1,
        # satellites = u8@0x24 and VPS = s8@0x29 — TELEMETRY_TRUTH re-verified
        # DataBase.get(0x24/0x29, size=1) bytecode; wider reads were wrong.
        "gps_sats": p[0x24],
        "gps_level": (status >> 0x12) & 0xF,
        "motor_cause": p[0x26] & 0x7F,
        "battery_pct": struct.unpack_from("<I", p, 0x28)[0] & 0xFF,
        "vps_m": struct.unpack_from("<b", p, 0x29)[0] * 0.1,
        "gohome_status": (status >> 5) & 0x7,
        "motor_up": (status >> 3) & 1, "in_air": bool(status & 0x0E),
        "fly_time_s": struct.unpack_from("<H", p, 0x2A)[0] // 10,
        "drone_type": p[0x30] if len(p) > 0x30 else None,
    }


# FLYC_STATE codes (byte@0x1e & 0x7f) — NON-DENSE enum from the MSDK jar static
# initializer; codes 18,20,21,22,34,40,42,44,45,47,48 are unused/unknown.
FLYC_STATES = {
    0: "Manual", 1: "Atti", 2: "Atti_CL", 3: "Atti_Hover", 4: "Hover",
    5: "GPS_Blake", 6: "GPS_Atti", 7: "GPS_CL", 8: "GPS_HomeLock",
    9: "GPS_HotPoint", 10: "AssistedTakeoff", 11: "AutoTakeoff",
    12: "AutoLanding", 13: "AttiLanding", 14: "NaviGo", 15: "GoHome",
    16: "ClickGo", 17: "Joystick", 19: "Cinematic", 23: "Atti_Limited",
    24: "Draw", 25: "MissionFollow", 26: "Tracking", 27: "Pointing",
    28: "PANO", 29: "Farming", 30: "FPV", 31: "SPORT", 32: "NOVICE",
    33: "FORCE_LANDING", 35: "TERRAIN_TRACKING", 36: "PALM_CONTROL",
    37: "QUICK_SHOT", 38: "TRIPOD_GPS", 39: "TRACK_HEADLOCK",
    41: "ENGINE_START", 43: "DETOUR", 46: "TIME_LAPSE", 49: "OMNI_MOVING",
    50: "POI_WITH_VISION", 51: "SMART_TRACK", 52: "LOST_POWER_FORCE_LANDING",
    100: "OTHER",
}

BATTERY_IDS = {(0x0D, 0x02)}

# ---------------- legacy litchis file transport (DEX-confirmed, FileSendPack) ----
# 10-byte inner header prepended to every file payload carried inside a DUML
# COMMON(0x00)/0x26 RequestFile frame, APP(0x02) -> CAMERA(0x01).
LITCHIS_CMD_LIST, LITCHIS_CMD_FILE = 0, 1
LITCHIS_T_REQUEST, LITCHIS_T_DATA, LITCHIS_T_ACK = 0, 1, 2
LITCHIS_T_PUSH, LITCHIS_T_ABORT = 3, 4
# SubType: which rendition of a file index
LITCHIS_SUB_ORG, LITCHIS_SUB_THM, LITCHIS_SUB_SCR = 0, 1, 2


def litchis_list(start_index: int = 0, count: int = 200,
                 storage: int = 1, subtype: int = LITCHIS_SUB_ORG,
                 session: int = 1) -> bytes:
    """DataRequestList.doPack bytes: 10B FileSendPack header + 7B body.
    storage occupies the top 2 bits of startIndex byte 3 (EXTERN1=1)."""
    body = struct.pack("<IHB", (start_index & 0x3FFFFFFF) | (storage << 30),
                       count, subtype)
    hdr = bytes([0x0A | (1 << 6), (LITCHIS_CMD_LIST << 5) | LITCHIS_T_REQUEST])
    hdr += struct.pack("<HHI", 10 + len(body), session, 0)
    return hdr + body


def litchis_file(file_index: int, sub_index: int = 0,
                 subtype: int = LITCHIS_SUB_ORG, grade: int = 0,
                 offset: int = 0, length: int = 0xFFFFFFFF,
                 session: int = 1) -> bytes:
    """DataRequestFile.doPack bytes: 10B header + 16B body."""
    body = struct.pack("<IHBBII", file_index, sub_index, subtype, grade,
                       offset, length)
    hdr = bytes([0x0A | (1 << 6), (LITCHIS_CMD_FILE << 5) | LITCHIS_T_REQUEST])
    hdr += struct.pack("<HHI", 10 + len(body), session, 0)
    return hdr + body


def litchis_ack(next_offset: int, ranges: list[tuple[int, int]],
                session: int = 1) -> bytes:
    """DataRequestAck: u32 next + u8 count + count x (u32 offset, u32 length)."""
    body = struct.pack("<IB", next_offset, len(ranges))
    for off, ln in ranges:
        body += struct.pack("<II", off, ln)
    hdr = bytes([0x0A | (1 << 6), (LITCHIS_CMD_FILE << 5) | LITCHIS_T_ACK])
    hdr += struct.pack("<HHI", 10 + len(body), session, 0)
    return hdr + body


def litchis_recv_parse(data: bytes) -> dict | None:
    """Parse one incoming FileRecvPack payload (the bytes inside DUML 0x00/0x27).
    RX asymmetry is DEX-confirmed: word@2 top nibble = packet index,
    low 12 bits = length."""
    if len(data) < 10 or data[0] & 0x3F != 0x0A:
        return None
    word = struct.unpack_from("<H", data, 2)[0]
    return {
        "version": data[0] >> 6,
        "cmd": data[1] >> 5, "type": data[1] & 0x1F,
        "pkt_idx": word >> 12, "length": word & 0xFFF,
        "session": struct.unpack_from("<H", data, 4)[0],
        "offset": struct.unpack_from("<I", data, 6)[0],
        "body": data[10:],
    }


def battery_parse(p: bytes) -> dict | None:
    """DataSmartBatteryGetPushDynamicData (0x0D/0x02), verified byte-perfect."""
    if len(p) < 0x15:
        return None
    return {
        "voltage_mv": struct.unpack_from("<I", p, 0x01)[0],
        "current_ma": struct.unpack_from("<i", p, 0x05)[0],   # neg = discharge
        "cap_full_mah": struct.unpack_from("<I", p, 0x09)[0],
        "cap_remain_mah": struct.unpack_from("<I", p, 0x0D)[0],
        "temp_c": struct.unpack_from("<h", p, 0x11)[0] * 0.1,
        "cells": p[0x13], "pct": p[0x14],
    }
