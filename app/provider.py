# SPDX-License-Identifier: MIT

"""Data Layer nodes, so a PLC or another app can switch between saved bus configurations:

  io-config/configurations        JSON list of the saved configurations' names
  io-config/activate              write a name to activate that configuration: the master's configuration is backed
                                  up, the saved one written (the bus stops briefly: master Init -> Op) and its edits
                                  restored. Reading gives the result of the last write ("started <name>" or
                                  "error: ..."); an unknown name or a write already running gives an error result
  io-config/activation/state      idle, running, done or error (of the last activation)
  io-config/activation/name       the configuration of the last activation
  io-config/activation/error      its error text ("" without an error)
"""

import json

from comm.datalayer import NodeClass
from ctrlxdatalayer.metadata_utils import AllowedOperation, MetadataBuilder, ReferenceType
from ctrlxdatalayer.provider_node import NodeCallback, ProviderNode, ProviderNodeCallbacks
from ctrlxdatalayer.variant import Result, Variant, VariantType

ROOT = "io-config"


class _Node:
    """A string node; reading calls read(), writing (if given) calls write(text)."""

    def __init__(self, provider, address, description, read, write=None):
        self.provider, self.address, self.read, self.write = provider, address, read, write
        ops = AllowedOperation.READ | AllowedOperation.BROWSE
        if write:
            ops |= AllowedOperation.WRITE
        b = MetadataBuilder(allowed=ops, description=description)
        b.set_node_class(NodeClass.NodeClass.Variable)
        b.add_reference(ReferenceType.read(), "types/datalayer/string")
        if write:
            b.add_reference(ReferenceType.write(), "types/datalayer/string")
        self.meta = b.build()
        self.node = ProviderNode(ProviderNodeCallbacks(self.on_create, self.on_remove, self.on_browse,
                                                       self.on_read, self.on_write, self.on_metadata))

    def register(self):
        return self.provider.register_node(self.address, self.node)

    def unregister(self):
        self.provider.unregister_node(self.address)

    def on_read(self, userdata, address, data, cb: NodeCallback):
        try:
            text = self.read()
        except Exception:
            cb(Result.FAILED, None)
            return
        with Variant() as v:
            v.set_string(text if isinstance(text, str) else json.dumps(text))
            cb(Result.OK, v)

    def on_write(self, userdata, address, data: Variant, cb: NodeCallback):
        if self.write is None:
            cb(Result.PERMISSION_DENIED, None)
            return
        if data.get_type() != VariantType.STRING:
            cb(Result.TYPE_MISMATCH, None)
            return
        text = data.get_string()
        try:
            self.write(text)
        except (ValueError, KeyError):
            cb(Result.INVALID_VALUE if hasattr(Result, "INVALID_VALUE") else Result.FAILED, None)
            return
        except Exception:
            cb(Result.FAILED, None)
            return
        with Variant() as v:
            v.set_string(text)
            cb(Result.OK, v)

    def on_metadata(self, userdata, address, cb: NodeCallback):
        cb(Result.OK, self.meta)

    def on_browse(self, userdata, address, cb: NodeCallback):
        with Variant() as v:
            v.set_array_string([])
            cb(Result.OK, v)

    def on_create(self, userdata, address, data, cb):
        cb(Result.UNSUPPORTED, None)

    def on_remove(self, userdata, address, cb):
        cb(Result.UNSUPPORTED, None)


class NodeRegistry:

    def __init__(self, provider, core, log=print):
        self.provider, self.core, self.log = provider, core, log
        self.nodes = {}
        self.last_activate = ""

    def _add(self, address, description, read, write=None):
        n = _Node(self.provider, address, description, read, write)
        if n.register() != Result.OK:
            self.log(f"Registering {address} failed")
        self.nodes[address] = n

    def register(self):
        core = self.core

        def activate(text):
            name = text.strip()
            try:
                core.start_activate(name)
            except Exception as e:
                self.last_activate = f"error: {e}"
                self.log(f"Data Layer activate {name!r} refused: {e}")
                raise ValueError(str(e)) from e
            self.last_activate = f"started {name}"
            self.log(f"Data Layer: activating {name}")

        self._add(f"{ROOT}/configurations", "Names of the saved configurations (JSON list)",
                  lambda: json.dumps([c["name"] for c in core.saved()]))
        self._add(f"{ROOT}/activate", "Write a saved configuration's name to activate it",
                  lambda: self.last_activate, write=activate)
        for field, description in (("state", "idle, running, done or error"), ("name", "Configuration of the last activation"),
                                   ("error", "Error text of the last activation")):
            self._add(f"{ROOT}/activation/{field}", description, lambda f=field: str(core.activation[f] or ""))

    def clear(self):
        for n in self.nodes.values():
            n.unregister()
        self.nodes = {}
