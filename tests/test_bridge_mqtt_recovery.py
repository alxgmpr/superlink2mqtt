import json

from superlink.bridge.config import MqttConfig, RuntimeConfig
from superlink.bridge.events import ButtonPressed, DeviceRemoved, DeviceStateEvent, PropertyEvent
from superlink.bridge.mqtt import MqttBridge
from superlink.bridge.runtime import BridgeRuntime
from superlink.bridge.store import DeviceRecord, InMemoryDeviceStore
from tests.support.fake_hal import FakeHal
from tests.support.fake_mqtt import FakeMqttClient

MAC = bytes.fromhex('9041b22e9a53')
BASE = 'test/superlink'


def make_bridge():
    store = InMemoryDeviceStore()
    store.save(DeviceRecord(mac=MAC, adopted=True))
    rt = BridgeRuntime(RuntimeConfig(gw_mac=b'123456'), FakeHal(), store)
    client = FakeMqttClient()
    config = MqttConfig(host='fake', base_topic=BASE, tls=True)
    return rt, client, MqttBridge(config, rt, client)


def battery(value):
    return PropertyEvent(mac=MAC, property_id=3, name='BATTERY', channel=0,
                         raw=bytes([value]), value=value, unit='%', decoded=True)


def test_start_waits_for_broker_without_blocking_radio():
    rt, client, bridge = make_bridge()
    client.auto_connect = False
    bridge.start()
    rt.poll_once(1.0)
    assert client.loop_running and client.async_connect_called
    assert client.published == []
    assert client.tls_enabled
    bridge.on_event(battery(90))
    bridge.on_event(battery(80))
    assert client.published == []
    client.fire_on_connect()
    assert client.find(f'{BASE}/bridge/availability') == 'online'
    assert client.find(f'{BASE}/{MAC.hex()}/availability') == 'offline'
    assert client.find(f'{BASE}/{MAC.hex()}/BATTERY') == '80'


def test_reconnect_replays_latest_retained_state_but_never_button_events():
    _, client, bridge = make_bridge()
    bridge.start()
    bridge.on_event(DeviceStateEvent(mac=MAC, state='active'))
    bridge.on_event(battery(90))
    bridge.on_event(ButtonPressed(mac=MAC, property_id=19, name='BUTTON_PRESSED', value=1))
    client.fire_on_disconnect()
    bridge.on_event(battery(80))
    bridge.on_event(DeviceStateEvent(mac=MAC, state='lost'))
    client.published.clear()  # simulate a broker with no retained data
    client.fire_on_connect()
    assert client.find(f'{BASE}/{MAC.hex()}/BATTERY') == '80'
    assert client.find(f'{BASE}/{MAC.hex()}/availability') == 'offline'
    assert client.find(f'{BASE}/bridge/availability') == 'online'
    assert client.find(f'homeassistant/sensor/{MAC.hex()}_BATTERY/config')
    assert client.find(f'{BASE}/{MAC.hex()}/BUTTON') is None
    assert f'{BASE}/+/+/set' in client.subscriptions


def test_removal_while_offline_replays_tombstones_not_stale_entities():
    _, client, bridge = make_bridge()
    bridge.start()
    bridge.on_event(battery(90))
    client.fire_on_disconnect()
    bridge.on_event(DeviceRemoved(mac=MAC, reason='factory_reset'))
    client.published.clear()
    client.fire_on_connect()
    assert client.find(f'homeassistant/sensor/{MAC.hex()}_BATTERY/config') == ''
    assert client.find(f'{BASE}/{MAC.hex()}/BATTERY') == ''


def test_failed_connack_does_not_publish_online_or_subscribe():
    _, client, bridge = make_bridge()
    bridge._paho_on_connect(client, None, {}, 5)
    assert client.published == []
    assert client.subscriptions == []


def test_nested_base_topic_routes_commands():
    rt, _, bridge = make_bridge()
    bridge._on_message(f'{BASE}/{MAC.hex()}/LED_ENABLED/set', 'OFF')
    rt.poll_once(1.0)
    assert rt._sessions[MAC]._pending_bodies == [bytes.fromhex('0e010e0000')]


def test_discovery_requires_both_bridge_and_sensor_online():
    _, client, bridge = make_bridge()
    bridge.start()
    bridge.on_event(DeviceStateEvent(mac=MAC, state='active'))
    bridge.on_event(battery(90))
    for topic in (f'homeassistant/sensor/{MAC.hex()}_BATTERY/config',
                  f'homeassistant/button/{MAC.hex()}_locate/config'):
        payload = json.loads(client.find(topic))
        assert payload['availability_mode'] == 'all'
        assert {item['topic'] for item in payload['availability']} == {
            f'{BASE}/bridge/availability', f'{BASE}/{MAC.hex()}/availability'}
        assert 'availability_topic' not in payload


def test_shutdown_confirms_offline_before_disconnect():
    _, client, bridge = make_bridge()
    bridge.start()
    bridge.stop()
    assert client.find(f'{BASE}/bridge/availability') == 'offline'
    assert client.publish_waited and not client.connected and not client.loop_running
    before = list(client.published)
    client.fire_on_connect()  # racing reconnect must not undo shutdown
    assert client.published == before


def test_reconnect_and_device_removal_cannot_interleave_stale_replay():
    import threading
    _, client, bridge = make_bridge()
    bridge.start()
    bridge.on_event(battery(90))
    entered = threading.Event()
    release = threading.Event()
    removed = threading.Event()
    original_publish = client.publish
    def pause_replay(topic, payload=None, **kwargs):
        if topic.endswith('/BATTERY') and payload == '90':
            entered.set()
            assert release.wait(3)
        return original_publish(topic, payload, **kwargs)
    client.publish = pause_replay
    reconnect = threading.Thread(target=client.fire_on_connect)
    def remove():
        bridge.on_event(DeviceRemoved(mac=MAC, reason='factory_reset'))
        removed.set()
    removal = threading.Thread(target=remove)
    reconnect.start()
    try:
        assert entered.wait(3)
        removal.start()
        assert not removed.wait(0.05)
    finally:
        release.set()
        reconnect.join(3)
        if removal.ident is not None:
            removal.join(3)
    assert removed.is_set()
    assert client.find(f'{BASE}/{MAC.hex()}/BATTERY') == ''
