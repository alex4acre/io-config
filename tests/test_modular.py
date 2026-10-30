# SPDX-License-Identifier: MIT

"""Modular devices (ETG.5001):  python3 -m unittest -q

The Elmo Titanium (vendor #x9A, product #x01280002, tests/data/esi-elmo) joined the X3's bus on 2026-10-30
behind a second XB-EC-12. Its ESI has 5 slots (Axis1_Slot1/2, Axis2_Slot1/2, CyclicRecorderSlot) and 23
modules; the device-level PDOs (#x1701.. / #x1B01..) are empty. Read on the device: #xF030 = #xF050 =
[#x288110, #x100, #x100], #xF000:01 module object distance #x800, #x1600/#x1A00 empty until the master
writes them. No I/O Engineering reference yet: the commands follow ETG.5001 / ETG.2000.
"""

import os
import re
import tempfile
import unittest

from app import configure, edits, eni, esi, generate, modules
from app.core import Core
from app.fake import DATA, FakeBus
from tests.test_app import read
from tests.test_drive import ESI_DIR, ESI_FILES

ESI_FILE = os.path.join(DATA, "esi-elmo", "ElmoTitanium_28_V220030_S.xml")
REFERENCE = eni.parse_eni(read(os.path.join(DATA, "controller-eni.xml")))
VENDOR, PRODUCT, REVISION = 0x9A, 0x01280002, 0x00220031


def source(esi_device, slave=None, pos=4):
    return {"pos": pos, "online": {"response": {"slaveIdentity": {"vendorId": VENDOR, "productCode": PRODUCT,
                                                                   "revisionNumber": REVISION}}},
            "eeprom": None, "eni_slave": slave, "esi_match": "nearest", "esi_device": esi_device}


def cmds(xml: str, position: int) -> list:
    """(comment, index, subindex, data, complete access) of a slave's CoE startup commands."""
    slave = re.findall(r"<Slave>.*?</Slave>", xml, re.S)[position]
    coe = re.search(r"<CoE>.*?</CoE>", slave, re.S).group(0)
    return [(re.search(r"<Comment>(.*?)</Comment>", c).group(1), int(re.search(r"<Index>(\d+)</Index>", c).group(1)),
             int(re.search(r"<SubIndex>(\d+)</SubIndex>", c).group(1)), re.search(r"<Data>(\w*)</Data>", c).group(1),
             'CompleteAccess="1"' in c) for c in re.findall(r"<InitCmd[^>]*>.*?</InitCmd>", coe, re.S)]


class ModularTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.esi = esi.parse_device(ESI_FILE, VENDOR, PRODUCT, 0x00220030)
        cls.io = [generate.device_from_eni(s) for s in REFERENCE["slaves"]]

    def build(self, dev):
        x = generate.generate_eni(self.io + [dev], "ethercatmaster", 2000)
        return x, x.decode("latin-1"), eni.parse_eni(x)

    def test_esi(self):
        self.assertTrue(modules.usable(self.esi))
        self.assertEqual([(s["name"], s["group"], s["default"]) for s in self.esi["slots"]],
                         [("Axis1_Slot1", 0, 0x288110), ("Axis1_Slot2", 0, 0x100), ("Axis2_Slot1", 1, 0x100),
                          ("Axis2_Slot2", 1, 0), ("CyclicRecorderSlot", 1, 0)])
        self.assertEqual(self.esi["slot_increments"], {"pdo": 0, "index": 0, "group_pdo": 16, "group_index": 0x800})
        self.assertTrue(self.esi["download_module_list"] and self.esi["pdo_config"] and self.esi["complete_access"])
        self.assertEqual(modules.choices(self.esi, self.esi["slots"][1]), [0, 0x100, 0x200, 0x300, 0x1100])

    def test_default_modules(self):
        dev = generate.device_from_esi(self.esi, "Titan")
        self.assertEqual(dev["modules"]["idents"], [0x288110, 0x100, 0x100, 0, 0])
        pdos = {p["index"]: p for p in dev["pdos"]}
        self.assertEqual(sorted(pdos), [0x1600, 0x1610, 0x1700, 0x1A00, 0x1A10, 0x1B00])
        self.assertEqual([(e["index"], e["bits"]) for e in pdos[0x1610]["entries"]], [(0x6840, 16), (0x687A, 32)])
        self.assertEqual([(e["index"], e["bits"]) for e in pdos[0x1A00]["entries"]], [(0x6041, 16), (0x6064, 32)])
        self.assertEqual(pdos[0x1610]["name"], "Axis2_Slot1 Cyclic Position Mode Outputs")
        self.assertEqual([(c["index"], c["data"]) for c in dev["mailbox"]["coe"]["init"]], [(0x6060, "08"), (0x6860, "08")])

    def test_eni_commands(self):
        _x, xml, parsed = self.build(generate.device_from_esi(self.esi, "Titan"))
        c = cmds(xml, len(self.io))
        self.assertEqual(c[0], ("download module ident list (0xf030)", 0xF030, 0, "0300" + "10812800" + "00010000" * 2, True))
        self.assertEqual(c[1:3], [("clear sm pdos (0x1c12)", 0x1C12, 0, "00", False),
                                  ("clear sm pdos (0x1c13)", 0x1C13, 0, "00", False)])
        mapping = {x[1]: x for x in c if x[0].endswith(" entries")}
        self.assertEqual(mapping[0x1600][3:], ("0200" + "10004060" + "20007A60", True))
        self.assertEqual(mapping[0x1A10][3:], ("0200" + "10004168" + "20006468", True))
        assign = [x for x in c if x[0].startswith("download pdo 0x1c1")]
        self.assertEqual([(x[1], x[3]) for x in assign], [(0x1C12, "0300" + "0017" + "0016" + "1016"),
                                                          (0x1C13, "0300" + "001B" + "001A" + "101A")])
        self.assertEqual([(x[1], x[3]) for x in c[-2:]], [(0x6060, "08"), (0x6860, "08")])
        slave = parsed["slaves"][-1]
        names = [v["name"] for v in slave["variables"]]
        self.assertIn("Titan/Axis1_Slot2_Cyclic_Position_Mode_Outputs.Controlword", names)
        self.assertIn("Titan/Axis2_Slot1_Cyclic_Position_Mode_Inputs.Position_actual_value", names)
        self.assertEqual(len(names), len(set(names)))

    def test_same_file_again(self):
        """The running configuration read back (modules from its #xF030) gives the same file."""
        x, _xml, parsed = self.build(generate.device_from_esi(self.esi, "Titan"))
        slave = parsed["slaves"][-1]
        dev, _note, _warn, err = configure.describe(source(self.esi, slave, len(self.io)))
        self.assertIsNone(err)
        self.assertEqual(self.build(dev)[0], x)

    def test_configuration_without_modules(self):
        """io-config before 1.5.0 configured the empty device-level PDOs: the next build takes the default modules."""
        old = dict(generate.device_from_esi(self.esi, "Titan"))
        old.pop("modules"), old.pop("module_esi")
        general = {p["index"]: p for p in self.esi["pdos"]}
        old["pdos"] = [dict(general[0x1702], sm=2), dict(general[0x1B01], sm=3)]
        old["mailbox"] = dict(old["mailbox"], coe=dict(old["mailbox"]["coe"], init=[]))
        _x, _xml, parsed = self.build(old)
        slave = parsed["slaves"][-1]
        self.assertEqual(slave["variables"], [])
        dev, _n, _w, _e = configure.describe(source(self.esi, slave, len(self.io)))
        self.assertEqual(dev["name"], "Titan")
        self.assertEqual(dev["modules"]["idents"], [0x288110, 0x100, 0x100, 0, 0])
        self.assertEqual(sorted(p["index"] for p in dev["pdos"]), [0x1600, 0x1610, 0x1700, 0x1A00, 0x1A10, 0x1B00])

    def test_edit_modules_and_dc(self):
        src = source(self.esi)
        edit = edits.validate(src, {"modules": [0, 0x100, 0, 0, 0], "dc_mode": "DC Off"})
        dev = configure.device_for(src, {src["pos"]: edit})
        self.assertEqual(sorted(p["index"] for p in dev["pdos"]), [0x1600, 0x1A00])
        self.assertIsNone(dev["dc"])
        _x, xml, _p = self.build(dev)
        c = cmds(xml, len(self.io))
        self.assertEqual(c[0][3], "0200" + "00000000" + "00010000")
        self.assertEqual([(x[1], x[3]) for x in c if 0x6000 <= x[1] < 0xF000], [(0x6060, "08")])
        slave = re.findall(r"<Slave>.*?</Slave>", xml, re.S)[-1]
        self.assertNotIn("set DC activation", slave)
        opts = edits.options(src, dev, edit)
        self.assertEqual([m["selected"] for m in opts["modules"]], [0, 0x100, 0, 0, 0])
        self.assertEqual(opts["dc_mode"], "DC Off")
        velocity = next(c for c in opts["modules"][2]["choices"] if c["ident"] == 0x200)      # what the UI previews
        self.assertEqual([(p["index"], p["bits"]) for p in velocity["pdos"]], [(0x1A10, 48), (0x1610, 48)])
        self.assertEqual(velocity["pdos"][1]["entries"][1]["index"], 0x68FF)
        self.assertFalse(opts["editable"])
        with self.assertRaises(edits.EditError):
            edits.validate(src, {"modules": [0x100, 0x288110, 0, 0, 0]})
        with self.assertRaises(edits.EditError):
            edits.validate(src, {"dc_mode": "nonsense"})

    def test_pdo_choice_from_before_modules(self):
        """io-config 1.4.3 saved a PDO choice for the Elmo (#x1702/#x1B01, empty): ignored, dropped on the next save."""
        src = source(self.esi)
        old = {"identity": [VENDOR, PRODUCT, REVISION], "pdos": [0x1702, 0x1B01]}
        dev = configure.device_for(src, {src["pos"]: old})
        self.assertEqual(len(dev["pdos"]), 6)
        self.assertNotIn("pdos", edits.validate(src, {"pdos": [0x1702, 0x1B01], "name": "Elmo1"}))

    def test_startup_parameter_on_top(self):
        src = source(self.esi)
        edit = edits.validate(src, {"startup": [{"index": 0x6065, "subindex": 0, "data": "E8030000"}]})
        dev = configure.device_for(src, {src["pos"]: edit})
        self.assertEqual([(c["index"], c["data"]) for c in dev["mailbox"]["coe"]["init"]],
                         [(0x6060, "08"), (0x6860, "08"), (0x6065, "E8030000")])


