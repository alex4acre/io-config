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
6. **SoE drives, DRAFT (1.3.0):** for the ctrlX DRIVE XCS2-F/H (SoE) found on the X3 (position 4, at port C of the
   coupler, rev 0x3 vs ESI 0x06000003). Real topology from the master (portSlaveHandles/previousPort) for
   PreviousPort. ESI: MDT/AT PDOs, 1 KB mailbox, DC Sync0 0x0300 with ShiftTime 60000, SoE startup commands.
   Generated (guessed, no reference yet): S-0-0015 = 7, S-0-0016 / S-0-0024 from the AT/MDT entries without
   the status/control word, S-0-0001/0002 = bus cycle in µs, then the ESI's and the user's SoE commands, as ENI
   Mailbox/SoE/InitCmds (OpCode 3, DriveNo, IDN, Elements 64, Attribute 0, Data). Plans with a drive are marked
   draft and need allow_draft to be written. SoE dictionaries (EtherCATDict) are kept in the ESI library; units
   repaired (UTF-8 shown as code page 850). Editor: MDT/AT content from mappable IDNs; SoE startup
   parameters. SoE parameter read/write via device_access/soe/parameter (needs the drive in PreOp+, i.e.
   configured). The FSoE slot (MinInstances 0) is not configured.
   **Needed:** an ENI from ctrlX I/O Engineering with this drive to replace the guesses with facts.
   **Tried on the X3 2026-10-28 (user approved):** the draft (tests/data/x3-drive-draft-1.3.0.xml) was accepted,
   checksums matched, the I/O stayed in Op; the drive reached PreOp (AL 0x02, code 0) and the master then set
   its requested state to PreOp (gave up on PS). Every SoE read timed out ("Ecat: Timeout" 0C850010) in PreOp;
   mailbox counters: SoE 22 read requests, 0 bytes, 0 writes. The original configuration was restored.
   Suspects: mailbox-state FMMU / SoE InitCmd format / something I/O Engineering adds for drives.
   **Reference (1.3.1):** after the user fixed the drive profile, I/O Engineering 4.6.1 configured the bus with
   the drive (tests/data/x3-drive-ioe.xml). The generator now produces it byte for byte from the scan + ESI and
   from the running configuration. Rules learned: master EoE section (MaxPorts = EoE devices + 1, MaxFrames 120,
   MaxMACs 0); EoE "set IP parameter" InitCmd per EoE device (MAC 02:60:34:FE:<address>, IP = master EoE network
   + n, mask, gateway = master, name); device name keeps a trailing "_"; Info RevisionNo = the ESI's revision;
   no revision check without CheckRevisionNo; protocols ordered AoE, CoE, SoE, FoE, EoE; SoE order 15, 24 (MDT),
   16 (AT) with comments Telegram type / MDT list / AT list; "BI check INIT" timeout 10000; DC start time = the
   Sync0 shift; ESI ESC registers -> "set PDI divider" (0x400), "set SM watchdog" (0x420), "set DC latch cfg";
   FMMUs cleared in index order. My 1.3.0 draft already had the right S-0-0016/S-0-0024 lists.
   On the X3 the drive then reached SafeOp (S-0-0095 "A0006 Safe-Operational"); its SoE parameters read
   through the app (tests/data/x3-drive-soe.json). Only changed MDT/AT content is still unverified (draft).
   1.3.1 installed on the X3 2026-10-29: plan identical to the running I/O Engineering file, identical write
   through the app OK, SoE read/write through the app on the real drive OK.
7. **Edited telegrams (1.3.2, 2026-10-29):** reference from I/O Engineering 4.6.1 read from the X3 (MDT +S-0-0041 before
   P-0-2250, AT -S-0-0084 +S-0-0011; tests/data/x3-drive-ioe-telegram.xml). The generator matched the layout already
   (S-0-0016/0024, SM2 26 bytes, frame 62 bytes, offsets); the only rule missing: entries already in the telegram keep
   the ESI's name and type (e.g. "Signal control word", WORD), only added IDNs take the SoE dictionary's. Now byte for
   byte; the draft gate is gone. The drive stays in SafeOp with any config until a MOTION axis uses it (the master's
   requestedState for the drive is safeop; no AL error). Installed on the X3; device write with the drive not yet tested.
8. **No mailbox + pairing (1.3.3/1.3.4, 2026-10-29):** policy from the user: ESI first, devices never seen before must
   work; references are checks, not preconditions. XI112108 (8DI/8DO, no mailbox, SM0 outputs at #x0F01 ctrl #x44
   and FMMUs OpOnly) is built from its ESI: no mailbox section/SMs/state FMMU/bit; OpOnly SMs and FMMUs written
   inactive in PS, activated in SO, deactivated in OS (ETG.2000). No I/O Engineering reference for it. Found with
   the real bus: the running ENI was matched by POSITION, so a device inserted before the drive made the drive
   lose its edited telegram (rebuilt from ESI). Now pair_in_order (LCS of vendor/product) pairs the running ENI
   and saved edits (which keep the bus they were made for) with their devices. 1.3.4 installed; plan on the X3
   correct, not written yet.
9. **Next:** offline add from the ESI library, DC mode choice, devices without a mailbox, more than one frame, slots/modules.

## Open questions

- Which ESI files to start with (ctrlX I/O family; ctrlX DRIVE; third-party).
- May the Rexroth ctrlX I/O ESI files be shipped in the snap?
- Is ctrlX COREvirtual (.27) able to run an EtherCAT master for testing, or is the X3 the only bus?
- Does a PLC project with I/O mapping from I/O Engineering keep working after the app writes a new
  ENI?
