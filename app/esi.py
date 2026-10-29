# SPDX-License-Identifier: MIT

"""The ESI library: EtherCAT device description files (EtherCATInfo XML) the user uploads.

Files are kept as uploaded in one folder; index.json lists every device in them (vendor, product code,
revision, name, group, file), so a lookup never parses all files. A device's details are parsed from its
file on demand. Uploads can be single XML files or zips; files that aren't ESI (IO-Link IODD, SoE
dictionaries, ...) are skipped with the reason.
"""

import io
import json
import os
import re
import threading
import zipfile

from app.eni import flag, num, parse_pdo
from app.xmlsafe import XmlError, parse_xml, root_tag

MAX_ZIP_FILES = 2000
MAX_UNZIPPED = 512 * 1024 * 1024

# root elements of XML files that are not ESI device descriptions, and why they are skipped
NOT_ESI = {
    "IODevice": "IO-Link device description (IODD)",
    "ExternalTextDocument": "IO-Link IODD texts",
    "EtherCATModule": "module description (not used yet)",
    "EtherCATDiag": "diagnosis texts (not used yet)",
    "safety_device_facet": "safety description (not used yet)",
    "DeviceDescription": "CODESYS device description",
    "DeviceCacheDescription": "CODESYS device description",
}


def _name(el, default=""):
    """The English name (LcId 1033) of an element with localized <Name> children."""
    names = el.findall("Name")
    for n in names:
        if n.get("LcId") in (None, "1033"):
            return (n.text or "").strip()
    return (names[0].text or "").strip() if names else default


def _safe_filename(name: str) -> str:
    base = os.path.basename(name.replace("\\", "/"))
    base = re.sub(r"[^A-Za-z0-9._ +-]", "_", base).strip(" .")
    if not base:
        raise ValueError("invalid file name")
    return base


def _infos(root):
    """The EtherCATInfo elements of a file (an EtherCATInfoList holds several)."""
    if root.tag == "EtherCATInfo":
        return [root]
    if root.tag == "EtherCATInfoList":
        return root.findall("EtherCATInfo")
    return []


def _device_key(dev):
    t = dev.find("Type")
    if t is None:
        return None
    return num(t.get("ProductCode")), num(t.get("RevisionNo"))


def index_file(path: str) -> list:
    """The index entries of one ESI file; XmlError/ValueError if it isn't one."""
    with open(path, "rb") as f:
        root = parse_xml(f.read())
    infos = _infos(root)
    if not infos:
        raise ValueError(NOT_ESI.get(root.tag, f"not an ESI file (root element {root.tag})"))
    out = []
    for info in infos:
        vendor = num(info.findtext("Vendor/Id"))
        vendor_name = _name(info.find("Vendor")) if info.find("Vendor") is not None else ""
        groups = {(g.findtext("Type") or "").strip(): _name(g) for g in info.findall("Descriptions/Groups/Group")}
        for dev in info.findall("Descriptions/Devices/Device"):
            key = _device_key(dev)
            if key is None or key[0] is None:
                continue
            group = (dev.findtext("GroupType") or "").strip()
            out.append({"vendor": vendor, "vendor_name": vendor_name, "product": key[0],
                        "revision": key[1] if key[1] is not None else 0,
                        "type": (dev.findtext("Type") or "").strip(), "name": _name(dev),
                        "group": groups.get(group, group), "file": os.path.basename(path)})
    if not out:
        raise ValueError("ESI file without devices")
    return out


