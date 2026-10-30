# SPDX-License-Identifier: MIT

"""CoE (CANopen over EtherCAT) object dictionary access through the EtherCAT master.

The master hands over the raw SDO-information answers (ETG.1000.6):
  object description  index(2) data type(2) max subindex(1) object code(1) name
  entry description   index(2) subindex(1) value info(1) data type(2) bit length(2) access(2) name
and SDO data as little-endian bytes. Verified on ctrlX I/O modules (tests/data/x3-coe.json).
"""

import re
import struct

DATA_TYPES = {0x01: "BOOL", 0x02: "SINT", 0x03: "INT", 0x04: "DINT", 0x05: "USINT", 0x06: "UINT", 0x07: "UDINT",
              0x08: "REAL", 0x09: "VISIBLE_STRING", 0x0A: "OCTET_STRING", 0x0B: "UNICODE_STRING", 0x11: "LREAL",
              0x15: "LINT", 0x1B: "ULINT", 0x1E: "BYTE", 0x1F: "WORD", 0x20: "DWORD", 0x2D: "BITARR8",
              0x2E: "BITARR16", 0x2F: "BITARR32",
              **{0x30 + n: f"BIT{n + 1}" for n in range(8)}}
OBJECT_CODES = {7: "VAR", 8: "ARRAY", 9: "RECORD"}
SIGNED = {"SINT", "INT", "DINT", "LINT"}
ACCESS_BITS = (("read_preop", 0x01), ("read_safeop", 0x02), ("read_op", 0x04), ("write_preop", 0x08),
               ("write_safeop", 0x10), ("write_op", 0x20), ("rx_mappable", 0x40), ("tx_mappable", 0x80),
               ("backup", 0x100), ("settings", 0x200))


class CoeError(ValueError):
    pass


def _name(data: bytes) -> str:
    return bytes(data).split(b"\0", 1)[0].decode("latin-1")


def object_description(data) -> dict:
    data = bytes(data)
    if len(data) < 6:
        raise CoeError("object description too short")
    index, dtype, max_sub, code = struct.unpack_from("<HHBB", data, 0)
    return {"index": index, "data_type": DATA_TYPES.get(dtype, f"{dtype:#06x}"), "max_subindex": max_sub,
            "object_code": OBJECT_CODES.get(code, str(code)), "name": _name(data[6:])}


def entry_description(data) -> dict:
    data = bytes(data)
    if len(data) < 10:
        raise CoeError("entry description too short")
    index, sub, _value_info, dtype, bits, access = struct.unpack_from("<HBBHHH", data, 0)
    flags = {name: bool(access & bit) for name, bit in ACCESS_BITS}
    return {"index": index, "subindex": sub, "data_type": DATA_TYPES.get(dtype, f"{dtype:#06x}"), "bits": bits,
            "access": flags, "readable": bool(access & 0x07), "writable": bool(access & 0x38),
            "name": _name(data[10:])}


