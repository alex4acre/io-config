# SPDX-License-Identifier: MIT

"""A simulated ctrlX Data Layer with the same interface as app.bus.DatalayerBus, for development and tests
on a machine without the Data Layer library (it needs Ubuntu 24.04) and without a ctrlX.

The EtherCAT master is replayed from a recording of a real ctrlX CORE X3 (tests/data: x3-bus.json with
the status nodes, each device's online info and EEPROM; controller-eni.xml and controller-ini.txt).
Values have the shape the real Data Layer gives ({"response": {...}}), and the surprises are simulated:
asking past the last device fails, and one EEPROM read longer than 64 words times out. Writing a new
configuration works as on the device (master to Init, fieldbus_config, checksums, master to Op).
"""

import json
import os
import threading

from app.bus import BusError
from app.generate import checksum

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
    nodes["scheduler/tasks/ctrlXAutomation/properties"] = {"name": "ctrlXAutomation", "event": "cyclic", "cycletime": 2000}
    nodes[MASTER + "device_access/slave_online_info"] = None      # needs arguments
    nodes[MASTER + "device_access/slave_eeprom"] = None
    for name in ("object_dictionary_list", "object_description", "entry_description", "sdo"):
        nodes[MASTER + "device_access/coe/" + name] = None
    # process data: one node per ENI variable, as the master publishes them
    from app.eni import parse_eni
    for s in parse_eni(bytes(eni))["slaves"]:
        for v in s["variables"]:
            nodes[f'{MASTER}realtime_data/{v["dir"]}/data/{v["name"]}'] = _initial(v)
    return nodes, rec["slaves"]


