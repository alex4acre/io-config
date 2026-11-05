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
  io-config/create                write or create with a whole configuration as JSON text (docs/api.md): it is checked,
                                  built and saved under its name; the master isn't touched. Reading gives the result of
                                  the last call ("started <name>" or "error: ..."); what can't be told at once is in:
  io-config/creation/state        idle, running, done or error (of the last document)
  io-config/creation/name         the document's name
  io-config/creation/errors       JSON list of what is wrong with it
  io-config/creation/warnings     JSON list of what to look at
  io-config/scan                  write anything to start a bus scan (no EEPROMs). Reading gives the last result
  io-config/scan/state            idle, running, done or error (of the last scan)
  io-config/bus                   the last scan as JSON: scan state and error, master state, devices
"""

import json

from comm.datalayer import NodeClass
from ctrlxdatalayer.metadata_utils import AllowedOperation, MetadataBuilder, ReferenceType
from ctrlxdatalayer.provider_node import NodeCallback, ProviderNode, ProviderNodeCallbacks
from ctrlxdatalayer.variant import Result, Variant, VariantType

ROOT = "io-config"


class _Node:
    """A string node; reading calls read(), writing (if given) calls write(text)."""

    def __init__(self, provider, address, description, read, write=None, create=False):
        """create: a "create" on the node does the same as a write (for clients that start a job that way)."""
        self.provider, self.address, self.read, self.write, self.create = provider, address, read, write, create
        ops = AllowedOperation.READ | AllowedOperation.BROWSE
        if write:
            ops |= AllowedOperation.WRITE
        if create:
            ops |= AllowedOperation.CREATE
        b = MetadataBuilder(allowed=ops, description=description)
        b.set_node_class(NodeClass.NodeClass.Variable)
        b.add_reference(ReferenceType.read(), "types/datalayer/string")
        if write:
            b.add_reference(ReferenceType.write(), "types/datalayer/string")
        if create:
            b.add_reference(ReferenceType.create(), "types/datalayer/string")
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
        if not self.create:
            cb(Result.UNSUPPORTED, None)
            return
        self.on_write(userdata, address, data, cb)

    def on_remove(self, userdata, address, cb):
        cb(Result.UNSUPPORTED, None)


def _scan_state(scan: dict) -> str:
    if scan["running"]:
        return "running"
    if scan["error"]:
        return "error"
    return "done" if scan["finished"] else "idle"


class NodeRegistry:

    def __init__(self, provider, core, log=print):
        self.provider, self.core, self.log = provider, core, log
        self.nodes = {}
        self.last_activate = ""
        self.last_create = ""
        self.last_scan = ""

    def _add(self, address, description, read, write=None, create=False):
        n = _Node(self.provider, address, description, read, write, create)
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

        def create(text):
            try:
                name = core.start_create(text)
            except Exception as e:
                self.last_create = f"error: {e}"
                self.log(f"Data Layer create refused: {e}")
                raise ValueError(str(e)) from e
            self.last_create = f"started {name}"
            self.log(f"Data Layer: creating {name}")

        def scan(_text):
            try:
                core.start_scan()
            except Exception as e:
                self.last_scan = f"error: {e}"
                raise ValueError(str(e)) from e
            self.last_scan = "started"

        def bus():
            state = core.state()
            keys = ("position", "name", "description", "address", "vendor", "product", "revision", "present", "state",
                    "level", "sources", "finding_texts")
            return json.dumps({"scan": {"state": _scan_state(state["scan"]), "error": state["scan"]["error"] or ""},
                               "master": state["master"],
                               "devices": [{k: d.get(k) for k in keys} for d in state["devices"]]})

        self._add(f"{ROOT}/create", "Write or create with a whole configuration as JSON: it is built and saved under its name, "
                  "the master isn't touched (activate does that)", lambda: self.last_create, write=create, create=True)
        self._add(f"{ROOT}/creation/state", "idle, running, done or error", lambda: core.creation["state"])
        self._add(f"{ROOT}/creation/name", "Name in the last document", lambda: core.creation["name"])
        self._add(f"{ROOT}/creation/errors", "What is wrong with the last document (JSON list)",
                  lambda: json.dumps(core.creation["errors"]))
        self._add(f"{ROOT}/creation/warnings", "What to look at in the last document (JSON list)",
                  lambda: json.dumps(core.creation["warnings"]))
        self._add(f"{ROOT}/scan", "Write anything to start a bus scan", lambda: self.last_scan, write=scan)
        self._add(f"{ROOT}/scan/state", "idle, running, done or error", lambda: _scan_state(core.state()["scan"]))
        self._add(f"{ROOT}/bus", "The last scan: scan state and error, master state, devices (JSON)", bus)

    def clear(self):
        for n in self.nodes.values():
            n.unregister()
        self.nodes = {}
