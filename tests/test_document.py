# SPDX-License-Identifier: MIT

"""A whole configuration as one JSON document (app/document.py, docs/api.md):  python3 -m unittest -q

Against the recorded X3 bus. What matters most: a document builds the same ENI as the same choices made in the UI, the
result doesn't depend on the running configuration, and neither the Configuration list nor the master is touched.
"""

import json
import os
import tempfile
import unittest

from app import eni, generate
from app.core import ConfigError, Core
from app.document import DocumentError
from app.fake import DATA, MASTER, FakeBus
from tests.test_app import ESI_DIR, ESI_FILES, read

COUPLER = {"vendor": "0x24", "product": "0x242A0F", "revision": "0x10100", "name": "Coupler"}
DI = {"vendor": 0x24, "product": 0x242A36, "revision": 0x100, "name": "Inputs"}
AO = {"vendor": "#x24", "product": "#x242A1F", "revision": "256", "name": "Analog"}
DRIVE_DIR = os.path.join(DATA, "esi-drive")


def doc(*devices, **top):
    return {"schema": 1, "name": "Line1", "cycle_us": 2000, "devices": list(devices), **top}


class Base(unittest.TestCase):
    bus_args = {}
    extra_esi = ()

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.config = os.path.join(self.dir.name, "activeConfiguration", "io-config", "config.json")
        self.bus = FakeBus(**self.bus_args)
        self.core = Core(self.config, self.bus, os.path.join(self.dir.name, "esi"), log=lambda m: None)
        for name in ESI_FILES:
            self.core.esi.add(name, read(os.path.join(ESI_DIR, name)))
        for path in self.extra_esi:
            self.core.esi.add(os.path.basename(path), read(path))
        self.core.start_scan()
        self.core.wait_scan()

    def tearDown(self):
        self.core.stop()
        self.dir.cleanup()

    def running(self):
        return bytes(self.bus.nodes[MASTER + "admin/fieldbus_config"]["response"]["eni"])

    def folder_bytes(self, *names):
        out = []
        for name in names:
            with open(os.path.join(os.path.dirname(self.config), name), "rb") as f:
                out.append(f.read())
        return out

    def assert_refused(self, document_, *patterns):
        with self.assertRaises(DocumentError) as e:
            self.core.create_document(document_)
        for pattern in patterns:
            self.assertRegex(str(e.exception), pattern)
        self.assertEqual(self.core.saved(), [])
        self.assertEqual(self.core.creation["state"], "error")
        return e.exception


