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
  eni: { same: ["same", "ok"], different: ["different", "bad"], none: ["not configured", "warn"] },
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
  for (const t of document.querySelectorAll(".tab")) t.classList.toggle("active", t.dataset.view === view);
  $("view-bus").hidden = view !== "bus";
  $("view-esi").hidden = view !== "esi";
  try { sessionStorage.setItem("view", view); } catch (e) { /* not needed */ }
  if (view === "esi") loadEsi();
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
  try {
    banner("");
    await api("POST", "/download", { plan_id: plan.id, confirm: true });
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
