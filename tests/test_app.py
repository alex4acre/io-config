# SPDX-License-Identifier: MIT

"""Unit tests against the simulated ctrlX (no Data Layer library needed):  python3 -m unittest -q"""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

import app.web
from app.core import ConfigError, Core
from app.fake import FakeBus


class CoreTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "io-config", "config.json")
        self.core = Core(self.path, FakeBus(), log=lambda m: None)

    def tearDown(self):
        self.core.stop()
        self.dir.cleanup()

    def test_defaults_without_a_file(self):
        self.assertEqual(self.core.config["setpoint"], 0.0)

    def test_update_saves_atomically(self):
        self.core.update_config({"setpoint": 2})
        with open(self.path) as f:
            self.assertEqual(json.load(f)["setpoint"], 2.0)
        self.assertFalse(os.path.exists(self.path + ".tmp"))

    def test_bad_values_are_refused(self):
        for bad in ({"setpoint": "x"}, {"setpoint": True}, {"cycle_s": 0}, {"other": 1}):
            with self.assertRaises(ConfigError):
                self.core.update_config(bad)

    def test_file_replaced_from_outside_is_reloaded(self):
        self.core.update_config({"setpoint": 1})
        with open(self.path, "w") as f:
            json.dump({"setpoint": 7.5, "note": "from a backup"}, f)
        self.assertTrue(self.core.reload_if_changed())
        self.assertEqual(self.core.config["setpoint"], 7.5)
        self.assertFalse(self.core.reload_if_changed())


class WebTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        app.web.core = Core(os.path.join(cls.dir.name, "config.json"), FakeBus(), log=lambda m: None)
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

    def call(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with self.opener.open(req, timeout=5) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")

    def test_page_is_served(self):
        with self.opener.open(self.base + "/io-config/", timeout=5) as r:
            self.assertIn(b"<title>", r.read())

    def test_static_files_stay_inside_www(self):
        self.assertEqual(self.call("GET", "/io-config/../app/web.py")[0], 404)

    def test_config_round_trip(self):
        self.assertEqual(self.call("PATCH", "/io-config/api/v1/config", {"setpoint": 3.5}), (200, app.web.core.config))
        self.assertEqual(self.call("GET", "/io-config/api/v1/config")[1]["setpoint"], 3.5)
        status, body = self.call("PATCH", "/io-config/api/v1/config", {"setpoint": "high"})
        self.assertEqual(status, 400)
        self.assertIn("setpoint", body["error"])

    def test_node_read_write(self):
        self.assertEqual(self.call("GET", "/io-config/api/v1/node?path=demo/flag"), (200, {"path": "demo/flag", "value": False}))
        self.assertEqual(self.call("PUT", "/io-config/api/v1/node?path=demo/flag", {"value": True})[1]["value"], True)
        self.assertEqual(self.call("GET", "/io-config/api/v1/node?path=no/such")[0], 502)

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
        self.assertIn("counter", json.loads(r.read()))
        conn.close()


if __name__ == "__main__":
    unittest.main()