class CreateTest(Base):

    def test_create_saves_a_proposed_configuration_and_touches_nothing_else(self):
        self.core.save_edit(1, {"name": "MyName"})
        before = self.folder_bytes("project.json", "edits.json")
        eni_before = self.running()
        r = self.core.create_document(doc(COUPLER, DI))
        self.assertEqual((r["name"], r["devices"], r["cycle_us"]), ("Line1", ["Coupler", "Inputs"], 2000))
        self.assertEqual(self.core.creation["state"], "done")
        s = self.core.saved()[0]
        self.assertEqual((s["name"], s["kind"], s["devices"], s["error"]), ("Line1", "proposed", ["Coupler", "Inputs"], None))
        self.assertEqual(self.folder_bytes("project.json", "edits.json"), before)
        self.assertEqual(self.running(), eni_before)
        slaves = eni.parse_eni(self.core.saved_file("Line1", "eni"))["slaves"]
        self.assertEqual([(x["name"], x["address"]) for x in slaves], [("Coupler", 1001), ("Inputs", 1002)])

    def test_no_warnings_about_the_bus_a_document_doesnt_care_about(self):
        r = self.core.create_document(doc(COUPLER))
        self.assertEqual(r["warnings"], [])

    def test_the_running_configuration_has_no_influence(self):
        first = self.core.create_document(doc(COUPLER, DI, AO)) and self.core.saved_file("Line1", "eni")
        self.core.start_activate("Line1")
        self.core.wait_scan()
        self.core.wait_scan()
        self.assertEqual(self.running(), first)                     # activate is what writes it
        self.core.create_document(doc(COUPLER, DI, AO, overwrite=True))
        self.assertEqual(self.core.saved_file("Line1", "eni"), first)

    def test_unnamed_devices_get_the_default_names(self):
        plain = [{k: d[k] for k in ("vendor", "product", "revision")} for d in (COUPLER, COUPLER)]
        self.assertEqual(self.core.create_document(doc(*plain))["devices"], ["XB_EC_12", "XB_EC_12_2"])

    def test_same_as_the_ui_for_the_same_choices(self):
        """UI route: a list of the same devices with the same edits, no base ENI."""
        self.core.project_update()
        for pos in reversed(range(len(self.core.cfg_devices))):
            self.core.project_remove(pos)
        self.core.project_add(0x24, 0x242A0F, 0x10100, "Coupler")
        self.core.project_add(0x24, 0x242A1F, 0x100, "Analog")
        options = self.core.edit_options(1)
        names = {options["variables"][0]["key"]: "MyName"}
        self.core.save_edit(1, {"variable_names": names})
        ui = self.core.plan(2000)
        ui_eni = self.core.plan_file("eni")
        self.assertEqual(ui["errors"], [])
        self.core.create_document(doc(COUPLER, dict(AO, variable_names=names)))
        self.assertEqual(self.core.saved_file("Line1", "eni"), ui_eni)

    def test_cycle_defaults_to_the_scheduler_task(self):
        d = doc(COUPLER)
        del d["cycle_us"]
        self.bus.nodes["scheduler/tasks/ctrlXAutomation/properties"]["cycletime"] = 4000
        self.assertEqual(self.core.create_document(d)["cycle_us"], 4000)

    def test_a_cycle_other_than_the_task_warns(self):
        r = self.core.create_document(doc(COUPLER, cycle_us=1000))
        self.assertRegex(r["warnings"][0], "differs from the scheduler task")

    def test_overwrite(self):
        self.core.create_document(doc(COUPLER))
        with self.assertRaises(ConfigError):
            self.core.create_document(doc(COUPLER, DI))
        self.assertEqual(self.core.creation["state"], "error")
        self.assertEqual(self.core.saved()[0]["devices"], ["Coupler"])
        self.core.create_document(doc(COUPLER, DI, overwrite=True))
        self.assertEqual(self.core.saved()[0]["devices"], ["Coupler", "Inputs"])

    def test_refused_form(self):
        for bad, pattern in (("{", "not valid JSON"), ("[]", "JSON object"),
                             (doc(COUPLER, schema=2), "schema"), (doc(COUPLER, name=""), "name"),
                             (doc(COUPLER, cycle_us=1), "cycle_us"), (doc(COUPLER, overwrite="yes"), "overwrite"),
                             (doc(), "at least one"), (doc(COUPLER, extra=1), 'unknown key "extra"'),
                             (doc(dict(COUPLER, nam="x")), 'unknown key "nam"'),
                             (doc({"vendor": 36, "product": "abc", "revision": 1}), "product is needed"),
                             (doc(dict(COUPLER, pdos=["x"])), "pdos must be a list of numbers"),
                             (doc(dict(COUPLER, startup=[1])), "startup must be a list")):
            self.assert_refused(bad, pattern)

    def test_all_problems_are_told_at_once(self):
        e = self.assert_refused(doc(dict(COUPLER, nam="x"), dict(DI, product="abc")))
        self.assertEqual(len(e.errors), 2)

    def test_refused_without_an_exact_esi(self):
        self.assert_refused(doc(dict(COUPLER, product="0x500")), "no ESI in the library")
        self.assert_refused(doc(dict(COUPLER, revision="0x10101")), "only revision #x00010100")

    def test_refused_choices(self):
        self.assert_refused(doc(COUPLER, dict(COUPLER, name="Coupler")), "used by positions")
        self.assert_refused(doc(dict(COUPLER, name="1bad")), "must start with a letter")
        self.assert_refused(doc(dict(AO, pdos=[0x7777])), "not offered|can't be changed")
        self.assert_refused(doc(dict(AO, dc_mode="Nope")), "DC mode")
        self.assert_refused(doc(dict(AO, variable_names={"in:Nothing.Here": "X"})), "not in its process data")
        self.assert_refused(doc(dict(AO, startup=[{"index": "0x8000", "subindex": 1}])), "data .hex. are needed")

    def test_hex_text_in_startup(self):
        d = doc(dict(AO, startup=[{"index": "0x8000", "subindex": "1", "data": "E803"}]))
        self.core.create_document(d)
        slave = eni.parse_eni(self.core.saved_file("Line1", "eni"))["slaves"][0]
        self.assertIn((0x8000, 1, "E803"), [(c["index"], c["subindex"], c["data"].upper()) for c in slave["coe_init"]])

    def test_connections(self):
        branch = doc(COUPLER, dict(DI, connected_to={"device": "Coupler", "port": "C"}))
        self.core.create_document(branch)
        slaves = eni.parse_eni(self.core.saved_file("Line1", "eni"))["slaves"]
        self.assertEqual(slaves[1]["previous"][0], "C")
        self.core.delete_saved("Line1")
        for bad, pattern in ((doc(dict(COUPLER, connected_to={"device": "X", "port": "B"})), "first device"),
                             (doc(COUPLER, dict(DI, connected_to={"device": "Nobody", "port": "B"})), "not a named device"),
                             (doc(COUPLER, dict(DI, connected_to={"device": "Coupler", "port": "A"})), "port must be"),
                             (doc(COUPLER, dict(DI, connected_to="Coupler")), "connected_to must be"),
                             (doc(COUPLER, DI, dict(AO, connected_to={"device": "Coupler", "port": "B"})), "already taken")):
            self.assert_refused(bad, pattern)

    def test_open_in_the_ui(self):
        names = {self.ao_key(): "MyName"}
        self.core.create_document(doc(COUPLER, dict(AO, variable_names=names)))
        created = self.core.saved_file("Line1", "eni")
        self.core.open_saved("Line1")
        self.assertEqual([d["name"] for d in self.core.cfg_devices], ["Coupler", "Analog"])
        self.core.plan(2000)
        self.assertEqual(self.core.plan_file("eni"), created)

    def ao_key(self):
        entry = self.core.esi.find(0x24, 0x242A1F, 0x100)[1]
        return generate.standard_variables(generate.device_from_esi(self.core.esi.device(entry), ""))[0]["key"]

    def test_the_data_layer_way(self):
        self.core.start_create(json.dumps(doc(COUPLER)))
        self.core.wait_create()
        self.assertEqual((self.core.creation["state"], self.core.creation["name"]), ("done", "Line1"))
        self.core.start_create(json.dumps(doc(dict(COUPLER, product="0x500"), name="Bad")))
        self.core.wait_create()
        self.assertEqual(self.core.creation["state"], "error")
        self.assertRegex(self.core.creation["errors"][0], "no ESI")
        self.assertEqual([s["name"] for s in self.core.saved()], ["Line1"])
        with self.assertRaises(ValueError):                         # told at once: the write itself fails
            self.core.start_create("{not json")

    def test_activate_needs_the_saved_one_only(self):
        self.core.create_document(doc(COUPLER, DI))
        self.core.start_activate("Line1")
        self.core.wait_scan()
        self.core.wait_scan()
        self.assertEqual(self.core.activation["state"], "done")
        self.assertEqual(self.running(), self.core.saved_file("Line1", "eni"))


