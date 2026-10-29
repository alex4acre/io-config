# SPDX-License-Identifier: MIT

"""The user's choices per bus position: the device name and which PDOs are assigned.

An edit is kept with the identity of the device it was made for (vendor, product code, revision) and is
only applied while that device sits at that position. The PDOs a device offers come from its ESI (or,
without one, its EEPROM); a device whose PDO assignment can't be changed (no CoE PDO assignment) keeps its
fixed PDOs, only its name can be changed.
"""

import re

from app import generate, soe

NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")
TRANSITIONS = ("IP", "PS", "SO")          # CoE startup commands can be sent on these state changes


class EditError(ValueError):
    pass


def _size(pdo) -> int:
    return sum(e["bits"] for e in pdo["entries"])


def catalog(src: dict):
    """(pdos the device offers, PDO assignment changeable?, why not) for a scanned position."""
    esi = src.get("esi_device") if src.get("esi_match") in ("exact", "nearest") else None
    if esi is not None:
        pdos = [generate._oversampled(p) for p in esi["pdos"]]
        can = bool(esi.get("pdo_assign"))
        return pdos, can, "" if can else "the ESI says the device's PDO assignment is fixed"
    eeprom = src.get("eeprom")
    if eeprom:
        can = bool((eeprom.get("coe") or {}).get("pdo_assign"))
        return eeprom["pdos"], can, "" if can else "the device's PDO assignment is fixed (EEPROM)"
    slave = src.get("eni_slave")
    if slave:
        return slave["pdos"], False, "no ESI and no EEPROM data: only the configured PDOs are known"
    return [], False, "the device is not described"


def options(src: dict, dev: dict, edit: dict = None) -> dict:
    """What the editor shows for one position. dev: the device as the configuration would be built
    (generate.device_from_* / apply_eni / apply_edit)."""
    pdos, can, why = catalog(src)
    selected = [p["index"] for p in dev["pdos"]] if dev else []
    default = [p["index"] for p in pdos if p["sm"] is not None]
    return {
        "position": src["pos"],
        "name": dev["name"] if dev else "",
        "editable": can,
        "reason": why,
        "edited": bool(edit),
        "selected": selected,
        "default": default,
        "pdos": [{"dir": p["dir"], "index": p["index"], "name": p["name"], "bits": _size(p),
                  "sm": p["sm"], "fixed": p["fixed"], "mandatory": p["mandatory"], "excludes": p["excludes"],
                  "entries": [{"index": e["index"], "subindex": e["subindex"], "name": e["name"], "bits": e["bits"],
                               "type": e.get("type", "")} for e in p["entries"]]} for p in pdos],
    }


