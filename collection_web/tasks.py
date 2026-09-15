import re
import math
from decimal import Decimal, InvalidOperation
from datetime import datetime
from zoneinfo import ZoneInfo

DEFAULT_PROMPT = 'Grasp Dahongpao Milk Tea with the left hand and then grasp If coconut with the right hand'

def default_task_id():
    return int(datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d'))


def scene_config(payload):
    if not isinstance(payload,dict):raise ValueError('场景配置格式错误')
    result={}
    for key,default,low,high,label in [('task_id',default_task_id(),0,99999999,'采集日期编号'),('scene_id',1,1,99999999,'场景编号')]:
        value=payload.get(key,default)
        if type(value) is int:number=value
        elif isinstance(value,str) and re.fullmatch(r'[0-9]{1,8}',value.strip()):number=int(value.strip())
        else:raise ValueError(f'{label}必须为 {low}—{high} 的整数')
        if not low<=number<=high:raise ValueError(f'{label}必须为 {low}—{high} 的整数')
        result[key]=number
    return result

def dataset_name(scene_id, task_id=None):
    values=scene_config({'scene_id':scene_id,'task_id':default_task_id() if task_id is None else task_id})
    return f"{values['task_id']}_scene{values['scene_id']}"

def normalize_height(height):
    """Optional episode metadata; never used for paths or robot motion."""
    raw=str(height).strip()
    if not re.fullmatch(r'\d{1,6}(?:\.\d{1,6})?',raw):
        raise ValueError('升降柱高度请填写非负米数，例如 0.8，最多六位小数')
    try:number=Decimal(raw)
    except InvalidOperation:raise ValueError('升降柱高度无效')
    height=format(number.normalize(),'f')
    return height

def parse_targets(prompt):
    matches = re.findall(r'(?:grasp|pick up)\s+(.+?)\s+with\s+(?:the\s+)?(left|right)\s+hand', prompt, flags=re.I)
    result = {'left': '', 'right': ''}
    for item, side in matches:
        result[side.lower()] = item.strip().rstrip('.')
    return result

def validate_task(payload):
    if not isinstance(payload, dict):
        raise ValueError('任务配置格式错误')
    prompt = payload.get('task_name', '')
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 500:
        raise ValueError('请输入 1—500 字的提示词')
    prompt = prompt.strip()
    if any(c in prompt for c in '/\\\x00\n\r') or prompt in ('.', '..'):
        raise ValueError('提示词不能包含路径分隔符或换行')
    if len(prompt.encode('utf-8')) > 230:
        raise ValueError('提示词过长，厂商会将其用作目录名；请控制在 230 UTF-8 字节内')
    result = {'task_name': prompt, **scene_config(payload)}
    record_depth = payload.get('record_depth', False)
    if type(record_depth) is not bool:
        raise ValueError('记录深度图必须是布尔值')
    result['record_depth'] = record_depth
    for key, default, low, high in [('subtask_num',2,1,20),('max_episode_time',1000,1,1200),('frequency',30,1,60)]:
        value = payload.get(key, default)
        if isinstance(value, bool): raise ValueError(f'{key} 必须为整数')
        try:
            number = float(value)
            if not math.isfinite(number) or not number.is_integer() or not low <= number <= high: raise ValueError()
        except (ValueError, TypeError): raise ValueError(f'{key} 必须为 {low}—{high} 的整数')
        result[key] = int(number)
    targets = parse_targets(prompt)
    for side in ('left','right'):
        value = payload.get(side, targets[side])
        if not isinstance(value, str) or len(value)>200: raise ValueError('目标物体名称无效')
        targets[side] = value.strip()
    height=payload.get('lift_height')
    if height is not None and str(height).strip():
        targets['lift_height']=normalize_height(height)
    description = prompt
    if targets['left'] and targets['right']:
        description = f"Stage 1: Grasp {targets['left']} with the left hand. Stage 2: Keep holding {targets['left']} with the left hand and grasp {targets['right']} with the right hand."
    scene = ' and '.join(targets[side] for side in ('left','right') if targets[side])
    result.update(scene_name=f"scene{result['scene_id']}",
        scene_desc=f'{scene} are placed on shelves within reach of the robot.' if scene else 'Objects are placed within reach of the robot.',
        action_desc=description, step_list=[])
    return result, targets
