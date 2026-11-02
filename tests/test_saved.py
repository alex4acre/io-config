# SPDX-License-Identifier: MIT

"""Saved configurations (io-config/saved/<name>/), against the recorded X3 bus:  python3 -m unittest -q

An active save keeps what the master runs (ENI + ini, read from the master); a proposed one the Configuration list with
its base, edits and the ENI/ini built from it. Activating writes an ENI back (backup first, the list and its edits stay),
also without a scan first (REST / Data Layer); opening makes a save the Configuration list.
"""

import os
import tempfile
import unittest

from app.core import ConfigError, Core
from app.fake import MASTER, FakeBus
from tests.test_app import ESI_DIR, ESI_FILES, read


class SavedTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.config = os.path.join(self.dir.name, "activeConfiguration", "io-config", "config.json")
        self.bus = FakeBus()
        self.core = self.new_core()
        self.core.start_scan()
        self.core.wait_scan()

    def new_core(self):
        core = Core(self.config, self.bus, os.path.join(self.dir.name, "esi"), log=lambda m: None)
        for name in ESI_FILES:
            core.esi.add(name, read(os.path.join(ESI_DIR, name)))
        return core

    def tearDown(self):
        self.core.stop()
        self.dir.cleanup()

    def running_eni(self):
        return bytes(self.bus.nodes[MASTER + "admin/fieldbus_config"]["response"]["eni"])

    def activate(self, core, name):
        core.start_activate(name)
        core.wait_scan()            # the activation
        core.wait_scan()            # the scan after it

    def test_save_list_delete(self):
        s = self.core.save_current("Line A")
        self.assertEqual((s["name"], s["active"], s["edits"], s["kind"]), ("Line A", True, 0, "active"))
        self.assertEqual(s["devices"], ["XB_EC_12", "XI190202", "XI422202", "XI522001"])
        self.assertEqual(self.core.saved_file("Line A", "eni"), self.running_eni())
        with self.assertRaises(ConfigError):
            self.core.save_current("Line A")                         # exists
        self.core.save_current("Line A", overwrite=True)
        for bad in ("", ".hidden", " x", "a/b", "x" * 65, "ä"):
            with self.assertRaises(ConfigError):
                self.core.save_current(bad)
        self.core.delete_saved("Line A")
        self.assertEqual(self.core.saved(), [])
        with self.assertRaises(KeyError):
            self.core.start_activate("Line A")

    def test_activation_leaves_the_list_and_its_edits_alone(self):
        self.core.save_current("plain")
        original = self.running_eni()
        self.core.save_edit(2, {"name": "AO1"})
        p = self.core.plan(1000)                                    # another cycle time, written through the app
        self.core.start_download(p["id"])
        self.core.wait_scan()
        self.core.wait_scan()
        self.assertNotEqual(self.running_eni(), original)
        entries = [dict(e) for e in self.core._load_project()["devices"]]
        self.activate(self.core, "plain")
        st = self.core.state()
        self.assertEqual((st["activation"]["state"], st["activation"]["name"], st["download"]["error"]),
                         ("done", "plain", None))
        self.assertEqual(self.running_eni(), original)
        self.assertEqual(self.core.load_edits()[2]["name"], "AO1")             # the edits are not restored or dropped
        self.assertEqual(self.core._load_project()["devices"], entries)
        self.assertTrue(any(s.startswith("backup ") for s in st["download"]["steps"]))
        self.assertIn("The master's configuration changed since this list was started", self.core.plan()["warnings"])

    def test_save_proposed_and_open(self):
        self.core.save_edit(2, {"name": "AO1"})
        self.add_and_move()
        s = self.core.save_proposed("next", cycle_us=1000)
        self.assertEqual((s["kind"], s["error"], s["edits"]), ("proposed", None, 1))
        self.assertIn("AO1", s["devices"])
        self.assertTrue(self.core.saved_file("next", "eni"))
        self.assertIn(b"1000000", self.core.saved_file("next", "eni"))          # the cycle field's value
        before = [dict(e) for e in self.core._load_project()["devices"]]
        self.core.project_update()                                  # forget it, then open the save
        self.assertEqual(self.core.load_edits(), {})
        self.core.open_saved("next")
        self.assertEqual(self.core._load_project()["devices"], before)
        self.assertEqual(self.core.load_edits()[1]["name"], "AO1")             # it moved with its device
        self.assertEqual([d["name"] for d in self.core.state()["configuration"]["devices"]][1], "AO1")
        self.assertEqual(len(self.bus.written), 0)                  # the bus isn't touched

    def add_and_move(self):
        lib = {d["type"]: d for d in self.core.esi.search("", limit=100)}
        d = lib["XI190202"]
        self.core.project_add(d["vendor"], d["product"], d["revision"])
        self.core.project_move(1, 1)

    def test_open_an_active_save(self):
        self.core.save_current("A")
        self.add_and_move()
        self.core.open_saved("A")
        cfg = self.core.state()["configuration"]
        self.assertEqual([d["name"] for d in cfg["devices"]], ["XB_EC_12", "XI190202", "XI422202", "XI522001"])
        self.assertFalse(cfg["differs"])
        self.assertEqual(self.core.load_edits(), {})
        self.assertEqual([e["address"] for e in self.core._load_project()["devices"]], [1001, 1002, 1003, 1004])
        self.assertTrue(self.core.plan()["identical"])

    def test_proposed_that_does_not_build(self):
        for _ in range(4):
            self.core.project_remove(0)                             # an empty list builds nothing
        s = self.core.save_proposed("empty")
        self.assertEqual(s["kind"], "proposed")
        self.assertTrue(s["error"])
        with self.assertRaisesRegex(ConfigError, "can't be activated"):
            self.core.start_activate("empty")
        with self.assertRaises(KeyError):
            self.core.saved_file("empty", "eni")
        self.core.project_update()
        self.core.open_saved("empty")
        self.assertEqual(self.core.state()["configuration"]["devices"], [])

    def test_open_is_refused_while_writing(self):
        self.core.save_current("A")
        self.core.download_state["running"] = True
        try:
            with self.assertRaisesRegex(ConfigError, "being written"):
                self.core.open_saved("A")
        finally:
            self.core.download_state["running"] = False

    def test_old_save_without_a_kind_is_active(self):
        self.core.save_current("old")
        import json
        path = os.path.join(self.core.saved_dir, "old", "info.json")
        with open(path) as f:
            info = json.load(f)
        info.pop("kind")
        with open(path, "w") as f:
            json.dump(info, f)
        self.assertEqual(self.core.saved()[0]["kind"], "active")
        self.core.open_saved("old")

    def test_active_save_falls_back_to_the_files(self):
        self.core.save_current("A")
        self.activate(self.core, "A")                               # the files hold it now
        self.bus.gone = True
        s = self.core.save_current("B")
        self.assertEqual(s["kind"], "active")
        self.assertEqual(self.core.saved_file("B", "eni"), self.running_eni())

    def test_activate_without_a_scan(self):
        """A PLC activates through the Data Layer right after the app started: no scan has run yet."""
        self.core.save_current("A")
        fresh = self.new_core()
        try:
            self.activate(fresh, "A")
            self.assertEqual(fresh.activation["state"], "done")
            self.assertEqual(len(self.bus.written), 1)
        finally:
            fresh.stop()

    def test_failed_activation_is_reported(self):
        import app.configure
        self.core.save_current("A")
        self.bus.fail_op = True                                      # the master stays in PreOp
        old = app.configure._wait_state
        app.configure._wait_state = lambda bus, base, wanted, timeout, log: old(bus, base, wanted, min(timeout, 1), log)
        try:
            self.activate(self.core, "A")
        finally:
            app.configure._wait_state = old
        self.assertEqual(self.core.activation["state"], "error")
        self.assertTrue(self.core.activation["error"])
