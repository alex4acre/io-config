# SPDX-License-Identifier: MIT

"""Builds the bus configuration for the EtherCAT master: the ENI (XML) and the ini (text).

Input is one device configuration per bus position (see device_from_esi / device_from_eeprom /
apply_eni). Everything that depends on the bus, not on the device type, is computed here: station
addresses, process-image offsets, FMMUs, the cyclic frame, working counters, the state-machine init
commands, DC and the process-image variables.

The layout and every command follow the ENI ctrlX I/O Engineering 4.6.1 generated for a real bus
(tests/data/controller-eni.xml); tests/test_generate.py checks that the same bus gives the same file,
byte for byte. Devices without a mailbox (e.g. ctrlX I/O XI112108) are built from their ESI alone: no mailbox
section, process data on the sync managers the ESI names, and sync managers / FMMUs marked OpOnly switched on
only in the SafeOp->Op transition (ETG.2000); no I/O Engineering reference for those yet. Buses with more than
one frame of process data are refused.
"""

import re
import xml.etree.ElementTree as ET

FIRST_ADDRESS = 1001
LOGICAL_OUTPUTS = 0x10000
LOGICAL_INPUTS = 0x10800
LOGICAL_MAILBOX_STATE = 0x80000
FRAME_HEADER = 10              # bytes before the first datagram's data in the process images
MAX_FRAME_DATA = 1400          # one Ethernet frame; more needs a second frame (not supported yet)

# EtherCAT commands
NOP, APRD, APWR, FPRD, FPWR, BRD, BWR, LRD, LWR, ARMW = 0, 1, 2, 4, 5, 7, 8, 10, 11, 13

# registers
AL_CONTROL, AL_STATUS, STATION_ADDRESS, EEPROM_CONFIG, EEPROM_CONTROL, EEPROM_DATA = 0x120, 0x130, 0x10, 0x500, 0x502, 0x508
SM_BASE, FMMU_BASE = 0x800, 0x600
DC_ACTIVATION, DC_START_TIME, DC_CYCLE_TIME, DC_SYSTEM_TIME = 0x980, 0x990, 0x9A0, 0x910

DATA_TYPE_NAMES = {"BOOL": "BIT"}       # the ENI writes BIT where ESI/EEPROM say BOOL
PROTOCOL_ORDER = ["AoE", "CoE", "SoE", "FoE", "EoE", "VoE"]     # the order the ENI lists mailbox protocols in
DEFAULT_TIMEOUTS = {"preop": 3000, "safeop_op": 10000, "back_to_init": 5000, "back_to_safeop": 200}   # ms, ESI defaults


class GenerateError(ValueError):
    pass


def variable_name(text: str) -> str:
    """"Ch. 1 Overcurrent" -> "Ch_1_Overcurrent" (the realtime_data names the master derives from the ENI)."""
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")


def device_name(text: str) -> str:
    """"ctrlX DRIVE XCS2-F/H (SoE)" -> "ctrlX_DRIVE_XCS2_F_H_SoE_" (I/O Engineering keeps the trailing "_")."""
    return re.sub(r"[^A-Za-z0-9]+", "_", text).lstrip("_") or "Device"


def le(value: int, size: int) -> str:
    return value.to_bytes(size, "little", signed=value < 0).hex().upper()


# --- device configurations ------------------------------------------------------------------------

def _sm_list(sms):
    return [{"no": s["no"], "type": s["type"], "start": s["start"], "size": s["size"], "control": s["control"],
             "enable": s["enable"], "op_only": s.get("op_only", False)} for s in sms]


def _oversampled(pdo: dict) -> dict:
    """An oversampling PDO as the ENI lists it: one entry per sample, named "<entry> <k>", and the PDO named
    "<name> 0" (what ctrlX I/O Engineering does for the XI190202 at factor 1)."""
    os_ = pdo.get("oversampling")
    if not os_:
        return pdo
    entries = []
    for k in range(os_["factor"]):
        for e in pdo["entries"]:
            entries.append(dict(e, index=e["index"] + k * os_["index_inc"] if e["index"] else 0,
                                name=f'{e["name"]} {k}' if e["index"] else e["name"]))
    return dict(pdo, name=f'{pdo["name"]} 0', entries=entries)


def device_from_esi(esi: dict, name: str) -> dict:
    """A device with the ESI's defaults: the PDOs the ESI assigns to a sync manager, the first DC mode."""
    mbx_out = next((s for s in esi["sm"] if s["type"] == "MBoxOut"), None)
    mbx_in = next((s for s in esi["sm"] if s["type"] == "MBoxIn"), None)
    mailbox = None
    if mbx_out and mbx_in:
        boot = esi.get("bootstrap")
        mailbox = {
            "send": (mbx_out["start"], mbx_out["size"]), "recv": (mbx_in["start"], mbx_in["size"]),
            "bootstrap": (int.from_bytes(boot[0:2], "little"), int.from_bytes(boot[2:4], "little"),
                          int.from_bytes(boot[4:6], "little"), int.from_bytes(boot[6:8], "little")) if boot else None,
            "protocols": sorted(esi["mailbox"], key=lambda x: PROTOCOL_ORDER.index(x) if x in PROTOCOL_ORDER else 99),
            "coe": {"pdo_assign": esi.get("pdo_assign", False), "profile_no": esi.get("profile_no"),
                    "timeout": esi.get("mailbox_timeout", 2000), "init": list(esi.get("coe_init", []))}
            if "CoE" in esi["mailbox"] else None,
            "soe": {"timeout": esi.get("mailbox_timeout", 2000), "init": list(esi.get("soe_init", [])),
                    "dictionary": esi.get("dictionary")}
            if "SoE" in esi["mailbox"] else None,
        }
    modes = esi.get("dc_modes") or []
    return {
        "name": name, "vendor": esi["vendor"], "product": esi["product"], "revision": esi["revision"],
        "check_revision": esi.get("check_revision", ""), "physics": esi.get("physics", "YY"), "esc": dict(esi.get("esc") or {}),
        "timeouts": {**DEFAULT_TIMEOUTS, **(esi.get("timeouts") or {})}, "fmmu": list(esi.get("fmmu") or []),
        "fmmu_op_only": list(esi.get("fmmu_op_only") or []),
        "sm": _sm_list(esi["sm"]), "mailbox": mailbox,
        "pdos": [_oversampled(p) for p in esi["pdos"] if p["sm"] is not None],
        "dc": {"assign_activate": modes[0]["assign_activate"], "shift": modes[0].get("shift_sync0") or 0}
              if modes and modes[0]["assign_activate"] else None,
        "source": "esi",
    }


