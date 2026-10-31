"""Sleep Number 360 foundation framing verified against an LPM-9000B.

The RF coordinator address is zero. The 7eff SMAC prefix and IEEE 802.15.4
FCS replace the BLE transport's 1616 prefix and running-sum checksum.
"""

from dataclasses import dataclass


def crc154(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0x8408 if crc & 1 else 0)
    return crc


@dataclass(frozen=True)
class Frame:
    node: int
    source: int
    destination: int
    inner_node: int
    origin: int
    command: int
    qualifier: int
    payload: bytes


def decode(data: bytes):
    if len(data) < 14 or data[:2] != b"\x7e\xff":
        return None
    if crc154(data[:-2]) != int.from_bytes(data[-2:], "little"):
        return None
    body = data[2:-2]
    if body[9] & 15 != len(body) - 10:
        return None
    return Frame(body[0], int.from_bytes(body[1:3], "big"),
                 int.from_bytes(body[3:5], "big"), body[5],
                 int.from_bytes(body[6:8], "big"), body[8], body[9] >> 4, body[10:])


def encode(target: int, command: int, qualifier=0, payload=b"", node=0x42) -> bytes:
    if type(target) is not int or not 0 < target < 0xFFFF:
        raise ValueError("A specific base address is required")
    if not 0 <= command < 0x80 or not 0 <= qualifier <= 15 or len(payload) > 15:
        raise ValueError("Invalid RF request")
    body = (bytes([node, 0, 0]) + target.to_bytes(2, "big")
            + bytes([node, 0, 0, command, (qualifier << 4) | len(payload)]) + payload)
    data = b"\x7e\xff" + body
    return data + crc154(data).to_bytes(2, "little")


def matches(frame: Frame, target: int, command: int, qualifier=0, writing=False):
    if (frame.source != target or frame.destination != 0 or frame.origin != target
            or frame.node & 0xF0 != 0x40 or frame.inner_node & 0xF0 != 0x40
            or frame.command != command | 0x80):
        return False
    # Errors may replace the qualifier with E/F. Empty write ACKs reset it to 0.
    return (frame.qualifier == qualifier or frame.qualifier in (14, 15)
            or writing and frame.qualifier == 0 and not frame.payload)


def head_payload(position):
    if type(position) is not int or not 0 <= position <= 100:
        raise ValueError("Head position must be an integer from 0 to 100")
    return bytes([position, 0]) + b"\xff" * 10


def foundation_status(data: bytes):
    if len(data) != 15 or not 0 <= data[1] <= 100:
        raise ValueError("Incomplete or invalid foundation status")
    return {"configured": bool(data[0] & 64), "homing": bool(data[0] & 32),
            "moving": bool(data[0] & 1), "head": data[1], "other_head": data[2],
            "actuator_status": list(data[9:13]),
            "fault": bool((data[9] | data[10]) & 0xFE),
            "right_preset": data[14] & 15, "left_preset": data[14] >> 4}


def system_status(data: bytes):
    if len(data) < 7:
        raise ValueError("Incomplete foundation configuration")
    return {"configuration": data[0], "right_intensity": data[1],
            "left_intensity": data[2], "has_light": bool(data[3] & 16),
            "has_foot": bool(data[3] & 4), "hardware_revision": data[4] & 15}


def outlet_status(data: bytes):
    if len(data) < 3:
        raise ValueError("Incomplete light state")
    return {"on": bool(data[0]), "timer": int.from_bytes(data[1:3], "little")}


def pinch_clear(data: bytes):
    # This app controls the head only; absent foot sensors are irrelevant.
    return len(data) == 5 and not (data[0] & 0xCC or data[3] or data[4])
