# SPDX-License-Identifier: MIT

"""The app's state: the configuration (config.json in the active configuration), the ESI library and the
last bus scan.

A scan only reads: the master's status, the configuration it runs with (ENI + ini), and per bus position
the online info and the EEPROM. EEPROMs are read in small chunks (one large read timed out on a real
ctrlX CORE X3 and logged an error) and cached per device, so a rescan doesn't read them again unless asked.
The number of devices comes from num_connected_slaves: asking past the last device makes the master log
"Device not found".

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

from app import coe, compare, configure, edits, eni, generate, sii
from app.esi import EsiLibrary

DEFAULTS = {"instance": "ethercatmaster"}
INSTANCES = "fieldbuses/ethercat/master/instances"
EEPROM_CHUNK_WORDS = 64
EEPROM_MAX_WORDS = 8192
MAX_BACKUPS = 20


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
        self.download_state = {"running": False, "steps": [], "finished": None, "error": None}
        self._thread = None
        # the active configuration: io-config/config.json is in it, and the master's files too
        self.active_dir = os.path.dirname(os.path.dirname(os.path.abspath(config_path)))
        self.backup_dir = os.path.join(os.path.dirname(os.path.abspath(config_path)), "backups")
        self.edits_path = os.path.join(os.path.dirname(os.path.abspath(config_path)), "edits.json")
        self.od_cache = {}           # (position, identity) -> object list

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
                "download": dict(self.download_state, steps=list(self.download_state["steps"])),
                "master": self.master,
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
                    return d
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
            master, devices, sources, current = self._scan()
            with self.lock:
                self.master, self.devices, self.sources, self.current = master, devices, sources, current
                self.plan_data = None
        except Exception as e:      # shown in the UI; the last good scan stays
            error = str(e)
            self.log(f"scan failed: {e}")
        with self.lock:
            self.scan_state = {"running": False, "progress": "", "finished": time.time(), "error": error}

    def _scan(self):
        base = f'{INSTANCES}/{self.config["instance"]}/'
        self._progress("master status")
        if self.config["instance"] not in self.instances():
            raise ConfigError(f'no EtherCAT master instance "{self.config["instance"]}" '
                              f'(found: {", ".join(self.instances()) or "none"})')
        state = self.bus.read(base + "admin/fieldbus_status/master_state").get("response", {})
        connected = self.bus.read(base + "admin/fieldbus_status/num_connected_slaves").get("response", {}).get("numSlaves", 0)
        configured = self.bus.read(base + "admin/fieldbus_status/num_configured_slaves").get("response", {}).get("numSlaves", 0)
        topology = self.bus.read(base + "admin/fieldbus_status/topology_status").get("response", {})
        master = {"state": state.get("currentState", ""), "requested": state.get("requestedState", ""),
                  "connected": connected, "configured": configured, "topology": topology.get("state", ""),
                  "eni": None, "ini": {}, "eni_error": None}

        self._progress("configuration (ENI)")
        config = None
        eni_bytes = ini_bytes = b""
        try:
            cfg = self.bus.read(base + "admin/fieldbus_config").get("response", {})
            eni_bytes, ini_bytes = _bytes(cfg.get("eni")), _bytes(cfg.get("ini"))
            if eni_bytes:
                config = eni.parse_eni(eni_bytes)
                master["eni"] = {"slaves": len(config["slaves"]), "inputs_bytes": config["inputs_bytes"],
                                 "outputs_bytes": config["outputs_bytes"], "size": len(eni_bytes)}
            master["ini"] = eni.parse_ini(_bytes(cfg.get("ini")).decode("latin-1"))
        except Exception as e:
            master["eni_error"] = str(e)
        cycle = master["ini"].get("EtherCAT.Master", {}).get("CycleTimeUs")
        master["cycle_us"] = int(cycle) if cycle and cycle.isdigit() else None
        eni_slaves = {s["position"]: s for s in (config or {}).get("slaves", [])}

        devices, sources = [], []
        master_op = master["state"] == "op"
        for pos in range(connected):
            self._progress(f"device {pos + 1} of {connected}")
            address = {"addressType": "autoincrement", "address": (-pos) & 0xFFFF}
            online = self.bus.read_args(base + "device_access/slave_online_info", {"request": address})
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
            report, match, device = self._report(pos, online, eeprom, eeprom_error, eni_slaves.get(pos), master_op)
            devices.append(report)
            sources.append({"pos": pos, "online": online, "eeprom": eeprom, "eni_slave": eni_slaves.get(pos),
                            "esi_match": match, "esi_device": device})
        for pos in sorted(p for p in eni_slaves if p >= connected):
            devices.append(self._report(pos, None, None, None, eni_slaves[pos], master_op)[0])
        return master, devices, sources, {"eni": eni_bytes, "ini": ini_bytes, "parsed": config}

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

    def _report(self, pos, online, eeprom, eeprom_error, eni_slave, master_op):
        identity = (online or {}).get("response", {}).get("slaveIdentity") or eni_slave or {}
        vendor = identity.get("vendorId", identity.get("vendor"))
        product = identity.get("productCode", identity.get("product"))
        revision = identity.get("revisionNumber", identity.get("revision"))
        match, entry, device, esi_error = self.esi.find(vendor, product, revision) + (None, None)
        if entry is not None:
            try:
                device = self.esi.device(entry)
            except Exception as e:
                esi_error = str(e)
        return compare.device_report(pos, online, eeprom, eeprom_error, eni_slave, match, entry, device,
                                     esi_error=esi_error, master_op=master_op), match, device

    # --- new configuration and download -------------------------------------------------------------

    def plan(self, cycle_us=None) -> dict:
        """The configuration the last scan's bus gets (not written yet)."""
        with self.lock:
            if self.current is None or self._busy():
                raise ConfigError("scan the bus first" if self.current is None else "a scan or download is running")
            if cycle_us is not None and (not isinstance(cycle_us, int) or isinstance(cycle_us, bool)
                                         or not 125 <= cycle_us <= 100000):
                raise ConfigError("cycle time must be 125 to 100000 µs")
            p = configure.build_plan(self.sources, self.current, self.config["instance"], cycle_us, self.load_edits())
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
            self.download_state = {"running": True, "steps": [], "finished": None, "error": None}
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
            self.download_state = {"running": True, "steps": [], "finished": None, "error": None}
            self._thread = threading.Thread(target=self._download_thread, args=(eni_bytes, ini_bytes, f"restore {name}"),
                                            name="download", daemon=True)
            self._thread.start()
            return dict(self.download_state)

    def _download_thread(self, eni_bytes, ini_bytes, what):
        base = f'{INSTANCES}/{self.config["instance"]}/'
        error = None
        steps = self.download_state["steps"]
        try:
            # never overwrite a configuration someone else downloaded since the last scan
            sums = {c["id"]: c["checksum"] for c in
                    self.bus.read(base + "admin/fieldbus_config/checksum").get("response", {}).get("checksums", [])}
            if self.current and self.current["eni"] and sums.get("eniCrc32") != generate.checksum(self.current["eni"]):
                raise configure.DownloadError("the master's configuration changed since the last scan; scan again")
            if self.current and self.current["eni"]:
                steps.append(f'backup {self._backup(self.current["eni"], self.current["ini"], what)}')
            folder = os.path.join(self.active_dir, "comm.ethercat.master", "config", self.config["instance"])
            steps += configure.write_config(self.bus, base, eni_bytes, ini_bytes,
                                            (os.path.join(folder, "ethercat.xml"), os.path.join(folder, "ethercat.ini")),
                                            self.log)
        except Exception as e:
            error = str(e)
            self.log(f"{what} failed: {e}")
        with self.lock:
            self.download_state = {"running": False, "steps": steps, "finished": time.time(), "error": error}
            self.plan_data = None
        self.start_scan()

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
            coe_cfg = ((dev or {}).get("mailbox") or {}).get("coe")
            out["coe"] = coe_cfg is not None
            out["fixed_startup"] = [c for c in (coe_cfg or {}).get("init", []) if not c.get("user") and not c.get("from_eni")]
            out["pdo_assign"] = bool((coe_cfg or {}).get("pdo_assign"))
            return out

    def save_edit(self, position: int, change: dict) -> dict:
        with self.lock:
            src = self._source(position)
            all_edits = self.load_edits()
            old = all_edits.get(position) if all_edits.get(position) and edits.applies(src, all_edits[position]) else {}
            merged = {k: v for k, v in old.items() if k != "identity"}
            merged.update({k: v for k, v in change.items() if k in ("name", "pdos", "startup")})
            checked = edits.validate(src, merged)
            configure.device_for(src, {position: checked})          # raises EditError if it can't be applied
            all_edits[position] = checked
            self._save_edits(all_edits)
            self.plan_data = None
        return self.edit_options(position)

    def reset_edit(self, position: int) -> dict:
        with self.lock:
            all_edits = self.load_edits()
            all_edits.pop(position, None)
            self._save_edits(all_edits)
            self.plan_data = None
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
        return {"addressType": "autoincrement", "address": (-position) & 0xFFFF}

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
        return {**e, "value": coe.decode(raw, e["data_type"], e["bits"]), "hex": bytes(raw).hex().upper()}

    def _device_state(self, position: int) -> str:
        """The device's EtherCAT state now (Init/PreOp/SafeOp/Op), read online."""
        a = self._coe_address(position)
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
                "comment": comment or f'{e["name"]} = {value}'}

    # --- process data (the running configuration's realtime_data variables) -------------------------

    def process_data(self) -> dict:
        with self.lock:
            parsed = (self.current or {}).get("parsed")
        if not parsed:
            raise ConfigError("scan the bus first (no configuration known)")
        base = f'{INSTANCES}/{self.config["instance"]}/realtime_data/'
        out = []
        for s in parsed["slaves"]:
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
                       "devices": [s["name"] for s in eni.parse_eni(eni_bytes)["slaves"]]}, f)
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
