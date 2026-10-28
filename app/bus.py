# SPDX-License-Identifier: MIT

"""Access to the ctrlX Data Layer as a client.

Values come back as plain Python: bool, int, float, str, lists, and dicts for flatbuffers objects
(converted to JSON with the node's read type, as the ctrlX web UI shows them). Objects are created
from dicts in the same JSON form (converted with the node's create type).

DatalayerBus is the real client; app.fake.FakeBus simulates a ctrlX for development and tests.
"""

import json
import threading


class BusError(Exception):
    pass


class DatalayerBus:

    def __init__(self, system, connection: str):
        import ctrlxdatalayer
        from ctrlxdatalayer.variant import Result, Variant, VariantType
        from comm.datalayer import Metadata, NodeClass
        self._dl = ctrlxdatalayer
        self._Result, self._Variant, self._VariantType = Result, Variant, VariantType
        self._Metadata, self._NodeClass = Metadata, NodeClass
        self.system = system
        self.client = system.factory().create_client(connection)
        self.converter = system.json_converter()
        self.lock = threading.RLock()        # one call at a time on the client
        self.type_cache = {}                  # type address -> bfbs bytes

    def is_connected(self) -> bool:
        return self.client.is_connected()

    def close(self):
        self.client.close()

    def _check(self, result, what):
        if result != self._Result.OK:
            raise BusError(f"{what}: {result.name}")

    # --- basic operations ---------------------------------------------------------------------------

    def browse(self, address: str) -> list:
        with self.lock:
            result, v = self.client.browse_sync(address)
            with v:
                self._check(result, f"browse {address}")
                return list(v.get_array_string() or [])

    def metadata(self, address: str) -> dict:
        with self.lock:
            result, v = self.client.metadata_sync(address)
            with v:
                self._check(result, f"metadata {address}")
                m = self._Metadata.Metadata.GetRootAsMetadata(v.get_flatbuffers(), 0)
                ops = m.Operations()
                names = {v: k for k, v in vars(self._NodeClass.NodeClass).items() if not k.startswith("_")}
                refs = {}
                for i in range(m.ReferencesLength()):
                    r = m.References(i)
                    refs[r.Type().decode()] = r.TargetAddress().decode()
                return {
                    "nodeClass": names.get(m.NodeClass(), str(m.NodeClass())),
                    "operations": {"read": ops.Read(), "write": ops.Write(), "create": ops.Create(),
                                   "delete": ops.Delete(), "browse": ops.Browse()} if ops else {},
                    "description": (m.Description() or b"").decode(),
                    "references": refs,
                }

    def read(self, address: str):
        with self.lock:
            result, v = self.client.read_sync(address)
            with v:
                self._check(result, f"read {address}")
                return self._to_python(address, v)

    def read_args(self, address: str, args: dict):
        """Read a node that takes input (a "Method" node, e.g. the EtherCAT master's slave_online_info):
        args in JSON form are converted with the node's read type and sent along with the read."""
        with self.lock:
            read_type = self.metadata(address)["references"].get("readType", "")
            if not read_type:
                raise BusError(f"read {address}: the node has no read type for arguments")
            with self._Variant() as tv:
                tv.set_flatbuffers(bytearray(self._type(read_type)))      # the library needs a writable buffer
                r, data, err = self.converter.parse_json_complex(json.dumps(args), tv)
                with data, err:
                    self._check(r, f"convert for {read_type} ({err.get_string() or ''})")
                    result, out = self.client.read_sync_args(address, data)   # out is data, now holding the answer
                    self._check(result, f"read {address}")
                    return self._to_python(address, out)

    def write_json(self, address: str, value):
        """Write a flatbuffers node from JSON without reading it first (a "Method" node such as the EtherCAT
        master's coe/sdo can't be read without arguments)."""
        with self.lock:
            result, err = self.client.write_json_sync(self.converter, address, json.dumps(value))
            with err:
                self._check(result, f"write {address} ({err.get_string() or ''})")

    def write(self, address: str, value):
        """Writes in the node's current type (read first, as the ctrlX samples do)."""
        with self.lock:
            result, v = self.client.read_sync(address)
            with v:
                self._check(result, f"read {address}")
                t = v.get_type()
                if t == self._VariantType.FLATBUFFERS:
                    result, err = self.client.write_json_sync(self.converter, address, json.dumps(value))
                    with err:
                        self._check(result, f"write {address} ({err.get_string() or ''})")
                    return
                self._set(v, t, value)
                result, _ = self.client.write_sync(address, v)
                self._check(result, f"write {address}")

    def create(self, address: str, value):
        """Create a node or run a "create" command (e.g. a MOTION command, a copy list) from JSON.

        create_sync answers in the same Variant it was given, so the answer is read while that
        Variant is still open."""
        with self.lock:
            create_type = self.metadata(address)["references"].get("createType", "")
            if value is None or not create_type:
                with self._Variant() as v:                     # a command without parameters
                    return self._create_and_read(address, v)
            # plain types (types/datalayer/bool8, string, array-of-float64, ...) are Variant types;
            # everything else (rt-copylist, MOTION command parameters, ...) is a flatbuffers type
            simple = None
            if create_type.startswith("types/datalayer/"):
                name = create_type[len("types/datalayer/"):].upper().replace("-", "_").replace("ARRAY_OF_", "ARRAY_")
                simple = getattr(self._VariantType, name, None)
            if simple is not None:
                with self._Variant() as v:
                    self._set(v, simple, value)
                    return self._create_and_read(address, v)
            with self._Variant() as tv:
                tv.set_flatbuffers(bytearray(self._type(create_type)))      # the library needs a writable buffer
                r, data, err = self.converter.parse_json_complex(json.dumps(value), tv)
                with data, err:
                    self._check(r, f"convert for {create_type} ({err.get_string() or ''})")
                    return self._create_and_read(address, data)

    def _create_and_read(self, address, v):
        result, out = self.client.create_sync(address, v)      # out is v, now holding the answer
        self._check(result, f"create {address}")
        return self._create_result(address, out)

    def remove(self, address: str):
        with self.lock:
            self._check(self.client.remove_sync(address), f"remove {address}")

    # --- conversions --------------------------------------------------------------------------------

    def _type(self, type_address: str) -> bytes:
        if type_address not in self.type_cache:
            result, v = self.client.read_sync(type_address)
            with v:
                self._check(result, f"read type {type_address}")
                self.type_cache[type_address] = bytes(v.get_flatbuffers())
        return self.type_cache[type_address]

    def _create_result(self, address, out):
        """What a create answers, e.g. a MOTION command's id. MOTION answers with a flatbuffers
        object of its createOutType (types/datalayer/uint64 = {"value": <id>}), not a plain number."""
        if out.get_type().value == 0:
            return None
        if out.get_type() == self._VariantType.FLATBUFFERS:
            out_type = self.metadata(address)["references"].get("createOutType", "")
            if not out_type:
                return None
            with self._Variant() as tv:
                tv.set_flatbuffers(bytearray(self._type(out_type)))
                r, js = self.converter.converter_generate_json_complex(out, tv, -1)
                with js:
                    self._check(r, f"convert the answer of {address}")
                    value = json.loads(js.get_string())
        else:
            value = self._to_python(address, out)
        if isinstance(value, dict) and set(value) == {"value"}:
            return value["value"]
        return value

    def _to_python(self, address, v):
        VT = self._VariantType
        t = v.get_type()
        if t == VT.FLATBUFFERS:
            read_type = self.metadata(address)["references"].get("readType", "")
            with self._Variant() as tv:
                tv.set_flatbuffers(bytearray(self._type(read_type)))
                result, js = self.converter.converter_generate_json_complex(v, tv, -1)
                with js:
                    self._check(result, f"convert {address}")
                    return json.loads(js.get_string())
        if t.value == 0:              # VariantType.UNKNON (sic)
            return None
        getter = getattr(v, "get_" + t.name.lower(), None)
        if getter is None:
            raise BusError(f"{address}: unsupported value type {t.name}")
        value = getter()
        return list(value) if isinstance(value, (tuple, bytes, bytearray)) else value

    def _set(self, v, t, value):
        VT = self._VariantType
        if t is None:
            raise BusError("unsupported value type")
        name = t.name.lower()
        if name.startswith("array_"):
            items = list(value)
            if "bool8" in name:
                items = [bool(x) for x in items]
            elif "float" in name:
                items = [float(x) for x in items]
            elif "string" in name:
                items = [str(x) for x in items]
            else:
                items = [int(x) for x in items]
            getattr(v, "set_" + name)(items)
        elif t == VT.BOOL8:
            v.set_bool8(bool(value))
        elif t == VT.STRING:
            v.set_string(str(value))
        elif name.startswith("float"):
            getattr(v, "set_" + name)(float(value))
        else:
            getattr(v, "set_" + name)(int(value))
