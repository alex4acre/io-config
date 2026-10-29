# SPDX-License-Identifier: MIT

"""Checks app.bus.DatalayerBus against the REAL ctrlX Data Layer library with a local broker (no ctrlX needed).
Needs Ubuntu 24.04 (the library needs GLIBCXX_3.4.31), e.g. an LXD container set up by the ctrlx-app-starter
skill's scripts/dl_container.sh:

    scripts/dl_container.sh check <app folder>

or by hand:  apt install ./ctrlx-datalayer-*.deb;  python3 -m venv v;  v/bin/pip install ctrlx-datalayer ctrlx-fbs
             v/bin/python tests/integration/dl_check.py

A provider here plays the EtherCAT master's "Method" nodes (slave_online_info, slave_eeprom) with the master's
real flatbuffers types (bfbs/ from the ctrlX SDK), answering from the recording in tests/data, so read_args
converts and reads exactly as it does against a ctrlX.
"""

import json
import os
import sys

import ctrlxdatalayer
from comm.datalayer import NodeClass
from ctrlxdatalayer.metadata_utils import AllowedOperation, MetadataBuilder, ReferenceType
from ctrlxdatalayer.provider_node import NodeCallback, ProviderNode, ProviderNodeCallbacks
from ctrlxdatalayer.variant import Result, Variant

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", ".."))
from app.bus import BusError, DatalayerBus  # noqa: E402
from app.fake import MASTER, recorded_nodes  # noqa: E402
from app import sii  # noqa: E402

failures = []


def check(cond, text):
    print(("ok   " if cond else "FAIL ") + text, flush=True)
    if not cond:
        failures.append(text)


class MethodNode:
    """A read-with-arguments node: answer(args) -> the answer as JSON, both in the node's flatbuffers type."""

    def __init__(self, system, type_address, answer):
        self.converter = system.json_converter()
        self.type_address, self.answer = type_address, answer
        self.bfbs = None
        self.seen = []
        self.node = ProviderNode(ProviderNodeCallbacks(self.on_create, self.on_remove, self.on_browse,
                                                       self.on_read, self.on_write, self.on_metadata))

    def on_read(self, userdata, address, data: Variant, cb: NodeCallback):
        with Variant() as tv:
            tv.set_flatbuffers(bytearray(self.bfbs))
            r, js = self.converter.converter_generate_json_complex(data, tv, -1)
            with js:
                if r != Result.OK:
                    cb(Result.INVALID_VALUE, None)
                    return
                args = json.loads(js.get_string())
            self.seen.append(args)
            try:
                answer = self.answer(args.get("request", {}))
            except LookupError:
                cb(Result.FAILED, None)
                return
            r, out, err = self.converter.parse_json_complex(json.dumps(answer), tv)
            with out, err:
                cb(Result.OK if r == Result.OK else Result.FAILED, out if r == Result.OK else None)

    def on_metadata(self, userdata, address, cb: NodeCallback):
        builder = MetadataBuilder(allowed=AllowedOperation.READ, description="simulated EtherCAT master method")
        builder.set_node_class(NodeClass.NodeClass.Method)
        builder.add_reference(ReferenceType.read(), self.type_address)
        cb(Result.OK, builder.build())

    def on_write(self, userdata, address, data, cb):
        cb(Result.UNSUPPORTED, None)

    def on_browse(self, userdata, address, cb):
        with Variant() as v:
            v.set_array_string([])
            cb(Result.OK, v)

    def on_create(self, userdata, address, data, cb):
        cb(Result.UNSUPPORTED, None)

    def on_remove(self, userdata, address, cb):
        cb(Result.UNSUPPORTED, None)


class VariableNode(MethodNode):
    """A readable/writable flatbuffers node (master_state, fieldbus_config): keeps the last written value as JSON."""

    def __init__(self, system, type_address, value):
        super().__init__(system, type_address, None)
        self.value = value
        self.written = []

    def _answer(self, cb, value):
        with Variant() as tv:
            tv.set_flatbuffers(bytearray(self.bfbs))
            r, out, err = self.converter.parse_json_complex(json.dumps(value), tv)
            with out, err:
                cb(Result.OK if r == Result.OK else Result.FAILED, out if r == Result.OK else None)

    def on_read(self, userdata, address, data, cb):
        self._answer(cb, self.value)

    def on_write(self, userdata, address, data: Variant, cb):
        with Variant() as tv:
            tv.set_flatbuffers(bytearray(self.bfbs))
            r, js = self.converter.converter_generate_json_complex(data, tv, -1)
            with js:
                if r != Result.OK:
                    cb(Result.INVALID_VALUE, None)
                    return
                value = json.loads(js.get_string())
        self.written.append(value)
        self._answer(cb, value)

    def on_metadata(self, userdata, address, cb):
        builder = MetadataBuilder(allowed=AllowedOperation.READ | AllowedOperation.WRITE, description="simulated")
        builder.set_node_class(NodeClass.NodeClass.Variable)
        builder.add_reference(ReferenceType.read(), self.type_address)
        builder.add_reference(ReferenceType.write(), self.type_address)
        cb(Result.OK, builder.build())


