# io-config: how devices are identified, cross-checked and applied

State: version 1.4.0 (2026-10-30). File paths are relative to the app folder `io-config/`. The code snippets are
shortened (marked `...`); the line numbers point to the full code.

## 1. The four columns in the device list

Each row is one bus position. Three columns compare a **source** with what the device itself reports on the
wire (its vendor ID, product code and revision from the master's online info). The fourth sums up all findings.

| Column | Source | What the badge means |
|---|---|---|
| **ESI** | The ESI library (the XML device descriptions you uploaded, *EtherCAT Slave Information*) | `exact` = an ESI with the same vendor, product and revision. `other rev.` = same product, a different revision (the nearest one is used). `missing` = no ESI for this product. |
| **ENI** | The configuration the master runs now (*EtherCAT Network Information*, the `ethercat.xml` written by I/O Engineering or by this app) | `same` = configured with the same identity. `other rev.` = same product, other revision. `different` = another device is configured there. `not configured` = the device is not in the running configuration (e.g. newly plugged in). `not on bus` = configured but missing. |
| **EEPROM** | The device's own EEPROM (*SII*, slave information interface), read through the master | `read` = read and decoded. `error` = couldn't be read. |
| **Check** | All findings for this position | `OK`, `Check (n)` = n warnings or notes, `Error (n)`. The Info tab of the device card lists them. |

Typical findings:
- the device isn't in Op while the master is;
- a revision differs between the device and the configuration or the ESI;
- the PDOs differ between the sources;
- a sync manager's start address differs;
- the EEPROM's identity doesn't match what the device reports.

## 2. Where the data comes from

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
reads the master's results, then each device's EEPROM through the master's EEPROM node. Two Data Layer calls
are in [app/bus.py](../app/bus.py): `read_args` for nodes that need arguments, and `write` / `write_json`.

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

## 3. The process, step by step

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

### 3.1 Identify: the ESI lookup

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

### 3.2 Identify: pairing with the running configuration

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

### 3.3 Cross-check

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
managers (start address, size) and the mailbox protocols. The worst finding gives the **Check** badge.

### 3.4 Describe each device (what the new configuration is built from)

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

### 3.5 Build: generate the ENI and ini

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

### 3.6 Apply: write to the master

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
- it checks that nobody wrote another configuration since the last scan (the master's ENI checksum);
- it saves the running configuration as a **backup** (`io-config/backups/<time>/`, the last 20). **Restore**
  writes a backup back the same way.

Saved configurations (1.4.0) use the same sequence. "Activate", by button, REST or Data Layer, backs up the
running configuration, writes the saved ENI and ini, then restores the edits saved with them
([app/core.py:468](../app/core.py#L468) `start_activate`).

## 4. Where to look for more

- [docs/design.md](design.md): the design history, every rule learned from the controller and what's open.
- Tests: `tests/test_generate.py` and `tests/test_drive.py` hold the byte-for-byte acceptance tests.
  `tests/test_nombx.py` covers devices without a mailbox and the real X3 bus with the inserted XI112108.
  `tests/test_saved.py` covers saved configurations.
