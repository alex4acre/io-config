# SPDX-License-Identifier: MIT

"""End-to-end UI check in headless Firefox via WebDriver (geckodriver), against the simulated ctrlX.

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
    check(until("!document.getElementById('noscan').hidden"), "not scanned yet")
    js("document.getElementById('scan').click();")
    check(until("document.querySelectorAll('#devices tbody tr').length === 4"), "scan shows 4 devices")
    names = js("return [...document.querySelectorAll('#devices tbody tr')].map(r => r.cells[1].textContent)")
    check(names == ["XB_EC_12", "XI190202", "XI422202", "XI522001"], f"device names {names}")
    check("op" in text("m-state") and "4 connected, 4 configured" in text("m-devices"), "master overview")
    esi = js("return [...document.querySelectorAll('#devices tbody tr')].map(r => r.cells[6].textContent)")
    check(esi == ["exact", "exact", "exact", "missing"], f"ESI column {esi} (XI522001 has no ESI yet)")
    check(js("return document.querySelectorAll('#devices tbody tr')[3].cells[9].textContent") == "Check (1)", "XI522001 flagged")
    js("document.querySelectorAll('#devices tbody tr')[2].click();")
    check(until("!document.getElementById('detail').hidden && "
                "document.getElementById('d-title').textContent === '2: XI422202'"), "detail of XI422202 opens")
    check(until("document.querySelectorAll('#d-pdos tbody tr').length === 6"), "6 PDOs side by side")
    check("agree" in text("d-findings"), "no findings for XI422202")
    check(js("return document.querySelectorAll('#d-vars tbody tr').length") == 8, "8 process image variables")
    check(js("return document.querySelectorAll('#d-init tbody tr').length") == 2, "2 startup parameters (ENI)")
    shot("bus")
    js("document.querySelectorAll('#devices tbody tr')[3].click();")
    check(until("document.getElementById('d-findings').textContent.includes('No ESI file')"), "XI522001 finding shown")
    shot("missing-esi")

    js("document.getElementById('tab-esi').click();")
    check(until("document.getElementById('esi-count').textContent.startsWith('3 files, 5 devices')"), "library shows 3 files")
    if UPLOAD:
        call("POST", S + f"/element/{find('#esi-files')}/value", {"text": UPLOAD})
        js("document.getElementById('esi-upload').click();")
        check(until("document.getElementById('esi-result').textContent.includes('1 device added')"), "ESI uploaded")
        check(until("document.getElementById('esi-count').textContent.startsWith('4 files, 6 devices')"), "library has 4 files")
        js("document.getElementById('esi-search').value = 'serial'; document.getElementById('esi-search').oninput();")
        check(until("document.querySelectorAll('#esi-devices tbody tr').length === 1"), "search finds XI522001")
        shot("esi")
        js("document.getElementById('tab-bus').click(); document.getElementById('scan').click();")
        check(until("document.querySelectorAll('#devices tbody tr')[3] && "
                    "document.querySelectorAll('#devices tbody tr')[3].cells[6].textContent === 'exact'"),
              "after a rescan XI522001 has its ESI")
finally:
    call("DELETE", S)

print("FAILED: " + "; ".join(failures) if failures else "ALL OK")
sys.exit(1 if failures else 0)