class ExportTest(Base):

    def round_trip(self, exported, expected):
        self.assertEqual(exported["warnings"], [], exported["document"])
        again = dict(exported["document"], name="Back", overwrite=True)
        self.core.create_document(again)
        self.assertEqual(self.core.saved_file("Back", "eni"), expected)

    def test_the_list_as_it_runs(self):
        self.round_trip(self.core.export_project(), self.running())

    def test_the_list_with_choices(self):
        self.core.save_edit(1, {"name": "Inputs"})
        key = self.core.edit_options(2)["variables"][0]["key"]
        self.core.save_edit(2, {"variable_names": {key: "MyName"}})
        exported = self.core.export_project("Mine")
        self.assertEqual(exported["document"]["name"], "Mine")
        self.assertEqual(exported["document"]["devices"][1]["name"], "Inputs")
        self.assertEqual(exported["document"]["devices"][2]["variable_names"], {key: "MyName"})
        self.core.plan()
        self.round_trip(exported, self.core.plan_file("eni"))

    def test_identity_in_hex(self):
        d = self.core.export_project()["document"]["devices"][0]
        self.assertEqual((d["vendor"], d["product"], d["revision"]), ("0x24", "0x242A0F", "0x10100"))

    def test_a_saved_active_one(self):
        self.core.save_current("Running")
        self.round_trip(self.core.export_saved("Running"), self.running())

    def test_a_saved_proposed_one(self):
        self.core.save_edit(1, {"name": "Inputs"})
        self.core.save_saved("Prop", "proposed")
        self.round_trip(self.core.export_saved("Prop"), self.core.saved_file("Prop", "eni"))

    def test_a_created_one_comes_back_as_written(self):
        d = doc(COUPLER, overwrite=True)
        self.core.create_document(d)
        self.assertEqual(self.core.export_saved("Line1"),
                         {"document": {k: v for k, v in d.items() if k != "overwrite"}, "warnings": []})

    def test_says_so_when_it_cant_be_rebuilt(self):
        self.core.esi.delete("BoschRexroth_ctrlX_IO_XB-EC-12.xml")
        self.core.start_scan()
        self.core.wait_scan()
        warnings = self.core.export_project()["warnings"]
        self.assertTrue(any("no ESI in the library" in w for w in warnings), warnings)
        self.assertTrue(any("would be refused" in w for w in warnings), warnings)

    def test_unknown_saved(self):
        with self.assertRaises(KeyError):
            self.core.export_saved("Nope")


