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

async function api(method, path, body) {
  const opts = { method, headers: { Authorization: "Bearer " + TOKEN } };
  if (body !== undefined) {
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

async function poll() {
  try {
    const s = await api("GET", "/state");
    $("counter").textContent = s.counter;
    $("uptime").textContent = s.uptime_s + " s";
    $("conn").textContent = s.connected ? "Data Layer connected" : "Data Layer offline";
    $("conn").className = "chip " + (s.connected ? "ok" : "bad");
  } catch (e) {
    $("conn").textContent = "App not reachable";
    $("conn").className = "chip bad";
  }
}

async function loadConfig() {
  const c = await api("GET", "/config");
  $("setpoint").value = c.setpoint;
}

$("save").onclick = async () => {
  try {
    const c = await api("PATCH", "/config", { setpoint: Number($("setpoint").value) });
    $("setpoint").value = c.setpoint;
    banner("Saved", true);
  } catch (e) {
    banner(e.message);
  }
};

$("read").onclick = async () => {
  try {
    const r = await api("GET", "/node?path=" + encodeURIComponent($("path").value.trim()));
    $("result").textContent = JSON.stringify(r.value, null, 2);
    banner("");
  } catch (e) {
    $("result").textContent = "";
    banner(e.message);
  }
};

$("write").onclick = async () => {
  let value;
  try {
    value = JSON.parse($("value").value);
  } catch (e) {
    banner("The value must be JSON: true, 1.5, \"text\", [1, 2]");
    return;
  }
  try {
    const r = await api("PUT", "/node?path=" + encodeURIComponent($("path").value.trim()), { value });
    $("result").textContent = JSON.stringify(r.value, null, 2);
    banner("Written", true);
  } catch (e) {
    banner(e.message);
  }
};

loadConfig().catch((e) => banner(e.message));
poll();
setInterval(poll, 1000);
