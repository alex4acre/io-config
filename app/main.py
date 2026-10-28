# SPDX-License-Identifier: MIT

"""I/O Config: EtherCAT bus scan with ESI, ENI and EEPROM cross-check

In the snap: UI at https://<ctrlx>/io-config, configuration in the app data of the active
configuration (io-config/config.json), Data Layer nodes under io-config/...

Development with a simulated ctrlX:  IO_CONFIG_FAKE=1 python3 -m app.main
                                     -> http://127.0.0.1:12617/io-config/?token=dev
"""

import os
import signal
import threading
import time

import app.web
from app.core import Core

SNAP_NAME = "io-config"
IN_SNAP = os.environ.get("SNAP_NAME") == SNAP_NAME
TCP_PORT = int(os.environ.get("IO_CONFIG_PORT", "12617"))
SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

__close_app = False


def handler(signum, frame):
    global __close_app
    __close_app = True


def log(message: str):
    print(message, flush=True)


def config_path() -> str:
    if not IN_SNAP:
        return os.environ.get("IO_CONFIG_CONFIG", os.path.join(SRC, "data", "config.json"))
    # the active configuration appears a little after the app starts on boot
    active = os.path.join(os.environ["SNAP_COMMON"], "solutions", "activeConfiguration")
    waited = 0
    while not os.path.isdir(active) and not __close_app:
        if waited % 10 == 0:
            log(f"Waiting for the active configuration {active}")
        time.sleep(1.0)
        waited += 1
    return os.path.join(active, SNAP_NAME, "config.json")


def start_datalayer():
    """(system, provider, bus) - the client bus and the provider share one Data Layer system."""
    if not IN_SNAP and os.environ.get("IO_CONFIG_FAKE"):
        from app.fake import FakeBus
        log("Using a simulated ctrlX (development mode)")
        return None, None, FakeBus()
    # outside the snap, e.g. "tcp://boschrexroth:boschrexroth@192.168.1.1?sslport=443" (needs Ubuntu 24.04)
    connection = "ipc://" if IN_SNAP else os.environ.get("DATALAYER_CONNECTION")
    if not connection:
        raise SystemExit("Set IO_CONFIG_FAKE=1 (simulated ctrlX) or DATALAYER_CONNECTION")
    import ctrlxdatalayer
    from app.bus import DatalayerBus
    system = ctrlxdatalayer.system.System("")
    system.start(False)
    bus = DatalayerBus(system, connection)
    while not __close_app:
        provider = system.factory().create_provider(connection)
        if provider.start() == ctrlxdatalayer.variant.Result.OK:
            return system, provider, bus
        provider.close()
        log("Starting the Data Layer provider failed, retrying in 5 s")
        time.sleep(5)
    return system, None, bus


def start_webserver():
    if IN_SNAP:
        # the ctrlX reverse proxy forwards /io-config to this socket (package-manifest.json, proxyMapping)
        sock_dir = os.path.join(os.environ["SNAP_DATA"], "package-run", SNAP_NAME)
        os.makedirs(sock_dir, exist_ok=True)
        sock_file = os.path.join(sock_dir, "web.sock")
        try:
            os.unlink(sock_file)
        except OSError:
            pass
        server = app.web.UnixSocketServer(sock_file, app.web.RequestHandler)
        log(f"Listening on {sock_file}")
    else:
        server = app.web.TcpServer(("127.0.0.1", TCP_PORT), app.web.RequestHandler)
        log(f"Listening on http://127.0.0.1:{TCP_PORT}{app.web.BASE}/?token=dev")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main():
    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)

    path = config_path()
    if __close_app:
        return
    system, provider, bus = start_datalayer()
    if __close_app:
        return
    registry_factory = None
    if provider is not None:
        from app.provider import NodeRegistry
        registry_factory = lambda core: NodeRegistry(provider, core, log=log)   # noqa: E731
    core = Core(path, bus, registry_factory=registry_factory, log=log)
    core.start()
    log(f"Configuration: {path}")
    app.web.core = core
    app.web.www_dir = os.path.join(os.environ["SNAP"], "www") if IN_SNAP else os.path.join(SRC, "www")
    server = start_webserver()

    last_check = time.monotonic()
    while not __close_app:
        time.sleep(0.5)
        if time.monotonic() - last_check > 5:
            last_check = time.monotonic()
            core.reload_if_changed()

    server.shutdown()
    server.server_close()
    core.stop()
    if provider is not None:
        provider.stop()
        provider.close()
    if hasattr(bus, "close"):
        bus.close()
    if system is not None:
        system.stop(False)
    log("Stopped")


if __name__ == "__main__":
    main()
