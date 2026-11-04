# SPDX-License-Identifier: MIT

"""Custom names for process variables (1.9.0): python3 -m unittest -q

The name replaces the <Pdo>.<Entry> part of the variable in the ENI's process image, so the master publishes
realtime_data/<dir>/data/<Device>/<name>. Nothing else in the ENI changes, names follow the device and are read back from
the running configuration.
"""

import os
import tempfile
import unittest

from app import eni, generate
from app.core import Core
from app.edits import EditError
from app.fake import DATA, MASTER, FakeBus
from tests.test_app import ESI_DIR, ESI_FILES, read

REFERENCE = read(os.path.join(DATA, "controller-eni.xml"))
REF8 = read(os.path.join(DATA, "x3-8dev-ioe.xml"))


class GeneratorTest(unittest.TestCase):

    def setUp(self):
        self.parsed = eni.parse_eni(REF8)
        self.slave = next(s for s in self.parsed["slaves"] if s["name"] == "XI110116")
        self.dev = generate.device_from_eni(self.slave)

    def test_standard_variables_match_the_reference(self):
        """Every variable of every device of I/O Engineering's file, in image order, is what standard_variables says."""
        for slave in self.parsed["slaves"]:
            dev = generate.device_from_eni(slave)
            names = {v["dir"] + "/" + v["standard"] for v in generate.standard_variables(dev)}
            self.assertEqual(names, {v["dir"] + "/" + v["name"].split("/", 1)[1] for v in slave["variables"]}, slave["name"])

    def test_nothing_read_back_from_a_standard_file(self):
        for slave in self.parsed["slaves"]:
            self.assertEqual(generate.names_from_eni(generate.device_from_eni(slave), slave), ({}, False), slave["name"])

    def test_read_back_by_place(self):
        renamed = dict(self.slave, variables=[dict(v) for v in self.slave["variables"]])
        renamed["variables"][0]["name"] = "XI110116/MyInput1"
        self.assertEqual(generate.names_from_eni(self.dev, renamed), ({"in:Channel_1.Value": "MyInput1"}, False))

    def test_read_back_refuses_a_different_device(self):
        renamed = dict(self.slave, variables=[dict(v) for v in self.slave["variables"][:-1]])
        renamed["variables"][0]["name"] = "XI110116/MyInput1"
        self.assertEqual(generate.names_from_eni(self.dev, renamed), ({}, True))

    def test_reordered_standard_names_are_not_read_as_custom(self):
        swapped = dict(self.slave, variables=[dict(v) for v in self.slave["variables"]])
        swapped["variables"][0]["name"], swapped["variables"][1]["name"] = swapped["variables"][1]["name"], swapped["variables"][0]["name"]
        self.assertEqual(generate.names_from_eni(self.dev, swapped), ({}, False))

    def test_clashes(self):
        dev = dict(self.dev, variable_names={"in:Channel_1.Value": "A", "in:Channel_2.Value": "a"})
        self.assertEqual(generate.name_clashes(dev), ["A", "a"])
        dev = dict(self.dev, variable_names={"in:Channel_1.Value": "Channel_2.Value"})
        self.assertEqual(generate.name_clashes(dev), ["Channel_2.Value"])
        self.assertEqual(generate.name_clashes(dict(self.dev, variable_names={"in:Channel_1.Value": "A"})), [])


class CoreTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bus = FakeBus()
        self.core = Core(os.path.join(self.tmp.name, "activeConfiguration", "io-config", "config.json"), self.bus,
                         os.path.join(self.tmp.name, "esi"), log=lambda m: None)
        self.addCleanup(self.core.stop)
        for name in ESI_FILES:
            self.core.esi.add(name, read(os.path.join(ESI_DIR, name)))
        self.scan()
        self.pos = next(d["position"] for d in self.core.state()["configuration"]["devices"] if d["name"] == "XI190202")
        self.variables = self.core.edit_options(self.pos)["variables"]
        self.first = self.variables[0]

    def scan(self):
        self.core.start_scan()
        self.core.wait_scan()

    def write(self, plan):
        self.core.start_download(plan["id"])
        self.core.wait_scan()
        self.core.wait_scan()

    def name_first(self, name="MyInput1"):
        return self.core.save_edit(self.pos, {"variable_names": {self.first["key"]: name}})

    def test_pdo_rows_carry_the_variable_keys(self):
        """The PDO table can name a variable from its entry, also before the PDO is saved as selected."""
        out = self.core.edit_options(self.pos)
        keys = {e["key"] for p in out["pdos"] for e in p["entries"] if e["index"]}
        self.assertTrue({v["key"] for v in out["variables"]} <= keys)
        self.assertEqual(out["variable_names"], {})
        self.name_first()
        self.assertEqual(self.core.edit_options(self.pos)["variable_names"], {self.first["key"]: "MyInput1"})
        self.assertEqual(self.first["pdo_name"], next(p["name"] for p in out["pdos"] if p["index"] == self.first["pdo"]))

    def test_names_listed(self):
        self.assertTrue(self.variables)
        self.assertEqual(self.first["name"], "")
        self.assertEqual(self.first["key"].split(":")[1], self.first["standard"])

    def test_only_the_process_image_changes(self):
        """The rest of the ENI stays byte for byte; the plan shows a rename, not a removal and an addition."""
        before = self.core.plan()
        self.assertTrue(before["identical"])
        old = self.core.plan_file("eni")
        self.name_first()
        plan = self.core.plan()
        new = self.core.plan_file("eni")
        self.assertEqual(new, old.replace(f'XI190202/{self.first["standard"]}<'.encode(), b"XI190202/MyInput1<"))
        self.assertNotEqual(new, old)
        self.assertEqual(plan["variables"]["renamed"], [{"from": f'XI190202/{self.first["standard"]}', "to": "XI190202/MyInput1"}])
        self.assertEqual((plan["variables"]["added"], plan["variables"]["removed"]), ([], []))
        self.assertFalse(plan["identical"])

    def test_written_read_back_and_survives_update(self):
        self.name_first()
        self.write(self.core.plan())
        self.assertIn(f"{MASTER}realtime_data/input/data/XI190202/MyInput1", self.bus.nodes)
        self.assertTrue(self.core.plan()["identical"], "after the write nothing differs")
        data = self.core.process_data(self.core.state()["devices"][self.pos]["position"])
        item = next(v for v in data["variables"] if v["name"] == "XI190202/MyInput1")
        self.assertEqual(item["standard"], self.first["standard"])
        self.assertNotIn("error", item)
        # Update from the active bus clears the edits; the name comes back from the running ENI
        self.core.project_update()
        self.core.wait_scan()
        self.assertEqual(self.core.load_edits(), {})
        self.assertEqual(self.core.edit_options(self.pos)["variables"][0]["name"], "MyInput1")
        self.assertTrue(self.core.plan()["identical"])

    def test_back_to_standard_after_a_write(self):
        self.name_first()
        self.write(self.core.plan())
        self.name_first("")
        self.assertEqual(self.core.edit_options(self.pos)["variables"][0]["name"], "")
        plan = self.core.plan()
        self.assertEqual(plan["variables"]["renamed"], [{"from": "XI190202/MyInput1", "to": f'XI190202/{self.first["standard"]}'}])

    def test_typing_the_standard_name_is_no_name(self):
        self.name_first(self.first["standard"])
        self.assertEqual(self.core.edit_options(self.pos)["variables"][0]["name"], "")
        self.core.plan()
        self.assertTrue(self.core.plan_data["identical"])

    def test_rules(self):
        for bad in ("1abc", "a b", "a.b", "a/b", "x" * 65):
            with self.assertRaises(EditError, msg=bad):
                self.name_first(bad)
        self.name_first("Same")
        second = self.variables[1]["key"]
        with self.assertRaisesRegex(EditError, "more than one"):
            self.core.save_edit(self.pos, {"variable_names": {second: "same"}})
        with self.assertRaisesRegex(EditError, "more than one"):
            self.core.save_edit(self.pos, {"variable_names": {second: self.variables[2]["standard"]}})

    def test_sending_some_rows_keeps_the_others(self):
        self.name_first()
        out = self.core.save_edit(self.pos, {"variable_names": {self.variables[1]["key"]: "Two"}})["variables"]
        self.assertEqual([v["name"] for v in out[:2]], ["MyInput1", "Two"])

    def test_device_rename_keeps_the_name(self):
        self.name_first()
        self.core.save_edit(self.pos, {"name": "Rack1"})
        self.core.plan()
        self.assertIn(b"Rack1/MyInput1<", self.core.plan_file("eni"))

    def test_name_of_a_variable_that_left_is_a_warning(self):
        self.core.save_edit(self.pos, {"variable_names": {"in:Gone.Value": "Lost"}})
        plan = self.core.plan()
        self.assertTrue(any('"Lost" is not written' in w for w in plan["warnings"]), plan["warnings"])
        self.assertNotIn(b"Lost", self.core.plan_file("eni"))


class ByteIdenticalTest(unittest.TestCase):

    def test_unnamed_generator_output_untouched(self):
        parsed = eni.parse_eni(REFERENCE)
        for slave in parsed["slaves"]:
            self.assertNotIn("variable_names", generate.device_from_eni(slave))


if __name__ == "__main__":
    unittest.main()
