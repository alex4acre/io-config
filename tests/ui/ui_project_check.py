# SPDX-License-Identifier: MIT

"""End-to-end UI check of the device list as a configuration, in headless Firefox (see ui_check.py). Two runs:
without devices and without a master (IO_CONFIG_FAKE_GONE=1 for the backend and the check) and on the fake bus.

    IO_CONFIG_FAKE=1 [IO_CONFIG_FAKE_GONE=1] IO_CONFIG_CONFIG=<new path> IO_CONFIG_ESI=<dir with XB-EC-12, XI190202, XI422202> python3 -m app.main
    /snap/bin/firefox.geckodriver --port 4444
    [IO_CONFIG_FAKE_GONE=1] python3 tests/ui/ui_project_check.py ~/snap/firefox/common/io-config-ui/shots-project
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
GONE = bool(os.environ.get("IO_CONFIG_FAKE_GONE"))
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


def names():
    return [r[0] for r in rows()]


def keys(css, text_):
    call("POST", S + f"/element/{find(css)}/value", {"text": text_})


def sugs():
    return js("return [...document.querySelectorAll('#add-suggest .sug')].map((li) => li.textContent)")


def field(id_):
    return js(f"return document.getElementById('{id_}').value")


def pick(search, name=None):
    """Adds a device the way a user does: type, arrow keys, Enter."""
    js("document.getElementById('add-device').value = ''")
    keys("#add-device", search)
    check(until(f"document.querySelectorAll('#add-suggest .sug').length >= 1 && "
                f"document.querySelector('#add-suggest .sug').textContent.includes('{search}')"), f"suggestions for {search}")
    time.sleep(0.8)                                                   # the list follows the typed text (debounce + request)
    keys("#add-device", "\ue015")                                    # ArrowDown: preview
    check(field("add-device").startswith(search + " - "), f"ArrowDown fills the box: {field('add-device')}")
    check(js("return document.getElementById('add').disabled"), "Add stays disabled while only previewing")
    keys("#add-device", "\ue007")                                    # Enter: pick
    check(not js("return document.getElementById('add').disabled") and js("return document.getElementById('add-suggest').hidden"),
          "Enter picks the device and enables Add")
    if name:
        keys("#add-name", name)
    n = len(rows())
    js("document.getElementById('add').click()")
    check(until(f"document.querySelectorAll('#devices tbody tr').length === {n + 1}"), f"{search} added to the list")
    return names()[-1]


def tools(i):
    return js(f"return [...document.querySelectorAll('#devices tbody tr')[{i}].querySelectorAll('.row-tools button')].map((b) => b.textContent)")


def click_tool(i, n):
    js(f"document.querySelectorAll('#devices tbody tr')[{i}].querySelectorAll('.row-tools button')[{n}].click()")


def configuration_state(on):
    check(until(f"document.getElementById('config-flag').hidden === {str(not on).lower()}"), f"Configuration flag {'shown' if on else 'hidden'}")
    if on:
        check(text("config-flag") == "Configuration", "flag reads exactly Configuration: " + text("config-flag"))
        check(not js("return document.getElementById('devices-hint').innerText.includes('process data')"),
              "hint without process data in the configuration state")
        check(js("return [...document.querySelectorAll('#devices tbody tr')].every((r) => r.cells[5].textContent === '-')"),
              "State column - for every row")
        check("not present" not in js("return document.getElementById('devices').textContent"), "no 'not present' badge")


try:
    call("POST", S + "/url", {"url": URL})
    check(until("document.getElementById('conn').textContent.includes('connected')"), "page loaded")
    if GONE:
        check(js("return !document.getElementById('noscan').hidden"), "not scanned yet")
        check(js("return !document.getElementById('bus-card').hidden && !document.getElementById('config-card').hidden"),
              "devices and configuration cards without a scan")
    else:
        js("document.getElementById('scan').click()")
        check(until("document.querySelectorAll('#devices tbody tr').length === 4 && !document.getElementById('scan').disabled", 30),
              "scan finds the 4 devices")
        configuration_state(False)
        check(js("return document.getElementById('devices').tBodies[0].rows[1].cells[5].textContent !== '-'"), "live State column")
        # arrows on an on-bus row
        check(tools(1) == ["\u25b2", "\u25bc", "Remove"], f"arrows and Remove on an on-bus row {tools(1)}")
        check(js("return document.querySelectorAll('#devices tbody tr')[1].querySelector('.row-tools button').title") == "Move up in the list",
              "title of the up arrow")
        click_tool(1, 1)                                              # XI190202 down
        check(until("document.querySelectorAll('#devices tbody tr')[2].cells[1].textContent === 'XI190202'"), "on-bus device moved down")
        configuration_state(True)
        shot("configuration-bus")
        js("document.querySelectorAll('#devices tbody tr')[2].click()")
        check(until("!document.getElementById('detail').hidden && !document.getElementById('p-settings').hidden"),
              "a device of the configuration opens on Settings")
        tabs = js("return [...document.querySelectorAll('.subtabs .tab')].filter((t) => !t.hidden).map((t) => t.dataset.panel)")
        check("pd" not in tabs and tabs[0] == "settings", f"no Process data tab {tabs}")
        check(js("return document.getElementById('d-writable').closest('label').hidden"), "Writable hidden")
        js("document.getElementById('d-close').click()")
        click_tool(2, 0)                                              # and back up
        check(until("document.querySelectorAll('#devices tbody tr')[1].cells[1].textContent === 'XI190202'"), "moved back up")
        js("window.confirm = () => true; document.getElementById('project-reset').click()")
        configuration_state(False)
        check(js("return document.getElementById('devices').tBodies[0].rows[1].cells[5].textContent !== '-'"), "live State column again")
        check(js("return document.getElementById('devices').tBodies[0].rows[1].cells[5].textContent !== ''"), "state text back")

    js("document.getElementById('add-box').open = true")
    check(until("document.querySelectorAll('#add-suggest .sug').length >= 3"), "the list opens on focus with an empty query")
    shot("suggestions")
    keys("#add-device", "XB-EC")
    check(until("document.querySelectorAll('#add-suggest .sug').length >= 1"), "suggestions while typing")
    time.sleep(0.8)
    keys("#add-device", "\ue015")
    typed = field("add-device")
    keys("#add-device", "\ue00c")                                    # Escape
    check(field("add-device") == "XB-EC" and js("return document.getElementById('add-suggest').hidden"), f"Escape restores the typed text (was {typed})")
    keys("#add-device", "\ue015")                                    # opens the list again
    check(until("document.querySelectorAll('#add-suggest .sug').length >= 1"), "ArrowDown opens the list")
    keys("#add-device", "\ue015\ue007")
    check(not js("return document.getElementById('add').disabled"), "picked")
    keys("#add-device", "x")
    check(js("return document.getElementById('add').disabled"), "editing the text clears the pick")
    js("document.getElementById('add-device').value = ''; document.getElementById('add-name').value = ''")

    first = len(rows())
    pick("XB-EC-12", "Coupler")
    configuration_state(True)
    check(names()[-1] == "Coupler", f"name given at add shows in the table: {names()}")
    check(field("add-device") == "" and field("add-name") == "", "the form is cleared after an add")
    pick("XI190202")
    pick("XI422202")
    check(len(rows()) == first + 3, f"3 devices added {rows()}")
    check(names()[-3:] == ["Coupler", "XI190202", "XI422202"], f"new devices at the bottom: {names()}")
    check(not js("return document.getElementById('project-reset').hidden"), "reset button with a project")
    # a bad name refuses the add and keeps the form
    n = len(rows())
    keys("#add-device", "XI190202")
    check(until("document.querySelectorAll('#add-suggest .sug').length >= 1 && "
                "document.querySelector('#add-suggest .sug').textContent.includes('XI190202')"), "suggestions for the bad-name add")
    time.sleep(0.8)
    keys("#add-device", "\ue015\ue007")
    keys("#add-name", "1bad")
    js("document.getElementById('add').click()")
    check(until("!document.getElementById('banner').hidden && document.getElementById('banner').textContent.includes('name must start')"),
          "bad name: " + text("banner"))
    check(len(rows()) == n and field("add-name") == "1bad", "nothing added, the form keeps what was typed")
    js("document.getElementById('add-name').value = ''; document.getElementById('add-device').value = ''")
    shot("project")

    last = len(rows()) - 1
    check(tools(last) == ["\u25b2", "\u25bc", "Remove"] and tools(0) == ["\u25b2", "\u25bc", "Remove"], "arrows and Remove on every row")
    check(js("return document.querySelectorAll('#devices tbody tr')[0].querySelector('.row-tools button').disabled") and
          js(f"return document.querySelectorAll('#devices tbody tr')[{last}].querySelectorAll('.row-tools button')[1].disabled"),
          "no up arrow at the top, no down arrow at the bottom")
    before = names()
    click_tool(last, 0)
    check(until(f"document.querySelectorAll('#devices tbody tr')[{last - 1}].cells[1].textContent === '{before[last]}'"), "moved up")
    click_tool(last - 1, 1)
    check(until(f"document.querySelectorAll('#devices tbody tr')[{last}].cells[1].textContent === '{before[last]}'"), "moved down")

    # click-to-edit name
    row_i = last
    js(f"document.querySelectorAll('#devices tbody tr')[{row_i}].cells[1].click()")
    check(until(f"!!document.querySelector('#devices tbody tr:nth-child({row_i + 1}) td input')"), "click on a name opens an input")
    check(js("return document.getElementById('detail').hidden"), "the device card did not open")
    time.sleep(2.5)                                                   # longer than the 1 s poll
    check(js(f"return !!document.querySelector('#devices tbody tr:nth-child({row_i + 1}) td input')"), "the poll keeps the input")
    shot("rename")
    keys(f"#devices tbody tr:nth-child({row_i + 1}) td input", "Zzz\ue00c")           # type, Escape
    check(until(f"document.querySelectorAll('#devices tbody tr')[{row_i}].cells[1].textContent === '{before[last]}'"), "Escape cancels")
    js(f"document.querySelectorAll('#devices tbody tr')[{row_i}].cells[1].click()")
    check(until(f"!!document.querySelector('#devices tbody tr:nth-child({row_i + 1}) td input')"), "input again")
    js(f"document.querySelector('#devices tbody tr:nth-child({row_i + 1}) td input').value = ''")
    keys(f"#devices tbody tr:nth-child({row_i + 1}) td input", "Analog_out\ue007")
    check(until(f"document.querySelectorAll('#devices tbody tr')[{row_i}].cells[1].textContent === 'Analog_out'", 10), "Enter saves the name")
    check(js("return document.getElementById('detail').hidden"), "still no device card")
    js(f"document.querySelectorAll('#devices tbody tr')[{row_i}].click()")
    check(until("!document.getElementById('detail').hidden && !document.getElementById('p-settings').hidden"),
          "a device opens on Settings")
    check(until("document.getElementById('d-title').textContent.includes('Analog_out')"), "card title shows the saved name")
    tabs = js("return [...document.querySelectorAll('.subtabs .tab')].filter((t) => !t.hidden).map((t) => t.dataset.panel)")
    check(tabs == ["settings", "startup", "info"], f"tabs of a CoE device in the configuration {tabs}")
    check(until("document.getElementById('e-name').value === 'Analog_out'"), "its name in Settings")
    check(js("return document.getElementById('d-writable').closest('label').hidden"), "Writable hidden")
    shot("device-in-configuration")
    js("document.getElementById('d-close').click()")

    js("document.getElementById('build').click()")
    check(until("!document.getElementById('plan').hidden"), "builds the list")
    msgs = text("plan-messages")
    check("not on the bus at the last scan" in msgs, f"warnings: {msgs}")
    if GONE:
        check(msgs.count("not on the bus at the last scan") == 3, f"a warning per absent device: {msgs}")
    check(js("return !document.getElementById('write').disabled"), "can be written")
    shot("plan")

    js("window.confirm = () => true; document.getElementById('write').click()")
    if GONE:
        check(until("document.getElementById('dl-title').textContent.startsWith('Written, with warnings')", 60),
              "written with warnings: " + text("dl-title"))
        check("next master start" in text("dl-steps"), "takes effect at the next master start")
        check(until("document.querySelectorAll('#devices tbody tr').length === 3"), "the project stays listed")
        check(until("document.getElementById('m-state').textContent.startsWith('unavailable - no EtherCAT master instance')"),
              "master shown as unavailable, with the reason: " + text("m-state"))
        shot("written")
        check(until("!document.querySelectorAll('#devices tbody tr .row-tools button')[2].disabled"), "tools enabled when the rescan is over")
        click_tool(0, 2)                                              # Remove the coupler
        check(until("document.querySelectorAll('#devices tbody tr').length === 2"), "removed")
        js("window.confirm = () => true; document.getElementById('project-reset').click()")
        configuration_state(False)
    else:
        check(until("document.getElementById('dl-title').textContent.startsWith('Written')", 60), "written: " + text("dl-title"))
        configuration_state(False)                                    # the master holds it: the configuration state ends
        check(until("document.querySelectorAll('#devices tbody tr').length === 7", 30), "7 devices after the write: " + str(rows()))
    errors = js("return window.__errors || []")
    check(not errors, f"no script errors {errors}")
finally:
    try:
        call("DELETE", S)
    except Exception as e:
        print(e)

print("FAILED: " + "; ".join(failures) if failures else "all good")
sys.exit(1 if failures else 0)
