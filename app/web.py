# SPDX-License-Identifier: MIT

"""Web UI and REST API, served behind the ctrlX reverse proxy at /io-config.

The proxy checks the bearer token for the "restricted" paths of the package manifest (/io-config/api), so
handlers only see authenticated requests. A handler that needs to call the ctrlX REST API itself can pass the
caller's token on: self.headers["Authorization"] to https://localhost/... (e.g. /automation/api/v2/nodes/...).
"""

import http.server
import json
import mimetypes
import os
import re
import socketserver
import traceback
from urllib.parse import parse_qs, unquote, urlparse

from app.bus import BusError
from app.core import ConfigError

BASE = "/io-config"
API = BASE + "/api/v1"
MAX_BODY = 1024 * 1024
MAX_UPLOAD = 64 * 1024 * 1024

core = None          # set by main
www_dir = None       # set by main

ROUTES = [
    ("GET", r"/state", "get_state"),
    ("GET", r"/config", "get_config"),
    ("PATCH", r"/config", "patch_config"),
    ("GET", r"/instances", "get_instances"),
    ("POST", r"/scan", "post_scan"),
    ("GET", r"/devices/(\d+)", "get_device"),
    ("GET", r"/devices/(\d+)/edit", "get_edit"),
    ("PUT", r"/devices/(\d+)/edit", "put_edit"),
    ("DELETE", r"/devices/(\d+)/edit", "delete_edit"),
    ("POST", r"/devices/(\d+)/edit/startup", "post_startup"),
    ("GET", r"/devices/(\d+)/objects", "get_objects"),
    ("GET", r"/devices/(\d+)/objects/(\d+)", "get_object"),
    ("PUT", r"/devices/(\d+)/objects/(\d+)/(\d+)", "put_sdo"),
    ("GET", r"/devices/(\d+)/soe", "get_soe_list"),
    ("GET", r"/devices/(\d+)/soe/(\d+)", "get_soe"),
    ("PUT", r"/devices/(\d+)/soe/(\d+)", "put_soe"),
    ("GET", r"/process-data", "get_process_data"),
    ("PUT", r"/process-data", "put_process_data"),
    ("POST", r"/plan", "post_plan"),
    ("GET", r"/plan", "get_plan"),
    ("GET", r"/plan/(eni|ini)", "get_plan_file"),
    ("POST", r"/download", "post_download"),
    ("GET", r"/backups", "get_backups"),
    ("GET", r"/backups/(\d{8}-\d{6})/(eni|ini)", "get_backup_file"),
    ("POST", r"/backups/(\d{8}-\d{6})/restore", "post_restore"),
    ("GET", r"/esi", "get_esi"),
    ("POST", r"/esi", "post_esi"),
    ("DELETE", r"/esi/([^/]+)", "delete_esi"),
    ("POST", r"/admin/cfg/load", "cfg_load"),
    ("POST", r"/admin/cfg/save", "cfg_save"),
]

