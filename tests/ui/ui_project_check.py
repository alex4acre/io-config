# SPDX-License-Identifier: MIT

"""End-to-end UI check of the Bus and Configuration tabs, in headless Firefox (see ui_check.py). Three runs, set by an
environment variable for the backend AND the check:
  IO_CONFIG_FAKE_GONE=1     no devices and no master: the list is built from nothing, phasing is disabled
  (none)                    the fake bus: live state, Init and back to OP, the list and the Bus tab side by side
  IO_CONFIG_FAKE_FAIL_OP=1  the master stays below Op: the message after phasing up

    IO_CONFIG_FAKE=1 [IO_CONFIG_FAKE_GONE=1|IO_CONFIG_FAKE_FAIL_OP=1] IO_CONFIG_CONFIG=<new path> IO_CONFIG_ESI=<dir with XB-EC-12, XI190202, XI422202> python3 -m app.main
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
FAIL_OP = bool(os.environ.get("IO_CONFIG_FAKE_FAIL_OP"))
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
    return js("return [...document.querySelectorAll('#cfg-devices tbody tr.listed')].map((r) => [r.cells[1].textContent, r.cells[3].textContent])")


def names():
    return [r[0] for r in rows()]


def bus_names():
    return js("return [...document.querySelectorAll('#devices tbody tr')].map((r) => r.cells[1].textContent)")


def keys(css, text_):
    call("POST", S + f"/element/{find(css)}/value", {"text": text_})


def sugs():
    return js("return [...document.querySelectorAll('#add-suggest .sug')].map((li) => li.textContent)")


def field(id_):
    return js(f"return document.getElementById('{id_}').value")


def tab(name):
    js(f"document.getElementById('tab-{name}').click()")


def pick(search, name=None):
    """Adds a device the way a user does: type, arrow keys, Enter."""
    js("document.getElementById('add-device').value = ''")
    keys("#add-device", search)
    check(until("document.querySelectorAll('#add-suggest .sug').length >= 1 && "
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
    check(until(f"document.querySelectorAll('#cfg-devices tbody tr.listed').length === {n + 1}"), f"{search} added to the list")
    return names()[-1]


def tools(i):
    return js(f"return [...document.querySelectorAll('#cfg-devices tbody tr.listed')[{i}].querySelectorAll('.row-tools button')].map((b) => b.textContent)")


def click_tool(i, n):
    js(f"document.querySelectorAll('#cfg-devices tbody tr.listed')[{i}].querySelectorAll('.row-tools button')[{n}].click()")


def confirms():
    js("window.__confirms = []; window.confirm = (m) => { window.__confirms.push(m); return window.__answer !== false; }")


def choose(value):
    """Picks a state in the master card's select the way a user does (a change event) and returns."""
    js(f"const s = document.getElementById('state-select'); s.value = '{value}'; s.dispatchEvent(new Event('change'))")


def master_text():
    return js("return document.getElementById('m-live').textContent")


