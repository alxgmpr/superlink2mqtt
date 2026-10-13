"use strict";
const $ = (id) => document.getElementById(id);
let token = "",
  state = null,
  previewDocument = null,
  previewJobId = null,
  sourceProbe = null,
  extractionState = { state: "idle" },
  selectedMac = null,
  timer = null,
  refreshing = false,
  previewVersion = 0;
function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function notice(message) {
  $("notice").textContent = message;
  $("notice").hidden = !message;
}
async function api(path, body) {
  const requestToken = token;
  const response = await fetch(`api/${path}`, {
    method: body === undefined ? "GET" : "POST",
    headers: {
      Authorization: `Bearer ${requestToken}`,
      "Content-Type": "application/json",
    },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    signal: AbortSignal.timeout(10000),
  });
  const result = await response.json();
  if (!response.ok) {
    if (response.status === 401 && token === requestToken) lock();
    throw new Error(result.error || "Gateway request failed");
  }
  return result;
}
function page(name) {
  for (const item of ["devices", "import", "activity", "setup"])
    $("page-" + item).hidden = item !== name;
  document.querySelectorAll(".nav").forEach((node) => {
    const active = node.dataset.page === name;
    node.classList.toggle("active", active);
    if (active) node.setAttribute("aria-current", "page");
    else node.removeAttribute("aria-current");
  });
  $("breadcrumb").textContent = {
    devices: "devices",
    import: "import",
    activity: "activity",
    setup: "gateway",
  }[name];
  document.title = `superlink2mqtt / ${$("breadcrumb").textContent}`;
}
function lock() {
  token = "";
  state = null;
  sourceProbe = null;
  extractionState = { state: "idle" };
  $("source-password").value = "";
  $("source-identity").hidden = true;
  $("source-credentials").hidden = true;
  $("extraction-status").hidden = true;
  $("review-extraction").hidden = true;
  $("confirm-source").checked = false;
  invalidatePreview();
  clearTimeout(timer);
  $("login").hidden = false;
  $("workspace").hidden = true;
  $("lock").hidden = true;
  $("connection").textContent = "locked";
  $("token").value = "";
  $("import-json").value = "";
  $("import-file").value = "";
  $("confirm-import").checked = false;
  $("apply-import").disabled = true;
  $("device-dialog").close();
}
function age(timestamp) {
  if (!timestamp) return "—";
  const seconds = Math.max(0, Math.floor(Date.now() / 1000 - timestamp));
  return seconds < 5
    ? "now"
    : seconds < 60
      ? `${seconds}s ago`
      : seconds < 3600
        ? `${Math.floor(seconds / 60)}m ago`
        : `${Math.floor(seconds / 3600)}h ago`;
}
function status(device) {
  return (
    {
      awaiting_reconnect: "Awaiting reconnect",
      discovered: "Discovered",
      adopting: "Pairing",
      adopted: "Adopted",
      active: "Connected",
      lost: "Link lost",
      beaconing: "Reconnecting",
      idle: "Waiting",
    }[device.state] || device.state
  );
}
function renderDevices() {
  const query = $("search").value.trim().toLowerCase();
  const devices = state.devices.filter((d) =>
    `${d.name} ${d.mac}`.toLowerCase().includes(query),
  );
  $("devices").replaceChildren();
  for (const d of devices) {
    const row = el("tr"),
      nameCell = el("td"),
      button = el("button", undefined, "device-name");
    const label = el("span", d.name);
    label.append(el("small", d.mac.match(/../g).join(":").toUpperCase()));
    button.append(label);
    button.addEventListener("click", () => showDevice(d.mac));
    nameCell.append(button);
    const stateCell = el("td");
    stateCell.append(
      el(
        "span",
        status(d),
        `state${["active", "adopted"].includes(d.state) ? "" : " waiting"}`,
      ),
    );
    const battery = d.properties.find((p) =>
      ["BATTERY", "BATTERY_LEVEL"].includes(p.name),
    );
    row.append(
      nameCell,
      stateCell,
      el("td", battery ? `${battery.value}${battery.unit || "%"}` : "—"),
      el("td", d.rssi == null ? "—" : `${d.rssi} dBm`),
      el("td", age(d.last_seen)),
    );
    $("devices").append(row);
  }
  $("empty").hidden = devices.length !== 0;
}
function render() {
  $("total").textContent = state.devices.length;
  $("recent").textContent = state.devices.filter(
    (d) => d.last_seen && Date.now() / 1000 - d.last_seen < 60,
  ).length;
  $("mqtt-status").textContent = state.mqtt;
  $("connection").textContent = state.demo ? "demo" : "connected";
  $("demo-banner").hidden = !state.demo;
  renderDevices();
  $("activity").replaceChildren();
  for (const item of state.activity) {
    const row = el("div", undefined, "activity-row"),
      text = el("div", item.message);
    if (item.mac) text.append(el("code", item.mac));
    row.append(
      el("time", new Date(item.time * 1000).toLocaleTimeString()),
      text,
    );
    $("activity").append(row);
  }
  if (!state.activity.length)
    $("activity").append(el("div", "No events.", "empty"));
  $("gateway-details").replaceChildren();
  for (const [label, value] of [
    ["Backend", state.demo ? "SX1302 (demo)" : "SX1302"],
    ["MAC", state.gateway_mac],
    ["MQTT", state.mqtt],
  ])
    $("gateway-details").append(el("dt", label), el("dd", value));
  if (selectedMac && $("device-dialog").open) fillDevice(selectedMac);
}
async function refresh() {
  if (refreshing || !token) return;
  refreshing = true;
  const requestToken = token;
  clearTimeout(timer);
  try {
    const snapshot = await api("state");
    if (token !== requestToken) return;
    state = snapshot;
    render();
    await pollExtraction();
  } catch (error) {
    notice(error.message);
    $("connection").textContent = "disconnected";
  } finally {
    refreshing = false;
    clearTimeout(timer);
    if (token) timer = setTimeout(refresh, 3000);
  }
}
function fillDevice(mac) {
  const d = state.devices.find((item) => item.mac === mac);
  if (!d) {
    $("device-dialog").close();
    return;
  }
  $("detail-name").textContent = d.name;
  $("detail-mac").textContent = `${d.mac} · ${status(d)}`;
  $("detail-readings").replaceChildren();
  for (const p of d.properties) {
    const item = el(
      "div",
      `${p.name.replaceAll("_", " ").toLowerCase()} · ch ${p.channel}`,
      "reading",
    );
    item.append(el("strong", `${p.value}${p.unit ? " " + p.unit : ""}`));
    $("detail-readings").append(item);
  }
  if (!d.properties.length)
    $("detail-readings").append(el("p", "No readings."));
  $("detail-actions").replaceChildren();
  const commands =
    d.state === "discovered"
      ? [["adopt", "Pair"]]
      : [
          ["locate", "Locate"],
          ["refresh", "Refresh"],
          ["reboot", "Reboot"],
        ];
  for (const [command, label] of commands) {
    const button = el("button", label, "secondary");
    button.disabled = state.demo;
    button.addEventListener("click", async () => {
      if (command === "reboot" && !confirm(`Reboot ${d.name}?`)) return;
      button.disabled = true;
      try {
        await api(`devices/${mac}/command`, { command });
        notice(`${label} queued.`);
        await refresh();
      } catch (error) {
        notice(error.message);
      } finally {
        button.disabled = state?.demo ?? true;
      }
    });
    $("detail-actions").append(button);
  }
}
function showDevice(mac) {
  selectedMac = mac;
  fillDevice(mac);
  $("device-dialog").showModal();
}
function invalidatePreview() {
  previewVersion += 1;
  previewDocument = null;
  previewJobId = null;
  $("apply-import").disabled = true;
  $("confirm-import").checked = false;
  $("preview-result").replaceChildren(el("p", "No keys selected."));
}
document
  .querySelectorAll("[data-page]")
  .forEach((button) =>
    button.addEventListener("click", () => page(button.dataset.page)),
  );
