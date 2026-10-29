# SPDX-License-Identifier: MIT

"""Saved configurations (io-config/saved/<name>/), against the recorded X3 bus:  python3 -m unittest -q

Saving keeps what the master runs (ENI + ini, read from the master) and the app's edits; activating writes it back
(backup first) and restores the edits, also without a scan first (REST / Data Layer).
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
        self.assertEqual((s["name"], s["active"], s["edits"]), ("Line A", True, 0))
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

    def test_activate_brings_back_the_configuration_and_its_edits(self):
        self.core.save_edit(2, {"name": "AO1"})
        self.core.save_current("with AO1")
        original = self.running_eni()
        # change the bus configuration: another cycle time, written through the app; forget the edit
        self.core.reset_edit(2)
        p = self.core.plan(1000)
        self.core.start_download(p["id"])
        self.core.wait_scan()
        self.core.wait_scan()
        self.assertNotEqual(self.running_eni(), original)
        self.assertFalse(self.core.saved()[0]["active"])
        self.assertEqual(self.core.load_edits(), {})

        self.activate(self.core, "with AO1")
        st = self.core.state()
        self.assertEqual((st["activation"]["state"], st["activation"]["name"], st["download"]["error"]),
                         ("done", "with AO1", None))
        self.assertEqual(self.running_eni(), original)
        self.assertEqual(self.core.load_edits()[2]["name"], "AO1")
        self.assertTrue(self.core.saved()[0]["active"])
        self.assertIn("edits of with AO1 restored (1)", st["download"]["steps"])
        self.assertTrue(any(s.startswith("backup ") for s in st["download"]["steps"]))

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
