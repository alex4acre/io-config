# SPDX-License-Identifier: MIT

"""SoE (Servo drive profile over EtherCAT, SERCOS parameters): IDNs, attributes, values, dictionaries.

An IDN is a 16-bit number: bit 15 S (standard) or P (product specific), bits 12..14 the parameter set,
bits 0..11 the number - 35017 = 0x88C9 is P-0-2249. Its attribute (SERCOS, IEC 61800-7-204) says how the
value is stored: length (2/4/8 bytes, or a list of 1/2/4/8-byte elements), display type, decimal places and
in which EtherCAT state it is write-protected. Lists start with 2 bytes actual length and 2 bytes maximum
length (in bytes), then the elements.

Device dictionaries come as EtherCATDict XML files next to the ESI (Profile/DictionaryFile). Not yet
checked against a real drive: an unconfigured drive stays in Init and SoE needs PreOp.
"""

import re
import struct

from app.xmlsafe import parse_xml

LENGTHS = {0b001: (2, False), 0b010: (4, False), 0b011: (8, False),
           0b100: (1, True), 0b101: (2, True), 0b110: (4, True), 0b111: (8, True)}
DISPLAY = {0b000: "binary", 0b001: "unsigned", 0b010: "signed", 0b011: "hex", 0b100: "text", 0b101: "idn",
           0b110: "float", 0b111: "signed"}
IDN_RE = re.compile(r"\s*([SP])-([0-7])-(\d{1,4})(?:\.\d+\.\d+)?\s*$", re.IGNORECASE)


class SoeError(ValueError):
    pass


def idn_text(idn: int) -> str:
    return f'{"P" if idn & 0x8000 else "S"}-{(idn >> 12) & 7}-{idn & 0xFFF:04d}'


def parse_idn(value) -> int:
    """S-0-0134 / P-0-0201 / a number -> the 16-bit IDN."""
    if isinstance(value, int) and not isinstance(value, bool):
        if not 0 <= value <= 0xFFFF:
            raise SoeError(f"IDN {value} out of range")
        return value
    m = IDN_RE.match(str(value))
    if m:
        number = int(m.group(3))
        if number > 4095:
            raise SoeError(f"IDN number {number} out of range")
        return (0x8000 if m.group(1).upper() == "P" else 0) | int(m.group(2)) << 12 | number
    try:
        return parse_idn(int(str(value), 0))
    except ValueError as e:
        raise SoeError(f"{value!r} is not an IDN (S-0-0134, P-0-0201 or a number)") from e


def attribute(value: int) -> dict:
    size, is_list = LENGTHS.get((value >> 16) & 7, (0, False))
    return {
        "raw": value,
        "size": size,                     # element size in bytes (0: unknown)
        "list": is_list,
        "command": bool(value & 0x80000),  # a procedure command, not a parameter
        "display": DISPLAY[(value >> 20) & 7],
        "decimals": (value >> 24) & 0xF,
        "write_protected": {"PreOp": bool(value & 0x10000000), "SafeOp": bool(value & 0x20000000),
                            "Op": bool(value & 0x40000000)},
    }


def _element(raw: bytes, attr: dict):
    if attr["display"] == "float":
        return struct.unpack("<f" if len(raw) == 4 else "<d", raw)[0] if len(raw) in (4, 8) else raw.hex().upper()
    if attr["display"] == "idn":
        return idn_text(int.from_bytes(raw[:2], "little"))
    signed = attr["display"] == "signed"
    number = int.from_bytes(raw, "little", signed=signed)
    if attr["decimals"] and attr["display"] in ("signed", "unsigned"):
        return number / 10 ** attr["decimals"]
    if attr["display"] in ("hex", "binary"):
        return f"0x{number:0{len(raw) * 2}X}"
    return number


def decode(data, attr: dict):
    data = bytes(data)
    if attr["list"]:
        if len(data) < 4:
            return []
        length = int.from_bytes(data[0:2], "little")
        body = data[4:4 + length]
        if attr["display"] == "text":
            return body.decode("latin-1")
        size = attr["size"] or 1
        return [_element(body[i:i + size], attr) for i in range(0, len(body) - size + 1, size)]
    if attr["display"] == "text":
        return data.decode("latin-1").rstrip("\0")
    return _element(data[:attr["size"] or len(data)], attr)


def _encode_element(value, attr: dict) -> bytes:
    size = attr["size"]
    if attr["display"] == "float":
        return struct.pack("<f" if size == 4 else "<d", float(value))
    if attr["display"] == "idn":
        return parse_idn(value).to_bytes(2, "little") + b"\0" * (size - 2)
    if isinstance(value, str):
        value = value.strip()
        number = float(value) if "." in value and not value.lower().startswith("0x") else int(value, 0)
    else:
        number = value
    if attr["decimals"] and attr["display"] in ("signed", "unsigned"):
        scaled = float(number) * 10 ** attr["decimals"]
        if abs(scaled - round(scaled)) > 1e-6:
            raise SoeError(f"{value!r} has more than {attr['decimals']} decimal places")
        number = round(scaled)
    if isinstance(number, float):
        if number != int(number):
            raise SoeError(f"{value!r} needs {attr['decimals']} decimal places at most")
        number = int(number)
    signed = attr["display"] == "signed"
    bits = size * 8
    lo, hi = (-(1 << (bits - 1)), (1 << (bits - 1)) - 1) if signed else (0, (1 << bits) - 1)
    if not lo <= number <= hi:
        raise SoeError(f"{value} is outside the parameter's range")
    return number.to_bytes(size, "little", signed=signed)


