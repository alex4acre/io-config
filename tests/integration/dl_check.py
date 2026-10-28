# SPDX-License-Identifier: MIT

"""Checks app.bus.DatalayerBus and app.provider against the REAL ctrlX Data Layer library with a local broker
(no ctrlX needed). Needs Ubuntu 24.04 (the library needs GLIBCXX_3.4.31), e.g. an LXD container set up by the
ctrlx-app-starter skill's scripts/dl_container.sh:

    scripts/dl_container.sh check <app folder>

or by hand:  apt install ./ctrlx-datalayer-*.deb;  python3 -m venv v;  v/bin/pip install ctrlx-datalayer ctrlx-fbs
             v/bin/python tests/integration/dl_check.py
"""

import json
import os
import sys
import tempfile
import time

import ctrlxdatalayer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from app.bus import BusError, DatalayerBus  # noqa: E402
from app.core import Core  # noqa: E402
from app.provider import NodeRegistry  # noqa: E402

failures = []


def check(cond, text):
    print(("ok   " if cond else "FAIL ") + text, flush=True)
    if not cond:
        failures.append(text)


def main():
    system = ctrlxdatalayer.system.System("")
    system.start(True)                         # with a broker: this is the local Data Layer
    provider = system.factory().create_provider("ipc://")
    check(provider.start() == ctrlxdatalayer.variant.Result.OK, "provider started with a local broker")
    bus = DatalayerBus(system, "ipc://")
    tmp = tempfile.TemporaryDirectory()
    core = Core(os.path.join(tmp.name, "config.json"), bus,
                registry_factory=lambda c: NodeRegistry(provider, c), log=print)
    core.start()
    time.sleep(0.5)
    try:
        check(bus.is_connected(), "client connected")
        children = bus.browse("io-config")
        check(sorted(children) == ["counter", "setpoint", "state"], f"browse io-config -> {children}")
        check(isinstance(bus.read("io-config/counter"), int), "read counter (uint64)")
        check(bus.read("io-config/setpoint") == 0.0, "read setpoint (float64)")
        bus.write("io-config/setpoint", 4.25)
        check(core.config["setpoint"] == 4.25 and bus.read("io-config/setpoint") == 4.25, "write setpoint through the Data Layer")
        state = json.loads(bus.read("io-config/state"))
        check(state.get("setpoint") == 4.25, f"read state (JSON string) -> {state}")
        md = bus.metadata("io-config/setpoint")
        check(md["operations"].get("write") is True and md["references"].get("readType") == "types/datalayer/float64",
              f"metadata -> {md}")
        try:
            bus.write("io-config/counter", 5)
            check(False, "writing a read-only node is refused")
        except BusError as e:
            check(True, f"writing a read-only node is refused ({e})")
        try:
            bus.read("io-config/nothing")
            check(False, "reading a missing node raises")
        except BusError as e:
            check(True, f"reading a missing node raises ({e})")
    finally:
        core.stop()
        bus.close()
        provider.stop()
        provider.close()
        system.stop(True)
        tmp.cleanup()
    print("FAILED: " + "; ".join(failures) if failures else "ALL OK", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