$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  token = $("token").value.trim();
  try {
    state = await api("state");
    $("token").value = "";
    $("login").hidden = true;
    $("workspace").hidden = false;
    $("lock").hidden = false;
    notice("");
    render();
    await pollExtraction();
    clearTimeout(timer);
    timer = setTimeout(refresh, 3000);
  } catch (error) {
    notice(error.message);
  }
});
$("lock").addEventListener("click", lock);
$("search").addEventListener("input", () => {
  if (state) renderDevices();
});
$("close-detail").addEventListener("click", () => $("device-dialog").close());
$("import-json").addEventListener("input", invalidatePreview);
$("import-file").addEventListener("change", async (event) => {
  invalidatePreview();
  const version = previewVersion;
  const file = event.target.files[0];
  if (!file) return;
  if (file.size > 100000) {
    notice("Maximum file size: 100 KB.");
    return;
  }
  const contents = await file.text();
  if (version === previewVersion) $("import-json").value = contents;
});
$("preview").addEventListener("click", async () => {
  invalidatePreview();
  const version = previewVersion;
  try {
    const document = JSON.parse($("import-json").value);
    const result = await api("import/preview", { document });
    if (version !== previewVersion) return;
    renderPreview(result);
    if (result.can_import) previewDocument = document;
    notice(
      result.can_import
        ? "Keys validated."
        : "Import blocked: devices already exist.",
    );
  } catch (error) {
    notice(error instanceof SyntaxError ? "Invalid JSON." : error.message);
  }
});
$("confirm-import").addEventListener("change", () => {
  $("apply-import").disabled =
    (!previewDocument && !previewJobId) ||
    !$("confirm-import").checked ||
    (state?.demo ?? true);
});
$("apply-import").addEventListener("click", async () => {
  if (!previewDocument && !previewJobId) return;
  $("apply-import").disabled = true;
  try {
    const result = await api(previewJobId ? "extraction/import" : "import", {
      ...(previewJobId
        ? { job_id: previewJobId }
        : { document: previewDocument }),
      confirmed: $("confirm-import").checked,
    });
    invalidatePreview();
    $("import-json").value = "";
    $("import-file").value = "";
    notice(
      `${result.imported} ${result.imported === 1 ? "device" : "devices"} imported. Awaiting reconnect.`,
    );
    page("devices");
    await refresh();
  } catch (error) {
    notice(error.message);
    invalidatePreview();
  }
});
$("diagnostics").addEventListener("click", () => {
  const url = URL.createObjectURL(
    new Blob([JSON.stringify(state, null, 2)], { type: "application/json" }),
  );
  const link = el("a");
  link.href = url;
  link.download = "superlink-diagnostics.json";
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});