def encode(value, attr: dict) -> bytes:
    """Bytes to write for a value (a list for list parameters; {"hex": ...} for raw bytes)."""
    if isinstance(value, dict) and "hex" in value:
        try:
            return bytes.fromhex(str(value["hex"]).replace(" ", ""))
        except ValueError as e:
            raise SoeError("not hex bytes") from e
    if attr["size"] == 0:
        raise SoeError("the parameter's length is unknown")
    try:
        if attr["list"]:
            if attr["display"] == "text":
                body = str(value).encode("latin-1")
            else:
                items = value if isinstance(value, list) else [x for x in str(value).replace(",", " ").split() if x]
                body = b"".join(_encode_element(x, attr) for x in items)
            return len(body).to_bytes(2, "little") + len(body).to_bytes(2, "little") + body
        if attr["display"] == "text":
            return str(value).encode("latin-1")
        return _encode_element(value, attr)
    except (TypeError, ValueError, struct.error) as e:
        if isinstance(e, SoeError):
            raise
        raise SoeError(f"{value!r} doesn't fit the parameter ({attr['display']}, {attr['size']} bytes)") from e


def _fix_text(text: str) -> str:
    """Units in Rexroth dictionaries are UTF-8 bytes shown as code page 850 ("┬░" for "°", "┬Ás" for "µs");
    repair that (or the same as Latin-1) where it applies."""
    for codec in ("cp850", "cp437", "latin-1"):
        try:
            fixed = text.encode(codec).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if fixed != text:
            text = fixed
            break
    return text if text.isprintable() else ""


def show(data, attr: dict) -> str:
    """A value for people: decoded, numbers with their hex most significant byte first ("1000 (0x000003E8)").
    The data stays little-endian in the drive and the ENI."""
    data = bytes(data)
    try:
        value = decode(data, attr)
    except (ValueError, struct.error):
        return " ".join(f"{b:02X}" for b in data) or "-"
    size = attr["size"]

    def one(v, raw):
        if attr["display"] in ("unsigned", "signed", "binary") and isinstance(v, (int, float)) and len(raw) in (1, 2, 4, 8):
            return f"{v:g} (0x{int.from_bytes(raw, 'little'):0{len(raw) * 2}X})" if isinstance(v, float) else \
                f"{v} (0x{int.from_bytes(raw, 'little'):0{len(raw) * 2}X})"
        return f"{v:g}" if isinstance(v, float) else str(v)

    if isinstance(value, list):
        body = data[4:]
        return ", ".join(one(v, body[i * size:(i + 1) * size]) for i, v in enumerate(value)) or "(empty list)"
    return one(value, data[:size or len(data)])


SEARCH_IDN = re.compile(r"([sp])?\s*-?\s*(?:([0-7])\s*-\s*)?(\d{1,4})", re.IGNORECASE)


def matches(query: str, idn: int, text: str) -> bool:
    """Parameter filter: every word is part of "S-0-0265 name", or is an IDN written short: S265, s 265, S-265,
    S0265, P-0-12, P12 or only the number (265)."""
    query = re.sub(r"\b([sp])\s+(?=\d)", r"\1", query, flags=re.IGNORECASE)    # "s 265" -> "s265"
    hay = f"{idn_text(idn)} {text}".lower()
    for w in query.split():
        m = SEARCH_IDN.fullmatch(w)
        if w.lower() in hay:
            continue
        if not m or int(m.group(3)) != idn & 0xFFF:
            return False
        if m.group(1) and (m.group(1).upper() == "P") != bool(idn & 0x8000):
            return False
        if m.group(2) is not None and int(m.group(2)) != (idn >> 12) & 7:
            return False
    return True


def parse_dictionary(data: bytes) -> dict:
    """An EtherCATDict file -> {idn: {"name", "type", "bits", "min", "max", "mapping", "attribute", "unit"}}."""
    root = parse_xml(data)
    if root.tag != "EtherCATDict":
        raise SoeError(f"not an SoE dictionary (root element {root.tag})")
    out = {}
    for o in root.iter("Object"):
        try:
            idn = int((o.findtext("Index") or "").strip(), 0)
        except ValueError:
            continue
        names = o.findall("Name")
        name = next(((n.text or "").strip() for n in names if n.get("LcId") in (None, "1033")),
                    (names[0].text or "").strip() if names else "")
        attr_text = (o.findtext("Flags/Attribute") or "").strip()
        unit = ""
        for p in o.findall("Properties/Property"):
            if (p.findtext("Name") or "").strip() == "UnitName":
                unit = (p.findtext("Desc") or "").strip()
        out[idn] = {"idn": idn, "text": idn_text(idn), "name": name, "type": (o.findtext("Type") or "").strip(),
                    "bits": int((o.findtext("BitSize") or "0").strip() or 0),
                    "min": (o.findtext("Info/MinValue") or "").strip() or None,
                    "max": (o.findtext("Info/MaxValue") or "").strip() or None,
                    "mapping": (o.findtext("Flags/PdoMapping") or "").strip(),
                    "attribute": int(attr_text.replace("#x", "0x"), 0) if attr_text else None,
                    "unit": _fix_text(unit)}
    return out