def validate(src: dict, edit: dict) -> dict:
    """The edit, checked and cleaned; EditError with all problems otherwise."""
    problems = []
    out = {}
    info = (src.get("online") or {}).get("response", {}).get("slaveIdentity", {})
    out["identity"] = [info.get("vendorId"), info.get("productCode"), info.get("revisionNumber")]
    if "name" in edit and edit["name"] not in (None, ""):
        if not isinstance(edit["name"], str) or not NAME_RE.fullmatch(edit["name"]):
            problems.append("the name must start with a letter and contain only letters, digits and _ (max 64)")
        else:
            out["name"] = edit["name"]
    if edit.get("pdos") is not None:
        pdos, can, why = catalog(src)
        if not can:
            problems.append(f"PDOs can't be changed: {why}")
        by_index = {p["index"]: p for p in pdos}
        chosen = edit["pdos"]
        if not isinstance(chosen, list) or not all(isinstance(x, int) and not isinstance(x, bool) for x in chosen):
            problems.append("pdos must be a list of PDO indexes")
            chosen = []
        for x in chosen:
            if x not in by_index:
                problems.append(f"PDO {x:#06x} is not offered by the device")
        for p in pdos:
            if p["mandatory"] and p["index"] not in chosen:
                problems.append(f'PDO {p["index"]:#06x} "{p["name"]}" is mandatory')
            for other in p["excludes"]:
                if p["index"] in chosen and other in chosen and p["index"] < other:
                    problems.append(f'PDOs {p["index"]:#06x} and {other:#06x} exclude each other')
        out["pdos"] = sorted(set(chosen), key=lambda x: [p["index"] for p in pdos].index(x) if x in by_index else 0)
    if edit.get("startup") is not None:
        rows = edit["startup"]
        if not isinstance(rows, list) or len(rows) > 200:
            problems.append("startup must be a list (at most 200 parameters)")
            rows = []
        clean = []
        for i, x in enumerate(rows, 1):
            if "idn" in x:                       # an SoE parameter (drives)
                try:
                    idn = soe.parse_idn(x["idn"])
                    data = str(x["data"]).replace(" ", "").upper()
                    bytes.fromhex(data)
                except (KeyError, TypeError, ValueError):
                    problems.append(f"startup parameter {i}: idn and data (hex) are needed")
                    continue
                if not data or len(data) > 2048:
                    problems.append(f"startup parameter {i}: data empty or too long")
                    continue
                clean.append({"idn": idn, "data": data, "transitions": ["PS"],
                              "comment": str(x.get("comment") or soe.idn_text(idn))[:120]})
                continue
            try:
                index, sub = int(x["index"]), int(x["subindex"])
                data = str(x["data"]).replace(" ", "").upper()
                bytes.fromhex(data)
                transitions = [str(tr) for tr in (x.get("transitions") or ["PS"])]
            except (KeyError, TypeError, ValueError):
                problems.append(f"startup parameter {i}: index, subindex and data (hex) are needed")
                continue
            if not (0 <= index <= 0xFFFF and 0 <= sub <= 255) or not data or len(data) > 2048:
                problems.append(f"startup parameter {i}: index/subindex out of range or data empty/too long")
            elif any(tr not in TRANSITIONS for tr in transitions):
                problems.append(f"startup parameter {i}: transitions must be some of {', '.join(TRANSITIONS)}")
            else:
                clean.append({"index": index, "subindex": sub, "data": data, "transitions": transitions,
                              "comment": str(x.get("comment") or f"{index:#06x}:{sub}")[:120]})
        out["startup"] = clean
    for key, direction, mark, lead in (("soe_mdt", "rx", "r", SOE_CONTROL_WORD), ("soe_at", "tx", "t", SOE_STATUS_WORD)):
        if edit.get(key) is None:
            continue
        dictionary = src.get("soe_dict")
        if not dictionary:
            problems.append("the drive's SoE dictionary is not in the ESI library")
            continue
        try:
            idns = [soe.parse_idn(x) for x in edit[key]]
        except soe.SoeError as e:
            problems.append(str(e))
            continue
        bad = [soe.idn_text(i) for i in idns if mark not in (dictionary.get(i) or {}).get("mapping", "")]
        if bad:
            problems.append(f'not mappable into the {"MDT" if mark == "r" else "AT"}: {", ".join(bad)}')
        if not idns or idns[0] != lead:
            problems.append(f'the {"MDT" if mark == "r" else "AT"} must start with {soe.idn_text(lead)}')
        if len(set(idns)) != len(idns):
            problems.append(f'{"MDT" if mark == "r" else "AT"}: an IDN appears twice')
        if sum(dictionary.get(i, {}).get("bits", 0) for i in idns) > 8 * 512:
            problems.append(f'{"MDT" if mark == "r" else "AT"}: more than 512 bytes')
        out[key] = idns
    if problems:
        raise EditError("; ".join(problems))
    return out


SOE_CONTROL_WORD, SOE_STATUS_WORD = 134, 135


def soe_type(dict_type: str) -> str:
    """Dictionary types like UINT_bin / UDINT_hex as the ENI's data types (UINT / UDINT)."""
    base = dict_type.split("_", 1)[0]
    return base if base in ("SINT", "INT", "DINT", "LINT", "USINT", "UINT", "UDINT", "ULINT", "REAL", "LREAL") else ""