def device_from_eeprom(sii: dict, name: str) -> dict:
    """A device learned from its EEPROM only (no ESI): default PDOs, no DC, ESI-default timeouts."""
    esi_like = {
        "vendor": sii["vendor"], "product": sii["product"], "revision": sii["revision"], "check_revision": "LW_EQ",
        "physics": "YY", "fmmu": sii.get("fmmu") or [], "sm": sii["sm"], "pdos": sii["pdos"],
        "mailbox": sii["mailbox"], "bootstrap": None, "dc_modes": [],
        "pdo_assign": bool((sii.get("coe") or {}).get("pdo_assign")), "profile_no": None,
    }
    dev = device_from_esi(esi_like, name)
    dev["source"] = "eeprom"
    return dev


def device_from_eni(slave: dict) -> dict:
    """A device described by the current configuration alone (no ESI): what the ENI holds is enough to write
    the same device again."""
    h = slave["harvest"]
    mbx = h["mailbox"]
    sms = [dict(sm, op_only=sm["no"] in h.get("sm_op_only", [])) for sm in slave["sm"]]
    if mbx:
        sms = [{"no": 0, "type": "MBoxOut", "start": mbx["send"][0], "size": mbx["send"][1],
                "control": h["sm_control"].get(0, 0x26), "enable": True},
               {"no": 1, "type": "MBoxIn", "start": mbx["recv"][0], "size": mbx["recv"][1],
                "control": h["sm_control"].get(1, 0x22), "enable": True}] + sms
    esi_like = {
        "vendor": slave["vendor"], "product": slave["product"], "revision": slave["revision"],
        "check_revision": "LW_EQ_HW_EQ_OR_G" if h["check_high"] else "LW_EQ" if h["check_low"] else "",
        "physics": slave["physics"] or "YY",
        "timeouts": h["timeouts"], "fmmu": [{"outputs": "Outputs", "inputs": "Inputs", "mailbox state": "MBoxState"}
                                           .get(k, k) for k in h["fmmu"]],
        "sm": sms, "pdos": slave["pdos"], "mailbox": slave["mailbox"],
        "bootstrap": b"".join(x.to_bytes(2, "little") for x in mbx["bootstrap"]) if mbx and mbx["bootstrap"] else None,
        "pdo_assign": bool(mbx and mbx["pdo_assign"]), "profile_no": mbx["coe_profile"] if mbx else None,
        "mailbox_timeout": mbx["coe_timeout"] if mbx else 2000, "dc_modes": [],
    }
    return apply_eni(device_from_esi(esi_like, slave["name"]), slave)


def apply_eni(dev: dict, slave: dict) -> dict:
    """Keeps the choices the current configuration made for this device: its name, assigned PDOs (as
    configured there), DC activation and extra CoE startup commands."""
    out = dict(dev, name=slave["name"], source="eni")
    assigned = set()
    for sm in slave["sm"]:
        assigned.update(sm.get("pdos", []))
    out["pdos"] = [p for p in slave["pdos"] if p["index"] in assigned]
    shift = int((slave.get("dc") or {}).get("ShiftTime") or 0) or (dev.get("dc") or {}).get("shift") or 0
    out["dc"] = {"assign_activate": slave["dc_activate"], "shift": shift} if slave.get("dc_activate") else None
    if out.get("mailbox") and out["mailbox"].get("coe") is not None:
        extra = [dict(c, from_eni=True) for c in slave.get("coe_init", []) if c["index"] not in (0x1C12, 0x1C13)]
        out["mailbox"] = dict(out["mailbox"], coe=dict(out["mailbox"]["coe"], init=extra))
    if slave["harvest"].get("eoe_mac"):
        out["eoe_mac"] = slave["harvest"]["eoe_mac"]
    return out


# --- XML helpers ------------------------------------------------------------------------------------

def _sub(parent, tag, text=None, **attrib):
    e = ET.SubElement(parent, tag, attrib)
    if text is not None:
        e.text = str(text)
    return e


