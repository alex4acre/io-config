# SPDX-License-Identifier: MIT

"""A simulated ctrlX Data Layer with the same interface as app.bus.DatalayerBus, for development and tests
on a machine without the Data Layer library (it needs Ubuntu 24.04) and without a ctrlX.

The EtherCAT master is replayed from a recording of a real ctrlX CORE X3 (tests/data: x3-bus.json with
the status nodes, each device's online info and EEPROM; controller-eni.xml and controller-ini.txt).
Values have the shape the real Data Layer gives ({"response": {...}}), and the surprises are simulated:
asking past the last device fails, and one EEPROM read longer than 64 words times out.
"""

import json
import os
import threading

from app.bus import BusError

DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests", "data")
MASTER = "fieldbuses/ethercat/master/instances/ethercatmaster/"


def recorded_nodes(data_dir: str = DATA) -> dict:
    with open(os.path.join(data_dir, "x3-bus.json"), encoding="utf-8") as f:
        rec = json.load(f)
    nodes = {MASTER + k: v for k, v in rec["status"].items()}
    with open(os.path.join(data_dir, "controller-eni.xml"), "rb") as f:
        eni = list(f.read())
    with open(os.path.join(data_dir, "controller-ini.txt"), "rb") as f:
        ini = list(f.read())
    nodes[MASTER + "admin/fieldbus_config"] = {"response": {"ini": ini, "eni": eni}}
    nodes[MASTER + "device_access/slave_online_info"] = None      # needs arguments
    nodes[MASTER + "device_access/slave_eeprom"] = None
    return nodes, rec["slaves"]


class FakeBus:

    def __init__(self, nodes: dict = None, slaves: list = None):
        self.lock = threading.RLock()
        if nodes is None:
            nodes, slaves = recorded_nodes()
        self.nodes = dict(nodes)
        self.slaves = list(slaves or [])    # [{"autoinc", "online_info", "eeprom"}] in bus order
        self.calls = []                     # (operation, address, args) for tests

    def is_connected(self) -> bool:
        return True

    def close(self):
        pass

    def browse(self, address: str) -> list:
        prefix = address.strip("/") + "/" if address.strip("/") else ""
        with self.lock:
            children = {p[len(prefix):].split("/")[0] for p in self.nodes if p.startswith(prefix)}
        if not children and address.strip("/") not in self.nodes:
            raise BusError(f"browse {address}: DL_INVALID_ADDRESS")
        return sorted(children)

    def metadata(self, address: str) -> dict:
        with self.lock:
            leaf = address in self.nodes
        return {"nodeClass": "Variable" if leaf else "Folder",
                "operations": {"read": leaf, "write": False, "create": False, "delete": False, "browse": True},
                "description": "", "references": {}}

    def read(self, address: str):
        with self.lock:
            self.calls.append(("read", address, None))
            if address not in self.nodes:
                raise BusError(f"read {address}: DL_INVALID_ADDRESS")
            if self.nodes[address] is None:
                raise BusError(f"read {address}: DL_TYPE_MISMATCH")
            return self.nodes[address]

    def _slave(self, request: dict):
        if request.get("addressType") != "autoincrement":
            raise BusError("read: DL_TYPE_MISMATCH")
        for s in self.slaves:
            if s["autoinc"] == request.get("address"):
                return s
        raise BusError("read: DL_FAILED (Ecat: Device not found)")

    def read_args(self, address: str, args: dict):
        with self.lock:
            self.calls.append(("read_args", address, args))
            request = (args or {}).get("request", {})
            if address == MASTER + "device_access/slave_online_info":
                return self._slave(request)["online_info"]
            if address == MASTER + "device_access/slave_eeprom":
                s = self._slave(request)
                length = request.get("maxLength", 0)
                if length > 64:
                    raise BusError("read: DL_TIMEOUT (Ecat: Timeout)")
                offset = request.get("eepromOffset", 0)
                return {"response": {"data": s["eeprom"][offset:offset + length]}}
            raise BusError(f"read {address}: DL_INVALID_ADDRESS")

    def write(self, address: str, value):
        raise BusError(f"write {address}: the fake is read-only")

    def create(self, address: str, value):
        raise BusError(f"create {address}: the fake is read-only")

    def remove(self, address: str):
        raise BusError(f"remove {address}: the fake is read-only")
