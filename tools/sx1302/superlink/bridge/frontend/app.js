"use strict";
const $ = (id) => document.getElementById(id);
const TABS = ["devices", "activity", "gateway", "setup"];
const STATES = {
  awaiting_reconnect: "Awaiting reconnect",
  discovered: "Discovered",
  adopting: "Pairing",
  adopted: "Adopted",
  active: "Connected",
  lost: "Link lost",
  beaconing: "Reconnecting",
  idle: "Waiting",
};
const ICONS = {
  door: "M7 2h10a1 1 0 0 1 1 1v18h2v2H4v-2h2V3a1 1 0 0 1 1-1zm1 2v17h8V4H8zm5 7h2v2h-2z",
  temp: "M15 13V5a3 3 0 0 0-6 0v8a5 5 0 1 0 6 0zm-3-9a1 1 0 0 1 1 1v3h-2V5a1 1 0 0 1 1-1z",
  radio:
    "M12 10a2 2 0 1 1 0 4 2 2 0 0 1 0-4zM7.05 7.05l1.41 1.41a5 5 0 0 0 0 7.08l-1.41 1.41a7 7 0 0 1 0-9.9zm9.9 0a7 7 0 0 1 0 9.9l-1.41-1.41a5 5 0 0 0 0-7.08zM4.22 4.22l1.42 1.42a9 9 0 0 0 0 12.72l-1.42 1.42a11 11 0 0 1 0-15.56zm15.56 0a11 11 0 0 1 0 15.56l-1.42-1.42a9 9 0 0 0 0-12.72z",
};

let token = "",
  ingressSession = false,
  sessionVersion = 0,
  state = null,
  timer = null,
  refreshing = false,
  selectedMac = null,
  cardNodes = new Map();

