# SPDX-License-Identifier: MIT

"""From a bus scan to a new configuration for the EtherCAT master, and the download.

build_plan() decides each device's description, in this order:
  1. the ESI (exact revision, else the nearest one with the device's real revision), else
  2. the EEPROM ("learned"),
  and on top of that 3. the current configuration's choices when the same device type sits at that
  position (name, assigned PDOs, DC activation, extra startup commands), so a bus configured with
  I/O Engineering is reproduced unchanged.
It also lists what changes for the users of the process image (realtime_data variables).

download() writes the configuration: backup, master to Init, write fieldbus_config, compare the master's
checksums, master to Op, and make sure the active configuration holds the new files (the master
loads comm.ethercat.master/config/<instance>/ethercat.xml + ethercat.ini at boot).
"""

import os
import time

from app import eni as eni_mod
from app import generate

PORTS = "ABCD"


class DownloadError(Exception):
    pass


def _variables(parsed) -> dict:
    out = {}
    for s in (parsed or {}).get("slaves", []):
        for v in s["variables"]:
            out[v["name"]] = (v["dir"], v["type"], v["bits"], v["offset"])
    return out


def build_plan(sources: list, current: dict, master_name: str, cycle_us: int = None) -> dict:
    """sources: per present device {"pos", "online", "eeprom", "eni_slave", "esi_match", "esi_device"};
    current: {"eni": bytes, "ini": bytes, "parsed": parse_eni() or None}."""
    devices, rows, errors, warnings = [], [], [], []
    used = set()
    ports = {}
    for src in sources:
        pos = src["pos"]
        info = (src.get("online") or {}).get("response", {})
        idn = info.get("slaveIdentity", {})
        vendor, product, revision = idn.get("vendorId"), idn.get("productCode"), idn.get("revisionNumber")
        if pos > 0 and info.get("previousPort") is not None and 0 <= info["previousPort"] < 4:
            ports[pos] = PORTS[info["previousPort"]]
        esi, slave = src.get("esi_device"), src.get("eni_slave")
        note = []
        if esi is not None and src.get("esi_match") in ("exact", "nearest"):
            name = esi["type"]
            dev = generate.device_from_esi(esi, "")
            if src["esi_match"] == "nearest":
                dev["revision"] = revision
                note.append(f'ESI of revision {esi["revision"]:#x}')
        elif src.get("eeprom"):
            name = src["eeprom"].get("order") or src["eeprom"].get("name") or "Device"
            dev = generate.device_from_eeprom(src["eeprom"], "")
            note.append("learned from the EEPROM (no ESI)")
            warnings.append(f"Position {pos}: no ESI, described from its EEPROM only (default PDOs, no DC)")
        else:
            errors.append(f"Position {pos}: no ESI and no EEPROM data, the device can't be described")
            rows.append({"position": pos, "name": "?", "source": None, "note": "", "inputs": 0, "outputs": 0})
            continue
        if slave is not None and (slave["vendor"], slave["product"]) == (vendor, product):
            dev = generate.apply_eni(dev, slave)
            dev["revision"] = revision
            note.insert(0, "kept from the current configuration")
        else:
            base = generate.device_name(name)
            dev["name"], i = base, 1
            while dev["name"] in used:
                i += 1
                dev["name"] = f"{base}_{i}"
            note.insert(0, "new: ESI defaults" if dev["source"] == "esi" else "new")
        used.add(dev["name"])
        devices.append(dev)
        rows.append({"position": pos, "name": dev["name"], "source": dev["source"], "note": "; ".join(note),
                     "inputs": sum(e["bits"] for p in dev["pdos"] if p["dir"] == "tx" for e in p["entries"]) // 8,
                     "outputs": sum(e["bits"] for p in dev["pdos"] if p["dir"] == "rx" for e in p["entries"]) // 8})

    ini_text = (current.get("ini") or b"").decode("latin-1")
    cycle = cycle_us or int(eni_mod.parse_ini(ini_text).get("EtherCAT.Master", {}).get("CycleTimeUs", 0) or 0) or 2000
    eni_bytes = ini_bytes = None
    if not errors:
        try:
            eni_bytes = generate.generate_eni(devices, master_name, cycle, ports)
            ini_bytes = generate.generate_ini(ini_text, cycle)
        except generate.GenerateError as e:
            errors.append(str(e))

    diff = {"added": [], "removed": [], "changed": []}
    if eni_bytes:
        old = _variables(current.get("parsed"))
        new = _variables(eni_mod.parse_eni(eni_bytes))
        diff["added"] = sorted(n for n in new if n not in old)
        diff["removed"] = sorted(n for n in old if n not in new)
        diff["changed"] = sorted(n for n in new if n in old and new[n] != old[n])
    old_names = [s["name"] for s in (current.get("parsed") or {}).get("slaves", [])]
    return {
        "devices": rows,
        "removed_devices": [n for n in old_names if n not in {d["name"] for d in devices}],
        "cycle_us": cycle,
        "errors": errors,
        "warnings": warnings,
        "variables": diff,
        "identical": bool(eni_bytes) and eni_bytes == current.get("eni") and ini_bytes == current.get("ini"),
        "eni": eni_bytes,
        "ini": ini_bytes,
    }


# --- download ----------------------------------------------------------------------------------------

def _wait_state(bus, base, wanted, timeout, log):
    end = time.monotonic() + timeout
    state = None
    while time.monotonic() < end:
        state = bus.read(base + "admin/fieldbus_status/master_state").get("response", {})
        if state.get("currentState") == wanted:
            return state
        time.sleep(0.5)
    raise DownloadError(f"the master did not reach {wanted} within {timeout} s "
                        f'(now {state.get("currentState") if state else "?"}, requested {state.get("requestedState") if state else "?"})')


def write_config(bus, base: str, eni: bytes, ini: bytes, active_files: tuple, log, op_timeout=60) -> list:
    """The download sequence; returns the steps done as text. active_files: (eni path, ini path) of the
    master's files in the active configuration."""
    steps = []

    def step(text):
        steps.append(text)
        log(f"download: {text}")

    bus.write(base + "admin/fieldbus_status/master_state", {"request": {"newState": "init"}})
    _wait_state(bus, base, "init", 30, log)
    step("master in Init")
    bus.write(base + "admin/fieldbus_config", {"request": {"eni": list(eni), "ini": list(ini)}})
    step(f"configuration written ({len(eni)} bytes ENI, {len(ini)} bytes ini)")
    sums = {c["id"]: c["checksum"] for c in
            bus.read(base + "admin/fieldbus_config/checksum").get("response", {}).get("checksums", [])}
    if sums.get("eniCrc32") != generate.checksum(eni) or sums.get("iniCrc32") != generate.checksum(ini):
        raise DownloadError(f"the master reports other checksums than the files written ({sums})")
    step("master checksums match")
    bus.write(base + "admin/fieldbus_status/master_state", {"request": {"newState": "op"}})
    _wait_state(bus, base, "op", op_timeout, log)
    step("master in Op")
    eni_path, ini_path = active_files
    time.sleep(1.0)
    if _same_file(eni_path, eni) and _same_file(ini_path, ini):
        step("the master stored the files in the active configuration itself")
    else:
        os.makedirs(os.path.dirname(eni_path), exist_ok=True)
        for path, data in ((eni_path, eni), (ini_path, ini)):
            tmp = path + ".tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        step("files written to the active configuration (loaded at the next boot)")
    return steps


def _same_file(path, data) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read() == data
    except OSError:
        return False