class ModularBusTest(unittest.TestCase):
    """The simulated X3 with the Elmo at port C of the coupler (not configured, Init)."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.bus = FakeBus(elmo=True)
        self.core = Core(os.path.join(self.dir.name, "active", "io-config", "config.json"), self.bus,
                         os.path.join(self.dir.name, "esi"), log=lambda m: None)
        for name in ESI_FILES:
            self.core.esi.add(name, read(os.path.join(ESI_DIR, name)))
        self.core.esi.add(os.path.basename(ESI_FILE), read(ESI_FILE))
        self.core.start_scan()
        self.core.wait_scan()

    def tearDown(self):
        self.core.stop()
        self.dir.cleanup()

    def download(self):
        p = self.core.plan()
        self.assertEqual(p["errors"], [])
        self.core.start_download(p["id"])
        self.core.wait_scan()
        self.core.wait_scan()
        self.assertIsNone(self.core.state()["download"]["error"])

    def test_build_write_and_build_again(self):
        d = self.core.state()["devices"][4]
        self.assertEqual((d["vendor"], d["sources"]["esi"]), (VENDOR, "nearest"))
        self.download()
        d = self.core.state()["devices"][4]
        texts = [f["text"] for f in self.core.device(4)["findings"]]
        self.assertIn("Modules: Axis1_Slot1 = Safety Module Basic PDO, Axis1_Slot2 = Cyclic Position Mode, "
                      "Axis2_Slot1 = Cyclic Position Mode, Axis2_Slot2 = Empty, CyclicRecorderSlot = Empty", texts)
        self.assertFalse([t for t in texts if "not in the ESI" in t])
        self.assertTrue(self.core.plan()["identical"])

    def test_edit_through_the_api(self):
        o = self.core.edit_options(4)
        self.assertEqual([m["slot"] for m in o["modules"]][:2], ["Axis1_Slot1", "Axis1_Slot2"])
        self.assertEqual(o["dc_mode"], "DC Sync")
        o = self.core.save_edit(4, {"modules": [0, 0x200, 0, 0, 0], "name": "Elmo1"})
        self.assertEqual([p["index"] for p in o["pdos"]], [0x1A00, 0x1600])
        self.download()
        s = eni.parse_eni(bytes(self.bus.written[-1][0]))["slaves"][4]
        self.assertEqual(s["name"], "Elmo1")
        self.assertIn("Elmo1/Axis1_Slot2_Cyclic_Velocity_Mode_Outputs.Target_velocity", [v["name"] for v in s["variables"]])
        self.assertTrue(self.core.plan()["identical"])


if __name__ == "__main__":
    unittest.main()
