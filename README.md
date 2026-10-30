# I/O Config

EtherCAT bus scan with ESI, ENI and EEPROM cross-check

A ctrlX CORE app (snap, core24, Python 3.12 in the snap). Generated from the ctrlx-app-starter template.
Design and phases: [docs/design.md](docs/design.md). Phase 1 (this version) only reads.

- **Web UI / REST:** `https://<ctrlx>/io-config` (sidebar entry "I/O Config"); REST under `/io-config/api/v1`, protected by the ctrlX proxy (bearer token).
- **Data Layer:** reads the EtherCAT master under `fieldbuses/ethercat/master/instances/<instance>/` (status, `admin/fieldbus_config`, `device_access/slave_online_info` and `slave_eeprom` with arguments). No nodes of its own.
- **Configuration:** `io-config/config.json` (`{"instance": "ethercatmaster"}`) in the app data of the active configuration.
- **ESI library:** `$SNAP_COMMON/esi` (uploaded files + `index.json`); not part of the active configuration because it can be large.

## Layout

| Path | What |
| --- | --- |
| `app/main.py` | start-up: config path, Data Layer (or the fake), web server (unix socket in the snap, TCP in development) |
| `app/core.py` | configuration file, ESI library, the scan (thread, EEPROM cache) |
| `app/sii.py` | decodes a device's EEPROM (SII) |
| `app/eni.py` | parses the ENI and ini the master runs with |
| `app/esi.py` | ESI library: upload (XML/zip), index, lookup, device details |
| `app/compare.py` | one position's sources side by side, with findings |
| `app/xmlsafe.py` | XML parsing that refuses DOCTYPE/ENTITY |
| `app/bus.py` | Data Layer client: browse/read/read_args/write/create/remove with plain Python values (flatbuffers as JSON) |
| `app/fake.py` | simulated Data Layer replaying a recorded ctrlX CORE X3 bus (`tests/data`) |
| `app/web.py` | REST routes and static files |
| `www/` | the UI (plain HTML/JS/CSS, no build step) |
| `configs/package-assets/io-config.package-manifest.json` | proxy mapping, sidebar menu, app data folder, configuration load/save hooks |
| `snap/snapcraft.yaml` | snap definition; `deps` part = third-party libraries only, so code-only rebuilds are fast |
| `deb/` | the ctrlx-datalayer .deb whose `libcomm_datalayer.so` is bundled |

## Develop

    IO_CONFIG_FAKE=1 python3 -m app.main          # http://127.0.0.1:12617/io-config/?token=dev
    python3 -m unittest -q                       # unit tests against the fake
    python3 tests/ui/ui_check.py <folder>        # headless Firefox check (see the file for set-up)
    tests/integration/dl_check.py                # real Data Layer library, Ubuntu 24.04 only:
    DL_CONTAINER=smtest ../ctrlx-baseline/tools/dl_container.sh check .

## Check, build and install

    ../ctrlx-baseline/tools/check.py .                  # against ctrlx-baseline (build.sh runs it too, report only)
    ../ctrlx-baseline/tools/build.sh arm64              # ctrlX CORE X3/X5/X7 (amd64: COREvirtual), foreground

Bump `version` in `snap/snapcraft.yaml`, `pyproject.toml` and the package manifest for every build you install.
How io-config relates to the shared ctrlx-baseline framework: `docs/how-it-works.md` §2.
Install: ctrlX web UI → Settings → Apps → enable "Allow installation from unknown source" → Install from file.
Logs: `sudo snap logs -f io-config` over SSH (app output does not always reach the web Logbook).