def _cmd(parent, transitions, comment, cmd, adp, ado, data=None, cnt=None, requires=True, retries=3,
         validate=None, before_slave=False, data_length=None, validate_type=None):
    c = _sub(parent, "InitCmd")
    for t in transitions.split(","):
        _sub(c, "Transition", t)
    if before_slave:
        _sub(c, "BeforeSlave", 1)
    _sub(c, "Comment", comment)
    if requires:
        _sub(c, "Requires", "cycle")
    _sub(c, "Cmd", cmd)
    _sub(c, "Adp", adp)
    _sub(c, "Ado", ado)
    if data_length is not None:
        _sub(c, "DataLength", data_length)
    else:
        _sub(c, "Data", data)
    if cnt is not None:
        _sub(c, "Cnt", cnt)
    _sub(c, "Retries", retries)
    if validate:
        v = _sub(c, "Validate", **({"Type": validate_type} if validate_type else {}))
        _sub(v, "Data", validate[0])
        if validate[1]:
            _sub(v, "DataMask", validate[1])
        _sub(v, "Timeout", validate[2])
    return c


def _master_cmds(parent):
    ip = "IP"
    for comment, cmd, ado, data, retries in (("read slave count", BRD, AL_STATUS, "0000", 0),
                                             ("read slave count", BRD, AL_STATUS, "0000", 0),
                                             ("enable ECAT IRQ", BWR, 0x200, "0400", 3),
                                             ("clear configured addresses", BWR, STATION_ADDRESS, "0000", 3),
                                             ("clear crc register", BWR, 0x300, "0000000000000000", 3)):
        _cmd(parent, ip, comment, cmd, 0, ado, data, retries=retries, before_slave=True)
    _cmd(parent, "IP,PI,BI,SI,OI", "clear fmmu", BWR, 0, FMMU_BASE, data_length=256, before_slave=True)
    _cmd(parent, ip, "clear sm", BWR, 0, SM_BASE, data_length=256, before_slave=True)
    _cmd(parent, ip, "clear dc system time", BWR, 0, DC_SYSTEM_TIME, data_length=32, before_slave=True)
    for comment, ado, data in (("clear dc cycle cfg", 0x981, "00"), ("reset dc speed", 0x930, "0010"),
                               ("configure dc filter", 0x934, "000C"), ("disable second physical address", 0x103, "00")):
        _cmd(parent, ip, comment, BWR, 0, ado, data, before_slave=True)


def _sm_data(start, size, control, active):
    return le(start, 2) + le(size, 2) + le(control, 1) + "00" + le(1 if active else 0, 2)


def _fmmu_data(logical, size, physical, kind, active, start_bit=0, end_bit=7):
    return (le(logical, 4) + le(size, 2) + le(start_bit, 1) + le(end_bit if size else 0xFF, 1) +
            le(physical, 2) + "00" + le(kind, 1) + le(1 if active else 0, 1) + "000000")


# --- the generator ----------------------------------------------------------------------------------

def _layout(devices):
    """Per device: output/input sizes and offsets; also the mailbox-state bit."""
    out_off = in_off = mbx = 0
    lay = []
    for pos, dev in enumerate(devices):
        sizes = {}
        for direction in ("rx", "tx"):
            bits = sum(e["bits"] for p in dev["pdos"] if p["dir"] == direction for e in p["entries"])
            if bits % 8:
                raise GenerateError(f'{dev["name"]}: {"outputs" if direction == "rx" else "inputs"} are {bits} bits, '
                                    "not whole bytes")
            sizes[direction] = bits // 8
        has_mbx = dev.get("mailbox") is not None
        lay.append({"pos": pos, "address": FIRST_ADDRESS + pos, "autoinc": (-pos) & 0xFFFF,
                    "out_size": sizes["rx"], "in_size": sizes["tx"], "out_off": out_off, "in_off": in_off,
                    "mbx_bit": mbx if has_mbx else None})
        out_off += sizes["rx"]
        in_off += sizes["tx"]
        mbx += has_mbx
    if max(out_off, in_off) > 2048:
        raise GenerateError("more than 2048 bytes of process data in one direction")
    return lay, out_off, in_off, mbx


def _revision_checks(parent, dev, ai):
    def check(comment_write, comment_read, word_addr, data, validate_type=None):
        _cmd(parent, "IP", comment_write, APWR, ai, EEPROM_CONTROL, "0001" + le(word_addr, 1) + "000000", cnt=1)
        _cmd(parent, "IP", comment_read, APRD, ai, EEPROM_DATA, "0" * len(data), cnt=1, validate=(data, "", 100),
             validate_type=validate_type)

    _cmd(parent, "IP", "assign EEPROM to ECAT", APWR, ai, EEPROM_CONFIG, "00", cnt=1)
    check("check vendor id", "check vendor id", 0x08, le(dev["vendor"], 4))
    check("check product code", "check product id", 0x0A, le(dev["product"], 4))
    mode = dev.get("check_revision", "")
    if not mode:                                         # the ESI asks for no revision check
        return
    check("check revision number (lo word)", "check revision number (lo word)", 0x0C, le(dev["revision"] & 0xFFFF, 2))
    if mode != "LW_EQ":                                  # LW_EQ: the low word only
        check("check revision number (high word >=)", "check revision number (high word >=)", 0x0D,
              le(dev["revision"] >> 16, 2), validate_type="EQ_OR_G")


def _ip_bytes(text: str) -> str:
    return bytes(reversed([int(x) for x in text.split(".")])).hex().upper()


