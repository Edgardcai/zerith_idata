from pathlib import Path
import pytest
import workbench


@pytest.fixture(scope='module')
def app():return workbench.load_legacy()


@pytest.mark.parametrize('root',[None,'/data/zerith_data/lerobot','/data/zerith_data/lerobot/twohands'])
def test_default_full_and_split_paths(app,root):
    payload=dict(robot_type='zerith',hdf5_root='/data/zerith_data/task_demo')
    if root:payload['lerobot_root']=root
    cfg=app.derive_paths(payload)
    assert app.lerobot_grade_dataset_dir(cfg,'A')==Path('/data/zerith_data/lerobot/twohands/task_demo/A')
    assert app.lerobot_stage_split_output_dir(cfg,'left_hand','A')==Path('/data/zerith_data/lerobot/lefthand/task_demo/A')
    assert app.lerobot_stage_split_output_dir(cfg,'lefthand','B')==Path('/data/zerith_data/lerobot/lefthand/task_demo/B')
    assert app.lerobot_stage_split_output_dir(cfg,'righthand','B')==Path('/data/zerith_data/lerobot/righthand/task_demo/B')


def test_custom_root_and_path_checks(app,tmp_path):
    cfg=app.derive_paths(dict(robot_type='zerith',hdf5_root=str(tmp_path/'task'),lerobot_root=str(tmp_path/'custom'/'twohands')))
    assert app.lerobot_stage_split_output_dir(cfg,'lefthand','A')==tmp_path/'custom'/'lefthand'/'task'/'A'
    with pytest.raises(ValueError):app.lerobot_stage_split_output_dir(cfg,'../escape','A')
    with pytest.raises(ValueError):app.lerobot_stage_split_output_dir(cfg,'lefthand','invalid')
    (tmp_path/'outside').mkdir()
    (tmp_path/'custom').mkdir(exist_ok=True)
    (tmp_path/'custom'/'lefthand').symlink_to(tmp_path/'outside',target_is_directory=True)
    with pytest.raises(ValueError):app.lerobot_stage_split_output_dir(cfg,'lefthand','A')
