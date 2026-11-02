# SPDX-License-Identifier: MIT

"""Puts the three sources for one bus position side by side and lists where they disagree.

  online  what the master sees on the wire (slave_online_info)
  eeprom  the device's own description (SII), decoded by app.sii
  eni     the slave in the configuration the master runs with (app.eni)
  esi     the matching device from the ESI library (app.esi)

Entries are compared by object index, subindex and bit length only: names and data type spellings
differ between the sources (the EEPROM says BOOL where the ENI says BIT) without meaning anything.
"""

from app import modules

# The master's states as the Data Layer names them (EthercatState), and how people read them. The one place for it:
# the UI has the same table (stateName in app.js) for the values the API gives.
STATE_NAMES = {"init": "Init", "preop": "Pre-OP", "bootstrap": "Bootstrap", "safeop": "Safe-OP", "op": "OP"}
AL_CODES = {1: "init", 2: "preop", 3: "bootstrap", 4: "safeop", 8: "op"}      # the AL status of a device
AL_STATES = {code: STATE_NAMES[name] for code, name in AL_CODES.items()}
LEVELS = ("ok", "info", "warning", "error")


def state_name(value) -> str:
    """A state value of the Data Layer (init, preop, bootstrap, safeop, op) for people; unknown values as read."""
    return STATE_NAMES.get(value, value)


def al_state(status) -> str:
    if status is None:
        return ""
    name = AL_STATES.get(status & 0x0F, f"{status & 0x0F:#x}")
    return name + (" (error)" if status & 0x10 else "")


def hx(n, width=4):
    return f"#x{n:0{width}X}" if isinstance(n, int) else "?"


def ident(vendor, product, revision):
    return f"vendor {hx(vendor, 1)}, product {hx(product, 8)}, revision {hx(revision, 8)}"


def _entries_key(pdo):
    return [(e["index"], e["subindex"] if e["index"] else 0, e["bits"]) for e in pdo["entries"]]


def _process_sms(sms):
    """The process-data sync managers that are in use (an unused one has size 0 and no PDOs)."""
    return {s["type"]: s for s in sms
            if s["type"] in ("Outputs", "Inputs") and (s.get("size") or s.get("pdos"))}