def main():
    _nodes, slaves = recorded_nodes()
    by_address = {s["autoinc"]: s for s in slaves}

    def online(req):
        return by_address[req["address"]]["online_info"]

    def eeprom(req):
        words = by_address[req["address"]]["eeprom"]
        return {"response": {"data": words[req["eepromOffset"]:req["eepromOffset"] + req["maxLength"]]}}

    system = ctrlxdatalayer.system.System("")
    system.start(True)                         # with a broker: this is the local Data Layer
    provider = system.factory().create_provider("ipc://")
    check(provider.start() == Result.OK, "provider started with a local broker")
    methods = {}
    def soe_parameter(req):
        return {"response": {"elementFlags": req.get("elementFlags"), "data": [0xD0, 0x07]}}

    for name, answer in (("slave_online_info", online), ("slave_eeprom", eeprom), ("parameter", soe_parameter)):
        type_address = f"types/ethercat/master/{name}"
        bfbs = os.path.join(HERE, "bfbs", name + ".bfbs")
        check(provider.register_type(type_address, bfbs) == Result.OK, f"type {type_address} registered")
        m = MethodNode(system, type_address, answer)
        with open(bfbs, "rb") as f:
            m.bfbs = f.read()
        node = MASTER + "device_access/" + ("soe/parameter" if name == "parameter" else name)
        check(provider.register_node(node, m.node) == Result.OK, f"node {name} registered")
        methods[name] = m
    variables = {}
    for name, path, value in (("master_state", "admin/fieldbus_status/master_state",
                               {"response": {"currentState": "op", "requestedState": "op"}}),
                              ("fieldbus_config", "admin/fieldbus_config", {"response": {"eni": [60, 97, 47, 62], "ini": [10]}}),
                              ("sdo", "device_access/coe/sdo", {"response": {"data": [0, 0, 0, 0]}})):
        type_address = f"types/ethercat/master/{name}"
        bfbs = os.path.join(HERE, "bfbs", name + ".bfbs")
        check(provider.register_type(type_address, bfbs) == Result.OK, f"type {type_address} registered")
        v = VariableNode(system, type_address, value)
        with open(bfbs, "rb") as f:
            v.bfbs = f.read()
        check(provider.register_node(MASTER + path, v.node) == Result.OK, f"node {name} registered")
        variables[name] = (path, v)
    bus = DatalayerBus(system, "ipc://")
    try:
        path, v = variables["master_state"]
        bus.write(MASTER + path, {"request": {"newState": "init"}})
        check(v.written and v.written[-1].get("request") == {"newState": "init"}, f"master_state written: {v.written}")
        path, v = variables["fieldbus_config"]
        check(bus.read(MASTER + path)["response"]["eni"] == [60, 97, 47, 62], "fieldbus_config read as byte list")
        eni_bytes = open(os.path.join(HERE, "..", "data", "controller-eni.xml"), "rb").read()
        bus.write(MASTER + path, {"request": {"eni": list(eni_bytes), "ini": [65, 13, 10]}})
        got = bytes(v.written[-1]["request"]["eni"]) if v.written else b""
        check(got == eni_bytes and v.written[-1]["request"]["ini"] == [65, 13, 10],
              f"fieldbus_config written: the provider got the {len(got)}-byte ENI unchanged")
        path, v = variables["sdo"]
        bus.write_json(MASTER + path, {"request": {"addressType": "autoincrement", "address": 65534, "objectIndex": 0x8000,
                                                   "subIndex": 1, "flags": "noFlags", "data": [232, 3, 0, 0], "maxLength": 4}})
        req = v.written[-1].get("request", {}) if v.written else {}
        check(req.get("objectIndex") == 0x8000 and req.get("data") == [232, 3, 0, 0] and req.get("flags") == "noFlags",
              f"SDO download via write_json arrives as sent: {req}")
        check(bus.is_connected(), "client connected")
        a = bus.read_args(MASTER + "device_access/slave_online_info",
                          {"request": {"addressType": "autoincrement", "address": 65534}})
        r = a.get("response", {})
        check(r.get("ethercatAddr") == 1003 and r.get("slaveIdentity", {}).get("productCode") == 0x242A1F,
              f"slave_online_info at 65534 -> {r.get('ethercatAddr')}, {r.get('slaveIdentity')}")
        check(methods["slave_online_info"].seen[-1].get("request") == {"addressType": "autoincrement", "address": 65534},
              f"the request arrived as sent: {methods['slave_online_info'].seen[-1]}")
        words = []
        while True:
            ans = bus.read_args(MASTER + "device_access/slave_eeprom", {"request": {
                "addressType": "autoincrement", "address": 65534, "eepromOffset": len(words), "maxLength": 64}})
            chunk = ans.get("response", {}).get("data") or []
            words += chunk
            if not chunk or sii.end_reached(words):
                break
        check(sii.decode(words)["order"] == "XI422202", f"EEPROM read in chunks and decoded ({len(words)} words)")
        for flags in ("value", "attribute"):
            ans = bus.read_args(MASTER + "device_access/soe/parameter", {"request": {
                "addressType": "autoincrement", "address": 65532, "driveNumber": 0, "elementFlags": flags, "idn": 1,
                "maxLength": 16}})
            seen = methods["parameter"].seen[-1].get("request", {})
            check(seen.get("elementFlags") == flags and seen.get("idn") == 1 and ans.get("response", {}).get("data") == [0xD0, 0x07],
                  f"SoE parameter read with elementFlags {flags!r}: request {seen}, answer {ans}")
        try:
            bus.read_args(MASTER + "device_access/slave_online_info",
                          {"request": {"addressType": "autoincrement", "address": 65532}})
            check(False, "past the last device raises")
        except BusError as e:
            check(True, f"past the last device raises ({e})")
        try:
            bus.read_args(MASTER + "device_access/slave_online_info", {"request": {"nosuchfield": 1}})
            check(False, "a request the type doesn't know raises")
        except BusError as e:
            check(True, f"a request the type doesn't know raises ({e})")
        nodes_check(provider, bus)
    finally:
        bus.close()
        for name in methods:
            provider.unregister_node(MASTER + "device_access/" + ("soe/parameter" if name == "parameter" else name))
        for path, _v in variables.values():
            provider.unregister_node(MASTER + path)
        provider.stop()
        provider.close()
        system.stop(True)
    print("FAILED: " + "; ".join(failures) if failures else "ALL OK", flush=True)
    return 1 if failures else 0


