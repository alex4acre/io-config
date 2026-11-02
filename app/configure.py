# SPDX-License-Identifier: MIT

"""From a bus scan to a new configuration for the EtherCAT master, and the download.

build_plan() decides each device's description, in this order:
  1. the ESI (exact revision, else the nearest one with the device's real revision), else
  2. the EEPROM ("learned"),
  and on top of that 3. the current configuration's choices when the same device type sits at that
  position (name, assigned PDOs, DC activation, extra startup commands), so a bus configured with
  I/O Engineering is reproduced unchanged.
It also lists what changes for the users of the process image (realtime_data variables).

A project device that isn't on the bus (bus_pos None) is built like any other, only with a warning.

write_config() writes the configuration, best effort: master to Init, write fieldbus_config, compare the master's
checksums, master to Op, and make sure the active configuration holds the new files (the master
loads comm.ethercat.master/config/<instance>/ethercat.xml + ethercat.ini at boot). What doesn't work is a warning,
so a configuration can also be prepared while the master isn't working.
"""

import os
import time

from app import edits
from app import eni as eni_mod
from app import generate

PORTS = "ABCD"


class DownloadError(Exception):
    pass



def pair_in_order(old: list, new: list) -> dict:
    """Index in old -> index in new for the devices that are the same (equal identities, e.g. [vendor, product]),
    keeping the bus order: the longest common subsequence. A device inserted into or removed from the middle of
    the bus then leaves the devices behind it paired with themselves instead of with their neighbours."""
    n, m = len(old), len(new)
    best = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            best[i][j] = best[i + 1][j + 1] + 1 if old[i] == new[j] else max(best[i + 1][j], best[i][j + 1])
    out, i, j = {}, 0, 0
    while i < n and j < m:
        if old[i] == new[j]:
            out[i] = j
            i, j = i + 1, j + 1
        elif best[i + 1][j] >= best[i][j + 1]:
            i += 1
        else:
            j += 1
    return out


def merge(project: list, bus: list) -> list:
    """The device list for project and bus identities: (project index or None, bus index or None) in order.
    A project device with no partner is absent; a bus device with no partner stays at its place on the bus."""
    paired = pair_in_order(project, bus)
    taken = set(paired.values())
    out, i, j = [], 0, 0
    while i < len(project) or j < len(bus):
        if i < len(project) and i not in paired:
            out.append((i, None))
            i += 1
        elif j < len(bus) and j not in taken:
            out.append((None, j))
            j += 1
        else:
            out.append((i, j))
            i, j = i + 1, j + 1
    return out


def absent(src: dict) -> bool:
    return src.get("bus_pos", src["pos"]) is None


def _variables(parsed) -> dict:
    out = {}
    for s in (parsed or {}).get("slaves", []):
        for v in s["variables"]:
            out[v["name"]] = (v["dir"], v["type"], v["bits"], v["offset"])
    return out


def topology(sources: list) -> dict:
    """position -> (port letter, upstream position) from the master's online info: a device's port A leads to
    its upstream device (portSlaveHandles[0]), previousPort is the port it is plugged into there. An absent device
    has src["previous"] instead; a device on the bus whose port that one occupies chains after it (the place the
    device has on the bus now is only free because the other one is missing)."""
    by_handle = {}
    for src in sources:
        info = (src.get("online") or {}).get("response", {})
        if info.get("slaveHandle") is not None:
            by_handle[info["slaveHandle"]] = src["pos"]
    out = {}
    for src in sources:
        info = (src.get("online") or {}).get("response", {})
        handles, port = info.get("portSlaveHandles") or [], info.get("previousPort")
        if src["pos"] > 0 and handles and handles[0] in by_handle and port is not None and 0 <= port < 4:
            out[src["pos"]] = (PORTS[port], by_handle[handles[0]])
    occupied = set()
    for src in sources:
        pos = src["pos"]
        if absent(src):
            if src.get("previous"):
                out[pos] = tuple(src["previous"])
                occupied.add(out[pos])
        elif out.get(pos) in occupied:
            out[pos] = ("B", pos - 1)
    return out


def describe(src: dict, used: set = None):
    """(device, notes, warning, error) for one scanned position, before the user's edits: the ESI, else the
    current configuration, else the EEPROM; the current configuration's choices on top."""
    info = (src.get("online") or {}).get("response", {})
    idn = info.get("slaveIdentity", {})
    vendor, product, revision = idn.get("vendorId"), idn.get("productCode"), idn.get("revisionNumber")
    esi, slave = src.get("esi_device"), src.get("eni_slave")
    note, warning = [], None
    if esi is not None and src.get("esi_match") in ("exact", "nearest"):
        name = esi["type"]
        dev = generate.device_from_esi(esi, "")
        if src["esi_match"] == "nearest":       # I/O Engineering keeps the ESI's revision (and its checks)
            note.append(f'ESI of revision {esi["revision"]:#x}, the device has {revision:#x}')
    elif slave is not None and (slave["vendor"], slave["product"], slave["revision"]) == (vendor, product, revision):
        name = slave["name"]
        dev = generate.device_from_eni(slave)
        note.append("no ESI: taken from the current configuration")
    elif src.get("eeprom"):
        name = src["eeprom"].get("order") or src["eeprom"].get("name") or "Device"
        dev = generate.device_from_eeprom(src["eeprom"], "")
        note.append("learned from the EEPROM (no ESI)")
        warning = f'Position {src["pos"]}: no ESI, described from its EEPROM only (default PDOs, no DC)'
    else:
        return None, [], None, f'Position {src["pos"]}: no ESI and no EEPROM data, the device can\'t be described'
    if slave is not None and (slave["vendor"], slave["product"]) == (vendor, product):
        dev = generate.apply_eni(dev, slave)
        dev["revision"] = slave["revision"]
        note.insert(0, "kept from the current configuration")
    else:
        base = generate.device_name(name)
        dev["name"], i = base, 1
        while used is not None and dev["name"] in used:
            i += 1
            dev["name"] = f"{base}_{i}"
        note.insert(0, "new: ESI defaults" if dev["source"] == "esi" else "new")
    return dev, note, warning, None


