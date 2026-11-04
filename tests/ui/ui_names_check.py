# SPDX-License-Identifier: MIT

"""UI check of the Names tab (1.9.0), in headless Firefox: name a variable, refused name, build shows the rename,
write, the Bus tab's Process data shows the name with the standard name as a tooltip.

    IO_CONFIG_FAKE=1 IO_CONFIG_CONFIG=<new path> IO_CONFIG_ESI=<dir with XB-EC-12, XI190202, XI422202> python3 -m app.main
    /snap/bin/firefox.geckodriver --port 4444
    python3 tests/ui/ui_names_check.py ~/snap/firefox/common/io-config-ui/shots-names
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
          "moz:firefoxOptions": {"args": ["-headless", "--width=1300", "--height=1000"]}}}})["sessionId"]
S = f"/session/{sid}"


def js(script):
    return call("POST", S + "/execute/sync", {"script": script, "args": []})


def until(script, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        if js("return !!(" + script + ")"):
            return True
        time.sleep(0.2)
    return False


def check(ok, what):
    print(("ok   " if ok else "FAIL ") + what)
    if not ok:
        failures.append(what)


def text(i):
    return js(f"return document.getElementById('{i}').textContent")


def shot(name):
    if OUT:
        os.makedirs(OUT, exist_ok=True)
        with open(os.path.join(OUT, name + ".png"), "wb") as f:
            f.write(base64.b64decode(call("GET", S + "/screenshot")))


def type_name(row, value):
    js(f"document.querySelectorAll('#e-names tbody input')[{row}].value = {json.dumps(value)}")


try:
    call("POST", S + "/url", {"url": URL})
    js("window.__errors = []; window.addEventListener('error', (e) => window.__errors.push(e.message)); window.confirm = () => true")
    js("document.getElementById('scan').click()")
    check(until("document.querySelectorAll('#devices tbody tr').length > 0", 40), "bus scanned")
    js("document.getElementById('tab-config').click()")
    check(until("document.querySelectorAll('#cfg-devices tbody tr.listed').length > 1"), "list shown")
    js("""for (const tr of document.querySelectorAll('#cfg-devices tbody tr.listed'))
            if (tr.textContent.includes('XI190202')) { tr.cells[2].click(); break; }""")
    check(until("!document.getElementById('cdetail').hidden"), "device card opens")
    js("document.getElementById('d-names').click()")
    check(until("document.querySelectorAll('#e-names tbody input').length > 0"), "Names tab lists the variables")
    check(js("return window.tipsMissing") == [], "every tooltip finds its element: " + str(js("return window.tipsMissing")))
    check(js("return document.querySelector('#e-names thead th').title") != "", "the Names columns have tooltips")
    check("/data/XI190202/" in text("n-example"), "example path: " + text("n-example"))
    shot("names-empty")
    standard = js("return document.querySelector('#e-names tbody tr').cells[1].textContent")
    # refused: stays typed, the message is beside the button
    type_name(0, "1 bad")
    js("document.getElementById('n-save').click()")
    check(until("document.getElementById('n-msg').className === 'error'"), "bad name refused beside the button: " + text("n-msg"))
    check(js("return document.querySelector('#e-names tbody input').value") == "1 bad", "the typed name stays")
    # saved
    type_name(0, "MyInput1")
    js("document.getElementById('n-save').click()")
    check(until("document.getElementById('n-msg').className === 'ok'"), "name saved: " + text("n-msg"))
    check("/data/XI190202/MyInput1" in text("n-example"), "example path follows: " + text("n-example"))
    # typed in Names, Settings saved: the typed text stays
    type_name(1, "StillTyping")
    js("document.getElementById('d-edit').click()")
    check(until("!document.getElementById('p-settings').hidden"), "Settings tab")
    js("document.getElementById('e-save').click()")
    check(until("document.getElementById('e-msg').className === 'ok'"), "Settings saved")
    js("document.getElementById('d-names').click()")
    check(js("return document.querySelectorAll('#e-names tbody input')[1].value") == "StillTyping", "unsaved name survives a Settings save")
    type_name(1, "")
    # build shows the rename
    js("document.getElementById('build').click()")
    check(until("!document.getElementById('plan').hidden && document.getElementById('plan-vars').querySelector('summary').textContent.includes('1 renamed')"),
          "the plan counts the rename: " + js("return document.getElementById('plan-vars').querySelector('summary').textContent"))
    check(f"XI190202/{standard} -> XI190202/MyInput1" in text("plan-vars-body"), "the plan shows old -> new: " + text("plan-vars-body"))
    shot("names-plan")
    # write, then the Bus tab shows it
    js("document.getElementById('write').click()")
    check(until("document.getElementById('dl-title').textContent.startsWith('Written')", 60), "written: " + text("dl-title"))
    check(until("!document.getElementById('scan').disabled", 30), "scan after the write")
    js("document.getElementById('tab-bus').click()")
    js("""for (const tr of document.querySelectorAll('#devices tbody tr'))
            if (tr.textContent.includes('XI190202')) { tr.cells[1].click(); break; }""")
    check(until("!document.getElementById('detail').hidden"), "Bus device card opens")
    check(until("document.getElementById('pd').tBodies[0].textContent.includes('MyInput1')"), "Process data shows the name")
    check(js("return document.getElementById('pd').tBodies[0].rows[0].cells[0].querySelector('span').title.includes(" + json.dumps(standard) + ")"),
          "tooltip gives the standard name")
    check(until("document.getElementById('pd').tBodies[0].rows[0].cells[3].textContent !== ''"), "the renamed variable has a value")
    shot("names-bus")
    errors = js("return window.__errors || []")
    check(not errors, f"no script errors {errors}")
finally:
    try:
        call("DELETE", S)
    except Exception as e:
        print(e)

print("FAILED: " + "; ".join(failures) if failures else "all good")
sys.exit(1 if failures else 0)
