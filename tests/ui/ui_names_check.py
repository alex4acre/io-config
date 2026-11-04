# SPDX-License-Identifier: MIT

"""UI check of variable names in the Settings PDO table (1.9.1), in headless Firefox: name a variable, refused name, build shows the rename,
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


def inputs():
    return "document.querySelectorAll('#e-pdos input[type=text]')"


def type_name(row, value):
    js(f"{inputs()}[{row}].value = {json.dumps(value)}")


try:
    call("POST", S + "/url", {"url": URL})
    js("window.__errors = []; window.addEventListener('error', (e) => window.__errors.push(e.message)); window.confirm = () => true")
    js("document.getElementById('scan').click()")
    check(until("document.querySelectorAll('#devices tbody tr').length > 0", 40), "bus scanned")
    js("document.getElementById('tab-config').click()")
    check(until("document.querySelectorAll('#cfg-devices tbody tr.listed').length > 1"), "list shown")
    # a device with selectable PDOs: ticking shows the fields, what was typed stays, nothing is saved here
    js("""for (const tr of document.querySelectorAll('#cfg-devices tbody tr.listed'))
            if (tr.textContent.includes('XI422202')) { tr.cells[2].click(); break; }""")
    check(until("document.getElementById('c-title').textContent.includes('XI422202') && " + inputs() + ".length > 0"), "XI422202 Settings has name fields")
    fields = js(f"return {inputs()}.length")
    unticked = js("""const b = [...document.querySelectorAll('#e-pdos input[type=checkbox]')].find((c) => !c.checked && !c.disabled);
        b.click(); return b.dataset.index""")
    box = "document.querySelector('#e-pdos input[type=checkbox][data-index=\"" + unticked + "\"]')"
    check(until(f"{inputs()}.length > {fields}"), "ticking a PDO shows its name fields before Save")
    check(js("return document.activeElement.type") == "checkbox", "the ticked box keeps the focus")
    type_name(0, "StillTyping")
    js(f"{box}.click()")
    check(until(f"{inputs()}.length === {fields}"), "unticking takes them away")
    check(js(f"return {inputs()}[0].value") == "StillTyping", "a typed name survives ticking a PDO")
    js("""for (const tr of document.querySelectorAll('#cfg-devices tbody tr.listed'))
            if (tr.textContent.includes('XI190202')) { tr.cells[2].click(); break; }""")
    check(until("document.getElementById('c-title').textContent.includes('XI190202') && !document.getElementById('p-settings').hidden"), "device card opens on Settings")
    check(until(f"{inputs()}.length && {inputs()}[0].placeholder.includes('Channel_1_Sample')"), "XI190202's fields, nothing typed carried over")
    check(js(f"return {inputs()}[0].value") == "", "the other device's typed name is gone")
    check(js("return !document.getElementById('d-names')"), "no Names tab any more")
    check(until(inputs() + ".length > 0"), "the PDO table has name fields")
    check(js("return window.tipsMissing") == [], "every tooltip finds its element: " + str(js("return window.tipsMissing")))
    check(js("return document.querySelectorAll('#e-pdos thead th')[5].title") != "", "the Variable name column has a tooltip")
    shot("names-empty")
    standard = js(f"return {inputs()}[0].placeholder")
    # layout: one variable on the PDO's row, several on sub-rows, a PDO that isn't ticked has none
    check(js(f"return {inputs()}[0].closest('tr').classList.contains('pdo')"), "a PDO with one variable has its field on its row")
    check(js("return document.querySelectorAll('#e-pdos tr.var input[type=text]').length") == 3, "Device_state has 3 sub-rows with fields")
    # refused: stays typed, the message is beside the Save button
    type_name(0, "1 bad")
    js("document.getElementById('e-save').click()")
    check(until("document.getElementById('e-msg').className === 'error'"), "bad name refused beside Save: " + text("e-msg"))
    check(js(f"return {inputs()}[0].value") == "1 bad", "the typed name stays")
    type_name(0, "MyInput1")
    # the one Save button saves it
    js("document.getElementById('e-save').click()")
    check(until("document.getElementById('e-msg').className === 'ok'"), "saved with the Settings Save: " + text("e-msg"))
    check(until(f"{inputs()}[0].dataset.saved === 'MyInput1'"), "the field shows the saved name")
    js("document.getElementById('e-pdos').scrollIntoView({ block: 'center' })")
    shot("names-saved")
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
