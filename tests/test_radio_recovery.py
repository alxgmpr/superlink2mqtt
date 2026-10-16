"""Radio outages keep management alive without replaying transmissions."""
from concurrent.futures import Future
import threading
import time

import pytest

from superlink.bridge.events import DeviceStateEvent, Locate
from superlink.bridge.management import Management
from superlink.bridge.runtime import BridgeRuntime
from superlink.bridge.store import InMemoryDeviceStore, DeviceRecord
from superlink.bridge.config import RuntimeConfig, WebConfig, MqttConfig
from superlink.bridge.web import ManagementServer
from superlink.bridge.mqtt import MqttBridge
from superlink.radio_remote import RemoteRadioConfig
from tests.support.fake_hal import FakeHal
from tests.support.fake_mqtt import FakeMqttClient
from tests.test_management import request, TOKEN, bundle

MAC = bytes.fromhex('020000000004')


def runtime(hal=None, store=None):
    config = RuntimeConfig(gw_mac=bytes.fromhex('020000000001'),
        radio=RemoteRadioConfig('unreachable.invalid', 'radio', 'key', 'hosts'))
    return BridgeRuntime(config, hal or FakeHal(), store or InMemoryDeviceStore())


def test_http_works_while_radio_connect_is_blocked():
    entered, release = threading.Event(), threading.Event()
    class SlowRadio(FakeHal):
        def start(self):
            entered.set()
            assert release.wait(5)
            raise ConnectionError('private remote diagnostic')
    rt = runtime(SlowRadio())
    web = ManagementServer(rt, WebConfig(TOKEN, port=0))
    worker = threading.Thread(target=rt.run)
    web.start()
    worker.start()
    try:
        assert entered.wait(1)
        start = time.monotonic()
        status, body, _ = request(web, 'GET', '/api/state')
        assert status == 200 and b'connecting' in body
        assert time.monotonic() - start < 1
        assert request(web, 'POST', '/api/import/preview', {'document': bundle()})[0] == 200
        assert request(web, 'GET', '/api/state', token=None)[0] == 401
        assert request(web, 'POST', f'/api/devices/{MAC.hex()}/command', {'command': 'locate'})[0] == 400
    finally:
        rt.stop()
        release.set()
        worker.join(2)
        web.stop()
    assert not worker.is_alive()


def test_failure_backoff_is_bounded_and_does_not_rewrite_store_every_retry():
    store = InMemoryDeviceStore()
    record = DeviceRecord(mac=MAC, adopted=True, primary_key=b'1' * 32, fallback_key=b'2' * 32)
    store.save(record)
    rt = runtime(store=store)
    management = Management(rt)
    for index, delay in enumerate([1, 2, 4, 8, 16, 30, 30]):
        rt._radio_failed(100 + index, OSError('secret-password'))
        assert rt._radio_retry_at == 100 + index + delay
        assert 'secret-password' not in str(management.snapshot())
    assert len(management.activity) == 1
    assert store.load_all()[0].primary_key == record.primary_key
    assert management.snapshot()['devices'][0]['state'] == 'lost'


def test_reconnect_resets_sessions_and_drops_uncertain_commands():
    store = InMemoryDeviceStore()
    store.save(DeviceRecord(mac=MAC, adopted=True, primary_key=b'1' * 32, fallback_key=b'2' * 32))
    rt = runtime(store=store)
    rt.radio_state = 'connected'
    old_session = rt._sessions[MAC]
    rt.core.submit(Locate(MAC))
    rt.submit_action(Locate(MAC))
    client = FakeMqttClient()
    mqtt = MqttBridge(MqttConfig('broker'), rt, client)
    mqtt.start()
    mqtt.on_event(DeviceStateEvent(MAC, 'active'))
    rt._radio_failed(10, ConnectionError())
    assert rt._sessions[MAC] is not old_session
    assert rt._action_queue.empty()
    assert rt.hal.sent == []
    assert client.find(f'superlink/{MAC.hex()}/availability') == 'offline'
    client.fire_on_connect()
    assert client.find(f'superlink/{MAC.hex()}/availability') == 'offline'
    rt.submit_action(Locate(MAC))
    assert rt._action_queue.empty()
    with pytest.raises(ValueError, match='Radio unavailable'):
        Management(rt).command(MAC.hex(), 'locate')
    rt._radio_start = Future()
    rt._radio_start.set_result(None)
    rt._connect_radio(11)
    assert rt.radio_state == 'connected' and rt.radio_error is None
    assert rt._radio_retry_delay == 1
    assert rt.hal.sent == []
    assert rt.store.load_all()[0].primary_key == b'1' * 32


def test_receive_loss_recovers_in_same_runtime():
    class FlakyRadio(FakeHal):
        starts = 0
        def start(self):
            self.starts += 1
            if self.starts == 1:
                raise OSError('offline at boot')
        def receive(self):
            if self.starts == 2:
                raise ConnectionError('disconnected after connect')
            rt.stop()
            return []
    hal = FlakyRadio()
    rt = runtime(hal)
    original = rt._radio_failed
    def fail(now, error):
        original(now, error)
        rt._radio_retry_at = now  # bounded test, production backoff tested above
    rt._radio_failed = fail
    rt.run()
    assert hal.starts == 3
    assert rt.radio_state == 'connected'
    assert hal.stopped


def test_mqtt_start_uses_async_connect_and_honors_tls():
    class OfflineBroker(FakeMqttClient):
        def connect(self, *args):
            raise AssertionError('synchronous connect prevents UI startup')
        def connect_async(self, *args):
            self.pending = args
    rt = runtime()
    client = OfflineBroker()
    bridge = MqttBridge(MqttConfig('offline.invalid', tls=True), rt, client)
    bridge.start()
    assert client.loop_running and client.tls
    assert client.find('superlink/bridge/availability') is None


def test_bad_mqtt_action_cannot_kill_polling_or_block_next_command():
    from superlink.bridge.events import SetProperty
    store = InMemoryDeviceStore()
    store.save(DeviceRecord(mac=MAC, adopted=True))
    rt = runtime(store=store)
    rt.radio_state = 'connected'
    rt.submit_action(SetProperty(MAC, 'NOT_A_PROPERTY', 1))
    rt.submit_action(Locate(MAC))
    queued = []
    rt._sessions[MAC].queue_body = queued.append
    rt.poll_once(100)
    assert len(queued) == 1
    assert rt._action_queue.empty()
