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
    for name, answer in (("slave_online_info", online), ("slave_eeprom", eeprom)):
        type_address = f"types/ethercat/master/{name}"
        bfbs = os.path.join(HERE, "bfbs", name + ".bfbs")
        check(provider.register_type(type_address, bfbs) == Result.OK, f"type {type_address} registered")
        m = MethodNode(system, type_address, answer)
        with open(bfbs, "rb") as f:
            m.bfbs = f.read()
        check(provider.register_node(MASTER + "device_access/" + name, m.node) == Result.OK, f"node {name} registered")
        methods[name] = m
    bus = DatalayerBus(system, "ipc://")
    try:
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
    finally:
        bus.close()
        for name in methods:
            provider.unregister_node(MASTER + "device_access/" + name)
        provider.stop()
        provider.close()
        system.stop(True)
    print("FAILED: " + "; ".join(failures) if failures else "ALL OK", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
