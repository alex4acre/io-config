# SPDX-License-Identifier: MIT

"""The master's EoE endpoint (ini [EtherCAT.Master.EoE.Endpoint]) as a setting of the list:  python3 -m unittest -q

The X3 ran with Enabled=FALSE (2026-11-06), so io-config, which keeps the ini, could not switch EoE on. I/O Engineering
writes Enabled=TRUE, 172.31.254.254, 255.255.255.0 and an EoE "set IP parameter" command per EoE device.
"""

import os
import tempfile
import unittest

from app import eni, generate
from app.core import ConfigError, Core
from app.document import DocumentError
from app.fake import DATA, FakeBus
from tests.test_app import ESI_DIR, ESI_FILES, read

DRIVE_DIR = os.path.join(DATA, "esi-drive")
ON = {"enabled": True, "ip": "172.31.254.254", "mask": "255.255.255.0"}
OFF = dict(ON, enabled=False)
X3_OFF_INI = ("[EtherCAT.Master]\r\nConfigMode=ENI_FILE\r\nSyncMode=FREERUN\r\nCycleTimeUs=2000\r\n"
              "[EtherCAT.Master.EoE.Endpoint]\r\nEnabled=FALSE\r\n")


class IniTest(unittest.TestCase):

    def test_an_unchanged_setting_keeps_the_text(self):
        text = read(os.path.join(DATA, "x3-8dev-ioe.ini")).decode("latin-1")
        same = generate.set_eoe(text, generate.eoe_from_ini(eni.parse_ini(text)))
        self.assertEqual(same.replace("\n", "\r\n") if "\r\n" in text else same, text)

    def test_switch_on_the_x3_ini(self):
        text = generate.set_eoe(X3_OFF_INI, ON)
        self.assertEqual(generate.eoe_from_ini(eni.parse_ini(text)), ON)
        self.assertIn("[EtherCAT.Master.EoE.Endpoint]\nEnabled=TRUE\nIpAddress=172.31.254.254\nSubnetMask=255.255.255.0", text)
        self.assertEqual(eni.parse_ini(text)["EtherCAT.Master"]["SyncMode"], "FREERUN")       # the rest stays

    def test_switch_off(self):
        text = generate.set_eoe(generate.set_eoe(X3_OFF_INI, ON), OFF)
        self.assertEqual(text.count("Enabled="), 1)
        self.assertEqual(eni.parse_ini(text)["EtherCAT.Master.EoE.Endpoint"], {"Enabled": "FALSE"})

    def test_a_missing_section_goes_after_the_master_keys(self):
        text = generate.set_eoe("[EtherCAT.Master]\nCycleTimeUs=2000\n[EtherCAT.Master.DC]\nSendOffsetNs=1\n", ON)
        self.assertEqual(text.split("\n")[2], "[EtherCAT.Master.EoE.Endpoint]")
        self.assertEqual(eni.parse_ini(text)["EtherCAT.Master.DC"], {"SendOffsetNs": "1"})

    def test_from_an_ini_without_it(self):
        self.assertEqual(generate.eoe_from_ini({}), OFF)

    def test_checks(self):
        self.assertEqual(generate.check_eoe(ON), [])
        self.assertEqual(generate.check_eoe(dict(ON, mask="255.255.0.0")), [])
        for bad in ({"enabled": True}, dict(ON, enabled="yes"), dict(ON, ip="172.31.254"), dict(ON, ip="172.31.254.300"),
                    dict(ON, ip="172.31.254.0"), dict(ON, ip="127.0.0.5"), dict(ON, mask="255.0.255.0"),
                    dict(ON, mask="255.255.255.128"), dict(ON, ip="01.2.3.4"), None):
            self.assertTrue(generate.check_eoe(bad), bad)


class ListTest(unittest.TestCase):
    """The drive (EoE) on the recorded X3 bus; the simulated master runs with EoE on."""

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

    def built(self):
        self.core.plan(2000)
        return self.core.plan_file("eni").decode("latin-1"), self.core.plan_file("ini").decode("latin-1"), self.core.plan_data

    def test_from_the_running_ini_until_set(self):
        self.assertEqual(self.core.state()["configuration"]["eoe"], dict(ON, own=False))
        x, _ini, _p = self.built()
        self.assertEqual(x.count("eoe init"), 1)
        self.assertEqual(self.core.plan_file("eni"), read(os.path.join(DATA, "x3-drive-ioe.xml")))   # unchanged by the setting

    def test_off_and_on_again(self):
        self.assertEqual(self.core.set_project_eoe(OFF), dict(OFF, own=True))
        x, ini, _p = self.built()
        self.assertEqual((x.count("eoe init"), "<EoE>" in x.split("<Slave>")[0]), (0, False))
        self.assertIn("Enabled=FALSE", ini)
        self.core.set_project_eoe(ON)
        x, ini, _p = self.built()
        self.assertEqual(x.count("eoe init"), 1)
        self.assertIn("Enabled=TRUE\r\nIpAddress=172.31.254.254", ini)

    def test_another_network(self):
        self.core.set_project_eoe({"enabled": True, "ip": "10.5.0.200", "mask": "255.255.0.0"})
        x, ini, _p = self.built()
        self.assertIn("IpAddress=10.5.0.200\r\nSubnetMask=255.255.0.0", ini)
        data = x.split("eoe init")[1].split("<Data>")[1].split("</Data>")[0].upper()
        # the drive: 10.5.0.1, mask 255.255.0.0, gateway the master (byte order reversed, as ETG.1000.6 has it)
        self.assertIn("0100050A" + "0000FFFF" + "C800050A", data)

    def test_master_address_taken_by_a_device(self):
        self.core.set_project_eoe(dict(ON, ip="172.31.254.1"))
        p = self.core.plan(2000)
        self.assertRegex(" ".join(p["errors"]), "is one the EoE devices get")

    def test_reset_kept_through_list_changes_cleared_by_update(self):
        self.core.set_project_eoe(OFF)
        self.core.project_move(1, 1)
        self.assertEqual(self.core.project_eoe(), dict(OFF, own=True))
        self.core.set_project_eoe(None)
        self.assertEqual(self.core.project_eoe(), dict(ON, own=False))
        self.core.set_project_eoe(OFF)
        self.core.project_update()
        self.assertEqual(self.core.project_eoe()["own"], False)

    def test_saved_and_opened(self):
        self.core.set_project_eoe(OFF)
        self.core.save_saved("P", "proposed")
        self.core.set_project_eoe(None)
        self.core.open_saved("P")
        self.assertEqual(self.core.project_eoe(), dict(OFF, own=True))

    def test_refused(self):
        with self.assertRaises(ConfigError):
            self.core.set_project_eoe(dict(ON, mask="255.255.255.128"))

    def test_document(self):
        exported = self.core.export_project()
        self.assertEqual((exported["document"]["eoe"], exported["warnings"]), (ON, []))
        self.core.create_document(dict(exported["document"], name="Off", eoe=OFF))
        self.assertIn("Enabled=FALSE", self.core.saved_file("Off", "ini").decode("latin-1"))
        self.assertNotIn("eoe init", self.core.saved_file("Off", "eni").decode("latin-1"))
        self.assertEqual(self.core.export_saved("Off")["document"]["eoe"], OFF)
        with self.assertRaises(DocumentError):
            self.core.create_document(dict(exported["document"], name="Bad", eoe={"enabled": True}))


if __name__ == "__main__":
    unittest.main()
