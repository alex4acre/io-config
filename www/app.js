// SPDX-License-Identifier: MIT
// I/O Config UI. The ctrlX sidebar opens /io-config/?token=<bearer token>; every API call sends it back.

"use strict";

const API = "api/v1";
const TOKEN = (() => {
  const t = new URLSearchParams(location.search).get("token");
  try {
    if (t) sessionStorage.setItem("token", t);
    return t || sessionStorage.getItem("token") || "";
  } catch (e) {
    return t || "";
  }
})();

const $ = (id) => document.getElementById(id);

async function api(method, path, body, raw) {
  const opts = { method, headers: { Authorization: "Bearer " + TOKEN } };
  if (raw !== undefined) {
    opts.headers["Content-Type"] = "application/octet-stream";
    opts.body = raw;
  } else if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(API + path, opts);
  const text = await r.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch (e) { data = { error: text }; }
  if (!r.ok) throw new Error((data && data.error) || r.status + " " + r.statusText);
  return data;
}

function banner(text, ok) {
  const b = $("banner");
  b.textContent = text || "";
  b.className = "banner" + (ok ? " ok" : "");
  b.hidden = !text;
}

// --- small helpers ------------------------------------------------------------------------------

const hex = (n, w = 4) => (typeof n === "number" ? "#x" + n.toString(16).toUpperCase().padStart(w, "0") : "-");

function el(tag, attrs, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") e.className = v;
    else if (k === "title") e.title = v;
    else e.setAttribute(k, v);
  }
  for (const c of children) {
    if (c === null || c === undefined) continue;
    e.append(c instanceof Node ? c : String(c));
  }
  return e;
}

function row(tbody, cells, attrs) {
  const tr = el("tr", attrs);
  for (const c of cells) tr.append(c instanceof Node && c.tagName === "TD" ? c : el("td", {}, c));
  tbody.append(tr);
  return tr;
}

const SOURCE_TEXT = {
  esi: { exact: ["exact", "ok"], nearest: ["other rev.", "warn"], none: ["missing", "bad"], error: ["error", "bad"] },
  eni: { same: ["same", "ok"], different: ["different", "bad"], none: ["not configured", "warn"], "not found": ["not on bus", "bad"] },
  eeprom: { read: ["read", "ok"], error: ["error", "warn"], "not read": ["not read", "muted"] },
};

function badge(source, match) {
  const [text, cls] = (SOURCE_TEXT[source] || {})[match] || [match || "-", "muted"];
  return el("span", { class: "badge " + cls }, text);
}

const LEVEL_TEXT = { ok: "OK", info: "OK", warning: "Check", error: "Error" };

function levelBadge(level, count) {
  const cls = { ok: "ok", info: "ok", warning: "warn", error: "bad" }[level] || "muted";
  return el("span", { class: "badge " + cls }, LEVEL_TEXT[level] + (count ? ` (${count})` : ""));
}

// --- views --------------------------------------------------------------------------------------

function show(view) {
  banner("");
  for (const t of document.querySelectorAll(".tab")) t.classList.toggle("active", t.dataset.view === view);
  $("view-bus").hidden = view !== "bus";
  $("view-esi").hidden = view !== "esi";
  $("view-pd").hidden = view !== "pd";
  try { sessionStorage.setItem("view", view); } catch (e) { /* not needed */ }
  if (view === "esi") loadEsi();
  if (view === "pd") loadPd();
}

for (const t of document.querySelectorAll(".tab")) t.onclick = () => show(t.dataset.view);

// --- bus ----------------------------------------------------------------------------------------

let lastFinished = null;
let selected = null;

function renderState(s) {
  $("conn").textContent = s.connected ? "Data Layer connected" : "Data Layer offline";
  $("conn").className = "chip " + (s.connected ? "ok" : "bad");
  $("instance").textContent = s.instance;
  $("scan").disabled = s.scan.running || s.download.running;
  $("progress").textContent = s.scan.running ? "Scanning: " + s.scan.progress : "";
  if (s.scan.error && !s.scan.running && s.scan.finished !== lastFinished) banner("Scan failed: " + s.scan.error);

  const m = s.master;
  $("noscan").hidden = !!m;
  $("master").hidden = !m;
  $("bus-card").hidden = !m;
  if (m) {
    $("m-state").textContent = m.state === m.requested ? m.state : `${m.state} (requested ${m.requested})`;
    $("m-devices").textContent = `${m.connected} connected, ${m.configured} configured`;
    $("m-topology").textContent = m.topology;
    $("m-cycle").textContent = m.cycle_us ? m.cycle_us + " µs" : "-";
    $("m-config").textContent = m.eni
      ? `ENI with ${m.eni.slaves} devices, ${m.eni.inputs_bytes} B inputs / ${m.eni.outputs_bytes} B outputs`
      : (m.eni_error ? "ENI not readable: " + m.eni_error : "no ENI loaded");
    $("m-time").textContent = s.scan.finished ? new Date(s.scan.finished * 1000).toLocaleString() : "-";
  }

  const tbody = $("devices").tBodies[0];
  tbody.replaceChildren();
  for (const d of s.devices) {
    const tr = row(tbody, [
      d.position, d.name, d.description, d.address ?? "-", hex(d.revision, 8),
      d.present ? d.state : el("span", { class: "badge bad" }, "missing"),
      badge("esi", d.sources.esi), badge("eni", d.sources.eni), badge("eeprom", d.sources.eeprom),
      levelBadge(d.level, d.findings),
    ], { class: "click" + (d.position === selected ? " selected" : "") });
    tr.onclick = () => openDevice(d.position);
  }
  if (s.scan.finished !== lastFinished) {
    lastFinished = s.scan.finished;
    if (selected !== null) openDevice(selected);
    if (plan) {                     // a new scan makes a built configuration stale
      plan = null;
      $("plan").hidden = true;
    }
  }
  renderConfig(s);
}