def _initial(v):
    t = v["type"].upper()
    if t in ("BIT", "BOOL"):
        return False
    if t.startswith("ARRAY"):
        return [0] * (v["bits"] // 8)
    if t in ("REAL", "LREAL"):
        return 0.0
    if v["name"].endswith("_Voltage"):
        return 24100                     # the coupler's supply voltages in mV
    return 0


def recorded_coe(data_dir: str = DATA) -> dict:
    """autoinc address -> recorded object dictionary (tests/data/x3-coe.json)."""
    with open(os.path.join(data_dir, "x3-coe.json"), encoding="utf-8") as f:
        return {int(k): v for k, v in json.load(f).items()}


def add_drive(nodes: dict, slaves: list, data_dir: str = DATA):
    """The ctrlX DRIVE XCS2-F/H (SoE) as it joined the X3's bus: at port C of the coupler, in Init (not
    configured). tests/data/x3-drive.json holds its real online info and EEPROM."""
    with open(os.path.join(data_dir, "x3-drive.json"), encoding="utf-8") as f:
        drive = json.load(f)
    slaves[0] = json.loads(json.dumps(slaves[0]))
    slaves[0]["online_info"]["response"]["portSlaveHandles"][2] = drive["online_info"]["response"]["slaveHandle"]
    slaves.append({"autoinc": drive["autoinc"], "online_info": drive["online_info"], "eeprom": drive["eeprom"]})
    nodes[MASTER + "admin/fieldbus_status/num_connected_slaves"] = {"response": {"numSlaves": len(slaves)}}


def add_elmo(nodes: dict, slaves: list, data_dir: str = DATA):
    """An Elmo Titanium (CoE, modular, tests/data/esi-elmo) at port C of the coupler, in Init: online info and
    EEPROM are the ctrlX DRIVE's recording with the Elmo's identity (no recording of the real one)."""
    with open(os.path.join(data_dir, "x3-drive.json"), encoding="utf-8") as f:
        elmo = json.load(f)
    info = elmo["online_info"]["response"]
    info["slaveIdentity"].update(vendorId=0x9A, productCode=0x01280002, revisionNumber=0x00220031)
    info["mbxProtocols"] = 2 | 4 | 8                   # EoE, CoE, FoE
    words = elmo["eeprom"]
    words[8:14] = [0x9A, 0, 0x0002, 0x0128, 0x0031, 0x0022]
    slaves[0] = json.loads(json.dumps(slaves[0]))
    slaves[0]["online_info"]["response"]["portSlaveHandles"][2] = info["slaveHandle"]
    slaves.append({"autoinc": elmo["autoinc"], "online_info": elmo["online_info"], "eeprom": words})
    nodes[MASTER + "admin/fieldbus_status/num_connected_slaves"] = {"response": {"numSlaves": len(slaves)}}


class FakeBus:

    def __init__(self, nodes: dict = None, slaves: list = None, drive: bool = False, elmo: bool = False):
        self.lock = threading.RLock()
        if nodes is None:
            nodes, slaves = recorded_nodes()
            if elmo:
                add_elmo(nodes, slaves)
            elif drive:
                add_drive(nodes, slaves)
                from app.soe import parse_dictionary
                with open(os.path.join(DATA, "esi-drive", "BoschRexroth_ctrlX_DRIVE_XCS2_F_H_SoE_Dictionary.xml"), "rb") as f:
                    self.soe_attributes = {i: p["attribute"] for i, p in parse_dictionary(f.read()).items()
                                           if p["attribute"] is not None}
                with open(os.path.join(DATA, "x3-drive-soe.json"), encoding="utf-8") as f:
                    self.soe_recorded = {int(k): bytes(v["value"]) for k, v in json.load(f).items()}
        self.nodes = dict(nodes)
        self.slaves = list(slaves or [])    # [{"autoinc", "online_info", "eeprom"}] in bus order
        self.calls = []                     # (operation, address, args) for tests
        self.written = []                   # (eni, ini) of each configuration write
        self.coe = recorded_coe() if self.slaves else {}       # autoinc address -> object dictionary
        self.fail_op = False                # True: the master stays in PreOp after requesting Op
        self.fail_init = False              # True: the master stays where it is after requesting Init
        self.refuse_config = False          # True: writing fieldbus_config fails
        self.gone = False                   # True: every access to the master fails
        self.soe = {}                       # (autoinc, idn) -> value bytes written (SoE), see _soe
        if not hasattr(self, "soe_recorded"):
            self.soe_recorded = {}          # idn -> value bytes read from the real drive (x3-drive-soe.json)

    def is_connected(self) -> bool:
        return True

    def close(self):
        pass

    def _check_gone(self, address: str):
        if self.gone and address.startswith("fieldbuses"):
            raise BusError(f"{address}: DL_INVALID_ADDRESS")

    def browse(self, address: str) -> list:
        self._check_gone(address)
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
            self._check_gone(address)
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
            self._check_gone(address)
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
            if address.startswith(MASTER + "device_access/coe/"):
                return self._coe(address.rsplit("/", 1)[1], request)
            if address == MASTER + "device_access/soe/parameter":
                return self._soe(request)
            raise BusError(f"read {address}: DL_INVALID_ADDRESS")

    def _coe(self, node, req):
        self._slave(req)
        dev = self.coe.get(req.get("address"))
        if dev is None or "__error__" in dev["list"]["all"]:
            raise BusError("read: DL_FAILED (Ecat CoE: Protocol not supported)")

        def answer(rec):
            if "__error__" in rec:
                raise BusError(f'read: DL_FAILED ({rec["__error__"].get("detail", "")})')
            return rec
        if node == "object_dictionary_list":
            return answer(dev["list"][req.get("listType", "all")])
        obj = dev["objects"].get(str(req.get("objectIndex")))
        if obj is None:
            raise BusError("read: DL_FAILED (Ecat SDO: Object does not exist)")
        if node == "object_description":
            return answer(obj["desc"])
        sub = str(req.get("subIndex", 0))
        if node == "entry_description":
            if sub not in obj["entries"]:
                raise BusError("read: DL_FAILED (Ecat SDO: Subindex does not exist)")
            return answer(obj["entries"][sub])
        if sub not in obj["values"]:
            raise BusError("read: DL_FAILED (Ecat SDO: Subindex does not exist)")
        return answer(obj["values"][sub])

    def _configured(self, s) -> bool:
        """A device is past Init when the configuration in the fake master lists its identity."""
        from app.eni import parse_eni
        idn = s["online_info"]["response"]["slaveIdentity"]
        eni = bytes(self.nodes[MASTER + "admin/fieldbus_config"]["response"]["eni"])
        return any((x["vendor"], x["product"]) == (idn["vendorId"], idn["productCode"]) for x in parse_eni(eni)["slaves"])

    def _soe(self, req):
        """SoE parameters of the drive: the attribute from its dictionary, values as read from the real drive
        in SafeOp (tests/data/x3-drive-soe.json), 0 for others, or what was written."""
        s = self._slave(req)
        if not s["online_info"]["response"].get("mbxProtocols", 0) & 0x10:       # bit 4: SoE
            raise BusError("read: DL_FAILED (Ecat SoE: Protocol not supported)")
        if not self._configured(s):
            raise BusError("read: DL_FAILED (Ecat: Device not found)")
        idn = req.get("idn")
        attr = self.soe_attributes.get(idn)
        if attr is None:
            raise BusError("read: DL_FAILED (Ecat SoE: IDN not existing)")
        flags = req.get("elementFlags")
        if flags == "attribute":
            return {"response": {"elementFlags": flags, "data": list(attr.to_bytes(4, "little"))}}
        if flags == "value":
            size = {0b001: 2, 0b010: 4, 0b011: 8}.get((attr >> 16) & 7)
            default = self.soe_recorded.get(idn) or (bytes(size) if size else bytes(4))
            return {"response": {"elementFlags": flags, "data": list(self.soe.get((req["address"], idn), default))}}
        raise BusError("read: DL_FAILED (Ecat SoE: element not supported by the fake)")

    soe_attributes = {}                     # idn -> attribute (from the drive's dictionary when drive=True)

    def write_json(self, address: str, value):
        """SDO downloads (device_access/coe/sdo): the new bytes become the object's value, if it is writable."""
        with self.lock:
            self.calls.append(("write_json", address, value))
            if address == MASTER + "device_access/soe/parameter":
                req = (value or {}).get("request", {})
                s = self._slave(req)
                if not self._configured(s):
                    raise BusError("write: DL_FAILED (Ecat: Device not found)")
                self.soe[(req["address"], req["idn"])] = bytes(req.get("data") or [])
                return
            if address != MASTER + "device_access/coe/sdo":
                raise BusError(f"write {address}: DL_INVALID_ADDRESS")
            req = (value or {}).get("request", {})
            self._slave(req)
            dev = self.coe.get(req.get("address"))
            if dev is None or "__error__" in dev["list"]["all"]:
                raise BusError("write: DL_FAILED (Ecat CoE: Protocol not supported)")
            obj = dev["objects"].get(str(req.get("objectIndex")))
            sub = str(req.get("subIndex", 0))
            if obj is None or sub not in obj["entries"]:
                raise BusError("write: DL_FAILED (Ecat SDO: Subindex does not exist)")
            access = bytes(obj["entries"][sub]["response"]["data"])[8] if "response" in obj["entries"][sub] else 0
            if not access & 0x38:
                raise BusError("write: DL_FAILED (Ecat SDO: Attempt to write a read only object)")
            obj["values"][sub] = {"response": {"data": list(req.get("data") or [])}}

    def write(self, address: str, value):
        """The EtherCAT master's writable nodes: master_state (state requests) and fieldbus_config (only in Init,
        the checksums follow). Configuration writes are kept in self.written for tests."""
        with self.lock:
            self.calls.append(("write", address, value))
            self._check_gone(address)
            if address == MASTER + "admin/fieldbus_status/master_state":
                new = (value or {}).get("request", {}).get("newState")
                if new not in ("init", "preop", "bootstrap", "safeop", "op"):
                    raise BusError(f"write {address}: DL_INVALID_VALUE")
                if new == "init" and self.fail_init:
                    self.nodes[address] = {"response": dict(self.nodes[address]["response"], requestedState="init")}
                    return
                if new == "op" and self.fail_op:
                    self.nodes[address] = {"response": {"currentState": "preop", "requestedState": "op"}}
                    return
                self.nodes[address] = {"response": {"currentState": new, "requestedState": new}}
                return
            if address == MASTER + "admin/fieldbus_config":
                if self.refuse_config:
                    raise BusError(f"write {address}: DL_FAILED")
                state = self.nodes[MASTER + "admin/fieldbus_status/master_state"]["response"]["currentState"]
                if state != "init":
                    raise BusError(f"write {address}: DL_FAILED (master not in Init)")
                req = (value or {}).get("request", {})
                eni, ini = bytes(req.get("eni") or []), bytes(req.get("ini") or [])
                self.nodes[address] = {"response": {"eni": list(eni), "ini": list(ini)}}
                self.nodes[MASTER + "admin/fieldbus_config/checksum"] = {"response": {"checksums": [
                    {"id": "eniCrc32", "checksum": checksum(eni)}, {"id": "iniCrc32", "checksum": checksum(ini)}]}}
                self.written.append((eni, ini))
                return
            if address.startswith(MASTER + "realtime_data/output/data/") and address in self.nodes:
                old = self.nodes[address]
                if isinstance(old, bool) != isinstance(value, bool) or isinstance(old, list) != isinstance(value, list):
                    raise BusError(f"write {address}: DL_TYPE_MISMATCH")
                self.nodes[address] = value
                return
            if address.startswith(MASTER + "realtime_data/input/data/") and address in self.nodes:
                raise BusError(f"write {address}: DL_PERMISSION_DENIED")
            raise BusError(f"write {address}: the fake only simulates the EtherCAT master's configuration")

    def create(self, address: str, value):
        raise BusError(f"create {address}: the fake is read-only")

    def remove(self, address: str):
        raise BusError(f"remove {address}: the fake is read-only")
