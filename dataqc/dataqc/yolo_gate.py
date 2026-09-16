"""Legacy wrist-camera matching first; one focused VLM call only for warnings."""
import hashlib
import importlib
import json
import sys
import time
from pathlib import Path
from typing import Literal

from .io import frame, load, normalized_task, parse_task, read_json, sha, write_json
from .motion import RULE_VERSION, warning_grade
from . import vision

VERSION = 'motion_category_v5'
_MODELS = {}


def cache_policy(cfg):
    if not cfg.get('vlm_enabled', False):
        return dict(grade_policy='motion_category_v5', motion_policy='trajectory_review_v3', vlm_enabled=False)
    return dict(grade_policy='motion_category_v5', motion_policy='trajectory_review_v3', vlm_enabled=cfg.get('vlm_enabled', False),
                confidence=cfg.get('yolo_confidence', .25), frame_offset=cfg.get('yolo_frame_offset', 40),
                model_hash=sha(cfg['yolo_path']),
                thresholds_hash=sha(cfg['yolo_thresholds_path']) if cfg.get('yolo_thresholds_path') else '')


def legacy_rules():
    path = str(Path(__file__).resolve().parents[1] / 'legacy/scripts/embodied_data_pipeline-main')
    if path not in sys.path:
        sys.path.insert(0, path)
    return importlib.import_module('lerobot_manual_screening_yolo')


def model_config(cfg):
    path = Path(cfg['yolo_path'])
    digest = sha(path)
    if cfg.get('yolo_sha256') and cfg['yolo_sha256'] != digest:
        raise ValueError('YOLO 权重已变化，请新建任务')
    threshold_path = cfg.get('yolo_thresholds_path')
    thresholds = legacy_rules()._threshold_payload(Path(threshold_path))['per_class_conf'] if threshold_path else {}
    for value in thresholds.values():
        if not 0 < float(value) <= 1:
            raise ValueError('YOLO 类别阈值必须在 (0,1] 内')
    return digest, thresholds


def predict_samples(root, samples, cfg, digest, thresholds):
    if digest not in _MODELS:
        from ultralytics import YOLO
        _MODELS.clear()
        _MODELS[digest] = YOLO(cfg['yolo_path'])
    model = _MODELS[digest]
    fallback = cfg.get('yolo_confidence', .25)
    minimum = min([fallback] + [float(v) for v in thresholds.values()])
    results = model.predict([frame(root, s['camera'], s['frame']) for s in samples],
                            device=cfg['device'], imgsz=640, conf=minimum, verbose=False)
    if len(results) != len(samples):
        raise ValueError('YOLO 检测结果与关键帧数量不一致')
    predictions = []
    for r in results:
        boxes = []
        for b in r.boxes:
            index = int(b.cls.item())
            confidence = float(b.conf.item())
            threshold = float(thresholds.get(str(index), fallback))
            if confidence >= threshold:
                name = model.names[index]
                boxes.append(dict(name=name, confidence=round(confidence, 4), threshold=threshold,
                                  box=[round(float(v), 2) for v in b.xyxy[0].tolist()],
                                  is_product=name not in legacy_rules().NON_PRODUCT_CLASSES))
        predictions.append(boxes)
    return predictions, dict(model.names)


def match_hand(hand, expected, moments, classes):
    legacy = legacy_rules()
    normal = legacy._normalise_name
    supported = normal(expected) in {normal(v) for v in classes.values()}
    matched_frames = set()
    for moment in moments:
        products = [v for v in moment['detections'] if v.get('is_product', True)
                    and v['name'] not in legacy.NON_PRODUCT_CLASSES]
        moment['matched'] = any(normal(v['name']) == normal(expected) for v in products)
        if moment['matched']:
            matched_frames.add(moment['frame'])
    matched = len(matched_frames)
    okay = supported and matched >= legacy.REQUIRED_MATCHES
    name = '左手' if hand == 'left' else '右手'
    reason = f'{name} {expected}：{matched}/3 个时刻匹配'
    if not supported:
        reason += '；当前 YOLO 权重不包含此商品类别'
    elif not okay:
        reason += '；未稳定识别，需视觉复核'
    return dict(hand=hand, expected=expected, status='pass' if okay else 'warn',
                supported=supported, matched_count=matched, required_matches=legacy.REQUIRED_MATCHES,
                moments=moments, reason=reason)