async function poll() {
  try {
    renderState(await api("GET", "/state"));
  } catch (e) {
    $("conn").textContent = "App not reachable";
    $("conn").className = "chip bad";
  }
}

$("scan").onclick = async () => {
  try {
    banner("");
    await api("POST", "/scan", { reread_eeprom: $("reread").checked });
    $("scan").disabled = true;
    poll();
  } catch (e) {
    banner(e.message);
  }
};

function sourceCell(s, fmt) {
  return s ? fmt(s) : el("span", { class: "muted" }, "-");
}

async function openDevice(pos) {
  let d;
  try {
    d = await api("GET", "/devices/" + pos);
  } catch (e) {
    $("detail").hidden = true;
    selected = null;
    return;
  }
  selected = pos;
  const protocols = d.sources.eeprom.mailbox || [];
  $("d-objects").hidden = protocols.length > 0 && !protocols.includes("CoE");
  $("d-soe").hidden = !protocols.includes("SoE");
  for (const tr of $("devices").tBodies[0].rows) tr.classList.toggle("selected", Number(tr.cells[0].textContent) === pos);
  $("detail").hidden = false;
  $("d-title").textContent = `${d.position}: ${d.name}`;
  $("d-ident").textContent = `${d.description} - vendor ${hex(d.vendor, 1)}, product ${hex(d.product, 8)}, ` +
    `revision ${hex(d.revision, 8)}` + (d.serial ? `, serial ${d.serial}` : "") +
    (d.sources.esi.file ? ` - ESI ${d.sources.esi.file}` : "");

  const fl = $("d-findings");
  fl.replaceChildren();
  if (!d.findings.length) fl.append(el("li", { class: "ok" }, "ESI, configuration and EEPROM agree."));
  for (const f of d.findings) fl.append(el("li", { class: f.level }, f.text));

  const sm = $("d-sm").tBodies[0];
  sm.replaceChildren();
  const smText = (s) => `SM${s.no} ${hex(s.start)}, ${s.size} B`;
  for (const r of d.sm) row(sm, [r.type, sourceCell(r.esi, smText), sourceCell(r.eni, smText), sourceCell(r.eeprom, smText)]);

  const pd = $("d-pdos").tBodies[0];
  pd.replaceChildren();
  for (const p of d.pdos) {
    const esi = sourceCell(p.esi, (s) => [s.sm === null ? "optional" : "SM" + s.sm, s.fixed ? "fixed" : "",
      s.mandatory ? "mandatory" : "", s.excludes.length ? "excl. " + s.excludes.map((x) => hex(x)).join(" ") : ""]
      .filter(Boolean).join(", "));
    const eni = p.eni ? (p.eni.assigned ? el("span", { class: "badge ok" }, "assigned") : "listed")
      : el("span", { class: "muted" }, "not assigned");
    const ee = sourceCell(p.eeprom, (s) => (s.sm === null ? "optional" : "SM" + s.sm));
    const entries = el("details", {}, el("summary", {}, `${p.entries.length} entries` +
      (p.differs.length ? " - differ in " + p.differs.join(", ").toUpperCase() : "")),
      el("ul", { class: "entries" }, ...p.entries.map((e) => el("li", {},
        e.index ? `${hex(e.index)}:${e.subindex} ${e.name} (${e.bits} bit${e.type ? ", " + e.type : ""})` : `padding ${e.bits} bit`))));
    row(pd, [p.dir === "rx" ? "out" : "in", hex(p.index), p.name, esi, eni, ee, entries],
      { class: p.differs.length ? "warnrow" : "" });
  }

  const vars = d.sources.eni.variables;
  $("d-dlpath").textContent = "fieldbuses/ethercat/master/instances/<instance>/realtime_data/<input|output>/data/<variable>";
  const vb = $("d-vars").tBodies[0];
  vb.replaceChildren();
  if (!vars.length) row(vb, [el("span", { class: "muted" }, "none (not in the configuration)"), "", "", "", ""]);
  for (const v of vars) row(vb, [v.name, v.dir, v.type, v.bits, `${Math.floor(v.offset / 8)}.${v.offset % 8}`]);

  const ib = $("d-init").tBodies[0];
  ib.replaceChildren();
  const inits = [...d.sources.eni.coe_init.map((c) => ["ENI", c]), ...d.sources.esi.coe_init.map((c) => ["ESI", c])];
  if (!inits.length) row(ib, [el("span", { class: "muted" }, "none"), "", "", "", ""]);
  for (const [src, c] of inits) {
    row(ib, [src, c.transitions.join(" "), `${hex(c.index)}:${c.subindex}${c.complete_access ? " (CA)" : ""}`,
      el("code", {}, c.data), c.comment]);
  }

  const dc = $("d-dc");
  dc.replaceChildren();
  const add = (k, v) => dc.append(el("dt", {}, k), el("dd", {}, v));
  add("On the device", d.dc === null ? "-" : (d.dc ? "supported" : "not supported"));
  const eniDc = d.sources.eni.dc;
  add("Configured (ENI)", eniDc ? Object.entries(eniDc).map(([k, v]) => `${k} ${v}`).join(", ") : "not used");
  const modes = d.sources.esi.dc_modes;
  add("ESI modes", modes.length ? modes.slice(0, 6).map((m) => `${m.name} (${m.desc}, ${hex(m.assign_activate)})`).join("; ") +
    (modes.length > 6 ? `; ... ${modes.length} in all` : "") : "-");
}

