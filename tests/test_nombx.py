# SPDX-License-Identifier: MIT

"""Devices without a mailbox, built from their ESI alone:  python3 -m unittest -q

The ctrlX I/O XI112108 (8 DI / 8 DO, tests/data/esi-nombx) joined the X3's bus on 2026-10-29. Its ESI has no
mailbox, process data on SM0 (outputs at #x0F01, ControlByte #x44) and SM1 (inputs at #x1000), and sync managers
and FMMUs marked OpOnly. Its bytes are checked against I/O Engineering in tests/test_8dev.py (2026-10-30).
"""

import os
import re
import unittest

from app import eni, esi, generate
from app.fake import DATA
from tests.test_app import read

ESI_FILE = os.path.join(DATA, "esi-nombx", "BoschRexroth_ctrlX_IO_XI112108.xml")
REFERENCE = eni.parse_eni(read(os.path.join(DATA, "controller-eni.xml")))


class NoMailboxTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.esi = esi.parse_device(ESI_FILE, 36, 0x242A2A, 0x100)
        cls.io = [generate.device_from_eni(s) for s in REFERENCE["slaves"]]

    def build(self, position):
        dev = generate.device_from_esi(self.esi, "XI112108")
        devices = self.io[:position] + [dev] + self.io[position:]
        x = generate.generate_eni(devices, "ethercatmaster", 2000).decode("latin-1")
        slaves = re.findall(r"<Slave>.*?</Slave>", x, re.S)
        return x, slaves, eni.parse_eni(x.encode("latin-1"))

    def test_esi(self):
        self.assertEqual([(s["type"], s["start"], s["control"], s["op_only"]) for s in self.esi["sm"]],
                         [("Outputs", 0x0F01, 0x44, True), ("Inputs", 0x1000, 0, False)])
        self.assertEqual((self.esi["fmmu"], self.esi["fmmu_op_only"]), (["Outputs", "Inputs"], [True, True]))
        self.assertIsNone(generate.device_from_esi(self.esi, "XI112108")["mailbox"])

    def test_device_without_mailbox(self):
        x, slaves, parsed = self.build(4)
        me = slaves[4]
        self.assertNotIn("<Mailbox>", me)
        self.assertNotIn("mailbox", me.lower().replace("<mailbox", ""))
        s = parsed["slaves"][4]
        self.assertEqual([(m["no"], m["type"], m["start"], m["size"], m["control"]) for m in s["sm"]],
                         [(0, "Outputs", 0x0F01, 1, 0x44), (1, "Inputs", 0x1000, 1, 0)])
        cmds = re.findall(r"<InitCmd>(.*?)</InitCmd>", me, re.S)

        def find(comment):
            c = [c for c in cmds if f"<Comment>{comment}</Comment>" in c]
            self.assertEqual(len(c), 1, comment)
            c = c[0]
            return (",".join(re.findall(r"<Transition>(\w+)</Transition>", c)), int(re.search(r"<Ado>(\d+)</Ado>", c).group(1)),
                    re.search(r"<Data>(\w*)</Data>", c).group(1))

        # OpOnly as I/O Engineering writes it (tests/test_8dev.py checks the bytes): the sync manager set up active
        # in SafeOp->Op, each process-data sync manager cleared in Op->SafeOp, FMMUs active from PreOp->SafeOp
        self.assertEqual(find("set sm 0 (outputs)"), ("SO", 0x800, "010F0100440001" + "00"))
        self.assertEqual(find("set sm 1 (inputs)"), ("PS", 0x808, "0010010000000100"))   # not OpOnly: PreOp->SafeOp
        self.assertEqual(len([c for c in cmds if "<Comment>clear output sm</Comment>" in c]), 2)
        self.assertFalse([c for c in cmds if "activate" in c])
        self.assertEqual(find("set fmmu 0(outputs)")[2][24:26], "01")        # FMMU written active
        self.assertEqual(len([c for c in cmds if "<Comment>set device state to BOOT</Comment>" in c]), 1)
        # only the 4 devices with a mailbox are in the cyclic mailbox-state read
        self.assertIn("<MailboxStates><StartAddr>524288</StartAddr><Count>4</Count></MailboxStates>", x)

    def test_mailbox_state_bits_skip_it(self):
        _x, slaves, _p = self.build(1)
        bits = [re.search(r"<StatusBitAddr>(\d+)</StatusBitAddr>", s) for s in slaves]
        self.assertEqual([int(b.group(1)) if b else None for b in bits], [0, None, 1, 2, 3])

    def test_devices_with_a_mailbox_unchanged(self):
        self.assertEqual(generate.generate_eni(self.io, "ethercatmaster", 2000),
                         read(os.path.join(DATA, "controller-eni.xml")))