def nodes_check(provider, bus):
    """The app's own nodes (app/provider.py) read and written through a client, as a PLC would."""
    from app.provider import NodeRegistry

    class Core:                                       # what NodeRegistry uses of app.core.Core
        activation = {"state": "idle", "name": "", "error": "", "started": None, "finished": None}
        started = []

        def saved(self):
            return [{"name": "Line A"}, {"name": "Line B"}]

        def start_activate(self, name):
            if name not in ("Line A", "Line B"):
                raise KeyError(f"no saved configuration {name}")
            self.started.append(name)
            self.activation = dict(self.activation, state="running", name=name)

    core = Core()
    reg = NodeRegistry(provider, core, log=lambda m: None)
    reg.register()
    try:
        check(json.loads(bus.read("io-config/configurations")) == ["Line A", "Line B"], "configurations node lists the names")
        check(bus.read("io-config/activation/state") == "idle", "activation/state reads idle")
        bus.write("io-config/activate", "Line B")
        check(core.started == ["Line B"] and bus.read("io-config/activation/state") == "running" and
              bus.read("io-config/activation/name") == "Line B", "writing a name to activate starts it")
        check(bus.read("io-config/activate") == "started Line B", "activate reads the result of the last write")
        try:
            bus.write("io-config/activate", "nothing")
            check(False, "an unknown name is refused")
        except BusError as e:
            check(bus.read("io-config/activate").startswith("error: ") and core.started == ["Line B"],
                  f"an unknown name is refused ({e})")
    finally:
        reg.clear()


if __name__ == "__main__":
    sys.exit(main())