// --- configuration: build, write, backups -------------------------------------------------------

let plan = null;
let downloadFinished = null;

function renderConfig(s) {
  $("config-card").hidden = !s.master;
  const busy = s.scan.running || s.download.running;
  $("build").disabled = busy;
  $("write").disabled = busy || !plan || plan.errors.length > 0;
  if (s.master && !$("cycle").value && s.master.cycle_us) $("cycle").value = s.master.cycle_us;

  const d = s.download;
  $("dl").hidden = !d.running && !d.finished;
  if (!$("dl").hidden) {
    $("dl-title").textContent = d.running ? "Writing to the EtherCAT master ..." :
      (d.error ? "Writing failed" : "Written");
    const ul = $("dl-steps");
    ul.replaceChildren(...d.steps.map((t) => el("li", { class: "ok" }, t)));
    if (d.error) ul.append(el("li", { class: "error" }, d.error));
  }
  if (d.finished && d.finished !== downloadFinished) {
    downloadFinished = d.finished;
    plan = null;
    $("plan").hidden = true;
    loadBackups();
  }
}

async function getFile(path, name) {
  try {
    const r = await fetch(API + path, { headers: { Authorization: "Bearer " + TOKEN } });
    if (!r.ok) throw new Error((await r.json()).error || r.statusText);
    const url = URL.createObjectURL(await r.blob());
    const a = el("a", { href: url, download: name });
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
  } catch (e) {
    banner(e.message);
  }
}

$("build").onclick = async () => {
  try {
    banner("");
    const cycle = Number($("cycle").value);
    plan = await api("POST", "/plan", cycle ? { cycle_us: cycle } : {});
  } catch (e) {
    plan = null;
    $("plan").hidden = true;
    banner(e.message);
    return;
  }
  $("plan").hidden = false;
  $("dl").hidden = true;
  const v = plan.variables;
  const nChanges = v.added.length + v.removed.length + v.changed.length;
  $("plan-summary").textContent = plan.errors.length ? "The configuration can't be built:" :
    plan.identical ? "Identical to the configuration the master runs now." :
    `Differs from the current configuration (${plan.eni_size} bytes ENI, cycle time ${plan.cycle_us} µs).`;
  $("plan-summary").className = plan.errors.length ? "error" : (plan.identical ? "ok" : "");
  const msgs = $("plan-messages");
  msgs.replaceChildren(...plan.errors.map((t) => el("li", { class: "error" }, t)),
    ...plan.warnings.map((t) => el("li", { class: "warning" }, t)),
    ...plan.removed_devices.map((n) => el("li", { class: "warning" }, `${n} is configured now but not on the bus: it will be removed`)));
  const tb = $("plan-devices").tBodies[0];
  tb.replaceChildren();
  for (const d of plan.devices) row(tb, [d.position, d.name, d.note || d.source || "-", d.inputs + " B", d.outputs + " B"]);
  $("plan-vars").querySelector("summary").textContent = nChanges
    ? `Process image: ${v.added.length} variables new, ${v.removed.length} removed, ${v.changed.length} changed (type or offset)`
    : "Process image: no variable changes";
  const body = $("plan-vars-body");
  body.replaceChildren();
  for (const [label, list] of [["New", v.added], ["Removed", v.removed], ["Changed", v.changed]]) {
    if (list.length) body.append(el("h3", {}, label), el("ul", { class: "files" }, ...list.map((n) => el("li", {}, n))));
  }
  $("write").disabled = plan.errors.length > 0;
};