class RealBusTest(unittest.TestCase):
    """The X3's bus as recorded on 2026-10-29 (tests/data/x3-bus-xi112108.json): coupler, 3 I/O modules and the
    new XI112108 on port B, the drive on port C; the master runs I/O Engineering's file without the XI112108."""

    def setUp(self):
        import json
        import tempfile
        from app.core import Core
        from app.fake import MASTER, FakeBus, recorded_nodes
        from tests.test_app import ESI_DIR, ESI_FILES
        with open(os.path.join(DATA, "x3-bus-xi112108.json"), encoding="utf-8") as f:
            slaves = json.load(f)["slaves"]
        nodes, _old = recorded_nodes()
        nodes[MASTER + "admin/fieldbus_status/num_connected_slaves"] = {"response": {"numSlaves": len(slaves)}}
        nodes[MASTER + "admin/fieldbus_config"] = {"response": {
            "eni": list(read(os.path.join(DATA, "x3-drive-ioe-telegram.xml"))),
            "ini": list(read(os.path.join(DATA, "controller-ini.txt")))}}
        self.dir = tempfile.TemporaryDirectory()
        self.core = Core(os.path.join(self.dir.name, "active", "io-config", "config.json"), FakeBus(nodes, slaves),
                         os.path.join(self.dir.name, "esi"), log=lambda m: None)
        for folder in (ESI_DIR, os.path.join(DATA, "esi-drive"), os.path.join(DATA, "esi-nombx")):
            for name in sorted(os.listdir(folder)):
                self.core.esi.add(name, read(os.path.join(folder, name)))
        self.core.start_scan()
        self.core.wait_scan()

    def tearDown(self):
        self.core.stop()
        self.dir.cleanup()

    def test_scan_and_plan(self):
        d = self.core.state()["devices"]
        self.assertEqual([x["product"] for x in d], [0x242A0F, 0x242A36, 0x242A1F, 0x242A2C, 0x242A2A, 0x21005311])
        self.assertEqual(d[4]["sources"]["esi"], "exact")
        p = self.core.plan()
        self.assertEqual(p["errors"], [])
        s = eni.parse_eni(self.core.plan_file("eni"))["slaves"]
        self.assertEqual([x["name"] for x in s], ["XB_EC_12", "XI190202", "XI422202", "XI522001", "XI112108",
                                                  "ctrlX_DRIVE_XCS2_F_H_SoE_"])
        self.assertEqual([(m["no"], m["type"], m["start"]) for m in s[4]["sm"]], [(0, "Outputs", 0x0F01), (1, "Inputs", 0x1000)])
        x = self.core.plan_file("eni").decode("latin-1")
        me = re.findall(r"<Slave>.*?</Slave>", x, re.S)[4]
        self.assertIn('<PreviousPort Selected="1"><Port>B</Port><PhysAddr>1004</PhysAddr>', me)
        self.assertNotIn("<Mailbox>", me)
        drive = re.findall(r"<Slave>.*?</Slave>", x, re.S)[5]
        self.assertIn('<PreviousPort Selected="1"><Port>C</Port><PhysAddr>1001</PhysAddr>', drive)
        # the edited telegram of the running configuration is kept for the drive
        self.assertIn("<Name>Homing: Speed during search for switch</Name>", drive)
        self.assertIn("<Data>0E000E0091002F0024005000C9882900CA88</Data>", drive)          # S-0-0024 as edited
        self.assertIn("ctrlX_DRIVE_XCS2_F_H_SoE_/MDT.Homing_Speed_during_search_for_switch", x)
        self.assertEqual(self.core.state()["devices"][5]["sources"]["eni"], "revision")       # paired, not "none"

    def test_saved_edit_follows_its_device(self):
        """An edit saved for the drive at position 4 (before the XI112108 was inserted) moves to position 5."""
        from app.configure import pair_in_order
        self.assertEqual(pair_in_order([[1], [2], [3]], [[1], [9], [2], [3]]), {0: 0, 1: 2, 2: 3})     # inserted
        self.assertEqual(pair_in_order([[1], [9], [2], [3]], [[1], [2], [3]]), {0: 0, 2: 1, 3: 2})     # removed
        import json
        os.makedirs(os.path.dirname(self.core.edits_path), exist_ok=True)
        old_bus = [[36, 0x242A0F], [36, 0x242A36], [36, 0x242A1F], [36, 0x242A2C], [36, 0x21005311]]
        with open(self.core.edits_path, "w", encoding="utf-8") as f:
            json.dump({"4": {"name": "Drive1", "identity": [36, 0x21005311, 3], "bus": old_bus}}, f)
        os.remove(self.core.project_path)               # an edits.json of 1.5.x without a list: it follows the bus once
        self.core.start_scan()
        self.core.wait_scan()
        edits = self.core.load_edits()
        self.assertEqual(list(edits), [5])
        self.assertEqual(edits[5]["bus"][4], [36, 0x242A2A])
        self.core.plan()
        self.assertEqual(eni.parse_eni(self.core.plan_file("eni"))["slaves"][5]["name"], "Drive1")
