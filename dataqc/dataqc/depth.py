"""Decode recorded depth frames using their HDF5 storage metadata."""
import math
import zlib

import cv2
import numpy as np


def _text(value):
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


def decode_depth(buffer, attrs):
    encoded = np.asarray(buffer, dtype=np.uint8)
    compression = _text(attrs.get("compressed_with", "")).lower()
    shape = None
    dtype = None
    if "orig_shape" in attrs:
        dimensions = np.asarray(attrs["orig_shape"])
        if (dimensions.ndim != 1 or len(dimensions) != 2
                or dimensions.dtype.kind not in "iu" or np.any(dimensions <= 0)):
            raise ValueError("orig_shape 必须为两个正整数")
        shape = tuple(int(value) for value in dimensions)
    if "orig_dtype" in attrs:
        dtype = np.dtype(_text(attrs["orig_dtype"]))
        if dtype.kind not in "uif":
            raise ValueError("orig_dtype 必须为数值类型")
    if compression == "zlib":
        if shape is None or dtype is None:
            raise ValueError("zlib 深度缺少 orig_shape 或 orig_dtype")
        expected_bytes = math.prod(shape) * dtype.itemsize
        stream = zlib.decompressobj()
        raw = stream.decompress(encoded.tobytes(), expected_bytes + 1)
        if (len(raw) != expected_bytes or not stream.eof
                or stream.unused_data or stream.unconsumed_tail):
            raise ValueError(f"zlib 数据不完整或字节数不匹配：{len(raw)} / {expected_bytes}")
        return np.frombuffer(raw, dtype=dtype).reshape(shape)
    if compression:
        raise ValueError(f"不支持的深度压缩方式：{compression}")
    image = cv2.imdecode(encoded, cv2.IMREAD_UNCHANGED)
    if image is None or image.ndim != 2:
        raise ValueError("深度图片解码失败或维度不是二维")
    if shape is not None and image.shape != shape:
        raise ValueError(f"深度尺寸不匹配：{image.shape} / {shape}")
    if dtype is not None and image.dtype != dtype:
        raise ValueError(f"深度类型不匹配：{image.dtype} / {dtype}")
    return image


def depth_integrity(dataset, expected_frames):
    """Return frame-level failures without aborting the remaining checks."""
    detail = dict(expected_frames=expected_frames, stored_frames=0,
                  decoded_frames=0, bad_frames=[])
    if dataset is None:
        detail["bad_frames"] = ["missing"]
        return "fail", detail
    detail["stored_frames"] = len(dataset)
    detail["compression"] = _text(dataset.attrs.get(
        "compressed_with", dataset.attrs.get("encoded_format", "image")))
    attrs = dict(dataset.attrs)
    errors = []
    for index in range(len(dataset)):
        try:
            decode_depth(dataset[index], attrs)
            detail["decoded_frames"] += 1
        except (ValueError, TypeError, OverflowError, OSError, zlib.error, cv2.error) as exc:
            detail["bad_frames"].append(index)
            errors.append(dict(frame=index, error=str(exc)))
    if errors:
        detail["decode_errors"] = errors
    if len(dataset) != expected_frames:
        detail["issues"] = [f"深度帧数 {len(dataset)} 与轨迹 {expected_frames} 帧不一致"]
    status = "fail" if detail["bad_frames"] or len(dataset) != expected_frames else "pass"
    return status, detail
