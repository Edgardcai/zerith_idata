import re
import math
from decimal import Decimal, InvalidOperation
from datetime import datetime
from zoneinfo import ZoneInfo

DEFAULT_PROMPT = 'Grasp Dahongpao Milk Tea with the left hand and then grasp If coconut with the right hand'
DEFAULT_PROMPT_MODE = 'tabletop'
PROMPT_MODES = ('shelf_grasp', 'tabletop', 'shelf_place')


def compose_task(prompt_mode, hand_mode, left='', right=''):
    if prompt_mode not in PROMPT_MODES:
        raise ValueError('未知的提示词配置')
    if hand_mode not in ('left', 'right', 'both'):
        raise ValueError('请选择左手、右手或双手')
    products = {'left': left, 'right': right}
    sides = ('left', 'right') if hand_mode == 'both' else (hand_mode,)
    for side in sides:
        value = products[side]
        if (not isinstance(value, str) or not value.strip() or len(value)>200
                or not value.isascii() or any(c in value for c in '/\\\x00\n\r')):
            raise ValueError('请选择有效的英文商品名称')
        products[side] = value.strip()
    left, right = products['left'], products['right']
    if prompt_mode == 'shelf_grasp':
        prompt = (f'Grasp {left} with the left hand and then grasp {right} with the right hand.'
                  if hand_mode == 'both' else f'Grasp {products[sides[0]]} with the {sides[0]} hand.')
        stages = [f'Grasp {products[side]} with the {side} hand.' for side in sides]
        if hand_mode == 'both':
            stages[1] = f'Keep holding {left} with the left hand and grasp {right} with the right hand.'
    elif prompt_mode == 'tabletop':
        places = [f'Place the item held in the {side} hand on the table.' for side in sides]
        grasps = [f'Grasp {products[side]} from the table with the {side} hand.' for side in sides]
        stages = places + grasps
        prompt = (f'Place the item held in the left hand on the table, then place the item held in the right hand on the table. '
                  f'Grasp {left} from the table with the left hand, then grasp {right} with the right hand.'
                  if hand_mode == 'both' else ' '.join(stages))
    else:
        stages = [f'Place {products[side]} on the shelf with the {side} hand.' for side in sides]
        prompt = (f'Place {left} on the shelf with the left hand, then place {right} with the right hand.'
                  if hand_mode == 'both' else stages[0])
        if hand_mode == 'both':
            stages[1] = f'Then place {right} with the right hand.'
    return {'task_name': prompt, 'stage_prompts': stages, 'subtask_num': len(stages),
            'left': left if 'left' in sides else '', 'right': right if 'right' in sides else ''}


DEFAULT_COMPOSED_PROMPT = compose_task(DEFAULT_PROMPT_MODE, 'both', 'Dahongpao Milk Tea', 'If coconut')['task_name']

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
    result = {'left': '', 'right': ''}
    for clause in re.split(r'\b(?=(?:grasp|pick up|place)\s)', prompt, flags=re.I):
        match = re.match(r'(?:grasp|pick up|place)\s+(.+?)\s+(?:from\s+the\s+table\s+|on\s+the\s+shelf\s+)?with\s+(?:the\s+)?(left|right)\s+hand', clause, flags=re.I)
        if match:
            result[match[2].lower()] = match[1].strip().rstrip('.')
    return result

def validate_task(payload):
    if not isinstance(payload, dict):
        raise ValueError('任务配置格式错误')
    composed = None
    if 'prompt_mode' in payload:
        composed = compose_task(payload['prompt_mode'], payload.get('hand_mode'), payload.get('left', ''), payload.get('right', ''))
        if 'task_name' in payload and payload['task_name'] != composed['task_name']:
            raise ValueError('提示词与选中配置不一致，请刷新页面后重试')
        if 'subtask_num' in payload:
            try:
                number = float(payload['subtask_num'])
            except (TypeError, ValueError):
                raise ValueError('阶段数与提示词配置不一致') from None
            if isinstance(payload['subtask_num'], bool) or number != composed['subtask_num']:
                raise ValueError('阶段数与提示词配置不一致')
        payload = {**payload, 'task_name': composed['task_name'], 'subtask_num': composed['subtask_num'],
                   'left': composed['left'], 'right': composed['right']}
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
    scene_description = f'{scene} are placed on shelves within reach of the robot.' if scene else 'Objects are placed within reach of the robot.'
    if composed is not None:
        description = ' '.join(f'Stage {i}: {text}' for i, text in enumerate(composed['stage_prompts'], 1))
        targets.update(prompt_mode=payload['prompt_mode'], hand_mode=payload['hand_mode'], stage_prompts=composed['stage_prompts'])
        if payload['prompt_mode'] == 'tabletop':
            scene_description = 'Items are held in the robot hands and placed on a table within reach.'
        elif payload['prompt_mode'] == 'shelf_place':
            scene_description = 'Items are held in the robot hands for placement on a shelf within reach.'
    result.update(scene_name=f"scene{result['scene_id']}",
        scene_desc=scene_description,
        action_desc=description, step_list=[])
    return result, targets
