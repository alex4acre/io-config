# SPDX-License-Identifier: MIT

"""The project: a configuration for devices that aren't on the bus, also without a working master.  python3 -m unittest -q

Absent devices stay in the configuration with a warning, devices can be added from the ESI library, and writing
is best effort: what the master doesn't do is a warning, the configuration then waits in the active configuration's files.
"""

import json
import os
import tempfile
import unittest

import app.configure
from app.configure import merge
from app.core import ConfigError, Core
from app.fake import DATA, MASTER, FakeBus
from tests.test_app import ESI_DIR, ESI_FILES, read

REFERENCE = read(os.path.join(DATA, "controller-eni.xml"))
NUM = MASTER + "admin/fieldbus_status/num_connected_slaves"
XI190202 = 1                                      # bus index in the recording: coupler, XI190202, XI422202, XI522001


def unplug(bus, index):
    """Takes a device off the fake bus: the devices behind it move up."""
    bus.slaves.pop(index)
    bus.slaves[:] = [dict(s, autoinc=(-i) & 0xFFFF) for i, s in enumerate(bus.slaves)]
    bus.nodes[NUM] = {"response": {"numSlaves": len(bus.slaves)}}


class MergeTest(unittest.TestCase):

    def test_merge(self):
        self.assertEqual(merge(["a", "b", "c"], ["a", "b", "c"]), [(0, 0), (1, 1), (2, 2)])
        self.assertEqual(merge(["a", "x", "c"], ["a", "c"]), [(0, 0), (1, None), (2, 1)])
        self.assertEqual(merge(["a", "c"], ["a", "n", "c"]), [(0, 0), (None, 1), (1, 2)])
        self.assertEqual(merge(["x"], ["n"]), [(0, None), (None, 0)])
        self.assertEqual(merge([], []), [])


class ProjectTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.config = os.path.join(self.dir.name, "activeConfiguration", "io-config", "config.json")
        self.bus = FakeBus()
        self.core = self.new_core()
        self.lib = {d["type"]: d for d in self.core.esi.search("", limit=100)}
        self.wait = app.configure._wait_state
        app.configure._wait_state = lambda bus, base, wanted, timeout, log: self.wait(bus, base, wanted, min(timeout, 1), log)

    def tearDown(self):
        app.configure._wait_state = self.wait
        self.core.stop()
        self.dir.cleanup()

    def new_core(self):
        core = Core(self.config, self.bus, os.path.join(self.dir.name, "esi"), log=lambda m: None)
        for name in ESI_FILES:
            core.esi.add(name, read(os.path.join(ESI_DIR, name)))
        return core

    def scan(self, core=None):
        core = core or self.core
        core.start_scan()
        core.wait_scan()
        return core.state()

    def write(self, core=None):
        core = core or self.core
        p = core.plan()
        self.assertEqual(p["errors"], [])
        core.start_download(p["id"])
        core.wait_scan()            # the download thread, then the rescan it starts
        core.wait_scan()
        return core.state()

    def add(self, type_name, **kw):
        d = self.lib[type_name]
        return self.core.project_add(d["vendor"], d["product"], d["revision"], **kw)

    def files(self):
        folder = os.path.join(self.core.active_dir, "comm.ethercat.master", "config", "ethercatmaster")
        return os.path.join(folder, "ethercat.xml"), os.path.join(folder, "ethercat.ini")

    # --- absent devices ------------------------------------------------------------------------------

    def test_unplugged_device_stays_in_the_configuration(self):
        unplug(self.bus, XI190202)
        s = self.scan()
        self.assertEqual([(d["name"], d["present"]) for d in s["devices"]],
                         [("XB_EC_12", True), ("XI190202", False), ("XI422202", True), ("XI522001", True)])
        self.assertEqual(s["devices"][1]["level"], "warning")
        self.assertFalse(s["project"])
        p = self.core.plan()
        self.assertTrue(p["identical"])
        self.assertEqual(self.core.plan_file("eni"), REFERENCE)
        self.assertEqual(p["removed_devices"], [])
        self.assertEqual(p["warnings"], ["Position 1: XI190202 is not present on the bus"])

    def test_device_taking_the_port_of_an_absent_one_chains_after_it(self):
        coupler = self.bus.slaves[0]["online_info"]["response"]
        behind = json.loads(json.dumps(self.bus.slaves[2]))
        behind["online_info"]["response"]["portSlaveHandles"][0] = coupler["slaveHandle"]
        behind["online_info"]["response"]["previousPort"] = 1
        self.bus.slaves[2] = behind
        unplug(self.bus, XI190202)
        self.scan()
        self.assertEqual(self.core.plan()["identical"], True)

    def test_online_access_uses_the_place_on_the_bus(self):
        unplug(self.bus, 2)                         # XI422202 (CoE)
        self.scan()
        self.assertEqual(self.core._autoinc(3)["address"], (-2) & 0xFFFF)
        for call in (lambda: self.core._autoinc(2), lambda: self.core.objects(2), lambda: self.core.sdo_write(2, 0x8000, 1, 1)):
            with self.assertRaisesRegex(ConfigError, "not present on the bus"):
                call()

    def test_edit_of_an_absent_device(self):
        unplug(self.bus, XI190202)
        self.scan()
        self.core.save_edit(1, {"name": "Inputs"})
        self.assertEqual(self.core.edit_options(1)["name"], "Inputs")
        self.assertEqual(self.core.plan()["devices"][1]["name"], "Inputs")
        self.scan()                                 # stays with its device
        self.assertEqual(self.core.edit_options(1)["name"], "Inputs")

    # --- the project list ----------------------------------------------------------------------------

    def test_add_from_the_library_moves_the_edits_along(self):
        self.scan()
        self.core.save_edit(2, {"name": "Analog"})
        self.assertEqual(self.add("XI522001", after=0), 1)
        s = self.core.state()
        self.assertTrue(s["project"])
        self.assertEqual([d["present"] for d in s["devices"]], [True, False, True, True, True])
        self.assertEqual(self.core.edit_options(3)["name"], "Analog")
        self.assertEqual(self.core.plan()["devices"][3]["name"], "Analog")
        self.assertEqual(self.core.plan()["devices"][1]["name"], "XI522001_2")   # the running one keeps its name
        self.scan()                                 # a rescan changes nothing about it
        self.assertEqual(self.core.state()["devices"][1]["present"], False)

    def test_port_of_an_added_device(self):
        self.scan()
        self.add("XI190202", upstream=0, port="C")
        from app import eni
        self.core.plan()
        parsed = eni.parse_eni(self.core.plan_file("eni"))
        self.assertEqual(parsed["slaves"][4]["previous"], ("C", parsed["slaves"][0]["address"]))

    def test_add_checks(self):
        self.scan()
        with self.assertRaisesRegex(ConfigError, "ESI library"):
            self.core.project_add(1, 2, 3)
        for kw in ({"after": 99}, {"after": 1, "upstream": 3}, {"port": "A"}):
            with self.assertRaises(ConfigError):
                self.add("XI190202", **kw)
        self.assertFalse(self.core.state()["project"])

    def test_remove_and_move_only_absent_devices(self):
        unplug(self.bus, XI190202)
        self.scan()
        with self.assertRaisesRegex(ConfigError, "on the bus"):
            self.core.project_remove(0)
        with self.assertRaisesRegex(ConfigError, "on the bus"):
            self.core.project_move(0, 1)
        self.assertEqual(self.core.project_move(1, 1), 2)
        self.assertEqual([d["name"] for d in self.core.state()["devices"]], ["XB_EC_12", "XI422202", "XI190202", "XI522001"])
        self.core.project_remove(2)
        self.assertEqual(self.core.plan()["removed_devices"], ["XI190202"])
        self.core.project_reset()
        self.assertFalse(self.core.state()["project"])
        self.assertEqual(len(self.core.state()["devices"]), 4)

    def test_changed_master_configuration_is_a_warning(self):
        self.scan()
        self.add("XI190202")
        other = read(os.path.join(DATA, "x3-drive-ioe.xml"))
        self.bus.nodes[MASTER + "admin/fieldbus_config"]["response"]["eni"] = list(other)
        self.scan()
        self.assertIn("The master's configuration changed since this project was started", self.core.plan()["warnings"])

    def test_good_write_ends_the_project(self):
        self.scan()
        self.add("XI190202")
        s = self.write()
        self.assertIsNone(s["download"]["error"])
        self.assertEqual(s["download"]["warnings"], [])
        self.assertFalse(s["project"])
        self.assertEqual([d["present"] for d in s["devices"]], [True, True, True, True, False])
        self.assertEqual(s["devices"][4]["name"], "XI190202_2")         # now from the running configuration

    # --- without a working master -----------------------------------------------------------------------

    def test_offline_project_without_a_master(self):
        self.bus.gone = True
        s = self.scan()
        self.assertIn("no EtherCAT master instance", s["scan"]["error"])
        self.assertEqual(s["master"]["state"], "unavailable")
        self.assertEqual(self.add("XB-EC-12"), 0)
        self.assertEqual(self.add("XI190202"), 1)
        self.core.save_edit(1, {"name": "Inputs"})
        s = self.write()
        d = s["download"]
        self.assertIsNone(d["error"])
        self.assertEqual(self.bus.written, [])
        self.assertTrue(any("next master start" in w for w in d["warnings"]))
        eni_path, ini_path = self.files()
        self.assertIn(b"<Name>Inputs</Name>", read(eni_path))
        self.assertTrue(read(ini_path))
        self.assertTrue(s["project"])                # the master doesn't hold it: nothing is lost
        self.assertEqual([x["name"] for x in s["devices"]], ["XB_EC_12", "Inputs"])

    def test_plan_without_a_scan_starts_from_the_files(self):
        self.scan()
        self.write()
        self.bus.gone = True
        fresh = self.new_core()
        self.assertEqual(len(fresh.state()["devices"]), 4)
        self.assertIsNone(fresh.state()["master"])
        self.assertTrue(fresh.plan()["identical"])

    def test_scan_falls_back_to_the_files(self):
        self.scan()
        self.write()
        self.bus.gone = True
        s = self.scan()
        self.assertTrue(s["master"]["from_files"])
        self.assertEqual([d["present"] for d in s["devices"]], [False] * 4)
        self.assertEqual(s["master"]["cycle_us"], 2000)

    def test_lost_master_doesnt_drop_edits_of_unconfigured_devices(self):
        self.scan()
        self.add("XI190202")
        self.core.project_reset()
        self.core.save_edit(3, {"name": "Serial"})
        self.bus.nodes[MASTER + "admin/fieldbus_config"]["response"]["eni"] = []    # not in any configuration
        self.scan()
        self.bus.gone = True
        self.scan()
        self.assertIn(3, self.core.load_edits())

    def steps_of(self, **flags):
        self.scan()
        for k, v in flags.items():
            setattr(self.bus, k, v)
        return self.write()["download"]

    def test_write_master_stays_out_of_init(self):
        d = self.steps_of(fail_init=True)
        self.assertIsNone(d["error"])
        self.assertTrue(any("not in Init" in w for w in d["warnings"]))
        self.assertTrue(any("next master start" in w for w in d["warnings"]))
        self.assertEqual(read(self.files()[0]), REFERENCE)
        self.assertEqual(self.bus.written, [])

    def test_write_refused(self):
        d = self.steps_of(refuse_config=True)
        self.assertIsNone(d["error"])
        self.assertTrue(any("did not take" in w for w in d["warnings"]))
        self.assertEqual(read(self.files()[0]), REFERENCE)

    def test_written_nowhere_is_an_error(self):
        self.scan()
        os.makedirs(self.core.active_dir, exist_ok=True)
        with open(os.path.join(self.core.active_dir, "comm.ethercat.master"), "w") as f:
            f.write("a file where the folder belongs")
        self.bus.refuse_config = True
        s = self.write()
        self.assertIn("neither the master nor the files", s["download"]["error"])

    def test_broken_project_file_is_ignored(self):
        self.scan()
        os.makedirs(os.path.dirname(self.core.project_path), exist_ok=True)
        for content in ("no json", '{"devices": [{"vendor": 1}]}', '{"devices": 3}', "[]"):
            with open(self.core.project_path, "w") as f:
                f.write(content)
            self.assertIsNone(self.core._load_project(), content)
