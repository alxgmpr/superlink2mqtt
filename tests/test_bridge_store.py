from superlink.bridge.store import DeviceRecord, InMemoryDeviceStore, JsonDeviceStore

MAC = bytes.fromhex("9041B22E9A53")


def _rec():
    return DeviceRecord(mac=MAC, device_type=0x0100,
                        primary_key=b"\x11" * 32, fallback_key=b"\x22" * 32,
                        adopted=True, tx_seq_hi=5, ul_counter_offset=5,
                        last_seen=123.0)


def test_inmemory_roundtrip():
    s = InMemoryDeviceStore()
    s.save(_rec())
    got = s.load_all()
    assert len(got) == 1 and got[0].mac == MAC and got[0].primary_key == b"\x11" * 32


def test_inmemory_save_is_upsert_and_delete():
    s = InMemoryDeviceStore()
    s.save(_rec())
    s.save(DeviceRecord(mac=MAC, device_type=0x0200))  # overwrite
    assert s.load_all()[0].device_type == 0x0200
    s.delete(MAC)
    assert s.load_all() == []


def test_json_roundtrip(tmp_path):
    path = str(tmp_path / "devices.json")
    s = JsonDeviceStore(path)
    s.save(_rec())
    reloaded = JsonDeviceStore(path).load_all()
    assert len(reloaded) == 1
    r = reloaded[0]
    assert r.mac == MAC and r.fallback_key == b"\x22" * 32 and r.adopted is True


def test_json_roundtrip_preserves_prop_sizes_int_keys(tmp_path):
    # prop_sizes maps int propertyId -> int valueSize. JSON stringifies dict
    # keys, so the store must restore them as ints, else PROPERTY_REPORT decode
    # (which looks up integer ids) silently breaks after a bridge restart.
    path = str(tmp_path / "devices.json")
    rec = DeviceRecord(mac=MAC, prop_sizes={1: 4, 3: 4, 15: 1})
    JsonDeviceStore(path).save(rec)
    reloaded = JsonDeviceStore(path).load_all()[0]
    assert reloaded.prop_sizes == {1: 4, 3: 4, 15: 1}


def test_atomic_store_keeps_private_previous_snapshot(tmp_path):
    import stat
    path = tmp_path / 'devices.json'
    store = JsonDeviceStore(str(path))
    store.save(_rec())
    before = path.read_bytes()
    store.delete(MAC)
    assert store.load_all() == []
    assert path.with_suffix('.json.bak').read_bytes() == before
    for item in (path, path.with_suffix('.json.bak')):
        assert stat.S_IMODE(item.stat().st_mode) == 0o600


def test_replace_failure_preserves_pairing_keys(tmp_path, monkeypatch):
    import os
    import pytest
    path = tmp_path / 'devices.json'
    store = JsonDeviceStore(str(path))
    store.save(_rec())
    before = path.read_bytes()
    replace = os.replace
    def fail_primary(source, target):
        if str(target) == str(path):
            raise OSError('simulated interrupted replacement')
        replace(source, target)
    monkeypatch.setattr(os, 'replace', fail_primary)
    with pytest.raises(OSError):
        store.delete(MAC)
    assert path.read_bytes() == before
    assert store.load_all()[0].primary_key == _rec().primary_key
    assert sorted(p.name for p in tmp_path.iterdir()) == ['devices.json', 'devices.json.bak']


def test_failed_write_cleans_temporary_file(tmp_path, monkeypatch):
    import json
    import pytest
    path = tmp_path / 'devices.json'
    store = JsonDeviceStore(str(path))
    store.save(_rec())
    before = path.read_bytes()
    def interrupted_dump(data, file, **kwargs):
        file.write('{')
        raise OSError('disk full')
    monkeypatch.setattr(json, 'dump', interrupted_dump)
    with pytest.raises(OSError):
        store.save(_rec())
    assert path.read_bytes() == before
    assert [p.name for p in tmp_path.iterdir()] == ['devices.json']


def test_corrupt_store_never_silently_restores_old_keys(tmp_path):
    import pytest
    from superlink.bridge.store import StoreError
    path = tmp_path / 'devices.json'
    store = JsonDeviceStore(str(path))
    store.save(_rec())
    store.delete(MAC)  # backup still contains a now-removed device
    backup = path.with_suffix('.json.bak').read_bytes()
    path.write_text('{broken')
    with pytest.raises(StoreError, match='backup'):
        store.load_all()
    with pytest.raises(StoreError):
        store.save(_rec())
    assert path.read_text() == '{broken'
    assert path.with_suffix('.json.bak').read_bytes() == backup
    path.unlink()
    with pytest.raises(StoreError, match='missing'):
        store.load_all()


def test_malformed_record_does_not_get_overwritten(tmp_path):
    import json
    import pytest
    from superlink.bridge.store import StoreError
    path = tmp_path / 'devices.json'
    for malformed in ([], {MAC.hex(): {}}, {MAC.hex(): {'mac': 'abcd'}},
                      {MAC.hex(): {'mac': MAC.hex(), 'primary_key': 'aa'}}):
        path.write_text(json.dumps(malformed))
        with pytest.raises(StoreError):
            JsonDeviceStore(str(path)).save(_rec())
        assert json.loads(path.read_text()) == malformed


def test_parallel_saves_preserve_all_records(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    store = JsonDeviceStore(str(tmp_path / 'devices.json'))
    records = [DeviceRecord(mac=i.to_bytes(6, 'big')) for i in range(20)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(store.save, records))
    assert {r.mac for r in store.load_all()} == {r.mac for r in records}
