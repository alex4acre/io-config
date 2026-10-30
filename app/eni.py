# SPDX-License-Identifier: MIT

"""Reads the bus configuration the EtherCAT master runs with: the ENI (EtherCAT Network Information, XML,
as ctrlX I/O Engineering generates it) and the ini (master settings, text).

Only what the app shows and compares is taken out of the ENI: per slave its identity, position, assigned
PDOs, sync managers, mailbox, CoE startup commands, DC, and its process-image variables.
"""

from app.xmlsafe import parse_xml


def num(text, default=None):
    """An ENI/ESI number: decimal, or hex written as #x1A00 (also 0x1A00)."""
    t = (text or "").strip()
    if not t:
        return default
    try:
        if t[:2] in ("#x", "#X", "0x", "0X"):
            return int(t[2:], 16)
        return int(t)
    except ValueError:
        return default


def flag(text) -> bool:
    return (text or "").strip().lower() in ("1", "true")


def _text(el, path, default=""):
    v = el.findtext(path)
    return v.strip() if v is not None else default


def parse_pdo(el, direction: str) -> dict:
    """A TxPdo/RxPdo element (same form in ENI and ESI)."""
    entries = []
    for e in el.findall("Entry"):
        index = num(e.findtext("Index"), 0)
        entries.append({"index": index, "subindex": num(e.findtext("SubIndex"), 0) if index else 0,
                        "name": _text(e, "Name"), "bits": num(e.findtext("BitLen"), 0),
                        "type": _text(e, "DataType")})
    sm = el.get("Sm")
    out = {"dir": direction, "index": num(el.findtext("Index"), 0),
           "name": (el.findtext("Name") or "").strip("\r\n\t"),     # spaces count: "Channel 1 Sample " + " 0"
           "sm": num(sm) if sm is not None else None, "fixed": flag(el.get("Fixed")),
           "mandatory": flag(el.get("Mandatory")),
           "excludes": [num(x.text, 0) for x in el.findall("Exclude")], "entries": entries}
    if el.get("OSFac") is not None:          # oversampling (ESI only)
        out["oversampling"] = {"factor": num(el.get("OSFac"), 1), "index_inc": num(el.get("OSIndexInc"), 1)}
    return out


