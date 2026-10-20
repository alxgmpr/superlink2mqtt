const { test } = require('node:test');
const assert = require('node:assert/strict');
const { fieldActive, settingsValues } = require('../tools/sx1302/superlink/bridge/frontend/settings.js');
test('embedded settings serialize ports and TLS using API types', () => {
  assert.deepEqual(settingsValues([{name:'host'},{name:'port',type:'number'},{name:'tls',type:'checkbox'},{name:'password',type:'password'}],
    {host:{value:'broker.invalid'},port:{value:'8883'},tls:{checked:true},password:{value:''}}),
    {host:'broker.invalid',port:8883,tls:true,password:''});
});
test('automatic MQTT hides custom-only required fields', () => {
  const field = {name:'host',required:true,when:{mode:'custom'}};
  assert.equal(fieldActive(field,{mode:'automatic'}),false);
  assert.equal(fieldActive(field,{mode:'custom'}),true);
  assert.equal(fieldActive({name:'mode'},{mode:'automatic'}),true);
});
