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
import threading
import time

from app import compare, eni, sii
from app.esi import EsiLibrary

DEFAULTS = {"instance": "ethercatmaster"}
INSTANCES = "fieldbuses/ethercat/master/instances"
EEPROM_CHUNK_WORDS = 64
EEPROM_MAX_WORDS = 8192


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
        self.eeprom_cache = {}       # (position, vendor, product, revision, serial) -> words
        self._thread = None

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

    def start_scan(self, reread_eeprom=False) -> dict:
        with self.lock:
            if self.scan_state["running"]:
                raise ConfigError("a scan is already running")
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
            master, devices = self._scan()
            with self.lock:
                self.master, self.devices = master, devices
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
        try:
            cfg = self.bus.read(base + "admin/fieldbus_config").get("response", {})
            eni_bytes = _bytes(cfg.get("eni"))
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

        devices = []
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
            devices.append(self._report(pos, online, eeprom, eeprom_error, eni_slaves.get(pos), master_op))
        for pos in sorted(p for p in eni_slaves if p >= connected):
            devices.append(self._report(pos, None, None, None, eni_slaves[pos], master_op))
        return master, devices

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
                                     esi_error=esi_error, master_op=master_op)
