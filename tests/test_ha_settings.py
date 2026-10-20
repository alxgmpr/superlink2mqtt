"""Actual persistence and failure paths for reusable HA settings forms."""
import json
import os
import stat
from pathlib import Path

import paramiko
import pytest

from superlink.bridge.ha_settings import HASettings, prepare
from superlink.bridge.config import WebConfig
from superlink.bridge.web import ManagementServer
from superlink.bridge.store import DeviceRecord, JsonDeviceStore
from tests.test_ha_setup import startup
from tests.test_management import request, TOKEN


@pytest.fixture
def settings(tmp_path):
    options = tmp_path / 'options.json'
    options.write_text('{}')
    return HASettings(tmp_path, options)


@pytest.fixture
def radio():
    key = paramiko.RSAKey.generate(1024)
    return {'host':'pi.invalid', 'username':'radio', 'port':22,
            'host_key':f'{key.get_name()} {key.get_base64()}'}


def save(settings, section, values, revision=None):
    return settings.save({'section':section, 'values':values, 'revision':revision or settings.revision()})


def test_fresh_setup_generates_once_then_prepares_with_custom_mqtt(settings, radio):
    result = save(settings, 'radio', radio)
    assert result['changed'] and result['public_key'].startswith('ssh-ed25519 ')
    key = settings.root / 'radio_ed25519'
    private = key.read_bytes()
    identity = settings.read()['gateway_mac']
    assert int(identity[:2],16) & 3 == 2
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert stat.S_IMODE(settings.path.stat().st_mode) == 0o600
    assert not save(settings,'radio',radio)['changed']
    assert key.read_bytes() == private and settings.read()['gateway_mac'] == identity
    save(settings, 'mqtt', {'mode':'custom','host':'broker.invalid','port':8883,'tls':True})
    config, missing = prepare(settings.root,settings.options_path,lambda: pytest.fail('must not use Supervisor'))
    assert not missing and config['gw_mac'] == identity
    assert config['mqtt']['host'] == 'broker.invalid' and config['mqtt']['tls']
    keys = paramiko.HostKeys(config['radio']['known_hosts'])
    assert keys.lookup('pi.invalid')
    assert not (settings.root/'devices.json').exists()


def test_preserves_existing_config_store_identity_and_ssh_keys(settings, radio):
    save(settings,'radio',radio)
    (settings.root/'bridge.yaml').write_text('gw_mac: "020000000099"\nadopt: []\nmqtt:\n  base_topic: original\n')
    store = JsonDeviceStore(str(settings.root/'devices.json'))
    store.save(DeviceRecord(mac=b'\x02'*6, adopted=True, primary_key=b'\xab'*32))
    paths = [settings.root/name for name in ('radio_ed25519','bridge.yaml','devices.json')]
    before = {p:p.read_bytes() for p in paths}
    save(settings,'radio',{**radio,'port':2222})
    save(settings,'mqtt',{'mode':'custom','host':'broker.invalid'})
    cfg, missing = prepare(settings.root,settings.options_path)
    assert not missing and cfg['gw_mac'] == '020000000099'
    assert cfg['mqtt']['base_topic'] == 'original'
    assert {p:p.read_bytes() for p in paths} == before
    assert paramiko.HostKeys(cfg['radio']['known_hosts']).lookup('[pi.invalid]:2222')


def test_password_never_returned_and_blank_preserves_saved_secret(settings):
    secret = 'synthetic-' + os.urandom(8).hex()
    result = save(settings,'mqtt',{'mode':'custom','host':'broker.invalid','password':secret})
    assert result['password_set'] and secret not in json.dumps(result)
    save(settings,'mqtt',{'password':''})
    assert settings.read()['mqtt']['password'] == secret
    assert secret not in json.dumps(settings.snapshot())


@pytest.mark.parametrize('patch', [{'port':0},{'port':True},{'host':'ssh://bad'},{'host':'bad\nhost'},
                                    {'username':'-command'}, {'host_key':'not a key'}, {'key_file':'/etc/passwd'}])
def test_invalid_radio_is_rejected_without_writes(settings, radio, patch):
    before = sorted(settings.root.iterdir())
    with pytest.raises(ValueError): save(settings,'radio',{**radio,**patch})
    assert sorted(settings.root.iterdir()) == before


def test_stale_revision_rejects_overwrite(settings, radio):
    old = settings.revision()
    save(settings,'radio',radio)
    before = settings.path.read_bytes()
    with pytest.raises(ValueError,match='changed elsewhere'):
        save(settings,'mqtt',{'mode':'automatic'},old)
    assert settings.path.read_bytes() == before


