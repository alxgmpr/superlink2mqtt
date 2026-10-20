"""Migration, authenticated HTTP and runtime ownership; no physical hardware."""
from contextlib import contextmanager
import http.client
import json
import os
import threading
import time

import pytest
import pysodium

from superlink.bridge.config import RuntimeConfig, WebConfig, DEFAULT_PAIRING_KEY
from superlink.bridge.importing import parse_import
from superlink.bridge.management import Management
from superlink.bridge.runtime import BridgeRuntime
from superlink.bridge.session import DeviceSession
from superlink.bridge.store import DeviceRecord, InMemoryDeviceStore, JsonDeviceStore
from superlink.bridge.web import ManagementServer
from superlink.decoder import parse_frame
from tests.support.fake_hal import FakeHal
from tests.support.sensor_peer import (SENSOR_MAC, GW_MAC, PRIMARY_KEY, FALLBACK_KEY,
                                       challenge, unwrap)

TOKEN = "test-web-token-never-a-real-secret-12345"


def test_ingress_requires_explicit_mode_and_supervisor_peer():
    from superlink.bridge.web import is_ingress
    assert not is_ingress(WebConfig(TOKEN), "172.30.32.2")
    config = WebConfig(TOKEN, ingress=True)
    assert is_ingress(config, "172.30.32.2")
    assert not is_ingress(config, "127.0.0.1")
    assert not is_ingress(config, "172.30.33.5")


def test_ingress_port_rejects_direct_clients_even_with_token():
    rt = runtime()
    web = ManagementServer(rt, WebConfig(TOKEN, port=0, ingress=True))
    web.start()
    try:
        for method, path in [("GET", "/"), ("GET", "/api/session"),
                             ("GET", "/api/state"), ("POST", f"/api/devices/{SENSOR_MAC.hex()}/command")]:
            conn = http.client.HTTPConnection(*web.address)
            conn.request(method, path, headers={"Authorization": f"Bearer {TOKEN}",
                                               "X-Forwarded-For": "172.30.32.2"})
            assert conn.getresponse().status == 403
            conn.close()
    finally:
        web.stop()


def bundle(mac=None):
    return {"version": 1, "devices": [{"mac": (mac or SENSOR_MAC).hex(),
        "key": PRIMARY_KEY.hex(), "fallbackKey": FALLBACK_KEY.hex(),
        "name": "Workshop door", "networkId": 3456}]}


def runtime(store=None):
    return BridgeRuntime(RuntimeConfig(gw_mac=GW_MAC), FakeHal(),
                         store if store is not None else InMemoryDeviceStore())


def test_import_reconnects_with_real_crypto_after_persistence(tmp_path):
    store = JsonDeviceStore(str(tmp_path / "devices.json"))
    rt = runtime(store)
    rt.core.import_devices(parse_import(bundle()))
    record, = JsonDeviceStore(store.path).load_all()
    restored = DeviceSession(record, GW_MAC, DEFAULT_PAIRING_KEY)
    restored.start(100)
    raw, key = challenge(restored._pubkey, context=PRIMARY_KEY, transport_key=FALLBACK_KEY)
    frames, events = restored.feed(parse_frame(raw), 1, 101)
    assert restored.session_key == key
    assert any(getattr(e, "state", None) == "active" for e in events)
    body = unwrap(frames[0].data, FALLBACK_KEY)
    assert pysodium.crypto_stream_xor(body[2:], len(body) - 2, bytes(24), key) == GW_MAC + SENSOR_MAC + bytes(4)
    assert restored.to_record().name == "Workshop door"
    assert restored.to_record().network_id == 3456
    assert restored.to_record().kdf_context == PRIMARY_KEY


@pytest.mark.parametrize("field,value", [
    ("mac", "1234"), ("mac", None), ("key", "SESSION-SECRET"),
    ("fallbackKey", ""), ("fallbackKey", None), ("name", ""),
    ("name", "x" * 81), ("name", "a\nb"), ("networkId", True),
    ("networkId", -1), ("networkId", 2**32), ("networkId", 0),
])
def test_invalid_import_is_safe_and_never_echoes_keys(field, value):
    doc = bundle()
    doc["devices"][0][field] = value
    rt = runtime()
    with pytest.raises(ValueError) as error:
        rt.core.import_devices(parse_import(doc))
    assert "SESSION-SECRET" not in str(error.value)
    assert PRIMARY_KEY.hex() not in str(error.value)
    assert rt.store.load_all() == []
    assert not rt.core.has_session(SENSOR_MAC)


