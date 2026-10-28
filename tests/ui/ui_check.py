# SPDX-License-Identifier: MIT

"""End-to-end UI check in headless Firefox via WebDriver (geckodriver), against the simulated ctrlX.

    IO_CONFIG_FAKE=1 IO_CONFIG_CONFIG=<path of a file that doesn't exist yet> python3 -m app.main   # a FRESH backend each run
    geckodriver --port 4444            # Ubuntu's Firefox snap: /snap/bin/firefox.geckodriver --port 4444
    python3 tests/ui/ui_check.py <screenshot folder>    # with the Firefox snap: a folder under ~/snap/firefox/common

Plain WebDriver over HTTP (no selenium needed). Stop the backend and geckodriver by PID afterwards.
"""

import base64
import json
import os
import sys
import time
import urllib.request

WD = "http://127.0.0.1:4444"
PORT = int(os.environ.get("IO_CONFIG_PORT", "12617"))
URL = f"http://127.0.0.1:{PORT}/io-config/?token=dev"
OUT = sys.argv[1] if len(sys.argv) > 1 else None
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
failures = []


def call(method, path, body=None):
    req = urllib.request.Request(WD + path, method=method, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with opener.open(req, timeout=60) as r:
        return json.loads(r.read())["value"]


sid = call("POST", "/session", {"capabilities": {"alwaysMatch": {"browserName": "firefox",
          "moz:firefoxOptions": {"args": ["-headless", "--width=1300", "--height=800"]}}}})["sessionId"]
S = f"/session/{sid}"


def js(script):
    return call("POST", S + "/execute/sync", {"script": script, "args": []})


def until(expr, timeout=8):
    end = time.time() + timeout
    while time.time() < end:
        if js("return (" + expr + ")"):
            return True
        time.sleep(0.2)
    return False


def check(cond, text):
    print(("ok   " if cond else "FAIL ") + text, flush=True)
    if not cond:
        failures.append(text)


def shot(name):
    if OUT:
        os.makedirs(OUT, exist_ok=True)
        with open(os.path.join(OUT, name + ".png"), "wb") as f:
            f.write(base64.b64decode(call("GET", S + "/screenshot")))


try:
    call("POST", S + "/url", {"url": URL})
    check(until("document.getElementById('counter').textContent !== '-'"), "state is shown")
    check(until("document.getElementById('conn').classList.contains('ok')"), "connection chip is green")
    js("document.getElementById('setpoint').value = '12.5'; document.getElementById('save').click();")
    check(until("document.getElementById('banner').textContent === 'Saved'"), "setpoint saved")
    with opener.open(f"http://127.0.0.1:{PORT}/io-config/api/v1/config", timeout=5) as r:
        check(json.loads(r.read())["setpoint"] == 12.5, "the backend has the new setpoint")
    js("document.getElementById('path').value = 'demo/flag'; document.getElementById('read').click();")
    check(until("document.getElementById('result').textContent.trim() === 'false'"), "Data Layer read")
    js("document.getElementById('value').value = 'true'; document.getElementById('write').click();")
    check(until("document.getElementById('result').textContent.trim() === 'true'"), "Data Layer write")
    js("document.getElementById('path').value = 'no/such/node'; document.getElementById('read').click();")
    check(until("!document.getElementById('banner').hidden && "
                "document.getElementById('banner').textContent.includes('INVALID_ADDRESS')"), "error shown in the banner")
    shot("main")
finally:
    call("DELETE", S)

print("FAILED: " + "; ".join(failures) if failures else "ALL OK")
sys.exit(1 if failures else 0)
