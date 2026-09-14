import json
import pytest
import workbench


@pytest.fixture(scope='module')
def app():
    return workbench.load_legacy()


@pytest.mark.parametrize('counts,expected', [
    ({'cam_high': 100, 'cam_left_wrist': 100, 'cam_right_wrist': 100}, 3),
    ({'cam_high': 100, 'cam_left_wrist': 100, 'cam_right_wrist': 0}, 2),
    ({'cam_high': 0, 'cam_left_wrist': 0, 'cam_right_wrist': 0}, 0),
    ({}, None), (None, None), ({'cam_high': None}, None),
])
def test_shared_report_camera_count(app, tmp_path, counts, expected):
    path = tmp_path / 'qc_report.json'
    path.write_text(json.dumps(dict(checks=[], summary=dict(camera_counts=counts))))
    result = app.qc_overview_record_from_json('episode1', path)
    assert result['camera_view_count'] == expected
    if expected is not None:
        assert result['camera_expected_view_count'] == 3


def test_legacy_camera_measurement_preserved(app, tmp_path):
    path = tmp_path / 'qc_report.json'
    path.write_text(json.dumps(dict(summary=dict(camera_counts={'a': 1}), checks=[dict(
        name='camera_completeness', detail={'_summary': dict(
            expected_required_view_count=3, present_required_view_count=2)})])))
    result = app.qc_overview_record_from_json('episode1', path)
    assert result['camera_view_count'] == 2
    assert result['camera_expected_view_count'] == 3


@pytest.mark.parametrize('left,right', [([10], [40]), ([], [20, 80]), (None, [60])])
def test_shared_gripper_counts_with_existing_camera_metrics(app, tmp_path, left, right):
    path = tmp_path / 'qc_report.json'
    path.write_text(json.dumps(dict(checks=[
        dict(name='camera_completeness', detail={'_summary': dict(present_required_view_count=3)}),
        dict(name='gripper_sequence', detail=dict(channels={
            'left': dict(action_close_frames=left, state_close_frames=[]),
            'right': dict(action_close_frames=right, state_close_frames=[])}))])))
    row = app.qc_overview_record_from_json('demo_0', path)
    assert row['left_gripper_close_events'] == (len(left) if left is not None else None)
    assert row['right_gripper_close_events'] == len(right)
    assert row['camera_view_count'] == 3


def test_legacy_gripper_counts_preserved(app, tmp_path):
    path = tmp_path / 'qc_report.json'
    path.write_text(json.dumps(dict(checks=[dict(name='gripper_activity', detail={
        'left': {'close_events': 1}, 'right': {'close_events': 2}})])))
    row = app.qc_overview_record_from_json('episode1', path)
    assert row['left_gripper_close_events'] == 1
    assert row['right_gripper_close_events'] == 2
