# SPDX-License-Identifier: MIT

"""Unit tests against the simulated ctrlX (no Data Layer library needed):  python3 -m unittest -q

The data in tests/data was recorded read-only from a ctrlX CORE X3 with an XB-EC-12 coupler and three
ctrlX I/O modules (XI190202, XI422202, XI522001), configured with ctrlX I/O Engineering 4.6.1.
"""

import copy
import io
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zipfile

import app.web
from app import compare, eni, sii
from app.core import ConfigError, Core
from app.esi import EsiLibrary
from app.fake import DATA, MASTER, FakeBus, recorded_nodes
from app.xmlsafe import XmlError, parse_xml

ESI_DIR = os.path.join(DATA, "esi")
ESI_FILES = sorted(os.listdir(ESI_DIR))


def read(path, mode="rb"):
    with open(path, mode) as f:
        return f.read()


def recording():
    with open(os.path.join(DATA, "x3-bus.json"), encoding="utf-8") as f:
        return json.load(f)


def full_library(folder):
    lib = EsiLibrary(folder, log=lambda m: None)
    for name in ESI_FILES:
        lib.add(name, read(os.path.join(ESI_DIR, name)))
    return lib


class SiiTest(unittest.TestCase):

    def test_recorded_eeproms(self):
        slaves = recording()["slaves"]
        decoded = [sii.decode(s["eeprom"]) for s in slaves]
        self.assertEqual([d["order"] for d in decoded], ["XB-EC-12", "XI190202", "XI422202", "XI522001"])
        for s, d in zip(slaves, decoded):
            idn = s["online_info"]["response"]["slaveIdentity"]
            self.assertEqual((d["vendor"], d["product"], d["revision"]),
                             (idn["vendorId"], idn["productCode"], idn["revisionNumber"]))
            self.assertTrue(sii.end_reached(s["eeprom"]))
        ao = decoded[2]
        self.assertEqual([(s["type"], s["start"], s["size"]) for s in ao["sm"]],
                         [("MBoxOut", 0x1000, 128), ("MBoxIn", 0x1080, 128), ("Outputs", 0x1100, 4), ("Inputs", 0x1400, 6)])
        self.assertEqual([(p["dir"], p["index"], p["sm"]) for p in ao["pdos"]],
                         [("tx", 0x1A00, 3), ("tx", 0x1A01, 3), ("tx", 0x1A02, 3), ("tx", 0x1A03, None),
                          ("rx", 0x1600, 2), ("rx", 0x1601, 2)])
        self.assertEqual(ao["pdos"][4]["entries"], [{"index": 0x7000, "subindex": 1, "name": "Value", "bits": 16, "type": "INT"}])
        self.assertEqual(ao["mailbox"], ["CoE", "FoE"])
        self.assertEqual(decoded[1]["mailbox"], ["FoE"])

    def test_partial_reads(self):
        words = recording()["slaves"][2]["eeprom"]
        self.assertFalse(sii.end_reached(words[:64]))
        with self.assertRaises(sii.SiiError):
            sii.decode(words[:32])