$("get-eni").onclick = () => getFile("/plan/eni", "ethercat.xml");
$("get-ini").onclick = () => getFile("/plan/ini", "ethercat.ini");

const WRITE_WARNING = "The EtherCAT master goes to Init while the configuration is written: the bus stops, outputs " +
  "drop and the PLC and MOTION lose their I/O until the master is back in Op.\n\n" +
  "The current configuration is saved as a backup first.";

$("write").onclick = async () => {
  if (!plan) return;
  const what = plan.identical ? "The new configuration is identical to the current one.\n\n" : "";
  if (!confirm(what + WRITE_WARNING + "\n\nWrite the configuration now?")) return;
  const draft = plan.draft && plan.draft.length > 0;
  if (draft && !confirm(`Position ${plan.draft.join(", ")}: the drive part of this configuration is a DRAFT that was ` +
    "not checked against a configuration from ctrlX I/O Engineering. The drive may not reach Op.\n\nWrite it anyway?")) return;
  try {
    banner("");
    await api("POST", "/download", { plan_id: plan.id, confirm: true, allow_draft: draft });
    $("write").disabled = true;
    poll();
  } catch (e) {
    banner(e.message);
  }
};

async function loadBackups() {
  try {
    const r = await api("GET", "/backups");
    const tb = $("backups").tBodies[0];
    tb.replaceChildren();
    if (!r.backups.length) row(tb, [el("span", { class: "muted" }, "none yet"), "", "", ""]);
    for (const b of r.backups) {
      const get = el("button", { class: "small" }, "ENI");
      get.onclick = () => getFile(`/backups/${b.name}/eni`, `${b.name}-ethercat.xml`);
      const restore = el("button", { class: "small" }, "Restore");
      restore.onclick = async () => {
        if (!confirm(`Write the configuration saved ${new Date(b.time * 1000).toLocaleString()} back to the master?\n\n` +
          WRITE_WARNING)) return;
        try {
          await api("POST", `/backups/${b.name}/restore`, { confirm: true });
          poll();
        } catch (e) {
          banner(e.message);
        }
      };
      row(tb, [new Date(b.time * 1000).toLocaleString(), b.before, b.devices.join(", "), el("span", {}, get, " ", restore)]);
    }
  } catch (e) {
    banner(e.message);
  }
}

$("backups-box").ontoggle = () => { if ($("backups-box").open) loadBackups(); };

// --- writes switch ----------------------------------------------------------------------------------

const writesAllowed = () => $("allow-writes").checked;
$("allow-writes").onchange = () => {
  document.body.classList.toggle("writes", writesAllowed());
  if ($("view-pd").hidden === false) loadPd();
  if (currentObject) openObject(currentObject);
};

// --- editor: name, PDOs, startup parameters -------------------------------------------------------

let editPos = null;
let editData = null;

function pdoBytes(sel) {
  let tx = 0;
  let rx = 0;
  for (const p of editData.pdos) {
    if (!sel.includes(p.index)) continue;
    if (p.dir === "tx") tx += p.bits; else rx += p.bits;
  }
  return [tx / 8, rx / 8];
}

function selectedPdos() {
  return [...$("e-pdos").querySelectorAll("input[type=checkbox]")].filter((c) => c.checked).map((c) => Number(c.dataset.index));
}

function renderEditor() {
  const d = editData;
  $("e-title").textContent = `Edit position ${d.position}`;
  $("e-name").value = d.name;
  $("e-reason").textContent = d.editable ? "" : "PDOs can't be changed: " + d.reason;
  const tb = $("e-pdos").tBodies[0];
  tb.replaceChildren();
  for (const p of d.pdos) {
    const box = el("input", { type: "checkbox" });
    box.dataset.index = p.index;
    box.checked = d.selected.includes(p.index);
    box.disabled = !d.editable || p.mandatory;
    box.onchange = updateEditorRules;
    const rules = [p.mandatory ? "mandatory" : "", p.fixed ? "fixed content" : "",
      p.excludes.length ? "excludes " + p.excludes.map((x) => hex(x)).join(" ") : ""].filter(Boolean).join(", ");
    const tr = row(tb, [el("td", {}, box), p.dir === "rx" ? "out" : "in", hex(p.index), p.name, p.bits / 8 + " B", rules],
      { class: d.default.includes(p.index) ? "default" : "" });
    tr.title = p.entries.map((e) => (e.index ? `${hex(e.index)}:${e.subindex} ${e.name} (${e.bits} bit)` : `padding ${e.bits} bit`)).join("\n");
  }
  updateEditorRules();
  const sb = $("e-startup").tBodies[0];
  sb.replaceChildren();
  for (const c of d.fixed_startup) {
    row(sb, [`${hex(c.index)}:${c.subindex}`, el("code", {}, c.data), c.transitions.join(" "), c.comment,
      el("span", { class: "muted" }, "from ESI/configuration")]);
  }
  for (const [i, c] of d.startup.entries()) {
    const del = el("button", { class: "small" }, "Remove");
    del.onclick = () => saveEdit({ startup: d.startup.filter((_, j) => j !== i) });
    row(sb, [c.idn !== undefined ? idnText(c.idn) : `${hex(c.index)}:${c.subindex}`, el("code", {}, c.data),
      c.transitions.join(" "), c.comment, del]);
  }
  renderSoeEditor();
  if (!d.fixed_startup.length && !d.startup.length) row(sb, [el("span", { class: "muted" }, "none"), "", "", "", ""]);
  $("e-msg").textContent = d.edited ? "This position has saved changes." : "Defaults (no saved changes).";
}

