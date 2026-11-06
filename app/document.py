# SPDX-License-Identifier: MIT

"""A complete bus configuration as one JSON document, for programs (Data Layer node io-config/create, REST
POST /configurations). The contract is in docs/api.md.

    {"schema": 1, "name": "Line1", "overwrite": false, "cycle_us": 4000,
     "devices": [{"vendor": "0x24", "product": "0x242A0F", "revision": "0x10100", "name": "Coupler"},
                 {"vendor": "0x24", "product": "0x242A36", "revision": "0x100", "name": "Inputs",
                  "connected_to": {"device": "Coupler", "port": "C"}, "pdos": [6656],
                  "variable_names": {"in:Channel_1.Value": "MyInput1"}}]}

The list order is the wiring order. parse() checks the document's form; what depends on the device (PDOs, modules, DC
modes, startup parameters) is checked by edits.validate, like an edit made in the UI. export() is the way back: the
document that describes a device list as it would be built.
"""

import json

from app import configure, edits, generate
from app.eni import num

SCHEMA = 1
MAX_DEVICES = 1024
MAX_SIZE = 1024 * 1024         # bytes of JSON, as for a REST body
TOP_KEYS = {"schema", "name", "overwrite", "cycle_us", "eoe", "devices"}
DEVICE_KEYS = {"vendor", "product", "revision", "name", "connected_to", "pdos", "modules", "dc_mode", "startup",
               "variable_names", "soe_mdt", "soe_at"}
EDIT_KEYS = ("name", "pdos", "modules", "dc_mode", "startup", "variable_names", "soe_mdt", "soe_at")
BRANCH_PORTS = ("B", "C", "D")


class DocumentError(ValueError):
    """Everything wrong with a document: .errors, and the warnings found until then."""

    def __init__(self, errors: list, warnings: list = None):
        super().__init__("; ".join(errors))
        self.errors = list(errors)
        self.warnings = list(warnings or [])


