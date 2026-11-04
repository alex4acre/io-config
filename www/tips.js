// SPDX-License-Identifier: MIT
// Tooltips for table columns and settings. A title already in index.html stays.

const COLUMN_TIPS = {
  devices: ["Place on the bus, counted from the master", "The device's name in the configuration",
    "The device type, from its description", "EtherCAT station address", "Revision the device reports",
    "EtherCAT state now: Init, Pre-OP, Bootstrap, Safe-OP or OP",
    "Is there a device description file (ESI) in the library for this device?",
    "How the master's running configuration (ENI) describes this device",
    "The device's own EEPROM. Not read in a normal scan: use Read EEPROM on the device card, or Read all EEPROMs",
    "Result of comparing the device, ESI, ENI and EEPROM. Hover a value to read its findings"],
  pd: ["Process data variable, as the master publishes it", "Output (to the device) or input (from the device)", "Data type", "Value now", "New value to write; needs Writable"],
  "o-list": ["CoE object index", "Object name", "Object type: variable, array or record"],
  "o-entries": ["Subindex", "Entry name", "Data type", "Access: read, write", "Value read from the device", "New value to write; needs Writable"],
  "s-list": ["SoE parameter number (IDN), e.g. S-0-0265", "Parameter name", "Unit"],
  "d-sm": ["Sync manager: the memory area for mailbox or process data", "As the ESI describes it", "As the running configuration writes it", "As the EEPROM describes it"],
  "d-pdos": ["Output (Rx) or input (Tx)", "PDO index", "PDO name", "Found in the ESI", "Found in the running configuration", "Found in the EEPROM", "The values inside the PDO"],
  "d-vars": ["Variable name in the running configuration", "Output or input", "Data type", "Size in bits", "Bit position in the process image"],
  "d-init": ["Where the command comes from", "State change that sends it, e.g. PS = Pre-OP to Safe-OP", "CoE object or SoE parameter", "Value written", "Comment in the configuration"],
  "cfg-devices": ["Place in this list, in wiring order; a device on the bus that isn't in the list has none", "Name in the configuration; click to rename", "The device type, from its description", "Revision this list configures"],
  "e-modules": ["Slot of the modular device", "Module in that slot"],
  "e-pdos": ["Assign this PDO to the configuration", "Output (Rx) or input (Tx)", "PDO index", "PDO name", "Size in bits", "Your name for the variable: letters, digits and _, starting with a letter. Empty = the standard name, shown in grey", "What the ESI requires or excludes"],
  "e-soe-cmds": ["SoE parameter number", "Value sent at start-up", "What the value does"],
  "e-startup": ["CoE object or SoE parameter", "Value written when the device starts", "State change that sends it, e.g. PS = Pre-OP to Safe-OP", "Your note"],
  "plan-devices": ["Place in the new configuration", "Device name", "Where the description came from: ESI, running configuration or EEPROM", "Input bytes", "Output bytes"],
  saved: ["Name you gave it", "Active: what the master ran. Proposed: your list, not written", "When it was saved", "Number of devices"],
  backups: ["When it was saved", "What replaced it", "Devices in it"],
  "esi-devices": ["Device type", "Device name", "Device group in the ESI", "Vendor", "Product code", "Revision", "ESI file it comes from"],
};

const SETTING_TIPS = {
  "tab-bus": "What is on the bus and how it runs now. Live values and online writes", "tab-config": "The device list and everything that gets written to the master",
  "tab-esi": "Device description files (ESI) the app knows",
  "state-set": "Asks the master for the state chosen under Request; asks you first", reread: "The next Scan bus here also reads every device's EEPROM for a full cross-check: about 5 s per device. Unticks itself",
  scan: "Reads the master's status, its running configuration and each device. Writes nothing",
  "d-read-eeprom": "Reads this device's EEPROM, a few seconds. Needed for a device with no ESI; also cross-checks it against its ESI",
  "c-read-eeprom": "Reads this device's EEPROM, a few seconds. Needed for a device with no ESI; also cross-checks it against its ESI",
  "d-close": "Closes this device card", "d-request-op": "Asks the master to bring this device to OP and shows the answer. Needs Writable",
  "d-pd": "Live inputs and outputs of this device", "d-objects": "The device's CoE object dictionary", "d-soe": "The drive's SoE parameters", "d-info": "Findings and what ESI, ENI and EEPROM say",
  "pd-live": "Updates the values every 200 ms", "o-refresh": "Reads the object list from the device again", "o-filter": "Finds an object by index, e.g. 8000 or 0x8000",
  "s-filter": "Finds a parameter, e.g. S265, P12 or 265", "s-write": "Writes the value to the drive now. Needs Writable", "s-startup": "Keeps the value as a startup parameter of the configuration",
  "add-device": "Type to search the ESI library", "add-name": "Name in the configuration. Optional: empty takes one from the ESI", add: "Adds the device at the bottom of the list",
  "c-close": "Closes this device card", "d-edit": "Name, PDOs, modules and DC mode", "d-startup": "Values written to the device when it starts",
  "e-name": "Must start with a letter; letters, digits and _ only. Unique in the list", "e-dc": "Distributed clocks mode from the ESI", "e-mdt-add": "Adds a parameter to the telegram from master to drive (MDT)",
  "e-at-add": "Adds a parameter to the telegram from drive to master (AT)", "e-save": "Saves these settings for this device. Nothing is written to the master", "e-reset": "Drops your settings for this device",
  cycle: "Master cycle time. A scan and Update set it to the active value. It must match scheduler task ctrlXAutomation",
  build: "Builds the configuration from the list and shows it. Writes nothing", "get-eni": "Downloads the built ENI file", "get-ini": "Downloads the built ini file",
  write: "Writes the built configuration to the master. The master restarts the bus; the old configuration goes to Backups",
  "save-name": "Name for the saved configuration", "save-active": "Keeps a copy of what the master runs now", "save-proposed": "Keeps your list and settings without writing them",
  "esi-files": "ESI files (XML) or a zip of them", "esi-upload": "Adds the chosen files to the library", "esi-search": "Filters by type, name, group, vendor, file or product code",
  "m-devices": "Devices found on the bus against devices configured", "m-topology": "Whether the wiring matches the configuration",
  "m-cycle": "Cycle time of the master's running configuration", "m-config": "Identity of the running configuration", "m-time": "When the bus was last read",
};

function applyTips() {
  const missing = [];
  const set = (element, text) => {
    if (element && !element.title) element.title = text;
  };
  for (const [table, tips] of Object.entries(COLUMN_TIPS)) {
    const heads = document.querySelectorAll(`#${table} thead th`);
    if (!heads.length) missing.push(table);
    heads.forEach((th, i) => {
      if (tips[i]) set(th, tips[i]);
    });
  }
  for (const [id, tip] of Object.entries(SETTING_TIPS)) {
    const element = document.getElementById(id);
    if (!element) {
      missing.push(id);
      continue;
    }
    set(element, tip);
    if (element.parentElement.tagName === "LABEL") set(element.parentElement, tip);
    if (element.tagName === "DD") set(element.previousElementSibling, tip);       // the label of a master field
  }
  if (missing.length) console.warn("tooltips for unknown elements:", missing);
  return missing;
}

window.tipsMissing = applyTips();
