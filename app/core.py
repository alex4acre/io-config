# SPDX-License-Identifier: MIT

"""The app's state: the configuration (config.json in the active configuration), the ESI library and the
last bus scan.

A scan only reads: the master's status, the configuration it runs with (ENI + ini), and per bus position
the online info and the EEPROM. EEPROMs are read in small chunks (one large read timed out on a real
ctrlX CORE X3 and logged an error) and cached per device, so a rescan doesn't read them again unless asked.
The number of devices comes from num_connected_slaves: asking past the last device makes the master log
"Device not found".

The device list (_assemble) is the running configuration's devices merged with the bus, until the user adds, removes
or moves a device. Then io-config/project.json holds the list ("Configuration" state): exactly the list, in the order
that is the wiring order, with no live data and no online access; a scan only pairs its devices with the list. A device
missing on the bus is "absent": described from its ESI or the configuration. A master that can't be read is no
obstacle: the scan then reports it and the list comes from the master's files in the active configuration.

File handling as in the ctrlx-app-starter template:
- config.json is written atomically (config.json.tmp, then rename; *.tmp is an appPrivateFile in the manifest,
  so it never ends up in a saved configuration);
- a file replaced from outside (Manage app data, loading a configuration or a backup) is picked up by
  reload_if_changed() and never overwritten with the old state.
"""

import json
import os
import re
import shutil
import threading
import time

from app import coe, compare, configure, edits, eni, generate, sii, soe
from app.esi import EsiLibrary

DEFAULTS = {"instance": "ethercatmaster"}
INSTANCES = "fieldbuses/ethercat/master/instances"
EEPROM_CHUNK_WORDS = 64
EEPROM_MAX_WORDS = 8192
MAX_BACKUPS = 20
CONFIGURING = ("the list is a configuration now: online access is off until it is written or you go back to "
               "the running configuration")
SAVED_NAME = re.compile(r"[A-Za-z0-9_\-][A-Za-z0-9 _.\-]{0,63}")



def _identity(online: dict) -> list:
    info = (online or {}).get("response", {}).get("slaveIdentity", {})
    return [info.get("vendorId"), info.get("productCode")]

def show_startup(rows: list, dictionary: dict) -> list:
    """Startup commands with "show": the value for people (the "data" hex stays in device byte order)."""
    out = []
    for c in rows:
        try:
            data = bytes.fromhex(c["data"])
        except (KeyError, TypeError, ValueError):
            out.append(c)
            continue
        if "idn" in c:
            a = (dictionary.get(c["idn"]) or {}).get("attribute")
            text = soe.show(data, soe.attribute(a)) if a is not None else coe.show(data)
        else:
            text = coe.show(data, c.get("data_type"), c.get("bits") or 0)
        out.append(dict(c, show=text))
    return out


class ConfigError(ValueError):
    pass


def validate(cfg: dict) -> dict:
    """The complete configuration from a (partial) one; ConfigError if a value is wrong."""
    out = dict(DEFAULTS)
    for key, value in cfg.items():
        if key not in DEFAULTS:
            raise ConfigError(f"unknown setting {key!r}")
        out[key] = value
    if not isinstance(out["instance"], str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,14}", out["instance"]):
        raise ConfigError('"instance" must be an EtherCAT master instance name')
    return out


def _bytes(value) -> bytes:
    return bytes(value or [])


def _device_names(eni_bytes) -> list:
    try:
        return [s["name"] for s in eni.parse_eni(eni_bytes)["slaves"]]
    except Exception:
        return []


