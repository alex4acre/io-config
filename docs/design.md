# io-config: design (2026-10-28)

A ctrlX CORE app that configures the EtherCAT bus from the controller's web UI, without ctrlX
I/O Engineering: scan the bus, identify each device, build the bus configuration (ENI + ini) and
download it to the EtherCAT Master app. Sidebar entry "I/O Config".

## What the controller offers (read on the X3 at 192.168.7.22)

Base path `fieldbuses/ethercat/master/instances/ethercatmaster/` (port XF50).

| Need | Node | Notes |
|---|---|---|
| Current config | `admin/fieldbus_config` | read/write `{request/response: {eni: bytes, ini: bytes}}` |
| Config checksums | `admin/fieldbus_config/checksum` | eniCrc32, iniCrc32 |
| Bus state | `admin/fieldbus_status/*` | master state, configured/connected slave count, topology |
| Scan | `device_access/slave_online_info` | identity, address, DC, mailbox protocols per position |
| Device self-description | `device_access/slave_eeprom` | SII EEPROM; read in 64-word chunks (one 2048-word read timed out and logged 0C850010) |
| Object dictionary | `device_access/coe/*` | CoE devices only |
| Live I/O | `realtime_data/input|output/data/<Slave>/<Pdo>.<Entry>` | names from the ENI, spaces become `_` |

Per-slave nodes are "Method" reads with input:
`GET <node>?data=<url-encoded {"type":"object","value":{"request":{"addressType":"autoincrement","address":<0,65535,...>}}}>`.

Reference files (shared folder `IO/`): `controller-eni.xml` and `controller-ini.txt` from I/O
Engineering 4.6.1 for XB_EC_12 + XI190202 + XI422202 + XI522001, plus the tree dump and online info.

## Device library

A library entry is only what is specific to one device type (vendor, product code, revision):
sync managers, mailbox sizes and protocols, PDOs (with fixed/optional/exclusion rules), startup
parameters (CoE init commands), DC modes, and display names. Everything else in the ENI is computed
per bus by the generator.

Entries come from three sources, combined per device found on the bus:

1. **ESI files (reference).** Uploaded by the user in the app (single XML or zip). The app indexes
   them (vendor/product/revision, name, group) and extracts entries on demand. They define what a
   device *can* do: all PDO options, the rules between them, DC modes, startup parameters.
2. **Existing ENI (current choices).** If the controller's current configuration already contains
   the device at that position with the same identity, its selections are taken over: assigned PDOs,
   startup parameters, DC mode, slave name. A bus configured by I/O Engineering is reproduced
   unchanged. An uploaded ENI file can be harvested the same way.
3. **Device EEPROM (truth on the wire).** Always read during a scan. It is used to cross-check
   the other two (identity, revision, sync manager sizes, PDO list) and as the fallback "learn"
   source when no ESI is available. Verified on XI422202: name, 4 sync managers, all PDOs with entry
   names and sizes, DC category. Not in the EEPROM: startup parameters, exact DC settings, PDO rules.

The UI shows, for each device, which sources were found and any mismatch, e.g. "ESI rev 0x100,
device rev 0x101", "ENI assigns PDO 0x1A03, ESI says it excludes 0x1A00", "no ESI: learned from
EEPROM, check PDOs".

Revision matching: exact revision first; otherwise the nearest lower revision of the same product,
flagged as a warning.

## Generator

Builds the ENI from the ordered device list and library entries: station addresses (1001...),
mailbox and process-data sync managers, FMMUs, logical process image (input/output offsets), cyclic
LRW/LRD/LWR frames with expected working counters, state-transition init commands, CoE startup
commands, DC configuration. The ini is taken over from the current one, with cycle time and DC
shifts editable.

**Acceptance test:** generating from the entries harvested from `controller-eni.xml` must give an
equivalent ENI (same slaves, PDOs, offsets, init commands, cyclic commands; formatting may differ).
Checked offline, before anything is downloaded.

## Download

1. Show a diff against the current configuration (devices, PDOs, process-image variables that
   disappear or move).
2. Save the current ENI + ini as a backup in the app's data.
3. Write `admin/fieldbus_config`, verify the checksums, request Op, watch the state and topology.
4. On failure, offer to restore the backup.

Changing the configuration renames or moves `realtime_data` variables, which can break PLC
projects, real-time copy lists and the signal-map app that use them; the diff must make this
visible.

## Storage

- ESI library: the app's own data folder (it can be large; not part of the active configuration).
- Bus configurations, the learned entries the user saved, and backups: in the active configuration,
  so they go with backup/restore.
- Third-party ESI files are not shipped inside the snap (vendor licenses); the user uploads them.

## Phases

1. **Read-only (1.0.0, built 2026-10-28):** scan, identity, EEPROM decode, parse current ENI, ESI
   upload and index, show the bus with the three sources and findings. Testable on .22 with no risk.
2. **Generator (1.1.0):** reproduces `controller-eni.xml` byte for byte from the current configuration's
   choices, from the ESI files alone, and from the ENI alone. Devices are described from the ESI, else the
   current configuration, else the EEPROM.
3. **Learn from the EEPROM** into the library (ESI upload is already in phase 1).
4. **Download (1.1.0):** checksum guard (no other download since the scan), backup in the active
   configuration (io-config/backups), master to Init, write `admin/fieldbus_config`, compare the master's
   checksums (CRC-32 without the final inversion, little-endian), master to Op, and the master's files
   `comm.ethercat.master/config/<instance>/ethercat.xml|.ini` in the active configuration (loaded at boot).
   Restore from a backup the same way. Tried on the X3 2026-10-28 with the identical configuration: ~10 s,
   checksums match, back in Op, and the master rewrote its active-configuration files itself.
5. **Editing and online access (1.2.0):** per position (io-config/edits.json, kept with the device identity):
   name, PDO selection (ESI rules: mandatory, exclusions; locked when the device has no CoE PDO
   assignment), startup parameters (CoE SDO writes at PS, added from the object view with the value encoded
   for the entry's data type). CoE object dictionary online (list, descriptions, values), SDO write now
   (refused when the entry isn't writable in the device's current state, e.g. 0x8000 settings are PreOp-only),
   process data read/write through realtime_data (outputs only, range-checked by ENI type).
   Device-tested on the X3 2026-10-28 (1.2.0): object lists ~0.5 s per device, process data 44 variables in
   0.12 s, analog output and 0x10F1:2 written and restored, startup parameter 0x8000:1 downloaded and read
   back from the device. 1.2.1: an edited startup list replaces the commands kept from the running
   configuration (1.2.0 added them, so a changed value appeared twice and a removed one stayed). Verified on
   the X3: 1000 -> 0 -> removed, the last ENI byte-identical to I/O Engineering's original.
6. **Next:** drives (modules/slots, PDO mapping content, SoE startup), DC mode choice, devices without a
   mailbox, more than one frame.

## Open questions

- Which ESI files to start with (ctrlX I/O family; ctrlX DRIVE; third-party).
- May the Rexroth ctrlX I/O ESI files be shipped in the snap?
- Is ctrlX COREvirtual (.27) able to run an EtherCAT master for testing, or is the X3 the only bus?
- Does a PLC project with I/O mapping from I/O Engineering keep working after the app writes a new
  ENI?