class ExportNodeTest(Base):
    """What the Data Layer node io-config/export does."""

    def export(self, name):
        self.core.start_export(name)
        self.core.wait_export()
        return self.core.exporting

    def test_the_list(self):
        x = self.export("")
        self.assertEqual((x["state"], x["result"], x["warnings"]), ("done", "started list", []))
        self.assertEqual(json.loads(x["document"]), self.core.export_project()["document"])

    def test_a_saved_one(self):
        d = doc(COUPLER)
        self.core.create_document(d)
        x = self.export(" Line1 ")
        self.assertEqual((x["state"], x["name"], x["result"]), ("done", "Line1", "started Line1"))
        self.assertEqual(json.loads(x["document"]), d)

    def test_a_document_can_go_straight_back_into_create(self):
        x = self.export("")
        self.core.start_create(json.dumps(dict(json.loads(x["document"]), name="Back")))
        self.core.wait_create()
        self.assertEqual(self.core.creation["state"], "done")
        self.assertEqual(self.core.saved_file("Back", "eni"), self.running())

    def test_an_unknown_name_fails_at_once(self):
        self.export("")
        for bad in ("Nope", "a/b"):
            with self.assertRaises(ConfigError):
                self.core.start_export(bad)
            x = self.core.exporting
            self.assertEqual((x["state"], x["document"]), ("error", ""))
            self.assertRegex(x["result"], "^error: ")

    def test_one_at_a_time(self):
        self.core.exporting["state"] = "running"
        with self.assertRaises(ConfigError):
            self.core.start_export("")
        self.assertEqual(self.core.exporting["state"], "running")   # the running one keeps its result
        self.core.exporting["state"] = "idle"

    def test_a_failure_in_the_thread_is_told(self):
        self.core.export_project = lambda: 1 / 0
        x = self.export("")
        self.assertEqual((x["state"], x["document"]), ("error", ""))
        self.assertRegex(x["result"], "^error: division")


