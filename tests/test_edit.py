# SPDX-License-Identifier: MIT

"""PDO selection, startup parameters, CoE objects and process data, against the recorded X3 bus:
python3 -m unittest -q (the object dictionaries in tests/data/x3-coe.json were read from the real modules)."""

import json
import os
import tempfile
import unittest

from app import coe, eni
from app.core import ConfigError, Core
from app.edits import EditError
from app.fake import MASTER, FakeBus
from tests.test_app import ESI_DIR, ESI_FILES, read


class EditTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.bus = FakeBus()
        self.core = Core(os.path.join(self.dir.name, "active", "io-config", "config.json"), self.bus,
                         os.path.join(self.dir.name, "esi"), log=lambda m: None)
        for name in ESI_FILES:
            self.core.esi.add(name, read(os.path.join(ESI_DIR, name)))
        self.core.start_scan()
        self.core.wait_scan()

    def tearDown(self):
        self.core.stop()
        self.dir.cleanup()

    def built(self, pos=2):
        self.core.plan()
        return eni.parse_eni(self.core.plan_file("eni"))["slaves"][pos]

    def test_options(self):
        o = self.core.edit_options(2)
        self.assertEqual((o["name"], o["editable"], o["coe"], o["pdo_assign"]), ("XI422202", True, True, True))
        self.assertEqual(o["selected"], o["default"])
        self.assertNotIn(0x1A03, o["default"])
        o = self.core.edit_options(1)
        self.assertFalse(o["editable"])          # XI190202: no CoE, PDOs fixed
        self.assertFalse(o["coe"])

    def test_pdo_selection_reaches_the_eni(self):
        self.core.save_edit(2, {"pdos": [0x1600, 0x1601, 0x1A03]})
        s = self.built()
        self.assertEqual([sm["pdos"] for sm in s["sm"]], [[0x1600, 0x1601], [0x1A03]])
        self.assertEqual([c["data"] for c in s["coe_init"]], ["020000160116", "0100031A"])
        self.assertEqual(len([v for v in s["variables"] if v["dir"] == "input"]), 6)
        self.assertEqual(self.core.load_edits()[2]["identity"], [36, 0x242A1F, 0x100])

    def test_refused_selections(self):
        for bad, text in (([0x1600, 0x1A00, 0x1A03], "exclude each other"), ([0x9999], "not offered"),
                          ("x", "list of PDO indexes")):
            with self.assertRaises(EditError) as cm:
                self.core.save_edit(2, {"pdos": bad})
            self.assertIn(text, str(cm.exception))
        with self.assertRaises(EditError):
            self.core.save_edit(1, {"pdos": [0x1A00]})                     # fixed device
        with self.assertRaises(EditError):
            self.core.save_edit(0, {"pdos": [0x1A02]})                     # 0x1A00/0x1A01 are mandatory
        self.assertEqual(self.core.load_edits(), {})

    def test_rename_and_reset(self):
        self.core.save_edit(1, {"name": "Inputs_A"})
        self.assertEqual(self.built(1)["name"], "Inputs_A")
        p = self.core.plan_summary()
        self.assertIn("Inputs_A/Channel_1_Sample_0.Value_0", p["variables"]["added"])
        with self.assertRaises(EditError):
            self.core.save_edit(1, {"name": "1 bad"})
        self.core.reset_edit(1)
        self.assertTrue(self.core.plan()["identical"])

    def test_edit_for_another_device_is_not_used(self):
        self.core.save_edit(2, {"name": "AO"})
        edits = self.core.load_edits()
        edits[2]["identity"][1] = 0x999
        self.core._save_edits(edits)
        p = self.core.plan()
        self.assertIn("another device", p["warnings"][0])
        self.assertTrue(p["identical"])

    def test_startup_parameters(self):
        entry = self.core.startup_entry(2, 0x8000, 1, 1000)
        self.assertEqual(entry["data"], "E8030000")
        self.core.save_edit(2, {"startup": [entry]})
        init = self.built()["coe_init"]
        self.assertEqual([(c["index"], c["data"]) for c in init][-1], (0x8000, "E8030000"))
        with self.assertRaises(ConfigError):
            self.core.startup_entry(2, 0x1008, 0, "x")                     # read-only
        with self.assertRaises(coe.CoeError):
            self.core.startup_entry(2, 0x8000, 2, 70000)                   # INT range
        with self.assertRaises(EditError):
            self.core.save_edit(2, {"startup": [{"index": 1, "subindex": 0, "data": "zz"}]})
        with self.assertRaises(EditError) as cm:
            self.core.save_edit(1, {"startup": [entry]})                  # no CoE on XI190202
        self.assertIn("no CoE mailbox", str(cm.exception))
        self.assertNotIn(1, self.core.load_edits())                        # refused edits are not saved


    def test_startup_replaces_the_kept_ones(self):
        """Seen on the X3: the kept 1000 and the new 0 both ended up in the ENI."""
        def gains():
            return [(c["index"], c["data"]) for c in self.built()["coe_init"] if c["index"] == 0x8000]
        self.core.save_edit(2, {"startup": [self.core.startup_entry(2, 0x8000, 1, 1000)]})
        self.core.plan()
        p = self.core.plan_data
        self.core.start_download(p["id"])
        self.core.wait_scan()
        self.core.wait_scan()
        self.core.reset_edit(2)
        self.assertEqual(gains(), [(0x8000, "E8030000")])                         # kept from the running configuration
        self.assertEqual(self.core.edit_options(2)["startup"][0]["data"], "E8030000")  # and shown for editing
        self.core.save_edit(2, {"startup": [self.core.startup_entry(2, 0x8000, 1, 0)]})
        self.assertEqual(gains(), [(0x8000, "00000000")])                         # replaced, not added
        self.core.save_edit(2, {"startup": []})
        self.assertEqual(gains(), [])                                             # and removable


class ObjectTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.bus = FakeBus()
        self.core = Core(os.path.join(self.dir.name, "active", "io-config", "config.json"), self.bus,
                         os.path.join(self.dir.name, "esi"), log=lambda m: None)
        self.core.start_scan()
        self.core.wait_scan()

    def tearDown(self):
        self.core.stop()
        self.dir.cleanup()

    def test_object_list_and_object(self):
        objs = self.core.objects(2)["objects"]
        self.assertEqual(len(objs), 30)
        settings = [o for o in objs if o["index"] == 0x8000][0]
        self.assertEqual((settings["name"], settings["object_code"], settings["startup"]), ("Channel 1 settings", "RECORD", True))
        o = self.core.object(2, 0x8000)
        self.assertEqual([(e.get("name"), e.get("data_type"), e.get("writable")) for e in o["entries"]][:3],
                         [("SubIndex 000", "USINT", False), ("User gain", "DINT", True), ("User offset", "INT", True)])
        self.assertEqual(self.core.object(2, 0x1008)["entries"][0]["value"], "XI422202 2Ch. Ana. Output +/-10V 16Bit BIPOLAR")
        gaps = [e for e in self.core.object(0, 0x10F3)["entries"] if "error" in e]
        self.assertTrue(gaps)                     # subindexes the device lists but doesn't have

    def test_no_coe(self):
        with self.assertRaises(Exception) as cm:
            self.core.objects(1)
        self.assertIn("has no CoE mailbox (FoE)", str(cm.exception))

    def test_sdo_write(self):
        with self.assertRaises(ConfigError) as cm:
            self.core.sdo_write(2, 0x8000, 1, 5)          # writable in PreOp only, the device is in Op
        self.assertIn("startup parameter", str(cm.exception))
        with self.assertRaises(ConfigError):
            self.core.sdo_write(2, 0x1008, 0, "x")        # read-only
        self.assertFalse([c for c in self.bus.calls if c[0] == "write_json"])
        # an entry writable in Op: 0x1C12:0 is not (PreOp); find one in the recording
        dev = self.bus.coe[65534]["objects"]
        writable_op = [(int(i), int(s)) for i, o in dev.items() for s, e in o["entries"].items()
                       if "response" in e and bytes(e["response"]["data"])[8] & 0x20]
        if writable_op:
            idx, sub = writable_op[0]
            e = coe.entry_description(dev[str(idx)]["entries"][str(sub)]["response"]["data"])
            value = 1 if e["data_type"] != "BOOL" else True
            self.assertEqual(self.core.sdo_write(2, idx, sub, value)["value"], value)


class ProcessDataTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.core = Core(os.path.join(self.dir.name, "a", "io-config", "config.json"), FakeBus(),
                         os.path.join(self.dir.name, "esi"), log=lambda m: None)

    def tearDown(self):
        self.core.stop()
        self.dir.cleanup()

    def test_read_and_write(self):
        with self.assertRaises(ConfigError):
            self.core.process_data()                      # no scan yet
        self.core.start_scan()
        self.core.wait_scan()
        pd = self.core.process_data()["variables"]
        self.assertEqual(len(pd), 44)
        self.assertEqual([v["dir"] for v in pd].count("output"), 8)
        self.assertEqual(self.core.write_output("XI422202/Channel_2.Value", "-100")["value"], -100)
        self.assertEqual(self.core.write_output("XI522001/Control.Transmit_request", True)["value"], True)
        self.assertEqual(self.core.write_output("XI522001/Transmit_data.Value", "414243")["value"][:4], [0x41, 0x42, 0x43, 0])
        for name, value in (("XI422202/Channel_1.Value", 40000), ("XI422202/Channel_1.Value", "x"),
                            ("XB_EC_12/UP_Supply_periphery.UP_Voltage", 1),
                            ("XI522001/Transmit_data.Value", "00" * 31)):
            with self.assertRaises((ConfigError, coe.CoeError)):
                self.core.write_output(name, value)
        with self.assertRaises(KeyError):
            self.core.write_output("nothing", 1)


if __name__ == "__main__":
    unittest.main()
