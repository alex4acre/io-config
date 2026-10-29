# SPDX-License-Identifier: MIT

"""Devices without a mailbox, built from their ESI alone:  python3 -m unittest -q

The ctrlX I/O XI112108 (8 DI / 8 DO, tests/data/esi-nombx) joined the X3's bus on 2026-10-29. Its ESI has no
mailbox, process data on SM0 (outputs at #x0F01, ControlByte #x44) and SM1 (inputs at #x1000), and sync managers
and FMMUs marked OpOnly. No I/O Engineering file for it yet, so this checks the structure, not the bytes.
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

        # OpOnly: set up inactive in PS, activated in SO, deactivated in OS
        self.assertEqual(find("set sm 0 (outputs)"), ("PS", 0x800, "010F0100440000" + "00"))
        self.assertEqual(find("activate sm 0 (outputs)"), ("SO", 0x806, "01"))
        self.assertEqual(find("deactivate sm 0 (outputs)"), ("OS", 0x806, "00"))
        self.assertEqual(find("set sm 1 (inputs)")[2][-4:], "0100")          # not OpOnly: active from SafeOp
        self.assertEqual(find("activate fmmu 0(outputs)"), ("SO", 0x60C, "01"))
        self.assertEqual(find("activate fmmu 1(inputs)"), ("SO", 0x61C, "01"))
        self.assertEqual(find("set fmmu 0(outputs)")[2][24:26], "00")        # FMMU written inactive
        # only the 4 devices with a mailbox are in the cyclic mailbox-state read
        self.assertIn("<MailboxStates><StartAddr>524288</StartAddr><Count>4</Count></MailboxStates>", x)

    def test_mailbox_state_bits_skip_it(self):
        _x, slaves, _p = self.build(1)
        bits = [re.search(r"<StatusBitAddr>(\d+)</StatusBitAddr>", s) for s in slaves]
        self.assertEqual([int(b.group(1)) if b else None for b in bits], [0, None, 1, 2, 3])

    def test_devices_with_a_mailbox_unchanged(self):
        self.assertEqual(generate.generate_eni(self.io, "ethercatmaster", 2000),
                         read(os.path.join(DATA, "controller-eni.xml")))