function idnText(n) {
  return `${n & 0x8000 ? "P" : "S"}-${(n >> 12) & 7}-${String(n & 0xfff).padStart(4, "0")}`;
}

let soeLists = null;

function renderSoeEditor() {
  const s = editData.soe;
  $("e-soe").hidden = !s;
  if (!s) return;
  if (!soeLists) soeLists = { mdt: s.mdt.map((x) => x.idn), at: s.at.map((x) => x.idn), names: {} };
  for (const x of [...s.mdt, ...s.at]) soeLists.names[x.idn] = `${x.name} (${x.bits} bit)`;
  for (const key of ["mdt", "at"]) {
    const tb = $("e-" + key).tBodies[0];
    tb.replaceChildren();
    soeLists[key].forEach((idn, i) => {
      const del = el("button", { class: "small" }, "Remove");
      del.disabled = i === 0;
      del.onclick = () => { soeLists[key].splice(i, 1); renderSoeEditor(); };
      row(tb, [idnText(idn), soeLists.names[idn] || "", del]);
    });
  }
  const cmds = $("e-soe-cmds").tBodies[0];
  cmds.replaceChildren();
  for (const c of s.commands) row(cmds, [c.text, el("code", {}, c.data), c.comment]);
}

async function fillSoeSuggestions(key) {
  const mark = key === "mdt" ? "r" : "t";
  try {
    const r = await api("GET", `/devices/${editPos}/soe?mapping=${mark}&limit=1000`);
    $(`e-${key}-list`).replaceChildren(...r.parameters.map((p) => el("option", { value: `${p.text} ${p.name}` })));
    for (const p of r.parameters) soeLists.names[p.idn] = `${p.name} (${p.bits} bit)`;
  } catch (e) { /* no dictionary */ }
}

function addSoe(key) {
  const text = $(`e-${key}-add`).value.trim();
  const m = text.match(/^([SP])-(\d)-(\d{1,4})/i);
  if (!m) { banner("Enter an IDN like S-0-0036 (pick one from the list)"); return; }
  const idn = (m[1].toUpperCase() === "P" ? 0x8000 : 0) | (Number(m[2]) << 12) | Number(m[3]);
  if (!soeLists[key].includes(idn)) soeLists[key].push(idn);
  $(`e-${key}-add`).value = "";
  renderSoeEditor();
}
$("e-mdt-btn").onclick = () => addSoe("mdt");
$("e-at-btn").onclick = () => addSoe("at");

function updateEditorRules() {
  const sel = selectedPdos();
  const problems = [];
  for (const p of editData.pdos) {
    for (const x of p.excludes) if (sel.includes(p.index) && sel.includes(x) && p.index < x) problems.push(`${hex(p.index)} and ${hex(x)} exclude each other`);
  }
  const [inB, outB] = pdoBytes(sel);
  $("e-sizes").textContent = `${inB} B inputs, ${outB} B outputs` + (problems.length ? " - " + problems.join("; ") : "");
  $("e-sizes").className = problems.length ? "error" : "muted";
  $("e-save").disabled = problems.length > 0;
}

async function openEditor(pos) {
  try {
    editData = await api("GET", `/devices/${pos}/edit`);
    editPos = pos;
    soeLists = null;
    $("editor").hidden = false;
    renderEditor();
    if (editData.soe) { fillSoeSuggestions("mdt"); fillSoeSuggestions("at"); }
    $("editor").scrollIntoView({ behavior: "smooth" });
  } catch (e) {
    banner(e.message);
  }
}

async function saveEdit(change) {
  try {
    banner("");
    editData = await api("PUT", `/devices/${editPos}/edit`, change);
    renderEditor();
    $("e-msg").textContent = "Saved. Build the configuration to see and write the result.";
    $("e-msg").className = "ok";
  } catch (e) {
    banner(e.message);
  }
}

