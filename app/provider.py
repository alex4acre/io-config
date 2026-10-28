# SPDX-License-Identifier: MIT

"""Data Layer nodes of io-config (for the PLC, HMIs and other apps):

  io-config/counter      uint64, counts the worker's cycles (read-only)
  io-config/setpoint     float64, writable; kept in config.json
  io-config/state        string, the app state as JSON (read-only)

Each node's value comes from a function, so nodes never hold a copy that can go stale.
"""

import json

from comm.datalayer import NodeClass
from ctrlxdatalayer.metadata_utils import AllowedOperation, MetadataBuilder, ReferenceType
from ctrlxdatalayer.provider_node import NodeCallback, ProviderNode, ProviderNodeCallbacks
from ctrlxdatalayer.variant import Result, Variant, VariantType

ROOT = "io-config"


def _meta(description: str, type_name: str, writable=False) -> Variant:
    ops = AllowedOperation.READ | AllowedOperation.BROWSE
    if writable:
        ops |= AllowedOperation.WRITE
    builder = MetadataBuilder(allowed=ops, description=description)
    builder.set_node_class(NodeClass.NodeClass.Variable)
    builder.add_reference(ReferenceType.read(), f"types/datalayer/{type_name}")
    if writable:
        builder.add_reference(ReferenceType.write(), f"types/datalayer/{type_name}")
    return builder.build()


class _Node:
    """A node whose value comes from read(); writable when write(value) is given (it returns the stored value)."""

    SETTERS = {"bool8": ("set_bool8", bool), "uint64": ("set_uint64", int), "int64": ("set_int64", int),
               "float64": ("set_float64", float), "string": ("set_string", str)}

    def __init__(self, provider, address, description, type_name, read, write=None, log=print):
        self.provider, self.address, self.description = provider, address, description
        self.type_name, self.read, self.write, self.log = type_name, read, write, log
        self.node = ProviderNode(ProviderNodeCallbacks(self.on_create, self.on_remove, self.on_browse,
                                                       self.on_read, self.on_write, self.on_metadata))

    def register(self):
        return self.provider.register_node(self.address, self.node)

    def unregister(self):
        self.provider.unregister_node(self.address)

    def _fill(self, v, value):
        setter, conv = self.SETTERS[self.type_name]
        getattr(v, setter)(conv(value))

    def on_read(self, userdata, address, data, cb: NodeCallback):
        try:
            value = self.read()
        except Exception as e:
            self.log(f"{address}: read failed: {e}")
            cb(Result.FAILED, None)
            return
        with Variant() as v:
            self._fill(v, value)
            cb(Result.OK, v)

    def on_write(self, userdata, address, data: Variant, cb: NodeCallback):
        if self.write is None:
            cb(Result.PERMISSION_DENIED, None)
            return
        t = data.get_type()
        # accept any number for a number node (the web UI and PLCs send different widths)
        number_types = {VariantType.FLOAT64, VariantType.FLOAT32, VariantType.INT64, VariantType.INT32,
                        VariantType.INT16, VariantType.INT8, VariantType.UINT64, VariantType.UINT32,
                        VariantType.UINT16, VariantType.UINT8}
        if self.type_name in ("float64", "int64", "uint64") and t in number_types:
            value = getattr(data, "get_" + t.name.lower())()
        elif self.type_name == "bool8" and t == VariantType.BOOL8:
            value = data.get_bool8()
        elif self.type_name == "string" and t == VariantType.STRING:
            value = data.get_string()
        else:
            cb(Result.TYPE_MISMATCH, None)
            return
        try:
            stored = self.write(value)
        except ValueError as e:
            self.log(f"{address}: write refused: {e}")
            cb(Result.INVALID_VALUE, None)
            return
        except Exception as e:
            self.log(f"{address}: write failed: {e}")
            cb(Result.FAILED, None)
            return
        with Variant() as v:
            self._fill(v, stored)
            cb(Result.OK, v)

    def on_metadata(self, userdata, address, cb: NodeCallback):
        cb(Result.OK, _meta(self.description, self.type_name, writable=self.write is not None))

    def on_browse(self, userdata, address, cb: NodeCallback):
        with Variant() as v:
            v.set_array_string([])
            cb(Result.OK, v)

    def on_create(self, userdata, address, data, cb):
        cb(Result.UNSUPPORTED, None)

    def on_remove(self, userdata, address, cb):
        cb(Result.UNSUPPORTED, None)


class NodeRegistry:
    """Registers the app's nodes; clear() removes them again (call it before the provider stops)."""

    def __init__(self, provider, core, log=print):
        self.provider, self.core, self.log = provider, core, log
        self.nodes = {}

    def add(self, name, description, type_name, read, write=None):
        address = f"{ROOT}/{name}"
        node = _Node(self.provider, address, description, type_name, read, write, log=self.log)
        if node.register() != Result.OK:
            self.log(f"Registering {address} failed")
        self.nodes[address] = node

    def register_all(self):
        core = self.core
        self.add("counter", "Worker cycles since the app started", "uint64", lambda: core.state()["counter"])
        self.add("setpoint", "Example setpoint (kept in config.json)", "float64",
                 lambda: core.config["setpoint"], write=core.set_setpoint)
        self.add("state", "App state (JSON)", "string", lambda: json.dumps(core.state()))

    def clear(self):
        for node in self.nodes.values():
            node.unregister()
        self.nodes = {}
