# io-config: configuring the bus from a program

A complete EtherCAT configuration is one JSON document. A program sends it, the app checks it, builds the ENI and ini, and
saves them under the document's name. Pushing it to the master is a separate step, because that stops the bus.

    1. create    send the document            the master and the Configuration list are not touched
    2. check     read the result              errors (nothing saved) or warnings
    3. activate  write the saved name         backup, master Init, write, master OP: the bus stops briefly
    4. check     read activation/state        running, done or error

Everything here needs the ESI files on the controller already: upload them on the ESI library tab or with `POST /esi`.

## Data Layer nodes

Through ctrlX REST these are `https://<core>/automation/api/v2/nodes/<node>`, with a JSON text as a string value.

| Node | Access | Value |
|---|---|---|
| `io-config/create` | write or create, read | Write the document as JSON text. Reading gives `started <name>` or `error: <text>`. Text that isn't a valid document fails the call. |
| `io-config/creation/state` | read | `idle`, `running`, `done`, `error` |
| `io-config/creation/name` | read | The document's name |
| `io-config/creation/errors` | read | JSON list. Not empty: nothing was saved. |
| `io-config/creation/warnings` | read | JSON list |
| `io-config/configurations` | read | JSON list of the saved names |
| `io-config/activate` | write, read | Write a saved name to push it to the master |
| `io-config/activation/state`, `name`, `error` | read | The last activation: `idle`, `running`, `done`, `error` |
| `io-config/scan` | write, read | Write any text to start a scan. Reading gives `started` or `error: <text>`. |
| `io-config/scan/state` | read | `idle`, `running`, `done`, `error` |
| `io-config/bus` | read | The last scan as JSON: `scan` {state, error}, `master`, `devices` |
| `io-config/export` | write, read | Write a saved name, or an empty text for the Configuration list. Reading gives `started <name>`, `started list` or `error: <text>`. A name that can't be exported fails the call. |
| `io-config/export/state` | read | `idle`, `running`, `done`, `error` |
| `io-config/export/document` | read | The document from the last export, as JSON text |
| `io-config/export/warnings` | read | JSON list, e.g. creating from the document would give a different ENI |

A build runs after the write has returned, so wait for `creation/state` to leave `running`, and likewise `export/state`. One export
runs at a time; reading doesn't build again, it gives the last result. A PLC string may be too short for a large document.

## REST

Under `https://<core>/io-config/api/v1`, with the same bearer token as the rest of ctrlX.

| Call | Answer |
|---|---|
| `POST /configurations` with the document | 201 `{name, devices, cycle_us, warnings, variables, identical}`. 400 `{error, errors, warnings}`, nothing saved. Answers when the build is done. |
| `GET /project/document?name=<name>` | 200 `{document, warnings}`: the Configuration list as a document |
| `GET /saved/<name>/document` | 200 `{document, warnings}`: a saved configuration as a document |
| `PUT /project/eoe` `{enabled, ip, mask}`, `DELETE /project/eoe` | The Configuration list's EoE setting; DELETE goes back to the running ini's |
| `POST /saved/<name>/activate` | 202: the same as writing to `io-config/activate` |
| `POST /scan`, `GET /state` | Start a scan; state of the app, `creation` and `activation` included |

`variables` says what changes against the running configuration for users of the process image: `added`, `removed`, `changed`,
`renamed`. `identical` is true when the master runs exactly this ENI and ini already.

The easiest way to a first document is to set a machine up once in the UI and export it. An export warns if creating from it
would not give the same ENI (the ENI can hold details a document can't carry, such as a kept EoE MAC address).

## The document

```json
{
  "schema": 1,
  "name": "Line1",
  "overwrite": false,
  "cycle_us": 4000,
  "devices": [
    { "vendor": "0x24", "product": "0x242A0F", "revision": "0x10100", "name": "Coupler" },
    { "vendor": "0x24", "product": "0x242A1F", "revision": "0x100", "name": "Analog",
      "connected_to": { "device": "Coupler", "port": "C" },
      "variable_names": { "in:Channel_1.Value": "Pressure" } }
  ]
}
```

Unknown keys are refused, so a mistyped option doesn't disappear. All problems are reported at once. Numbers can be plain, or
text in hex (`"0x1A00"`, `"#x1A00"`).

| Key | Needed | Meaning |
|---|---|---|
| `schema` | yes | 1 |
| `name` | yes | Name to save under: 1 to 64 letters, digits, spaces, `-`, `_`, `.`; not starting with `.` or a space |
| `devices` | yes | The devices, in wiring order, at least one |
| `overwrite` | no | true replaces a saved configuration of this name; otherwise that name is refused |
| `eoe` | no | The master's EoE endpoint: `{"enabled": true, "ip": "172.31.254.254", "mask": "255.255.255.0"}`; the mask must be 255.255.255.0 or wider. Optional `"devices": {"first", "last", "mask", "gateway", "dns"}`: the range EoE devices get their addresses from, in list order, skipping the master's and fixed ones (default: .1 to .254 of the master's network, its mask, the master as gateway, no DNS server). Default: as the running ini. |
| `cycle_us` | no | 125 to 100000. Default: the cycle of the scheduler task `ctrlXAutomation`. Another value gives a warning: the master reports an invalid cycle time until they match. |

Per device:

| Key | Needed | Meaning |
|---|---|---|
| `vendor`, `product`, `revision` | yes | Which ESI to use: the library must hold exactly this one. A device on the bus with another revision is written with the ESI's, as in the UI. |
| `name` | no | Unique, letters, digits and `_`, starting with a letter. Default: the ESI's type, then `_2`, `_3`. |
| `connected_to` | no | `{"device": <name of a device above it>, "port": "B", "C" or "D"}`, for a branch. Default: port B of the device above it. |
| `pdos` | no | Assigned PDO indexes, where the ESI lets them be changed. Default: the ESI's. |
| `modules` | no | Modular devices: a module ident per slot, 0 = empty. Default: the ESI's. |
| `dc_mode` | no | Name of a DC mode in the ESI. Default: its first. |
| `startup` | no | CoE: `{"index", "subindex", "data" (hex, device byte order), "transitions" (IP, PS, SO; default PS), "comment"}`. SoE: `{"idn", "data", "comment"}`. Added to the ESI's own commands. |
| `variable_names` | no | `"in:<standard name>"` or `"out:<standard name>"` -> name. To find them, add the device to the Configuration list: `GET /devices/<list position>/edit` lists them under `variables` (`key`). |
| `eoe` | no | EoE devices only: `{"enabled": false}` leaves EoE off; `"mode": "switch"` makes it a switch port (MAC only, no IP); `"ip"`, `"mask"`, `"gateway"`, `"dns"`, `"dns_name"` (up to 31 characters), `"mac"` (`"02:60:34:FE:03:E9"` or 12 hex digits). Each one left out is automatic. |
| `soe_mdt`, `soe_at` | no | SoE drives: the telegrams as lists of IDNs (`47`, `"S-0-0047"`) |

## What a document doesn't do

- The result depends only on the document and the ESI files: not on the bus or the running configuration, and no fallback to a
  device's EEPROM. A device whose ESI is missing is refused.
- It doesn't check that the devices are connected. A configuration that doesn't fit the bus is found at activation: the master
  doesn't reach OP and `activation/error` says so.
- Opening a created configuration in the UI makes it the Configuration list, as with any saved one.

Activating stops the bus. A PLC that does it from its own program has to bring motion and outputs to a safe state first.
