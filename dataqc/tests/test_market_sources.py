import json

from fastapi.testclient import TestClient

from dataqc import market_api


def test_extra_recording_root_preserves_old_records_and_limits_scope(tmp_path, monkeypatch):
    base = tmp_path / 'old' / 'data'
    extra = tmp_path / 'new' / 'data' / 'market_recordings'
    monkeypatch.setattr(market_api, 'BASE', base)
    monkeypatch.setattr(market_api, 'EXTRA_ROOTS', (extra,))
    monkeypatch.setattr(market_api, 'HOME', tmp_path / 'reports')

    def episode(path, ident):
        path.mkdir(parents=True)
        (path / 'episode.hdf5').touch()
        (path / 'episode_meta.json').write_text(json.dumps({
            'episode_id': ident * 32, 'inference': {'orderId': 'order'},
            'complete': True, 'outcome': 'completed',
        }))
        return path

    old = episode(base / 'market_recordings' / 'batch' / 'episode', '1')
    new = episode(extra / 'batch' / 'episode', '2')
    outside = episode(tmp_path / 'unrelated' / 'episode', '3')
    episode(extra / 'batch' / 'unfinished.partial', '4')
    episode(extra / 'lerobot' / 'exported', '5')
    (extra / 'alias').symlink_to(outside, target_is_directory=True)
    old_report = market_api.report_path(old)
    client = TestClient(market_api.app)

    cfg = client.get('/api/config').json()
    assert cfg['root'] == str(base)
    assert cfg['output'] == str(base / 'lerobot')
    assert cfg['browse_root'] == str(tmp_path)
    assert set(cfg['sources']) == {str(base / 'market_recordings'), str(extra)}
    rows = client.get('/api/records').json()
    assert {r['path'] for r in rows['records']} == {str(old), str(new)}
    assert len({r['id'] for r in rows['records']}) == 2
    assert market_api.report_path(old) == old_report
    for path in (old, new):
        response = client.get('/api/episode', params={
            'root': rows['root'], 'id': str(path.relative_to(tmp_path)),
        })
        assert response.status_code == 200
    selected = client.get('/api/records', params={'root': str(extra)}).json()
    assert [r['path'] for r in selected['records']] == [str(new)]
    assert client.get('/api/records', params={'root': str(outside.parent)}).status_code == 400
    for ident in ('unrelated/episode', str((extra / 'alias').relative_to(tmp_path)), '../outside'):
        assert client.get('/api/episode', params={'root': str(tmp_path), 'id': ident}).status_code == 400
    assert client.post('/api/jobs', json={'root': str(tmp_path)}).status_code == 400

    monkeypatch.setattr(market_api, 'EXTRA_ROOTS', ())
    assert client.get('/api/records').json()['root'] == str(base)
    assert market_api.report_path(old) == old_report