def _eoe_ip(name: str, phys: int, eoe: dict, mac: str = None) -> str:
    """EoE "set IP parameter" request (ETG.1000.6): MAC 02:60:34:FE:<station address> (or the MAC the running
    configuration gave the device: I/O Engineering keeps it when the device moves), the next address of the
    master's EoE network, its mask, the master as gateway, no DNS server, the device name as DNS name."""
    mac = mac or "026034FE" + phys.to_bytes(2, "big").hex().upper()
    data = (le(2, 4) + le(0x2F, 4) + mac +
            _ip_bytes(eoe["ip"]) + _ip_bytes(eoe["mask"]) + _ip_bytes(eoe["gateway"]) + "00000000")
    return data + name.encode("latin-1")[:31].ljust(32, b"\0").hex().upper()


def eoe_addresses(ini: dict, devices: list) -> dict:
    """position -> EoE IP settings for the devices with EoE, from the master's EoE endpoint in the ini."""
    ep = ini.get("EtherCAT.Master.EoE.Endpoint", {})
    if ep.get("Enabled", "").upper() != "TRUE" or not ep.get("IpAddress"):
        return {}
    gw = [int(x) for x in ep["IpAddress"].split(".")]
    mask = ep.get("SubnetMask", "255.255.255.0")
    out, n = {}, 0
    for pos, d in enumerate(devices):
        if "EoE" in ((d.get("mailbox") or {}).get("protocols") or []):
            n += 1
            ip = gw[:3] + [n]
            out[pos] = {"ip": ".".join(map(str, ip)), "mask": mask, "gateway": ep["IpAddress"]}
    return out


