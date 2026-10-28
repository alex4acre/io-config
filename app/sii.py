# SPDX-License-Identifier: MIT

"""Decodes an EtherCAT device's EEPROM (SII, "slave information interface", ETG.1000.6).

The EtherCAT master reads it word by word (device_access/slave_eeprom); this module turns the words into
the same shape the ESI and ENI parsers give: identity, sync managers, PDOs, mailbox protocols, DC.

Layout: words 0x00..0x3F are fixed (identity at byte 0x10, mailbox protocols at word 0x1C); from word 0x40
on come categories, each a type word, a size word (in words) and the data, until type 0xFFFF.
"""

import struct

CAT_STRINGS, CAT_GENERAL, CAT_FMMU, CAT_SYNCM, CAT_TXPDO, CAT_RXPDO, CAT_DC = 10, 30, 40, 41, 50, 51, 60
CAT_END = 0xFFFF
FIRST_CATEGORY_WORD = 0x40

SM_TYPES = {0: "", 1: "MBoxOut", 2: "MBoxIn", 3: "Outputs", 4: "Inputs"}
MAILBOX_BITS = ((0x01, "AoE"), (0x02, "EoE"), (0x04, "CoE"), (0x08, "FoE"), (0x10, "SoE"), (0x20, "VoE"))

# CoE data type numbers (ETG.1000.6) as the ESI/ENI name them
DATA_TYPES = {0x01: "BOOL", 0x02: "SINT", 0x03: "INT", 0x04: "DINT", 0x05: "USINT", 0x06: "UINT", 0x07: "UDINT",
              0x08: "REAL", 0x09: "STRING", 0x0A: "OCTET_STRING", 0x11: "LREAL", 0x15: "LINT", 0x1B: "ULINT",
              0x30: "BIT1", 0x31: "BIT2", 0x32: "BIT3", 0x33: "BIT4", 0x34: "BIT5", 0x35: "BIT6", 0x36: "BIT7",
              0x37: "BIT8"}


class SiiError(ValueError):
    pass


def to_bytes(words) -> bytes:
    return b"".join(struct.pack("<H", w & 0xFFFF) for w in words)


def end_reached(words) -> bool:
    """True when the words read so far contain the end-of-categories marker (reading can stop)."""
    b = to_bytes(words)
    p = FIRST_CATEGORY_WORD * 2
    while p + 4 <= len(b):
        cat, size = struct.unpack_from("<HH", b, p)
        if cat == CAT_END:
            return True
        p += 4 + size * 2
    return False


def _categories(b: bytes):
    p = FIRST_CATEGORY_WORD * 2
    while p + 4 <= len(b):
        cat, size = struct.unpack_from("<HH", b, p)
        if cat == CAT_END:
            return
        body = b[p + 4:p + 4 + size * 2]
        if len(body) < size * 2:
            raise SiiError(f"category {cat} is cut off (EEPROM read too short)")
        yield cat, body
        p += 4 + size * 2


def _strings(body: bytes) -> list:
    out, q = [], 1
    for _ in range(body[0] if body else 0):
        n = body[q]
        out.append(body[q + 1:q + 1 + n].decode("latin-1"))
        q += 1 + n
    return out


def _pdos(body: bytes, direction: str, text) -> list:
    pdos, q = [], 0
    while q + 8 <= len(body):
        index, n_entries, sm, _dc_sync, name, flags = struct.unpack_from("<HBBBBH", body, q)
        q += 8
        entries = []
        for _ in range(n_entries):
            e_index, e_sub, e_name, e_type, bits, _e_flags = struct.unpack_from("<HBBBBH", body, q)
            q += 8
            entries.append({"index": e_index, "subindex": e_sub if e_index else 0, "name": text(e_name),
                            "bits": bits, "type": DATA_TYPES.get(e_type, "") if e_index else ""})
        pdos.append({"dir": direction, "index": index, "name": text(name),
                     "sm": None if sm == 0xFF else sm, "fixed": bool(flags & 0x0010),
                     "mandatory": bool(flags & 0x0001), "excludes": [], "entries": entries})
    return pdos


def decode(words) -> dict:
    """The device description from its EEPROM words."""
    b = to_bytes(words)
    if len(b) < FIRST_CATEGORY_WORD * 2:
        raise SiiError(f"EEPROM too short ({len(words)} words)")
    vendor, product, revision, serial = struct.unpack_from("<IIII", b, 0x10)
    mailbox = struct.unpack_from("<H", b, 0x1C * 2)[0]
    out = {"vendor": vendor, "product": product, "revision": revision, "serial": serial,
           "name": "", "order": "", "group": "", "sm": [], "pdos": [], "dc": False, "coe": None, "fmmu": [],
           "mailbox": [name for bit, name in MAILBOX_BITS if mailbox & bit]}
    cats = list(_categories(b))
    strings = []
    for cat, body in cats:
        if cat == CAT_STRINGS:
            strings = _strings(body)

    def text(i):
        return strings[i - 1] if 0 < i <= len(strings) else ""

    for cat, body in cats:
        if cat == CAT_GENERAL and len(body) >= 6:
            out["group"], out["order"], out["name"] = text(body[0]), text(body[2]), text(body[3])
            coe = body[5]       # bit 0 SDO, 1 SDO info, 2 PDO assign, 3 PDO config, 4 upload at start, 5 complete access
            out["coe"] = {"sdo_info": bool(coe & 0x02), "pdo_assign": bool(coe & 0x04), "pdo_config": bool(coe & 0x08),
                          "complete_access": bool(coe & 0x20)}
        elif cat == CAT_FMMU:
            out["fmmu"] = [{1: "Outputs", 2: "Inputs", 3: "MBoxState"}.get(x, "") for x in body if x not in (0, 0xFF)]
        elif cat == CAT_SYNCM:
            for i in range(0, len(body) - 7, 8):
                start, length, control, _status, enable, sm_type = struct.unpack_from("<HHBBBB", body, i)
                out["sm"].append({"no": i // 8, "type": SM_TYPES.get(sm_type, str(sm_type)), "start": start,
                                  "size": length, "control": control, "enable": bool(enable & 1)})
        elif cat == CAT_TXPDO:
            out["pdos"] += _pdos(body, "tx", text)
        elif cat == CAT_RXPDO:
            out["pdos"] += _pdos(body, "rx", text)
        elif cat == CAT_DC:
            out["dc"] = True
    return out