def test_duplicate_or_partial_bundle_never_imports_any_device():
    rt = runtime()
    doc = bundle()
    doc["devices"].append(dict(doc["devices"][0]))
    with pytest.raises(ValueError, match="duplicate"):
        rt.core.import_devices(parse_import(doc))
    assert rt.store.load_all() == []
    doc["devices"][1] = {"mac": "aabbccddeeff"}
    with pytest.raises(ValueError, match="Device 2"):
        parse_import(doc)


def test_accepts_native_controller_request_and_normalized_request():
    item = bundle()["devices"][0]
    assert parse_import({"env": {"type": "request", "action": "addDevice"}, "pay": item})[0].mac == SENSOR_MAC
    assert parse_import({"method": "addDevice", "params": item})[0].mac == SENSOR_MAC
    with pytest.raises(ValueError):
        parse_import({"env": {"type": "event", "action": "addDevice"}, "pay": item})
    with pytest.raises(ValueError):
        parse_import({"version": True, "devices": [item]})


def test_import_backups_private_atomic_store_and_refuses_overwrite(tmp_path):
    path = tmp_path / "devices.json"
    store = JsonDeviceStore(str(path))
    other = DeviceRecord(mac=bytes.fromhex("aabbccddeeff"))
    store.save(other)
    before = path.read_bytes()
    management = Management(runtime(store))
    management.runtime.core.import_devices(parse_import(bundle()))
    backup, = tmp_path.glob("devices.json.backup-*")
    assert backup.read_bytes() == before
    assert path.stat().st_mode & 0o777 == 0o600
    assert backup.stat().st_mode & 0o777 == 0o600
    current = path.read_bytes()
    with pytest.raises(ValueError, match="existing"):
        management.runtime.core.import_devices(parse_import(bundle()))
    assert path.read_bytes() == current
    assert len(list(tmp_path.glob("devices.json.backup-*"))) == 1


def test_failed_replace_preserves_registry_and_no_partial_session(tmp_path, monkeypatch):
    store = JsonDeviceStore(str(tmp_path / "devices.json"))
    store.save(DeviceRecord(mac=b"\xaa" * 6))
    before = open(store.path, "rb").read()
    replace = os.replace
    def fail(source, target):
        if target == store.path:
            raise OSError("disk error")
        return replace(source, target)
    monkeypatch.setattr(os, "replace", fail)
    rt = runtime(store)
    with pytest.raises(OSError):
        rt.core.import_devices(parse_import(bundle()))
    assert open(store.path, "rb").read() == before
    assert not rt.core.has_session(SENSOR_MAC)
    assert not list(tmp_path.glob(".superlink-*"))


def test_existing_device_conflicts_and_snapshots_hide_keys():
    rt = runtime()
    management = Management(rt)
    management.runtime.core.import_devices(parse_import(bundle()))
    with pytest.raises(ValueError):
        management.runtime.core.import_devices(parse_import(bundle()))
    snapshot = management.snapshot()
    assert snapshot["devices"][0]["state"] == "awaiting_reconnect"
    encoded = json.dumps(snapshot)
    for secret in (PRIMARY_KEY.hex(), FALLBACK_KEY.hex(), "transport_key", "kdf_context"):
        assert secret not in encoded


def test_cancelled_request_cannot_mutate_later():
    rt = runtime()
    management = Management(rt)
    future = rt.submit_management(lambda: management.runtime.core.import_devices(parse_import(bundle())))
    assert future.cancel()
    rt.poll_once(time.monotonic())
    assert rt.store.load_all() == []


@contextmanager
def running_web():
    rt = runtime()
    web = ManagementServer(rt, WebConfig(token=TOKEN, port=0))
    thread = threading.Thread(target=rt.run)
    thread.start()
    web.start()
    try:
        yield rt, web
    finally:
        rt.stop()
        thread.join(timeout=2)
        web.stop()
        assert not thread.is_alive()


def request(web, method, path, body=None, token=TOKEN, headers=None):
    conn = http.client.HTTPConnection(*web.address, timeout=10)
    request_headers = {"Content-Type": "application/json"}
    if token is not None:
        request_headers["Authorization"] = "Bearer " + token
    request_headers.update(headers or {})
    try:
        conn.request(method, path, None if body is None else json.dumps(body), request_headers)
        response = conn.getresponse()
        return response.status, response.read(), dict(response.getheaders())
    finally:
        conn.close()