try:
    call("POST", S + "/url", {"url": URL})
    check(until("document.getElementById('conn').textContent.includes('connected')"), "page loaded")
    confirms()
    check(js("return !document.getElementById('tab-bus').hidden && !document.getElementById('tab-config').hidden "
             "&& !document.getElementById('tab-esi').hidden"), "tabs Bus, Configuration, ESI library")
    check(until("document.getElementById('m-live').textContent !== ''"), "the master's state shows before any scan")

    if FAIL_OP:
        check(until("document.getElementById('m-live').textContent === 'OP'"), "state OP without a scan: " + master_text())
        choose("init")
        js("document.getElementById('state-set').click()")
        check(until("document.getElementById('m-live').textContent === 'Init'", 15), "set to Init: " + master_text())
        choose("op")
        js("document.getElementById('state-set').click()")
        check(until("!document.getElementById('m-request').hidden", 20), "a message when OP is not reached")
        msg = text("m-request")
        check("did not reach OP" in msg and "Pre-OP" in msg, f"the message names both states: {msg}")
        check(until("document.getElementById('m-live').textContent.startsWith('Pre-OP')", 10), "badge shows Pre-OP: " + master_text())
        check("(requested OP)" in master_text(), "with the requested state: " + master_text())
        check(js("return document.getElementById('m-live').firstChild.classList.contains('warn')"), "Pre-OP is a warn badge")
        time.sleep(3)
        check(not js("return document.getElementById('m-request').hidden"), "the message stays visible")
        shot("phase-failed")
        print("FAILED: " + "; ".join(failures) if failures else "all good")
        sys.exit(1 if failures else 0)

    if GONE:
        check(js("return !document.getElementById('noscan').hidden"), "not scanned yet")
        check(until("document.getElementById('m-live').textContent === 'unavailable'"), "state unavailable: " + master_text())
        check(js("return document.getElementById('m-live').firstChild.classList.contains('bad')"), "unavailable is a bad badge")
        check(js("return document.getElementById('state-set').disabled"), "Set state is off when the master can't be read")
        check("can't be read" in js("return document.getElementById('state-set').title"), "with the reason as title")
        tab("config")
        check(js("return !document.getElementById('cfg-note').hidden && document.getElementById('cfg-note').textContent.includes('no list yet')"),
              "note: no list yet")
    else:
        check(until("document.getElementById('m-live').textContent === 'OP'"), "state OP without a scan: " + master_text())
        check(js("return document.getElementById('m-live').firstChild.classList.contains('ok')"), "OP is an ok badge")
        check(js("return !document.getElementById('noscan').hidden"), "(not scanned yet)")
        js("document.getElementById('scan').click()")
        check(until("document.querySelectorAll('#devices tbody tr').length === 4 && !document.getElementById('scan').disabled", 30),
              "scan finds the 4 devices")
        check(js("return document.querySelectorAll('#devices tbody tr button').length") == 0, "the Bus tab has no list tools")
        check(js("return [...document.querySelectorAll('#devices tbody tr')].every((r) => r.cells[7].textContent === 'same')"),
              "all devices are in the active configuration")
        # Init and back to OP, with confirmation
        js("window.__answer = false")
        choose("init")
        js("document.getElementById('state-set').click()")
        check(js("return window.__confirms.length") == 1 and "Requests Init on the real bus (now OP). Outputs stop when leaving OP." in js("return window.__confirms[0]"),
              "Init asks first: " + str(js("return window.__confirms")))
        time.sleep(1)
        check(master_text() == "OP", "declining changes nothing")
        js("window.__answer = true")
        js("document.getElementById('state-set').click()")
        check(until("document.getElementById('m-live').textContent === 'Init'", 15), "set to Init: " + master_text())
        check(js("return document.getElementById('m-live').firstChild.classList.contains('warn')"), "Init is a warn badge")
        check(until("!document.getElementById('scan').disabled && document.querySelectorAll('#devices tbody tr').length === 4"),
              "the scan after it finished")
        check(js("return document.getElementById('state-select').value") == "init", "the select follows the state after a request")
        choose("op")
        js("document.getElementById('state-set').click()")
        check("Devices start exchanging process data" in js("return window.__confirms[window.__confirms.length - 1]"),
              "OP asks first")
        check(until("document.getElementById('m-live').textContent === 'OP'", 15), "set to OP: " + master_text())
        check(js("return document.getElementById('m-request').hidden"), "no message when it worked")
        check(until("!document.getElementById('scan').disabled"), "scan done")
        tab("config")
        check(until("document.querySelectorAll('#cfg-devices tbody tr.listed').length === 4"), "the list has the 4 devices of the bus")
        check(js("return document.getElementById('cfg-note').hidden"), "no note while the list equals the bus")
        # the trash button on a device on the bus: not configured, Scan from this tab keeps it so, Add to list brings it back
        click_tool(1, 2)
        unconfigured = "document.querySelector('#cfg-devices tbody tr:not(.listed)')"
        check(until(f"!!{unconfigured}"), "the removed device is still shown")
        row1 = js(f"const r = {unconfigured}; return [r.cells[0].textContent, r.cells[1].textContent, r.cells[4].textContent, r.cells[5].textContent]")
        check(row1 == ["-", "XI190202", "No", "Yes"], f"tagged present, not configured: {row1}")
        check("On the bus but not configured: 1." in text("cfg-note"), "note counts it: " + text("cfg-note"))
        check(js("return document.querySelectorAll('#cfg-devices tbody tr')[1] === " + unconfigured), "shown at its wiring place")
        last_scan = text("m-time")
        time.sleep(1.2)
        js("document.getElementById('cfg-scan').click()")
        check(until(f"document.getElementById('m-time').textContent !== {last_scan!r}", 30), "Scan bus on the Configuration tab scans")
        check(until(f"!!{unconfigured}"), "the scan doesn't change the list")
        js(f"{unconfigured}.querySelector('button').click()")
        check(until(f"!{unconfigured}"), "Add to list")
        check(names()[1].startswith("XI190202"), f"back at its place: {names()}")
        check(js("return document.querySelectorAll('#cfg-devices thead th').length") == 7, "list columns: Pos, Name, Device, Revision, Configured, Present, tools")

    # --- the add form: message above the button
    js("document.getElementById('add-box').open = true")
    check(until("document.querySelectorAll('#add-suggest .sug').length >= 3"), "the list opens on focus with an empty query")
    shot("suggestions")
    order = js("return [...document.getElementById('add-box').querySelectorAll('.row, #add-msg')].map((e) => e.id || e.className)")
    check(order[-2:] == ["add-msg", "row"] or "add-msg" in order, f"layout {order}")
    keys("#add-device", "XB-EC")
    check(until("document.querySelectorAll('#add-suggest .sug').length >= 1"), "suggestions while typing")
    check(js("return document.getElementById('add-msg').textContent") == "Pick a device from the list", "message: pick a device")
    check(js("return !document.getElementById('add-msg').hidden && document.getElementById('add').disabled"), "message shown, Add disabled")
    time.sleep(0.8)
    keys("#add-device", "\ue015")
    typed = field("add-device")
    keys("#add-device", "\ue00c")                                    # Escape
    check(field("add-device") == "XB-EC" and js("return document.getElementById('add-suggest').hidden"), f"Escape restores the typed text (was {typed})")
    keys("#add-device", "\ue015")                                    # opens the list again
    check(until("document.querySelectorAll('#add-suggest .sug').length >= 1"), "ArrowDown opens the list")
    keys("#add-device", "\ue015\ue007")
    check(not js("return document.getElementById('add').disabled") and js("return document.getElementById('add-msg').hidden"),
          "picked: message gone, Add enabled")
    keys("#add-device", "x")
    check(js("return document.getElementById('add').disabled") and js("return !document.getElementById('add-msg').hidden"),
          "editing the text clears the pick (message again)")
    js("document.getElementById('add-device').value = ''; document.getElementById('add-name').value = ''")
    js("document.getElementById('add-device').oninput()")
    time.sleep(0.5)

    first = len(rows())
    pick("XB-EC-12", "Coupler")
    check(names()[-1] == "Coupler", f"name given at add shows in the list: {names()}")
    check(field("add-device") == "" and field("add-name") == "", "the form is cleared after an add")
    check(js("return document.getElementById('add-msg').hidden"), "the message is cleared after an add")
    pick("XI190202")
    pick("XI422202")
    check(len(rows()) == first + 3, f"3 devices added {rows()}")
    dup = "" if GONE else "_2"                                         # on the bus the same types exist already: unique defaults
    check(names()[-3:] == ["Coupler", "XI190202" + dup, "XI422202" + dup], f"new devices at the bottom: {names()}")
    if not GONE:
        check(bus_names() == ["XB_EC_12", "XI190202", "XI422202", "XI522001"], "the Bus tab did not change")
        check(js("return !document.getElementById('cfg-note').hidden"), "note: devices that are not on the bus")
        check("Configured but not on the bus: 3." in text("cfg-note"), "note text: " + text("cfg-note"))
        tags = js("return [...document.querySelectorAll('#cfg-devices tbody tr.listed')].map((r) => r.cells[4].textContent + '/' + r.cells[5].textContent)")
        check(tags[:4] == ["Yes/Yes"] * 4 and tags[-3:] == ["Yes/No"] * 3, f"Configured/Present tags: {tags}")

    # a name another row has (any case): live message above the button
    keys("#add-name", names()[0].upper())
    check(until("document.getElementById('add-msg').textContent.includes('already used by position 0')"),
          "duplicate name in the add form: " + text("add-msg"))
    js("document.getElementById('add-name').value = ''; document.getElementById('add-name').oninput()")

    # bad names: live message, and the server's refusal above the button
    n = len(rows())
    keys("#add-device", "XI190202")
    check(until("document.querySelectorAll('#add-suggest .sug').length >= 1 && "
                "document.querySelector('#add-suggest .sug').textContent.includes('XI190202')"), "suggestions for the bad-name add")
    time.sleep(0.8)
    keys("#add-device", "\ue015\ue007")
    keys("#add-name", "1bad")
    check(js("return document.getElementById('add-msg').textContent").startswith("The name must start with a letter"),
          "live message for a bad name: " + text("add-msg"))
    check(js("return document.getElementById('add').disabled"), "Add disabled with a bad name")
    js("document.getElementById('add-name').value = '1bad'; document.getElementById('add').disabled = false; document.getElementById('add').click()")
    check(until("document.getElementById('add-msg').textContent.includes('name must start')"), "server refusal above the button: " + text("add-msg"))
    check(js("return document.getElementById('banner').hidden"), "not in the top banner")
    js("document.getElementById('add-msg').scrollIntoView({block: 'center'})")
    shot("add-message")
    check(len(rows()) == n and field("add-name") == "1bad", "nothing added, the form keeps what was typed")
    js("document.getElementById('add-name').value = ''; document.getElementById('add-device').value = ''; "
       "document.getElementById('add-device').oninput()")
    shot("project")

    last = len(rows()) - 1
    check(tools(last) == ["\u25b2", "\u25bc", ""] and tools(0) == ["\u25b2", "\u25bc", ""], "arrows and a trash button on every row")
    trash = js(f"return document.querySelectorAll('#cfg-devices tbody tr.listed')[0].querySelectorAll('.row-tools button')[2]")
    check(js("const b = document.querySelectorAll('#cfg-devices tbody tr.listed')[0].querySelectorAll('.row-tools button')[2]; "
             "return b.getAttribute('aria-label') === 'Remove' && b.title === 'Remove' && !!b.querySelector('svg') && b.textContent.trim() === ''"),
          "trash button: svg icon with title and aria-label Remove")
    check(js("return document.querySelectorAll('#cfg-devices tbody tr.listed')[0].querySelector('.row-tools button').title") == "Move up in the list",
          "title of the up arrow")
    check(js("return document.querySelectorAll('#cfg-devices tbody tr.listed')[0].querySelector('.row-tools button').disabled") and
          js(f"return document.querySelectorAll('#cfg-devices tbody tr.listed')[{last}].querySelectorAll('.row-tools button')[1].disabled"),
          "no up arrow at the top, no down arrow at the bottom")
    before = names()
    click_tool(last, 0)
    check(until(f"document.querySelectorAll('#cfg-devices tbody tr.listed')[{last - 1}].cells[1].textContent === '{before[last]}'"), "moved up")
    click_tool(last - 1, 1)
    check(until(f"document.querySelectorAll('#cfg-devices tbody tr.listed')[{last}].cells[1].textContent === '{before[last]}'"), "moved down")
    if not GONE:                                                      # an on-bus row moves too
        click_tool(1, 1)
        check(until("document.querySelectorAll('#cfg-devices tbody tr.listed')[2].cells[1].textContent === 'XI190202'"), "an on-bus device moved down")
        click_tool(2, 0)
        check(until("document.querySelectorAll('#cfg-devices tbody tr.listed')[1].cells[1].textContent === 'XI190202'"), "and back up")
        check(bus_names() == ["XB_EC_12", "XI190202", "XI422202", "XI522001"], "the Bus tab never changed")

    # click-to-edit name
    row_i = last
    js(f"document.querySelectorAll('#cfg-devices tbody tr.listed')[{row_i}].cells[1].click()")
    check(until(f"!!document.querySelector('#cfg-devices tbody tr:nth-child({row_i + 1}) td input')"), "click on a name opens an input")
    check(js("return document.getElementById('cdetail').hidden"), "the device card did not open")
    time.sleep(2.5)                                                   # longer than the 1 s poll
    check(js(f"return !!document.querySelector('#cfg-devices tbody tr:nth-child({row_i + 1}) td input')"), "the poll keeps the input")
    shot("rename")
    js(f"document.querySelector('#cfg-devices tbody tr:nth-child({row_i + 1}) td input').value = ''")
    keys(f"#cfg-devices tbody tr:nth-child({row_i + 1}) td input", names()[0].lower() + "\ue007")      # another row's name, in lower case
    check(until(f"document.querySelector('#cfg-devices tbody tr:nth-child({row_i + 1}) .rename-msg').textContent.includes('already used by position 0')"),
          "a duplicate name is refused next to the input: " + js(f"return document.querySelector('#cfg-devices tbody tr:nth-child({row_i + 1}) .rename-msg').textContent"))
    check(js(f"return !!document.querySelector('#cfg-devices tbody tr:nth-child({row_i + 1}) td input')"), "the input stays open")
    check(js("return document.getElementById('banner').hidden"), "no banner for it")
    shot("rename-duplicate")
    js(f"document.querySelector('#cfg-devices tbody tr:nth-child({row_i + 1}) td input').value = ''")
    keys(f"#cfg-devices tbody tr:nth-child({row_i + 1}) td input", "Zzz\ue00c")           # type, Escape
    check(until(f"document.querySelectorAll('#cfg-devices tbody tr.listed')[{row_i}].cells[1].textContent === '{before[last]}'"), "Escape cancels")
    js(f"document.querySelectorAll('#cfg-devices tbody tr.listed')[{row_i}].cells[1].click()")
    check(until(f"!!document.querySelector('#cfg-devices tbody tr:nth-child({row_i + 1}) td input')"), "input again")
    js(f"document.querySelector('#cfg-devices tbody tr:nth-child({row_i + 1}) td input').value = ''")
    keys(f"#cfg-devices tbody tr:nth-child({row_i + 1}) td input", "Analog_out\ue007")
    check(until(f"document.querySelectorAll('#cfg-devices tbody tr.listed')[{row_i}].cells[1].textContent === 'Analog_out'", 10), "Enter saves the name")
    check(js("return document.getElementById('cdetail').hidden"), "still no device card")
    if not GONE:
        check(bus_names()[-1] == "XI522001" and "Analog_out" not in bus_names(), "the Bus tab does not show the new name")
    js(f"document.querySelectorAll('#cfg-devices tbody tr.listed')[{row_i}].cells[2].click()")
    check(until("!document.getElementById('cdetail').hidden && !document.getElementById('p-settings').hidden"),
          "a device opens on Settings")
    check(until("document.getElementById('c-title').textContent.includes('Analog_out')"), "card title shows the saved name")
    tabs = js("return [...document.querySelectorAll('#cdetail .subtabs .tab')].filter((t) => !t.hidden).map((t) => t.dataset.panel)")
    check(tabs == ["settings", "startup"], f"tabs of a CoE device on the Configuration card {tabs}")
    check(until("document.getElementById('e-name').value === 'Analog_out'"), "its name in Settings")
    shot("device-in-configuration")
    js("document.getElementById('c-close').click()")

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
        check(until("document.querySelectorAll('#cfg-devices tbody tr.listed').length === 3"), "the list stays")
        tab("bus")
        check(until("document.getElementById('m-live').textContent === 'unavailable'"), "master shown as unavailable: " + master_text())
        check("DL_INVALID_ADDRESS" in text("m-reason"), "with the reason: " + text("m-reason"))
        shot("written")
        tab("config")
        check(until("!document.querySelectorAll('#cfg-devices tbody tr.listed .row-tools button')[2].disabled"), "tools enabled when the rescan is over")
        click_tool(0, 2)                                              # remove the coupler with the trash button
        check(until("document.querySelectorAll('#cfg-devices tbody tr.listed').length === 2"), "removed")
    else:
        check(until("document.getElementById('dl-title').textContent.startsWith('Written')", 60), "written: " + text("dl-title"))
        check(until("!document.getElementById('scan').disabled"), "scan after the write")
        check(until("document.querySelectorAll('#cfg-devices tbody tr.listed').length === 7", 30), "the list stays after the write: " + str(rows()))
        check(until("document.querySelectorAll('#devices tbody tr').length === 7", 30), "the Bus tab shows what the master runs now")
        # Update from the active bus: confirm, replaces the list
        confirms()
        js("document.getElementById('project-update').click()")
        check(js("return window.__confirms.length") == 1 and "Replaces the list and all unwritten changes with the master's running configuration" in
              js("return window.__confirms[0]"), "Update asks first: " + str(js("return window.__confirms")))
        check(until("document.querySelectorAll('#cfg-devices tbody tr.listed').length === 7 && document.getElementById('cfg-note').textContent === 'Configured but not on the bus: 3.'"),
              "after Update the list is the running configuration, missing devices counted: " + text("cfg-note"))
    errors = js("return window.__errors || []")
    check(not errors, f"no script errors {errors}")
finally:
    try:
        call("DELETE", S)
    except Exception as e:
        print(e)

print("FAILED: " + "; ".join(failures) if failures else "all good")
sys.exit(1 if failures else 0)
