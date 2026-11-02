# SPDX-License-Identifier: MIT

"""End-to-end UI check of a project without devices and without a master, in headless Firefox (see ui_check.py).

    IO_CONFIG_FAKE=1 IO_CONFIG_FAKE_GONE=1 IO_CONFIG_CONFIG=<new path> IO_CONFIG_ESI=<dir with XB-EC-12, XI190202, XI422202> python3 -m app.main
    /snap/bin/firefox.geckodriver --port 4444
    python3 tests/ui/ui_project_check.py ~/snap/firefox/common/io-config-ui/shots-project
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


def find(css):
    return next(iter(call("POST", S + "/element", {"using": "css selector", "value": css}).values()))


def text(id_):
    return js(f"return document.getElementById('{id_}').textContent")


def rows():
    return js("return [...document.querySelectorAll('#devices tbody tr')].map((r) => [r.cells[1].textContent, r.cells[5].textContent])")


def add(search):
    js(f"const s = document.getElementById('add-search'); s.value = '{search}'; s.dispatchEvent(new Event('input'))")
    check(until(f"document.getElementById('add-device').options.length >= 1 && "
                f"document.getElementById('add-device').options[0].textContent.includes('{search}')"), f"library search finds {search}")
    n = len(rows())
    js("document.getElementById('add').click()")
    check(until(f"document.querySelectorAll('#devices tbody tr').length === {n + 1}"), f"{search} added to the list")


try:
    call("POST", S + "/url", {"url": URL})
    check(until("document.getElementById('conn').textContent.includes('connected')"), "page loaded")
    check(js("return !document.getElementById('noscan').hidden"), "not scanned yet")
    check(js("return !document.getElementById('bus-card').hidden && !document.getElementById('config-card').hidden"),
          "devices and configuration cards without a scan")

    js("document.getElementById('add-box').open = true")
    check(until("document.getElementById('add-device').options.length >= 3"), "the library's devices to choose from")
    add("XB-EC-12")
    add("XI190202")
    add("XI422202")
    check(until("document.getElementById('add-after').options.length === 4"), "insert after lists the devices")
    check(rows() == [["XB-EC-12", "not present"], ["XI190202", "not present"], ["XI422202", "not present"]], f"list {rows()}")
    check(not js("return document.getElementById('project-reset').hidden"), "reset button with a project")
    shot("project")

    js("document.querySelectorAll('#devices tbody tr')[2].querySelector('button').click()")      # Up
    check(until("document.querySelectorAll('#devices tbody tr')[1].cells[1].textContent === 'XI422202'"), "moved up")
    js("document.querySelectorAll('#devices tbody tr')[1].querySelectorAll('button')[1].click()")  # Down
    check(until("document.querySelectorAll('#devices tbody tr')[1].cells[1].textContent === 'XI190202'"), "moved down")

    js("document.querySelectorAll('#devices tbody tr')[2].click()")
    check(until("!document.getElementById('detail').hidden && !document.getElementById('p-settings').hidden"),
          "an absent device opens on Settings")
    tabs = js("return [...document.querySelectorAll('.subtabs .tab')].filter((t) => !t.hidden).map((t) => t.dataset.panel)")
    check(tabs == ["settings", "startup", "info"], f"tabs of an absent CoE device {tabs}")
    check(js("return document.getElementById('d-writable').disabled"), "Writable off for an absent device")
    check(until("document.getElementById('e-name').value === 'XI422202'"), "its name in Settings")
    shot("absent-device")
    js("document.querySelectorAll('#devices tbody tr')[1].click()")
    check(until("!document.getElementById('d-startup').hidden === false"), "no Startup tab without CoE/SoE (XI190202)")

    js("document.getElementById('build').click()")
    check(until("!document.getElementById('plan').hidden"), "builds without a scan")
    msgs = text("plan-messages")
    check(msgs.count("not present on the bus") == 3, f"a warning per absent device: {msgs}")
    check(js("return !document.getElementById('write').disabled"), "can be written")
    shot("plan")

    js("window.confirm = () => true; document.getElementById('write').click()")
    check(until("document.getElementById('dl-title').textContent.startsWith('Written, with warnings')", 60),
          "written with warnings: " + text("dl-title"))
    check("next master start" in text("dl-steps"), "takes effect at the next master start")
    check(until("document.querySelectorAll('#devices tbody tr').length === 3"), "the project stays listed")
    check(until("document.getElementById('m-state').textContent.startsWith('unavailable - no EtherCAT master instance')"),
          "master shown as unavailable, with the reason: " + text("m-state"))
    shot("written")

    js("document.querySelectorAll('#devices tbody tr')[0].querySelectorAll('button')[2].click()")  # Remove
    check(until("document.querySelectorAll('#devices tbody tr').length === 2"), "removed")
    errors = js("return window.__errors || []")
    check(not errors, f"no script errors {errors}")
finally:
    try:
        call("DELETE", S)
    except Exception as e:
        print(e)

print("FAILED: " + "; ".join(failures) if failures else "all good")
sys.exit(1 if failures else 0)
