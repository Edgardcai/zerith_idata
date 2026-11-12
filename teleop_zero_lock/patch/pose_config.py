"""Shared, dependency-free settings for the H1 PRO collection pose."""
import math

ANGLE_FIELDS = ('waist_pitch_rad', 'waist_yaw_rad', 'head_yaw_rad', 'head_pitch_rad')
# Intersection of the shipped H1 PRO URDF and the factory waist controller limits.
ANGLE_LIMITS = ((0.0, 1.309), (-0.9, 0.9), (-1.5708, 1.5708), (-0.7854, 0.5236))
ANGLE_LABELS = ('腰部 pitch', '腰部 yaw', '头部 yaw', '头部 pitch')
POSE_VERSION = 'collection-1.1-fixed-pose'


def number(value, label, lower, upper):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f'{label}必须为 {lower}～{upper}')
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        raise ValueError(f'{label}必须为 {lower}～{upper}') from None
    if not math.isfinite(result) or not lower <= result <= upper:
        raise ValueError(f'{label}必须为 {lower}～{upper}')
    return result


def validate_config(value):
    if not isinstance(value, dict) or not isinstance(value.get('lift_enabled', False), bool):
        raise ValueError('固定姿态设置格式错误')
    result = {'lift_enabled': value.get('lift_enabled', False),
              'lift_height_m': number(value.get('lift_height_m', .4), '升降柱高度（m）', 0, .8)}
    for field, label, (lower, upper) in zip(ANGLE_FIELDS, ANGLE_LABELS, ANGLE_LIMITS):
        result[field] = number(value.get(field, 0.0), label + '（rad）', lower, upper)
    return result
