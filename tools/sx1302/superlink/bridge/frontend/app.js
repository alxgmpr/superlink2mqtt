"use strict";
const $ = (id) => document.getElementById(id);
const TABS = ["devices", "activity", "gateway"];
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
const macLabel = (mac) => mac ? mac.match(/../g).join(":").toUpperCase() : "Not configured";
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
  if (!state) return;
  for (const t of TABS) $(`tab-${t}`).hidden = t !== name;
  document.querySelectorAll("[data-tab]").forEach((b) => b.setAttribute("aria-selected", b.dataset.tab === name));
}
document.querySelectorAll("[data-tab]").forEach((b) =>
  b.addEventListener("click", () => tab(b.dataset.tab)),
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
  $("setup-dialog").close();
  setupFlow = new SetupFlow();
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
// Start only after all controllers below have initialized.


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
    if ($("setup-error").textContent.startsWith("Connection interrupted.")) $("setup-error").hidden = true;
    render();
  } catch (error) {
    toast(error.message);
    if ($("setup-dialog").open) {
      $("setup-error").textContent = "Connection interrupted. We’ll keep checking; your progress is saved for this visit.";
      $("setup-error").hidden = false;
      $("setup-next").disabled = true;
      $("setup-devices").querySelectorAll("button").forEach((b) => { b.disabled = true; });
    }
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
  renderSetup();
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
  $("devices-empty").hidden = state.devices.some((d) => d.adopted) || !!state.setup?.blocked;
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

// ---- first sensor setup -----------------------------------------------------
let setupFlow = new SetupFlow();
const setupGuide = "https://github.com/alxgmpr/superlink2mqtt/blob/codex/ha-setup-recovery/homeassistant/superlink2mqtt/DOCS.md";
const art = {
  pi: '<rect x="43" y="45" width="104" height="76" rx="12" class="art-board"/><rect x="74" y="66" width="34" height="32" rx="5"/><path d="M80 60v6m10-6v6m10-6v6M80 98v6m10-6v6m10-6v6M68 74h6m-6 14h6m34-14h6m-6 14h6M55 65h10m-10 10h6m-6 10h6m-6 10h10M119 58h16v14h-16zM119 83h16v22h-16z"/><circle cx="54" cy="111" r="2"/><path d="M133 45V22m-7 5 7-5 7 5"/>',
  home: '<path d="m344 77 42-35 42 35v43h-84z" class="art-board"/><path d="M374 120V91h24v29M356 76h11v12h-11zM404 76h11v12h-11z"/><circle cx="386" cy="65" r="5"/>',
  sensor: '<rect x="64" y="36" width="42" height="89" rx="13" class="art-board"/><rect x="115" y="49" width="19" height="62" rx="8"/><circle cx="85" cy="53" r="3" class="art-light"/><path d="M78 106h14"/>',
  waves: '<path d="M166 71q12 12 0 24m12-35q23 23 0 46m13-58q35 35 0 70" class="art-signal"/>',
};
const diagrams = [
  ["Your Pi radio connects to Home Assistant over your network", `${art.pi}${art.home}<path d="M166 83h160" class="art-link"/><rect x="216" y="66" width="60" height="34" rx="17" class="art-pill"/><text x="246" y="88">LAN</text><text x="96" y="150">Pi + SX1302</text><text x="386" y="150">Home Assistant</text>`],
  ["MQTT carries readings from SuperLink to Home Assistant", `<rect x="42" y="48" width="94" height="70" rx="13" class="art-board"/><path d="M77 72q-10 12 0 24m24-24q10 12 0 24M70 65q-17 19 0 38m38-38q17 19 0 38"/><circle cx="89" cy="84" r="4"/><path d="M153 83h50m74 0h50" class="art-link"/><rect x="209" y="51" width="62" height="64" rx="12" class="art-board"/><path d="M222 68h36m-36 16h36m-36 16h36"/><circle cx="229" cy="68" r="2"/><circle cx="250" cy="84" r="2"/>${art.home}<text x="89" y="150">SuperLink</text><text x="240" y="150">MQTT broker</text><text x="386" y="150">Home Assistant</text>`],
  ["A sensor in pairing mode is discovered by your radio", `${art.sensor}${art.waves}<g transform="translate(263 0)">${art.pi}</g><text x="99" y="150">Your sensor</text><text x="359" y="150">Your radio</text><path d="M228 83h58" class="art-link"/>`],
  ["A sensor reading reaches your Home Assistant device", `${art.sensor}<path d="M155 83h167" class="art-link"/><circle cx="240" cy="83" r="21" class="art-board"/><path d="m230 83 7 7 14-15" class="art-signal"/>${art.home}<text x="99" y="150">First reading</text><text x="386" y="150">Home Assistant</text>`],
];
function drawSetup(index) {
  const [description, content] = diagrams[index];
  $("setup-art").innerHTML = `<svg viewBox="0 0 480 170" role="img" aria-label="${description}"><g fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${content}</g></svg>`;
}
function setupLink(text, href, external = false) {
  const a = el("a", text);
  a.href = href;
  a.target = external ? "_blank" : "_top";
  a.rel = "noopener noreferrer";
  return a;
}
function changeSetup(index) {
  setupFlow.index = index;
  delete $("setup-devices").dataset.signature;
  $("setup-help").open = false;
  renderSetup();
  $("setup-title").focus();
}
function dismissSetup() {
  setupFlow.dismiss();
  $("setup-dialog").close();
}
$("setup-close").addEventListener("click", dismissSetup);
$("setup-dialog").addEventListener("cancel", () => setupFlow.dismiss());
$("setup-open").addEventListener("click", () => {
  setupFlow.opened = true;
  renderSetup();
});
$("setup-back").addEventListener("click", () => changeSetup(setupFlow.index - 1));
$("setup-next").addEventListener("click", () => {
  if (!setupFlow.ready(state)) return;
  if (setupFlow.index === 3) {
    dismissSetup();
    tab("devices");
  } else changeSetup(setupFlow.index + 1);
});
async function pairFromSetup(mac) {
  if (setupFlow.pendingMac || !radioReady() || demo()) return;
  const version = sessionVersion;
  setupFlow.selectedMac = mac;
  setupFlow.pendingMac = mac;
  $("setup-error").hidden = true;
  renderSetup();
  try {
    await api(`devices/${mac}/command`, { command: "adopt" });
    if (version === sessionVersion) await refresh();
  } catch (error) {
    if (version !== sessionVersion) return;
    if (error.status === 400) setupFlow.pendingMac = null;
    $("setup-error").textContent = error.message;
    $("setup-error").hidden = false;
  }
}
function renderSetup() {
  setupFlow.observe(state);
  if (!setupFlow.opened) return;
  const dialog = $("setup-dialog");
  if (!dialog.open) {
    $("dialog").close();
    dialog.showModal();
  }
  const index = setupFlow.index;
  const device = state.devices.find((d) => d.mac === setupFlow.selectedMac);
  if (device?.has_session || device?.adopted) setupFlow.pendingMac = null;
  const ready = setupFlow.ready(state);
  const title = ["Connect your radio", "Connect MQTT", "Pair your first sensor", ready ? "Your sensor is ready" : "Try your sensor"][index];
  const description = [
    "Power your Pi + SX1302 and connect it to your home network.",
    "MQTT carries sensor readings into Home Assistant.",
    "Put a new or factory-reset sensor in pairing mode, close to your radio.",
    ready ? "A reading arrived. You can now find this sensor in Devices." : "Open the door, trigger motion, or wake the sensor. We’ll wait for its first reading.",
  ][index];
  const changed = dialog.dataset.step !== String(index);
  if (changed) {
    dialog.dataset.step = String(index);
    drawSetup(index);
    $("setup-help").open = false;
  }
  $("setup-counter").textContent = `STEP ${index + 1} OF 4`;
  $("setup-title").textContent = title;
  $("setup-description").textContent = description;
  document.querySelectorAll(".setup-progress li").forEach((li, i) => {
    li.classList.toggle("complete", i < index);
    if (i === index) li.setAttribute("aria-current", "step");
    else li.removeAttribute("aria-current");
  });
  $("setup-back").hidden = index === 0;
  $("setup-next").hidden = index === 2;
  $("setup-next").disabled = !ready;
  $("setup-next").textContent = index === 3 ? "View devices" : "Continue";
  const pending = state.setup?.missing || [];
  let message = index === 0 ? (ready ? (radioReady() ? "Radio connected" : "Radio configuration ready") : "Waiting for your radio") :
    index === 1 ? (ready ? "MQTT connected" : "Waiting for MQTT") :
    index === 2 ? (!radioReady() ? "Radio disconnected. Pairing will be available when it reconnects." : device?.has_session || setupFlow.pendingMac ? "Pairing… keep the sensor awake" : state.devices.some((d) => !d.adopted && !d.has_session) ? "Choose a sensor to pair" : "Listening for sensors…") :
    ready ? `${device.name} · ${format(device.properties[0])}` : "Waiting for a reading…";
  if (index === 3 && state.mqtt !== "connected") message = "MQTT disconnected. Reconnect your broker to finish.";
  if (index === 3 && !radioReady()) message = "Radio disconnected. Waiting for it to reconnect.";
  if (index === 0 && pending.includes("radio_host")) message = "Add your Pi’s address in app Configuration";
  else if (index === 0 && pending.some((name) => name !== "mqtt")) message = "Finish the radio connection below";
  const status = $("setup-status");
  status.classList.toggle("ready", ready);
  status.textContent = message;
  const candidates = index === 2 ? state.devices.filter((d) => !d.adopted) : [];
  const signature = JSON.stringify([index, candidates.map((d) => [d.mac, d.name, d.has_session]), setupFlow.pendingMac, radioReady()]);
  if ($("setup-devices").dataset.signature !== signature) {
    $("setup-devices").dataset.signature = signature;
    $("setup-devices").replaceChildren(...candidates.map((d) => {
      const row = el("div", null, "setup-device");
      const name = el("div", d.name);
      name.append(el("small", macLabel(d.mac)));
      const button = el("button", d.has_session || d.mac === setupFlow.pendingMac ? "Pairing…" : "Pair", "filled");
      button.disabled = !radioReady() || demo() || d.has_session || !!setupFlow.pendingMac;
      button.addEventListener("click", () => pairFromSetup(d.mac));
      row.append(name, button);
      return row;
    }));
  }
  const helpSignature = JSON.stringify([index, ingressSession, state.backend, pending]);
  if ($("setup-help-body").dataset.signature !== helpSignature) {
    $("setup-help-body").dataset.signature = helpSignature;
    $("setup-help-title").textContent = ["Connection help", "Set up MQTT", "Sensor not appearing?", "Check in Home Assistant"][index];
    const help = $("setup-help-body");
    help.replaceChildren();
    if (index === 0) {
      if (ingressSession) {
        help.append(el("p", "Set radio_host to your Pi’s IP address in app Configuration."),
          setupLink("Open app configuration ↗", "/config/app/local_superlink2mqtt/config"));
      }
      if (state.backend === "sx1302-ssh") {
        help.append(el("p", "The Pi needs its radio service and a dedicated SSH connection. Use the radio connection guide to prepare them."));
        if (pending.length) {
          const list = el("ul");
          const labels = {radio_host: "Pi address", radio_ed25519: "Radio SSH key", known_hosts: "Verified Pi host key", "bridge.yaml": "Gateway configuration"};
          for (const item of pending) if (labels[item]) list.append(el("li", `${labels[item]} — needed`));
          help.append(list);
        }
      } else help.append(el("p", "Check radio power, antenna and the SX1302 service. Only one process can use the radio."));
      help.append(setupLink("Radio connection guide ↗", `${setupGuide}#install-and-provision-the-radio`, true));
    } else if (index === 1) {
      help.append(el("p", ingressSession ? "Start the Mosquitto broker app. This app picks up its credentials automatically." : "Set the MQTT broker in your bridge configuration, then restart."),
        el("p", "Add the MQTT integration in Home Assistant, using that same broker."));
      if (ingressSession) help.append(setupLink("Open integrations ↗", "/config/integrations"));
      else help.append(setupLink("MQTT setup guide ↗", `${setupGuide}#before-starting`, true));
    } else if (index === 2) {
      help.append(el("p", "Follow your model’s pairing instructions and check its battery. A sensor already paired to UniFi needs a factory reset first."),
        el("p", "Resetting clears the previous pairing. Fresh keys are created when you pair here."));
    } else {
      help.append(el("p", "In the MQTT integration, find your sensor and check that its entity changes when you operate it."));
      if (ingressSession) help.append(setupLink("Open integrations ↗", "/config/integrations"));
      help.append(el("p", "A connected radio alone doesn’t confirm a sensor reading. Keep the sensor awake if we’re still waiting."));
    }
  }
  if (changed) {
    $("setup-title").focus();
  }
}
bootstrap();
