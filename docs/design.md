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
9. **Device card + saved configurations (1.3.5 / 1.4.0, 2026-10-29):** clicking a device opens its card (Process
   data first, 200 ms, per device). Saved configurations: running ENI+ini + edits under a name; activate by button,
   REST or Data Layer (io-config/activate, io-config/activation/*), the app's first own nodes; activation backs up,
   writes without a scan check and restores the edits. 1.4.0 installed on the X3, save/activate not yet tried there.
10. **Modular devices + DC mode (1.5.0, 2026-10-30):** an Elmo Titanium (CoE, 2 axes) behind a second XB-EC-12 got
   no process data from 1.4.3: its ESI is a Modular Device Profile (ETG.5001) device, the process data is in
   modules put into slots, the device-level PDOs (#x1701.., #x1B01..) are empty free-mapping containers without a
   default sync manager, and the app had picked two of them (0 bytes). Read on the device: #xF030 = #xF050 =
   [#x288110 safety, #x100 position, #x100 position], #xF000:01 = #x800, #x1600/#x1A00 empty (the master must
   write the PDO contents, ESI PdoConfig="true"), #x1700/#x1B00 hold the safety module's 36 entries.
   `app/modules.py`: slots + modules from the ESI (only modules defined in the ESI file itself; the ctrlX DRIVE's
   FSoE option is in a separate module file, so it stays built as before), PDO/object indexes moved per slot
   (DependOnSlot x SlotPdoIncrement) and slot group (DependOnSlotGroup x SlotGroupPdoIncrement /
   SlotGroupIndexIncrement), PDO names prefixed with the slot name (two axes, same module, distinct variables).
   ENI startup order (PS): #xF030 module list (complete access; empty slots at the end left out, like the
   device's own list), clear #x1C12/#x1C13, each module PDO's entries (complete access when the ESI allows it,
   else per subindex; placeholder-only PDOs such as the recorder skipped), the assignment, the modules' startup
   commands (e.g. #x6060/#x6860 = 8). A running ENI's modules are read back from its #xF030; without one (the
   1.4.3 file) the ESI defaults are used. Settings tab: module per slot, DC mode (ESI OpModes). No I/O
   Engineering reference for a modular device yet: commands follow ETG.5001/ETG.2000; ask for one to compare.
   1.5.1: a PDO choice saved before 1.5.0 for a modular device (the X3's Elmo had #x1702/#x1B01) is ignored and
   dropped on the next save (1.5.0 applied it and the plan gave the Elmo 0 bytes); no "in the ESI but not in the
   EEPROM" notes for module PDOs.
   1.5.2: the Settings tab shows the PDOs of the chosen modules as soon as a slot changes (each module choice carries
   its PDOs, numbered for the slot), with a "Not saved yet" note; the Startup tab follows after Save.
11. **Project configuration (1.6.0, 2026-11-02):** a configuration can be built and written with devices absent or the
   master not working. No checks, only a build warning per absent device.
   - Device list = project merged with bus (`configure.merge`, `Core._assemble`, no bus access). The project is the
     running ENI's devices, or `io-config/project.json` ({eni_checksum, devices[uid, vendor, product, revision, port,
     upstream]}) once the user adds, removes or moves a device. An absent device is described from its ESI or the
     running ENI; `src["bus_pos"]` is None; online access (objects, SoE, process data writes) is refused with "not
     present on the bus". Behaviour change: a device that was dropped from the build when unplugged now STAYS in the
     configuration until the user removes it.
   - Editing: add from the ESI library (Insert after / Connected to / Port B, C or D); Up, Down and Remove only for
     absent devices; "Back to the running configuration" deletes the project. REST: `POST /project/devices`,
     `DELETE /project/devices/<pos>`, `POST /project/devices/<pos>/move {by}`, `DELETE /project`.
   - Scan is tolerant: master unavailable -> state "unavailable", connected 0, message shown; the ENI falls back to the
     active configuration's files. Before the first scan the list also comes from those files.
   - Write is best effort: Init -> fieldbus_config + checksums -> Op -> files. Each failed step is a warning; the UI shows
     "Written, with warnings"; if the master didn't take it: "takes effect at the next master start". Error only if
     neither master nor files took it. Activating a saved configuration still reports state error on warnings.
   - Guard removed: a master configuration changed since the last scan no longer refuses the write; it is a step note,
     and the backup taken first holds what the master really ran (read fresh, else last scan, else files).
   - project.json is deleted once the master holds a written configuration (checksums match), kept if only the files took
     it. Build warning if the master's ENI changed since the project started.
   - Fixed: a new device inserted ahead of a running device of the same type gets a suffix (_2) instead of a duplicate name.
   - Known limits: Startup-parameter "add from object" for an absent CoE device needs the device online (the data type is
     read from it); SoE works from the dictionary. The Settings tab may show a new device's unsuffixed name while the plan
     gives it the _2 suffix.
   - **Configuration state (1.6.0, rework 2026-11-02):** the list is the project's entries only, in wiring order.
     No port choices: POST /project/devices takes {vendor, product, revision, name?}, a new device goes to the
     bottom (upstream = the entry above, port B). Arrows and Remove on every row (no absent-only check),
     `configure.move_entry/remove_entry/normalise` keep (upstream, port) a sensible chain (first child takes the
     moved/removed device's place, normalise pass). Once project.json exists the Devices card shows the flag
     "Configuration" and live data stops: state "-", no process data, no CoE/SoE online access (`_autoinc`,
     `process_data`, `write_output` refuse), device card opens on Settings. `_assemble` pairs entries with the bus in
     order (`pair_in_order`), no `merge`: bus devices not in the list are not inserted (a build warning each);
     unpaired entries use their stored port ("not on the bus at the last scan" warning), paired ones their real port
     (a drive on port C moved away and back is on C again). The running-configuration link of an entry is its station
     address (entries from before have none and use the pairing). Edits follow the device explicitly
     (`_remap_edits`), not by LCS. A rename alone (PUT edit) does not start the state; the table shows the saved
     edit's name. UI: ▲ ▼ buttons, one input with a suggestion list for "Add device from ESI" (keys as in
     plc-data-explorer), optional name, click-to-edit names.
12. **Bus and Configuration tabs (1.7.0, 2026-11-02):** the 1.6.1 "Configuration state" (list changes switch live data
   off) is replaced by two tabs.
   - **Two views in the core.** `devices/sources` = the Bus view, always `merge` of the running configuration and the
     bus, no edit names, online access through it (the 1.6.1 refusals and `configuring` are gone). `cfg_devices/
     cfg_sources` = the Configuration view from project.json (entries only, in order, LCS-paired with the bus devices
     for real online info/EEPROM/`bus_pos`, `configuration=True` reports, edit names). `_source` = bus,
     `_cfg_source` = configuration; `/devices/<pos>/edit` (GET/PUT/DELETE) and the edit options mean a Configuration
     row; `POST /devices/<pos>/edit/startup` takes a Bus position (it reads the device) and saves for the row found
     by `cfg_position` (same `bus_pos`), else ConfigError. A scan refreshes the pairing only.
   - **The list always exists once there is data** (`_ensure_project` from the Bus view, `_follow_edits` one last time
     so 1.5.x edits.json stays valid); after that edits follow only through `_remap_edits`. Add/move/remove are refused
     only during a download.
   - **Base ENI** `project-base.xml/.ini` (atomic writes; the whole `io-config/` folder is in the active configuration,
     `*.tmp` private): the list's settings link (station address) to this, not to the running ENI. Build compares with
     the running one (identical, diff, "changed since" checksum) but starts from the base's ini/cycle. A project
     without a base uses the running configuration.
   - **Update from the active bus** (`POST /project/update`, confirm; replaces `DELETE /project`): entries from the Bus
     view with fresh uids and topology, base = running ENI, edits cleared. On a matching bus the next plan is
     byte-identical. **After a successful plan write** (`_after_write`): list kept, base = written ENI/ini, entries
     relinked by index, checksum updated, edits stay. Restore and activation do not touch list, edits or base (edits
     are no longer restored by activation; the "changed since" warning covers it).
   - **Saved configurations**: kind `active` (master ENI/ini, fallback active files, no edits; old saves without a kind
     are active) or `proposed` (project.json + base + edits + ENI/ini built with the cycle field; unbuildable ones saved
     with `error`, openable, not activatable). `POST /saved {name, kind, overwrite, cycle_us}`, `POST /saved/<n>/open
     {confirm}` (refused during a download; active saves become entries from their ENI incl. PreviousPort topology).
   - **Live master state** (`GET /master/state`, light read of master_state only, cached 1.5 s, errors logged on change)
     and **phasing** (`POST /master/state {state}`, 1.7.1: all five EthercatState values; refused when busy/unreadable; a thread writes the request,
     waits up to 15 s, leaves a message if not reached, then scans). The list, edits and base are untouched.
     Fake: `IO_CONFIG_FAKE_FAIL_OP=1` keeps the master in PreOp.
   - UI: Bus tab (master card with state badge and state select, device table without tools, device card with Process
     data / Objects / Parameters / Info); Configuration tab (list with ▲ ▼ and a trash icon button, name click-to-edit,
     note + "Update from the active bus", add form with a message line above the button, device card with Settings /
     Startup, build/plan/write, saved configurations with Kind, backups).
   - **1.7.1:** (a) unique names: `configure.device_names` is the one rule for the plan and the Configuration table (explicit
     edit name first, else the base ENI's name, else the ESI name with _2, _3 against all names, case ignored; explicit
     names are seeded first, so a default never takes a name another row asks for). `save_edit` and `project_add` refuse a
     name another displayed row has; Build lists old duplicates by name and positions as a plan error (the generate.py
     check stays the last guard). (b) State text: `compare.STATE_NAMES` / `AL_CODES` (Init, Pre-OP, Bootstrap, Safe-OP, OP)
     and `stateName` in app.js are the only mappings; Data Layer values unchanged; device AL status text and messages
     use them (`write_config` step texts are unchanged). (c) A drop-down + "Set state" for all five requestable states
     (`REQUESTABLE`, from `ethercat_state.fbs`; `unknown` is not requestable); a refused write shows the master's answer
     and the scan still runs.
13. **Configured and Present tags (1.8.0, 2026-11-03, user's decision):** Scan bus also on the Configuration tab (same
   `POST /scan`; never changes the list). Each row has Configured (in the list) and Present (on the bus at the last
   scan, `-` until a scan says). Bus devices outside the list are rows without a position at their wiring place
   (`configuration.unconfigured`: `bus_pos`, `before`), with Add to list (`POST /project/devices/add-from-bus`, no ESI
   needed). **Changes 1.7:** the first list and Update from the active bus take the running configuration (missing
   devices included), no longer new bus devices; a master without a configuration therefore starts with an empty list.
14. **Modules with less than a whole byte (1.8.1, 2026-11-03):** e.g. XI213204 (4 BOOL outputs, ESI `DefaultSize="1"`), also
   XI110204, XI150204, XI211204, XI231302, XI232302, XI233204, XI234204, XI250204. Each direction's size rounds up to
   bytes; the variables keep their bit offsets, the next device starts on a byte. No I/O Engineering reference for such a
   module yet (none in the running ENIs seen): compare when one is available.
15. **Cycle time from the active configuration (1.8.2, 2026-11-03, user's decision):** every scan and Update from the
   active bus set the Cycle time field to the active value, also over a typed one. Build warns (never refuses) when the
   cycle differs from scheduler task `ctrlXAutomation`, or when the task can't be read.
16. **Tooltips, Findings and Request OP (1.8.3, 2026-11-03, user's decisions):** `www/tips.js` holds a tooltip per column and
   setting; badges say what they mean; the Findings badge (was "Check") lists its findings. A device with no process data and
   no ESI FMMUs gets no FMMU commands and no ProcessData element, byte for byte as I/O Engineering (XC-EC-02 stayed in Pre-OP
   with them). A device below the master's state: finding with the AL code in words or "no fault". **Request OP**
   (`POST /devices/<pos>/request-op`, confirm, behind Writable): writes `device_access/slave_state` (node: "Get and set slave
   state"), waits up to `master_wait`, reports the state and code, then scans. The write hasn't run on a real device yet.
17. **EEPROMs only when asked for (1.8.4, 2026-11-03, user's decision):** measured on the X7, 12 devices: 71.7 s with EEPROMs, 0.3 s
   without. A scan reads none, except with Read all EEPROMs (Bus tab, unticks itself). Read EEPROM on both device cards
   (`POST /devices/<pos>/read-eeprom`, `/project/devices/<pos>/read-eeprom`). Reads saved in `$SNAP_COMMON/esi-eeprom.json`,
   keyed by identity + serial, or + place when the device reports serial 0; they are shown and compared in later scans without a read.
   A changed EEPROM on the same serial isn't noticed until read again. Device card tabs follow the ESI's protocols, else the EEPROM's.
18. **Next:** more than one frame, free PDO mapping (editing PDO contents),
   modules from separate module files (EtherCATModule, e.g. ctrlX DRIVE FSoE).

## Open questions

- Which ESI files to start with (ctrlX I/O family; ctrlX DRIVE; third-party).
- May the Rexroth ctrlX I/O ESI files be shipped in the snap?
- Is ctrlX COREvirtual (.27) able to run an EtherCAT master for testing, or is the X3 the only bus?
- Does a PLC project with I/O mapping from I/O Engineering keep working after the app writes a new
  ENI?