$("e-save").onclick = () => {
  const change = { name: $("e-name").value.trim() };
  if (editData.editable) change.pdos = selectedPdos();
  if (editData.soe && soeLists) {
    change.soe_mdt = soeLists.mdt;
    change.soe_at = soeLists.at;
  }
  soeLists = null;
  saveEdit(change);
};
$("e-reset").onclick = async () => {
  if (!confirm("Forget the saved name, PDO choice and startup parameters of this position?")) return;
  try {
    editData = await api("DELETE", `/devices/${editPos}/edit`);
    soeLists = null;
    renderEditor();
  } catch (e) {
    banner(e.message);
  }
};
$("e-close").onclick = () => { $("editor").hidden = true; editPos = null; };
$("d-edit").onclick = () => { if (selected !== null) openEditor(selected); };

// --- CoE objects ----------------------------------------------------------------------------------

let objPos = null;
let objList = [];
let currentObject = null;

function renderObjectList() {
  const f = $("o-filter").value.trim().toLowerCase();
  const tb = $("o-list").tBodies[0];
  tb.replaceChildren();
  for (const o of objList) {
    if (f && !(`${hex(o.index)} ${o.name}`.toLowerCase().includes(f))) continue;
    const tr = row(tb, [hex(o.index), o.name, [o.object_code, o.startup ? "startup" : ""].filter(Boolean).join(", ")],
      { class: "click" + (o.index === currentObject ? " selected" : "") });
    tr.onclick = () => openObject(o.index);
  }
}

async function openObjects(pos, refresh) {
  objPos = pos;
  currentObject = null;
  $("objects").hidden = false;
  $("o-title").textContent = `Objects of position ${pos}`;
  $("o-obj-title").textContent = "Loading the object list ...";
  $("o-entries").tBodies[0].replaceChildren();
  $("o-list").tBodies[0].replaceChildren();
  $("objects").scrollIntoView({ behavior: "smooth" });
  try {
    objList = (await api("GET", `/devices/${pos}/objects` + (refresh ? "?refresh=1" : ""))).objects;
    $("o-obj-title").textContent = `${objList.length} objects - choose one`;
    renderObjectList();
  } catch (e) {
    objList = [];
    $("o-obj-title").textContent = "Not available: " + e.message;
  }
}

function accessText(a) {
  const r = ["PreOp", "SafeOp", "Op"].filter((s) => a["read_" + s.toLowerCase()]);
  const w = ["PreOp", "SafeOp", "Op"].filter((s) => a["write_" + s.toLowerCase()]);
  return (r.length ? "R" : "") + (w.length ? "W" : "") + (w.length && w.length < 3 ? ` (write ${w.join("/")})` : "");
}

async function openObject(index) {
  currentObject = index;
  renderObjectList();
  let o;
  try {
    o = await api("GET", `/devices/${objPos}/objects/${index}`);
  } catch (e) {
    $("o-obj-title").textContent = e.message;
    return;
  }
  $("o-obj-title").textContent = `${hex(o.index)} ${o.name} (${o.object_code})`;
  const tb = $("o-entries").tBodies[0];
  tb.replaceChildren();
  for (const e of o.entries) {
    if (e.data_type === undefined) {
      row(tb, [e.subindex, el("span", { class: "muted" }, e.error || "-"), "", "", "", "", ""]);
      continue;
    }
    const value = e.error ? el("span", { class: "muted" }, "-") : (typeof e.value === "string" && e.value === e.hex ? el("code", {}, e.hex) : String(e.value));
    let input = "";
    let actions = "";
    if (e.writable) {
      input = el("input", { type: "text", value: e.error ? "" : String(e.value) });
      const now = el("button", { class: "small" }, "Write now");
      now.disabled = !writesAllowed();
      now.title = writesAllowed() ? "" : "Turn on \"Allow writes\" first";
      now.onclick = async () => {
        if (!confirm(`Write ${input.value} to ${hex(o.index)}:${e.subindex} "${e.name}" on the device now?`)) return;
        try {
          await api("PUT", `/devices/${objPos}/objects/${o.index}/${e.subindex}`, { value: input.value, confirm: true });
          openObject(o.index);
        } catch (err) {
          banner(err.message);
        }
      };
      const keep = el("button", { class: "small" }, "Startup");
      keep.title = "Keep this value as a startup parameter in the configuration";
      keep.onclick = async () => {
        try {
          await api("POST", `/devices/${objPos}/edit/startup`, { index: o.index, subindex: e.subindex, value: input.value });
          banner(`Startup parameter ${hex(o.index)}:${e.subindex} = ${input.value} saved for position ${objPos}. ` +
            "Build the configuration to apply it.", true);
          if (editPos === objPos) openEditor(objPos);
        } catch (err) {
          banner(err.message);
        }
      };
      actions = el("span", {}, now, " ", keep);
    }
    row(tb, [e.subindex, e.name, `${e.data_type} (${e.bits})`, accessText(e.access), value, input, actions]);
  }
}