def decode(data, data_type: str, bits: int):
    """A value as Python (int, float, bool, str); bytes it can't interpret as hex."""
    data = bytes(data)
    if data_type == "BOOL" or (data_type == "BIT1"):
        return bool(data[0] & 1) if data else False
    if data_type.startswith("BIT") and data_type[3:].isdigit():
        return data[0] & ((1 << int(data_type[3:])) - 1) if data else 0
    if data_type in ("REAL", "LREAL"):
        size = 4 if data_type == "REAL" else 8
        if len(data) >= size:
            return struct.unpack("<f" if size == 4 else "<d", data[:size])[0]
    if data_type == "VISIBLE_STRING":
        return data.split(b"\0", 1)[0].decode("latin-1")
    if data_type in SIGNED or data_type in ("USINT", "UINT", "UDINT", "ULINT", "BYTE", "WORD", "DWORD",
                                             "BITARR8", "BITARR16", "BITARR32"):
        size = max(1, (bits + 7) // 8)
        if len(data) >= size:
            return int.from_bytes(data[:size], "little", signed=data_type in SIGNED)
    return data.hex().upper()


def show(data, data_type: str = None, bits: int = 0) -> str:
    """A value for people: decoded, numbers with their hex most significant byte first ("1000 (0x000003E8)").
    The data stays little-endian in the device and the ENI; without a data type 1/2/4/8 bytes count as unsigned."""
    data = bytes(data)
    if data_type:
        value = decode(data, data_type, bits)
        if isinstance(value, bool):
            return "TRUE" if value else "FALSE"
        if isinstance(value, float):
            return f"{value:g}"
        if isinstance(value, int):
            size = max(1, (bits + 7) // 8) if bits else len(data)
            return f"{value} (0x{int.from_bytes(data[:size], 'little'):0{size * 2}X})"
        if data_type == "VISIBLE_STRING":
            return f'"{value}"'
    if len(data) in (1, 2, 4, 8):
        return f"{int.from_bytes(data, 'little')} (0x{int.from_bytes(data, 'little'):0{len(data) * 2}X})"
    return " ".join(f"{b:02X}" for b in data) or "-"


def matches(query: str, index: int, text: str) -> bool:
    """Object filter: every word is part of "#x8000 name", or names the index as hex (8000, x8000, 0x8000, #x8000)."""
    hay = f"#x{index:04x} 0x{index:04x} {text}".lower()
    for w in query.lower().split():
        m = re.fullmatch(r"(?:0x|#x|x)?([0-9a-f]{1,4})", w)
        if not (w in hay or (m and int(m.group(1), 16) == index)):
            return False
    return True


def encode(value, data_type: str, bits: int) -> bytes:
    """Bytes for an SDO download; CoeError if the value doesn't fit. Strings with "0x" or a {"hex": ...}
    value are taken as raw bytes."""
    if isinstance(value, dict) and "hex" in value:
        return _hex(value["hex"])
    if isinstance(value, str) and value.lower().startswith("0x") and data_type not in ("VISIBLE_STRING",):
        if data_type in ("OCTET_STRING",):
            return _hex(value[2:])
        value = int(value, 16)
    if data_type == "BOOL" or data_type == "BIT1":
        if isinstance(value, str):
            value = value.strip().lower() in ("1", "true", "on")
        return bytes([1 if value else 0])
    if data_type in ("REAL", "LREAL"):
        try:
            return struct.pack("<f" if data_type == "REAL" else "<d", float(value))
        except (TypeError, ValueError) as e:
            raise CoeError(f"{value!r} is not a number") from e
    if data_type == "VISIBLE_STRING":
        b = str(value).encode("latin-1", "replace")
        if bits and len(b) > bits // 8:
            raise CoeError(f"text longer than {bits // 8} characters")
        return b
    if data_type == "OCTET_STRING":
        return _hex(str(value))
    size = max(1, (bits + 7) // 8)
    try:
        number = int(str(value), 0) if isinstance(value, str) else int(value)
    except (TypeError, ValueError) as e:
        raise CoeError(f"{value!r} is not a whole number") from e
    if isinstance(value, float) and value != number:
        raise CoeError(f"{value!r} is not a whole number")
    signed = data_type in SIGNED
    lo, hi = (-(1 << (bits - 1)), (1 << (bits - 1)) - 1) if signed else (0, (1 << bits) - 1)
    if not lo <= number <= hi:
        raise CoeError(f"{number} is outside {lo}..{hi} ({data_type})")
    return number.to_bytes(size, "little", signed=signed)


def _hex(text: str) -> bytes:
    t = str(text).replace(" ", "")
    try:
        return bytes.fromhex(t)
    except ValueError as e:
        raise CoeError(f"{text!r} is not hex bytes") from e


# --- process data (realtime_data variables, typed as in the ENI) --------------------------------------

PD_RANGES = {"SINT": (-128, 127), "INT": (-32768, 32767), "DINT": (-(1 << 31), (1 << 31) - 1),
             "LINT": (-(1 << 63), (1 << 63) - 1), "USINT": (0, 255), "BYTE": (0, 255), "UINT": (0, 65535),
             "WORD": (0, 65535), "UDINT": (0, (1 << 32) - 1), "DWORD": (0, (1 << 32) - 1),
             "ULINT": (0, (1 << 64) - 1), "LWORD": (0, (1 << 64) - 1)}


def process_value(value, eni_type: str, bits: int):
    """An output value checked against the variable's ENI type, as the Data Layer node takes it."""
    t = eni_type.upper()
    if t in ("BIT", "BOOL"):
        if isinstance(value, str):
            value = value.strip().lower() in ("1", "true", "on")
        if not isinstance(value, (bool, int)):
            raise CoeError("a bit needs true or false")
        return bool(value)
    if t in ("REAL", "LREAL"):
        try:
            return float(value)
        except (TypeError, ValueError) as e:
            raise CoeError(f"{value!r} is not a number") from e
    if t in PD_RANGES:
        try:
            number = int(str(value), 0) if isinstance(value, str) else int(value)
        except (TypeError, ValueError) as e:
            raise CoeError(f"{value!r} is not a whole number") from e
        lo, hi = PD_RANGES[t]
        if not lo <= number <= hi:
            raise CoeError(f"{number} is outside {lo}..{hi} ({t})")
        return number
    if t.startswith("ARRAY") and t.endswith("OF BYTE"):
        n = bits // 8
        if isinstance(value, str):
            value = list(_hex(value))
        if not isinstance(value, list) or len(value) > n or not all(isinstance(x, int) and 0 <= x <= 255 for x in value):
            raise CoeError(f"up to {n} bytes (0..255) or hex text are needed")
        return value + [0] * (n - len(value))
    raise CoeError(f"writing {eni_type} is not supported")
