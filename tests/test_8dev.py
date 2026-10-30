# SPDX-License-Identifier: MIT

"""The X3's bus on 2026-10-30, eight devices, against the ENI ctrlX I/O Engineering wrote for it:
python3 -m unittest -q

tests/data/x3-8dev-ioe.xml / .ini: coupler, XI190202, XI422202, XI522001, then three modules without a mailbox
(XI112108 8 DI / 8 DO, XI110116 16 DI, XI211116 16 DO on two 1-byte sync managers, OpOnly outputs), then the
ctrlX DRIVE (EoE MAC kept from when it was at address 1005). The three modules come from their ESI
(tests/data/esi-nombx), the others from the ENI, so this checks the generator's rules for devices without a
mailbox byte for byte: per-sync-manager sizes, OpOnly, BOOT commands, the kept EoE MAC.
"""

import os
import re
import unittest

from app import eni, esi, generate
from app.fake import DATA
from tests.test_app import read

REFERENCE = read(os.path.join(DATA, "x3-8dev-ioe.xml"))
INI = read(os.path.join(DATA, "x3-8dev-ioe.ini"))
FROM_ESI = {4: ("XI112108", 0x242A2A), 5: ("XI110116", None), 6: ("XI211116", None)}


def esi_device(name, product):
    path = os.path.join(DATA, "esi-nombx", f"BoschRexroth_ctrlX_IO_{name}.xml")
    if product is None:
        product = int(re.search(rb'ProductCode="#x([0-9A-Fa-f]+)"', read(path)).group(1), 16)
    return generate.device_from_esi(esi.parse_device(path, 36, product, 0x100), name)


class EightDevicesTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.parsed = eni.parse_eni(REFERENCE)
        devices = []
        for pos, slave in enumerate(cls.parsed["slaves"]):
            devices.append(esi_device(*FROM_ESI[pos]) if pos in FROM_ESI else generate.device_from_eni(slave))
        # the drive as the app describes it: its ESI (other revision) with the running configuration's choices on top
        drive = cls.parsed["slaves"][7]
        path = os.path.join(DATA, "esi-drive", "BoschRexroth_ctrlX_DRIVE_XCS2_F_H_SoE.xml")
        dev = generate.apply_eni(generate.device_from_esi(esi.parse_device(path, drive["vendor"], drive["product"],
                                                                           0x06000003), ""), drive)
        devices[7] = dict(dev, revision=drive["revision"])
        cls.devices = devices
        ini = eni.parse_ini(INI.decode("latin-1"))
        cycle = int(ini["EtherCAT.Master"]["CycleTimeUs"])
        ports = {}
        for pos, x in enumerate(re.findall(rb"<Slave>.*?</Slave>", REFERENCE, re.S)):
            m = re.search(rb"<PreviousPort Selected=\"1\"><Port>(\w)</Port><PhysAddr>(\d+)</PhysAddr>", x)
            if m:
                ports[pos] = (m.group(1).decode(), next(i for i, s in enumerate(cls.parsed["slaves"])
                                                         if s["address"] == int(m.group(2))))
        cls.eni = generate.generate_eni(devices, "ethercatmaster", cycle, ports, generate.eoe_addresses(ini, devices))
        cls.ini = generate.generate_ini(INI.decode("latin-1"), cycle)

    def slaves(self, data):
        return re.findall(rb"<Slave>.*?</Slave>", data, re.S)

    def test_each_device_byte_for_byte(self):
        for pos, (mine, ref) in enumerate(zip(self.slaves(self.eni), self.slaves(REFERENCE))):
            self.assertEqual(mine.decode("latin-1"), ref.decode("latin-1"), f"slave {pos}")

    def test_whole_eni_and_ini(self):
        self.assertEqual(self.eni, REFERENCE)
        self.assertEqual(self.ini, INI)

    def test_xi211116_sync_managers(self):
        s = eni.parse_eni(self.eni)["slaves"][6]
        self.assertEqual([(m["start"], m["size"]) for m in s["sm"]], [(0x0F00, 1), (0x0F01, 1)])

    def test_eni_round_trip_keeps_op_only_and_mac(self):
        """The same bus described only from the reference ENI is written the same way again."""
        devs = [generate.device_from_eni(s) for s in self.parsed["slaves"]]
        self.assertEqual([sm["op_only"] for sm in devs[6]["sm"]], [True, True])
        self.assertEqual(devs[7]["eoe_mac"], "026034FE03ED")


if __name__ == "__main__":
    unittest.main()