def device_for(src: dict, user_edits: dict, used: set = None) -> dict:
    """The device as the configuration gets it, the user's edit for its position applied (None if it can't be described)."""
    dev, _note, _warning, _error = describe(src, used)
    edit = (user_edits or {}).get(src["pos"])
    if dev is not None and edit and edits.applies(src, edit):
        dev = edits.apply_edit(dev, src, edit)
    return dev


def build_plan(sources: list, current: dict, master_name: str, cycle_us: int = None, user_edits: dict = None) -> dict:
    """sources: per present device {"pos", "online", "eeprom", "eni_slave", "esi_match", "esi_device"};
    current: {"eni": bytes, "ini": bytes, "parsed": parse_eni() or None}; user_edits: position -> edit."""
    devices, rows, errors, warnings = [], [], [], []
    used = {src["eni_slave"]["name"] for src in sources if src.get("eni_slave")}       # also of devices further down
    ports = topology(sources)
    for src in sources:
        pos = src["pos"]
        dev, note, warning, error = describe(src, used)
        if error:
            errors.append(error)
            rows.append({"position": pos, "name": "?", "source": None, "note": "", "inputs": 0, "outputs": 0})
            continue
        if warning:
            warnings.append(warning)
        if absent(src):
            warnings.append(f'Position {pos}: {dev["name"]} is not present on the bus')
        edit = (user_edits or {}).get(pos)
        if edit and edits.applies(src, edit):
            try:
                dev = edits.apply_edit(dev, src, edit)
                note.append("edited")
            except edits.EditError as e:
                errors.append(f"Position {pos}: {e}")
        elif edit:
            warnings.append(f"Position {pos}: the saved edit was made for another device and is not used")
        used.add(dev["name"])
        devices.append(dev)
        rows.append({"position": pos, "name": dev["name"], "source": dev["source"], "note": "; ".join(note),
                     "present": not absent(src),
                     "inputs": sum(e["bits"] for p in dev["pdos"] if p["dir"] == "tx" for e in p["entries"]) // 8,
                     "outputs": sum(e["bits"] for p in dev["pdos"] if p["dir"] == "rx" for e in p["entries"]) // 8})

    ini_text = (current.get("ini") or b"").decode("latin-1")
    cycle = cycle_us or int(eni_mod.parse_ini(ini_text).get("EtherCAT.Master", {}).get("CycleTimeUs", 0) or 0) or 2000
    eni_bytes = ini_bytes = None
    if not errors:
        try:
            eoe = generate.eoe_addresses(eni_mod.parse_ini(ini_text or generate.DEFAULT_INI), devices)
            eni_bytes = generate.generate_eni(devices, master_name, cycle, ports, eoe)
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


def write_config(bus, base: str, eni: bytes, ini: bytes, active_files: tuple, log, op_timeout=60) -> tuple:
    """The download sequence, best effort: returns (steps done, warnings, whether the master holds the configuration).
    DownloadError only if neither the master nor the files took it. active_files: (eni path, ini path) of the
    master's files in the active configuration."""
    steps, warnings = [], []

    def step(text):
        steps.append(text)
        log(f"download: {text}")

    def warn(text):
        warnings.append(text)
        log(f"download: {text}")

    try:
        bus.write(base + "admin/fieldbus_status/master_state", {"request": {"newState": "init"}})
        _wait_state(bus, base, "init", 30, log)
        step("master in Init")
    except Exception as e:
        warn(f"master not in Init: {e}")
    took = False
    try:
        bus.write(base + "admin/fieldbus_config", {"request": {"eni": list(eni), "ini": list(ini)}})
        step(f"configuration written ({len(eni)} bytes ENI, {len(ini)} bytes ini)")
        sums = {c["id"]: c["checksum"] for c in
                bus.read(base + "admin/fieldbus_config/checksum").get("response", {}).get("checksums", [])}
        if sums.get("eniCrc32") == generate.checksum(eni) and sums.get("iniCrc32") == generate.checksum(ini):
            took = True
            step("master checksums match")
        else:
            warn(f"the master reports other checksums than the files written ({sums})")
    except Exception as e:
        warn(f"the master did not take the configuration: {e}")
    try:
        bus.write(base + "admin/fieldbus_status/master_state", {"request": {"newState": "op"}})
        _wait_state(bus, base, "op", op_timeout, log)
        step("master in Op")
    except Exception as e:
        warn(f"master not in Op: {e}")
    eni_path, ini_path = active_files
    if took:
        time.sleep(1.0)
    if _same_file(eni_path, eni) and _same_file(ini_path, ini):
        step("the master stored the files in the active configuration itself")
    else:
        try:
            os.makedirs(os.path.dirname(eni_path), exist_ok=True)
            for path, data in ((eni_path, eni), (ini_path, ini)):
                tmp = path + ".tmp"
                with open(tmp, "wb") as f:
                    f.write(data)
                os.replace(tmp, path)
            step("files written to the active configuration (loaded at the next boot)")
        except OSError as e:
            if not took:
                raise DownloadError(f'neither the master nor the files took the configuration: {"; ".join(warnings)}; {e}') from e
            warn(f"files not written to the active configuration: {e}")
    if not took:
        warn("the master did not take the configuration: it takes effect at the next master start")
    return steps, warnings, took


def _same_file(path, data) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read() == data
    except OSError:
        return False