$("o-filter").oninput = renderObjectList;
$("o-refresh").onclick = () => { if (objPos !== null) openObjects(objPos, true); };
$("o-close").onclick = () => { $("objects").hidden = true; objPos = null; currentObject = null; };
$("d-objects").onclick = () => { if (selected !== null) openObjects(selected); };

// --- SoE parameters ------------------------------------------------------------------------------

let soePos = null;
let soeIdn = null;
let soeTimer = null;

async function loadSoeList() {
  try {
    const r = await api("GET", `/devices/${soePos}/soe?q=${encodeURIComponent($("s-filter").value.trim())}&mapping=${$("s-mapping").value}`);
    const tb = $("s-list").tBodies[0];
    tb.replaceChildren();
    for (const p of r.parameters) {
      const tr = row(tb, [p.text, p.name, p.unit], { class: "click" + (p.idn === soeIdn ? " selected" : "") });
      tr.onclick = () => openSoe(p.idn);
    }
    $("s-param-title").textContent = soeIdn === null ? `${r.parameters.length} parameters - choose one` : $("s-param-title").textContent;
  } catch (e) {
    $("s-param-title").textContent = e.message;
  }
}

async function openSoeList(pos) {
  soePos = pos;
  soeIdn = null;
  $("soe").hidden = false;
  $("s-title").textContent = `Parameters of position ${pos}`;
  $("s-param").replaceChildren();
  $("s-edit").hidden = true;
  $("soe").scrollIntoView({ behavior: "smooth" });
  loadSoeList();
}

async function openSoe(idn) {
  soeIdn = idn;
  for (const tr of $("s-list").tBodies[0].rows) tr.classList.toggle("selected", tr.cells[0].textContent === idnText(idn));
  $("s-param-title").textContent = idnText(idn);
  const dl = $("s-param");
  dl.replaceChildren();
  const add = (k, v) => dl.append(el("dt", {}, k), el("dd", {}, v));
  try {
    const p = await api("GET", `/devices/${soePos}/soe/${idn}`);
    $("s-param-title").textContent = `${p.text} ${p.name}`;
    add("Value", `${Array.isArray(p.value) ? p.value.join(", ") : p.value} ${p.unit || ""}`);
    const a = p.attribute;
    add("Type", `${a.display}${a.list ? " list" : ""}, ${a.size} byte(s)${a.decimals ? `, ${a.decimals} decimals` : ""}`);
    const prot = Object.entries(a.write_protected).filter(([, v]) => v).map(([k]) => k);
    add("Write-protected in", prot.length ? prot.join(", ") : "-");
    add("Raw", el("code", {}, p.hex));
    $("s-edit").hidden = a.command;
    $("s-value").value = Array.isArray(p.value) ? p.value.join(" ") : String(p.value);
    $("s-write").disabled = !writesAllowed();
    $("s-write").title = writesAllowed() ? "" : "Turn on \"Allow writes\" first";
  } catch (e) {
    add("Online", el("span", { class: "muted" }, e.message));
    $("s-edit").hidden = false;               // a startup parameter can still be added from the dictionary
    $("s-write").disabled = true;
  }
}

$("s-write").onclick = async () => {
  if (!confirm(`Write ${$("s-value").value} to ${idnText(soeIdn)} on the drive now?`)) return;
  try {
    await api("PUT", `/devices/${soePos}/soe/${soeIdn}`, { value: $("s-value").value, confirm: true });
    openSoe(soeIdn);
  } catch (e) {
    banner(e.message);
  }
};
$("s-startup").onclick = async () => {
  try {
    await api("POST", `/devices/${soePos}/edit/startup`, { idn: soeIdn, value: $("s-value").value });
    banner(`Startup parameter ${idnText(soeIdn)} = ${$("s-value").value} saved for position ${soePos}. ` +
      "Build the configuration to apply it.", true);
    if (editPos === soePos) openEditor(soePos);
  } catch (e) {
    banner(e.message);
  }
};
$("s-filter").oninput = () => { clearTimeout(soeTimer); soeTimer = setTimeout(loadSoeList, 250); };
$("s-mapping").onchange = loadSoeList;
$("s-close").onclick = () => { $("soe").hidden = true; soePos = null; };
$("d-soe").onclick = () => { if (selected !== null) openSoeList(selected); };

// --- process data --------------------------------------------------------------------------------

let pdBusy = false;

function pdValue(v) {
  if (v.error) return el("span", { class: "muted", title: v.error }, "?");
  if (Array.isArray(v.value)) return el("code", {}, v.value.map((b) => b.toString(16).padStart(2, "0")).join(" ").toUpperCase());
  return String(v.value);
}

