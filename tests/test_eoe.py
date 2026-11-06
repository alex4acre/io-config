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

    def test_the_master_address_is_skipped(self):
        self.core.set_project_eoe(dict(ON, ip="172.31.254.1"))
        self.assertEqual(self.core.plan(2000)["errors"], [])
        self.assertEqual(self.core._eoe_map()[4]["ip"], "172.31.254.2")

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

    def test_refused_mask(self):
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


def _ip(text):
    return "".join(f"{int(x):02X}" for x in reversed(text.split(".")))


class DeviceTest(ListTest):
    """Each EoE device's own setting and the master's device range. The drive is position 4, address 1005."""

    def init_data(self):
        x = self.built()[0]
        if "eoe init" not in x:
            return None
        return x.split("eoe init")[1].split("<Data>")[1].split("</Data>")[0].upper()

    def test_default_as_io_engineering(self):
        data = self.init_data()
        self.assertEqual(data[:16], "020000002F000000")             # MAC, IP, mask, gateway, DNS name
        self.assertEqual(data[16:28], "026034FE03ED")               # 02:60:34:FE:<1005>
        self.assertEqual(data[28:52], _ip("172.31.254.1") + _ip("255.255.255.0") + _ip("172.31.254.254"))

    def test_own_address_gateway_dns_name_mac(self):
        options = self.core.save_edit(4, {"eoe": {"ip": "172.31.254.50", "gateway": "172.31.254.1", "dns": "8.8.8.8",
                                                  "dns_name": "drive1", "mac": "02-60-34-aa-bb-cc"}})
        self.assertEqual(options["eoe"]["settings"]["mac"], "026034AABBCC")
        self.assertEqual(options["eoe"]["effective"]["ip"], "172.31.254.50")
        data = self.init_data()
        self.assertEqual(data[:16], "020000003F000000")             # + DNS server
        self.assertEqual(data[16:28], "026034AABBCC")
        self.assertEqual(data[28:60], _ip("172.31.254.50") + _ip("255.255.255.0") + _ip("172.31.254.1") + _ip("8.8.8.8"))
        self.assertTrue(data[60:].startswith(b"drive1".hex().upper() + "00"))

    def test_switch_port(self):
        self.core.save_edit(4, {"eoe": {"mode": "switch"}})
        data = self.init_data()
        self.assertEqual(data[:16], "0200000001000000")             # the MAC only
        self.assertEqual(data[28:], "0" * (32 + 64))

    def test_off(self):
        self.core.save_edit(4, {"eoe": {"enabled": False}})
        self.assertIsNone(self.init_data())
        self.assertNotIn("<MaxPorts>", self.built()[0])            # no EoE device left: no EoE section at the master
        self.core.save_edit(4, {"eoe": None})                       # back to automatic
        self.assertIsNotNone(self.init_data())

    def test_device_range(self):
        self.core.set_project_eoe(dict(ON, devices={"first": "172.31.254.100", "mask": "255.255.0.0", "dns": "1.1.1.1"}))
        data = self.init_data()
        self.assertEqual(data[28:60], _ip("172.31.254.100") + _ip("255.255.0.0") + _ip("172.31.254.254") + _ip("1.1.1.1"))
        self.assertEqual(self.core.edit_options(4)["eoe"]["effective"]["ip"], "172.31.254.100")

    def test_range_exhausted(self):
        self.core.set_project_eoe(dict(ON, devices={"first": "172.31.254.10", "last": "172.31.254.10"}))
        self.core.save_edit(4, {"eoe": {}})
        self.assertEqual(self.core.plan(2000)["errors"], [])
        self.core.set_project_eoe(dict(ON, devices={"first": "172.31.254.254", "last": "172.31.254.254"}))   # the master's
        self.assertRegex(" ".join(self.core.plan(2000)["errors"]), "has no address left")
        self.assertRegex(self.core.edit_options(4)["eoe"]["error"], "has no address left")

    def test_refused(self):
        for bad, pattern in (({"ip": "1.2.3"}, "not an IPv4"), ({"mode": "x"}, "mode"), ({"mac": "0260"}, "MAC"),
                             ({"mask": "255.0.255.0"}, "valid mask"), ({"dns_name": "x" * 40}, "DNS name"),
                             ({"other": 1}, "may hold"), ({"enabled": "yes"}, "enabled")):
            with self.assertRaisesRegex(ValueError, pattern):
                self.core.save_edit(4, {"eoe": bad})
        with self.assertRaisesRegex(ValueError, "has no EoE"):
            self.core.save_edit(1, {"eoe": {"enabled": False}})
        with self.assertRaises(ConfigError):
            self.core.set_project_eoe(dict(ON, devices={"first": "172.31.254.9", "last": "172.31.254.1"}))

    def test_through_a_document(self):
        self.core.save_edit(4, {"eoe": {"ip": "172.31.254.77"}})
        self.core.set_project_eoe(dict(ON, devices={"dns": "1.1.1.1"}))
        exported = self.core.export_project()
        self.assertEqual(exported["warnings"], [])
        self.assertEqual(exported["document"]["devices"][4]["eoe"], {"ip": "172.31.254.77"})
        self.assertEqual(exported["document"]["eoe"]["devices"], {"dns": "1.1.1.1"})
        self.core.plan(2000)
        self.core.create_document(dict(exported["document"], name="Back"))
        self.assertEqual(self.core.saved_file("Back", "eni"), self.core.plan_file("eni"))

    def test_a_kept_mac_goes_into_the_document(self):
        """The running configuration gave the drive the MAC of another address: the export says so."""
        kept = "026034FE0400"
        self.core.save_edit(4, {"eoe": {"mac": kept}})
        self.assertEqual(self.core.export_project()["document"]["devices"][4]["eoe"], {"mac": kept})


if __name__ == "__main__":
    unittest.main()
