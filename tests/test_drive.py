# SPDX-License-Identifier: MIT

"""SoE drive support against the ctrlX DRIVE XCS2-F/H that joined the X3's bus:  python3 -m unittest -q

Real: the drive's online info, EEPROM and topology (tests/data/x3-drive.json), its ESI and SoE dictionary
(tests/data/esi-drive). Synthetic: its SoE parameter values (it was never in PreOp on the X3 yet).
"""

import json
import os
import re
import tempfile
import unittest

from app import eni, soe
from app.configure import topology
from app.core import ConfigError, Core
from app.edits import EditError
from app.esi import EsiLibrary
from app.fake import DATA, MASTER, FakeBus
from tests.test_app import ESI_DIR, ESI_FILES, read

DRIVE_DIR = os.path.join(DATA, "esi-drive")
DICTIONARY = "BoschRexroth_ctrlX_DRIVE_XCS2_F_H_SoE_Dictionary.xml"
REFERENCE = read(os.path.join(DATA, "controller-eni.xml"))


class SoeTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.dictionary = soe.parse_dictionary(read(os.path.join(DRIVE_DIR, DICTIONARY)))

    def test_idns(self):
        self.assertEqual([soe.idn_text(x) for x in (134, 35017, 0x8000 | 0x1000 | 5)], ["S-0-0134", "P-0-2249", "P-1-0005"])
        self.assertEqual([soe.parse_idn(x) for x in ("S-0-0134", "p-0-2249", "0x86", 47)], [134, 35017, 134, 47])
        for bad in ("S-0-9999", "X-0-1", "S-0-0134x", 70000):
            with self.assertRaises(soe.SoeError):
                soe.parse_idn(bad)

    def test_dictionary(self):
        self.assertEqual(len(self.dictionary), 1857)
        p = self.dictionary[47]
        self.assertEqual((p["name"], p["mapping"], p["unit"], p["bits"]), ("Cyclic position control: Command value", "rt", "°", 32))
        self.assertEqual(self.dictionary[1]["unit"], "µs")                         # repaired from "┬Ás"
        a = soe.attribute(p["attribute"])
        self.assertEqual((a["size"], a["list"], a["display"], a["decimals"]), (4, False, "signed", 4))
        self.assertEqual(soe.attribute(self.dictionary[386]["attribute"])["write_protected"],
                         {"PreOp": True, "SafeOp": True, "Op": True})

    def test_values(self):
        lst = soe.attribute(self.dictionary[27]["attribute"])
        esi_data = bytes.fromhex("200020000802950063009400000000000000000000000000000000000000000000000000")
        self.assertEqual(soe.decode(esi_data, lst)[:4], ["S-0-0520", "S-0-0149", "S-0-0099", "S-0-0148"])
        self.assertEqual(soe.encode(["S-0-0520", "S-0-0149"], lst).hex().upper(), "0400040008029500")
        cycle = soe.attribute(self.dictionary[1]["attribute"])
        self.assertEqual((soe.decode(bytes.fromhex("E803"), cycle), soe.encode(2000, cycle).hex()), (1000, "d007"))
        pos = soe.attribute(self.dictionary[47]["attribute"])
        self.assertEqual(soe.decode(soe.encode("-12.5", pos), pos), -12.5)
        for bad in ("1.23456", "x", 2 ** 40):
            with self.assertRaises(soe.SoeError):
                soe.encode(bad, pos)
        with self.assertRaises(soe.SoeError):
            soe.encode(70000, cycle)


class DriveEsiTest(unittest.TestCase):

    def test_esi_and_dictionary_upload(self):
        with tempfile.TemporaryDirectory() as d:
            lib = EsiLibrary(d, log=lambda m: None)
            for name in sorted(os.listdir(DRIVE_DIR)):
                lib.add(name, read(os.path.join(DRIVE_DIR, name)))
            self.assertEqual(lib.summary(), {"files": 1, "devices": 1})
            match, entry = lib.find(0x24, 0x21005311, 0x3)               # the drive reports 0x3, the ESI 0x06000003
            self.assertEqual((match, entry["revision"]), ("nearest", 0x06000003))
            dev = lib.device(entry)
            self.assertEqual((dev["mailbox"], dev["dictionary"]), (["EoE", "FoE", "SoE"], DICTIONARY))
            self.assertEqual([c["idn"] for c in dev["soe_init"]], [1, 2, 32, 33, 34, 27, 329, 26, 328])
            self.assertEqual(dev["dc_modes"][0]["shift_sync0"], 60000)
            self.assertEqual([(x["name"], x["min"], x["max"], x["default"]) for x in dev["slots"]],
                             [("FSoE Option", 0, 1, 780784)])
            self.assertEqual(dev["modules"], {})          # its modules are in a separate module file: not built from modules
            self.assertEqual(len(lib.dictionary(DICTIONARY)), 1857)


class DriveBusTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.bus = FakeBus(drive=True)
        self.core = Core(os.path.join(self.dir.name, "active", "io-config", "config.json"), self.bus,
                         os.path.join(self.dir.name, "esi"), log=lambda m: None)
        for name in ESI_FILES:
            self.core.esi.add(name, read(os.path.join(ESI_DIR, name)))
        for name in sorted(os.listdir(DRIVE_DIR)):
            self.core.esi.add(name, read(os.path.join(DRIVE_DIR, name)))
        self.core.start_scan()
        self.core.wait_scan()
        for d in self.core.state()["configuration"]["unconfigured"]:
            self.core.project_add_from_bus(d["bus_pos"])

    def tearDown(self):
        self.core.stop()
        self.dir.cleanup()

    def drive_in_op(self):
        r = json.loads(json.dumps(self.bus.slaves[4]["online_info"]))
        r["response"]["alStatus"] = 8
        self.bus.slaves[4]["online_info"] = r

    def test_scan_and_topology(self):
        d = self.core.state()["devices"][4]
        self.assertEqual((d["name"], d["state"], d["sources"]["esi"]), ("ctrlX DRIVE XCS2-F/H (SoE)", "Init", "nearest"))
        self.assertEqual(topology(self.core.sources), {1: ("B", 0), 2: ("B", 1), 3: ("B", 2), 4: ("C", 0)})

    def test_same_file_as_io_engineering_with_the_drive(self):
        """The acceptance test: ctrlX I/O Engineering 4.6.1 configured this bus with the drive (defaults) on the X3
        (tests/data/x3-drive-ioe.xml); the app builds the same file from the scan and the ESI files."""
        p = self.core.plan()
        self.assertEqual(p["errors"], [])
        self.assertEqual(self.core.plan_file("eni"), read(os.path.join(DATA, "x3-drive-ioe.xml")))
        x = self.core.plan_file("eni").decode("latin-1")
        drive = x[x.index("<Name>ctrlX_DRIVE"):]
        self.assertIn("<PreviousPort Selected=\"1\"><Port>C</Port><PhysAddr>1001</PhysAddr>", drive)
        idns = re.findall(r"<IDN>(\d+)</IDN><Elements>64</Elements><Attribute>0</Attribute><Data>([0-9A-F]+)</Data>", drive)
        self.assertEqual(idns[:5], [("15", "0700"), ("24", "0C000C0091002F0024005000C988CA88"),
                                    ("16", "0C000C009000820117025400CB888601"), ("1", "D007"), ("2", "D007")])
        new = eni.parse_eni(self.core.plan_file("eni"))
        old = eni.parse_eni(REFERENCE)
        for a, b in zip(new["slaves"][:4], old["slaves"][:4]):        # the I/O part stays as it was
            self.assertEqual((a["name"], a["pdos"], a["sm"], a["coe_init"], a["variables"]),
                             (b["name"], b["pdos"], b["sm"], b["coe_init"], b["variables"]))
        self.assertEqual([s["sm"] for s in new["slaves"][4:]][0][-2:],
                         [{"no": 2, "type": "Outputs", "start": 0x1600, "size": 22, "control": 0x64, "enable": True,
                           "pdos": [0x18]},
                          {"no": 3, "type": "Inputs", "start": 0x1A00, "size": 20, "control": 0x22, "enable": True,
                           "pdos": [0x10]}])

    def test_running_configuration_with_the_drive_is_identical(self):
        """What the app sees on the X3 now: the master runs I/O Engineering's file with the drive."""
        ref = read(os.path.join(DATA, "x3-drive-ioe.xml"))
        self.bus.nodes[MASTER + "admin/fieldbus_config"] = {"response": {
            "eni": list(ref), "ini": list(read(os.path.join(DATA, "controller-ini.txt")))}}
        self.core.start_scan()
        self.core.wait_scan()
        d = self.core.state()["devices"][4]
        self.assertEqual((d["name"], d["sources"]["eni"]), ("ctrlX_DRIVE_XCS2_F_H_SoE_", "revision"))   # 0x06000003 vs 0x3
        p = self.core.plan()
        self.assertEqual((p["identical"], p["errors"]), (True, []))

    def test_changed_telegrams_same_file_as_io_engineering(self):
        """Acceptance test for edited telegrams: in I/O Engineering 4.6.1 the user added S-0-0041 to the MDT (before
        P-0-2250), removed S-0-0084 from the AT and added S-0-0011 at its end (tests/data/x3-drive-ioe-telegram.xml,
        read from the X3). Entries that were in the telegram keep the ESI's name and type, added ones come from the
        SoE dictionary."""
        o = self.core.edit_options(4)["soe"]
        mdt, at = [x["idn"] for x in o["mdt"]], [x["idn"] for x in o["at"]]
        mdt.insert(mdt.index(0x8000 + 2250), 41)
        at.remove(84)
        self.core.save_edit(4, {"soe_mdt": mdt, "soe_at": at + [11]})
        p = self.core.plan()
        self.assertEqual(p["errors"], [])
        self.assertEqual(self.core.plan_file("eni"), read(os.path.join(DATA, "x3-drive-ioe-telegram.xml")))
        self.core.start_download(p["id"])
        self.core.wait_scan()
        self.core.wait_scan()
        self.assertIsNone(self.core.state()["download"]["error"])
        self.assertEqual(len(self.bus.written), 1)

    def test_telegram_editing(self):
        o = self.core.edit_options(4)
        self.assertEqual([x["text"] for x in o["soe"]["at"]][:2], ["S-0-0135", "S-0-0144"])
        self.core.save_edit(4, {"soe_at": [x["idn"] for x in o["soe"]["at"]] + [51], "name": "Drive1"})
        s = eni.parse_eni(self.core.plan() and self.core.plan_file("eni"))["slaves"][4]
        self.assertEqual(s["name"], "Drive1")
        self.assertEqual(s["sm"][-1]["size"], 24)
        self.assertIn("Drive1/AT.Encoder_actual_values_ENC_1_Position", [v["name"] for v in s["variables"]])
        for bad, text in (([386, 135], "must start with S-0-0135"), ([135, 35017], "not mappable into the AT"),
                          ([135, 386, 386], "appears twice")):
            with self.assertRaises(EditError) as cm:
                self.core.save_edit(4, {"soe_at": bad})
            self.assertIn(text, str(cm.exception))

    def test_soe_parameters(self):
        self.assertEqual(self.core.soe_parameters(4, "position control: command value", "r")["parameters"][0]["text"], "S-0-0047")
        with self.assertRaises(Exception) as cm:
            self.core.soe_read(4, "S-0-0047")                        # unconfigured: the drive is in Init
        self.assertIn("Device not found", str(cm.exception))
        with self.assertRaises(ConfigError):
            self.core.soe_read(2, 1)                                 # XI422202 has no SoE
        p = self.core.plan()
        self.core.start_download(p["id"])
        self.core.wait_scan()
        self.core.wait_scan()
        self.drive_in_op()
        self.assertEqual(self.core.soe_read(4, "S-0-0047")["unit"], "°")
        self.assertEqual(self.core.soe_read(4, "S-0-0095")["value"], "A0006 Safe-Operational")       # read on the X3
        self.assertEqual(self.core.soe_read(4, "S-0-0016")["value"],
                         ["S-0-0144", "S-0-0386", "S-0-0535", "S-0-0084", "P-0-2251", "S-0-0390"])
        self.assertEqual(self.core.soe_read(4, "S-0-0386")["value"], 30929.3222)
        self.assertEqual(self.core.soe_write(4, "P-0-2249", "123.4567")["value"], 123.4567)
        with self.assertRaises(ConfigError) as cm:
            self.core.soe_write(4, "S-0-0032", 3)                    # write-protected in Op
        self.assertIn("startup parameter", str(cm.exception))
        entry = self.core.soe_startup_entry(4, "S-0-0032", "0x0003")
        self.assertEqual(entry["data"], "0300")
        self.core.save_edit(4, {"startup": [entry]})
        x = self.core.plan() and self.core.plan_file("eni").decode("latin-1")
        self.assertIn("<IDN>32</IDN><Elements>64</Elements><Attribute>0</Attribute><Data>0300</Data>", x)
        with self.assertRaises(ConfigError):
            self.core.soe_startup_entry(4, "S-0-0386", 1)            # write-protected in PreOp


if __name__ == "__main__":
    unittest.main()
