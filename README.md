# I/O Config

EtherCAT bus scan with ESI, ENI and EEPROM cross-check

A ctrlX CORE app (snap, core24, Python 3.12 in the snap). Generated from the ctrlx-app-starter template.

- **Web UI / REST:** `https://<ctrlx>/io-config` (sidebar entry "I/O Config"); REST under `/io-config/api/v1`, protected by the ctrlX proxy (bearer token).
- **Data Layer:** `io-config/counter` (uint64), `io-config/setpoint` (float64, writable), `io-config/state` (JSON string).
- **Configuration:** `io-config/config.json` in the app data of the active configuration (Settings → Manage app data); saved with ctrlX configurations and backups.

## Layout

| Path | What |
| --- | --- |
| `app/main.py` | start-up: config path, Data Layer (or the fake), web server (unix socket in the snap, TCP in development) |
| `app/core.py` | configuration file and the worker: replace the example logic here |
| `app/provider.py` | the app's own Data Layer nodes |
| `app/bus.py` | Data Layer client: browse/read/write/create/remove with plain Python values (flatbuffers as JSON) |
| `app/fake.py` | simulated Data Layer with the same interface, for development and tests |
| `app/web.py` | REST routes and static files |
| `www/` | the UI (plain HTML/JS/CSS, no build step) |
| `configs/package-assets/io-config.package-manifest.json` | proxy mapping, sidebar menu, app data folder, configuration load/save hooks |
| `snap/snapcraft.yaml` | snap definition; `deps` part = third-party libraries only, so code-only rebuilds are fast |
| `deb/` | the ctrlx-datalayer .deb whose `libcomm_datalayer.so` is bundled |

## Develop

    IO_CONFIG_FAKE=1 python3 -m app.main          # http://127.0.0.1:12617/io-config/?token=dev
    python3 -m unittest -q                       # unit tests against the fake
    python3 tests/ui/ui_check.py <folder>        # headless Firefox check (see the file for set-up)
    tests/integration/dl_check.py                # real Data Layer library, Ubuntu 24.04 only

## Build and install

    snapcraft pack --build-for=amd64 --use-lxd 2>&1 | tee build.log     # ctrlX COREvirtual
    snapcraft pack --build-for=arm64 --use-lxd 2>&1 | tee build.log     # ctrlX CORE X3/X5/X7

Bump `version` in `snap/snapcraft.yaml` and `pyproject.toml` for every build you install.
Install: ctrlX web UI → Settings → Apps → enable "Allow installation from unknown source" → Install from file.
Logs: `sudo snap logs -f io-config` over SSH (app output does not always reach the web Logbook).