class Core:

    def __init__(self, config_path: str, bus, esi_folder: str, log=print):
        self.config_path = config_path
        self.bus = bus
        self.log = log
        self.lock = threading.RLock()
        self.config = self._load()
        self.file_id = self._file_id()
        self.esi = EsiLibrary(esi_folder, log=log)
        self.scan_state = {"running": False, "progress": "", "finished": None, "error": None}
        self.master = None           # status of the master at the last scan
        self.devices = []            # compare.device_report() per position
        self.sources = []            # per device on the bus: online info, EEPROM, ENI slave, ESI device
        self.current = None          # {"eni", "ini", "parsed"}: the configuration the master ran at the last scan
        self.eeprom_cache = {}       # (position, vendor, product, revision, serial) -> words
        self.plan_data = None        # the last configuration built from a scan (what a download writes)
        self.plan_id = 0
        self.download_state = {"running": False, "steps": [], "warnings": [], "finished": None, "error": None}
        self._thread = None
        # the active configuration: io-config/config.json is in it, and the master's files too
        self.active_dir = os.path.dirname(os.path.dirname(os.path.abspath(config_path)))
        self.backup_dir = os.path.join(os.path.dirname(os.path.abspath(config_path)), "backups")
        self.saved_dir = os.path.join(os.path.dirname(os.path.abspath(config_path)), "saved")
        self.activation = {"state": "idle", "name": "", "error": "", "started": None, "finished": None}
        self.edits_path = os.path.join(os.path.dirname(os.path.abspath(config_path)), "edits.json")
        self.od_cache = {}           # (position, identity) -> object list
        self.project_path = os.path.join(os.path.dirname(os.path.abspath(config_path)), "project.json")
        self.raw = None              # the last scan, unmerged: master, onlines, eeproms, configured, current
        self.configuring = False     # the list is a project: a configuration, no live data
        self.unlisted = []           # bus devices that are not in the project (descriptions)
        self._start_from_files()

    # --- configuration file ------------------------------------------------------------------------

    def _load(self) -> dict:
        try:
            with open(self.config_path, encoding="utf-8") as f:
                return validate(json.load(f))
        except FileNotFoundError:
            return dict(DEFAULTS)
        except (ValueError, OSError) as e:
            self.log(f"{self.config_path}: {e}; using the defaults")
            return dict(DEFAULTS)

    def _save(self):
        os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
        tmp = self.config_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.config, f, indent=2)
        os.replace(tmp, self.config_path)
        self.file_id = self._file_id()

    def _file_id(self):
        try:
            st = os.stat(self.config_path)
            return st.st_ino, st.st_mtime_ns, st.st_size
        except FileNotFoundError:
            return None

    def reload_if_changed(self) -> bool:
        """Pick up a config.json replaced from outside (Manage app data, loading a configuration)."""
        with self.lock:
            if self._file_id() == self.file_id:
                return False
            self.log("config.json changed, reloading")
            self.config = self._load()
            self.file_id = self._file_id()
            return True

    def update_config(self, changes: dict) -> dict:
        with self.lock:
            self.config = validate({**self.config, **changes})
            self._save()
            return dict(self.config)

    # --- API used by web.py -----------------------------------------------------------------------

    def instances(self) -> list:
        try:
            return self.bus.browse(INSTANCES)
        except Exception:
            return []

    def state(self) -> dict:
        with self.lock:
            return {
                "connected": self.bus.is_connected(),
                "instance": self.config["instance"],
                "scan": dict(self.scan_state),
                "download": dict(self.download_state, steps=list(self.download_state["steps"]),
                                 warnings=list(self.download_state["warnings"])),
                "activation": dict(self.activation),
                "master": self.master,
                "project": self._load_project() is not None,
                "devices": [self._summary(d) for d in self.devices],
                "esi": self.esi.summary(),
            }

    @staticmethod
    def _summary(d: dict) -> dict:
        keys = ("position", "name", "description", "address", "vendor", "product", "revision", "present",
                "state", "level")
        out = {k: d[k] for k in keys}
        out["sources"] = {k: v["match"] for k, v in d["sources"].items()}
        out["findings"] = len([f for f in d["findings"] if f["level"] != "info"])
        return out

    def device(self, position: int) -> dict:
        with self.lock:
            for d in self.devices:
                if d["position"] == position:
                    dictionary = next((x.get("soe_dict") for x in self.sources if x["pos"] == position), None) or {}
                    sources = {k: dict(v, coe_init=show_startup(v["coe_init"], dictionary)) if "coe_init" in v else v
                               for k, v in d["sources"].items()}
                    return dict(d, sources=sources)
        raise KeyError(f"no device at position {position}")

    def _busy(self) -> bool:
        return self.scan_state["running"] or self.download_state["running"]

    def start_scan(self, reread_eeprom=False) -> dict:
        with self.lock:
            if self._busy():
                raise ConfigError("a scan or download is already running")
            self.scan_state = {"running": True, "progress": "starting", "finished": None, "error": None}
            if reread_eeprom:
                self.eeprom_cache.clear()
            self._thread = threading.Thread(target=self._scan_thread, name="scan", daemon=True)
            self._thread.start()
            return dict(self.scan_state)

    def wait_scan(self, timeout=60):
        t = self._thread
        if t is not None:
            t.join(timeout)

    def stop(self):
        self.wait_scan(10)

    # --- scan ------------------------------------------------------------------------------------

    def _progress(self, text):
        with self.lock:
            self.scan_state["progress"] = text

    def _scan_thread(self):
        error = None
        try:
            raw = self._scan()
            with self.lock:
                self.raw = raw
                self._assemble()
                self.plan_data = None
            error = raw["master"]["error"]
            if error:
                self.log(f"scan: {error}")
        except Exception as e:      # shown in the UI; the last good scan stays
            error = str(e)
            self.log(f"scan failed: {e}")
        with self.lock:
            self.scan_state = {"running": False, "progress": "", "finished": time.time(), "error": error}

    def _active_files(self) -> tuple:
        folder = os.path.join(self.active_dir, "comm.ethercat.master", "config", self.config["instance"])
        return os.path.join(folder, "ethercat.xml"), os.path.join(folder, "ethercat.ini")

    def _read_active_files(self) -> tuple:
        """(ENI, ini) the master loads at boot, empty if there are none."""
        out = []
        for path in self._active_files():
            try:
                with open(path, "rb") as f:
                    out.append(f.read())
            except OSError:
                out.append(b"")
        return tuple(out)

    def _start_from_files(self):
        """Before the first scan: the list as the active configuration's files and the project give it."""
        try:
            eni_bytes, ini_bytes = self._read_active_files()
            parsed = eni.parse_eni(eni_bytes) if eni_bytes else None
            self.raw = {"master": None, "onlines": [], "eeproms": [],
                        "configured": sorted((parsed or {}).get("slaves", []), key=lambda s: s["position"]),
                        "current": {"eni": eni_bytes, "ini": ini_bytes, "parsed": parsed} if eni_bytes else None}
            self._assemble()
        except Exception as e:
            self.raw = {"master": None, "onlines": [], "eeproms": [], "configured": [], "current": None}
            self.log(f"active configuration not readable: {e}")

    def _scan(self) -> dict:
        base = f'{INSTANCES}/{self.config["instance"]}/'
        self._progress("master status")
        master = {"state": "unavailable", "requested": "", "connected": 0, "configured": 0, "topology": "",
                  "eni": None, "ini": {}, "eni_error": None, "error": None, "from_files": False}
        try:
            if self.config["instance"] not in self.instances():
                raise ConfigError(f'no EtherCAT master instance "{self.config["instance"]}" '
                                  f'(found: {", ".join(self.instances()) or "none"})')
            state = self.bus.read(base + "admin/fieldbus_status/master_state").get("response", {})
            connected = self.bus.read(base + "admin/fieldbus_status/num_connected_slaves").get("response", {}).get("numSlaves", 0)
            configured = self.bus.read(base + "admin/fieldbus_status/num_configured_slaves").get("response", {}).get("numSlaves", 0)
            topology = self.bus.read(base + "admin/fieldbus_status/topology_status").get("response", {})
            master.update(state=state.get("currentState", ""), requested=state.get("requestedState", ""),
                          connected=connected, configured=configured, topology=topology.get("state", ""))
        except Exception as e:
            master["error"] = str(e)
        connected = master["connected"]

        self._progress("configuration (ENI)")
        config = None
        eni_bytes = ini_bytes = b""
        try:
            if not master["error"]:
                cfg = self.bus.read(base + "admin/fieldbus_config").get("response", {})
                eni_bytes, ini_bytes = _bytes(cfg.get("eni")), _bytes(cfg.get("ini"))
            if not eni_bytes:
                eni_bytes, ini_bytes = self._read_active_files()
                master["from_files"] = bool(eni_bytes)
            if eni_bytes:
                config = eni.parse_eni(eni_bytes)
                master["eni"] = {"slaves": len(config["slaves"]), "inputs_bytes": config["inputs_bytes"],
                                 "outputs_bytes": config["outputs_bytes"], "size": len(eni_bytes)}
            master["ini"] = eni.parse_ini(ini_bytes.decode("latin-1"))
        except Exception as e:
            master["eni_error"] = str(e)
        cycle = master["ini"].get("EtherCAT.Master", {}).get("CycleTimeUs")
        master["cycle_us"] = int(cycle) if cycle and cycle.isdigit() else None
        configured_slaves = sorted((config or {}).get("slaves", []), key=lambda s: s["position"])
        if master["error"]:
            master["configured"] = len(configured_slaves)

        onlines, eeproms = [], []
        for pos in range(connected):
            self._progress(f"device {pos + 1} of {connected}")
            address = {"addressType": "autoincrement", "address": (-pos) & 0xFFFF}
            try:
                online = self.bus.read_args(base + "device_access/slave_online_info", {"request": address})
            except Exception as e:      # the devices behind it can't be asked either
                master["error"] = f"device {pos} not readable: {e}"
                break
            identity = online.get("response", {}).get("slaveIdentity", {})
            key = (pos, identity.get("vendorId"), identity.get("productCode"), identity.get("revisionNumber"),
                   identity.get("serialNumber"))
            eeprom, eeprom_error = None, None
            try:
                if key not in self.eeprom_cache:
                    self.eeprom_cache[key] = self._read_eeprom(base, address)
                eeprom = sii.decode(self.eeprom_cache[key])
            except Exception as e:
                eeprom_error = str(e)
            onlines.append(online)
            eeproms.append((eeprom, eeprom_error))
        return {"master": master, "onlines": onlines, "eeproms": eeproms, "configured": configured_slaves,
                "current": {"eni": eni_bytes, "ini": ini_bytes, "parsed": config}}

    def _assemble(self):
        """self.devices and self.sources from the last scan and the project (no bus access). A position is the
        place in this list; src["bus_pos"] is the device's place on the bus, None if it is absent. With a project the
        list is the project's entries only, paired with the bus devices in order; without one it is the running
        configuration merged with the bus."""
        raw = self.raw
        project = self._load_project()
        configured = raw["configured"]
        bus_ids = [_identity(o) for o in raw["onlines"]]
        if project is not None:
            entries = project["devices"]
            ids = [[e["vendor"], e["product"]] for e in entries]
            on_bus = configure.pair_in_order(ids, bus_ids)
            walk = [(e, on_bus.get(e)) for e in range(len(entries))]
        else:
            entries = [{"vendor": s["vendor"], "product": s["product"], "revision": s["revision"]} for s in configured]
            walk = configure.merge([[e["vendor"], e["product"]] for e in entries], bus_ids)
            ids = [bus_ids[b] if b is not None else [entries[e]["vendor"], entries[e]["product"]] for e, b in walk]
        # the running configuration's device for each position: same type, in bus order (not the same position,
        # so a device inserted before others doesn't make them lose their settings)
        paired = configure.pair_in_order([[s["vendor"], s["product"]] for s in configured], ids)
        eni_slaves = {paired[k]: s for k, s in enumerate(configured) if k in paired}
        if project is not None:                # a moved device keeps its settings: linked by its station address
            by_address = {s["address"]: s for s in configured}
            for pos, e in enumerate(entries):
                if "address" in e:
                    slave = by_address.get(e["address"])
                    if slave and [slave["vendor"], slave["product"]] == ids[pos]:
                        eni_slaves[pos] = slave
                    else:
                        eni_slaves.pop(pos, None)
        uid_pos = {entries[e]["uid"]: pos for pos, (e, _b) in enumerate(walk) if e is not None and "uid" in entries[e]}
        address_pos = {s["address"]: paired[k] for k, s in enumerate(configured) if k in paired}

        master_op = (raw["master"] or {}).get("state") == "op"
        devices, sources = [], []
        for pos, (e, b) in enumerate(walk):
            entry = entries[e] if e is not None else {}
            slave = eni_slaves.get(pos)
            if b is not None:
                online = raw["onlines"][b]
                eeprom, eeprom_error = raw["eeproms"][b]
            else:
                online = {"response": {"slaveIdentity": {"vendorId": entry["vendor"], "productCode": entry["product"],
                                                         "revisionNumber": entry["revision"], "serialNumber": 0}}}
                eeprom, eeprom_error = None, None
            if project is not None:            # no live data: the report knows the list's identity (the EEPROM is shown, not compared)
                listed = {"response": {"slaveIdentity": {"vendorId": entry["vendor"], "productCode": entry["product"],
                                                         "revisionNumber": entry["revision"], "serialNumber": 0}}}
                report, match, device = self._report(pos, listed, eeprom, eeprom_error, slave, master_op, absent=b is None,
                                                     configuration=True)
            else:
                report, match, device = self._report(pos, online, eeprom, eeprom_error, slave, master_op, absent=b is None)
            devices.append(report)
            src = {"pos": pos, "bus_pos": b, "uid": entry.get("uid"), "online": online, "eeprom": eeprom,
                   "eni_slave": slave, "esi_match": match, "esi_device": device}
            if b is None:
                src["previous"] = self._previous_port(pos, entry, slave, uid_pos, address_pos)
            sources.append(src)
            if device and device.get("dictionary"):          # SoE drives: their parameter dictionary
                try:
                    src["soe_dict"] = self.esi.dictionary(device["dictionary"])
                except KeyError as err:
                    self.log(str(err))
        self.unlisted = []
        if project is not None:
            for b in sorted(set(range(len(bus_ids))) - set(on_bus.values())):
                eeprom, eeprom_error = raw["eeproms"][b]
                d = self._report(0, raw["onlines"][b], eeprom, eeprom_error, None, master_op)[0]
                self.unlisted.append(d["description"] or d["name"])
        self.configuring = project is not None
        self.master, self.devices, self.sources, self.current = raw["master"], devices, sources, raw["current"]
        self._follow_edits()
        self._edit_names()

    def _edit_names(self):
        """The names in the device list: the saved edit's name, else the configuration's or the ESI's."""
        user = self.load_edits()
        for d, src in zip(self.devices, self.sources):
            edit = user.get(src["pos"])
            if edit and edit.get("name") and edits.applies(src, edit):
                d["name"] = edit["name"]

    @staticmethod
    def _previous_port(pos, entry, slave, uid_pos, address_pos):
        """(port letter, upstream position) of an absent device, None if unknown: a chain after the device before it."""
        if "uid" in entry:
            port, upstream = entry.get("port"), uid_pos.get(entry.get("upstream"))
        elif slave and slave.get("previous"):
            port, upstream = slave["previous"][0], address_pos.get(slave["previous"][1])
        else:
            return None
        return (port, upstream) if port and port in configure.PORTS and upstream is not None and upstream < pos else None

    def _read_eeprom(self, base: str, address: dict) -> list:
        words = []
        while len(words) < EEPROM_MAX_WORDS:
            answer = self.bus.read_args(base + "device_access/slave_eeprom", {"request": {
                **address, "eepromOffset": len(words), "maxLength": EEPROM_CHUNK_WORDS}})
            chunk = answer.get("response", {}).get("data") or []
            if not chunk:
                break
            words += chunk
            if sii.end_reached(words):
                break
        return words

    def _report(self, pos, online, eeprom, eeprom_error, eni_slave, master_op, absent=False, configuration=False):
        identity = online.get("response", {}).get("slaveIdentity", {})
        vendor, product, revision = identity.get("vendorId"), identity.get("productCode"), identity.get("revisionNumber")
        match, entry, device, esi_error = self.esi.find(vendor, product, revision) + (None, None)
        if entry is not None:
            try:
                device = self.esi.device(entry)
            except Exception as e:
                esi_error = str(e)
        return compare.device_report(pos, online, eeprom, eeprom_error, eni_slave, match, entry, device,
                                     esi_error=esi_error, master_op=master_op, absent=absent,
                                     configuration=configuration), match, device

    # --- the project: the devices the configuration is built for (io-config/project.json) ---------------

    def _load_project(self):
        """{"eni_checksum", "devices": [{uid, vendor, product, revision, port, upstream (uid)}]}, None without one."""
        try:
            with open(self.project_path, encoding="utf-8") as f:
                data = json.load(f)
            for e in data["devices"]:
                e["vendor"], e["product"], e["revision"], e["uid"]      # all of them or no project
            return data
        except FileNotFoundError:
            return None
        except (ValueError, OSError, KeyError, TypeError) as e:
            self.log(f"{self.project_path}: {e!r}; ignoring the project")
            return None

    def _save_project(self, devices: list):
        old = self._load_project()
        running = (self.current or {}).get("eni")
        data = {"eni_checksum": old["eni_checksum"] if old else generate.checksum(running) if running else None,
                "devices": devices}
        os.makedirs(os.path.dirname(self.project_path), exist_ok=True)
        tmp = self.project_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self.project_path)

    def _project_changed(self):
        self._assemble()
        self.plan_data = None

    def _project_entries(self) -> list:
        """The device list now as project entries; every device gets a uid. Without a project the ports come from
        the master's topology; with one the entries' own upstream and port stay (they are the wiring)."""
        project = self._load_project()
        used = {s["uid"] for s in self.sources if s["uid"] is not None}
        fresh = iter(n for n in range(len(used) + len(self.sources) + 1) if n not in used)
        uids = [s["uid"] if s["uid"] is not None else next(fresh) for s in self.sources]
        ports = configure.topology(self.sources)
        out = []
        for i, (s, uid) in enumerate(zip(self.sources, uids)):
            idn = s["online"]["response"]["slaveIdentity"]
            port, upstream = ports.get(s["pos"], (None, None))
            slave = s.get("eni_slave")
            entry = {"uid": uid, "vendor": idn["vendorId"], "product": idn["productCode"], "revision": idn["revisionNumber"],
                     "port": port, "upstream": None if upstream is None else uids[upstream],
                     "address": slave["address"] if slave else None}      # the running configuration's device
            if project is not None:
                entry["port"], entry["upstream"] = project["devices"][i]["port"], project["devices"][i]["upstream"]
            out.append(entry)
        return out

    def _project_edit(self):
        if self._busy():
            raise ConfigError("a scan or download is running")
        return self._project_entries()

    def _check_name(self, name):
        if name not in (None, "") and (not isinstance(name, str) or not edits.NAME_RE.fullmatch(name)):
            raise ConfigError("the name must start with a letter and contain only letters, digits and _ (max 64)")

    def project_add(self, vendor, product, revision, name=None) -> int:
        """Appends a device from the ESI library at the bottom of the list (plugged into port B of the device
        above it) and saves its name, if given. Returns its position."""
        with self.lock:
            entries = self._project_edit()
            if self.esi.find(vendor, product, revision)[0] != "exact":
                raise ConfigError("no such device in the ESI library")
            self._check_name(name)
            uid = max((e["uid"] for e in entries), default=-1) + 1
            entries.append({"uid": uid, "vendor": vendor, "product": product, "revision": revision,
                            "port": "B" if entries else None, "upstream": entries[-1]["uid"] if entries else None,
                            "address": None})
            self._save_project(entries)
            self._remap_edits(list(range(len(entries) - 1)) + [None], entries)
            self._project_changed()
            if name:
                self.save_edit(len(entries) - 1, {"name": name})
            return len(entries) - 1

    def project_remove(self, position: int):
        """Takes any device out of the list; the devices behind it close up."""
        with self.lock:
            entries = self._project_edit()
            if not isinstance(position, int) or not 0 <= position < len(entries):
                raise ConfigError(f"no device at position {position}")
            configure.remove_entry(entries, position)
            self._save_project(entries)
            self._remap_edits([i for i in range(len(entries) + 1) if i != position], entries)
            self._project_changed()

    def project_move(self, position: int, by: int) -> int:
        """Moves any device up (negative) or down in the list; returns its new position."""
        with self.lock:
            entries = self._project_edit()
            if not isinstance(position, int) or not 0 <= position < len(entries):
                raise ConfigError(f"no device at position {position}")
            if not isinstance(by, int) or isinstance(by, bool):
                raise ConfigError("by must be a number of places")
            target = max(0, min(len(entries) - 1, position + by))
            if target == position:
                return position
            configure.move_entry(entries, position, target)
            self._save_project(entries)
            order = list(range(len(entries)))
            order.insert(target, order.pop(position))
            self._remap_edits(order, entries)
            self._project_changed()
            return target

    def project_reset(self):
        """Back to the running configuration's devices."""
        with self.lock:
            if self._busy():
                raise ConfigError("a scan or download is running")
            if os.path.exists(self.project_path):
                os.remove(self.project_path)
            self._project_changed()

    # --- new configuration and download -------------------------------------------------------------

    def plan(self, cycle_us=None) -> dict:
        """The configuration the device list gets (not written yet); no scan needed."""
        with self.lock:
            if self._busy():
                raise ConfigError("a scan or download is running")
            if cycle_us is not None and (not isinstance(cycle_us, int) or isinstance(cycle_us, bool)
                                         or not 125 <= cycle_us <= 100000):
                raise ConfigError("cycle time must be 125 to 100000 µs")
            current = self.current or {"eni": b"", "ini": b"", "parsed": None}
            p = configure.build_plan(self.sources, current, self.config["instance"], cycle_us, self.load_edits(),
                                     self.unlisted if self.configuring else None)
            project = self._load_project()
            if project and current["eni"] and project.get("eni_checksum") != generate.checksum(current["eni"]):
                p["warnings"].append("The master's configuration changed since this project was started")
            self.plan_id += 1
            self.plan_data = dict(p, id=self.plan_id)
            return self.plan_summary()

    def plan_summary(self) -> dict:
        p = self.plan_data
        if p is None:
            raise KeyError("no configuration built")
        return {k: v for k, v in p.items() if k not in ("eni", "ini")} | {
            "eni_size": len(p["eni"] or b""), "ini_text": (p["ini"] or b"").decode("latin-1")}

    def plan_file(self, which: str) -> bytes:
        p = self.plan_data
        if p is None or not p.get(which):
            raise KeyError("no configuration built")
        return p[which]

    def start_download(self, plan_id: int) -> dict:
        with self.lock:
            if self._busy():
                raise ConfigError("a scan or download is already running")
            p = self.plan_data
            if p is None or p["id"] != plan_id:
                raise ConfigError("the configuration changed; build it again")
            if p["errors"] or not p["eni"]:
                raise ConfigError("the configuration has errors: " + "; ".join(p["errors"]))
            self.download_state = {"running": True, "steps": [], "warnings": [], "finished": None, "error": None}
            self._thread = threading.Thread(target=self._download_thread, args=(p["eni"], p["ini"], "download"),
                                            name="download", daemon=True)
            self._thread.start()
            return dict(self.download_state)

    def start_restore(self, name: str) -> dict:
        folder = self._backup_folder(name)
        with open(os.path.join(folder, "ethercat.xml"), "rb") as f:
            eni_bytes = f.read()
        with open(os.path.join(folder, "ethercat.ini"), "rb") as f:
            ini_bytes = f.read()
        with self.lock:
            if self._busy():
                raise ConfigError("a scan or download is already running")
            self.download_state = {"running": True, "steps": [], "warnings": [], "finished": None, "error": None}
            self._thread = threading.Thread(target=self._download_thread, args=(eni_bytes, ini_bytes, f"restore {name}"),
                                            name="download", daemon=True)
            self._thread.start()
            return dict(self.download_state)

    def _running_config(self, steps: list) -> dict:
        """What to back up: read from the master, else the last scan's, else the active configuration's files."""
        known = self.current or {}
        try:
            cfg = self.bus.read(f'{INSTANCES}/{self.config["instance"]}/admin/fieldbus_config').get("response", {})
            running = {"eni": _bytes(cfg.get("eni")), "ini": _bytes(cfg.get("ini"))}
        except Exception as e:
            self.log(f"the master's configuration not readable: {e}")
            running = {}
        if running.get("eni"):
            if known.get("eni") and known["eni"] != running["eni"]:
                steps.append("note: the master's configuration changed since the last scan")
            return running
        if known.get("eni"):
            return known
        eni_bytes, ini_bytes = self._read_active_files()
        return {"eni": eni_bytes, "ini": ini_bytes}

    def _download_thread(self, eni_bytes, ini_bytes, what, after=None):
        """after(): run once the configuration is written. Warnings: what didn't work, the configuration is at least
        in the files; an error: it isn't anywhere."""
        base = f'{INSTANCES}/{self.config["instance"]}/'
        error = None
        steps, warnings = self.download_state["steps"], self.download_state["warnings"]
        try:
            running = self._running_config(steps)
            if running.get("eni"):
                steps.append(f'backup {self._backup(running["eni"], running["ini"], what)}')
            done, warned, took = configure.write_config(self.bus, base, eni_bytes, ini_bytes, self._active_files(), self.log)
            steps += done
            warnings += warned
            if took and os.path.exists(self.project_path):      # the master holds it now: the project is done
                os.remove(self.project_path)
            if after:
                steps.append(after())
        except Exception as e:
            error = str(e)
            self.log(f"{what} failed: {e}")
        with self.lock:
            self.download_state = {"running": False, "steps": steps, "warnings": warnings, "finished": time.time(), "error": error}
            self.plan_data = None
            if what.startswith("activate "):
                problem = error or "; ".join(warnings)
                self.activation.update(state="error" if problem else "done", error=problem, finished=time.time())
        self.start_scan()

    # --- saved configurations (io-config/saved/<name>/ in the active configuration) -------------------

    def _saved_folder(self, name: str, exists=True) -> str:
        if not isinstance(name, str) or not SAVED_NAME.fullmatch(name):
            raise ConfigError("a name has 1 to 64 letters, digits, spaces, '-', '_' or '.', and doesn't start with '.' or a space")
        folder = os.path.join(self.saved_dir, name)
        if exists and not os.path.isfile(os.path.join(folder, "ethercat.xml")):
            raise KeyError(f"no saved configuration {name}")
        return folder

    def saved(self) -> list:
        """The saved configurations, newest first; "active": the master runs that ENI (as of the last scan)."""
        running = (self.current or {}).get("eni")
        out = []
        try:
            names = os.listdir(self.saved_dir)
        except FileNotFoundError:
            return []
        for name in names:
            folder = os.path.join(self.saved_dir, name)
            try:
                with open(os.path.join(folder, "info.json"), encoding="utf-8") as f:
                    info = json.load(f)
                with open(os.path.join(folder, "ethercat.xml"), "rb") as f:
                    same = running is not None and f.read() == running
            except (OSError, ValueError):
                continue
            out.append({"name": name, "time": info.get("time"), "devices": info.get("devices", []),
                        "edits": info.get("edits", 0), "active": same})
        return sorted(out, key=lambda x: x["time"] or 0, reverse=True)

    def save_current(self, name: str, overwrite=False) -> dict:
        """Saves what the master runs now (read from it, not from the last scan) and the app's edits."""
        folder = self._saved_folder(name, exists=False)
        if os.path.exists(folder) and not overwrite:
            raise ConfigError(f'a configuration named "{name}" is saved already')
        cfg = self.bus.read(f'{INSTANCES}/{self.config["instance"]}/admin/fieldbus_config').get("response", {})
        eni_bytes, ini_bytes = _bytes(cfg.get("eni")), _bytes(cfg.get("ini"))
        if not eni_bytes:
            raise ConfigError("the EtherCAT master runs no configuration (no ENI)")
        all_edits = self.load_edits()
        tmp = folder + ".tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp)
        for file, data in (("ethercat.xml", eni_bytes), ("ethercat.ini", ini_bytes)):
            with open(os.path.join(tmp, file), "wb") as f:
                f.write(data)
        with open(os.path.join(tmp, "edits.json"), "w", encoding="utf-8") as f:
            json.dump({str(k): v for k, v in sorted(all_edits.items())}, f, indent=2)
        with open(os.path.join(tmp, "info.json"), "w", encoding="utf-8") as f:
            json.dump({"time": time.time(), "edits": len(all_edits),
                       "devices": [s["name"] for s in eni.parse_eni(eni_bytes)["slaves"]]}, f)
        shutil.rmtree(folder, ignore_errors=True)
        os.replace(tmp, folder)
        return next(x for x in self.saved() if x["name"] == name)

    def delete_saved(self, name: str):
        shutil.rmtree(self._saved_folder(name))

    def saved_file(self, name: str, which: str) -> bytes:
        with open(os.path.join(self._saved_folder(name), "ethercat.xml" if which == "eni" else "ethercat.ini"), "rb") as f:
            return f.read()

    def start_activate(self, name: str) -> dict:
        """Writes a saved configuration to the master (after a backup of the running one) and brings back the edits
        saved with it. Works without a scan (REST / Data Layer); waits for a scan that is running."""
        folder = self._saved_folder(name)
        eni_bytes = self.saved_file(name, "eni")
        ini_bytes = self.saved_file(name, "ini")
        with open(os.path.join(folder, "edits.json"), encoding="utf-8") as f:
            saved_edits = json.load(f)

        def restore_edits():
            self._save_edits({int(k): v for k, v in saved_edits.items()})
            return f"edits of {name} restored ({len(saved_edits)})"

        def run():
            end = time.time() + 120
            while self.scan_state["running"] and time.time() < end:
                time.sleep(0.2)
            self._download_thread(eni_bytes, ini_bytes, f"activate {name}", after=restore_edits)

        with self.lock:
            if self.download_state["running"]:
                raise ConfigError("a configuration is being written already")
            self.download_state = {"running": True, "steps": [], "warnings": [], "finished": None, "error": None}
            self.activation = {"state": "running", "name": name, "error": "", "started": time.time(), "finished": None}
            self._thread = threading.Thread(target=run, name="activate", daemon=True)
            self._thread.start()
            return dict(self.activation)

    # --- the user's edits per position (io-config/edits.json in the active configuration) -------------

    def load_edits(self) -> dict:
        try:
            with open(self.edits_path, encoding="utf-8") as f:
                data = json.load(f)
            return {int(k): v for k, v in data.items() if isinstance(v, dict)}
        except FileNotFoundError:
            return {}
        except (ValueError, OSError) as e:
            self.log(f"{self.edits_path}: {e}; ignoring the edits")
            return {}

    def _save_edits(self, data: dict):
        os.makedirs(os.path.dirname(self.edits_path), exist_ok=True)
        tmp = self.edits_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({str(k): v for k, v in sorted(data.items())}, f, indent=2)
        os.replace(tmp, self.edits_path)

    def _bus_ids(self) -> list:
        return [_identity(s["online"]) for s in self.sources]

    def _remap_edits(self, order: list, entries: list):
        """After the list changed: the saved edits follow their device. order[new position] = old position (None: a
        new device); an edit of a removed device is dropped. Each edit gets the new list as its "bus", so
        _follow_edits has nothing left to guess (it can't tell two devices of one type apart)."""
        old = self.load_edits()
        ids = [[e["vendor"], e["product"]] for e in entries]
        self._save_edits({new: dict(old[o], bus=ids) for new, o in enumerate(order) if o is not None and o in old})

    def _follow_edits(self):
        """After a change of the device list: move saved edits with their device when devices were inserted or removed
        before it. Each edit keeps the list it was made for ("bus"); edits saved before 1.3.4 have none and stay put.
        Without a good scan the list says nothing about the bus: an edit with no device then stays as it is."""
        all_edits = self.load_edits()
        now = self._bus_ids()
        if not now:
            return
        master = (self.raw or {}).get("master")
        trusted = bool(master) and not master["error"]
        moved, changed = {}, False
        for pos, edit in sorted(all_edits.items()):
            target = pos
            if edit.get("bus") is not None and edit["bus"] != now:
                target = configure.pair_in_order(edit["bus"], now).get(pos)
                changed = True
                if target is None or target in moved:
                    if trusted:
                        self.log(f"the saved edit of position {pos} has no device any more; dropped")
                    else:
                        moved.setdefault(pos, edit)
                    continue
                edit = dict(edit, bus=now)
            moved[target] = edit
        if changed:
            self._save_edits(moved)

    def _source(self, position: int) -> dict:
        for s in self.sources:
            if s["pos"] == position:
                return s
        raise KeyError(f"no device at position {position} in the last scan")

    def edit_options(self, position: int) -> dict:
        """What the editor shows: the PDOs the device offers, the selection the configuration would get, the
        device name and the startup parameters."""
        with self.lock:
            src = self._source(position)
            all_edits = self.load_edits()
            edit = all_edits.get(position)
            if edit and not edits.applies(src, edit):
                edit = None
            dev = configure.device_for(src, {position: edit} if edit else {})
            out = edits.options(src, dev, edit)
            if edit and "startup" in edit:
                out["startup"] = edit["startup"]
            else:                       # not edited yet: what the configuration keeps now, as the starting point
                out["startup"] = edits.kept_startup(configure.device_for(src, {}))
            dictionary = src.get("soe_dict") or {}
            out["startup"] = show_startup(out["startup"], dictionary)
            coe_cfg = ((dev or {}).get("mailbox") or {}).get("coe")
            out["coe"] = coe_cfg is not None
            out["fixed_startup"] = show_startup([c for c in (coe_cfg or {}).get("init", [])
                                                 if not c.get("user") and not c.get("from_eni")], dictionary)
            out["pdo_assign"] = bool((coe_cfg or {}).get("pdo_assign"))
            soe_cfg = ((dev or {}).get("mailbox") or {}).get("soe")
            out["soe"] = None
            if soe_cfg is not None:
                cycle = (self.master or {}).get("cycle_us") or 2000
                out["soe"] = {
                    "dictionary": bool(dictionary),
                    "mdt": [{"idn": e["index"], "text": soe.idn_text(e["index"]), "name": e["name"], "bits": e["bits"]}
                            for p in dev["pdos"] if p["dir"] == "rx" for e in p["entries"]],
                    "at": [{"idn": e["index"], "text": soe.idn_text(e["index"]), "name": e["name"], "bits": e["bits"]}
                           for p in dev["pdos"] if p["dir"] == "tx" for e in p["entries"]],
                    "commands": [dict(c, text=soe.idn_text(c["idn"])) for c in show_startup(
                                 generate.soe_commands(dev, cycle), dictionary) if not c.get("user")],
                }
            return out

    def _edit_changed(self):
        self._assemble()                  # the list shows the saved name
        self.plan_data = None

    def save_edit(self, position: int, change: dict) -> dict:
        with self.lock:
            src = self._source(position)
            all_edits = self.load_edits()
            old = all_edits.get(position) if all_edits.get(position) and edits.applies(src, all_edits[position]) else {}
            merged = {k: v for k, v in old.items() if k != "identity"}
            merged.update({k: v for k, v in change.items() if k in ("name", "pdos", "startup", "soe_mdt", "soe_at", "modules", "dc_mode")})
            checked = edits.validate(src, merged)
            configure.device_for(src, {position: checked})          # raises EditError if it can't be applied
            all_edits[position] = dict(checked, bus=self._bus_ids())
            self._save_edits(all_edits)
            self._edit_changed()
        return self.edit_options(position)

    def reset_edit(self, position: int) -> dict:
        with self.lock:
            all_edits = self.load_edits()
            all_edits.pop(position, None)
            self._save_edits(all_edits)
            self._edit_changed()
        return self.edit_options(position)

    # --- CoE objects of a device (online, through the master's mailbox) ------------------------------

    def _coe_address(self, position: int) -> dict:
        src = self._source(position)
        protocols = None
        if src.get("esi_device") is not None:
            protocols = src["esi_device"].get("mailbox", [])
        elif src.get("eeprom"):
            protocols = src["eeprom"].get("mailbox", [])
        if protocols is not None and "CoE" not in protocols:
            raise ConfigError(f'the device at position {position} has no CoE mailbox '
                              f'({", ".join(protocols) or "no mailbox protocols"}), so it has no objects to read or write')
        return self._autoinc(position)

    def _autoinc(self, position: int) -> dict:
        """The device's address on the bus; a device that isn't there has none."""
        if self.configuring:
            raise ConfigError(CONFIGURING)
        bus_pos = self._source(position).get("bus_pos")
        if bus_pos is None:
            raise ConfigError(f"the device at position {position} is not present on the bus")
        return {"addressType": "autoincrement", "address": (-bus_pos) & 0xFFFF}

    def _coe_base(self) -> str:
        return f'{INSTANCES}/{self.config["instance"]}/device_access/coe/'

    def objects(self, position: int, refresh=False) -> dict:
        """The device's object dictionary: index, name, object code, data type, startup-parameter flag."""
        a = self._coe_address(position)
        src = self._source(position)
        key = (position, json.dumps((src.get("online") or {}).get("response", {}).get("slaveIdentity", {}), sort_keys=True))
        if refresh or key not in self.od_cache:
            base = self._coe_base()
            indices = self.bus.read_args(base + "object_dictionary_list", {"request": {
                **a, "listType": "all", "maxElements": 4096}}).get("response", {}).get("indices") or []
            try:
                startup = set(self.bus.read_args(base + "object_dictionary_list", {"request": {
                    **a, "listType": "startupParam", "maxElements": 4096}}).get("response", {}).get("indices") or [])
            except Exception:
                startup = set()
            out = []
            for idx in indices:
                try:
                    d = coe.object_description(self.bus.read_args(base + "object_description", {"request": {
                        **a, "objectIndex": idx, "maxLength": 512}}).get("response", {}).get("data") or [])
                except Exception as e:
                    d = {"index": idx, "name": f"({e})", "object_code": "", "data_type": "", "max_subindex": 0}
                d["startup"] = idx in startup
                out.append(d)
            self.od_cache[key] = out
        return {"position": position, "objects": self.od_cache[key]}

    def object(self, position: int, index: int) -> dict:
        """One object with all its entries (descriptions and current values)."""
        a = self._coe_address(position)
        base = self._coe_base()
        d = coe.object_description(self.bus.read_args(base + "object_description", {"request": {
            **a, "objectIndex": index, "maxLength": 512}}).get("response", {}).get("data") or [])
        entries = []
        for sub in range(d["max_subindex"] + 1):
            try:
                e = coe.entry_description(self.bus.read_args(base + "entry_description", {"request": {
                    **a, "objectIndex": index, "subIndex": sub, "valueInfo": 0, "maxLength": 512}})
                    .get("response", {}).get("data") or [])
            except Exception as ex:
                entries.append({"subindex": sub, "error": str(ex)})
                continue
            if e["readable"]:
                try:
                    data = self.bus.read_args(base + "sdo", {"request": {**a, "objectIndex": index, "subIndex": sub,
                                                                       "flags": "noFlags", "maxLength": 1024}})
                    raw = data.get("response", {}).get("data") or []
                    e["value"] = coe.decode(raw, e["data_type"], e["bits"])
                    e["hex"] = bytes(raw).hex().upper()
                    e["show"] = coe.show(raw, e["data_type"], e["bits"])
                except Exception as ex:
                    e["error"] = str(ex)
            entries.append(e)
        return {"position": position, **d, "entries": entries}

    def sdo_write(self, position: int, index: int, sub: int, value) -> dict:
        """Writes one object entry on the device (online; not kept over a restart, see startup parameters)."""
        a = self._coe_address(position)
        base = self._coe_base()
        e = coe.entry_description(self.bus.read_args(base + "entry_description", {"request": {
            **a, "objectIndex": index, "subIndex": sub, "valueInfo": 0, "maxLength": 512}}).get("response", {}).get("data") or [])
        if not e["writable"]:
            raise ConfigError(f"{index:#06x}:{sub} is read-only")
        state = self._device_state(position)
        allowed = {"PreOp": "write_preop", "SafeOp": "write_safeop", "Op": "write_op"}.get(state)
        if allowed and not e["access"][allowed]:
            when = [s for s, k in (("PreOp", "write_preop"), ("SafeOp", "write_safeop"), ("Op", "write_op")) if e["access"][k]]
            raise ConfigError(f'{index:#06x}:{sub} "{e["name"]}" can only be written in {" / ".join(when)}; the device is in '
                              f"{state}. Add it as a startup parameter: the master writes it at every start-up.")
        data = coe.encode(value, e["data_type"], e["bits"])
        self.bus.write_json(base + "sdo", {"request": {**a, "objectIndex": index, "subIndex": sub, "flags": "noFlags",
                                                         "data": list(data), "maxLength": len(data)}})
        self.log(f"SDO write position {position} {index:#06x}:{sub} = {data.hex().upper()}")
        raw = self.bus.read_args(base + "sdo", {"request": {**a, "objectIndex": index, "subIndex": sub, "flags": "noFlags",
                                                            "maxLength": 1024}}).get("response", {}).get("data") or []
        return {**e, "value": coe.decode(raw, e["data_type"], e["bits"]), "hex": bytes(raw).hex().upper(),
                "show": coe.show(raw, e["data_type"], e["bits"])}

    def _device_state(self, position: int) -> str:
        """The device's EtherCAT state now (Init/PreOp/SafeOp/Op), read online."""
        a = self._autoinc(position)
        info = self.bus.read_args(f'{INSTANCES}/{self.config["instance"]}/device_access/slave_online_info',
                                  {"request": a}).get("response", {})
        return compare.al_state(info.get("alStatus")).split(" ")[0]

    def startup_entry(self, position: int, index: int, sub: int, value, comment="") -> dict:
        """A startup parameter for the edit: the value encoded for the entry's data type (read from the device)."""
        a = self._coe_address(position)
        e = coe.entry_description(self.bus.read_args(self._coe_base() + "entry_description", {"request": {
            **a, "objectIndex": index, "subIndex": sub, "valueInfo": 0, "maxLength": 512}}).get("response", {}).get("data") or [])
        if not e["writable"]:
            raise ConfigError(f"{index:#06x}:{sub} is read-only")
        data = coe.encode(value, e["data_type"], e["bits"])
        return {"index": index, "subindex": sub, "data": data.hex().upper(), "transitions": ["PS"],
                "comment": comment or f'{e["name"]} = {value}', "data_type": e["data_type"], "bits": e["bits"]}

    # --- SoE parameters of a drive (online, through the master's mailbox) ----------------------------

    def _soe_device(self, position: int):
        src = self._source(position)
        esi = src.get("esi_device")
        protocols = (esi or {}).get("mailbox") or (src.get("eeprom") or {}).get("mailbox") or []
        if "SoE" not in protocols:
            raise ConfigError(f"the device at position {position} has no SoE mailbox")
        return src, esi

    def soe_dictionary(self, position: int) -> dict:
        src, esi = self._soe_device(position)
        name = (esi or {}).get("dictionary")
        if not name:
            raise ConfigError("the device's ESI names no SoE dictionary")
        return self.esi.dictionary(name)

    def soe_parameters(self, position: int, text: str = "", mapping: str = "", limit: int = 400) -> dict:
        """The drive's parameters from its dictionary (offline): filter by IDN/name text and r (MDT) / t (AT)."""
        out = []
        for idn, p in self.soe_dictionary(position).items():
            if mapping and mapping not in p["mapping"]:
                continue
            if soe.matches(text, idn, p["name"]):
                out.append(p)
                if len(out) >= limit:
                    break
        return {"position": position, "parameters": out}

    def _soe_request(self, position: int, idn: int, flags: str, data=None, length=1024) -> dict:
        req = {**self._autoinc(position), "driveNumber": 0,
               "elementFlags": flags, "idn": idn, "maxLength": length}
        if data is not None:
            req["data"] = list(data)
        return {"request": req}

    def _soe_attribute(self, position: int, idn: int) -> int:
        try:
            p = self.soe_dictionary(position).get(idn)
            if p and p["attribute"] is not None:
                return p["attribute"]
        except (ConfigError, KeyError):
            pass
        raw = self.bus.read_args(f'{INSTANCES}/{self.config["instance"]}/device_access/soe/parameter',
                                 self._soe_request(position, idn, "attribute")).get("response", {}).get("data") or []
        return int.from_bytes(bytes(raw[:4]), "little")

    def soe_read(self, position: int, idn) -> dict:
        self._soe_device(position)
        idn = soe.parse_idn(idn)
        attr = soe.attribute(self._soe_attribute(position, idn))
        raw = self.bus.read_args(f'{INSTANCES}/{self.config["instance"]}/device_access/soe/parameter',
                                 self._soe_request(position, idn, "value")).get("response", {}).get("data") or []
        info = {}
        try:
            info = self.soe_dictionary(position).get(idn) or {}
        except (ConfigError, KeyError):
            pass
        return {"idn": idn, "text": soe.idn_text(idn), "name": info.get("name", ""), "unit": info.get("unit", ""),
                "attribute": attr, "value": soe.decode(raw, attr), "hex": bytes(raw).hex().upper(),
                "show": soe.show(raw, attr)}

    def soe_write(self, position: int, idn, value) -> dict:
        self._soe_device(position)
        idn = soe.parse_idn(idn)
        attr = soe.attribute(self._soe_attribute(position, idn))
        state = self._device_state(position)
        if attr["command"]:
            raise ConfigError(f"{soe.idn_text(idn)} is a procedure command; starting commands is not supported")
        if attr["write_protected"].get(state):
            free = [s for s, p in attr["write_protected"].items() if not p]
            raise ConfigError(f"{soe.idn_text(idn)} is write-protected in {state}"
                              + (f" (writable in {' / '.join(free)}); add it as a startup parameter" if free else ""))
        data = soe.encode(value, attr)
        self.bus.write_json(f'{INSTANCES}/{self.config["instance"]}/device_access/soe/parameter',
                            self._soe_request(position, idn, "value", data, len(data)))
        self.log(f"SoE write position {position} {soe.idn_text(idn)} = {data.hex().upper()}")
        return self.soe_read(position, idn)

    def soe_startup_entry(self, position: int, idn, value) -> dict:
        idn = soe.parse_idn(idn)
        attr = soe.attribute(self._soe_attribute(position, idn))
        if attr["command"]:
            raise ConfigError(f"{soe.idn_text(idn)} is a procedure command")
        if attr["write_protected"].get("PreOp"):
            raise ConfigError(f"{soe.idn_text(idn)} is write-protected in PreOp, a startup parameter can't set it")
        name = (self.soe_dictionary(position).get(idn) or {}).get("name", "")
        return {"idn": idn, "data": soe.encode(value, attr).hex().upper(), "transitions": ["PS"],
                "comment": f"{soe.idn_text(idn)} {name} = {value}"[:120]}

    # --- process data (the running configuration's realtime_data variables) -------------------------

    def process_data(self, position: int = None) -> dict:
        """The running configuration's variables with their values; with a position only that device's (the
        configuration's device paired with it by the last scan; none if the device isn't configured yet)."""
        with self.lock:
            if self.configuring:
                raise ConfigError(CONFIGURING)
            parsed = (self.current or {}).get("parsed")
            slaves = parsed["slaves"] if parsed else []
            if position is not None:
                slave = self._source(position).get("eni_slave")
                slaves = [slave] if slave else []
        if not parsed:
            raise ConfigError("scan the bus first (no configuration known)")
        base = f'{INSTANCES}/{self.config["instance"]}/realtime_data/'
        out = []
        for s in slaves:
            for v in s["variables"]:
                item = {"device": s["name"], "name": v["name"], "dir": v["dir"], "type": v["type"], "bits": v["bits"]}
                try:
                    item["value"] = self.bus.read(f'{base}{v["dir"]}/data/{v["name"]}')
                except Exception as e:
                    item["error"] = str(e)
                out.append(item)
        return {"variables": out, "time": time.time()}

    def write_output(self, name: str, value):
        with self.lock:
            if self.configuring:
                raise ConfigError(CONFIGURING)
            parsed = (self.current or {}).get("parsed")
        var = next((v for s in (parsed or {}).get("slaves", []) for v in s["variables"] if v["name"] == name), None)
        if var is None:
            raise KeyError(f"no variable {name} in the running configuration")
        if var["dir"] != "output":
            raise ConfigError(f"{name} is an input")
        address = f'{INSTANCES}/{self.config["instance"]}/realtime_data/output/data/{name}'
        self.bus.write(address, coe.process_value(value, var["type"], var["bits"]))
        return {"name": name, "value": self.bus.read(address)}

    # --- backups (in the active configuration: io-config/backups/<time>/) ----------------------------

    def _backup(self, eni_bytes, ini_bytes, why) -> str:
        name = time.strftime("%Y%m%d-%H%M%S")
        folder = os.path.join(self.backup_dir, name)
        os.makedirs(folder, exist_ok=True)
        for file, data in (("ethercat.xml", eni_bytes), ("ethercat.ini", ini_bytes)):
            with open(os.path.join(folder, file), "wb") as f:
                f.write(data)
        with open(os.path.join(folder, "info.json"), "w", encoding="utf-8") as f:
            json.dump({"time": time.time(), "before": why,
                       "devices": _device_names(eni_bytes)}, f)
        for old in self.backups()[MAX_BACKUPS:]:
            shutil.rmtree(os.path.join(self.backup_dir, old["name"]), ignore_errors=True)
        return name

    def backups(self) -> list:
        out = []
        try:
            names = sorted(os.listdir(self.backup_dir), reverse=True)
        except FileNotFoundError:
            return []
        for name in names:
            try:
                with open(os.path.join(self.backup_dir, name, "info.json"), encoding="utf-8") as f:
                    out.append({"name": name, **json.load(f)})
            except (OSError, ValueError):
                continue
        return out

    def _backup_folder(self, name: str) -> str:
        if not re.fullmatch(r"\d{8}-\d{6}", name) or not os.path.isdir(os.path.join(self.backup_dir, name)):
            raise KeyError(f"no backup {name}")
        return os.path.join(self.backup_dir, name)
