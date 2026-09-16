import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from integrations.yolo_settings import configure_yolo_environment


PREFIX = 'PIPELINE_MANUAL_SCREENING_YOLO_'


@pytest.fixture
def setup_yolo(tmp_path, monkeypatch):
    import os
    monkeypatch.setattr(os, 'environ', os.environ.copy())
    for key in list(os.environ):
        if key.startswith(PREFIX):
            monkeypatch.delenv(key)
    cfg = dict(yolo_path=str(tmp_path/'products.pt'), yolo_thresholds_path='',
               yolo_confidence=.25, device='cpu')

    def load(extra=None):
        configure_yolo_environment(cfg | (extra or {}), tmp_path)
        path = Path(__file__).resolve().parents[1]/'legacy/scripts/embodied_data_pipeline-main/lerobot_manual_screening_yolo.py'
        spec = importlib.util.spec_from_file_location('isolated_manual_yolo', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    return cfg, load


def test_fresh_deployment_without_threshold_file_runs_detection(setup_yolo, tmp_path, monkeypatch):
    import sys
    cfg, load = setup_yolo
    Path(cfg['yolo_path']).write_bytes(b'fixture')
    calls = []
    class Model:
        def predict(self, **kwargs):
            calls.append(kwargs)
            boxes = SimpleNamespace(cls=np.array([0, 1]), conf=np.array([.29, .31]),
                                    xyxy=np.array([[0, 0, 10, 10], [1, 1, 11, 11]]))
            return [SimpleNamespace(boxes=boxes, names={0:'Tea', 1:'Milk'})]
    monkeypatch.setitem(sys.modules, 'ultralytics', SimpleNamespace(YOLO=lambda _:Model()))
    module = load(dict(yolo_confidence=.3))
    result = module.YoloRuntime().predict([tmp_path/'frame.jpg'])
    assert calls[0]['conf'] == .3 and calls[0]['device'] == 'cpu'
    assert [d['class_name'] for d in result['predictions'][0]] == ['Milk']
    assert result['predictions'][0][0]['threshold'] == .3
    assert not (tmp_path/'config/yolo-thresholds.json').exists()


def test_explicit_threshold_path_overrides_conventional_file(setup_yolo, tmp_path):
    _, load = setup_yolo
    old = tmp_path/'config/yolo-thresholds.json';old.parent.mkdir()
    old.write_text(json.dumps(dict(per_class_conf={'0':.1})))
    custom = tmp_path/'custom.json';custom.write_text(json.dumps(dict(per_class_conf={'0':.7})))
    module = load(dict(yolo_thresholds_path=str(custom)))
    assert module._threshold_payload()['per_class_conf'] == {'0':.7}


def test_existing_conventional_thresholds_are_preserved(setup_yolo, tmp_path):
    _, load = setup_yolo
    path = tmp_path/'config/yolo-thresholds.json';path.parent.mkdir()
    path.write_text(json.dumps(dict(per_class_conf={'0':.65})))
    assert load()._threshold_payload()['per_class_conf'] == {'0':.65}


@pytest.mark.parametrize('content', [None, '{broken', '{"per_class_conf":{}}',
                                    '{"per_class_conf":{"0":0}}',
                                    '{"per_class_conf":{"0":1.1}}',
                                    '{"per_class_conf":{"0":"nan"}}'])
def test_explicit_invalid_thresholds_never_silently_fall_back(setup_yolo, tmp_path, content):
    _, load = setup_yolo
    path = tmp_path/'custom.json'
    if content is not None:path.write_text(content)
    module = load(dict(yolo_thresholds_path=str(path)))
    with pytest.raises(RuntimeError, match='阈值'):
        module._threshold_payload()


def test_environment_override_is_preserved(setup_yolo, tmp_path, monkeypatch):
    _, load = setup_yolo
    path = tmp_path/'override.json';path.write_text(json.dumps(dict(per_class_conf={'0':.8})))
    monkeypatch.setenv(PREFIX+'THRESHOLDS', str(path))
    monkeypatch.setenv(PREFIX+'DEVICE', '2')
    module = load()
    assert module._threshold_payload()['per_class_conf'] == {'0':.8}
    assert module.YOLO_DEVICE == '2'