def test_corrupt_store_or_missing_identity_cannot_generate_replacement(settings, radio):
    (settings.root/'devices.json').write_text('broken')
    with pytest.raises(ValueError): save(settings,'radio',radio)
    assert not (settings.root/'radio_ed25519').exists()
    (settings.root/'devices.json').unlink()
    JsonDeviceStore(str(settings.root/'devices.json')).save(DeviceRecord(mac=b'\x02'*6,adopted=True))
    with pytest.raises(ValueError,match='original gateway'):
        save(settings,'radio',radio)
    assert not settings.path.exists()


def test_atomic_replace_failure_preserves_last_settings(settings, monkeypatch):
    save(settings,'mqtt',{'mode':'automatic'})
    before = settings.path.read_bytes()
    def fail(source, target):
        if Path(target) == settings.path: raise OSError('disk error')
        original(source,target)
    original = os.replace
    monkeypatch.setattr(os,'replace',fail)
    with pytest.raises(OSError): save(settings,'mqtt',{'base_topic':'changed'})
    assert settings.path.read_bytes() == before
    assert not list(settings.root.glob('.settings-*'))


def test_http_settings_auth_validation_and_persistence(settings, radio):
    pending = startup.PendingSetup(); pending.update(settings.root,['radio_host'])
    web = ManagementServer(pending,WebConfig(TOKEN,port=0),management=pending,settings=settings)
    web.start()
    try:
        assert request(web,'GET','/api/settings',token=None)[0] == 401
        assert request(web,'POST','/api/settings',{},token=None)[0] == 401
        status, body, _ = request(web,'GET','/api/settings')
        snapshot = json.loads(body); assert status == 200
        body = {'section':'radio','values':radio,'revision':snapshot['revision']}
        status, data, _ = request(web,'POST','/api/settings',body)
        assert status == 200 and json.loads(data)['changed']
        assert request(web,'POST','/api/settings',body)[0] == 400
        assert request(web,'GET','/settings.js')[0] == 200
    finally: web.stop()


@pytest.mark.parametrize("stale", [False, True])
def test_runtime_save_stops_services_before_reexec(settings, monkeypatch, stale):
    from superlink.bridge import runtime as module
    from superlink.bridge.config import RuntimeConfig
    calls = []
    class Runtime:
        default_mac = None
        def stop(self): calls.append('stop-runtime')
        def run(self):
            save(web_settings[0],'mqtt',{'mode':'automatic'})
    class Web:
        def __init__(self, runtime, config, *, settings): web_settings.append(settings)
        def start(self): calls.append('start-web')
        def stop(self): calls.append('stop-web')
    class MQTT:
        def stop(self): calls.append('stop-mqtt')
    web_settings = []
    monkeypatch.setenv('SUPERLINK_HA_ROOT',str(settings.root))
    monkeypatch.setenv('SUPERLINK_HA_OPTIONS',str(settings.options_path))
    monkeypatch.setenv('SUPERLINK_HA_LAUNCHER','/app/run.py')
    monkeypatch.setenv('SUPERLINK_HA_REVISION','stale' if stale else settings.revision())
    monkeypatch.setattr(module.RuntimeConfig,'load',lambda _: RuntimeConfig(gw_mac=b'\x02'*6,web=WebConfig(TOKEN)))
    monkeypatch.setattr(module,'build_runtime',lambda *a: Runtime())
    monkeypatch.setattr('superlink.hal.SX1302',lambda **kw: object())
    monkeypatch.setattr(module,'start_control_socket_if_configured',lambda *a: None)
    monkeypatch.setattr(module,'start_mqtt_if_configured',lambda *a: MQTT())
    monkeypatch.setattr('superlink.bridge.web.ManagementServer',Web)
    monkeypatch.setattr(module.os,'execv',lambda *a: calls.append('reexec'))
    module.main([])
    assert calls == (['reexec'] if stale else ['start-web','stop-runtime','stop-web','stop-mqtt','reexec'])


def test_saved_password_is_not_forwarded_to_a_different_broker(settings):
    save(settings,'mqtt',{'mode':'custom','host':'one.invalid','password':'test'})
    with pytest.raises(ValueError,match='different broker'):
        save(settings,'mqtt',{'host':'two.invalid','password':''})
    assert settings.read()['mqtt']['host'] == 'one.invalid'
    save(settings,'mqtt',{'host':'two.invalid','clear_password':True})
    assert settings.read()['mqtt']['password'] == ''


def test_settings_read_does_not_create_keys_or_identity(settings):
    before = sorted(settings.root.iterdir())
    state = settings.snapshot()
    assert state['public_key'] == ''
    assert sorted(settings.root.iterdir()) == before
