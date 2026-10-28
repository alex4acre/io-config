# SPDX-License-Identifier: MIT

"""A simulated ctrlX Data Layer with the same interface as app.bus.DatalayerBus, for development and tests
on a machine without the Data Layer library (it needs Ubuntu 24.04) and without a ctrlX.

Add the nodes the app uses, with values as a real ctrlX gives them (copy them from a read-only dump of the
device), so the fake behaves like the device. Where the device surprised you, simulate the surprise here.
"""

import threading

from app.bus import BusError


class FakeBus:

    def __init__(self, nodes: dict = None):
        self.lock = threading.RLock()
        self.nodes = dict(nodes) if nodes is not None else {
            "framework/metrics/system/cpu-utilisation-percent": 12.5,
            "framework/metrics/system/memavailable-mb": 1450.0,
            "motion/state/opstate": "Running",
            "scheduler/admin/state": "OPERATING",
            "demo/flag": False,
            "demo/position": [0.0, 0.0, 0.0],
        }
        self.created = []           # (address, value) of each create, for tests

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
                "operations": {"read": leaf, "write": leaf, "create": False, "delete": False, "browse": True},
                "description": "", "references": {}}

    def read(self, address: str):
        with self.lock:
            if address not in self.nodes:
                raise BusError(f"read {address}: DL_INVALID_ADDRESS")
            return self.nodes[address]

    def write(self, address: str, value):
        with self.lock:
            if address not in self.nodes:
                raise BusError(f"read {address}: DL_INVALID_ADDRESS")
            old = self.nodes[address]
            if type(old) is not type(value) and not (isinstance(old, float) and isinstance(value, int)):
                raise BusError(f"write {address}: DL_TYPE_MISMATCH")
            self.nodes[address] = float(value) if isinstance(old, float) else value

    def create(self, address: str, value):
        with self.lock:
            self.created.append((address, value))
        return None

    def remove(self, address: str):
        with self.lock:
            if self.nodes.pop(address, None) is None:
                raise BusError(f"remove {address}: DL_INVALID_ADDRESS")
