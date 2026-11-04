# SPDX-License-Identifier: MIT

"""UI check of a modular device (Elmo Titanium: modules per slot, DC mode) in headless Firefox, against the
simulated ctrlX WITH the Elmo (IO_CONFIG_FAKE_ELMO=1) and an ESI library holding the I/O ESIs plus
tests/data/esi-elmo. Otherwise set up like tests/ui/ui_check.py.

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
    js("document.getElementById('tab-config').click(); document.querySelector('#cfg-devices tbody tr:not(.listed) button').click();")
    until("!document.querySelector('#cfg-devices tbody tr:not(.listed)')")
    js("document.getElementById('tab-config').click(); document.querySelectorAll('#cfg-devices tbody tr')[4].cells[2].click();")
    check(until("document.getElementById('c-title').textContent.startsWith('4:')"), "Elmo card of the Configuration tab opens")
    js("document.getElementById('d-edit').click();")
    check(until("!document.getElementById('e-modules-box').hidden && document.querySelectorAll('#e-modules tbody tr').length === 5"),
          "Settings shows 5 slots")
    sel = js("return [...document.querySelectorAll('#e-modules select')].map(s => s.options[s.selectedIndex].text)")
    check(sel[:3] == ["Safety Module Basic PDO (#x288110)", "Cyclic Position Mode (#x0100)", "Cyclic Position Mode (#x0100)"],
          f"default modules {sel}")
    check(js("return document.getElementById('e-dc').value") == "DC Sync", "DC mode DC Sync")
    check(js("return document.querySelectorAll('#e-pdos tbody tr.pdo').length") == 6, "6 module PDOs listed")
    js("document.getElementById('e-modules-box').scrollIntoView();")
    shot("elmo-settings-defaults")
    js("const s = document.querySelectorAll('#e-modules select'); s[2].value = '512'; s[2].onchange();")
    check(until("document.querySelector('#e-pdos tbody').textContent.includes('Axis2_Slot1 Cyclic Velocity Mode Outputs')"),
          "PDO table follows a module change before Save")
    check(js("return document.getElementById('e-msg').textContent").startswith("Not saved yet"), "not-saved note")
    check(js("return document.getElementById('e-sizes').textContent").startswith("23 B inputs, 23 B outputs"),
          f"sizes follow: {js('return document.getElementById(' + chr(39) + 'e-sizes' + chr(39) + ').textContent')}")
    js("const s = document.querySelectorAll('#e-modules select'); s[0].value = '0'; s[0].onchange(); s[2].value = '0'; s[2].onchange();")
    check(until("document.querySelectorAll('#e-pdos tbody tr.pdo').length === 2"), "2 PDOs shown before Save")
    js("document.getElementById('e-name').value = 'Elmo1'; document.getElementById('e-save').click();")
    check(until("document.getElementById('e-msg').textContent.startsWith('Saved')"), "module edit saved")
    check(until("document.querySelectorAll('#e-pdos tbody tr.pdo').length === 2"), "2 PDOs after the edit")
    js("document.getElementById('e-modules-box').scrollIntoView();")
    shot("elmo-settings-edited")
    js("document.getElementById('plan-devices').tBodies[0].replaceChildren(); document.getElementById('build').click();")
    check(until("document.querySelector('#plan-devices tbody').textContent.includes('Elmo1')"), "plan lists Elmo1")
    js("window.confirm = () => true; document.getElementById('write').click();")
    check(until("document.getElementById('dl-title').textContent === 'Written'", 30), "configuration written")
    js("document.getElementById('tab-bus').click(); document.querySelectorAll('#devices tbody tr')[4].click();")
    check(until("document.body.textContent.includes('Axis1_Slot2 = Cyclic Position Mode')", 15), "Info lists the modules")
    shot("elmo-written")
finally:
    call("DELETE", S)

print("FAILED: " + "; ".join(failures) if failures else "ALL OK")
sys.exit(1 if failures else 0)
