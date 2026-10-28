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

core = None          # set by main
www_dir = None       # set by main

ROUTES = [
    ("GET", r"/state", "get_state"),
    ("GET", r"/config", "get_config"),
    ("PATCH", r"/config", "patch_config"),
    ("GET", r"/browse", "browse"),
    ("GET", r"/node", "read_node"),
    ("PUT", r"/node", "write_node"),
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

    # --- Data Layer access (an example of using the client bus) ---------------------------------

    def browse(self):
        path = self.q("path")
        self.send_json(200, {"path": path, "children": core.bus.browse(path)})

    def read_node(self):
        path = self.q("path")
        if not path:
            raise HttpError(400, "path is missing")
        self.send_json(200, {"path": path, "value": core.bus.read(path)})

    def write_node(self):
        path = self.q("path")
        d = self.read_json()
        if not path or "value" not in d:
            raise HttpError(400, 'path and {"value": ...} are needed')
        core.bus.write(path, d["value"])
        self.send_json(200, {"path": path, "value": core.bus.read(path)})

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
