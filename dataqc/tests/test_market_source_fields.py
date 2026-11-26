from pathlib import Path

import h5py
import numpy as np
import pytest

from dataqc.export import path_for
from dataqc.market_source_fields import (
    archive_path, read_source_field, source_identity, validate_source_fields, write_source_fields,
)


def test_market_export_preserves_raw_source_fields(tmp_path):
    source = tmp_path / 'episode.hdf5'
    root = tmp_path / 'LeRobot'
    pose = np.array([[1.1234567890123, 2., -3.], [4., 5., 6.]], dtype=np.float64)
    camera = 'observation/images/rs/cam_high/color'
    with h5py.File(source, 'w') as file:
        file.attrs['complete'] = True
        file['observation/state/base/pose'] = pose
        file['timestamp/t'] = np.array([0.0, 0.03942187293457])
        file['timing/action_valid'] = np.array([[True, False], [False, True]])
        file.create_dataset('language_instruction', data=['抓取', '放置'], dtype=h5py.string_dtype())
        images = file.create_dataset(camera, (2,), dtype=h5py.vlen_dtype(np.uint8))
        images[0] = np.array([255, 216, 255, 217], dtype=np.uint8)
        images[1] = np.array([255, 255, 216, 255, 217], dtype=np.uint8)
        images.attrs['encoded_format'] = 'jpeg'

    video = path_for(root, 0, 'observation.images.cam_high')
    video.parent.mkdir(parents=True)
    video.write_bytes(b'video file validated by the standard exporter')
    for mode, index in [('video', 0), ('original', 1)]:
        relative = write_source_fields(source, root, index, camera_mode=mode)
        result = validate_source_fields(root / relative, root, index)
        assert result['source_identity'] == source_identity(source)
        assert set(result['fields']) == {
            camera, 'language_instruction', 'observation/state/base/pose',
            'timestamp/t', 'timing/action_valid',
        }
        saved_pose = read_source_field(root, index, '/observation/state/base/pose')
        assert saved_pose.dtype == np.float64
        np.testing.assert_array_equal(saved_pose, pose)
        assert read_source_field(root, index, 'language_instruction')[0] == '抓取'.encode()
        value = read_source_field(root, index, camera)
        if mode == 'video':
            assert value == video
        else:
            np.testing.assert_array_equal(value[1], np.array([255, 255, 216, 255, 217], dtype=np.uint8))

    video.unlink()
    with pytest.raises(ValueError, match='Missing LeRobot video'):
        validate_source_fields(archive_path(root, 0), root, 0)