function renderPreview(result) {
  $("preview-result").replaceChildren();
  for (const d of result.devices) {
    const row = el(
      "div",
      `${d.name}${d.conflict ? " · Already exists" : " · Ready"}`,
      `preview-row${d.conflict ? " conflict" : ""}`,
    );
    row.append(el("small", d.mac));
    $("preview-result").append(row);
  }
}
function renderExtraction() {
  const job = extractionState;
  const active = ![
    "idle",
    "ready",
    "failed",
    "expired",
    "imported",
    "recovery_required",
  ].includes(job.state);
  $("probe-source").disabled =
    active || job.state === "recovery_required" || (state?.demo ?? true);
  $("extract-keys").disabled =
    !sourceProbe ||
    !$("confirm-source").checked ||
    active ||
    job.state === "recovery_required" ||
    (state?.demo ?? true);
  $("extract-keys").hidden = job.state === "recovery_required";
  $("recover-source").hidden = job.state !== "recovery_required";
  $("review-extraction").hidden = job.state !== "ready";
  $("extraction-status").hidden = job.state === "idle";
  $("source-identity").hidden = !sourceProbe;
  $("source-credentials").hidden =
    !sourceProbe && job.state !== "recovery_required";
  const minutes = Math.ceil((job.expires_in || 0) / 60);
  const signature = JSON.stringify([
    job.state,
    job.message,
    job.host,
    job.fingerprint,
    job.count,
    job.excluded,
    minutes,
    job.recovery_command,
  ]);
  if ($("extraction-status").dataset.signature !== signature) {
    $("extraction-status").dataset.signature = signature;
    $("extraction-status").replaceChildren(
      el("strong", job.state.replaceAll("_", " ")),
      ...(["failed", "recovery_required"].includes(job.state)
        ? [el("p", job.message || "")]
        : []),
    );
    if (job.host)
      $("extraction-status").append(
        el("code", `${job.host} · ${job.fingerprint}`, "fingerprint"),
      );
    if (job.state === "ready") {
      $("extraction-status").append(
        el(
          "p",
          `${job.count} ready / ${job.excluded} excluded / expires ${minutes}m`,
        ),
      );
    }
    if (job.state === "recovery_required" && job.recovery_command) {
      $("extraction-status").append(
        el("code", job.recovery_command, "fingerprint"),
      );
    }
  }
  if (previewJobId && (job.state !== "ready" || job.id !== previewJobId))
    invalidatePreview();
}
async function pollExtraction() {
  const requestToken = token;
  const result = await api("extraction");
  if (token !== requestToken) return;
  extractionState = result;
  renderExtraction();
}
function invalidateSource() {
  sourceProbe = null;
  $("confirm-source").checked = false;
  $("source-password").value = "";
  $("source-identity").hidden = true;
  $("source-credentials").hidden =
    extractionState.state !== "recovery_required";
  renderExtraction();
}
for (const id of ["source-host", "source-port"])
  $(id).addEventListener("input", invalidateSource);