class EniTest(unittest.TestCase):

    def test_controller_eni(self):
        e = eni.parse_eni(read(os.path.join(DATA, "controller-eni.xml")))
        self.assertEqual([s["name"] for s in e["slaves"]], ["XB_EC_12", "XI190202", "XI422202", "XI522001"])
        self.assertEqual([s["autoinc"] for s in e["slaves"]], [0, 65535, 65534, 65533])
        self.assertEqual((e["inputs_bytes"], e["outputs_bytes"]), (89, 89))
        ao = e["slaves"][2]
        self.assertEqual([(s["type"], s["pdos"]) for s in ao["sm"]], [("Outputs", [0x1600, 0x1601]), ("Inputs", [0x1A00, 0x1A01, 0x1A02])])
        self.assertEqual(len(ao["coe_init"]), 2)
        self.assertEqual(ao["coe_init"][0]["index"], 0x1C12)
        self.assertEqual([v["name"] for v in ao["variables"] if v["dir"] == "output"],
                         ["XI422202/Channel_1.Value", "XI422202/Channel_2.Value"])
        self.assertEqual(e["slaves"][1]["dc"]["CycleTime0"], "2000000")
        self.assertIsNone(ao["dc"])

    def test_ini(self):
        ini = eni.parse_ini(read(os.path.join(DATA, "controller-ini.txt"), "r"))
        self.assertEqual(ini["EtherCAT.Master"]["CycleTimeUs"], "2000")

    def test_not_an_eni(self):
        with self.assertRaises(ValueError):
            eni.parse_eni(b"<EtherCATInfo/>")
        with self.assertRaises(XmlError):
            eni.parse_eni(b"<EtherCATConfig")

    def test_numbers(self):
        self.assertEqual([eni.num("#x1A00"), eni.num("0x10"), eni.num("42"), eni.num(""), eni.num("x")],
                         [0x1A00, 16, 42, None, None])


class XmlSafeTest(unittest.TestCase):

    def test_doctype_is_refused(self):
        bomb = b'<?xml version="1.0"?><!DOCTYPE a [<!ENTITY x "xx">]><a>&x;</a>'
        with self.assertRaises(XmlError):
            parse_xml(bomb)

    def test_namespaces_are_dropped(self):
        self.assertEqual(parse_xml(b'<a xmlns="urn:x"><b/></a>').find("b").tag, "b")


class EsiTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.folder = os.path.join(self.dir.name, "esi")
        self.lib = EsiLibrary(self.folder, log=lambda m: None)

    def tearDown(self):
        self.dir.cleanup()

    def test_add_find_and_parse(self):
        r = self.lib.add("BoschRexroth_ctrlX_IO_XI422202.xml", read(os.path.join(ESI_DIR, "BoschRexroth_ctrlX_IO_XI422202.xml")))
        self.assertEqual(r, {"added": [{"file": "BoschRexroth_ctrlX_IO_XI422202.xml", "devices": 2}], "skipped": []})
        self.assertEqual(self.lib.find(0x24, 0x242A1F, 0x100)[0], "exact")
        match, entry = self.lib.find(0x24, 0x242A1F, 0x200)
        self.assertEqual((match, entry["revision"]), ("nearest", 0x100))        # highest revision below
        self.assertEqual(self.lib.find(0x24, 0x999, 1), ("none", None))
        d = self.lib.device(self.lib.find(0x24, 0x242A1F, 0x100)[1])
        self.assertEqual(d["type"], "XI422202")
        compact = [p for p in d["pdos"] if p["index"] == 0x1A03][0]
        self.assertIsNone(compact["sm"])
        self.assertEqual(compact["excludes"], [0x1A00, 0x1A01, 0x1A02])
        self.assertEqual(d["mailbox"], ["CoE", "FoE"])
        self.assertFalse(d["modular"])

    def test_zip_with_other_files(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("ESI/BoschRexroth_ctrlX_IO_XI190202.xml", read(os.path.join(ESI_DIR, "BoschRexroth_ctrlX_IO_XI190202.xml")))
            z.writestr("ESI/valve-IODD1.1.xml", '<?xml version="1.0"?><IODevice/>')
            z.writestr("ESI/readme.txt", "hello")
            z.writestr("ESI/broken.xml", "<EtherCATInfo")
        r = self.lib.add("esi.zip", buf.getvalue())
        self.assertEqual(r["added"], [{"file": "BoschRexroth_ctrlX_IO_XI190202.xml", "devices": 1}])
        reasons = {s["file"]: s["reason"] for s in r["skipped"]}
        self.assertEqual(reasons["ESI/valve-IODD1.1.xml"], "IO-Link device description (IODD)")
        self.assertEqual(reasons["ESI/readme.txt"], "not an XML file")
        self.assertIn("not valid XML", reasons["ESI/broken.xml"])
        self.assertEqual(self.lib.files(), ["BoschRexroth_ctrlX_IO_XI190202.xml"])

    def test_index_survives_a_restart_and_delete(self):
        self.lib.add("a.xml", read(os.path.join(ESI_DIR, "BoschRexroth_ctrlX_IO_XB-EC-12.xml")))
        again = EsiLibrary(self.folder, log=lambda m: None)
        self.assertEqual(again.summary(), {"files": 1, "devices": 2})
        again.delete("a.xml")
        self.assertEqual(again.summary(), {"files": 0, "devices": 0})
        with self.assertRaises(KeyError):
            again.delete("a.xml")

    def test_file_names_stay_in_the_folder(self):
        self.lib.add("../../evil.xml", read(os.path.join(ESI_DIR, "BoschRexroth_ctrlX_IO_XB-EC-12.xml")))
        self.assertEqual(self.lib.files(), ["evil.xml"])
        self.assertFalse(os.path.exists(os.path.join(self.dir.name, "evil.xml")))

    def test_search(self):
        for name in ESI_FILES:
            self.lib.add(name, read(os.path.join(ESI_DIR, name)))
        self.assertEqual({e["type"] for e in self.lib.search("serial")}, {"XI522001"})
        self.assertEqual(len(self.lib.search("")), 6)
        self.assertEqual(len(self.lib.search("ctrlx", limit=2)), 2)


class CompareTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        cls.lib = full_library(cls.dir.name)
        cls.rec = recording()
        cls.eni = eni.parse_eni(read(os.path.join(DATA, "controller-eni.xml")))

    @classmethod
    def tearDownClass(cls):
        cls.dir.cleanup()

    def report(self, pos, online=None, eeprom=None, eni_slave=None, esi_device=None):
        s = self.rec["slaves"][pos]
        online = online or s["online_info"]
        idn = online["response"]["slaveIdentity"]
        match, entry = self.lib.find(idn["vendorId"], idn["productCode"], idn["revisionNumber"])
        return compare.device_report(pos, online, eeprom or sii.decode(s["eeprom"]), None,
                                     eni_slave or self.eni["slaves"][pos], match, entry,
                                     esi_device or (self.lib.device(entry) if entry else None))

    def test_recorded_bus_agrees(self):
        for pos in range(4):
            r = self.report(pos)
            self.assertEqual((r["level"], r["findings"]), ("ok", []), r["name"])
            self.assertEqual({k: v["match"] for k, v in r["sources"].items()}, {"esi": "exact", "eni": "same", "eeprom": "read"})

    def test_wrong_device_at_a_position(self):
        r = self.report(2, eni_slave=self.eni["slaves"][3])
        self.assertEqual(r["level"], "error")
        self.assertIn("Configured here: XI522001", r["findings"][0]["text"])

    def test_device_error_and_state(self):
        online = copy.deepcopy(self.rec["slaves"][1]["online_info"])
        online["response"]["alStatus"] = 0x12
        online["response"]["alStatusCode"] = 0x1D
        r = self.report(1, online=online)
        self.assertEqual(r["findings"][0], {"level": "error", "text": "Device reports an error: AL status PreOp (error), code #x001D"})
        online["response"]["alStatus"] = 0x04
        self.assertIn("SafeOp while the master is in Op", self.report(1, online=online)["findings"][0]["text"])

    def test_pdo_problems(self):
        slave = copy.deepcopy(self.eni["slaves"][2])
        slave["sm"][1]["pdos"].append(0x1A03)                       # excluded by 0x1A00..0x1A02
        [p for p in slave["pdos"] if p["index"] == 0x1600][0]["entries"][0]["bits"] = 32   # differs from the ESI
        texts = [f["text"] for f in self.report(2, eni_slave=slave)["findings"]]
        self.assertIn("PDOs #x1A00 and #x1A03 are both assigned, but the ESI says they exclude each other", texts)
        self.assertIn('PDO #x1600 "Channel 1": entries in the ENI differ from the ESI', texts)

    def test_sync_manager_start(self):
        ee = sii.decode(self.rec["slaves"][2]["eeprom"])
        ee["sm"][3]["start"] = 0x1800
        texts = [f["text"] for f in self.report(2, eeprom=ee)["findings"]]
        self.assertEqual(texts, ["Inputs sync manager start address differs: ESI #x1400, ENI #x1400, EEPROM #x1800"])

    def test_missing_device(self):
        s = self.eni["slaves"][3]
        r = compare.device_report(3, None, None, None, s, "exact", None, None)
        self.assertEqual((r["present"], r["level"], r["name"]), (False, "error", "XI522001"))
        self.assertEqual(r["sources"]["eni"]["match"], "not found")


class CoreTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "io-config", "config.json")
        self.bus = FakeBus()
        self.core = Core(self.path, self.bus, os.path.join(self.dir.name, "esi"), log=lambda m: None)
        for name in ESI_FILES:
            self.core.esi.add(name, read(os.path.join(ESI_DIR, name)))

    def tearDown(self):
        self.core.stop()
        self.dir.cleanup()

    def scan(self, reread=False):
        self.core.start_scan(reread_eeprom=reread)
        self.core.wait_scan()
        return self.core.state()

    def test_scan(self):
        s = self.scan()
        self.assertIsNone(s["scan"]["error"])
        self.assertEqual(s["master"]["state"], "op")
        self.assertEqual(s["master"]["cycle_us"], 2000)
        self.assertEqual([(d["name"], d["level"]) for d in s["devices"]],
                         [("XB_EC_12", "ok"), ("XI190202", "ok"), ("XI422202", "ok"), ("XI522001", "ok")])
        outputs = [v["name"] for v in self.core.device(2)["sources"]["eni"]["variables"] if v["dir"] == "output"]
        self.assertEqual(outputs, ["XI422202/Channel_1.Value", "XI422202/Channel_2.Value"])

    def test_reads_only_and_never_past_the_last_device(self):
        self.scan()
        ops = {c[0] for c in self.bus.calls}
        self.assertEqual(ops, {"read", "read_args"})
        positions = {c[2]["request"]["address"] for c in self.bus.calls if c[0] == "read_args"}
        self.assertEqual(positions, {0, 65535, 65534, 65533})
        sizes = {c[2]["request"]["maxLength"] for c in self.bus.calls if c[1].endswith("slave_eeprom")}
        self.assertEqual(sizes, {64})

    def test_eeprom_cache(self):
        self.scan()
        count = lambda: sum(1 for c in self.bus.calls if c[1].endswith("slave_eeprom"))   # noqa: E731
        first = count()
        self.scan()
        self.assertEqual(count(), first)
        self.scan(reread=True)
        self.assertEqual(count(), 2 * first)

    def test_missing_and_extra_devices(self):
        self.bus.slaves.pop()                     # XI522001 unplugged
        self.bus.nodes[MASTER + "admin/fieldbus_status/num_connected_slaves"] = {"response": {"numSlaves": 3}}
        s = self.scan()
        self.assertEqual([(d["name"], d["present"], d["level"]) for d in s["devices"]][-1], ("XI522001", False, "error"))

    def test_unknown_instance(self):
        self.core.update_config({"instance": "other"})
        s = self.scan()
        self.assertIn('no EtherCAT master instance "other" (found: ethercatmaster)', s["scan"]["error"])
        self.assertIsNone(s["master"])

    def test_config(self):
        with self.assertRaises(ConfigError):
            self.core.update_config({"instance": "bad name"})
        with self.assertRaises(ConfigError):
            self.core.update_config({"other": 1})
        self.core.update_config({"instance": "ecat2"})
        with open(self.path, "w") as f:
            json.dump({"instance": "ethercatmaster"}, f)
        self.assertTrue(self.core.reload_if_changed())
        self.assertEqual(self.core.config["instance"], "ethercatmaster")

    def test_fake_simulates_the_device(self):
        with self.assertRaises(Exception):
            self.bus.read_args(MASTER + "device_access/slave_online_info", {"request": {"addressType": "autoincrement", "address": 65532}})
        with self.assertRaises(Exception):
            self.bus.read_args(MASTER + "device_access/slave_eeprom", {"request": {"addressType": "autoincrement", "address": 0, "maxLength": 2048}})
        nodes, slaves = recorded_nodes()
        self.assertEqual(len(slaves), 4)


class WebTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        app.web.core = Core(os.path.join(cls.dir.name, "config.json"), FakeBus(), os.path.join(cls.dir.name, "esi"),
                            log=lambda m: None)
        app.web.www_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "www")
        cls.server = app.web.TcpServer(("127.0.0.1", 0), app.web.RequestHandler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        app.web.core.stop()
        cls.dir.cleanup()

    def call(self, method, path, body=None, raw=None):
        data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with self.opener.open(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")

    def test_page_is_served(self):
        with self.opener.open(self.base + "/io-config/", timeout=5) as r:
            self.assertIn(b"<title>I/O Config</title>", r.read())

    def test_static_files_stay_inside_www(self):
        self.assertEqual(self.call("GET", "/io-config/../app/web.py")[0], 404)

    def test_scan_and_device(self):
        self.assertEqual(self.call("POST", "/io-config/api/v1/scan", {})[0], 202)
        app.web.core.wait_scan()
        status, s = self.call("GET", "/io-config/api/v1/state")
        self.assertEqual((status, len(s["devices"])), (200, 4))
        status, d = self.call("GET", "/io-config/api/v1/devices/2")
        self.assertEqual((status, d["name"]), (200, "XI422202"))
        self.assertEqual(self.call("GET", "/io-config/api/v1/devices/9")[0], 404)

    def test_esi_upload_list_delete(self):
        name = "BoschRexroth_ctrlX_IO_XI522001.xml"
        status, r = self.call("POST", "/io-config/api/v1/esi?name=" + name, raw=read(os.path.join(ESI_DIR, name)))
        self.assertEqual((status, r["added"]), (200, [{"file": name, "devices": 1}]))
        status, r = self.call("GET", "/io-config/api/v1/esi?q=serial")
        self.assertEqual((status, r["files"], [d["type"] for d in r["devices"]]), (200, [name], ["XI522001"]))
        self.assertEqual(self.call("POST", "/io-config/api/v1/esi?name=x.xml", raw=b"<IODevice/>")[1]["skipped"][0]["reason"],
                         "IO-Link device description (IODD)")
        self.assertEqual(self.call("DELETE", "/io-config/api/v1/esi/" + name)[0], 200)
        self.assertEqual(self.call("GET", "/io-config/api/v1/esi")[1]["files"], [])
        self.assertEqual(self.call("POST", "/io-config/api/v1/esi", raw=b"<a/>")[0], 400)

    def test_unknown_route_and_method(self):
        self.assertEqual(self.call("GET", "/io-config/api/v1/nothing")[0], 404)
        self.assertEqual(self.call("DELETE", "/io-config/api/v1/state")[0], 405)

    def test_unread_body_does_not_break_the_next_request(self):
        """The ctrlX proxy reuses connections: a body the handler ignored must be drained."""
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        conn.request("POST", "/io-config/api/v1/admin/cfg/save", body=b"{}", headers={"Content-Type": "application/json"})
        r = conn.getresponse()
        r.read()
        self.assertEqual(r.status, 204)
        conn.request("GET", "/io-config/api/v1/state")
        r = conn.getresponse()
        self.assertEqual(r.status, 200)
        self.assertIn("devices", json.loads(r.read()))
        conn.close()


if __name__ == "__main__":
    unittest.main()