def device_report(position, online, eeprom, eeprom_error, eni_slave, esi_match, esi_entry, esi_device,
                  esi_error=None, master_op=True, absent=False, configuration=False) -> dict:
    """configuration: the list is a project, no live data: online is the list's identity, the state is "-" and
    absent means "not paired with a device of the last scan". The EEPROM of a paired device is shown but not compared."""
    findings = []
    shown_eeprom, shown_error = eeprom, eeprom_error
    if configuration:
        eeprom = eeprom_error = None

    def add(level, text):
        findings.append({"level": level, "text": text})

    info = (online or {}).get("response", online or {})
    identity = info.get("slaveIdentity", {}) if info else {}
    vendor, product, revision = identity.get("vendorId"), identity.get("productCode"), identity.get("revisionNumber")
    al = info.get("alStatus") if info else None

    # --- on the wire
    if absent:
        add("warning", "Not on the bus at the last scan" if configuration else "Not present on the bus")
    else:
        if al is not None and al & 0x10:
            add("error", f"Device reports an error: AL status {al_state(al)}, code {hx(info.get('alStatusCode', 0))}")
        elif al is not None and master_op and al & 0x0F != 8:
            add("warning", f"Device is in {al_state(al)} while the master is in {state_name('op')}")

    # --- current configuration (ENI)
    if eni_slave is None and info:
        add("warning", "Not in the current configuration")
    elif eni_slave is not None and info:
        if (eni_slave["vendor"], eni_slave["product"]) != (vendor, product):
            add("error", f'Configured here: {eni_slave["name"]} ({ident(eni_slave["vendor"], eni_slave["product"], eni_slave["revision"])}); '
                         f"found: {ident(vendor, product, revision)}")
        elif eni_slave["revision"] != revision:
            add("warning", f'Configured with revision {hx(eni_slave["revision"], 8)}, the device has {hx(revision, 8)}')

    # --- ESI
    if esi_error:
        add("error", f"ESI could not be read: {esi_error}")
    elif esi_match == "none":
        add("warning", "No ESI file for this device in the library")
    elif esi_match == "nearest":
        add("warning", f'ESI only for revision {hx(esi_entry["revision"], 8)} (device has {hx(revision, 8)})')
    if esi_device and esi_device.get("modular"):
        if not modules.usable(esi_device):
            add("info", "Modular device: its modules are not in its ESI file (separate module files are not read yet)")
        elif eni_slave is not None:
            idents = modules.idents_from_startup(esi_device, eni_slave.get("coe_init", []))
            if idents is None:
                add("warning", "Modular device without a module list in the current configuration: its module "
                               "process data is missing. Build the configuration to add the modules (Settings tab).")
            else:
                add("info", "Modules: " + ", ".join(f'{sl["name"]} = {modules.name(esi_device, x)}'
                                                    for sl, x in zip(esi_device["slots"], idents)))

    # --- EEPROM
    if eeprom_error:
        add("warning", f"EEPROM could not be read: {eeprom_error}")
    elif eeprom and info and (eeprom["vendor"], eeprom["product"], eeprom["revision"]) != (vendor, product, revision):
        add("error", f'EEPROM identity ({ident(eeprom["vendor"], eeprom["product"], eeprom["revision"])}) differs from what the device reports')

    # --- PDOs side by side (a modular device: also the PDOs of its configured, else default, modules)
    modular = modules.usable(esi_device)
    if modular:
        idents = (modules.idents_from_startup(esi_device, eni_slave.get("coe_init", [])) if eni_slave else None) \
            or modules.defaults(esi_device)
        esi_device = dict(esi_device, pdos=esi_device["pdos"] + modules.pdos(esi_device, idents))
    rows = {}
    for source, desc in (("esi", esi_device), ("eni", eni_slave), ("eeprom", eeprom)):
        for p in (desc or {}).get("pdos", []):
            row = rows.setdefault((p["dir"], p["index"]), {"dir": p["dir"], "index": p["index"], "name": p["name"]})
            row[source] = p
    assigned = set()
    if eni_slave:
        for sm in eni_slave["sm"]:
            assigned.update(sm.get("pdos", []))
    pdo_rows = []
    for key in sorted(rows, key=lambda k: (k[0] != "rx", k[1])):
        r = rows[key]
        esi_p, eni_p, ee_p = r.get("esi"), r.get("eni"), r.get("eeprom")
        out = {"dir": r["dir"], "index": r["index"], "name": (esi_p or eni_p or ee_p)["name"],
               "esi": None if esi_p is None else {"sm": esi_p["sm"], "fixed": esi_p["fixed"], "mandatory": esi_p["mandatory"],
                                                  "excludes": esi_p["excludes"]},
               "eni": None if eni_p is None else {"sm": eni_p["sm"], "assigned": r["index"] in assigned},
               "eeprom": None if ee_p is None else {"sm": ee_p["sm"]},
               "entries": (esi_p or eni_p or ee_p)["entries"], "differs": []}
        ref = esi_p or ee_p
        for source, p in (("eni", eni_p), ("eeprom", ee_p)):
            if p is not None and ref is not None and p is not ref and _entries_key(p) != _entries_key(ref):
                out["differs"].append(source)
                add("warning", f'PDO {hx(r["index"])} "{out["name"]}": entries in the {source.upper()} differ from the '
                               f'{"ESI" if ref is esi_p else "EEPROM"}')
        if esi_device and esi_p is None:
            if eni_p is not None:
                add("error", f'PDO {hx(r["index"])} "{out["name"]}" is configured but not in the ESI')
            elif ee_p is not None:
                add("warning", f'PDO {hx(r["index"])} "{out["name"]}" is in the EEPROM but not in the ESI')
        if esi_device and eeprom and ee_p is None and esi_p is not None and not modular:   # EEPROMs list no module PDOs
            add("info", f'PDO {hx(r["index"])} "{out["name"]}" is in the ESI but not in the EEPROM')
        pdo_rows.append(out)

    if eni_slave and esi_device:
        esi_pdos = {p["index"]: p for p in esi_device["pdos"]}
        for idx in sorted(assigned):
            for other in esi_pdos.get(idx, {}).get("excludes", []):
                if other in assigned and idx < other:
                    add("error", f"PDOs {hx(idx)} and {hx(other)} are both assigned, but the ESI says they exclude each other")
        for p in esi_device["pdos"]:
            if p["mandatory"] and p["index"] not in assigned:
                add("warning", f'Mandatory PDO {hx(p["index"])} "{p["name"]}" is not assigned')

    # --- sync managers for process data
    sm_rows = []
    by_source = {"esi": _process_sms(esi_device["sm"]) if esi_device else {},
                 "eni": _process_sms(eni_slave["sm"]) if eni_slave else {},
                 "eeprom": _process_sms(eeprom["sm"]) if eeprom else {}}
    for sm_type in ("Outputs", "Inputs"):
        row = {"type": sm_type}
        for source, sms in by_source.items():
            s = sms.get(sm_type)
            row[source] = None if s is None else {"no": s["no"], "start": s["start"], "size": s["size"]}
        starts = {source: row[source]["start"] for source in by_source if row[source]}
        if len(set(starts.values())) > 1:
            add("error", f"{sm_type} sync manager start address differs: " +
                ", ".join(f"{s.upper()} {hx(v)}" for s, v in starts.items()))
        sm_rows.append(row)

    # --- mailbox protocols
    if esi_device and eeprom and set(esi_device["mailbox"]) != set(eeprom["mailbox"]):
        add("info", f'Mailbox protocols: ESI {", ".join(esi_device["mailbox"]) or "none"}, '
                    f'EEPROM {", ".join(eeprom["mailbox"]) or "none"}')

    level = max((f["level"] for f in findings), key=LEVELS.index, default="ok")
    eeprom, eeprom_error = shown_eeprom, shown_error
    name = (eni_slave or {}).get("name") or (esi_device or {}).get("type") or (eeprom or {}).get("order") or "?"
    return {
        "position": position,
        "name": name,
        "description": (esi_device or {}).get("name") or (eeprom or {}).get("name", ""),
        "address": (eni_slave or {}).get("address") if absent or configuration else info.get("ethercatAddr"),
        "vendor": vendor,
        "product": product,
        "revision": revision,
        "serial": None if absent or configuration else identity.get("serialNumber"),
        "present": not (absent or configuration),
        "mailbox": (esi_device or {}).get("mailbox") or (eeprom or {}).get("mailbox") or [],
        "state": "-" if configuration else al_state(al),
        "dc": None if absent or configuration else bool(info.get("dcSupport")),
        "sources": {
            "esi": {"match": "error" if esi_error else esi_match,
                    "file": (esi_entry or {}).get("file"), "revision": (esi_entry or {}).get("revision"),
                    "group": (esi_entry or {}).get("group"),
                    "dc_modes": (esi_device or {}).get("dc_modes", []), "coe_init": (esi_device or {}).get("coe_init", [])},
            "eni": {"match": "none" if eni_slave is None else "not found" if absent and not configuration else
                    "same" if (eni_slave["vendor"], eni_slave["product"], eni_slave["revision"]) == (vendor, product, revision)
                    else "revision" if (eni_slave["vendor"], eni_slave["product"]) == (vendor, product) else "different",
                    "name": (eni_slave or {}).get("name"), "address": (eni_slave or {}).get("address"),
                    "dc": (eni_slave or {}).get("dc"), "coe_init": (eni_slave or {}).get("coe_init", []),
                    "variables": (eni_slave or {}).get("variables", [])},
            "eeprom": {"match": "error" if eeprom_error else ("read" if eeprom else "not read"),
                       "name": (eeprom or {}).get("name"), "order": (eeprom or {}).get("order"),
                       "mailbox": (eeprom or {}).get("mailbox", []), "dc": (eeprom or {}).get("dc")},
        },
        "pdos": pdo_rows,
        "sm": sm_rows,
        "findings": findings,
        "level": level,
    }
