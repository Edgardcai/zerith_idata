from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient

from dataqc import api, db
from dataqc.checks import numeric_checks
from dataqc.motion import source_height


def test_catalog_and_whole_dataset_creation(tmp_path, monkeypatch):
    base = tmp_path / 'zerith_data'
    group = base / 'Milk_Tea_0.4'
    for relative in ['episode_000001', 'batch/episode_000002']:
        p = group / relative
        p.mkdir(parents=True)
        (p / 'episode.hdf5').touch()
    single = base / 'episode_000099'
    single.mkdir()
    (single / 'episode.hdf5').touch()
    outside = tmp_path / 'outside'
    outside.mkdir()
    (base / 'external_0').symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(api, 'SOURCE_ROOT', base)
    monkeypatch.setattr(api, 'SIM_SOURCE_ROOT', tmp_path / 'sim_data')
    monkeypatch.setattr(db, 'DB', tmp_path / 'qc.sqlite3')
    monkeypatch.setattr(api, 'sha', lambda _: 'test-sha')
    original_settings = api.settings()
    monkeypatch.setattr(api, 'settings', lambda: original_settings | {'stationary_frames': 60})
    db.init()
    client = TestClient(api.app)
    catalog = client.get('/api/datasets').json()
    assert len(catalog) == 1
    assert catalog[0]['root'] == str(group)
    assert catalog[0]['count'] == 2
    assert catalog[0]['height']['expected_m'] == 0.4
    for invalid in [base, group / 'episode_000001', single, base / 'external_0']:
        response = client.post('/api/runs', json={'root': str(invalid)})
        assert response.status_code == 422
    response = client.post('/api/runs', json={'root': str(group)})
    assert response.status_code == 200
    run = db.get_run(response.json()['id'])
    assert run['root'] == str(group)
    assert run['config']['selected_count'] == run['config']['source_count'] == 2
    assert run['config']['stationary_frames'] == 60
    assert len(db.episodes(run['id'])) == 2
    assert client.post('/api/runs', json={'root': str(group)}).status_code == 409


def test_height_belongs_to_first_level_dataset():
    path = Path('/data/zerith_data/Milk_Tea_0.4/batch_0.8/episode_000001')
    assert source_height(path)['expected_m'] == 0.4
    assert 'error' in source_height(Path('/data/zerith_data/Milk_Tea/batch_0.4/episode_000001'))


def test_posture_spike_cannot_become_arm_hard_failure():
    s = np.zeros((150, 23))
    s[50:55, 17:21] = 1.0
    checks = numeric_checks(s, s.copy(), np.arange(150) / 30)
    assert next(c for c in checks if c['key'] == 'posture_state')['status'] == 'warn'
    assert not any(c['status'] == 'fail' for c in checks if c['key'] != 'stationary')


def test_warning_summary_and_failure_reason_export(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB', tmp_path / 'qc.sqlite3')
    db.init()
    rid = db.create('/data/zerith_data/Milk_Tea_0.4', 'auto', {}, ['episode_000001'])
    ep = db.episodes(rid)[0]
    db.update('episodes', ep['id'], status='rejected', grade='F', reason='右爪闭合两次', data={
        'raw_report': {'checks': [
            {'key': 'posture_state', 'label': '腰头零位', 'status': 'warn', 'detail': {'issues': ['Q99 超限']}}
        ]}
    })
    client = TestClient(api.app)
    detail = client.get(f'/api/runs/{rid}').json()['episodes'][0]
    assert detail['warnings'] == ['腰头零位：Q99 超限']
    assert detail['reason'] == '右爪闭合两次'
    csv = client.get(f'/api/runs/{rid}/report.csv').text
    assert 'reason' in csv and '右爪闭合两次' in csv