$("confirm-source").addEventListener("change", renderExtraction);
$("extraction-probe-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  invalidateSource();
  const host = $("source-host").value.trim(),
    port = Number($("source-port").value),
    requestToken = token;
  $("probe-source").disabled = true;
  try {
    const result = await api("extraction/probe", { host, port });
    if (
      token !== requestToken ||
      host !== $("source-host").value.trim() ||
      port !== Number($("source-port").value)
    )
      return;
    sourceProbe = result;
    $("source-fingerprint").textContent =
      `${result.host}:${result.port} · ${result.fingerprint}`;
    $("source-identity").hidden = false;
    $("source-credentials").hidden = false;
  } catch (error) {
    notice(error.message);
  } finally {
    renderExtraction();
  }
});
$("extract-keys").addEventListener("click", async () => {
  if (!sourceProbe || !$("confirm-source").checked) return;
  invalidatePreview();
  $("extract-keys").disabled = true;
  const requestToken = token;
  try {
    const pending = api("extraction/start", {
      probe_id: sourceProbe.probe_id,
      username: $("source-user").value.trim(),
      password: $("source-password").value,
      confirmed: $("confirm-source").checked,
    });
    $("source-password").value = "";
    const result = await pending;
    if (token !== requestToken) return;
    sourceProbe = null;
    extractionState = result;
    notice("Extracting. Keep UniFi connected.");
  } catch (error) {
    notice(error.message);
  } finally {
    renderExtraction();
  }
});
$("review-extraction").addEventListener("click", async () => {
  invalidatePreview();
  const version = previewVersion,
    jobId = extractionState.id;
  try {
    const result = await api("extraction/preview", { job_id: jobId });
    if (version !== previewVersion) return;
    renderPreview(result);
    if (result.can_import) previewJobId = jobId;
    notice(
      result.can_import
        ? "Stop the previous gateway before import."
        : "Devices already exist.",
    );
  } catch (error) {
    notice(error.message);
  }
});
$("recover-source").addEventListener("click", async () => {
  $("recover-source").disabled = true;
  const requestToken = token;
  try {
    const pending = api("extraction/recover", {
      job_id: extractionState.id,
      username: $("source-user").value.trim(),
      password: $("source-password").value,
    });
    $("source-password").value = "";
    const result = await pending;
    if (token === requestToken) extractionState = result;
  } catch (error) {
    notice(error.message);
  } finally {
    $("recover-source").disabled = false;
    renderExtraction();
  }
});