def yolo_match(root, report, cfg, cache, progress=lambda _: None):
    started = time.perf_counter()
    d = load(root)
    targets = parse_task(normalized_task(d['task'])) or {}
    if not targets:
        raise ValueError('无法解析左右手目标物品，请人工补全提示词')
    channels = next(c['detail']['channels'] for c in report['checks'] if c['key'] == 'gripper_sequence')
    offset = cfg.get('yolo_frame_offset', 40)
    samples = []
    for hand, expected in targets.items():
        closes = channels[hand]['action_close_frames']
        if len(closes) != 1:
            raise ValueError('每手必须有唯一闭合时刻，先完成数值质检')
        for moment, value in zip(legacy_rules().MOMENTS, (closes[0]-offset, closes[0], closes[0]+offset)):
            samples.append(dict(hand=hand, expected=expected, moment=moment,
                                camera=f'cam_{hand}_wrist', frame=max(0, min(d['n']-1, value))))
    digest, thresholds = model_config(cfg)
    signature = hashlib.sha256(json.dumps(dict(version=VERSION, model=digest, samples=samples,
                                 confidence=cfg.get('yolo_confidence', .25), thresholds=thresholds), sort_keys=True).encode()).hexdigest()
    cached = read_json(Path(cache) / 'yolo_match.json')
    if cached.get('signature') == signature:
        return cached
    progress('YOLO 左右手匹配 · 每手闭合前 / 闭合时 / 闭合后')
    predictions, classes = predict_samples(root, samples, cfg, digest, thresholds)
    for sample, detections in zip(samples, predictions):
        sample['detections'] = detections
    hands = [match_hand(hand, expected, [s for s in samples if s['hand'] == hand], classes)
             for hand, expected in targets.items()]
    warnings = [h['reason'] for h in hands if h['status'] == 'warn']
    result = dict(version=VERSION, signature=signature, status='warn' if warnings else 'pass',
                  hands=hands, warnings=warnings, classes=classes, model_hash=digest,
                  model_path=cfg['yolo_path'], frame_offset=offset, confidence=cfg.get('yolo_confidence', .25),
                  elapsed_seconds=time.perf_counter()-started)
    write_json(Path(cache) / 'yolo_match.json', result)
    return result


class HandCheck(vision.Strict):
    hand: Literal['left', 'right']
    status: Literal['pass', 'fail', 'uncertain']
    observed_item: str
    reason: str
    evidence_ids: list[str]


class MatchingReview(vision.Strict):
    hands: list[HandCheck]


def review_warnings(root, gate, cfg, cache, evidence, progress, all_hands=False):
    warned = list(gate['hands']) if all_hands else [h for h in gate['hands'] if h['status'] == 'warn']
    content = [dict(type='input_text', text=
        '独立核对以下每只手的商品与实际操作手是否匹配，YOLO 通过也必须核对，不做全程动作八项检查。'
        '每只手的 before/close/after 为闭合前40帧、闭合时及闭合后40帧（以提供帧号为准）。'
        '先看对应腕相机，再用同时刻头部相机辅助定位。不要把另一只手或货架上出现的商品当作该手拿取的商品。'
        '目标商品名是完整品牌标签，不可拆词解释或改写。YOLO 缺少类别、漏检、遮挡不代表拿错；看不清用 uncertain。'
        'pass 需要可见的匹配证据；fail 仅用于清楚看见该手拿了不同商品或操作手错误。'
        '只返回被要求的手，每手恰好一项，中文原因简短，evidence_ids 每手最多3个且只引用提供的原样ID。'
        + json.dumps([dict(hand=h['hand'],expected=h['expected'],warning=h['reason'],
                           moments=[dict(moment=m['moment'],frame=m['frame'],camera=m['camera'],detections=m['detections'])
                                    for m in h['moments']]) for h in warned], ensure_ascii=False))]
    sent = set()
    for hand in warned:
        for moment in hand['moments']:
            for camera in (moment['camera'], 'cam_high'):
                key = (camera, moment['frame'])
                if key not in sent:
                    content.extend(vision.image_content(root, *key, evidence))
                    sent.add(key)
    progress(f"类别双检 · {cfg['api_model']} 定点复核 {len(warned)} 只手 / {len(sent)} 张图")
    result = vision.call_vlm(content, MatchingReview, cfg | dict(api_attempts=1, max_output_tokens=1200), cache, progress)
    result=MatchingReview.model_validate(result).model_dump()
    expected_hands = sorted(h['hand'] for h in warned)
    if sorted(h['hand'] for h in result['hands']) != expected_hands:
        raise ValueError('VLM 未逐手返回预警核对结果，需人工确认')
    for h in result['hands']:
        frames = {m['frame'] for item in warned if item['hand'] == h['hand'] for m in item['moments']}
        local = {k:v for k,v in evidence.items() if v['frame'] in frames and k.startswith(('cam_high:', f"cam_{h['hand']}_wrist:"))}
        vision.review_invalid_refs(h, local)
    return result


def inspect(root, report, cfg, cache, progress=lambda _: None, **kwargs):
    from .assessment import inspect as assess
    return assess(root, report, cfg, cache, progress, **kwargs)
