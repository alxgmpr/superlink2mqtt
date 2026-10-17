"use strict";

// Keep onboarding decisions independent of the DOM so reconnects and delayed
// pairing acknowledgements follow the same rules in the UI and tests.
class SetupFlow {
  constructor() {
    this.index = 0;
    this.opened = false;
    this.prompted = false;
    this.selectedMac = null;
    this.pendingMac = null;
  }
  observe(snapshot) {
    if (!this.prompted && !snapshot.demo && !snapshot.setup?.blocked && !snapshot.devices.some((d) => d.adopted)) {
      this.prompted = true;
      this.opened = true;
    }
    if (this.opened && this.index === 2 && !this.selectedMac) {
      this.selectedMac = snapshot.devices.find((d) => d.has_session || d.adopted)?.mac || null;
    }
    const device = snapshot.devices.find((d) => d.mac === this.selectedMac);
    if (this.opened && this.index === 2 && device?.adopted) this.index = 3;
  }
  dismiss() { this.opened = false; this.prompted = true; }
  ready(snapshot) {
    if (this.index === 0) return snapshot.radio?.state === "connected" ||
      (snapshot.radio?.state === "configured" && snapshot.setup?.missing?.length === 1 && snapshot.setup.missing[0] === "mqtt");
    if (this.index === 1) return snapshot.mqtt === "connected";
    if (this.index === 2) return false; // Pair on a discovered sensor, then wait for adoption.
    return snapshot.radio?.state === "connected" && snapshot.mqtt === "connected" && snapshot.devices.some((d) => d.mac === this.selectedMac && d.adopted &&
      d.state === "active" && d.properties.length > 0);
  }
}
if (typeof module !== "undefined") module.exports = { SetupFlow };
