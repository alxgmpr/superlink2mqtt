"""Real paho/Mosquitto recovery tests; never connect to the configured home broker."""
import shutil
import socket
import subprocess
import threading
import time

import pytest
import paho.mqtt.client as mqtt

from superlink.bridge.config import MqttConfig, RuntimeConfig
from superlink.bridge.events import DeviceStateEvent, PropertyEvent
from superlink.bridge.mqtt import MqttBridge
from superlink.bridge.runtime import BridgeRuntime
from superlink.bridge.store import DeviceRecord, InMemoryDeviceStore
from tests.support.fake_hal import FakeHal

pytestmark = pytest.mark.mqtt_broker
MAC = bytes.fromhex('9041b22e9a53')
BASE = 'integration/superlink'


def wait_until(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    assert predicate(), 'condition did not become true before timeout'


class Broker:
    def __init__(self, executable, directory):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            self.port = sock.getsockname()[1]
        self.config = directory / 'mosquitto.conf'
        self.config.write_text(f'listener {self.port} 127.0.0.1\n'
                               'allow_anonymous true\npersistence false\n')
        self.executable = executable
        self.process = None
        self.log = (directory / 'mosquitto.log').open('w')

    def start(self):
        self.process = subprocess.Popen([self.executable, '-c', str(self.config)],
                                        stdout=self.log, stderr=subprocess.STDOUT)
        def listening():
            assert self.process.poll() is None, 'private broker exited unexpectedly'
            try:
                with socket.create_connection(('127.0.0.1', self.port), timeout=0.1):
                    return True
            except OSError:
                return False
        wait_until(listening)

    def stop(self):
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            finally:
                if self.process.poll() is None:
                    self.process.kill()
                    self.process.wait(timeout=5)
            self.process = None


@pytest.fixture
def broker(tmp_path):
    executable = shutil.which('mosquitto')
    if executable is None:
        pytest.skip('install mosquitto to run real broker tests (required in CI)')
    broker = Broker(executable, tmp_path)
    try:
        yield broker
    finally:
        broker.stop()
        broker.log.close()


class Observer:
    def __init__(self, port):
        self.messages = {}
        self.ready = threading.Event()
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self.client.on_connect = lambda client, *_: client.subscribe('#', qos=1)
        self.client.on_subscribe = lambda *_: self.ready.set()
        self.client.on_message = self.on_message
        self.client.connect_async('127.0.0.1', port)
        self.client.loop_start()

    def on_message(self, client, userdata, message):
        self.messages[message.topic] = message.payload.decode()

    def stop(self):
        self.client.disconnect()
        self.client.loop_stop()


def test_late_broker_restart_and_command_recovery(broker):
    store = InMemoryDeviceStore()
    store.save(DeviceRecord(mac=MAC, adopted=True))
    rt = BridgeRuntime(RuntimeConfig(gw_mac=b'123456'), FakeHal(), store)
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    bridge = MqttBridge(MqttConfig(host='127.0.0.1', port=broker.port,
                                   base_topic=BASE), rt, client)
    observer = None
    try:
        started = time.monotonic()
        bridge.start()  # no listening broker yet
        assert time.monotonic() - started < 1.0
        rt.poll_once(1.0)
        bridge.on_event(DeviceStateEvent(mac=MAC, state='active'))
        bridge.on_event(PropertyEvent(mac=MAC, property_id=3, name='BATTERY',
                                     channel=0, raw=b'\x5a', value=90,
                                     unit='%', decoded=True))
        broker.start()
        observer = Observer(broker.port)
        assert observer.ready.wait(10)
        state = f'{BASE}/{MAC.hex()}/BATTERY'
        availability = f'{BASE}/bridge/availability'
        discovery = f'homeassistant/sensor/{MAC.hex()}_BATTERY/config'
        wait_until(lambda: observer.messages.get(state) == '90'
                   and observer.messages.get(availability) == 'online'
                   and discovery in observer.messages)
        observer.stop()
        observer = None
        broker.stop()  # persistence=false: the replacement broker is empty
        wait_until(lambda: not bridge._connected)
        bridge.on_event(PropertyEvent(mac=MAC, property_id=3, name='BATTERY',
                                     channel=0, raw=b'\x50', value=80,
                                     unit='%', decoded=True))
        bridge.on_event(DeviceStateEvent(mac=MAC, state='lost'))
        broker.start()
        observer = Observer(broker.port)
        assert observer.ready.wait(10)
        wait_until(lambda: observer.messages.get(state) == '80'
                   and observer.messages.get(availability) == 'online'
                   and observer.messages.get(f'{BASE}/{MAC.hex()}/availability') == 'offline'
                   and discovery in observer.messages)
        # Commands still reach the running runtime after resubscription.
        for name, value in [('NOT_A_PROPERTY', '1'), ('LED_ENABLED', 'ON')]:
            observer.client.publish(f'{BASE}/{MAC.hex()}/{name}/set', value,
                                    qos=1).wait_for_publish(timeout=5)
        wait_until(lambda: rt._action_queue.qsize() == 2)
        rt.poll_once(2.0)
        assert rt._sessions[MAC]._pending_bodies == [bytes.fromhex('0e010e0001')]
        bridge.stop()
        wait_until(lambda: observer.messages.get(availability) == 'offline')
    finally:
        bridge.stop()
        if observer is not None:
            observer.stop()


def test_last_will_marks_bridge_offline_after_process_death(broker):
    import os
    from pathlib import Path
    import sys
    broker.start()
    observer = Observer(broker.port)
    process = None
    try:
        assert observer.ready.wait(10)
        script = '''
import time
from types import SimpleNamespace
import paho.mqtt.client as mqtt
from superlink.bridge.config import MqttConfig
from superlink.bridge.mqtt import MqttBridge
from superlink.bridge.store import InMemoryDeviceStore
runtime = SimpleNamespace(store=InMemoryDeviceStore(), add_event_sink=lambda sink: None)
bridge = MqttBridge(MqttConfig(host='127.0.0.1', port=PORT, base_topic='crash-test'),
                    runtime, mqtt.Client(mqtt.CallbackAPIVersion.VERSION2))
bridge.start()
while True:
    time.sleep(1)
'''.replace('PORT', str(broker.port))
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'tools/sx1302'))
        process = subprocess.Popen([sys.executable, '-c', script], env=env)
        topic = 'crash-test/bridge/availability'
        wait_until(lambda: observer.messages.get(topic) == 'online')
        process.kill()
        process.wait(timeout=5)
        wait_until(lambda: observer.messages.get(topic) == 'offline')
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        observer.stop()