function pdSetter(v) {
  if (v.dir !== "output" || !writesAllowed()) return "";
  const send = async (value) => {
    try {
      await api("PUT", "/process-data", { name: v.name, value });
      loadPd();
    } catch (e) {
      banner(e.message);
    }
  };
  if (typeof v.value === "boolean") {
    const b = el("button", { class: "small" }, v.value ? "Set false" : "Set true");
    b.onclick = () => send(!v.value);
    return b;
  }
  const input = el("input", { type: "text", placeholder: Array.isArray(v.value) ? "hex bytes" : "value" });
  const b = el("button", { class: "small" }, "Set");
  b.onclick = () => send(input.value);
  input.onkeydown = (ev) => { if (ev.key === "Enter") send(input.value); };
  return el("span", {}, input, " ", b);
}

async function loadPd() {
  if (pdBusy) return;
  const focused = document.activeElement && document.activeElement.closest && document.activeElement.closest("#pd");
  if (focused) return;                          // don't replace a field the user is typing in
  pdBusy = true;
  try {
    const r = await api("GET", "/process-data");
    const f = $("pd-filter").value.trim().toLowerCase();
    const tb = $("pd").tBodies[0];
    tb.replaceChildren();
    let n = 0;
    for (const v of r.variables) {
      if (f && !v.name.toLowerCase().includes(f)) continue;
      n++;
      row(tb, [v.name, v.dir === "output" ? "out" : "in", v.type, el("td", { class: "num" }, pdValue(v)), pdSetter(v)]);
    }
    $("pd-info").textContent = `${n} of ${r.variables.length} variables, ${new Date(r.time * 1000).toLocaleTimeString()}`;
  } catch (e) {
    $("pd-info").textContent = e.message;
  } finally {
    pdBusy = false;
  }
}

$("pd-filter").oninput = loadPd;
setInterval(() => { if (!$("view-pd").hidden && $("pd-live").checked) loadPd(); }, 1000);

$("d-close").onclick = () => {
  selected = null;
  $("detail").hidden = true;
  for (const tr of $("devices").tBodies[0].rows) tr.classList.remove("selected");
};

// --- ESI library ----------------------------------------------------------------------------------

let searchTimer = null;

async function loadEsi() {
  try {
    const r = await api("GET", "/esi?q=" + encodeURIComponent($("esi-search").value.trim()));
    $("esi-count").textContent = `${r.files.length} files, ${r.total} devices` +
      (r.devices.length < r.total ? ` - ${r.devices.length} shown` : "");
    const tb = $("esi-devices").tBodies[0];
    tb.replaceChildren();
    for (const d of r.devices) {
      row(tb, [d.type, d.name, d.group, d.vendor_name || hex(d.vendor, 1), hex(d.product, 8), hex(d.revision, 8), d.file]);
    }
    const fl = $("esi-filelist");
    fl.replaceChildren();
    for (const f of r.files) {
      const del = el("button", { class: "small" }, "Delete");
      del.onclick = async () => {
        if (!confirm(`Delete ${f} from the library?`)) return;
        try {
          await api("DELETE", "/esi/" + encodeURIComponent(f));
          loadEsi();
        } catch (e) {
          banner(e.message);
        }
      };
      fl.append(el("li", {}, f, " ", del));
    }
  } catch (e) {
    banner(e.message);
  }
}

$("esi-search").oninput = () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(loadEsi, 250);
};

$("esi-upload").onclick = async () => {
  const files = [...$("esi-files").files];
  if (!files.length) {
    banner("Choose one or more files first");
    return;
  }
  const out = $("esi-result");
  out.replaceChildren();
  let added = 0;
  const skipped = [];
  $("esi-upload").disabled = true;
  try {
    for (const [i, f] of files.entries()) {
      out.replaceChildren(el("p", { class: "muted" }, `Uploading ${i + 1} of ${files.length}: ${f.name}`));
      try {
        const r = await api("POST", "/esi?name=" + encodeURIComponent(f.name), undefined, f);
        added += r.added.reduce((n, a) => n + a.devices, 0);
        skipped.push(...r.skipped);
      } catch (e) {
        skipped.push({ file: f.name, reason: e.message });
      }
    }
  } finally {
    $("esi-upload").disabled = false;
  }
  const reasons = {};
  for (const s of skipped) (reasons[s.reason] = reasons[s.reason] || []).push(s.file);
  out.replaceChildren(el("p", { class: added ? "ok" : "muted" }, `${added} device${added === 1 ? "" : "s"} added.`),
    ...Object.entries(reasons).map(([reason, names]) => el("details", {},
      el("summary", {}, `${names.length} skipped: ${reason}`), el("ul", { class: "files" }, ...names.map((n) => el("li", {}, n))))));
  $("esi-files").value = "";
  loadEsi();
};

// --- start --------------------------------------------------------------------------------------

let startView = "bus";
try { startView = sessionStorage.getItem("view") || "bus"; } catch (e) { /* default */ }
show(startView);
poll();
setInterval(poll, 1000);