class DriveTest(Base):
    """The SoE drive on a branch of the coupler, against I/O Engineering's own file."""
    bus_args = {"drive": True}
    extra_esi = tuple(os.path.join(DRIVE_DIR, f) for f in sorted(os.listdir(DRIVE_DIR)))

    def setUp(self):
        super().setUp()
        for d in self.core.state()["configuration"]["unconfigured"]:
            self.core.project_add_from_bus(d["bus_pos"])

    def test_byte_for_byte_as_io_engineering(self):
        exported = self.core.export_project()
        self.assertEqual(exported["warnings"], [], exported["document"])
        self.assertEqual(exported["document"]["devices"][4]["connected_to"]["port"], "C")
        self.core.create_document(dict(exported["document"], name="Back"))
        self.assertEqual(self.core.saved_file("Back", "eni"), read(os.path.join(DATA, "x3-drive-ioe.xml")))

    def test_edited_telegram_byte_for_byte(self):
        options = self.core.edit_options(4)["soe"]
        mdt = [e["idn"] for e in options["mdt"]]
        at = [e["idn"] for e in options["at"]]
        mdt.insert(mdt.index(0x8000 | 2250), 41)
        at = [x for x in at if x != 84] + [11]
        self.core.save_edit(4, {"soe_mdt": mdt, "soe_at": at})
        exported = self.core.export_project()
        self.assertEqual((exported["document"]["devices"][4]["soe_mdt"], exported["document"]["devices"][4]["soe_at"]), (mdt, at))
        self.assertEqual(exported["warnings"], [], exported["document"])
        self.core.create_document(dict(exported["document"], name="Back"))
        self.assertEqual(self.core.saved_file("Back", "eni"), read(os.path.join(DATA, "x3-drive-ioe-telegram.xml")))

    def test_opened_with_the_drive_on_the_bus_keeps_its_edits(self):
        """The document names the ESI's revision (0x06000003), the drive on the bus reports 0x3."""
        options = self.core.edit_options(4)["soe"]
        at = [e["idn"] for e in options["at"] if e["idn"] != 84] + [11]
        self.core.save_edit(4, {"soe_at": at, "name": "Drive"})
        d = self.core.export_project()["document"]
        self.assertEqual(d["devices"][4]["revision"], "0x6000003")
        self.core.create_document(dict(d, name="Back"))
        created = self.core.saved_file("Back", "eni")
        self.core.open_saved("Back")
        self.core.plan(2000)
        self.assertEqual(self.core.plan_data["warnings"], [])
        self.assertEqual(self.core.plan_file("eni"), created)
        self.assertEqual(self.core.cfg_devices[4]["name"], "Drive")

    def test_soe_dictionary_rule_applies(self):
        drive = self.core.export_project()["document"]["devices"][4]
        drive.pop("connected_to")
        self.assert_refused(doc(dict(drive, soe_mdt=[47])), "must start with S-0-0134")


class ElmoTest(Base):
    """A modular device. The fake drive has revision 0x220031, the ESI 0x220030."""
    bus_args = {"elmo": True}
    extra_esi = (os.path.join(DATA, "esi-elmo", "ElmoTitanium_28_V220030_S.xml"),)
    ELMO = {"vendor": 0x9A, "product": 0x01280002, "revision": 0x220030, "name": "Elmo"}

    def test_modules_from_the_document(self):
        self.core.create_document(doc(self.ELMO))
        default = eni.parse_eni(self.core.saved_file("Line1", "eni"))["slaves"][0]
        self.core.create_document(doc(dict(self.ELMO, modules=[0x288110, 0x100, 0, 0, 0]), overwrite=True))
        one_axis = eni.parse_eni(self.core.saved_file("Line1", "eni"))["slaves"][0]
        self.assertLess(len(one_axis["variables"]), len(default["variables"]))
        self.core.delete_saved("Line1")
        self.assert_refused(doc(dict(self.ELMO, modules=[1, 2])), "modules must be a list of 5")

    def test_export_of_chosen_modules_round_trips(self):
        self.core.create_document(doc(dict(self.ELMO, modules=[0x288110, 0x100, 0, 0, 0])))
        created = self.core.saved_file("Line1", "eni")
        self.core.open_saved("Line1")
        exported = self.core.export_project()
        self.assertEqual(exported["document"]["devices"][0]["modules"], [0x288110, 0x100, 0, 0, 0])
        self.assertEqual(exported["warnings"], [])
        self.core.create_document(dict(exported["document"], name="Back"))
        self.assertEqual(self.core.saved_file("Back", "eni"), created)

    def test_a_device_with_another_revision_exports_the_revision_of_its_esi(self):
        for d in self.core.state()["configuration"]["unconfigured"]:
            self.core.project_add_from_bus(d["bus_pos"])
        exported = self.core.export_project()
        self.assertEqual(exported["document"]["devices"][-1]["revision"], "0x220030")
        self.assertEqual(exported["warnings"], [])


if __name__ == "__main__":
    unittest.main()
