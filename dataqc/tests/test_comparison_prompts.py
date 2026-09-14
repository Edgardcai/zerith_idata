"""Fixed collection grammar and membership in the comparison product list."""
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'legacy/scripts/embodied_data_pipeline-main'))
import zerith_lerobot_qc as qc


@pytest.mark.parametrize('text,mode', [
    ('Grasp Coca-Cola with the left hand and then grasp Yili Peach Yogurt with the right hand.', 'twohand'),
    ('Grasp Coca-Cola with the left hand.', 'left_hand'),
    ('Grasp Yili Peach Yogurt with the right hand.', 'righthand'),
])
def test_allowed_templates(text, mode):
    assert qc.prompt_error(text, mode) == ''
    assert qc.prompt_error(text, None) == ''
    assert qc.prompt_format(text) == qc.PROMPT_TEMPLATES[mode]


@pytest.mark.parametrize('text,mode,reason', [
    ('Grasp Coca-Cola with the left hand', None, '句末英文句号'),
    ('Grasp Coca-Cola with the left hand.', 'righthand', '目录不符'),
    ('grasp Coca-Cola with the left hand.', None, '固定模板'),
    ('Grasp New Product with the left hand.', None, 'New Product'),
    ('Grasp coca-cola with the left hand.', None, '商品列表'),
    ('Grasp Coca-Cola with the left hand and then grasp Unknown with the right hand.', None, 'Unknown'),
    ('Grasp Coca-Cola with the right hand and then grasp Yili Peach Yogurt with the left hand.', None, '固定模板'),
    ('Grasp Coca-Cola with the left hand. extra', None, '固定模板'),
])
def test_reject_format_or_unknown_item(text, mode, reason):
    assert reason in qc.prompt_error(text, mode)


def test_report_locates_unknown_items_and_accepts_different_valid_modes(tmp_path):
    descriptors = [dict(path=str(tmp_path / mode), platform=platform, tasks=[text], name=mode, id=mode)
                   for mode, platform, text in [
                       ('lefthand', 'simulation', 'Grasp Coca-Cola with the left hand.'),
                       ('righthand', 'real', 'Grasp Yili Peach Yogurt with the right hand.')]]
    assert qc.prompt_report(descriptors)['status'] == 'pass'
    descriptors[1]['tasks'] = ['Grasp Invented Drink with the right hand.']
    report = qc.prompt_report(descriptors)
    assert report['status'] == 'fail'
    assert report['platform_statuses'] == {'simulation': 'pass', 'real': 'fail'}
    assert 'Invented Drink' in report['issue_details'][0]['reason']
    assert report['issue_details'][0]['datasets'][0]['path'] == descriptors[1]['path']
    assert report['allowed_items'] == list(qc.cp.KNOWN_ITEMS)
    assert 'Invented Drink' in qc.check_reason(dict(name='task_consistency', detail=dict(
        errors=['物品名称不在商品列表中：Invented Drink'])))
