# SPDX-License-Identifier: MIT

"""The project: a configuration for devices that aren't on the bus, also without a working master.  python3 -m unittest -q

Absent devices stay in the configuration with a warning, devices can be added from the ESI library, and writing
is best effort: what the master doesn't do is a warning, the configuration then waits in the active configuration's files.
Once the list is changed it is a configuration ("Configuration" state): exactly the list in wiring order, no live data.
"""

import json
import os
import tempfile
import unittest

import app.configure
from app import eni
from app.configure import merge, move_entry, normalise, remove_entry
from app.core import ConfigError, Core
from app.fake import DATA, MASTER, FakeBus
from tests.test_app import ESI_DIR, ESI_FILES, read

DRIVE_DIR = os.path.join(DATA, "esi-drive")

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


def chain(n=4):
    return [{"uid": i, "upstream": i - 1 if i else None, "port": "B" if i else None} for i in range(n)]


def wiring(entries):
    return [(e["uid"], e["upstream"], e["port"]) for e in entries]


class WiringTest(unittest.TestCase):

    def test_move_down_and_up_in_a_line(self):
        e = chain()
        move_entry(e, 1, 2)
        self.assertEqual(wiring(e), [(0, None, None), (2, 0, "B"), (1, 2, "B"), (3, 1, "B")])
        move_entry(e, 2, 1)
        self.assertEqual(wiring(e), wiring(chain()))

    def test_move_to_the_top_and_bottom(self):
        e = chain()
        move_entry(e, 3, 0)
        self.assertEqual(wiring(e), [(3, None, None), (0, 3, "B"), (1, 0, "B"), (2, 1, "B")])
        move_entry(e, 0, 3)
        self.assertEqual(wiring(e), [(0, 3, None)[:1] + (None, None), (1, 0, "B"), (2, 1, "B"), (3, 2, "B")])

    def test_children_of_the_moved_device(self):
        e = chain()
        e[2]["upstream"], e[2]["port"] = 0, "C"            # 2 hangs on port C of 0, 3 behind 2
        move_entry(e, 0, 1)                                 # the coupler moves down: 1 (its first child) takes its place
        self.assertEqual(wiring(e)[0], (1, None, None))
        self.assertEqual(wiring(e)[1], (0, 1, "B"))
        self.assertEqual(wiring(e)[2], (2, 0, "C"))
        self.assertEqual(wiring(e)[3], (3, 2, "B"))

    def test_normalise(self):
        e = chain()
        e[1]["upstream"] = 3                                # upstream later in the list
        e[2]["upstream"], e[2]["port"] = 0, "B"             # port B of 0 is taken by 1 ... (after 1 is fixed: 1 is (0,B))
        e[3]["port"] = None                                 # no port
        normalise(e)
        self.assertEqual(wiring(e), [(0, None, None), (1, 0, "B"), (2, 1, "B"), (3, 2, "B")])

    def test_remove(self):
        e = chain()
        remove_entry(e, 1)
        self.assertEqual(wiring(e), [(0, None, None), (2, 0, "B"), (3, 2, "B")])
        e = chain()
        e[3]["upstream"], e[3]["port"] = 1, "C"
        e[2]["upstream"] = 1                                # two children on 1: 2 (port B) and 3 (port C)
        remove_entry(e, 1)
        self.assertEqual(wiring(e), [(0, None, None), (2, 0, "B"), (3, 2, "B")])
        remove_entry(e, 0)
        self.assertEqual(wiring(e), [(2, None, None), (3, 2, "B")])


