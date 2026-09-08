import re
import math
from decimal import Decimal, InvalidOperation

DEFAULT_PROMPT = 'Grasp Dahongpao Milk Tea with the left hand and then grasp If coconut with the right hand'

def dataset_name(targets, height):
    """Return a single safe directory component; height is a label, never motion."""
    names=[]
    for side in ('left','right'):
        value=re.sub(r'\s+', '', targets.get(side,''))
        if not value or not all(c.isalnum() or c=='-' for c in value):
            raise ValueError('目录命名需要左右商品名称；请使用文字、数字或短横线')
        names.append(value)
    raw=str(height).strip()
    if not re.fullmatch(r'\d{1,6}(?:\.\d{1,6})?',raw):
        raise ValueError('升降柱高度请填写非负米数，例如 0.8，最多六位小数')
    try:number=Decimal(raw)
    except InvalidOperation:raise ValueError('升降柱高度无效')
    height=format(number.normalize(),'f')
    name='_'.join([*names,height])
    if len(name.encode('utf-8'))>240:raise ValueError('商品名称过长，生成的目录名超过 240 字节')
    return name,height

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
    result = {'task_name': prompt}
    for key, default, low, high in [('task_id',1,0,999999),('subtask_num',2,1,20),('max_episode_time',1000,1,1200),('frequency',30,1,60)]:
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
        _,targets['lift_height']=dataset_name(targets,height)
    description = prompt
    if targets['left'] and targets['right']:
        description = f"Stage 1: Grasp {targets['left']} with the left hand. Stage 2: Keep holding {targets['left']} with the left hand and grasp {targets['right']} with the right hand."
    scene = ' and '.join(targets[side] for side in ('left','right') if targets[side])
    result.update(scene_id=0, scene_name='Beverage selection',
        scene_desc=f'{scene} are placed on shelves within reach of the robot.' if scene else 'Objects are placed within reach of the robot.',
        action_desc=description, step_list=[])
    return result, targets
