# SPDX-License-Identifier: MIT

"""Modular devices (ETG.5001 Modular Device Profile): process data from modules put into slots.

The ESI lists the device's slots, the modules each slot takes and its default module; the modules (PDOs,
CoE startup commands) are defined in the same file. A module's PDO index moves with the slot it sits in
(DependOnSlot: + slot number x SlotPdoIncrement) and its slot group (DependOnSlotGroup: + group x
SlotGroupPdoIncrement); object indexes of entries and startup commands move with SlotGroupIndexIncrement
(e.g. Elmo Titanium: axis 2 uses #x1610 / #x6840 where axis 1 uses #x1600 / #x6040).

The master writes, in PreOp->SafeOp: the configured module list (#xF030, when the ESI asks for it), the PDO
contents (the module PDOs are empty in the device until then), the PDO assignment and the modules' startup
commands. Seen on the Elmo Titanium 2026-10-30: #xF030/#xF050 = [#x288110, #x100, #x100] (the ESI defaults
without the empty slots at the end), #x1600/#x1A00 empty.

Devices whose modules are only in separate module files (e.g. the ctrlX DRIVE's FSoE option) are not built
from modules: describe() keeps them as before.
"""

MODULE_LIST = 0xF030
EMPTY = 0


def usable(esi: dict) -> bool:
    """Whether the device can be built from modules: it has slots and the ESI defines every default module."""
    slots = (esi or {}).get("slots") or []
    mods = (esi or {}).get("modules") or {}
    return bool(slots) and bool(mods) and all(not s["default"] or s["default"] in mods for s in slots)


def defaults(esi: dict) -> list:
    return [s["default"] or EMPTY for s in esi["slots"]]


def choices(esi: dict, slot: dict) -> list:
    """The modules a slot takes that the ESI defines (EMPTY first when the slot may stay empty)."""
    mods = esi["modules"]
    out = [x for x in slot["idents"] if x in mods]
    out += [m["ident"] for m in mods.values() if m["class"] and m["class"] in slot["classes"] and m["ident"] not in out]
    if (EMPTY in slot["idents"] or slot["min"] == 0) and EMPTY not in out:
        out.insert(0, EMPTY)
    elif EMPTY in out:
        out.remove(EMPTY)
        out.insert(0, EMPTY)
    return out


def name(esi: dict, ident: int) -> str:
    return "Empty" if ident == EMPTY else (esi["modules"].get(ident) or {}).get("name", f"{ident:#x}")


def _offsets(esi: dict, no: int):
    slot, inc = esi["slots"][no], esi["slot_increments"]
    pdo_inc = slot["pdo_increment"] if slot.get("pdo_increment") is not None else inc["pdo"]
    index_inc = slot["index_increment"] if slot.get("index_increment") is not None else inc["index"]
    return {"slot": no * pdo_inc, "group": slot["group"] * inc["group_pdo"]}, \
           {"slot": no * index_inc, "group": slot["group"] * inc["group_index"]}


def _moved(index: int, depends: dict, offsets: dict) -> int:
    if not index:
        return index
    return index + sum(offsets[k] for k in ("slot", "group") if (depends or {}).get(k))


def pdos(esi: dict, idents: list) -> list:
    """The PDOs of the chosen modules, numbered for their slots. The PDO name starts with the slot name, so two
    axes with the same module give different process-image variables."""
    out = []
    for no, ident in enumerate(idents):
        mod = esi["modules"].get(ident)
        if not mod:
            continue
        pdo_off, index_off = _offsets(esi, no)
        slot_name = esi["slots"][no]["name"]
        for p in mod["pdos"]:
            entries = [{k: v for k, v in dict(e, index=_moved(e["index"], e.get("depends"), index_off)).items()
                        if k != "depends"} for e in p["entries"]]
            q = {k: v for k, v in p.items() if k != "depends"}
            out.append(dict(q, index=_moved(p["index"], p.get("depends"), pdo_off),
                            name=f'{slot_name} {p["name"]}', entries=entries))
    return out


def startup(esi: dict, idents: list) -> list:
    """The modules' CoE startup commands, object indexes moved for their slot group."""
    out = []
    for no, ident in enumerate(idents):
        mod = esi["modules"].get(ident)
        if not mod:
            continue
        _pdo_off, index_off = _offsets(esi, no)
        for c in mod["init"]:
            out.append(dict({k: v for k, v in c.items() if k != "depends"},
                            index=_moved(c["index"], c.get("depends"), index_off), module=True))
    return out


def module_list_data(idents: list) -> str:
    """#xF030 written with complete access: count, padding byte, one UDINT per slot (empty slots at the end left out)."""
    n = len(idents)
    while n and idents[n - 1] == EMPTY:
        n -= 1
    return f"{n:02X}00" + "".join(x.to_bytes(4, "little").hex().upper() for x in idents[:n])


def idents_from_startup(esi: dict, coe_init: list):
    """The module list a configuration writes (its #xF030 command), one ident per slot; None if it has none."""
    for c in coe_init:
        if c["index"] == MODULE_LIST and c["subindex"] == 0 and c.get("complete_access"):
            data = bytes.fromhex(c["data"])
            if len(data) < 2:
                return None
            n = data[0]
            idents = [int.from_bytes(data[2 + 4 * k:6 + 4 * k], "little") for k in range(n) if len(data) >= 6 + 4 * k]
            return (idents + [EMPTY] * len(esi["slots"]))[:len(esi["slots"])]
    return None


def is_generated(c: dict) -> bool:
    """Startup commands the generator writes itself for a modular device (module list, PDO contents and assignment)."""
    return c["index"] == MODULE_LIST or 0x1600 <= c["index"] <= 0x17FF or 0x1A00 <= c["index"] <= 0x1BFF \
        or c["index"] in (0x1C12, 0x1C13)


def apply(dev: dict, esi: dict, idents: list) -> dict:
    """The device with these modules: their PDOs and startup commands, the module list to write."""
    out = dict(dev, pdos=pdos(esi, idents), modules={"idents": list(idents),
                                                     "download_list": bool(esi.get("download_module_list")),
                                                     "pdo_config": bool(esi.get("pdo_config")),
                                                     "complete_access": bool(esi.get("complete_access"))})
    mbx = out.get("mailbox")
    if mbx and mbx.get("coe") is not None:
        init = [c for c in mbx["coe"].get("init", []) if not c.get("module")]
        mod_init = startup(esi, idents)
        # the ESI's device commands first, then the modules' (user and kept commands follow in apply_edit / apply_eni)
        fixed = [c for c in init if not c.get("from_eni") and not c.get("user")]
        rest = [c for c in init if c.get("from_eni") or c.get("user")]
        out["mailbox"] = dict(mbx, coe=dict(mbx["coe"], init=fixed + mod_init + rest))
    return out