def parse_device(path: str, vendor: int, product: int, revision: int) -> dict:
    """One device's details from its ESI file, in the shape the ENI parser and the EEPROM decoder give."""
    with open(path, "rb") as f:
        root = parse_xml(f.read())
    for info in _infos(root):
        if num(info.findtext("Vendor/Id")) != vendor:
            continue
        for dev in info.findall("Descriptions/Devices/Device"):
            if _device_key(dev) != (product, revision):
                continue
            mailbox = dev.find("Mailbox")
            protocols, init = [], []
            if mailbox is not None:
                protocols = [c.tag for c in mailbox if c.tag in ("AoE", "EoE", "CoE", "FoE", "SoE", "VoE")]
                for c in mailbox.findall("CoE/InitCmd"):
                    init.append({"transitions": [t.text for t in c.findall("Transition")],
                                 "comment": (c.findtext("Comment") or "").strip(),
                                 "index": num(c.findtext("Index"), 0), "subindex": num(c.findtext("SubIndex"), 0),
                                 "data": (c.findtext("Data") or "").strip(),
                                 "complete_access": flag(c.get("CompleteAccess"))})
            sms = []
            for i, sm in enumerate(dev.findall("Sm")):
                sms.append({"no": i, "type": (sm.text or "").strip(), "start": num(sm.get("StartAddress"), 0),
                            "size": num(sm.get("DefaultSize"), 0), "control": num(sm.get("ControlByte"), 0),
                            "enable": flag(sm.get("Enable")), "op_only": flag(sm.get("OpOnly"))})
            dc_modes = [{"name": (m.findtext("Name") or "").strip(), "desc": (m.findtext("Desc") or "").strip(),
                         "assign_activate": num(m.findtext("AssignActivate"), 0),
                         "shift_sync0": num(m.findtext("ShiftTimeSync0"), 0)} for m in dev.findall("Dc/OpMode")]
            soe_init = [{"transitions": [t.text for t in c.findall("Transition")], "idn": num(c.findtext("IDN"), 0),
                         "data": (c.findtext("Data") or "").strip().upper(), "comment": (c.findtext("Comment") or "").strip(),
                         "drive": num(c.get("Chn"), 0)} for c in dev.findall("Mailbox/SoE/InitCmd")]
            coe = mailbox.find("CoE") if mailbox is not None else None
            sm_timeouts = dev.find("Info/StateMachine/Timeout")
            timeouts = {key: num(sm_timeouts.findtext(tag), default) if sm_timeouts is not None else default
                        for key, tag, default in (("preop", "PreopTimeout", 3000), ("safeop_op", "SafeopOpTimeout", 10000),
                                                  ("back_to_init", "BackToInitTimeout", 5000),
                                                  ("back_to_safeop", "BackToSafeopTimeout", 200))}
            bootstrap = (dev.findtext("Eeprom/BootStrap") or "").strip()
            return {
                "vendor": vendor, "product": product, "revision": revision,
                "type": (dev.findtext("Type") or "").strip(), "name": _name(dev),
                "group": (dev.findtext("GroupType") or "").strip(),
                "physics": dev.get("Physics") or "",
                "check_revision": dev.find("Type").get("CheckRevisionNo") or "",
                "timeouts": timeouts,
                "mailbox_timeout": num(dev.findtext("Info/Mailbox/Timeout/ResponseTimeout"), 2000),
                "fmmu": [(f.text or "").strip() for f in dev.findall("Fmmu")],
                "fmmu_op_only": [flag(f.get("OpOnly")) for f in dev.findall("Fmmu")],
                "bootstrap": bytes.fromhex(bootstrap) if len(bootstrap) == 16 else None,
                "pdo_assign": coe is not None and flag(coe.get("PdoAssign")),
                "profile_no": num(dev.findtext("Profile/ProfileNo")),
                "dictionary": (dev.findtext("Profile/DictionaryFile") or "").strip() or None,
                "esc": {r.tag[3:]: num(r.text) for r in (dev.find("ESC") if dev.find("ESC") is not None else [])
                        if r.tag.startswith("Reg") and num(r.text) is not None},
                "soe_init": soe_init,
                "slots": [{"name": (s.findtext("Name") or "").strip(), "min": num(s.get("MinInstances"), 0),
                           "max": num(s.get("MaxInstances"), 1)} for s in dev.findall("Slots/Slot")],
                "sm": sms,
                "pdos": [parse_pdo(p, "tx") for p in dev.findall("TxPdo")] +
                        [parse_pdo(p, "rx") for p in dev.findall("RxPdo")],
                "mailbox": protocols,
                "coe_init": init,
                "dc_modes": dc_modes,
                "modular": dev.find("Slots") is not None or dev.find("Modules") is not None,
            }
    raise KeyError(f"device {vendor:#x}/{product:#x}/{revision:#x} not in {os.path.basename(path)}")