def applies(src: dict, edit: dict) -> bool:
    info = (src.get("online") or {}).get("response", {}).get("slaveIdentity", {})
    return edit.get("identity") == [info.get("vendorId"), info.get("productCode"), info.get("revisionNumber")]


def apply_edit(dev: dict, src: dict, edit: dict) -> dict:
    """The device with the user's name and PDO choice."""
    out = dict(dev)
    if edit.get("name"):
        out["name"] = edit["name"]
    if edit.get("pdos") is not None:
        pdos, _can, _why = catalog(src)
        by_index = {p["index"]: p for p in pdos}
        sm_for = {"tx": next((s["no"] for s in dev["sm"] if s["type"] == "Inputs"), None),
                  "rx": next((s["no"] for s in dev["sm"] if s["type"] == "Outputs"), None)}
        chosen = []
        for x in edit["pdos"]:
            p = by_index.get(x)
            if p is None:
                continue
            sm = p["sm"] if p["sm"] is not None else sm_for[p["dir"]]
            if sm is None:
                raise EditError(f'PDO {x:#06x}: the device has no sync manager for {p["dir"]} data')
            chosen.append(dict(p, sm=sm))
        out["pdos"] = chosen
        out["source"] = "edited"
    for key, direction, name in (("soe_mdt", "rx", "MDT"), ("soe_at", "tx", "AT")):
        if edit.get(key) is None:
            continue
        dictionary = src.get("soe_dict") or {}
        pdos = []
        for p in out["pdos"]:
            if p["dir"] == direction:
                # like I/O Engineering: entries already in the telegram keep their ESI (or ENI) name and type,
                # only added IDNs come from the SoE dictionary
                known = {e["index"]: e for e in p["entries"]}
                entries = [dict(known[i]) if i in known else
                           {"index": i, "subindex": 0, "name": dictionary.get(i, {}).get("name", soe.idn_text(i)),
                            "bits": dictionary.get(i, {}).get("bits", 0), "type": soe_type(dictionary.get(i, {}).get("type", ""))}
                           for i in edit[key]]
                p = dict(p, entries=entries)
            pdos.append(p)
        out["pdos"] = pdos
        out["source"] = "edited"
    if "startup" in edit:
        # the user's list replaces the startup commands kept from the current configuration (it starts as a
        # copy of them in the editor); the ESI's own commands stay
        mbx = out.get("mailbox")
        if not mbx or (mbx.get("coe") is None and mbx.get("soe") is None):
            if edit["startup"]:
                raise EditError("the device has no CoE mailbox for startup parameters")
            return out
        coe_rows = [x for x in edit["startup"] if "idn" not in x]
        soe_rows = [x for x in edit["startup"] if "idn" in x]
        if coe_rows and mbx.get("coe") is None or soe_rows and mbx.get("soe") is None:
            raise EditError("the device has no " + ("CoE" if coe_rows and mbx.get("coe") is None else "SoE")
                            + " mailbox for these startup parameters")
        if mbx.get("coe") is not None:
            init = [c for c in mbx["coe"].get("init", []) if not c.get("from_eni")]
            mbx = dict(mbx, coe=dict(mbx["coe"], init=init + [dict(x, complete_access=False, user=True) for x in coe_rows]))
        if mbx.get("soe") is not None:
            mbx = dict(mbx, soe=dict(mbx["soe"], init=list(mbx["soe"].get("init", [])) + [dict(x, user=True) for x in soe_rows]))
        out["mailbox"] = mbx
    return out


def kept_startup(dev: dict) -> list:
    """The startup commands a device keeps from the current configuration, in the editor's form."""
    coe = ((dev or {}).get("mailbox") or {}).get("coe") or {}
    return [{"index": c["index"], "subindex": c["subindex"], "data": c["data"], "transitions": c["transitions"],
             "comment": c.get("comment") or f'{c["index"]:#06x}:{c["subindex"]}'}
            for c in coe.get("init", []) if c.get("from_eni")]
