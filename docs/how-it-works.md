# io-config: how devices are identified, cross-checked and applied

State: version 1.7.1, ctrlx-baseline 1.0.0 (2026-11-02). File paths are relative to the app folder `io-config/`.
The code snippets are io-config's own code, shortened (marked `...`); the line numbers point to the full code. The
shared parts every ctrlX app has (section 2) are described, not shown: their code and rules are in
`ctrlx-baseline/` (`STANDARD.md`, `framework/`).

## 1. The four columns in the device list

The page has three tabs: **Bus** (what the bus and the master show, always live, with these columns and the device
card), **Configuration** (the device list you edit, build and write, section 4.7) and **ESI library**. The columns
below are on the Bus tab. Each row is one bus position. Three columns compare a **source** with what the device itself reports on the
wire (its vendor ID, product code and revision from the master's online info). The fourth sums up all findings.

| Column | Source | What the badge means |
|---|---|---|
| **ESI** | The ESI library (the XML device descriptions you uploaded, *EtherCAT Slave Information*) | `exact` = an ESI with the same vendor, product and revision. `other rev.` = same product, a different revision (the nearest one is used). `missing` = no ESI for this product. |
| **ENI** | The configuration the master runs now (*EtherCAT Network Information*, the `ethercat.xml` written by I/O Engineering or by this app) | `same` = configured with the same identity. `other rev.` = same product, other revision. `different` = another device is configured there. `not in active configuration` (a warn badge) = the device is on the bus but not in the running configuration (e.g. newly plugged in). `not on bus` = configured but missing (it stays in the configuration, see section 4.7). |
| **EEPROM** | The device's own EEPROM (*SII*, slave information interface), read through the master | `read` = read and decoded. `error` = couldn't be read. |
| **Findings** | All findings for this position (hover to read them) | `OK`, `Warning (n)` = n warnings, `Error (n)`. The Info tab of the device card lists them. |

Typical findings:
- the device isn't in Op while the master is;
- a revision differs between the device and the configuration or the ESI;
- the PDOs differ between the sources;
- a sync manager's start address differs;
- the EEPROM's identity doesn't match what the device reports.

## 2. How io-config is built: shared parts and its own parts

io-config is a ctrlX app like the others in `~/ctrlx-apps`. Everything that isn't about EtherCAT, such as reaching
the Data Layer, serving the web page, storing settings and starting and stopping, is the same in every app. That
shared part is the **framework** of `ctrlx-baseline`, and its rules are in `ctrlx-baseline/STANDARD.md`.
io-config's own code is the EtherCAT part.

### 2.1 Today: the old layout

io-config was made from the app template before the baseline existed (2026-10-28), so it still has its **own copies**
of the shared parts: [app/bus.py](../app/bus.py), the request handling in [app/web.py](../app/web.py), the
start-up in [app/main.py](../app/main.py) and the node base in [app/provider.py](../app/provider.py). The baseline
was partly built from io-config: its additions to these copies (marked ★ below) were taken into the framework.

`ctrlx-baseline/tools/check.py` runs before every build. For io-config it reports the old layout and one warning
[BUS1]: io-config's `bus.py` treats MOTION's `OK_NO_CONTENT` answer as an error. io-config doesn't talk to MOTION,
and no EtherCAT master node has been seen to answer that way, so it isn't known to matter; the framework's client
has the fix. io-config moves to the framework the next time it's
reworked (STANDARD.md §12), as its own commit. Its URL, Data Layer nodes and REST routes stay the same.

### 2.2 Which shared part does what for io-config

| Shared part (framework) | What it does for io-config | io-config today |
|---|---|---|
| Data Layer client (`bus`) | every call to the EtherCAT master: reads, reads with arguments ★, JSON writes ★; errors carry the Data Layer code | `app/bus.py` |
| Simulated ctrlX (`fakebus`) | development and tests without a controller | `app/fake.py`: replays the recorded X3 bus (below) |
| Web server (`web`) | routing below `/io-config/api/v1`, error answers, reading uploads, file downloads ★, the configuration load/save hooks | first half of `app/web.py` |
| Own Data Layer nodes (`provider`) | publishing `io-config/...` nodes, including "write a command, read the result" nodes ★ | `app/provider.py` |
| App data (`appdata`) | `config.json` and `edits.json` in the active configuration: atomic writes, reload when replaced from outside | `app/core.py` (`_save`, `reload_if_changed`) |
| Start-up (`runtime`) | snap or development mode, waiting for the active configuration, a folder outside the configuration ★, a provider that may give up ★, web server, main loop, shutdown | `app/main.py` |
| UI helpers (`ctrlx.js`, `base.css`) | token, API calls, error banner, colours | the top of `www/app.js`, `www/style.css` |

### 2.3 io-config's own code on top of the shared parts

Everything in sections 3 and 4 is io-config's own code: `core.py`, `sii.py`, `eni.py`, `esi.py`, `generate.py`,
`configure.py`, `edits.py`, `compare.py`, `coe.py`, `soe.py`. Where io-config plugs into the shared parts:

**Start-up: the ESI library lives outside the configuration.** ESI files can be large and belong to the controller,
not to a configuration, so the library is in `$SNAP_COMMON/esi` rather than in the app data (with the framework:
the start-up's folder for data outside the configuration).

```python
# app/main.py:52 esi_folder
def esi_folder() -> str:
    if IN_SNAP:
        return os.path.join(os.environ["SNAP_COMMON"], "esi")
    return os.environ.get("IO_CONFIG_ESI", os.path.join(SRC, "data", "esi"))

# app/main.py:114 main (shortened): the core gets the bus, its settings file and the ESI library
core = Core(path, bus, esi_folder(), log=log)
...
log(f"Configuration: {path}, ESI library: {core.esi.folder} ({core.esi.summary()['devices']} devices)")
```

The configurator works without its own Data Layer nodes, so the start-up stops trying to start the provider after a
minute ([app/main.py:73](../app/main.py#L73)) instead of waiting forever.

**Own Data Layer nodes: switching saved configurations from a PLC.** `io-config/activate` is a command node: a PLC or
another app writes a configuration's name, and reading the node gives the result of the last write.

```python
# app/provider.py:106 register (shortened)
def activate(text):
    name = text.strip()
    try:
        core.start_activate(name)
    except Exception as e:
        self.last_activate = f"error: {e}"
        raise ValueError(str(e)) from e                 # the write is refused (DL_INVALID_VALUE)
    self.last_activate = f"started {name}"

self._add(f"{ROOT}/configurations", "Names of the saved configurations (JSON list)",
          lambda: json.dumps([c["name"] for c in core.saved()]))
self._add(f"{ROOT}/activate", "Write a saved configuration's name to activate it",
          lambda: self.last_activate, write=activate)
for field, description in (("state", ...), ("name", ...), ("error", ...)):
    self._add(f"{ROOT}/activation/{field}", description, lambda f=field: str(core.activation[f] or ""))
```

**Web server: io-config's routes.** The REST API has about 30 routes (scan, devices, CoE objects, SoE parameters,
process data, plan, download, backups, saved configurations, ESI library). They're thin: each calls the core. The
ESI upload takes the file itself as the request body, with a larger limit than ordinary JSON requests:

```python
# app/web.py:345 post_esi (shortened)
def post_esi(self):
    """The body is the file itself (XML or zip); its name comes in ?name=."""
    name = self.q("name")
    ...
    if length > MAX_UPLOAD:                             # 64 MB
        raise HttpError(413, f"File too large (max {MAX_UPLOAD // (1024 * 1024)} MB)")
    data = self.rfile.read(length)
    self.send_json(200, core.esi.add(name, data))

# app/web.py:336 post_activate: the same activation as the Data Layer node
def post_activate(self, name):
    self.send_json(202, core.start_activate(name))
```

**Simulated ctrlX: the recorded X3 bus.** The fake doesn't invent devices. It replays what the X3's master answered
(online info, EEPROMs, CoE dictionaries in `tests/data/`), so the scan, the cross-check and the byte-for-byte tests
run on real data without a controller. `IO_CONFIG_FAKE_DRIVE=1` adds the recorded ctrlX DRIVE with its SoE parameters.

```python
# app/fake.py:75 FakeBus (shortened)
class FakeBus:
    def __init__(self, nodes: dict = None, slaves: list = None, drive: bool = False):
        if nodes is None:
            nodes, slaves = recorded_nodes()            # tests/data/x3-bus.json
            if drive:
                add_drive(nodes, slaves)                # tests/data/x3-drive.json, plugged into the coupler's port C
                ...                                     # SoE attributes from the drive's SoE dictionary
```

**App data: two files in the active configuration.** `io-config/config.json` (the master instance) and
`io-config/edits.json` (your edits per device) travel with ctrlX configurations and backups, and so do the saved
configurations and backups (`io-config/saved/<name>/`, `io-config/backups/<time>/`). The ESI library doesn't (see
above).

## 3. Where the data comes from

The app talks **only to the ctrlX EtherCAT master** (the `comm.ethercat.master` app on the controller), through
the **Data Layer**. It doesn't open the Ethernet port itself or send any EtherCAT frames. Everything comes from
nodes the master already offers under `fieldbuses/ethercat/master/instances/ethercatmaster/`:

| Data | Data Layer node (relative to the instance) | Type |
|---|---|---|
| Master state, number of devices, topology | `admin/fieldbus_status/master_state`, `.../num_connected_slaves`, `.../num_configured_slaves`, `.../topology_status` | read |
| The running configuration (ENI + ini bytes) | `admin/fieldbus_config` | read; written for a download |
| Identity, address, AL state, ports per device | `device_access/slave_online_info` | read with an argument (the position) |
| EEPROM contents | `device_access/slave_eeprom` | read with arguments (position, word offset, length) |
| CoE objects, SoE parameters | `device_access/coe/...`, `device_access/soe/parameter` | read/write with arguments |
| Process data | `realtime_data/input|output/data/<variable>` | read/write |

So the **master scans the bus** (it does that on its own at start-up and when the topology changes), and the app
reads the master's results, then each device's EEPROM through the master's EEPROM node. All calls go through the
shared Data Layer client (section 2). The master's device-access nodes need two calls io-config added to it:
`read_args` for nodes that take arguments (position, EEPROM offset), and `write_json` for nodes that can't be read
without arguments (SDO, SoE). Both are now part of the baseline's client.

```python
# app/core.py:212 _scan (shortened)
state = self.bus.read(base + "admin/fieldbus_status/master_state").get("response", {})
connected = self.bus.read(base + "admin/fieldbus_status/num_connected_slaves").get("response", {}).get("numSlaves", 0)
...
cfg = self.bus.read(base + "admin/fieldbus_config").get("response", {})
eni_bytes, ini_bytes = _bytes(cfg.get("eni")), _bytes(cfg.get("ini"))
config = eni.parse_eni(eni_bytes)                     # the running configuration, parsed
...
for pos in range(connected):
    address = {"addressType": "autoincrement", "address": (-pos) & 0xFFFF}
    onlines.append(self.bus.read_args(base + "device_access/slave_online_info", {"request": address}))
```

```python
# app/core.py:285 _read_eeprom: 64 words per call until the EEPROM's end marker
while len(words) < EEPROM_MAX_WORDS:
    answer = self.bus.read_args(base + "device_access/slave_eeprom", {"request": {
        **address, "eepromOffset": len(words), "maxLength": EEPROM_CHUNK_WORDS}})
    chunk = answer.get("response", {}).get("data") or []
    if not chunk:
        break
    words += chunk
    if sii.end_reached(words):
        break
```

The EEPROM words are cached per position + identity + serial number. A rescan doesn't read them again unless
"Reread EEPROMs" is ticked. [app/sii.py](../app/sii.py) decodes them (ETG.1000.6): identity, mailbox protocols,
sync managers, FMMUs, PDOs and DC.

## 4. The process, step by step

```
 Scan                     Cross-check                    Build                       Write
 ────                     ───────────                    ─────                       ─────
 master status            per position:                  per position:               backup of the running config
 running ENI + ini   ──>  ESI   (library lookup)   ──>   device from ESI       ──>   master -> Init
 online info / pos        ENI   (paired by type)         (else ENI, else EEPROM)     write ENI + ini
 EEPROM / pos             EEPROM (decoded)               + current choices           compare checksums
                          -> findings, badges            + your edits                master -> Op
                                                         -> ENI + ini (generated)    files in active configuration
```

### 4.1 Identify: the ESI lookup

```python
# app/esi.py:294 EsiLibrary.find
def find(self, vendor, product, revision):
    """(match, entry): "exact", "nearest" (highest revision below, else the closest one) or "none"."""
    same = [e for e in self.entries if e["vendor"] == vendor and e["product"] == product]
    for e in same:
        if e["revision"] == revision:
            return "exact", e
    if not same:
        return "none", None
    lower = [e for e in same if e["revision"] < revision]
    pick = max(lower, key=lambda e: e["revision"]) if lower else min(same, key=lambda e: e["revision"])
    return "nearest", pick
```

The library index (`$SNAP_COMMON/esi/index.json`) lists every device in every uploaded file, so a lookup doesn't
parse XML. Only the matching device is parsed, by [app/esi.py:95](../app/esi.py#L95) `parse_device`.

### 4.2 Identify: pairing with the running configuration

Each device on the bus is paired with its entry in the running ENI **by device type in bus order** (1.3.4). Before
that the pairing was by position, so a device plugged in before others made those others lose their settings.

```python
# app/configure.py:33 pair_in_order: longest common subsequence of [vendor, product]
def pair_in_order(old: list, new: list) -> dict:
    n, m = len(old), len(new)
    best = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            best[i][j] = best[i + 1][j + 1] + 1 if old[i] == new[j] else max(best[i + 1][j], best[i][j + 1])
    ...   # walk the table: equal entries are paired, the others are inserted / removed devices

# app/core.py, in _scan
bus_ids = [_identity(o) for o in onlines]
paired = configure.pair_in_order([[s["vendor"], s["product"]] for s in configured_slaves], bus_ids)
eni_slaves = {paired[k]: s for k, s in enumerate(configured_slaves) if k in paired}
```

Example from the X3: the running ENI had coupler, 3 I/O modules and the drive. Then the XI112108 was plugged in
before the drive. The pairing gives coupler…XI522001 → same entries, XI112108 → none (new), drive → the old drive
entry. The drive keeps its edited telegram.

Saved edits from the editor carry the bus they were made for and move with their device the same way
(`_follow_edits`, [app/core.py:519](../app/core.py#L519)).

### 4.3 Cross-check

[app/compare.py:43](../app/compare.py#L43) `device_report` puts online info, EEPROM, ENI entry and ESI device side
by side and adds a finding for each disagreement:

```python
# app/compare.py:43 device_report (shortened)
if al is not None and al & 0x10:
    add("error", f"Device reports an error: AL status {al_state(al)}, code ...")
elif al is not None and master_op and al & 0x0F != 8:
    add("warning", f"Device is in {al_state(al)} while the master is in Op")

if eni_slave is None and info:
    add("warning", "Not in the current configuration")
elif (eni_slave["vendor"], eni_slave["product"]) != (vendor, product):
    add("error", f'Configured here: {eni_slave["name"]} (...); found: ...')
elif eni_slave["revision"] != revision:
    add("warning", f'Configured with revision ..., the device has ...')

if esi_match == "none":
    add("warning", "No ESI file for this device in the library")
elif esi_match == "nearest":
    add("warning", f'ESI only for revision ... (device has ...)')

if eeprom and (eeprom["vendor"], eeprom["product"], eeprom["revision"]) != (vendor, product, revision):
    add("error", "EEPROM identity (...) differs from what the device reports")
```

After these checks it puts each PDO next to its counterparts from the ESI, the ENI and the EEPROM. PDOs are
compared by object index, subindex and bit length only: names and type spellings differ between sources without
meaning anything (the EEPROM says BOOL where the ENI says BIT). It does the same for the process-data sync
managers (start address, size) and the mailbox protocols. The worst finding gives the **Findings** badge.

### 4.4 Describe each device (what the new configuration is built from)

[app/configure.py:78](../app/configure.py#L78) `describe` picks the best source per position, then lays the
current configuration's choices and your edits on top:

```python
# app/configure.py:78 describe (shortened)
if esi is not None and src.get("esi_match") in ("exact", "nearest"):
    dev = generate.device_from_esi(esi, "")                   # 1. the ESI (the reference)
elif slave is not None and (slave["vendor"], slave["product"], slave["revision"]) == (vendor, product, revision):
    dev = generate.device_from_eni(slave)                     # 2. no ESI: the running configuration
elif src.get("eeprom"):
    dev = generate.device_from_eeprom(src["eeprom"], "")      # 3. no ESI, not configured: the EEPROM
    warning = "no ESI, described from its EEPROM only (default PDOs, no DC)"
else:
    error = "no ESI and no EEPROM data, the device can't be described"
if slave is not None and (slave["vendor"], slave["product"]) == (vendor, product):
    dev = generate.apply_eni(dev, slave)                      # keep the running configuration's choices
    note.insert(0, "kept from the current configuration")
```

- `device_from_esi` ([app/generate.py:80](../app/generate.py#L80)) takes the ESI's defaults:
  - the PDOs the ESI assigns to a sync manager;
  - the first DC mode;
  - the mailbox (CoE / SoE / EoE) with the ESI's startup commands;
  - sync managers and FMMUs, including OpOnly (devices without a mailbox, e.g. the XI112108).
- `apply_eni` ([app/generate.py:151](../app/generate.py#L151)) keeps what the running ENI chose for the same device:
  - name;
  - PDO assignment;
  - startup parameters;
  - drive telegram content.
- Your edits come last ([app/edits.py:173](../app/edits.py#L173) `apply_edit`): name, PDO selection, startup
  parameters, MDT/AT content. Edited MDT/AT entries keep the ESI's names and types; only added IDNs take theirs
  from the drive's SoE dictionary (as I/O Engineering does).

### 4.5 Build: generate the ENI and ini

[app/configure.py:125](../app/configure.py#L125) `build_plan` does three things:
- runs the steps above for every position;
- takes the real cabling from the online info (which port of which device each device is plugged into,
  `topology`, [app/configure.py:61](../app/configure.py#L61));
- generates the files.

```python
# app/generate.py:618 generate_eni (outline)
lay, out_total, in_total, n_mbx = _layout(devices)    # process-image offsets per device, mailbox-state bits
...                                                   # master section: mailbox states, EoE
for pos, dev in enumerate(devices):
    _slave(config, dev, lay[pos], cycle_ns, previous_ports.get(pos), ...)   # per device: identity, PDOs,
                                                                            # sync managers, FMMUs, mailbox,
                                                                            # state-machine init commands, DC
_cyclic(config, lay, out_total, in_total, n_mbx, ref_clock)                 # the cyclic frame (LRW/LRD/...)
```

The generator follows what ctrlX I/O Engineering 4.6.1 writes. Three acceptance tests check that it produces
I/O Engineering's files **byte for byte**:
- the 4 I/O modules;
- the bus with the ctrlX DRIVE;
- the drive with an edited telegram.

Devices I/O Engineering never configured for us (e.g. the XI112108 without a mailbox) are built from their ESI
by the same rules. Those are checked for structure, not bytes.

The plan shows:
- per device, where its description came from;
- errors and warnings;
- the process-image changes (variables added, removed or moved);
- whether the result is identical to the running configuration.

You can download the ENI and the ini before writing.

### 4.6 Apply: write to the master

```python
# app/configure.py:201 write_config (the download sequence)
bus.write(base + "admin/fieldbus_status/master_state", {"request": {"newState": "init"}})
_wait_state(bus, base, "init", 30, log)
bus.write(base + "admin/fieldbus_config", {"request": {"eni": list(eni), "ini": list(ini)}})
sums = {c["id"]: c["checksum"] for c in
        bus.read(base + "admin/fieldbus_config/checksum").get("response", {}).get("checksums", [])}
if sums.get("eniCrc32") != generate.checksum(eni) or sums.get("iniCrc32") != generate.checksum(ini):
    raise DownloadError("the master reports other checksums than the files written")
bus.write(base + "admin/fieldbus_status/master_state", {"request": {"newState": "op"}})
_wait_state(bus, base, "op", op_timeout, log)
# then: the master stores the files in the active configuration itself (the app checks, else writes them)
```

Before this the app does two things ([app/core.py:370](../app/core.py#L370) `_download_thread`):
- it notes if someone wrote another configuration since the last scan (since 1.6.0 this no longer stops the write);
- it saves the running configuration as a **backup** (`io-config/backups/<time>/`, the last 20), read from the
  master at that moment. **Restore** writes a backup back the same way.

Since 1.6.0 each step of the sequence above is best effort: a step that fails becomes a warning (section 4.7), so the
snippet shows the order of the steps, not the error handling.

Saved configurations (1.4.0) use the same sequence. "Activate", by button, REST or Data Layer, backs up the
running configuration and writes the saved ENI and ini; since 1.7.0 it no longer touches the device list or its
edits (`start_activate` in [app/core.py](../app/core.py)). The Data Layer way is the `io-config/activate` node
(section 2.3).

### 4.7 The Configuration tab: the device list, without the devices or a working master (1.7.0)

The **Bus** tab shows what is there: the master with its state (read every 2 s, also without a scan), the devices
with their statuses, process data, objects and parameters. It is always live and has no editing tools. Online writes
(outputs, CoE, SoE) stay there behind the per-device "Writable" box. The **Configuration** tab is where you change
things; you can prepare a configuration for devices that are not on the bus yet, or while the master is not working.
The app then only warns; it does not stop you.

- **The list.** The first time there is data (the running configuration, the bus, or both), the app creates the
  device list in `io-config/project.json` from the master's running configuration, with the saved settings of earlier
  versions. After that **only you change it**: **Scan bus** (also on this tab) refreshes which device is on the bus, but
  never the list. **Update from the active bus** replaces the list and all unwritten changes with the running
  configuration (it asks first).
- **Configured and Present.** Every row has both tags. Configured = in this list, so it gets written. Present = found on
  the bus at the last scan. A configured device that is missing shows Present: No. A bus device that isn't in the list
  appears at its wiring place, without a position, as Configured: No; **Add to list** puts it in, and the trash button
  on a device that is still on the bus takes it back to that state. A note above the table counts both cases.
- **The order of the list is the wiring order.** A new device (**Add device from ESI**: type in the box, pick from the
  suggestions with the arrow keys and Enter or a click, give it a name if you like) goes to the bottom. The arrows
  (▲ ▼) and the trash button work on every row, on the bus or not, except while a configuration is being written.
  A device that is not on the bus is assumed to be connected to port B of the device above it until it has been
  scanned; a device that is on the bus keeps the port it really sits on. There is no port choice. The line above the
  Add button says what is wrong with the text or the name (or what the server refused).
- **Rename**: click a name in the list (Enter or leaving the field saves, Esc cancels). Settings, PDOs and startup
  parameters are in the device card of the list (Settings, Startup). The Bus tab shows the device's own name.
- **Settings stay with their device** when you move it: a listed device is linked to the ENI the list started from
  (`io-config/project-base.xml/.ini`) by its station address, and its saved edits move with it. Because the list
  owns that base, a running configuration that someone else changes does not change the list's settings; Build
  warns ("The master's configuration changed since this list was started") and still compares with the running one.
- **Build** builds exactly the list. It warns once per listed device that is not on the bus at the last scan and once
  per bus device that is not in the list ("... is on the bus but not in this configuration"); a moved device
  therefore warns on both sides. No warning if the list equals the bus. An empty list is an error. The cycle time
  field starts from the base's ini.
- **Write** is best effort: Init, write, check the checksums, Op, then the files. A step that fails is a warning, and
  the result says "Written, with warnings". If the master did not take the configuration, it takes effect at the
  next master start. It is an error only if neither the master nor the files took it. **The list stays after a
  write**: what was written becomes the new base, the entries are linked to it by position, the edits stay. The next
  Build is "identical". Restoring a backup or activating a saved configuration does not touch the list.
- **Saved configurations** (Configuration tab, one list, a Kind column). "Save active configuration" keeps the ENI/ini
  the master runs (read from it, else from the active configuration's files). "Save proposed configuration" keeps
  the list with its base and settings, and the ENI/ini built from it with the cycle field's value; if it doesn't
  build it is saved anyway, without ENI/ini and with the reason, and can be opened but not activated. **Activate**
  writes an ENI to the master (backup first). **Open** (asks first) makes a saved configuration the list; the bus is
  not touched.
- **Startup parameters from the Bus tab** ("Startup" on an object or SoE parameter) are saved for the Configuration
  row of the same device; if the device is not in the list the app says so.
- **Master state.** The Bus tab's master card shows the state as a badge (OP green, anything else yellow,
  unavailable red, "(requested X)" when it differs). States are shown as Init, Pre-OP, Bootstrap, Safe-OP and OP;
  the Data Layer values stay `init`, `preop`, `bootstrap`, `safeop`, `op`. A drop-down with those five and a **Set state**
  button request a state on the real bus. Choosing does nothing by itself, and the 2 s poll leaves your choice alone
  until the request is done. Set state asks first, naming the transition ("Requests Safe-OP on the real bus (now OP).
  Outputs stop when leaving OP."; Bootstrap is only meant for firmware updates), and is off while a scan, download or
  request runs, when the master can't be read, or when the choice is the state already. A scan follows the request.
  If the master refuses the request (its answer is shown on the card) or does not reach the state within about 15 s,
  the card keeps a message naming both states.
- **Names are unique** (case ignored) in the list. A rename or an add that would repeat another row's name is
  refused with "the name ... is already used by position N". A device without a name gets its ESI name, with _2, _3
  ... when needed, and the list shows exactly the names Build will use. Duplicates that already exist (older saved
  edits, an opened old save) are not renamed; Build refuses and names them and their positions.
- If the master cannot be read, the scan shows state "unavailable" and the Bus tab's list comes from the files of the
  active configuration.

## 5. Where to look for more

- [docs/design.md](design.md): the design history, every rule learned from the controller and what's open.
- `ctrlx-baseline/STANDARD.md`: the rules all apps follow and the shared framework (§2), how an app moves to it
  (§12). `ctrlx-baseline/tools/check.py ~/ctrlx-apps/io-config` shows where io-config stands.
- Tests: `tests/test_generate.py` and `tests/test_drive.py` hold the byte-for-byte acceptance tests.
  `tests/test_nombx.py` covers devices without a mailbox and the real X3 bus with the inserted XI112108.
  `tests/test_saved.py` covers saved configurations.