def _number(value):
    """A plain number, or text in hex (0x1A00, #x1A00) or decimal; None if it is neither."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return num(value) if isinstance(value, str) else None


def _numbers(values, what, problems):
    if not isinstance(values, list):
        problems.append(f"{what} must be a list")
        return []
    out = [_number(x) for x in values]
    if None in out:
        problems.append(f"{what} must be a list of numbers")
    return [x for x in out if x is not None]


def _startup(rows, label, problems):
    if not isinstance(rows, list) or not all(isinstance(x, dict) for x in rows):
        problems.append(f"{label}: startup must be a list of parameters")
        return []
    out = []
    for x in rows:
        row = dict(x)
        for key in ("index", "subindex"):
            if key in row:
                value = _number(row[key])
                if value is None:
                    problems.append(f"{label}: startup {key} {row[key]!r} is not a number")
                else:
                    row[key] = value
        out.append(row)
    return out


def _connection(device, label, seen, used, problems):
    """(port, upstream position) of a device from its connected_to, by default port B of the device above it."""
    position = len(seen)
    wanted = device.get("connected_to")
    if wanted is None:
        if position == 0:
            return None
        port, upstream = "B", position - 1
    else:
        if position == 0:
            problems.append(f"{label}: the first device has nothing to be connected to")
            return None
        if not isinstance(wanted, dict) or set(wanted) != {"device", "port"}:
            problems.append(f'{label}: connected_to must be {{"device": <name>, "port": "B", "C" or "D"}}')
            return None
        port, upstream = wanted["port"], seen.get(str(wanted["device"]).lower())
        if port not in BRANCH_PORTS:
            problems.append(f'{label}: connected_to port must be "B", "C" or "D"')
            return None
        if upstream is None:
            problems.append(f'{label}: connected_to names "{wanted["device"]}", which is not a named device above it')
            return None
    if (port, upstream) in used:
        problems.append(f"{label}: port {port} of device {upstream} is already taken by another device")
        return None
    used.add((port, upstream))
    return port, upstream


def parse(doc) -> dict:
    """The document (dict, or JSON text) checked and in plain numbers; DocumentError with all problems otherwise.
    Unknown keys are refused: a mistyped option must not disappear without a word."""
    if isinstance(doc, str):
        if len(doc.encode("utf-8", "replace")) > MAX_SIZE:
            raise DocumentError([f"the document is larger than {MAX_SIZE // 1024} KB"])
        try:
            doc = json.loads(doc)
        except ValueError as e:
            raise DocumentError([f"not valid JSON: {e}"]) from e
    if not isinstance(doc, dict):
        raise DocumentError(["the document must be a JSON object"])
    problems = [f'unknown key "{k}"' for k in sorted(set(doc) - TOP_KEYS)]
    if doc.get("schema") != SCHEMA or isinstance(doc.get("schema"), bool):
        problems.append(f'"schema" must be {SCHEMA}')
    if not isinstance(doc.get("name"), str) or not doc["name"]:
        problems.append('"name" is needed')
    if not isinstance(doc.get("overwrite", False), bool):
        problems.append('"overwrite" must be true or false')
    cycle = doc.get("cycle_us")
    if cycle is not None and (not isinstance(cycle, int) or isinstance(cycle, bool) or not 125 <= cycle <= 100000):
        problems.append('"cycle_us" must be 125 to 100000')
    eoe = doc.get("eoe")
    if eoe is not None:
        problems += generate.check_eoe(eoe)
    devices = doc.get("devices")
    if not isinstance(devices, list) or not devices:
        problems.append('"devices" must be a list with at least one device')
        devices = []
    if len(devices) > MAX_DEVICES:
        problems.append(f'"devices" holds more than {MAX_DEVICES} devices')
        devices = []
    out, seen, used = [], {}, set()
    for position, device in enumerate(devices):
        label = f"device {position}"
        if not isinstance(device, dict):
            problems.append(f"{label}: must be an object")
            seen[f"#{position}"] = position          # keeps the positions of the devices below it right
            out.append(None)
            continue
        if isinstance(device.get("name"), str) and device["name"]:
            label = f'device {position} "{device["name"]}"'
        problems += [f'{label}: unknown key "{k}"' for k in sorted(set(device) - DEVICE_KEYS)]
        row = {}
        for key in ("vendor", "product", "revision"):
            row[key] = _number(device.get(key))
            if row[key] is None:
                problems.append(f"{label}: {key} is needed, as a number")
        row["previous"] = _connection(device, label, seen, used, problems)
        edit = {k: device[k] for k in EDIT_KEYS if k in device}
        for key in ("pdos", "modules"):
            if key in edit:
                edit[key] = _numbers(edit[key], f"{label}: {key}", problems)
        if "startup" in edit:
            edit["startup"] = _startup(edit["startup"], label, problems)
        row["edit"] = edit
        row["label"] = label
        out.append(row)
        seen[str(device.get("name") or f"#{position}").lower()] = position
    if problems:
        raise DocumentError(problems)
    return {"name": doc["name"], "overwrite": doc.get("overwrite", False), "cycle_us": cycle, "eoe": eoe, "devices": out}


# --- the way back: a device list as a document ------------------------------------------------------------

def _hex(value) -> str:
    return f"0x{value:X}" if isinstance(value, int) else str(value)


def _startup_rows(dev: dict, esi: dict) -> list:
    """The startup parameters the device has beyond the ESI's own commands, which a new build adds by itself."""
    own = {(c["index"], c["subindex"], c["data"].upper()) for c in (esi or {}).get("coe_init", [])}
    mailbox = dev.get("mailbox") or {}
    rows = []
    for c in (mailbox.get("coe") or {}).get("init", []):
        if (c.get("from_eni") or c.get("user")) and (c["index"], c["subindex"], c["data"].upper()) not in own:
            row = {k: c[k] for k in ("index", "subindex", "data", "transitions", "comment") if k in c}
            row.update({k: c[k] for k in ("data_type", "bits") if k in c})
            rows.append(row)
    for c in (mailbox.get("soe") or {}).get("init", []):
        if c.get("user"):
            rows.append({k: c[k] for k in ("idn", "data", "transitions", "comment") if k in c})
    return rows