def _slave(config, dev, lay, cycle_ns, previous, inputs_vars, outputs_vars, eoe=None):
    pos, phys, ai = lay["pos"], lay["address"], lay["autoinc"]
    t = dev["timeouts"]
    s = _sub(config, "Slave")
    info = _sub(s, "Info")
    for tag, value in (("Name", dev["name"]), ("PhysAddr", phys), ("AutoIncAddr", ai), ("Physics", dev["physics"]),
                       ("VendorId", dev["vendor"]), ("ProductCode", dev["product"]), ("RevisionNo", dev["revision"]),
                       ("SerialNo", 0)):
        _sub(info, tag, value)

    # process data
    pd = _sub(s, "ProcessData")
    if lay["out_size"]:
        send = _sub(pd, "Send")
        _sub(send, "BitStart", (FRAME_HEADER + lay["out_off"]) * 8)
        _sub(send, "BitLength", lay["out_size"] * 8)
    if lay["in_size"]:
        recv = _sub(pd, "Recv")
        _sub(recv, "BitStart", (FRAME_HEADER + lay["in_off"]) * 8)
        _sub(recv, "BitLength", lay["in_size"] * 8)
    process_sms = [sm for sm in dev["sm"] if sm["type"] in ("Outputs", "Inputs")]

    def sm_size(sm):
        """Bytes of one process-data sync manager: the whole direction, or (several sync managers per
        direction, e.g. XI211116 outputs on #x0F00 and #x0F01) the PDOs assigned to it."""
        if sum(1 for x in process_sms if x["type"] == sm["type"]) == 1:
            return lay["out_size"] if sm["type"] == "Outputs" else lay["in_size"]
        return (sum(e["bits"] for p in dev["pdos"] if p["sm"] == sm["no"] for e in p["entries"]) + 7) // 8

    for sm in process_sms:
        pdos = [p for p in dev["pdos"] if p["sm"] == sm["no"]]
        size = sm_size(sm)
        e = _sub(pd, f'Sm{sm["no"]}')
        _sub(e, "Type", sm["type"])
        if size:
            _sub(e, "DefaultSize", size)
        _sub(e, "StartAddress", sm["start"])
        _sub(e, "ControlByte", sm["control"])
        _sub(e, "Enable", 1)           # always 1 in the ENI; the register value below says whether it is active
        for p in pdos:
            _sub(e, "Pdo", p["index"])
    for direction, tag in (("rx", "RxPdo"), ("tx", "TxPdo")):
        for p in dev["pdos"]:
            if p["dir"] != direction:
                continue
            attrib = {"Fixed": "1"} if p["fixed"] else {}
            if p["mandatory"]:
                attrib["Mandatory"] = "1"
            attrib["Sm"] = str(p["sm"])
            attrib["Su"] = "1001"
            e = _sub(pd, tag, **attrib)
            _sub(e, "Index", f'#x{p["index"]:x}')
            _sub(e, "Name", p["name"])
            for x in p.get("excludes", []):
                _sub(e, "Exclude", x)
            for x in p["entries"]:
                en = _sub(e, "Entry")
                _sub(en, "Index", f'#x{x["index"]:x}')
                _sub(en, "SubIndex", x["subindex"] if x["index"] else 0)
                _sub(en, "BitLen", x["bits"])
                _sub(en, "Name", x["name"] if x["index"] else "")
                if x["index"] and x.get("type"):
                    _sub(en, "DataType", DATA_TYPE_NAMES.get(x["type"], x["type"]))

    # process image variables
    for direction, start, target in (("tx", lay["in_off"], inputs_vars), ("rx", lay["out_off"], outputs_vars)):
        bit = (FRAME_HEADER + start) * 8
        for p in dev["pdos"]:
            if p["dir"] != direction:
                continue
            for x in p["entries"]:
                if x["index"]:
                    target.append((f'{dev["name"]}/{variable_name(p["name"])}.{variable_name(x["name"])}',
                                   DATA_TYPE_NAMES.get(x["type"], x["type"]), x["bits"], bit))
                bit += x["bits"]

    # mailbox
    mbx = dev["mailbox"]
    if mbx is not None:
        m = _sub(s, "Mailbox")
        send = _sub(m, "Send")
        _sub(send, "Start", mbx["send"][0])
        _sub(send, "Length", mbx["send"][1])
        recv = _sub(m, "Recv")
        _sub(recv, "Start", mbx["recv"][0])
        _sub(recv, "Length", mbx["recv"][1])
        _sub(recv, "StatusBitAddr", lay["mbx_bit"])
        if mbx["bootstrap"]:
            b = _sub(m, "BootStrap")
            bs = _sub(b, "Send")
            _sub(bs, "Start", mbx["bootstrap"][0])
            _sub(bs, "Length", mbx["bootstrap"][1])
            br = _sub(b, "Recv")
            _sub(br, "Start", mbx["bootstrap"][2])
            _sub(br, "Length", mbx["bootstrap"][3])
        for proto in mbx["protocols"]:
            _sub(m, "Protocol", proto)
        coe = mbx.get("coe")
        if coe is not None:
            ce = _sub(m, "CoE")
            ic = _sub(ce, "InitCmds")
            if coe["pdo_assign"]:
                for index, direction in ((0x1C12, "rx"), (0x1C13, "tx")):
                    pdos = [p["index"] for p in dev["pdos"] if p["dir"] == direction]
                    c = _sub(ic, "InitCmd", **({"CompleteAccess": "1"} if pdos else {}))
                    _sub(c, "Transition", "PS")
                    _sub(c, "Comment", f"download pdo {index:#x} content" if pdos else f"clear sm pdos ({index:#x})")
                    _sub(c, "Timeout", coe.get("timeout", 2000))
                    _sub(c, "Ccs", 1)
                    _sub(c, "Index", index)
                    _sub(c, "SubIndex", 0)
                    _sub(c, "Data", (le(len(pdos), 1) + "00" + "".join(le(x, 2) for x in pdos)) if pdos else "00")
            for x in coe.get("init", []):
                c = _sub(ic, "InitCmd", **({"CompleteAccess": "1"} if x.get("complete_access") else {}))
                for tr in x["transitions"]:
                    _sub(c, "Transition", tr)
                _sub(c, "Comment", x.get("comment", ""))
                _sub(c, "Timeout", coe.get("timeout", 2000))
                _sub(c, "Ccs", 1)
                _sub(c, "Index", x["index"])
                _sub(c, "SubIndex", x["subindex"])
                _sub(c, "Data", x["data"])
            if coe.get("profile_no") is not None:
                pr = _sub(_sub(ce, "Profile"), "ChannelInfo")
                _sub(pr, "ProfileNo", coe["profile_no"])
        if mbx.get("soe") is not None:
            ic = _sub(_sub(m, "SoE"), "InitCmds")
            for x in soe_commands(dev, cycle_ns // 1000):
                c = _sub(ic, "InitCmd")
                for tr in x["transitions"]:
                    _sub(c, "Transition", tr)
                _sub(c, "Comment", x.get("comment", ""))
                _sub(c, "Timeout", mbx["soe"].get("timeout", 2000))
                _sub(c, "OpCode", 3)                     # write request
                _sub(c, "DriveNo", x.get("drive", 0))
                _sub(c, "IDN", x["idn"])
                _sub(c, "Elements", 64)                  # the value
                _sub(c, "Attribute", 0)
                _sub(c, "Data", x["data"])
        if "EoE" in mbx["protocols"] and eoe is not None:
            c = _sub(_sub(_sub(m, "EoE"), "InitCmds"), "InitCmd", Fixed="1")
            for tr in ("IP", "PS"):
                _sub(c, "Transition", tr)
            _sub(c, "Comment", "eoe init")
            _sub(c, "Timeout", 0)
            _sub(c, "Data", _eoe_ip(dev["name"], phys, eoe, dev.get("eoe_mac")))

    # state machine
    ic = _sub(s, "InitCmds")
    init_v = ("0100", "0F00")
    _cmd(ic, "PI,BI,SI,OI", "set device state to INIT", APWR, ai, AL_CONTROL, "0100")
    _cmd(ic, "PI,SI,OI", "check device state for INIT", APRD, ai, AL_STATUS, "0000", validate=init_v + (t["back_to_init"],))
    _cmd(ic, "BI", "check device state for INIT", APRD, ai, AL_STATUS, "0000", validate=init_v + (10000,))
    _cmd(ic, "IP,IB", "set device state to INIT", APWR, ai, AL_CONTROL, "0100", cnt=1)
    _cmd(ic, "IP,IB", "check device state for INIT", APRD, ai, AL_STATUS, "0000", cnt=1, validate=init_v + (t["preop"],))
    _revision_checks(ic, dev, ai)
    _cmd(ic, "IP,IB", "set physical address", APWR, ai, STATION_ADDRESS, le(phys, 2), cnt=1)
    _cmd(ic, "SP,OP", "set device state to preop", FPWR, phys, AL_CONTROL, "0200", retries=300)
    if dev.get("dc"):
        _cmd(ic, "SI,OI", "clear DC activation", APWR, ai, DC_ACTIVATION, "0000")
        _cmd(ic, "SP,OP", "clear DC activation", FPWR, phys, DC_ACTIVATION, "0000")
        _cmd(ic, "PS", "set DC cycle time", FPWR, phys, DC_CYCLE_TIME, le(cycle_ns, 4) + le(0, 4), cnt=1)
        _cmd(ic, "PS", "set DC start time", FPWR, phys, DC_START_TIME, le(dev["dc"].get("shift") or 0, 8), cnt=1)
        _cmd(ic, "PS", "set DC activation", FPWR, phys, DC_ACTIVATION, le(dev["dc"]["assign_activate"], 2), cnt=1)
        if dev.get("esc"):                       # devices whose ESI sets ESC registers (seen: ctrlX DRIVE)
            _cmd(ic, "PS", "set DC latch cfg", FPWR, phys, 0x9A8, "0000")
    zero8 = "0" * 16
    mbx_sms = [(0, "mailbox out", mbx["send"], 0x26), (1, "mailbox in", mbx["recv"], 0x22)] if mbx else []
    sm_by_type = {}                              # the first sync manager of each type (the FMMU starts there)
    for sm in dev["sm"]:
        sm_by_type.setdefault(sm["type"], sm)
    any_op_only = any(sm.get("op_only") for sm in process_sms)
    for no, label, (start, size), control in mbx_sms:
        control = sm_by_type.get("MBoxOut" if no == 0 else "MBoxIn", {}).get("control", control)
        ado = SM_BASE + 8 * no
        _cmd(ic, "IP,IB,PI,SI,OI", f"clear sm {no} ({label})", FPWR, phys, ado, zero8)
        _cmd(ic, "BI", f"clear sm {no} ({label})", APWR, ai, ado, zero8)
        _cmd(ic, "IP", f"set sm {no} ({label})", FPWR, phys, ado, _sm_data(start, size, control, True), cnt=1, requires=False)
    for sm in process_sms:
        label = "outputs" if sm["type"] == "Outputs" else "inputs"
        size = sm_size(sm)
        ado = SM_BASE + 8 * sm["no"]
        _cmd(ic, "IP,SP,SI,OP,OI", f'clear sm {sm["no"]} ({label})', FPWR, phys, ado, zero8)
        active = sm["enable"] and size > 0
        # OpOnly (ETG.2000) as ctrlX I/O Engineering writes it (tests/data/x3-8dev-ioe.xml): the sync manager is set
        # up active only in SafeOp->Op, and every process-data sync manager of such a device is cleared in Op->SafeOp
        if any_op_only:
            _cmd(ic, "OS", "clear output sm", FPWR, phys, ado, zero8, requires=False)
        _cmd(ic, "SO" if sm.get("op_only") else "PS", f'set sm {sm["no"]} ({label})', FPWR, phys, ado,
             _sm_data(sm["start"], size, sm["control"], active), cnt=1, requires=False)
    fmmus = dev["fmmu"] or (["Outputs", "Inputs", "MBoxState"] if mbx else ["Outputs", "Inputs"])
    process_fmmus, mbx_fmmu = [], None          # FMMUs are written active, OpOnly or not (as I/O Engineering)
    for i, kind in enumerate(fmmus):
        if kind in ("Outputs", "Inputs"):
            sm = sm_by_type.get(kind)
            size = lay["out_size"] if kind == "Outputs" else lay["in_size"]
            logical = (LOGICAL_OUTPUTS + lay["out_off"]) if kind == "Outputs" else (LOGICAL_INPUTS + lay["in_off"])
            data = _fmmu_data(logical, size, sm["start"] if sm else 0, 2 if kind == "Outputs" else 1, size > 0)
            _cmd(ic, "PS", f"set fmmu {i}({kind.lower()})", FPWR, phys, FMMU_BASE + 16 * i, data, cnt=1, requires=False)
            process_fmmus.append(i)
        elif kind == "MBoxState" and mbx:
            bit = lay["mbx_bit"]
            data = _fmmu_data(LOGICAL_MAILBOX_STATE + bit // 8, 1, SM_BASE + 8 + 5, 1, True, bit % 8, bit % 8)
            _cmd(ic, "IP,IB", f"set fmmu {i}(mailbox state)", FPWR, phys, FMMU_BASE + 16 * i, data, cnt=1, requires=False)
            mbx_fmmu = i
    if "0400" in dev.get("esc", {}):
        _cmd(ic, "IP", "set PDI divider", FPWR, phys, 0x400, le(dev["esc"]["0400"], 2), cnt=1, requires=False)
    if "0420" in dev.get("esc", {}):
        _cmd(ic, "IP", "set SM watchdog", FPWR, phys, 0x420, le(dev["esc"]["0420"], 2), cnt=1, requires=False)
    if mbx and mbx["bootstrap"]:
        for no, label, (start, size) in ((0, "bootstrap out", mbx["bootstrap"][0:2]), (1, "bootstrap in", mbx["bootstrap"][2:4])):
            control = sm_by_type.get("MBoxOut" if no == 0 else "MBoxIn", {}).get("control", 0x26 if no == 0 else 0x22)
            _cmd(ic, "IB", f"set sm {no} ({label})", FPWR, phys, SM_BASE + 8 * no, _sm_data(start, size, control, True),
                 cnt=1, requires=False)
    _cmd(ic, "IP,IB", "assign EEProm to PDI", FPWR, phys, EEPROM_CONFIG, "01", cnt=1)
    _cmd(ic, "II", "assign EEPROM back to ECAT", APWR, -pos, EEPROM_CONFIG, "00")
    _cmd(ic, "IP", "set device state to preop", FPWR, phys, AL_CONTROL, "0200", cnt=1, retries=300)
    state6 = "000000000000"
    _cmd(ic, "SP,OP", "check device state for preop", FPRD, phys, AL_STATUS, state6,
         validate=("020000000000", "0F0000000000", t["preop"]))
    _cmd(ic, "IP", "check device state for preop", FPRD, phys, AL_STATUS, state6,
         validate=("020000000000", "1F0000000000", t["preop"]))
    _cmd(ic, "IP,BI", "assign EEPROM back to ECAT", APWR, -pos, EEPROM_CONFIG, "00")
    if (mbx and mbx["bootstrap"]) or not mbx:     # I/O Engineering writes these for devices without a mailbox too
        _cmd(ic, "IB", "set device state to BOOT", FPWR, phys, AL_CONTROL, "1300", cnt=1)
        _cmd(ic, "IB", "check device state to BOOT", FPRD, phys, AL_STATUS, state6, cnt=1,
             validate=("030000000000", "1F0000000000", t["preop"]))
    _cmd(ic, "OS", "set device state to SAFEOP", FPWR, phys, AL_CONTROL, "0400")
    _cmd(ic, "PS", "set device state to SAFEOP", FPWR, phys, AL_CONTROL, "0400", cnt=1)
    for i in sorted(process_fmmus + ([mbx_fmmu] if mbx_fmmu is not None else [])):     # in FMMU order
        transitions = "PI,BI,SI,OI" if i == mbx_fmmu else "SP,SI,OP,OI"
        _cmd(ic, transitions, f"clear fmmu {i}", FPWR, phys, FMMU_BASE + 16 * i, "0" * 32, requires=False)
    _cmd(ic, "PS", "check device state for SAFEOP", FPRD, phys, AL_STATUS, state6, cnt=1,
         validate=("040000000000", "1F0000000000", t["safeop_op"]))
    _cmd(ic, "OS", "check device state for SAFEOP", FPRD, phys, AL_STATUS, state6,
         validate=("040000000000", "1F0000000000", t["back_to_safeop"]))
    _cmd(ic, "SO", "set device state to OP", FPWR, phys, AL_CONTROL, "0800", cnt=1)
    _cmd(ic, "SO", "check device state to OP", FPRD, phys, AL_STATUS, state6, cnt=1,
         validate=("080000000000", "1F0000000000", t["safeop_op"]))

    if previous is not None:
        pp = _sub(s, "PreviousPort", Selected="1")
        _sub(pp, "Port", previous[0])
        _sub(pp, "PhysAddr", previous[1])
    return s


SOE_CONTROL_WORD, SOE_STATUS_WORD = 134, 135
SOE_CYCLE_IDNS = (1, 2)              # NC cycle time, SoE cycle time: in µs, from the bus cycle


def _idn_list(idns) -> str:
    body = "".join(le(i, 2) for i in idns)
    n = len(idns) * 2
    return le(n, 2) + le(n, 2) + body


def soe_commands(dev: dict, cycle_us: int) -> list:
    """The SoE startup commands of a drive, as ctrlX I/O Engineering writes them (tests/data/x3-drive-ioe.xml):
    telegram type 7 (configurable), the MDT and AT configuration lists from the PDO entries (without the
    control / status word that always lead the telegrams), then the ESI's commands with the cycle times
    set from the bus cycle, then the user's."""
    soe = dev["mailbox"]["soe"]
    at = [e["index"] for p in dev["pdos"] if p["dir"] == "tx" for e in p["entries"] if e["index"] != SOE_STATUS_WORD]
    mdt = [e["index"] for p in dev["pdos"] if p["dir"] == "rx" for e in p["entries"] if e["index"] != SOE_CONTROL_WORD]
    out = [{"transitions": ["PS"], "idn": 15, "data": le(7, 2), "comment": "Telegram type"},
           {"transitions": ["PS"], "idn": 24, "data": _idn_list(mdt), "comment": "MDT list"},
           {"transitions": ["PS"], "idn": 16, "data": _idn_list(at), "comment": "AT list"}]
    for x in soe.get("init", []):
        x = dict(x)
        if x["idn"] in SOE_CYCLE_IDNS:
            x["data"] = le(cycle_us, 2)
        out.append(x)
    return out


def _cyclic(config, lay, out_total, in_total, n_mbx, ref_clock):
    frame = _sub(_sub(config, "Cyclic"), "Frame")
    offset = 0

    def cmd(states, code, data_length, cnt=None, addr=None, adp=None, ado=None, offs=None):
        c = _sub(frame, "Cmd")
        for st in states:
            _sub(c, "State", st)
        _sub(c, "Comment", "cyclic cmd")
        _sub(c, "Cmd", code)
        if addr is not None:
            _sub(c, "Addr", addr)
        else:
            _sub(c, "Adp", adp)
            _sub(c, "Ado", ado)
        _sub(c, "DataLength", data_length)
        if cnt is not None:
            _sub(c, "Cnt", cnt)
        _sub(c, "InputOffs", offs)
        _sub(c, "OutputOffs", offs)

    op_safe, all3 = ("OP", "SAFEOP"), ("OP", "SAFEOP", "PREOP")
    if in_total:
        cmd(op_safe, LRD, in_total, cnt=sum(1 for x in lay if x["in_size"]), addr=LOGICAL_INPUTS, offs=0)
    if out_total:
        cmd(op_safe, LWR, out_total, cnt=sum(1 for x in lay if x["out_size"]), addr=LOGICAL_OUTPUTS, offs=0)
    offset = max(in_total, out_total)
    cmd(all3, BRD, 2, cnt=len(lay), adp=0, ado=AL_STATUS, offs=offset)
    offset += 2
    if ref_clock is not None:
        cmd(all3, NOP, 4, adp=0, ado=0x900, offs=offset)
        offset += 4
        cmd(all3, ARMW, 8, adp=ref_clock, ado=DC_SYSTEM_TIME, offs=offset)
        offset += 8
        cmd(all3, APWR, 8, adp=ref_clock, ado=DC_SYSTEM_TIME, offs=offset)
        offset += 8
    if n_mbx:
        cmd(all3, LRD, (n_mbx + 7) // 8, addr=LOGICAL_MAILBOX_STATE, offs=offset)
        offset += (n_mbx + 7) // 8
    if offset > MAX_FRAME_DATA:
        raise GenerateError(f"{offset} bytes of cyclic data need more than one frame (not supported yet)")
    return FRAME_HEADER + offset


def generate_eni(devices: list, master_name: str, cycle_us: int, previous_ports: dict = None, eoe: dict = None) -> bytes:
    """The ENI for devices in bus order. previous_ports: position -> (port letter, position of the upstream
    device) from the online topology (a letter alone: the device before); default ("B", position - 1), a line."""
    if not devices:
        raise GenerateError("no devices")
    names = [d["name"] for d in devices]
    if len(set(names)) != len(names):
        raise GenerateError("device names must be unique")
    lay, out_total, in_total, n_mbx = _layout(devices)
    cycle_ns = cycle_us * 1000
    ref = next((i for i, d in enumerate(devices) if d.get("dc")), None)

    root = ET.Element("EtherCATConfig", {
        "xmlns:xsd": "http://www.w3.org/2001/XMLSchema", "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance",
        "xsi:noNamespaceSchemaLocation": "EtherCATConfig.xsd"})
    config = _sub(root, "Config")
    master = _sub(config, "Master")
    info = _sub(master, "Info")
    for tag, value in (("Name", master_name), ("Destination", "FFFFFFFFFFFF"), ("Source", "FFFFFFFFFFFF"),
                       ("EtherType", "A488")):
        _sub(info, tag, value)
    ms = _sub(master, "MailboxStates")
    _sub(ms, "StartAddr", LOGICAL_MAILBOX_STATE)
    _sub(ms, "Count", n_mbx)
    if eoe:
        e = _sub(master, "EoE")
        _sub(e, "MaxPorts", len(eoe) + 1)
        _sub(e, "MaxFrames", 120)
        _sub(e, "MaxMACs", 0)
    _master_cmds(_sub(master, "InitCmds"))

    inputs_vars, outputs_vars = [], []
    for pos, (dev, ly) in enumerate(zip(devices, lay)):
        previous = None
        if pos > 0:
            port = (previous_ports or {}).get(pos, "B")
            letter, parent = (port, pos - 1) if isinstance(port, str) else port
            previous = (letter, lay[parent]["address"])
        s = _slave(config, dev, ly, cycle_ns, previous, inputs_vars, outputs_vars, (eoe or {}).get(pos))
        if dev.get("dc"):
            dc = _sub(s, "DC")
            if pos == ref:
                _sub(dc, "ReferenceClock", 1)
            _sub(dc, "CycleTime0", cycle_ns)
            _sub(dc, "CycleTime1", 0)
            _sub(dc, "ShiftTime", dev["dc"].get("shift") or 0)
    image_size = _cyclic(config, lay, out_total, in_total, n_mbx, lay[ref]["autoinc"] if ref is not None else None)

    image = _sub(config, "ProcessImage")
    for tag, variables in (("Inputs", inputs_vars), ("Outputs", outputs_vars)):
        part = _sub(image, tag)
        _sub(part, "ByteSize", image_size)
        for name, dtype, bits, offs in variables:
            v = _sub(part, "Variable")
            _sub(v, "Name", name)
            _sub(v, "DataType", dtype)
            _sub(v, "BitSize", bits)
            _sub(v, "BitOffs", offs)
    body = ET.tostring(root, encoding="unicode")
    return ('<?xml version="1.0" encoding="iso-8859-1"?>' + body).encode("latin-1", "replace")


def generate_ini(current: str, cycle_us: int) -> bytes:
    """The master's ini: the current one with the cycle time set (or a default one)."""
    text = current or DEFAULT_INI
    lines, done = [], False
    for line in text.replace("\r\n", "\n").split("\n"):
        if line.strip().startswith("CycleTimeUs="):
            line, done = f"CycleTimeUs={cycle_us}", True
        lines.append(line)
    if not done:
        raise GenerateError("the ini has no CycleTimeUs")
    return "\r\n".join(lines).encode("latin-1")


DEFAULT_INI = """[EtherCAT.Master]
ConfigMode=ENI_FILE
SyncMode=DCM_AUTO
CycleTimeUs=2000
AllSlavesMustReachMasterState=FALSE
WkcErrorMode=CLEAR_INPUTS
MasterSyncUnits=default
[EtherCAT.Master.DC]
SyncShiftTimeNs=700000
SendOffsetNs=100000
SyncOffsetNs=600000
InputShiftTimeNs=-200000
ContinuousDelayCompensation=FALSE
[EtherCAT.Master.MasterSyncUnits.default]
MsuId=1001
"""


def checksum(data: bytes) -> list:
    """The checksum the master reports for a configuration file (admin/fieldbus_config/checksum)."""
    import zlib
    return list((zlib.crc32(data) ^ 0xFFFFFFFF).to_bytes(4, "little"))
