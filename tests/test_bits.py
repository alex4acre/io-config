# SPDX-License-Identifier: MIT

"""Modules with less than a whole byte of process data, built from their ESI:  python3 -m unittest -q

XI213204 has 4 BOOL outputs and XI110204 has 4 inputs, each with no padding entries (tests/data/esi-bits).
No I/O Engineering reference for such a module yet: this checks the structure, not the bytes.
"""

import os
import re
import unittest

from app import eni, esi, generate
from app.fake import DATA

DO4 = os.path.join(DATA, "esi-bits", "BoschRexroth_ctrlX_IO_XI213204.xml")
DI4 = os.path.join(DATA, "esi-bits", "BoschRexroth_ctrlX_IO_XI110204.xml")
XI112108 = os.path.join(DATA, "esi-nombx", "BoschRexroth_ctrlX_IO_XI112108.xml")


def product(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return int(re.search(r'ProductCode="#x(\w+)"', f.read()).group(1), 16)


def device(path, name):
    return generate.device_from_esi(esi.parse_device(path, 36, product(path), 0x100), name)


class BitsTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.xml = generate.generate_eni([device(DO4, "DO4"), device(DI4, "DI4"), device(XI112108, "Next")],
                                        "ethercatmaster", 2000)
        cls.parsed = eni.parse_eni(cls.xml)
        cls.do4, cls.di4, cls.next = cls.parsed["slaves"]
        cls.text = cls.xml.decode("latin-1")
        cls.slaves = re.findall(r"<Slave>.*?</Slave>", cls.text, re.S)

    def test_sync_managers_are_one_byte(self):
        self.assertEqual([(m["type"], m["size"]) for m in self.do4["sm"]], [("Outputs", 1)])
        self.assertEqual([(m["type"], m["size"]) for m in self.di4["sm"]], [("Inputs", 1)])

    def test_process_image_takes_a_whole_byte(self):
        self.assertIn("<Send><BitStart>80</BitStart><BitLength>8</BitLength></Send>", self.slaves[0])
        self.assertIn("<Recv><BitStart>80</BitStart><BitLength>8</BitLength></Recv>", self.slaves[1])

    def test_next_device_starts_on_the_next_byte(self):
        self.assertIn("<Send><BitStart>88</BitStart>", self.slaves[2])
        self.assertIn("<Recv><BitStart>88</BitStart>", self.slaves[2])

    def test_variables_keep_their_bits(self):
        self.assertEqual([(v["bits"], v["offset"]) for v in self.do4["variables"]], [(1, 80), (1, 81), (1, 82), (1, 83)])
        self.assertEqual([(v["bits"], v["offset"]) for v in self.di4["variables"]], [(1, 80), (1, 81), (1, 82), (1, 83)])
        self.assertEqual({v["dir"] for v in self.do4["variables"]}, {"output"})
        self.assertEqual({v["dir"] for v in self.di4["variables"]}, {"input"})
        self.assertEqual(min(v["offset"] for v in self.next["variables"]), 88)

    def test_fmmu_maps_the_whole_byte(self):
        data = [re.search(r"<Data>(\w+)</Data>", c).group(1) for c in re.findall(r"<InitCmd>.*?</InitCmd>", self.slaves[0], re.S)
                if "<Comment>set fmmu 0(outputs)</Comment>" in c]
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0][8:16], "01000007")           # 1 byte, start bit 0, end bit 7


if __name__ == "__main__":
    unittest.main()
