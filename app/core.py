# SPDX-License-Identifier: MIT

"""The app's state: the configuration (config.json in the active configuration) and a cyclic worker.

Replace the example (a counter and a setpoint) with the app's own logic; keep the file handling:
- config.json is written atomically (config.json.tmp, then rename; *.tmp is an appPrivateFile in the manifest,
  so it never ends up in a saved configuration);
- a file replaced from outside (Manage app data, loading a configuration or a backup) is picked up by
  reload_if_changed() and never overwritten with the old state.
"""

import json
import os
import threading
import time

DEFAULTS = {"setpoint": 0.0, "cycle_s": 1.0, "note": ""}


class ConfigError(ValueError):
    pass


def validate(cfg: dict) -> dict:
    """The complete configuration from a (partial) one; ConfigError if a value is wrong."""
    out = dict(DEFAULTS)
    for key, value in cfg.items():
        if key not in DEFAULTS:
            raise ConfigError(f"unknown setting {key!r}")
        out[key] = value
    if isinstance(out["setpoint"], bool) or not isinstance(out["setpoint"], (int, float)):
        raise ConfigError('"setpoint" must be a number')
    out["setpoint"] = float(out["setpoint"])
    if isinstance(out["cycle_s"], bool) or not isinstance(out["cycle_s"], (int, float)) or not 0.05 <= out["cycle_s"] <= 60:
        raise ConfigError('"cycle_s" must be a number from 0.05 to 60')
    if not isinstance(out["note"], str):
        raise ConfigError('"note" must be text')
    return out


class Core:

    def __init__(self, config_path: str, bus, registry_factory=None, log=print):
        self.config_path = config_path
        self.bus = bus
        self.log = log
        self.lock = threading.RLock()
        self.config = self._load()
        self.file_id = self._file_id()
        self.counter = 0
        self.started = time.time()
        self.registry = registry_factory(self) if registry_factory else None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="worker", daemon=True)

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

    # --- API used by web.py and provider.py -----------------------------------------------------------

    def update_config(self, changes: dict) -> dict:
        with self.lock:
            self.config = validate({**self.config, **changes})
            self._save()
            return dict(self.config)

    def set_setpoint(self, value: float) -> float:
        return self.update_config({"setpoint": value})["setpoint"]

    def state(self) -> dict:
        with self.lock:
            return {"counter": self.counter, "setpoint": self.config["setpoint"],
                    "uptime_s": round(time.time() - self.started, 1), "connected": self.bus.is_connected()}

    # --- worker ------------------------------------------------------------------------------------

    def start(self):
        if self.registry is not None:
            self.registry.register_all()
        self._thread.start()

    def _run(self):
        while not self._stop.wait(self.config["cycle_s"]):
            with self.lock:
                self.counter += 1

    def stop(self):
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(5)
        if self.registry is not None:
            self.registry.clear()