mimetypes.add_type("text/javascript", ".js")


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class RequestHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # keep the journal for app messages

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_PUT(self):
        self.dispatch("PUT")

    def do_PATCH(self):
        self.dispatch("PATCH")

    def do_DELETE(self):
        self.dispatch("DELETE")

    def dispatch(self, method: str):
        self._body_read = False
        url = urlparse(self.path)
        path = url.path
        self.query = parse_qs(url.query)
        try:
            if path.startswith(API):
                sub = path[len(API):].rstrip("/")
                matched = False
                for route_method, pattern, handler in ROUTES:
                    m = re.fullmatch(pattern, sub)
                    if m:
                        matched = True
                        if route_method == method:
                            return getattr(self, handler)(*[unquote(g) for g in m.groups()])
                raise HttpError(405 if matched else 404, "Method not allowed" if matched else "Not found")
            if method == "GET" and (path == BASE or path.startswith(BASE + "/")):
                return self.send_static(path[len(BASE):].lstrip("/") or "index.html")
            raise HttpError(404, "Not found")
        except HttpError as e:
            self.send_json(e.status, {"error": str(e)})
        except KeyError as e:
            self.send_json(404, {"error": str(e.args[0]) if e.args else "Not found"})
        except (ConfigError, ValueError) as e:
            self.send_json(400, {"error": str(e)})
        except BusError as e:
            self.send_json(502, {"error": str(e)})
        except Exception as e:
            traceback.print_exc()
            self.send_json(500, {"error": str(e)})
        finally:
            self.drain_body()

    def drain_body(self):
        """Read a request body the handler didn't use. The ctrlX reverse proxy reuses the connection, and a left-over
        body (e.g. the "{}" of a POST) would otherwise be read as the start of the next request ("501 {}GET")."""
        length = int(self.headers.get("Content-Length") or 0)
        if self._body_read or not length:
            return
        if length > 16 * MAX_BODY:
            self.close_connection = True
        else:
            self.rfile.read(length)

    # --- helpers ---------------------------------------------------------------------------------

    def q(self, name, default=""):
        return (self.query.get(name) or [default])[0]

    def read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise HttpError(413, "Request body too large")
        self._body_read = True
        try:
            data = json.loads(self.rfile.read(length) or b"null")
        except ValueError as e:
            raise HttpError(400, f"Invalid JSON: {e}") from e
        if not isinstance(data, dict):
            raise HttpError(400, "A JSON object is expected")
        return data

    def send_bytes(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status, obj):
        self.send_bytes(status, json.dumps(obj).encode("utf-8"), "application/json")

    def send_static(self, rel):
        root = os.path.realpath(www_dir)
        full = os.path.realpath(os.path.join(root, rel))
        if not full.startswith(root + os.sep) or not os.path.isfile(full):
            raise HttpError(404, "Not found")
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/"):
            ctype += "; charset=utf-8"
        with open(full, "rb") as f:
            self.send_bytes(200, f.read(), ctype)

    # --- app -------------------------------------------------------------------------------------

    def get_state(self):
        self.send_json(200, core.state())

    def get_config(self):
        self.send_json(200, core.config)

    def patch_config(self):
        self.send_json(200, core.update_config(self.read_json()))

    def get_instances(self):
        self.send_json(200, {"instances": core.instances()})

    # --- bus scan ----------------------------------------------------------------------------------

    def post_scan(self):
        d = self.read_json() if int(self.headers.get("Content-Length") or 0) else {}
        self.send_json(202, core.start_scan(reread_eeprom=bool(d.get("reread_eeprom"))))

    def get_device(self, position):
        self.send_json(200, core.device(int(position)))

    # --- edits: name, PDO selection, startup parameters ---------------------------------------------

    def get_edit(self, position):
        self.send_json(200, core.edit_options(int(position)))

    def put_edit(self, position):
        self.send_json(200, core.save_edit(int(position), self.read_json()))

    def delete_edit(self, position):
        self.send_json(200, core.reset_edit(int(position)))

    def post_startup(self, position):
        """Adds a startup parameter from a value typed for an object entry (encoded with the entry's data type)."""
        d = self.read_json()
        current = core.edit_options(int(position))["startup"]
        if "idn" in d:                   # SoE drive parameter
            entry = core.soe_startup_entry(int(position), d["idn"], d.get("value"))
            current = [x for x in current if x.get("idn") != entry["idn"]] + [entry]
        else:
            try:
                index, sub = int(d["index"]), int(d["subindex"])
            except (KeyError, TypeError, ValueError) as e:
                raise HttpError(400, '{"index", "subindex", "value"} or {"idn", "value"} are needed') from e
            entry = core.startup_entry(int(position), index, sub, d.get("value"), d.get("comment", ""))
            current = [x for x in current if "idn" in x or (x["index"], x["subindex"]) != (index, sub)] + [entry]
        self.send_json(200, core.save_edit(int(position), {"startup": current}))

    # --- CoE objects ---------------------------------------------------------------------------------

    def get_objects(self, position):
        self.send_json(200, core.objects(int(position), refresh=self.q("refresh") == "1"))

    def get_object(self, position, index):
        self.send_json(200, core.object(int(position), int(index)))

    def put_sdo(self, position, index, sub):
        d = self.read_json()
        if "value" not in d or d.get("confirm") is not True:
            raise HttpError(400, '{"value": ..., "confirm": true} is needed')
        self.send_json(200, core.sdo_write(int(position), int(index), int(sub), d["value"]))

    # --- SoE parameters ------------------------------------------------------------------------------

    def get_soe_list(self, position):
        self.send_json(200, core.soe_parameters(int(position), self.q("q"), self.q("mapping"), int(self.q("limit", "400"))))

    def get_soe(self, position, idn):
        self.send_json(200, core.soe_read(int(position), int(idn)))

    def put_soe(self, position, idn):
        d = self.read_json()
        if "value" not in d or d.get("confirm") is not True:
            raise HttpError(400, '{"value": ..., "confirm": true} is needed')
        self.send_json(200, core.soe_write(int(position), int(idn), d["value"]))

    # --- process data --------------------------------------------------------------------------------

    def get_process_data(self):
        self.send_json(200, core.process_data())

    def put_process_data(self):
        d = self.read_json()
        if not isinstance(d.get("name"), str) or "value" not in d:
            raise HttpError(400, '{"name": <variable>, "value": ...} is needed')
        self.send_json(200, core.write_output(d["name"], d["value"]))

    # --- new configuration, download, backups -------------------------------------------------------

    def post_plan(self):
        d = self.read_json() if int(self.headers.get("Content-Length") or 0) else {}
        self.send_json(200, core.plan(d.get("cycle_us")))

    def get_plan(self):
        self.send_json(200, core.plan_summary())

    def send_file(self, data: bytes, filename: str, content_type: str):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def get_plan_file(self, which):
        if which == "eni":
            self.send_file(core.plan_file("eni"), "ethercat.xml", "application/xml")
        else:
            self.send_file(core.plan_file("ini"), "ethercat.ini", "text/plain")

    def post_download(self):
        d = self.read_json()
        if not isinstance(d.get("plan_id"), int) or d.get("confirm") is not True:
            raise HttpError(400, '{"plan_id": <id of the built configuration>, "confirm": true} is needed')
        self.send_json(202, core.start_download(d["plan_id"], allow_draft=d.get("allow_draft") is True))

    def get_backups(self):
        self.send_json(200, {"backups": core.backups()})

    def get_backup_file(self, name, which):
        folder = core._backup_folder(name)
        file = "ethercat.xml" if which == "eni" else "ethercat.ini"
        with open(os.path.join(folder, file), "rb") as f:
            self.send_file(f.read(), f"{name}-{file}", "application/xml" if which == "eni" else "text/plain")

    def post_restore(self, name):
        d = self.read_json()
        if d.get("confirm") is not True:
            raise HttpError(400, '{"confirm": true} is needed')
        self.send_json(202, core.start_restore(name))

    # --- ESI library -------------------------------------------------------------------------------

    def get_esi(self):
        self.send_json(200, {"total": core.esi.summary()["devices"], "files": sorted(core.esi.files()),
                             "devices": core.esi.search(self.q("q"), limit=int(self.q("limit", "300")))})

    def post_esi(self):
        """The body is the file itself (XML or zip); its name comes in ?name=."""
        name = self.q("name")
        if not name:
            raise HttpError(400, "name is missing")
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_UPLOAD:
            raise HttpError(413, f"File too large (max {MAX_UPLOAD // (1024 * 1024)} MB)")
        self._body_read = True
        data = self.rfile.read(length)
        self.send_json(200, core.esi.add(name, data))

    def delete_esi(self, name):
        core.esi.delete(name)
        self.send_json(200, core.esi.summary())

    # --- active configuration (ctrlX calls these when a configuration is loaded or saved) ---------

    def cfg_load(self):
        data = self.read_json()
        if data.get("phase") == "load":
            core.reload_if_changed()
        self.send_bytes(204, b"", "application/json")

    def cfg_save(self):
        self.send_bytes(204, b"", "application/json")


class UnixSocketServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def get_request(self):
        request, _ = super().get_request()
        return request, ("local", 0)


class TcpServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
