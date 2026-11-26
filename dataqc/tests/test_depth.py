import zlib

import cv2
import h5py
import numpy as np
import pytest

from dataqc.checks import raw_checks
from dataqc.depth import decode_depth, depth_integrity
from dataqc.io import CAMS
from dataqc.reporting import presentation
from test_pipeline import source


@pytest.fixture
def depth_image():
    return np.array([[0, 1, 256], [1024, 32000, 65535]], dtype=np.uint16)


def metadata(image):
    return dict(compressed_with="zlib", orig_shape=image.shape,
                orig_dtype=str(image.dtype))


def test_zlib_depth_preserves_all_16_bits(depth_image):
    encoded = np.frombuffer(zlib.compress(depth_image.tobytes()), dtype=np.uint8)
    assert cv2.imdecode(encoded, cv2.IMREAD_UNCHANGED) is None
    decoded = decode_depth(encoded, metadata(depth_image))
    np.testing.assert_array_equal(decoded, depth_image)
    assert decoded.dtype == np.uint16


@pytest.mark.parametrize("with_metadata", [False, True])
def test_png_depth_remains_supported(depth_image, with_metadata):
    ok, encoded = cv2.imencode(".png", depth_image)
    assert ok
    attrs = dict(encoded_format="png")
    if with_metadata:
        attrs.update(orig_shape=depth_image.shape, orig_dtype="uint16")
    np.testing.assert_array_equal(decode_depth(encoded, attrs), depth_image)


@pytest.mark.parametrize("damage", ["empty", "truncated", "checksum", "trailing", "short_raw", "long_raw",
                                    "shape", "dtype", "missing_shape", "missing_dtype", "codec"])
def test_corrupt_depth_and_incorrect_metadata_are_rejected(depth_image, damage):
    attrs = metadata(depth_image)
    raw = depth_image.tobytes()
    encoded = zlib.compress(raw)
    if damage == "empty": encoded = b""
    elif damage == "truncated": encoded = encoded[:-1]
    elif damage == "checksum": encoded = encoded[:-1] + bytes([encoded[-1] ^ 1])
    elif damage == "trailing": encoded += b"extra"
    elif damage == "short_raw": encoded = zlib.compress(raw[:-2])
    elif damage == "long_raw": encoded = zlib.compress(raw + b"xx")
    elif damage == "shape": attrs["orig_shape"] = (2, 4)
    elif damage == "dtype": attrs["orig_dtype"] = "uint8"
    elif damage == "missing_shape": del attrs["orig_shape"]
    elif damage == "missing_dtype": del attrs["orig_dtype"]
    elif damage == "codec": attrs["compressed_with"] = "unknown"
    with pytest.raises((ValueError, zlib.error)):
        decode_depth(np.frombuffer(encoded, dtype=np.uint8), attrs)


def test_frame_failure_is_local_and_count_mismatch_is_reported(tmp_path, depth_image):
    encoded = np.frombuffer(zlib.compress(depth_image.tobytes()), dtype=np.uint8)
    with h5py.File(tmp_path / "frames.hdf5", "w") as f:
        d = f.create_dataset("depth", (3,), dtype=h5py.vlen_dtype(np.dtype("uint8")))
        d.attrs.update(metadata(depth_image))
        d[0] = d[2] = encoded
        d[1] = np.frombuffer(b"damaged", dtype=np.uint8)
        status, detail = depth_integrity(d, 4)
    assert status == "fail"
    assert detail["bad_frames"] == [1]
    assert detail["decoded_frames"] == 2 and detail["stored_frames"] == 3
    assert detail["decode_errors"][0]["frame"] == 1
    assert "3" in detail["issues"][0] and "4" in detail["issues"][0]
    assert depth_integrity(None, 4)[1]["bad_frames"] == ["missing"]


def test_zlib_depth_is_checked_in_full_pipeline(source, depth_image):
    with h5py.File(source / "episode.hdf5", "a") as f:
        f.attrs["depth_recorded"] = True
        n = int(f.attrs["total_frames"])
        for cam in CAMS:
            d = f.create_dataset(f"observation/images/rs/{cam}/depth", (n,),
                                 dtype=h5py.vlen_dtype(np.dtype("uint8")))
            d.attrs.update(metadata(depth_image))
            for i in range(n):
                d[i] = np.frombuffer(zlib.compress((depth_image + i).tobytes()), dtype=np.uint8)
        f["observation/images/rs/cam_left_wrist/depth"][42] = np.frombuffer(b"bad", dtype=np.uint8)
    report = raw_checks(source)
    checks = {c["key"]: c for c in report["checks"] if c["key"].startswith("depth_")}
    assert checks["depth_cam_high"]["status"] == checks["depth_cam_right_wrist"]["status"] == "pass"
    assert checks["depth_cam_high"]["detail"]["decoded_frames"] == n
    left = checks["depth_cam_left_wrist"]
    assert left["status"] == "fail" and left["detail"]["bad_frames"] == [42]
    assert left["detail"]["decoded_frames"] == n - 1
    assert report["hard_fail"]
    text = next(i["text"] for i in presentation(report)["items"] if i["key"] == "depth_cam_high")
    assert f"已解码 {n} / {n} 帧" in text


def test_previous_rule_report_is_not_reused(tmp_path):
    from dataqc.incremental import completed_report, policy
    from dataqc.io import write_json
    from dataqc import config
    cfg = config.settings()
    old_policy = policy(cfg) | dict(rules="zerith_qc_8")
    write_json(tmp_path / "manual_report.json", dict(
        inspection_policy=old_policy, quality_grade="F", execution="completed",
        raw_report=dict(version="zerith_qc_8", checks=[])))
    assert completed_report(tmp_path, cfg) is None
