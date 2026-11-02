# SPDX-License-Identifier: MIT

"""The master's state without a scan, and phasing up (Op) and down (Init) from the Bus tab:  python3 -m unittest -q"""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

import app.web

from app.bus import BusError
from app.core import ConfigError, Core
from app.fake import MASTER, FakeBus
from tests.test_app import ESI_DIR, ESI_FILES, read

STATE = MASTER + "admin/fieldbus_status/master_state"


class MasterStateTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.bus = FakeBus()
        self.core = Core(os.path.join(self.dir.name, "active", "io-config", "config.json"), self.bus,
                         os.path.join(self.dir.name, "esi"), log=lambda m: None)
        for name in ESI_FILES:
            self.core.esi.add(name, read(os.path.join(ESI_DIR, name)))
        self.core.master_wait = 1
        self.core.start_scan()
        self.core.wait_scan()

    def tearDown(self):
        self.core.stop()
        self.dir.cleanup()

    def request(self, state):
        self.core.request_master_state(state)
        self.core.wait_scan()               # the request
        self.core.wait_scan()               # the scan after it

    def test_live_status_without_a_scan(self):
        self.assertEqual(self.core.master_status()["state"], "op")
        self.bus.nodes[STATE] = {"response": {"currentState": "safeop", "requestedState": "op"}}
        self.assertEqual(self.core.master_status()["state"], "op")                 # cached for a moment
        s = self.core.master_status(max_age=0)
        self.assertEqual((s["state"], s["requested"], s["error"]), ("safeop", "op", None))
        self.assertEqual(self.core.scan_state["running"], False)

    def test_unreadable_master_is_unavailable_and_logged_once(self):
        logged = []
        self.core.log = logged.append
        self.bus.gone = True
        for _ in range(3):
            s = self.core.master_status(max_age=0)
        self.assertEqual(s["state"], "unavailable")
        self.assertTrue(s["error"])
        self.assertEqual(len(logged), 1)
        self.bus.gone = False
        self.assertEqual(self.core.master_status(max_age=0)["state"], "op")

    def test_phase_down_then_up_scans_afterwards(self):
        self.request("init")
        self.assertEqual(self.bus.nodes[STATE]["response"]["currentState"], "init")
        st = self.core.state()
        self.assertEqual(st["master"]["state"], "init")                          # the scan after it refreshed the card
        self.assertEqual((st["master_request"]["running"], st["master_request"]["message"]), (False, ""))
        self.request("op")
        self.assertEqual(self.core.state()["master"]["state"], "op")
        self.assertEqual(self.bus.written, [])                                   # no configuration written
        self.assertEqual(len(self.core.state()["configuration"]["devices"]), 4)  # the list is untouched

    def test_all_five_states_and_nothing_else(self):
        for state in ("preop", "bootstrap", "safeop", "op", "init"):
            self.request(state)
            self.assertEqual(self.bus.nodes[STATE]["response"]["currentState"], state)
            self.assertEqual(self.core.state()["master"]["state"], state)
        for bad in ("unknown", "", None, "OP", "Pre-OP", 1):
            with self.assertRaisesRegex(ConfigError, "must be one of"):
                self.core.request_master_state(bad)

    def test_a_refused_write_is_shown_with_the_masters_answer_and_the_scan_runs(self):
        orig = self.bus.write

        def refuse(address, value):
            if address == STATE:
                raise BusError(f"write {address}: DL_FAILED (Ecat: transition not allowed)")
            return orig(address, value)
        self.bus.write = refuse
        before = self.core.scan_state["finished"]
        self.request("bootstrap")
        m = self.core.state()["master_request"]
        self.assertIn("Requesting Bootstrap failed", m["message"])
        self.assertIn("transition not allowed", m["message"])
        self.assertGreater(self.core.scan_state["finished"], before)             # the scan ran anyway
        self.assertEqual(self.core.state()["master"]["state"], "op")

    def test_refused_while_busy_or_unreadable(self):
        self.core.scan_state["running"] = True
        with self.assertRaisesRegex(ConfigError, "running"):
            self.core.request_master_state("init")
        self.core.scan_state["running"] = False
        self.core.download_state["running"] = True
        with self.assertRaisesRegex(ConfigError, "running"):
            self.core.request_master_state("init")
        self.core.download_state["running"] = False
        self.bus.gone = True
        with self.assertRaisesRegex(ConfigError, "can't be read"):
            self.core.request_master_state("op")
        self.assertEqual([c for c in self.bus.calls if c[0] == "write"], [])

    def test_not_reaching_the_state_leaves_a_message(self):
        self.request("init")
        self.bus.fail_op = True
        self.request("op")
        m = self.core.state()["master_request"]
        self.assertIn("did not reach OP", m["message"])
        self.assertIn("it is in Pre-OP, OP is requested", m["message"])
        self.assertEqual(self.core.state()["master"]["state"], "preop")
        self.request("init")                                                     # the message goes with the next request
        self.assertEqual(self.core.state()["master_request"]["message"], "")


class RoutesTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.bus = FakeBus()
        self.core = Core(os.path.join(self.dir.name, "active", "io-config", "config.json"), self.bus,
                         os.path.join(self.dir.name, "esi"), log=lambda m: None)
        for name in ESI_FILES:
            self.core.esi.add(name, read(os.path.join(ESI_DIR, name)))
        self.core.master_wait = 1
        app.web.core = self.core
        app.web.www_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "www")
        self.server = app.web.TcpServer(("127.0.0.1", 0), app.web.RequestHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}/io-config/api/v1"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.core.start_scan()
        self.core.wait_scan()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.core.stop()
        self.dir.cleanup()

    def call(self, method, path, body=None):
        req = urllib.request.Request(self.base + path, data=None if body is None else json.dumps(body).encode(),
                                     method=method, headers={"Content-Type": "application/json"})
        try:
            with self.opener.open(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")

    def test_master_state_routes(self):
        self.assertEqual(self.call("GET", "/master/state")[1]["state"], "op")
        for bad in ({"state": "unknown"}, {"state": "OP"}, {}, {"state": 3}):
            self.assertEqual(self.call("POST", "/master/state", bad)[0], 400)
        status, r = self.call("POST", "/master/state", {"state": "init"})
        self.assertEqual((status, r["running"], r["requested"]), (202, True, "init"))
        self.core.wait_scan()
        self.core.wait_scan()
        self.core.master_request["running"] = True
        self.assertEqual(self.call("POST", "/master/state", {"state": "op"})[0], 400)          # busy: refused
        self.core.master_request["running"] = False
        self.assertEqual(self.call("GET", "/state")[1]["master"]["state"], "init")

    def test_project_update_and_open_need_confirmation(self):
        self.assertEqual(self.call("POST", "/project/update", {})[0], 400)
        self.assertEqual(self.call("POST", "/project/update", {"confirm": True})[0], 200)
        self.assertEqual(self.call("DELETE", "/project")[0], 404)
        status, s = self.call("POST", "/saved", {"name": "P", "kind": "proposed"})
        self.assertEqual((status, s["kind"]), (201, "proposed"))
        self.assertEqual(self.call("POST", "/saved", {"name": "Q", "kind": "other"})[0], 400)
        self.assertEqual(self.call("POST", "/saved/P/open", {})[0], 400)
        self.assertEqual(self.call("POST", "/saved/P/open", {"confirm": True})[0], 200)
        self.assertEqual(self.call("POST", "/saved/Nope/open", {"confirm": True})[0], 404)

    def test_startup_from_the_bus_goes_to_the_configuration_row(self):
        self.core.project_remove(1)                                 # the list: coupler, XI422202, XI522001
        status, r = self.call("POST", "/devices/2/edit/startup", {"index": 0x8000, "subindex": 1, "value": 1000})
        self.assertEqual((status, r["position"], r["configuration_position"]), (200, 1, 1))
        self.assertEqual(self.core.load_edits()[1]["startup"][-1]["index"], 0x8000)
        status, r = self.call("POST", "/devices/1/edit/startup", {"index": 0x8000, "subindex": 1, "value": 1})
        self.assertEqual(status, 400)
        self.assertIn("not in the Configuration list", r["error"])


if __name__ == "__main__":
    unittest.main()
