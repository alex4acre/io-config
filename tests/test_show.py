# SPDX-License-Identifier: MIT

"""Values shown to people (decoded, hex most significant byte first) and the short IDN / index search:
python3 -m unittest -q"""

import unittest

from app import coe, soe
from app.core import show_startup


class ShowTest(unittest.TestCase):
    def test_coe_values(self):
        self.assertEqual(coe.show(bytes.fromhex("E8030000")), "1000 (0x000003E8)")       # no type: unsigned
        self.assertEqual(coe.show(bytes.fromhex("E803"), "INT", 16), "1000 (0x03E8)")
        self.assertEqual(coe.show(bytes.fromhex("F6FF"), "INT", 16), "-10 (0xFFF6)")
        self.assertEqual(coe.show(b"\x01", "BOOL", 1), "TRUE")
        self.assertEqual(coe.show(b"abc\0", "VISIBLE_STRING", 32), '"abc"')
        self.assertEqual(coe.show(bytes(range(3))), "00 01 02")                         # no number size: bytes

    def test_soe_values(self):
        u32 = soe.attribute(0x00120000)                       # 4 bytes, unsigned
        self.assertEqual(soe.show(bytes.fromhex("E8030000"), u32), "1000 (0x000003E8)")
        u16_list = soe.attribute(0x00150000)                  # list of 2-byte elements, unsigned
        self.assertEqual(soe.show(bytes.fromhex("04000400" "0100" "0200"), u16_list), "1 (0x0001), 2 (0x0002)")
        idn_list = soe.attribute(0x00550000)                  # list of IDNs
        self.assertEqual(soe.show(bytes.fromhex("02000200" "2F00"), idn_list), "S-0-0047")

    def test_startup_rows_keep_their_data(self):
        rows = [{"index": 0x8000, "subindex": 1, "data": "E8030000", "transitions": ["PS"], "comment": ""},
                {"idn": 1, "data": "E803", "transitions": ["PS"], "comment": ""}]
        out = show_startup(rows, {1: {"attribute": 0x00110000}})
        self.assertEqual([r["show"] for r in out], ["1000 (0x000003E8)", "1000 (0x03E8)"])
        self.assertEqual([r["data"] for r in out], ["E8030000", "E803"])               # the ENI bytes are unchanged


class SearchTest(unittest.TestCase):
    def test_idn_short_forms(self):
        for q in ("S265", "s265", "s 265", "S-265", "S0265", "S-0-0265", "s-0-265", "265", "velo"):
            self.assertTrue(soe.matches(q, 265, "Velocity limit"), q)
        for q in ("P265", "S-1-0265", "S26", "S2650", "torque"):
            self.assertFalse(soe.matches(q, 265, "Velocity limit"), q)
        self.assertTrue(soe.matches("P12", 0x8000 | 12, "x"))
        self.assertTrue(soe.matches("P-0-12", 0x8000 | 12, "x"))
        self.assertFalse(soe.matches("S12", 0x8000 | 12, "x"))

    def test_object_index(self):
        for q in ("8000", "x8000", "0x8000", "#x8000", "channel", "8000 settings"):
            self.assertTrue(coe.matches(q, 0x8000, "Channel 1 settings"), q)
        self.assertFalse(coe.matches("8010", 0x8000, "Channel 1 settings"))


if __name__ == "__main__":
    unittest.main()
