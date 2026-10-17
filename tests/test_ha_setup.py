"""First-run setup remains reachable without modifying identities or keys."""
import importlib.util
import json
from pathlib import Path

import pytest

from superlink.bridge.config import WebConfig
from superlink.bridge.store import DeviceRecord, JsonDeviceStore
from superlink.bridge.web import ManagementServer
from tests.test_management import request, TOKEN

spec = importlib.util.spec_from_file_location('ha_startup', Path(__file__).parents[1] / 'homeassistant/superlink2mqtt/run.py')
startup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(startup)


def provision(tmp_path):
    options = tmp_path / 'options.json'
    options.write_text(json.dumps({'radio_host':'pi.invalid','radio_user':'radio','radio_port':22}))
    for name in ('radio_ed25519','known_hosts'):
        (tmp_path/name).write_text('synthetic')
    (tmp_path/'bridge.yaml').write_text('gw_mac: "020000000001"\nadopt: []\n')
    return options


def test_empty_install_serves_ingress_setup_without_creating_files(tmp_path, monkeypatch):
    options = tmp_path/'options.json'; options.write_text('{}')
    config, missing = startup.prepare(tmp_path, options)
    assert config is None and set(missing) == {'radio_host','radio_ed25519','known_hosts','bridge.yaml'}
    pending = startup.PendingSetup(); assert pending.update(tmp_path, missing)
    monkeypatch.setattr('superlink.bridge.web.is_ingress', lambda config, peer: config.ingress)
    web = ManagementServer(pending, WebConfig(TOKEN,port=0,ingress=True),management=pending)
    web.start()
    try:
        status, body, _ = request(web,'GET','/api/state',token=None)
        assert status == 200
        state = json.loads(body)
        assert state['devices'] == [] and state['setup']['missing'] == missing
        assert request(web,'GET','/onboarding.js',token=None)[0] == 200
        assert request(web,'POST','/api/devices/020000000001/command',{'command':'adopt'},token=None)[0] == 400
    finally: web.stop()
    assert sorted(p.name for p in tmp_path.iterdir()) == ['options.json']


def test_prepare_resumes_with_provisioned_files_and_preserves_them(tmp_path):
    options = provision(tmp_path)
    before = {p.name:p.read_bytes() for p in tmp_path.iterdir()}
    def mqtt(): return {'host':'broker','port':1883,'username':'test','password':'test'}
    config, missing = startup.prepare(tmp_path, options, mqtt)
    assert missing == [] and config['radio']['host'] == 'pi.invalid'
    assert config['gw_mac'] == '020000000001'
    assert config['mqtt']['password'] == 'test'
    assert {p.name:p.read_bytes() for p in tmp_path.iterdir()} == before


@pytest.mark.parametrize('content', ['not-a-mapping', 'gw_mac: [broken]', 'gw_mac: xyz', 'gw_mac: ['])
def test_invalid_identity_stays_in_setup_without_replacing_it(tmp_path, content):
    options = provision(tmp_path)
    (tmp_path/'bridge.yaml').write_text(content)
    assert startup.prepare(tmp_path, options)[1] == ['bridge.yaml']
    assert (tmp_path/'bridge.yaml').read_text() == content


def test_missing_mqtt_keeps_setup_available(tmp_path):
    options = provision(tmp_path)
    def mqtt(): raise OSError('private upstream response')
    assert startup.prepare(tmp_path, options, mqtt) == (None,['mqtt'])
    pending = startup.PendingSetup(); pending.update(tmp_path,['mqtt'])
    assert pending.snapshot()['radio']['state'] == 'configured'
    assert 'private' not in json.dumps(pending.snapshot())


def test_existing_sensor_remains_visible_and_keys_never_leave_setup(tmp_path):
    store = JsonDeviceStore(str(tmp_path/'devices.json'))
    store.save(DeviceRecord(mac=b'\x02'*6,adopted=True,primary_key=b'\xab'*32))
    before = (tmp_path/'devices.json').read_bytes()
    pending = startup.PendingSetup(); pending.update(tmp_path,['radio_host'])
    assert pending.snapshot()['devices'][0]['adopted']
    assert 'abababab' not in json.dumps(pending.snapshot())
    assert (tmp_path/'devices.json').read_bytes() == before


def test_corrupt_store_is_blocked_not_treated_as_empty(tmp_path):
    (tmp_path/'devices.json').write_text('broken')
    pending = startup.PendingSetup()
    assert not pending.update(tmp_path,[])
    assert pending.snapshot()['setup']['blocked']
    assert (tmp_path/'devices.json').read_text() == 'broken'


def test_startup_waits_then_closes_setup_server_before_launch(tmp_path, monkeypatch):
    options = provision(tmp_path)
    config, _ = startup.prepare(tmp_path, options, lambda: {'host':'broker','port':1883,'username':'test','password':'test'})
    outcomes = iter([(None,['mqtt']), (config,[])])
    monkeypatch.setattr(startup, 'prepare', lambda *_: next(outcomes))
    monkeypatch.setattr(startup.signal, 'signal', lambda *_: None)
    calls = []
    class Stop:
        def is_set(self): return False
        def wait(self, delay): calls.append(('wait',delay))
    class Server:
        def __init__(self, runtime, web, *, management):
            assert web.ingress and management.snapshot()['setup']['missing'] == ['mqtt']
        def start(self): calls.append('start')
        def stop(self): calls.append('stop')
    monkeypatch.setattr(startup.threading, 'Event', Stop)
    monkeypatch.setattr(startup, 'ManagementServer', Server)
    monkeypatch.setattr(startup.os, 'umask', lambda *_: None)
    monkeypatch.setenv('SUPERLINK_WEB_TOKEN', TOKEN)
    monkeypatch.setattr(startup.os, 'execvp', lambda *args: calls.append(('exec',args)))
    runtime_path = tmp_path/'runtime.yaml'
    startup.main(tmp_path, options, runtime_path)
    assert calls[:3] == ['start',('wait',5),'stop']
    assert calls[3][0] == 'exec'
    assert not (tmp_path/'devices.json').exists()