// ---- helpers ----------------------------------------------------------------
function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text != null) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function icon(name, off) {
  const wrap = el("span", null, `icon${off ? " off" : ""}`);
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  path.setAttribute("d", ICONS[name]);
  svg.append(path);
  wrap.append(svg);
  return wrap;
}
function chip(text, dot) {
  const node = el("span", null, "chip");
  if (dot) node.append(el("span", null, `dot ${dot}`));
  node.append(el("span", text));
  return node;
}
let toastTimer = null;
function toast(message) {
  clearTimeout(toastTimer);
  $("toast").textContent = message;
  $("toast").hidden = !message;
  if (message) toastTimer = setTimeout(() => ($("toast").hidden = true), 5000);
}
async function api(path, body) {
  const requestToken = token;
  try {
    return await requestJSON(`api/${path}`, {
    method: body === undefined ? "GET" : "POST",
    headers: { Authorization: `Bearer ${requestToken}`, "Content-Type": "application/json" },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  } catch (error) {
    if ([401, 403].includes(error.status) && token === requestToken) lock();
    throw error;
  }
}
const demo = () => state?.demo ?? true;
const radioReady = () => state?.radio?.state === "connected";
const macLabel = (mac) => mac.match(/../g).join(":").toUpperCase();
const online = (d) => ["active", "adopted"].includes(d.state);
function age(ts) {
  if (!ts) return "never";
  const s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (s < 5) return "now";
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  return `${Math.floor(s / 3600)}h ago`;
}
const isBattery = (p) => ["BATTERY", "BATTERY_LEVEL"].includes(p.name);
function format(p) {
  if (typeof p.value === "boolean") return p.name === "MOTION" ? (p.value ? "Detected" : "Clear") : p.value ? "On" : "Off";
  if (typeof p.value === "string") return p.value.charAt(0).toUpperCase() + p.value.slice(1);
  return `${p.value}${p.unit ? (p.unit === "%" ? "%" : ` ${p.unit}`) : ""}`;
}
const label = (name) => name.charAt(0) + name.slice(1).replaceAll("_", " ").toLowerCase();
function deviceIcon(d) {
  const names = d.properties.map((p) => p.name).join(" ");
  const kind = /DOOR|ENTRY/.test(names) ? "door" : /TEMP/.test(names) ? "temp" : "radio";
  return icon(kind, !online(d));
}

// ---- navigation + session --------------------------------------------------
function tab(name) {
  if (name !== "setup" && !state) return;
  for (const t of TABS) $(`tab-${t}`).hidden = t !== name;
  document.querySelectorAll("[data-tab]").forEach((b) => b.setAttribute("aria-selected", b.dataset.tab === name));
}
document.querySelectorAll("[data-tab],[data-goto]").forEach((b) =>
  b.addEventListener("click", () => tab(b.dataset.tab || b.dataset.goto)),
);

function lock() {
  sessionVersion++;
  token = "";
  state = null;
  clearTimeout(timer);
  $("token").value = "";
  $("login").hidden = ingressSession;
  $("session-status").hidden = !ingressSession;
  $("session-message").textContent = "Home Assistant session expired. Reopen the app from the sidebar or retry.";
  $("health").hidden = true;
  cardNodes.clear();
  $("devices").replaceChildren();
  $("activity").replaceChildren();
  $("gateway").replaceChildren();
  $("tabs").hidden = $("lock").hidden = $("mqtt-chip").hidden = $("demo-chip").hidden = true;
  for (const t of TABS) $(`tab-${t}`).hidden = true;
  $("dialog").close();
}
$("lock").addEventListener("click", lock);

async function unlock(ingress = false) {
  const version = ++sessionVersion;
  ingressSession = ingress;
  token = ingress ? "ingress" : $("token").value.trim();
  try {
    const snapshot = await api("state");
    if (version !== sessionVersion) return;
    state = snapshot;
    toast("");
    $("token").value = "";
    $("login").hidden = true;
    $("session-status").hidden = true;
    $("tabs").hidden = false;
    $("lock").hidden = ingress;
    tab("devices");
    render();
    clearTimeout(timer);
    timer = setTimeout(refresh, 3000);
  } catch (error) {
    if (ingress) {
      $("session-status").hidden = false;
      $("session-message").textContent = error.message;
    }
    toast(error.message);
  }
}
$("login-form").addEventListener("submit", (e) => {
  e.preventDefault();
  unlock();
});
async function bootstrap() {
  $("retry-session").disabled = true;
  try {
    const session = await requestJSON("api/session");
    ingressSession = session.ingress === true;
    if (ingressSession) await unlock(true);
    else {
      $("session-status").hidden = true;
      $("login").hidden = false;
    }
  } catch (error) {
    $("session-status").hidden = false;
    $("session-message").textContent = error.message;
  } finally {
    $("retry-session").disabled = false;
  }
}
$("retry-session").addEventListener("click", bootstrap);
bootstrap();

async function refresh() {
  if (refreshing || !token) return;
  refreshing = true;
  const requestToken = token;
  const version = sessionVersion;
  clearTimeout(timer);
  try {
    const snapshot = await api("state");
    if (token !== requestToken || version !== sessionVersion) return;
    state = snapshot;
    render();
  } catch (error) {
    toast(error.message);
    if (state) {
      $("health").textContent = `${error.message} Displayed readings may be stale.`;
      $("health").hidden = false;
    }
    $("mqtt-chip").replaceChildren(el("span", null, "dot error"), el("span", "Bridge offline"));
  } finally {
    refreshing = false;
    clearTimeout(timer);
    if (token) timer = setTimeout(refresh, 3000);
  }
}

// ---- rendering ---------------------------------------------------------------
function render() {
  $("health").hidden = radioReady();
  $("health").textContent = state.radio?.error || "Connecting to the radio. The bridge will retry automatically; sensor readings may be stale.";
  const mqttDot = { connected: "ok", disconnected: "error" }[state.mqtt] || "";
  $("mqtt-chip").replaceChildren(el("span", null, `dot ${mqttDot}`), el("span", `MQTT ${state.mqtt}`));
  $("mqtt-chip").hidden = false;
  $("demo-chip").hidden = !state.demo;
  renderDevices();
  renderActivity();
  renderGateway();
  if (selectedMac && $("dialog").open) fillDialog();
}

function renderDevices() {
  const query = $("search").value.trim().toLowerCase();
  const devices = state.devices.filter((d) => `${d.name} ${d.mac} ${macLabel(d.mac)}`.toLowerCase().includes(query));
  const cards = devices.map((d) => {
    // Reuse card nodes across polls so a click spanning a refresh still lands.
    let card = cardNodes.get(d.mac);
    if (!card) {
      card = el("button", null, "card device");
      card.addEventListener("click", () => openDialog(d.mac));
      cardNodes.set(d.mac, card);
    }
    const head = el("div", null, "device-head");
    const title = el("div");
    title.append(el("div", d.name, "device-name"), el("div", STATES[d.state] || d.state, "secondary"));
    head.append(deviceIcon(d), title);
    const reading = d.properties.find((p) => !isBattery(p));
    const battery = d.properties.find(isBattery);
    const meta = el("div", null, "meta");
    if (battery) meta.append(chip(`🔋 ${format(battery)}`));
    if (d.rssi != null) meta.append(chip(`${d.rssi} dBm`));
    meta.append(chip(age(d.last_seen), online(d) ? "ok" : d.state === "lost" ? "error" : "warn"));
    card.replaceChildren(head, el("div", reading ? format(reading) : "—", "primary-reading"), meta);
    return card;
  });
  const current = [...$("devices").children];
  if (current.length !== cards.length || cards.some((c, i) => c !== current[i])) $("devices").replaceChildren(...cards);
  $("devices-empty").hidden = state.devices.length !== 0;
  $("search-empty").hidden = !state.devices.length || devices.length !== 0;
  for (const mac of cardNodes.keys()) if (!state.devices.some((d) => d.mac === mac)) cardNodes.delete(mac);
  const recent = state.devices.filter((d) => d.last_seen && Date.now() / 1000 - d.last_seen < 60).length;
  $("device-count").textContent = state.devices.length ? `${state.devices.length} ${state.devices.length === 1 ? "device" : "devices"} · ${recent} seen in last minute` : "";
}
$("search").addEventListener("input", () => state && renderDevices());

function renderActivity() {
  const rows = state.activity.map((item) => {
    const li = el("li");
    const text = el("div", item.message, "grow");
    if (item.mac) text.append(el("div", macLabel(item.mac), "secondary"));
    li.append(el("time", new Date(item.time * 1000).toLocaleTimeString()), text);
    return li;
  });
  $("activity").replaceChildren(...(rows.length ? rows : [el("li", "No activity since the bridge started.", "empty")]));
}

function renderGateway() {
  const backend = state.demo ? "SX1302 (demo)" : state.backend === "sx1302-ssh" ? "SX1302 over SSH" : "SX1302";
  $("gateway").replaceChildren(
    ...[
      ["Radio backend", backend],
      ["Radio connection", state.radio?.state || "unknown"],
      ["Gateway MAC", macLabel(state.gateway_mac)],
      ["MQTT", state.mqtt],
      ["Devices", String(state.devices.length)],
    ].map(([k, v]) => {
      const li = el("li");
      li.append(el("span", k, "grow"), el("span", v, "value"));
      return li;
    }),
  );
}

// ---- device dialog -------------------------------------------------------------
function openDialog(mac) {
  selectedMac = mac;
  fillDialog();
  $("dialog").showModal();
}
$("d-close").addEventListener("click", () => $("dialog").close());
$("dialog").addEventListener("close", () => {
  selectedMac = null;
  delete $("d-actions").dataset.signature;
});

function fillDialog() {
  const d = state.devices.find((item) => item.mac === selectedMac);
  if (!d) return $("dialog").close();
  $("d-icon").replaceWith(Object.assign(deviceIcon(d), { id: "d-icon" }));
  $("d-name").textContent = d.name;
  $("d-sub").textContent = `${macLabel(d.mac)} · ${STATES[d.state] || d.state}`;
  const rows = [
    ...d.properties.map((p) => [`${label(p.name)}${p.channel > 1 ? ` (ch ${p.channel})` : ""}`, format(p)]),
    ["Signal", d.rssi == null ? "—" : `${d.rssi} dBm${d.snr == null ? "" : ` · SNR ${d.snr}`}`],
    ["Firmware", d.firmware || "—"],
    ["Last seen", age(d.last_seen)],
  ];
  $("d-readings").replaceChildren(
    ...rows.map(([k, v]) => {
      const li = el("li");
      li.append(el("span", k, "grow"), el("span", v, "value"));
      return li;
    }),
  );
  const signature = `${d.mac}:${d.state}:${d.has_session}:${d.adopted}:${demo()}:${radioReady()}`;
  if ($("d-actions").dataset.signature === signature) return;
  $("d-actions").dataset.signature = signature;
  const commands =
    d.state === "discovered" && !d.has_session
      ? [["adopt", "Pair", "filled"]]
      : [
          ["locate", "Locate"],
          ["refresh", "Refresh info"],
          ["reboot", "Reboot", "danger"],
        ];
  $("d-actions").replaceChildren(
    el("span", "Commands are delivered when the sensor next wakes.", "secondary grow"),
    ...commands.map(([command, text, cls]) => {
      const button = el("button", text, cls);
      button.disabled = demo() || !radioReady() || (command !== "adopt" && (!d.has_session || !d.adopted));
      button.addEventListener("click", async () => {
        if (command === "reboot" && !confirm(`Reboot ${d.name}?`)) return;
        button.disabled = true;
        try {
          await api(`devices/${d.mac}/command`, { command });
          toast(`${text} queued for ${d.name}.`);
          await refresh();
        } catch (error) {
          toast(error.message);
        } finally {
          button.disabled = demo() || !radioReady();
        }
      });
      return button;
    }),
  );
}

// ---- diagnostics -------------------------------------------------------------------
$("diagnostics").addEventListener("click", () => {
  const url = URL.createObjectURL(new Blob([JSON.stringify(state, null, 2)], { type: "application/json" }));
  const link = el("a");
  link.href = url;
  link.download = "superlink-diagnostics.json";
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});