def test_http_auth_origin_limits_and_safe_static_paths():
    with running_web() as (_, web):
        assert request(web, "GET", "/api/state", token=None)[0] == 401
        assert request(web, "GET", "/api/state", token="wrong")[0] == 401
        assert request(web, "POST", f"/api/devices/{SENSOR_MAC.hex()}/command", {}, headers={"Origin": "https://evil.example"})[0] == 403
        assert request(web, "POST", f"/api/devices/{SENSOR_MAC.hex()}/command", {}, headers={"Origin": "http://["})[0] == 403
        assert request(web, "POST", f"/api/devices/{SENSOR_MAC.hex()}/command", {}, headers={"Content-Type": "text/plain"})[0] == 415
        assert request(web, "POST", f"/api/devices/{SENSOR_MAC.hex()}/command", {"x": "a" * 131072})[0] == 413
        assert request(web, "GET", "/../store.py")[0] == 404
        code, body, headers = request(web, "GET", "/", token=None)
        assert code == 200 and b"superlink" in body
        assert "Content-Security-Policy" in headers
        assert headers["Cache-Control"] == "no-store"


def test_ingress_bootstrap_and_api_use_supervisor_auth(monkeypatch):
    import superlink.bridge.web as module
    # Peer filtering is tested separately; emulate the verified ingress peer.
    monkeypatch.setattr(module, "is_ingress", lambda config, peer: config.ingress)
    with running_web() as (_, web):
        web.config.ingress = True
        status, body, _ = request(web, "GET", "/api/session", token=None)
        assert status == 200 and json.loads(body) == {"ingress": True, "settings": False}
        assert request(web, "GET", "/api/state", token=None)[0] == 200


@pytest.mark.parametrize("ingress", [False, True])
def test_removed_import_and_extraction_routes_cannot_mutate_devices(monkeypatch, ingress):
    import superlink.bridge.web as module
    monkeypatch.setattr(module, "is_ingress", lambda config, peer: config.ingress)
    with running_web() as (rt, web):
        web.config.ingress = ingress
        token = None if ingress else TOKEN
        assert not hasattr(web, "extraction")
        assert request(web, "GET", "/api/extraction", token=token)[0] == 404
        for path in ("import", "import/preview", "extraction/probe", "extraction/start",
                     "extraction/recover", "extraction/preview", "extraction/import"):
            assert request(web, "POST", f"/api/{path}",
                           {"document": bundle(), "confirmed": True}, token=token)[0] == 404
        assert rt.store.load_all() == []
        assert not rt.core.has_session(SENSOR_MAC)


@pytest.mark.parametrize("ingress", [False, True])
def test_http_pair_command_and_state(monkeypatch, ingress):
    import superlink.bridge.web as module
    monkeypatch.setattr(module, "is_ingress", lambda config, peer: config.ingress)
    from tests.support.sensor_peer import discovery
    with running_web() as (rt, web):
        web.config.ingress = ingress
        token = None if ingress else TOKEN
        rt.submit_management(lambda: rt.core.feed(discovery(), 1, time.monotonic())).result(2)
        path = f"/api/devices/{SENSOR_MAC.hex()}/command"
        assert request(web, "POST", path, {"command": []}, token=token)[0] == 400
        code, body, _ = request(web, "POST", path, {"command": "adopt"}, token=token)
        assert code == 200 and json.loads(body)["state"] == "queued"
        assert rt.core.has_session(SENSOR_MAC)
        assert request(web, "POST", path, {"command": "adopt"}, token=token)[0] == 400
        assert request(web, "POST", path, {"command": "locate"}, token=token)[0] == 400
        snapshot = request(web, "GET", "/api/state", token=token)[1]
        assert PRIMARY_KEY.hex().encode() not in snapshot
        assert b"Adopt queued" in snapshot
        assert b"Sensor response" not in snapshot


def test_web_config_requires_private_token_and_valid_port(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text('gw_mac: "aabbccddeeff"\nweb: {}\n')
    monkeypatch.delenv("SUPERLINK_WEB_TOKEN", raising=False)
    with pytest.raises(ValueError, match="token"):
        RuntimeConfig.load(str(config))
    monkeypatch.setenv("SUPERLINK_WEB_TOKEN", TOKEN)
    loaded = RuntimeConfig.load(str(config))
    assert loaded.web.host == "127.0.0.1"
    assert TOKEN not in repr(loaded)
    with pytest.raises(ValueError):
        WebConfig(TOKEN, port=True)
    with pytest.raises(ValueError):
        WebConfig(TOKEN + "\n")


def test_pairing_blocks_device_commands_until_persisted_adoption():
    from superlink.bridge.events import DeviceStateEvent
    from tests.fixtures.captured_frames import DISCOVERY_FRAME_RAW
    rt = runtime()
    manager = Management(rt)
    rt.core.feed(DISCOVERY_FRAME_RAW, 1, 100)
    mac = next(iter(manager.observed))
    assert manager.command(mac.hex(), 'adopt')['state'] == 'queued'
    with pytest.raises(ValueError, match='pairing'):
        manager.command(mac.hex(), 'reboot')
    assert manager.snapshot()['devices'][0]['has_session']
    assert not manager.snapshot()['devices'][0]['adopted']