class EsiLibrary:

    def __init__(self, folder: str, log=print):
        self.folder = folder
        self.log = log
        self.lock = threading.RLock()
        self._cache = {}             # (file, vendor, product, revision) -> parsed device
        self._dict_cache = {}        # dictionary file -> parsed dictionary
        os.makedirs(folder, exist_ok=True)
        self.entries = self._load_index()

    # --- index ---------------------------------------------------------------------------------

    @property
    def _index_path(self):
        return os.path.join(self.folder, "index.json")

    def _load_index(self) -> list:
        try:
            with open(self._index_path, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return self._rebuild()
        except (ValueError, OSError) as e:
            self.log(f"ESI index unreadable ({e}), rebuilding it")
            return self._rebuild()

    def _rebuild(self) -> list:
        entries = []
        for name in sorted(self.files()):
            try:
                entries += index_file(os.path.join(self.folder, name))
            except (XmlError, ValueError, OSError) as e:
                self.log(f"ESI {name}: {e}")
        self._save(entries)
        return entries

    def _save(self, entries):
        tmp = self._index_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(entries, f)
        os.replace(tmp, self._index_path)

    def files(self) -> list:
        return [n for n in os.listdir(self.folder) if n.lower().endswith(".xml")]

    # --- upload / delete -----------------------------------------------------------------------

    def add(self, filename: str, data: bytes) -> dict:
        """Adds an uploaded XML or zip; returns {"added": [...], "skipped": [...]} (file names, reasons)."""
        if data[:4] == b"PK\x03\x04":
            return self._add_zip(data)
        result = {"added": [], "skipped": []}
        self._add_xml(filename, data, result)
        return result

    def _add_zip(self, data: bytes) -> dict:
        result = {"added": [], "skipped": []}
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as e:
            raise ValueError(f"not a valid zip: {e}") from e
        members = [m for m in zf.infolist() if not m.is_dir()]
        if len(members) > MAX_ZIP_FILES or sum(m.file_size for m in members) > MAX_UNZIPPED:
            raise ValueError("zip too large")
        for m in members:
            if not m.filename.lower().endswith(".xml"):
                result["skipped"].append({"file": m.filename, "reason": "not an XML file"})
                continue
            self._add_xml(m.filename, zf.read(m), result)
        return result

    def _add_xml(self, filename: str, data: bytes, result: dict):
        try:
            name = _safe_filename(filename)
        except ValueError as e:
            result["skipped"].append({"file": filename, "reason": str(e)})
            return
        if not name.lower().endswith(".xml"):
            name += ".xml"
        with self.lock:
            tmp = os.path.join(self.folder, name + ".upload")
            with open(tmp, "wb") as f:
                f.write(data)
            try:
                tag = root_tag(tmp)
                if tag in NOT_ESI:
                    raise ValueError(NOT_ESI[tag])
                if tag == "EtherCATDict":        # an SoE/CoE dictionary that ESI files refer to by name
                    os.replace(tmp, os.path.join(self.folder, name))
                    self._dict_cache.pop(name, None)
                    result["added"].append({"file": name, "devices": 0, "dictionary": True})
                    return
                entries = index_file(tmp)
            except (XmlError, ValueError) as e:
                os.unlink(tmp)
                result["skipped"].append({"file": filename, "reason": str(e)})
                return
            path = os.path.join(self.folder, name)
            os.replace(tmp, path)
            for e in entries:
                e["file"] = name
            self.entries = [e for e in self.entries if e["file"] != name] + entries
            self._cache = {k: v for k, v in self._cache.items() if k[0] != name}
            self._save(self.entries)
        result["added"].append({"file": name, "devices": len(entries)})

    def delete(self, filename: str):
        name = _safe_filename(filename)
        with self.lock:
            path = os.path.join(self.folder, name)
            if name not in self.files():
                raise KeyError(f"no ESI file {name}")
            os.unlink(path)
            self.entries = [e for e in self.entries if e["file"] != name]
            self._cache = {k: v for k, v in self._cache.items() if k[0] != name}
            self._save(self.entries)

    # --- lookup --------------------------------------------------------------------------------

    def search(self, text: str = "", limit: int = 300) -> list:
        words = text.lower().split()
        out = []
        for e in self.entries:
            hay = f'{e["type"]} {e["name"]} {e["group"]} {e["vendor_name"]} {e["file"]} {e["product"]:#x}'.lower()
            if all(w in hay for w in words):
                out.append(e)
                if len(out) >= limit:
                    break
        return out

    def find(self, vendor: int, product: int, revision: int):
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

    def device(self, entry: dict) -> dict:
        key = (entry["file"], entry["vendor"], entry["product"], entry["revision"])
        with self.lock:
            if key not in self._cache:
                if len(self._cache) > 64:
                    self._cache.clear()
                self._cache[key] = parse_device(os.path.join(self.folder, entry["file"]), *key[1:])
            return self._cache[key]

    def dictionary(self, filename: str) -> dict:
        """A device dictionary (EtherCATDict) the library holds, parsed; KeyError if it isn't there."""
        from app.soe import parse_dictionary
        name = _safe_filename(filename)
        with self.lock:
            if name not in self._dict_cache:
                path = os.path.join(self.folder, name)
                if not os.path.isfile(path):
                    raise KeyError(f"dictionary {name} is not in the ESI library (upload it)")
                with open(path, "rb") as f:
                    self._dict_cache = {name: parse_dictionary(f.read())}      # one at a time: they are large
            return self._dict_cache[name]

    def summary(self) -> dict:
        return {"files": len({e["file"] for e in self.entries}), "devices": len(self.entries)}