def _slave(el, position: int) -> dict:
    info = el.find("Info")
    pd = el.find("ProcessData")
    pdos, sms = [], []
    if pd is not None:
        pdos = [parse_pdo(p, "tx") for p in pd.findall("TxPdo")] + [parse_pdo(p, "rx") for p in pd.findall("RxPdo")]
        for i in range(8):
            sm = pd.find(f"Sm{i}")
            if sm is not None:
                sms.append({"no": i, "type": _text(sm, "Type"), "start": num(sm.findtext("StartAddress"), 0),
                            "size": num(sm.findtext("DefaultSize"), 0), "control": num(sm.findtext("ControlByte"), 0),
                            "enable": flag(sm.findtext("Enable")), "pdos": [num(p.text, 0) for p in sm.findall("Pdo")]})
    mailbox = el.find("Mailbox")
    protocols, coe_init = [], []
    if mailbox is not None:
        protocols = [p.text.strip() for p in mailbox.findall("Protocol") if p.text]
        for c in mailbox.findall("CoE/InitCmds/InitCmd"):
            coe_init.append({"transitions": [t.text for t in c.findall("Transition")], "comment": _text(c, "Comment"),
                             "index": num(c.findtext("Index"), 0), "subindex": num(c.findtext("SubIndex"), 0),
                             "data": _text(c, "Data"), "complete_access": flag(c.get("CompleteAccess"))})
    dc = el.find("DC")
    dc_activate = None
    sm_control, fmmu, timeouts, check_high, check_low, sm_op_only = {}, [], {}, False, False, []
    for c in el.findall("InitCmds/InitCmd"):
        ado, comment = num(c.findtext("Ado")), (c.findtext("Comment") or "").strip()
        transitions = [t.text for t in c.findall("Transition")]
        data = (c.findtext("Data") or "").strip()
        if ado == 0x0980 and "PS" in transitions:
            dc_activate = int.from_bytes(bytes.fromhex(data), "little")
        elif comment.startswith("set sm ") and len(data) == 16:
            sm_control[int(comment.split()[2])] = int(data[8:10], 16)
            if "SO" in transitions:              # OpOnly sync manager: set up (active) in SafeOp->Op
                sm_op_only.append(int(comment.split()[2]))
        elif comment.startswith("set fmmu ") and "(" in comment:
            fmmu.append(comment.split("(", 1)[1].rstrip(")"))
        elif "high word" in comment:
            check_high = True
        elif "revision number (lo word)" in comment:
            check_low = True
        timeout = num(c.findtext("Validate/Timeout"))
        for key, match in (("back_to_init", ("check device state for INIT", "PI")),
                           ("preop", ("check device state for preop", "IP")),
                           ("safeop_op", ("check device state for SAFEOP", "PS")),
                           ("back_to_safeop", ("check device state for SAFEOP", "OS"))):
            if timeout is not None and comment == match[0] and match[1] in transitions:
                timeouts[key] = timeout
    mbx = None
    if mailbox is not None and mailbox.find("Send") is not None:
        def pair(path):
            return num(mailbox.findtext(path + "/Start"), 0), num(mailbox.findtext(path + "/Length"), 0)
        boot = mailbox.find("BootStrap")
        mbx = {"send": pair("Send"), "recv": pair("Recv"),
               "bootstrap": pair("BootStrap/Send") + pair("BootStrap/Recv") if boot is not None else None,
               "coe_profile": num(mailbox.findtext("CoE/Profile/ChannelInfo/ProfileNo")),
               "coe_timeout": num(mailbox.findtext("CoE/InitCmds/InitCmd/Timeout"), 2000),
               "pdo_assign": any(c["index"] in (0x1C12, 0x1C13) for c in coe_init)}
    return {
        "position": position,
        "name": _text(info, "Name"),
        "address": num(info.findtext("PhysAddr")),
        "autoinc": num(info.findtext("AutoIncAddr")),
        "vendor": num(info.findtext("VendorId")),
        "product": num(info.findtext("ProductCode")),
        "revision": num(info.findtext("RevisionNo")),
        "serial": num(info.findtext("SerialNo")),
        "pdos": pdos,
        "sm": sms,
        "mailbox": protocols,
        "coe_init": coe_init,
        "dc": {c.tag: (c.text or "").strip() for c in dc.iter() if c is not dc and not len(c)} if dc is not None else None,
        "dc_activate": dc_activate,
        "physics": _text(info, "Physics"),
        "harvest": {"sm_control": sm_control, "fmmu": fmmu, "timeouts": timeouts, "check_high": check_high,
                    "check_low": check_low, "sm_op_only": sm_op_only,
                    "mailbox": mbx,
                    # the EoE MAC the device got (I/O Engineering keeps it when the device moves): bytes 8..13
                    "eoe_mac": ((mailbox.findtext("EoE/InitCmds/InitCmd/Data") or "").strip()[16:28] or None)
                    if mailbox is not None else None},
        "variables": [],
    }


def parse_eni(data) -> dict:
    """The ENI as {"master": {...}, "slaves": [...], "inputs_bytes", "outputs_bytes"}; ValueError if it isn't one."""
    root = parse_xml(data)
    if root.tag != "EtherCATConfig":
        raise ValueError(f"not an ENI (root element {root.tag})")
    config = root.find("Config")
    if config is None:
        raise ValueError("ENI without Config")
    slaves = [_slave(s, i) for i, s in enumerate(config.findall("Slave"))]
    by_name = {s["name"]: s for s in slaves}
    out = {"master": {"name": _text(config, "Master/Info/Name")}, "slaves": slaves,
           "inputs_bytes": 0, "outputs_bytes": 0}
    image = config.find("ProcessImage")
    if image is not None:
        for direction, tag in (("input", "Inputs"), ("output", "Outputs")):
            part = image.find(tag)
            if part is None:
                continue
            out[f"{tag.lower()}_bytes"] = num(part.findtext("ByteSize"), 0)
            for v in part.findall("Variable"):
                name = _text(v, "Name")
                slave = by_name.get(name.split("/", 1)[0])
                if slave is not None:
                    slave["variables"].append({"name": name, "dir": direction, "type": _text(v, "DataType"),
                                               "bits": num(v.findtext("BitSize"), 0),
                                               "offset": num(v.findtext("BitOffs"), 0)})
    return out


def parse_ini(text: str) -> dict:
    """The master's ini as {section: {key: value}}."""
    out, section = {}, ""
    for line in text.splitlines():
        line = line.strip()
        if not line or line[0] in ";#":
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            out.setdefault(section, {})
        elif "=" in line:
            key, value = line.split("=", 1)
            out.setdefault(section, {})[key.strip()] = value.strip()
    return out

