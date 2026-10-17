const assert = require('node:assert/strict');
const { test } = require('node:test');
const { SetupFlow } = require('../tools/sx1302/superlink/bridge/frontend/onboarding.js');
const snapshot = (devices = [], extra = {}) => ({demo: false, devices, radio: {state: 'connected'}, mqtt: 'connected', ...extra});
const sensor = (extra = {}) => ({mac: '020000000001', adopted: false, has_session: false, state: 'discovered', properties: [], ...extra});
test('empty install and discovered sensor open onboarding, saved sensor and demo do not', () => {
  for (const devices of [[], [sensor()]]) {
    const flow = new SetupFlow(); flow.observe(snapshot(devices)); assert.equal(flow.opened, true);
  }
  for (const data of [snapshot([sensor({adopted:true})]), snapshot([], {demo:true}), snapshot([], {setup:{blocked:true}})]) {
    const flow = new SetupFlow(); flow.observe(data); assert.equal(flow.opened, false);
  }
});
test('dismissal survives polling, including a radio disconnect', () => {
  const flow = new SetupFlow(); flow.observe(snapshot()); flow.dismiss();
  flow.observe(snapshot([], {radio:{state:'disconnected'}}));
  assert.equal(flow.opened, false);
});
test('connection steps require live readiness; bootstrap can proceed to missing MQTT', () => {
  const flow = new SetupFlow();
  assert.equal(flow.ready(snapshot([], {radio:{state:'disconnected'}})), false);
  assert.equal(flow.ready(snapshot([], {radio:{state:'configured'},setup:{missing:['mqtt']}})), true);
  assert.equal(flow.ready(snapshot([], {radio:{state:'configured'},setup:{missing:['known_hosts']}})), false);
  flow.index = 1;
  assert.equal(flow.ready(snapshot([], {mqtt:'disabled'})), false);
  assert.equal(flow.ready(snapshot()), true);
});
test('queued or active-but-uncommitted pairing cannot advance or claim completion', () => {
  const flow = new SetupFlow(); flow.index = 2; flow.opened = true; flow.prompted = true;
  flow.selectedMac = sensor().mac;
  flow.observe(snapshot([sensor({has_session:true,state:'active'})]));
  assert.equal(flow.index,2); assert.equal(flow.ready(snapshot()),false);
  flow.observe(snapshot([sensor({has_session:true,adopted:true})]));
  assert.equal(flow.index,3);
  assert.equal(flow.ready(snapshot([sensor({adopted:true,state:'active'})])),false);
  assert.equal(flow.ready(snapshot([sensor({adopted:true,state:'active',properties:[{value:1}]})])),true);
  assert.equal(flow.ready(snapshot([sensor({adopted:true,state:'lost',properties:[{value:1}]})])),false);
  assert.equal(flow.ready(snapshot([sensor({adopted:true,state:'active',properties:[{value:1}]})], {mqtt:'disconnected'})),false);
  assert.equal(flow.ready(snapshot([sensor({adopted:true,state:'active',properties:[{value:1}]})], {radio:{state:'disconnected'}})),false);
});
test('allowlisted auto-pairing is followed through to the reading step', () => {
  const flow = new SetupFlow(); flow.observe(snapshot()); flow.index = 2;
  flow.observe(snapshot([sensor({has_session:true})]));
  assert.equal(flow.selectedMac,sensor().mac);
  flow.observe(snapshot([sensor({adopted:true})]));
  assert.equal(flow.index,3); assert.equal(flow.opened,true);
});
