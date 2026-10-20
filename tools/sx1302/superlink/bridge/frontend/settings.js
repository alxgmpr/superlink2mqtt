"use strict";

// A mountable form: the wizard and Gateway share the same fields, draft and save path.
function fieldActive(field, values) {
  return Object.entries(field.when || {}).every(([key, value]) => values[key] === value);
}
function settingsValues(fields, inputs) {
  return Object.fromEntries(fields.map((field) => [field.name, field.type === "checkbox" ? inputs[field.name].checked :
    field.type === "number" ? Number(inputs[field.name].value) : inputs[field.name].value]));
}
class SettingsForm {
  constructor(section, snapshot, save, reload, onChange) {
    this.section = section;
    this.fields = snapshot.fields[section];
    this.save = save;
    this.reload = reload;
    this.onChange = onChange;
    this.inputs = {};
    this.rows = {};
    this.dirty = false;
    this.busy = false;
    const make = (tag, text, cls) => {
      const node = document.createElement(tag);
      if (text) node.textContent = text;
      if (cls) node.className = cls;
      return node;
    };
    this.element = make("form", "", "settings-form");
    this.element.noValidate = true;
    this.element.setAttribute("aria-label", section === "radio" ? "Radio settings" : "MQTT settings");
    this.grid = make("div", "", "settings-fields");
    this.element.append(this.grid);
    const advanced = make("details", "", "settings-advanced");
    advanced.append(make("summary", "Advanced settings"));
    const advancedFields = make("div", "", "settings-fields");
    advanced.append(advancedFields);
    for (const field of this.fields) {
      const row = make("label", "", `field settings-field field-${field.name}`);
      const input = make(field.type === "select" ? "select" : "input");
      input.name = field.name;
      input.setAttribute("aria-label", field.label);
      if (field.type === "checkbox") row.classList.add("settings-toggle");
      input.id = `settings-${section}-${field.name}`;
      if (field.type === "select") {
        for (const [value, label] of field.choices) {
          const option = make("option", label); option.value = value; input.append(option);
        }
      } else {
        input.type = field.type || "text";
        input.autocomplete = field.type === "password" ? "new-password" : "off";
        input.spellcheck = false;
        if (field.min != null) input.min = field.min;
        if (field.max != null) input.max = field.max;
        if (field.type === "number") input.step = "1";
        input.maxLength = ["host_key", "password"].includes(field.name) ? 4096 : 255;
        input.placeholder = field.placeholder || "";
      }
      row.append(make("span", field.label), input);
      if (field.help) {
        const help = make("small", field.help); help.id = `${input.id}-help`;
        input.setAttribute("aria-describedby", help.id); row.append(help);
      }
      this.inputs[field.name] = input;
      this.rows[field.name] = row;
      (field.advanced ? advancedFields : this.grid).append(row);
      input.addEventListener("input", () => {
        this.dirty = true;
        this.status.textContent = "Unsaved changes";
        this.status.classList.remove("setup-error");
        this.update();
        this.onChange();
      });
    }
    if (advancedFields.children.length) this.element.append(advanced);
    this.keyDetails = make("details", "", "settings-key");
    this.keyDetails.append(make("summary", "Authorize this app on your Pi"),
      make("p", "Add this public key to your Pi account’s authorized_keys, restricted to the radio service."));
    const keyLabel = make("label", "Public key", "field");
    this.key = make("textarea"); this.key.readOnly = true; this.key.rows = 3;
    this.key.setAttribute("aria-label", "App public key");
    keyLabel.append(this.key);
    const copy = make("button", "Copy public key"); copy.type = "button";
    copy.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(this.key.value); copy.textContent = "Copied"; }
      catch { this.key.select(); copy.textContent = "Key selected — copy to clipboard"; }
    });
    const guide = make("a", "Radio installation guide ↗");
    guide.href = "https://github.com/alxgmpr/superlink2mqtt/blob/codex/ha-setup-recovery/homeassistant/superlink2mqtt/DOCS.md#install-and-provision-the-radio";
    guide.target = "_blank"; guide.rel = "noopener noreferrer";
    this.keyDetails.append(keyLabel, copy, guide);
    if (section === "radio") this.element.append(this.keyDetails);
    this.status = make("p", "", "settings-message"); this.status.setAttribute("role", "status");
    this.submit = make("button", section === "radio" ? "Save radio settings" : "Save MQTT settings", "filled");
    this.submit.type = "submit";
    this.reloadButton = make("button", "Reload saved settings"); this.reloadButton.type = "button";
    this.reloadButton.hidden = true;
    this.reloadButton.addEventListener("click", async () => {
      try {
        const data = await this.reload(); this.hydrate(data, true);
        this.status.textContent = "Saved settings loaded.";
      } catch (error) { this.status.textContent = error.message; }
      this.onChange();
    });
    const actions = make("div", "", "settings-actions"); actions.append(this.submit, this.reloadButton);
    this.element.append(this.status, actions);
    this.element.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (this.busy) return;
      for (const input of Object.values(this.inputs)) {
        if (!input.checkValidity()) { const details = input.closest("details"); if (details) details.open = true; }
      }
      if (!this.element.reportValidity()) return;
      const hadKey = !!this.key.value;
      this.busy = true; this.update(); this.onChange();
      this.status.textContent = "Saving…";
      this.status.classList.remove("setup-error");
      try {
        const data = await this.save({section: this.section, revision: this.revision, values: this.values()});
        this.hydrate(data, true);
        this.status.textContent = data.changed ? "Saved. Reconnecting with these settings…" : "Settings are already saved.";
        if (!hadKey && data.public_key && this.section === "radio") this.keyDetails.open = true;
      } catch (error) {
        this.status.textContent = error.message;
        this.status.classList.add("setup-error");
        this.reloadButton.hidden = false;
      } finally {
        this.busy = false; this.update(); this.onChange();
      }
    });
    this.hydrate(snapshot, true);
  }
  values() { return settingsValues(this.fields, this.inputs); }
  hydrate(snapshot, force = false) {
    if (this.dirty && !force) return;
    this.revision = snapshot.revision;
    for (const field of this.fields) {
      const value = snapshot.values[this.section][field.name];
      if (field.type === "checkbox") this.inputs[field.name].checked = !!value;
      else this.inputs[field.name].value = value ?? "";
      if (field.type === "password") this.inputs[field.name].placeholder = snapshot.password_set ? "Saved · leave blank to keep" : "Optional";
    }
    this.key.value = snapshot.public_key || "";
    this.keyDetails.hidden = !snapshot.public_key;
    this.dirty = false;
    this.reloadButton.hidden = true;
    this.update();
  }
  update() {
    const values = this.values();
    for (const field of this.fields) {
      const active = fieldActive(field, values);
      this.rows[field.name].hidden = !active;
      this.inputs[field.name].disabled = this.busy || !active;
      this.inputs[field.name].required = active && !!field.required;
    }
    this.submit.disabled = this.busy;
    this.reloadButton.disabled = this.busy;
  }
  mount(container) {
    if (this.element.parentElement !== container) container.append(this.element);
  }
}
if (typeof module !== "undefined") module.exports = { fieldActive, settingsValues };