class DriveProjectTest(unittest.TestCase):
    """The ctrlX DRIVE sits on port C of the coupler (FakeBus(drive=True))."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.bus = FakeBus(drive=True)
        self.core = Core(os.path.join(self.dir.name, "activeConfiguration", "io-config", "config.json"), self.bus,
                         os.path.join(self.dir.name, "esi"), log=lambda m: None)
        for folder, names in ((ESI_DIR, ESI_FILES), (DRIVE_DIR, sorted(os.listdir(DRIVE_DIR)))):
            for name in names:
                self.core.esi.add(name, read(os.path.join(folder, name)))
        self.core.start_scan()
        self.core.wait_scan()
        for d in self.core.state()["configuration"]["unconfigured"]:
            self.core.project_add_from_bus(d["bus_pos"])

    def tearDown(self):
        self.core.stop()
        self.dir.cleanup()

    def previous(self):
        self.core.plan()
        return [(s["name"], s["previous"]) for s in eni.parse_eni(self.core.plan_file("eni"))["slaves"]]

    def test_port_c_device_moved_away_and_back_gets_its_port_again(self):
        before = self.previous()
        self.assertEqual(before[4][1], ("C", 1001))
        self.core.project_move(4, -1)                               # the drive moves up between two I/O devices
        self.assertEqual(self.core.state()["configuration"]["devices"][3]["name"][:4], "ctrl")
        moved = self.previous()
        self.assertEqual(moved[3][1], ("B", 1003))                  # not paired with the bus any more: behind its neighbour
        self.assertEqual(moved[4][1], ("B", 1004))                  # the I/O device that took its place chains after it
        self.core.project_move(3, 1)
        self.assertEqual(self.previous(), before)
        p = self.core.plan()
        self.assertEqual(p["warnings"], [])

    def test_device_added_behind_the_drive_hangs_on_its_port_b(self):
        self.core.project_add(*[self.core.esi.search("XI190202", limit=1)[0][k] for k in ("vendor", "product", "revision")])
        slaves = self.previous()
        self.assertEqual(slaves[5][1], ("B", 1005))                         # behind the drive (address 1005)


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

    def entries(self):
        with open(self.core.project_path, encoding="utf-8") as f:
            return json.load(f)["devices"]

    def names(self):
        return [d["name"] for d in self.core.state()["configuration"]["devices"]]

    def bus_names(self):
        return [d["name"] for d in self.core.state()["devices"]]

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
        self.assertEqual(s["devices"][0]["sources"]["eni"], "same")
        p = self.core.plan()
        self.assertTrue(p["identical"])
        self.assertEqual(self.core.plan_file("eni"), REFERENCE)
        self.assertEqual(p["removed_devices"], [])
        self.assertEqual(p["warnings"], ["Position 1: XI190202 is not on the bus at the last scan"])

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

    def test_add_appends_and_keeps_the_others(self):
        self.scan()
        self.core.save_edit(2, {"name": "Analog"})
        self.assertEqual(self.add("XI522001"), 4)
        s = self.core.state()["configuration"]
        self.assertEqual([d["present"] for d in s["devices"]], [True] * 4 + [False])       # the added one isn't on the bus
        self.assertEqual([d["state"] for d in s["devices"]], ["-"] * 5)
        self.assertEqual(self.names(), ["XB_EC_12", "XI190202", "Analog", "XI522001", "XI522001_2"])
        self.assertEqual(self.core.edit_options(2)["name"], "Analog")
        p = self.core.plan()
        self.assertEqual(p["devices"][4]["name"], "XI522001_2")                 # the running one keeps its name
        old, new = eni.parse_eni(REFERENCE), eni.parse_eni(self.core.plan_file("eni"))
        for i in (0, 1, 3):                                                      # appending changes nothing before it
            a, b = new["slaves"][i], old["slaves"][i]
            self.assertEqual((a["pdos"], a["sm"], a["coe_init"], a["variables"]), (b["pdos"], b["sm"], b["coe_init"], b["variables"]))
        self.assertEqual(new["slaves"][2]["name"], "Analog")
        self.assertEqual(p["warnings"], ["Position 4: XI522001_2 is not on the bus at the last scan"])
        self.scan()                                 # a rescan changes nothing about the list
        self.assertEqual(self.names(), ["XB_EC_12", "XI190202", "Analog", "XI522001", "XI522001_2"])
        self.assertTrue(self.core.state()["configuration"]["differs"])

    def test_add_then_remove_is_the_running_configuration(self):
        self.scan()
        self.add("XI190202")
        self.core.project_remove(4)
        p = self.core.plan()
        self.assertTrue(p["identical"])
        self.assertEqual(self.core.plan_file("eni"), REFERENCE)
        self.assertEqual(p["warnings"], [])

    def test_port_of_an_added_device(self):
        self.scan()
        self.add("XI190202")
        self.core.plan()
        parsed = eni.parse_eni(self.core.plan_file("eni"))
        self.assertEqual(parsed["slaves"][4]["previous"], ("B", parsed["slaves"][3]["address"]))     # port B of the device above
        self.assertEqual(self.entries()[4]["port"], "B")
        self.assertEqual(self.entries()[4]["upstream"], self.entries()[3]["uid"])

    def test_add_checks(self):
        self.scan()
        with self.assertRaisesRegex(ConfigError, "ESI library"):
            self.core.project_add(1, 2, 3)
        for bad in ("1abc", "a b", "x" * 65, 7):
            with self.assertRaisesRegex(ConfigError, "name must start"):
                self.add("XI190202", name=bad)
        self.assertEqual(self.names(), ["XB_EC_12", "XI190202", "XI422202", "XI522001"])
        self.assertEqual(self.core.load_edits(), {})

    def test_name_at_add(self):
        self.scan()
        self.assertEqual(self.add("XI190202", name="Inputs_2"), 4)
        self.assertEqual(self.names()[4], "Inputs_2")
        self.assertEqual(self.core.edit_options(4)["name"], "Inputs_2")
        self.assertEqual(self.core.plan()["devices"][4]["name"], "Inputs_2")

    def test_move_any_device(self):
        self.scan()
        self.assertEqual(self.core.project_move(1, 1), 2)
        self.assertEqual(self.names(), ["XB_EC_12", "XI422202", "XI190202", "XI522001"])
        self.assertEqual(self.core.project_move(0, 1), 1)               # even the coupler
        self.assertEqual(self.names(), ["XI422202", "XB_EC_12", "XI190202", "XI522001"])
        self.assertEqual(self.core.project_move(0, -1), 0)              # past the top: nothing
        self.assertEqual(self.core.project_move(3, 5), 3)
        with self.assertRaises(ConfigError):
            self.core.project_move(9, 1)
        wiring_now = [(e["upstream"], e["port"]) for e in self.entries()]
        self.assertEqual(wiring_now[0], (None, None))
        self.assertTrue(all(p == "B" for _u, p in wiring_now[1:]))

    def test_move_down_and_up_is_identical(self):
        self.scan()
        self.core.project_move(1, 1)
        p = self.core.plan()
        self.assertFalse(p["identical"])
        self.assertEqual(len(p["warnings"]), 2)                     # moved: it warns on both sides
        self.assertRegex(p["warnings"][0], r"^Position \d: XI\w+ is not on the bus at the last scan$")
        self.assertRegex(p["warnings"][1], "^XI.+ is on the bus but not in this configuration$")
        self.core.project_move(2, -1)
        p = self.core.plan()
        self.assertTrue(p["identical"])
        self.assertEqual(self.core.plan_file("eni"), REFERENCE)
        self.assertEqual(p["warnings"], [])

    def test_moved_device_keeps_its_settings(self):
        """Linked to the running configuration by station address, not by position or type order."""
        self.scan()
        self.core.project_move(1, 1)
        self.assertEqual([s["eni_slave"]["name"] for s in self.core.cfg_sources], ["XB_EC_12", "XI422202", "XI190202", "XI522001"])
        self.assertEqual(self.core.cfg_sources[1]["eni_slave"]["address"], 1003)
        self.assertEqual([e["address"] for e in self.entries()], [1001, 1003, 1002, 1004])
        # an entry without address key (project from before) is paired by type and order as in the live list
        with open(self.core.project_path) as f:
            data = json.load(f)
        for e in data["devices"]:
            del e["address"]
        with open(self.core.project_path, "w") as f:
            json.dump(data, f)
        self.core._project_changed()
        self.assertEqual([s["eni_slave"]["name"] for s in self.core.cfg_sources if s["eni_slave"]].count("XB_EC_12"), 1)
        self.assertEqual(len([s for s in self.core.cfg_sources if s["eni_slave"]]), 3)

    def test_edits_follow_a_move_with_duplicate_types(self):
        self.scan()
        self.add("XI190202")                                        # a second XI190202 at the bottom
        self.core.save_edit(1, {"name": "Bus_one"})
        self.core.save_edit(4, {"name": "New_one"})
        self.assertEqual(self.core.project_move(4, -3), 1)          # the two of one type swap places
        self.assertEqual(self.names(), ["XB_EC_12", "New_one", "Bus_one", "XI422202", "XI522001"])
        self.assertEqual(self.core.edit_options(1)["name"], "New_one")
        self.assertEqual(self.core.edit_options(2)["name"], "Bus_one")
        self.assertEqual(sorted(self.core.load_edits()), [1, 2])
        self.scan()                                                 # nothing guesses again
        self.assertEqual(self.names(), ["XB_EC_12", "New_one", "Bus_one", "XI422202", "XI522001"])
        self.core.project_remove(1)                                 # a removed device's edit goes with it
        self.assertEqual(self.names(), ["XB_EC_12", "Bus_one", "XI422202", "XI522001"])
        self.assertEqual(sorted(self.core.load_edits()), [1])
        self.assertEqual(self.core.plan()["devices"][1]["name"], "Bus_one")

    def test_bus_device_not_in_the_list(self):
        self.scan()
        self.core.project_remove(1)                                 # XI190202 is on the bus but leaves the list
        self.assertEqual(self.names(), ["XB_EC_12", "XI422202", "XI522001"])
        p = self.core.plan()
        self.assertEqual(len(p["devices"]), 3)
        self.assertEqual(len(p["warnings"]), 1)
        self.assertRegex(p["warnings"][0], "^.+ is on the bus but not in this configuration$")
        self.assertEqual(p["removed_devices"], ["XI190202"])
        self.scan()                                                 # a scan doesn't put it back
        self.assertEqual(self.names(), ["XB_EC_12", "XI422202", "XI522001"])
        self.core.project_update()
        self.assertEqual(len(self.core.state()["configuration"]["devices"]), 4)

    def test_remove_all_devices(self):
        self.scan()
        for _ in range(4):
            self.core.project_remove(0)
        self.assertEqual(self.core.state()["configuration"]["devices"], [])
        p = self.core.plan()
        self.assertEqual(p["errors"], ["no devices"])
        self.assertEqual(p["devices"], [])
        self.add("XB-EC-12")                                        # and a list can start again
        self.assertEqual(self.entries()[0]["upstream"], None)
        self.assertEqual(self.core.plan()["errors"], [])

    def test_bus_keeps_its_online_access_while_the_list_changes(self):
        self.scan()
        self.add("XI190202")
        self.core.project_move(1, 1)
        self.assertEqual(self.core._autoinc(1)["address"], (-1) & 0xFFFF)       # the Bus view's positions
        self.assertIn("variables", self.core.process_data(1))
        self.assertTrue(self.core.state()["devices"][1]["present"])
        self.assertEqual(self.bus_names(), ["XB_EC_12", "XI190202", "XI422202", "XI522001"])

    def test_cfg_position_maps_a_bus_device_to_its_row(self):
        self.scan()
        self.assertEqual([self.core.cfg_position(i) for i in range(4)], [0, 1, 2, 3])
        self.core.project_remove(1)                                 # XI190202 leaves the list; the others move up
        self.assertEqual([self.core.cfg_position(i) for i in (0, 2, 3)], [0, 1, 2])
        with self.assertRaisesRegex(ConfigError, "not in the Configuration list"):
            self.core.cfg_position(1)

    def test_rename_shows_in_the_list_not_on_the_bus(self):
        self.scan()
        self.core.save_edit(1, {"name": "Inputs"})
        s = self.core.state()
        self.assertEqual(self.names()[1], "Inputs")
        self.assertEqual(self.bus_names()[1], "XI190202")
        self.assertTrue(s["devices"][1]["present"])
        self.core.reset_edit(1)
        self.assertEqual(self.names()[1], "XI190202")

    def new_device_on_the_bus(self):
        """A ctrlX DRIVE joins the bus after the list exists."""
        from app.fake import add_drive
        for name in sorted(os.listdir(DRIVE_DIR)):
            self.core.esi.add(name, read(os.path.join(DRIVE_DIR, name)))
        add_drive(self.bus.nodes, self.bus.slaves)

    def test_rescan_with_a_new_device_leaves_the_list_and_flags_it(self):
        self.scan()
        self.core.save_edit(2, {"name": "Analog"})
        self.assertFalse(self.core.state()["configuration"]["differs"])
        self.new_device_on_the_bus()
        s = self.scan()
        self.assertEqual(len(s["devices"]), 5)                      # the Bus view shows it ...
        self.assertEqual((s["devices"][4]["present"], s["devices"][4]["sources"]["eni"]), (True, "none"))   # ... not in the active configuration
        self.assertEqual(len(s["configuration"]["devices"]), 4)     # the list doesn't change
        self.assertTrue(s["configuration"]["differs"])
        self.assertEqual(self.names()[2], "Analog")
        p = self.core.plan()
        self.assertEqual(len(p["warnings"]), 1)
        self.assertRegex(p["warnings"][0], "is on the bus but not in this configuration")

    def test_update_takes_the_running_configuration_and_clears_the_edits(self):
        self.scan()
        self.core.save_edit(2, {"name": "Analog"})
        self.new_device_on_the_bus()
        self.scan()
        self.core.project_update()
        c = self.core.state()["configuration"]
        self.assertEqual(len(c["devices"]), 4)                      # the new device stays out ...
        self.assertEqual([(d["configured"], d["present"], d["position"]) for d in c["unconfigured"]], [(False, True, None)])
        self.assertTrue(c["differs"])
        self.assertEqual(self.core.load_edits(), {})
        self.assertEqual(self.names()[2], "XI422202")
        self.core.project_add_from_bus(c["unconfigured"][0]["bus_pos"])         # ... until it is added
        c = self.core.state()["configuration"]
        self.assertEqual((len(c["devices"]), c["unconfigured"], c["differs"]), (5, [], False))
        self.assertEqual(self.core.plan()["warnings"], [])

    def test_update_keeps_a_missing_configured_device(self):
        self.scan()
        unplug(self.bus, XI190202)
        self.scan()
        self.core.project_update()
        c = self.core.state()["configuration"]
        self.assertEqual([(d["name"], d["configured"], d["present"]) for d in c["devices"]],
                         [("XB_EC_12", True, True), ("XI190202", True, False), ("XI422202", True, True), ("XI522001", True, True)])
        self.assertTrue(c["scanned"])
        self.assertEqual(c["unconfigured"], [])

    def test_first_list_leaves_a_new_device_out_and_its_edit_follows(self):
        self.scan()
        self.core.save_edit(3, {"name": "Serial"})
        self.new_device_on_the_bus()
        os.remove(self.core.project_path)
        c = self.scan()["configuration"]
        self.assertEqual(len(c["devices"]), 4)
        self.assertEqual(c["devices"][3]["name"], "Serial")
        self.assertEqual(len(c["unconfigured"]), 1)

    def test_removing_a_present_device_makes_it_not_configured(self):
        self.scan()
        self.core.project_remove(1)
        c = self.core.state()["configuration"]
        self.assertEqual([(d["name"], d["bus_pos"], d["before"]) for d in c["unconfigured"]], [("XI190202", 1, 1)])
        self.assertEqual(self.core.project_add_from_bus(1), 1)
        self.assertEqual(self.names(), ["XB_EC_12", "XI190202", "XI422202", "XI522001"])
        self.assertTrue(self.core.plan()["identical"])
        with self.assertRaisesRegex(ConfigError, "not in the list"):
            self.core.project_add_from_bus(1)

    def test_a_device_with_settings_keeps_them_when_another_is_added_before_it(self):
        self.scan()
        self.core.project_remove(1)
        self.core.save_edit(1, {"name": "Analog"})                  # XI422202, now at list position 1
        self.core.project_add_from_bus(1)
        self.assertEqual(self.names(), ["XB_EC_12", "XI190202", "Analog", "XI522001"])

    def test_build_warns_when_the_cycle_differs_from_the_scheduler_task(self):
        self.scan()
        self.assertEqual(self.core.plan()["warnings"], [])
        warnings = self.core.plan(1000)["warnings"]
        self.assertEqual(len(warnings), 1)
        self.assertRegex(warnings[0], r"1000 µs differs from the scheduler task ctrlXAutomation \(2000 µs\)")

    def test_build_warns_when_the_scheduler_task_cant_be_read(self):
        self.scan()
        del self.bus.nodes["scheduler/tasks/ctrlXAutomation/properties"]
        p = self.core.plan()
        self.assertEqual(p["errors"], [])
        self.assertRegex(p["warnings"][0], "ctrlXAutomation could not be read .*check that it is 2000 µs")

    def test_update_on_a_matching_bus_is_identical(self):
        self.scan()
        self.core.project_move(1, 1)
        self.core.save_edit(2, {"name": "Analog"})
        self.core.project_update()
        self.assertEqual(self.names(), ["XB_EC_12", "XI190202", "XI422202", "XI522001"])
        p = self.core.plan()
        self.assertTrue(p["identical"])
        self.assertEqual(self.core.plan_file("eni"), REFERENCE)

    def test_update_is_refused_while_busy(self):
        self.scan()
        self.core.download_state["running"] = True
        try:
            with self.assertRaises(ConfigError):
                self.core.project_update()
            with self.assertRaisesRegex(ConfigError, "being written"):
                self.core.project_move(1, 1)
        finally:
            self.core.download_state["running"] = False

    def test_base_survives_an_external_change_of_the_running_configuration(self):
        self.scan()
        self.core.save_edit(2, {"name": "Analog"})
        other = read(os.path.join(DATA, "x3-drive-ioe.xml"))
        self.bus.nodes[MASTER + "admin/fieldbus_config"]["response"]["eni"] = list(other)
        self.scan()
        p = self.core.plan()
        self.assertFalse(p["identical"])
        self.assertIn("The master's configuration changed since this list was started", p["warnings"])
        x = eni.parse_eni(self.core.plan_file("eni"))
        self.assertEqual([s["name"] for s in x["slaves"]], ["XB_EC_12", "XI190202", "Analog", "XI522001"])
        self.assertEqual(x["slaves"][3]["coe_init"], eni.parse_eni(REFERENCE)["slaves"][3]["coe_init"])

    def test_project_without_a_base_uses_the_running_configuration(self):
        """A project.json of 1.6.x has no base files."""
        self.scan()
        self.core._remove_base()
        self.core._assemble()
        self.assertIsNone(self.core._load_base())
        self.assertEqual(self.core.cfg_sources[1]["eni_slave"]["name"], "XI190202")
        self.assertTrue(self.core.plan()["identical"])

    def test_the_list_is_created_once_there_is_data(self):
        self.assertFalse(self.core.state()["configuration"]["exists"])         # nothing known before the first scan
        self.assertFalse(os.path.exists(self.core.project_path))
        self.scan()
        self.assertTrue(os.path.exists(self.core.project_path))
        self.assertEqual(self.names(), ["XB_EC_12", "XI190202", "XI422202", "XI522001"])
        self.assertTrue(os.path.exists(self.core.base_paths[0]))

    # --- unique names ---------------------------------------------------------------------------------

    def plan_names(self):
        p = self.core.plan()
        self.assertEqual(p["errors"], [])
        return [d["name"] for d in p["devices"]]

    def test_table_names_are_the_names_build_uses(self):
        """Two same-type devices without names, an explicit name equal to another device's default, a moved device."""
        self.scan()
        self.add("XI190202")                                        # default XI190202_2
        self.add("XI422202")                                        # default XI422202_2
        self.assertEqual(self.names()[-2:], ["XI190202_2", "XI422202_2"])
        self.assertEqual(self.names(), self.plan_names())
        self.core.project_move(5, -1)                               # the new XI422202 moves above the new XI190202
        self.assertEqual(self.names()[4:], ["XI422202_2", "XI190202_2"])        # defaults stay where they can
        self.assertEqual(self.names(), self.plan_names())
        self.core.save_edit(5, {"name": "XI422202_2x"})
        self.core.save_edit(1, {"name": "XI190202_2"})              # an explicit name equal to a default further down
        self.assertEqual(self.names()[1], "XI190202_2")
        self.assertEqual(self.names()[4:], ["XI422202_2", "XI422202_2x"])
        self.assertEqual(len({n.lower() for n in self.names()}), 6)
        self.assertEqual(self.names(), self.plan_names())

    def test_explicit_name_wins_over_a_default_further_down(self):
        self.scan()
        self.add("XI190202")                                        # position 4, default XI190202_2
        self.assertEqual(self.names()[4], "XI190202_2")
        self.core.save_edit(1, {"name": "Digital"})                 # nobody has XI190202 now: the default is plain
        self.assertEqual(self.names()[4], "XI190202")
        self.assertEqual(self.names(), self.plan_names())
        self.core.reset_edit(1)
        self.assertEqual(self.names()[4], "XI190202_2")
        with self.assertRaisesRegex(ConfigError, "already used by position 1"):
            self.core.save_edit(4, {"name": "XI190202"})            # position 1 has that name as displayed

    def test_duplicate_names_are_refused(self):
        self.scan()
        with self.assertRaisesRegex(ConfigError, 'the name "xi422202" is already used by position 2'):
            self.core.save_edit(1, {"name": "xi422202"})               # case does not matter
        self.core.save_edit(1, {"name": "Valve1"})
        with self.assertRaisesRegex(ConfigError, 'the name "VALVE1" is already used by position 1'):
            self.core.save_edit(2, {"name": "VALVE1"})
        self.core.save_edit(1, {"name": "VALVE1"})                  # its own name, only the case changes
        self.assertEqual(self.names()[1], "VALVE1")
        self.core.save_edit(1, {"name": "VALVE1"})                  # keeping it
        with self.assertRaisesRegex(ConfigError, 'already used by position 1'):
            self.add("XI190202", name="valve1")
        self.assertEqual(len(self.names()), 4)                      # nothing changed
        self.assertEqual(self.core.load_edits()[1]["name"], "VALVE1")

    def test_old_duplicates_stay_and_build_names_them(self):
        self.scan()

        def identity(i):
            info = self.core.cfg_sources[i]["online"]["response"]["slaveIdentity"]
            return [info[k] for k in ("vendorId", "productCode", "revisionNumber")]
        self.core._save_edits({1: {"name": "Same", "identity": identity(1)}, 2: {"name": "same", "identity": identity(2)}})
        self.core._assemble_cfg()
        self.assertEqual(self.names()[1:3], ["Same", "same"])           # not renamed
        p = self.core.plan()
        self.assertEqual(len(p["errors"]), 1)
        self.assertIn('"Same" is used by positions 1, 2', p["errors"][0])

    def test_edit_options_show_the_displayed_name(self):
        self.scan()
        self.add("XI190202")
        self.assertEqual(self.core.edit_options(4)["name"], "XI190202_2")

    def test_written_list_stays_identical_and_keeps_its_settings(self):
        self.scan()
        self.core.save_edit(2, {"name": "Analog"})
        self.core.project_move(1, 1)
        s = self.write()
        self.assertIsNone(s["download"]["error"])
        self.assertEqual(self.names(), ["XB_EC_12", "Analog", "XI190202", "XI522001"])
        self.assertEqual(self.core.load_edits()[1]["name"], "Analog")
        p = self.core.plan()
        self.assertTrue(p["identical"])
        self.assertEqual(self.core.plan_file("eni"), bytes(self.bus.nodes[MASTER + "admin/fieldbus_config"]["response"]["eni"]))
        self.assertEqual([e["address"] for e in self.core._load_project()["devices"]], [1001, 1002, 1003, 1004])

    def test_changed_master_configuration_is_a_warning(self):
        self.scan()
        self.add("XI190202")
        other = read(os.path.join(DATA, "x3-drive-ioe.xml"))
        self.bus.nodes[MASTER + "admin/fieldbus_config"]["response"]["eni"] = list(other)
        self.scan()
        self.assertIn("The master's configuration changed since this list was started", self.core.plan()["warnings"])

    def test_good_write_keeps_the_list(self):
        self.scan()
        self.add("XI190202")
        s = self.write()
        self.assertIsNone(s["download"]["error"])
        self.assertEqual(s["download"]["warnings"], [])
        self.assertTrue(s["configuration"]["exists"])
        self.assertEqual([d["present"] for d in s["devices"]], [True, True, True, True, False])
        self.assertEqual(s["devices"][4]["name"], "XI190202_2")         # the Bus view: from the running configuration
        self.assertEqual(self.names()[4], "XI190202_2")                 # the list: from its new base
        self.assertTrue(self.core.plan()["identical"])

    # --- without a working master -----------------------------------------------------------------------

    def test_offline_project_without_a_master(self):
        self.bus.gone = True
        s = self.scan()
        self.assertIn("no EtherCAT master instance", s["scan"]["error"])
        self.assertEqual(s["master"]["state"], "unavailable")
        self.assertEqual(self.add("XB-EC-12"), 0)                   # the list starts empty without data
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
        self.assertTrue(s["configuration"]["exists"])
        self.assertEqual([x["name"] for x in s["configuration"]["devices"]], ["XB_EC_12", "Inputs"])

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

    def test_lost_master_doesnt_touch_the_list_or_its_edits(self):
        self.scan()
        self.core.save_edit(3, {"name": "Serial"})
        self.bus.gone = True
        self.scan()
        self.assertIn(3, self.core.load_edits())
        self.assertEqual(self.names()[3], "Serial")
        self.assertEqual(len(self.names()), 4)

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