@pytest.mark.parametrize('hostname_matches', [True, False])
def test_tls_verifies_broker_hostname(broker, tmp_path, monkeypatch, hostname_matches):
    openssl = shutil.which('openssl')
    if openssl is None:
        pytest.skip('openssl required for temporary broker TLS certificates')
    cert = tmp_path / 'broker.crt'
    key = tmp_path / 'broker.key'
    san = 'IP:127.0.0.1' if hostname_matches else 'DNS:wrong-host.invalid'
    subprocess.run([openssl, 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                    '-keyout', str(key), '-out', str(cert), '-days', '1',
                    '-subj', '/CN=Test Broker', '-addext', f'subjectAltName={san}'],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    with broker.config.open('a') as file:
        file.write(f'certfile {cert}\nkeyfile {key}\n')
    monkeypatch.setenv('SSL_CERT_FILE', str(cert))
    broker.start()
    store = InMemoryDeviceStore()
    rt = BridgeRuntime(RuntimeConfig(gw_mac=b'123456'), FakeHal(), store)
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    bridge = MqttBridge(MqttConfig(host='127.0.0.1', port=broker.port, tls=True), rt, client)
    failed = threading.Event()
    bridge._paho_on_connect_fail = lambda *_: failed.set()
    try:
        bridge.start()
        if hostname_matches:
            wait_until(lambda: bridge._connected)
        else:
            assert failed.wait(10)
            assert not bridge._connected
    finally:
        bridge.stop()