def _telegram(dev: dict, direction: str) -> list:
    return [e["index"] for p in dev["pdos"] if p["dir"] == direction for e in p["entries"]]


def _choices(row: dict, src: dict, dev: dict, label: str, warnings: list):
    """The settings where the device differs from what its ESI gives by itself (nothing to say without an ESI)."""
    esi = src.get("esi_device") if src.get("esi_match") in ("exact", "nearest") else None
    if esi is None:
        return
    default = generate.device_from_esi(esi, "")
    if dev.get("modules"):
        if dev["modules"]["idents"] != default["modules"]["idents"]:
            row["modules"] = list(dev["modules"]["idents"])
    elif {p["index"] for p in dev["pdos"]} != {p["index"] for p in default["pdos"]}:
        if edits.catalog(src)[1]:
            row["pdos"] = [p["index"] for p in dev["pdos"]]
        else:
            warnings.append(f"{label}: its PDOs differ from the ESI, which says they can't be changed")
    active = (dev.get("dc") or {}).get("assign_activate", 0)
    if active != (default.get("dc") or {}).get("assign_activate", 0):
        mode = next((m["name"] for m in esi.get("dc_modes") or [] if m["assign_activate"] == active), None)
        if mode:
            row["dc_mode"] = mode
        else:
            warnings.append(f"{label}: its DC activation {active:#x} is no DC mode of the ESI")
    if ((dev.get("mailbox") or {}).get("soe")) is not None:
        for key, direction in (("soe_mdt", "rx"), ("soe_at", "tx")):
            if _telegram(dev, direction) != _telegram(default, direction):
                row[key] = _telegram(dev, direction)


def export(sources: list, user_edits: dict, name: str, cycle_us=None, eoe: dict = None) -> tuple:
    """(document, warnings) for a device list, as configure.build_plan would build it. Always names every device, since
    connected_to refers to names."""
    user_edits = user_edits or {}
    names = configure.device_names(sources, user_edits)
    ports = configure.topology(sources)
    devices, warnings = [], []
    for src, device_name in zip(sources, names):
        pos = src["pos"]
        identity = (src.get("online") or {}).get("response", {}).get("slaveIdentity", {})
        # a document's revision says which ESI to use: a device with another revision is built with the ESI's, as in the UI
        revision = identity.get("revisionNumber")
        if src.get("esi_match") in ("exact", "nearest") and src.get("esi_device"):
            revision = src["esi_device"]["revision"]
        row = {"vendor": _hex(identity.get("vendorId")), "product": _hex(identity.get("productCode")), "revision": _hex(revision)}
        label = f'device {pos} "{device_name}"' if device_name else f"device {pos}"
        if device_name:
            row["name"] = device_name
        port, upstream = ports.get(pos, ("B", pos - 1))
        if pos > 0 and (port, upstream) != ("B", pos - 1):
            row["connected_to"] = {"device": names[upstream], "port": port}
        if src.get("esi_match") not in ("exact", "nearest"):
            warnings.append(f"{label}: no ESI in the library, so creating from this document is refused until there is one")
        try:
            dev = configure.device_for(src, user_edits)
        except edits.EditError as e:
            warnings.append(f"{label}: {e}")
            dev = None
        if dev is not None:
            _choices(row, src, dev, label, warnings)
            startup = _startup_rows(dev, src.get("esi_device"))
            if startup:
                row["startup"] = startup
            names_used = {k: v for k, v in (dev.get("variable_names") or {}).items() if v}
            if names_used:
                row["variable_names"] = names_used
        devices.append(row)
    document = {"schema": SCHEMA, "name": name}
    if cycle_us:
        document["cycle_us"] = cycle_us
    if eoe:
        document["eoe"] = {k: eoe[k] for k in ("enabled", "ip", "mask")}
    document["devices"] = devices
    return document, warnings
