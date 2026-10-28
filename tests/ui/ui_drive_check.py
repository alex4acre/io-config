# SPDX-License-Identifier: MIT

"""UI check of the drive views (SoE parameters, telegram editor, draft confirmation) in headless Firefox, against
the simulated ctrlX WITH the ctrlX DRIVE (IO_CONFIG_FAKE_DRIVE=1) and an ESI library holding the I/O ESIs plus
tests/data/esi-drive. Otherwise set up like tests/ui/ui_check.py.

    IO_CONFIG_FAKE=1 IO_CONFIG_CONFIG=<path of a file that doesn't exist yet> python3 -m app.main   # a FRESH backend each run
    geckodriver --port 4444            # Ubuntu's Firefox snap: /snap/bin/firefox.geckodriver --port 4444
    python3 tests/ui/ui_check.py <screenshot folder> [<XI522001 ESI file to upload>]
                                       # with the Firefox snap: both under ~/snap/firefox/common
The backend's ESI library must hold the XB-EC-12, XI190202 and XI422202 files (IO_CONFIG_ESI), not XI522001.

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
UPLOAD = sys.argv[2] if len(sys.argv) > 2 else None      # path of an ESI file Firefox may read (XI522001)
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


def find(css):
    return next(iter(call("POST", S + "/element", {"using": "css selector", "value": css}).values()))


def text(id_):
    return js(f"return document.getElementById('{id_}').textContent")


try:
    call("POST", S + "/url", {"url": URL})
    check(until("document.getElementById('conn').classList.contains('ok')"), "connection chip is green")
    js("document.getElementById('scan').click();")
    check(until("document.querySelectorAll('#devices tbody tr').length === 5", 20), "scan shows 5 devices")
    row4 = js("return [...document.querySelectorAll('#devices tbody tr')[4].cells].map(c => c.textContent)")
    check(row4[1] == "ctrlX DRIVE XCS2-F/H (SoE)" and row4[5] == "Init" and row4[6] == "other rev.", f"drive row {row4}")
    js("document.querySelectorAll('#devices tbody tr')[4].click();")
    check(until("document.getElementById('d-title').textContent.startsWith('4:')"), "drive detail opens")
    check(js("return document.getElementById('d-objects').hidden && !document.getElementById('d-soe').hidden"),
          "SoE button shown, CoE button hidden")

    js("document.getElementById('d-soe').click();")
    check(until("document.querySelectorAll('#s-list tbody tr').length > 100", 15), "SoE parameter list loads")
    js("document.getElementById('s-filter').value = 'S-0-0047'; document.getElementById('s-filter').oninput();")
    check(until("document.querySelectorAll('#s-list tbody tr').length === 1"), "filter finds S-0-0047")
    js("document.querySelector('#s-list tbody tr').click();")
    check(until("document.getElementById('s-param').textContent.includes('Device not found')"),
          "online read explains the unconfigured drive")
    shot("soe-unconfigured")

    js("document.getElementById('d-edit').click();")
    check(until("!document.getElementById('e-soe').hidden && document.querySelectorAll('#e-at tbody tr').length === 7"),
          "editor shows the AT with 7 IDNs")
    js("document.getElementById('e-at-add').value = 'S-0-0051 Encoder'; document.getElementById('e-at-btn').click();")
    check(until("document.querySelectorAll('#e-at tbody tr').length === 8"), "S-0-0051 added to the AT")
    js("document.getElementById('e-name').value = 'Drive1'; document.getElementById('e-save').click();")
    check(until("document.getElementById('e-msg').textContent.startsWith('Saved')"), "drive edit saved")
    shot("drive-editor")

    js("document.getElementById('plan-devices').tBodies[0].replaceChildren(); document.getElementById('build').click();")
    check(until("document.querySelector('#plan-devices tbody').textContent.includes('Drive1')"), "plan lists Drive1")
    check("DRAFT" in text("plan-messages"), "plan says the drive part is a draft")
    js("window.confirm = () => true; document.getElementById('write').click();")
    check(until("document.getElementById('dl-title').textContent === 'Written'", 30), "draft written after both confirmations")
    js("document.querySelector('#s-list tbody tr').click();")
    check(until("document.getElementById('s-param').textContent.includes('Value')", 15), "S-0-0047 read online once configured")
    shot("soe-configured")
finally:
    call("DELETE", S)

print("FAILED: " + "; ".join(failures) if failures else "ALL OK")
sys.exit(1 if failures else 0)
