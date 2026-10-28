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

    # configuration: build (identical), change the cycle time, write, backup
    js("document.getElementById('build').click();")
    check(until("!document.getElementById('plan').hidden && "
                "document.getElementById('plan-summary').textContent.startsWith('Identical')"), "built configuration is identical")
    check(js("return document.querySelectorAll('#plan-devices tbody tr').length") == 4, "plan lists 4 devices")
    js("document.getElementById('cycle').value = '1000'; document.getElementById('build').click();")
    check(until("document.getElementById('plan-summary').textContent.includes('cycle time 1000')"), "cycle time change differs")
    check("no variable changes" in js("return document.querySelector('#plan-vars summary').textContent"),
          "cycle time change keeps the variables")
    shot("plan")
    js("window.confirm = () => true; document.getElementById('write').click();")
    check(until("document.getElementById('dl-title').textContent === 'Written'", 20), "configuration written")
    steps = text("dl-steps")
    check("checksums match" in steps and "Op" in steps and "backup" in steps, f"download steps: {steps}")
    check(until("document.getElementById('m-cycle').textContent === '1000 µs'", 20), "rescan shows the new cycle time")
    js("document.getElementById('backups-box').open = true;")
    check(until("document.querySelectorAll('#backups tbody tr button').length === 2"), "one backup listed")
    shot("written")

    # editor: XI422202 -> compact state PDO, rename, a startup parameter from the objects view
    js("document.querySelectorAll('#devices tbody tr')[2].click();")
    check(until("document.getElementById('d-title').textContent === '2: XI422202'"), "detail of XI422202 again")
    js("document.getElementById('d-edit').click();")
    check(until("!document.getElementById('editor').hidden && document.querySelectorAll('#e-pdos tbody tr').length === 6"),
          "editor lists 6 PDOs")
    js("""const boxes = [...document.querySelectorAll('#e-pdos input[type=checkbox]')];
          for (const b of boxes) { const i = Number(b.dataset.index); if ([0x1a00, 0x1a01, 0x1a02].includes(i)) b.checked = false;
            if (i === 0x1a03) b.checked = true; b.onchange && b.onchange(); }
          document.getElementById('e-name').value = 'AO1';""")
    check("2 B inputs, 4 B outputs" in text("e-sizes"), f"editor sizes: {text('e-sizes')}")
    js("document.getElementById('e-save').click();")
    check(until("document.getElementById('e-msg').textContent.startsWith('Saved')"), "edit saved")
    js("document.getElementById('d-objects').click();")
    check(until("document.querySelectorAll('#o-list tbody tr').length === 30", 15), "30 objects listed")
    js("[...document.querySelectorAll('#o-list tbody tr')].find(r => r.cells[0].textContent === '#x8000').click();")
    check(until("document.getElementById('o-obj-title').textContent.includes('Channel 1 settings')", 15), "object 0x8000 opens")
    js("""const r = [...document.querySelectorAll('#o-entries tbody tr')].find(r => r.cells[1].textContent === 'User gain');
          r.querySelector('input').value = '1000'; [...r.querySelectorAll('button')].find(b => b.textContent === 'Startup').click();""")
    check(until("document.getElementById('banner').textContent.includes('Startup parameter #x8000:1 = 1000 saved')"),
          "startup parameter added")
    shot("objects")
    check(until("[...document.querySelectorAll('#e-startup tbody tr')].some(r => r.cells[0].textContent === '#x8000:1')"),
          "editor shows the startup parameter")
    js("document.getElementById('plan-devices').tBodies[0].replaceChildren(); document.getElementById('build').click();")
    check(until("document.querySelector('#plan-devices tbody').textContent.includes('AO1')"), "plan uses the new name")
    check(js("return document.getElementById('plan-summary').textContent").startswith("Differs"), "edited configuration differs")
    shot("editor")
    js("document.getElementById('allow-writes').click();")
    until("""[...document.querySelectorAll('#o-entries tbody tr')].some(r => r.cells[1].textContent === 'User gain' &&
             [...r.querySelectorAll('button')].some(b => b.textContent === 'Write now' && !b.disabled))""")
    js("""const r = [...document.querySelectorAll('#o-entries tbody tr')].find(r => r.cells[1].textContent === 'User gain');
          window.confirm = () => true; [...r.querySelectorAll('button')].find(b => b.textContent === 'Write now').click();""")
    check(until("document.getElementById('banner').textContent.includes('can only be written in PreOp')"),
          "online write of a PreOp-only object is explained")

    # process data
    js("document.getElementById('tab-pd').click();")
    check(until("document.querySelectorAll('#pd tbody tr').length === 44"), "44 process data variables")
    js("document.getElementById('pd-filter').value = 'Channel_1.Value'; document.getElementById('pd-filter').oninput();")
    check(until("document.querySelectorAll('#pd tbody tr').length === 1"), "filter finds the analog output")
    js("""const r = document.querySelector('#pd tbody tr'); r.querySelector('input').value = '1234';
          r.querySelector('button').click();""")
    check(until("document.querySelector('#pd tbody tr') && document.querySelector('#pd tbody tr').cells[3].textContent === '1234'"),
          "output written and read back")
    shot("process-data")
    js("document.getElementById('allow-writes').click(); document.getElementById('tab-bus').click();")

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
