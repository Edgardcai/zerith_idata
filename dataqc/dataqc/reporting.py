"""Lossless issue lists and concise, measured descriptions for reviewers."""
import re
from .motion import RULE_VERSION

STATUS={'pass':'正常','warn':'预警','fail':'失败','review':'待复核','na':'未检查','info':'信息'}


def number(value,digits=3):
    if not isinstance(value,(int,float)):return '未提供'
    text=f'{value:.{digits}f}'
    # Do not round a failing measurement into an apparently passing boundary.
    if float(text)!=value and any(abs(float(text))==round(limit,digits) for limit in (.0001,.02,.05,.1,.8)):
        return f'{value:.8f}'.rstrip('0').rstrip('.')
    return text


def human(text):
    text=str(text).replace('State','状态').replace('Action','指令')
    for old,new in [('cam_high','头部'),('cam_left_wrist','左腕'),('cam_right_wrist','右腕'),
                    ('waist_pitch','腰部俯仰'),('waist_yaw','腰部偏航'),('head_yaw','头部偏航'),('head_pitch','头部俯仰')]:
        text=text.replace(old,new)
    text=re.sub(r'\bleft_arm_(\d+)\b',r'左臂关节\1',text)
    text=re.sub(r'\bright_arm_(\d+)\b',r'右臂关节\1',text)
    text=re.sub(r'\bleft_joint_(\d+)\b',r'左臂关节\1',text)
    text=re.sub(r'\bright_joint_(\d+)\b',r'右臂关节\1',text)
    text=re.sub(r'\bstate\b','状态',text);text=re.sub(r'\baction\b','指令',text)
    return text


def presentation(report):
    from .quality_policy import display_report
    report=display_report(report)
    raw=report.get('raw_report') or (report if 'version' in report else {})
    visual=report.get('visual') or {}
    checks=raw.get('checks') or report.get('checks') or []
    n=raw.get('frames') or report.get('summary',{}).get('frames') or 0
    items=[]
    def add(key,label,status,text,frames=(),standard='',source='基础质检'):
        frames=sorted(set(int(f) for f in frames if isinstance(f,int) and not isinstance(f,bool) and f>=0))
        status_label=({'category':'未启用','motion_vlm':'无法判断','gripper_feedback':'无独立反馈'}.get(key,'未提供/不适用') if status=='na' else STATUS.get(status,status))
        items.append(dict(key=key,label=human(label),status=status,status_label=status_label,
                          text=human(text),frames=frames,standard=standard,source=source,
                          problem=status in ('fail','warn','review')))
    arm_keys={c.get('key') for c in checks if c.get('key','').startswith('arm_state_')}
    for c in checks:
        k=c.get('key') or c.get('detail',{}).get('shared_key') or c.get('name','')
        if k=='lift_height':continue
        label=c.get('label',k);status=c.get('status','na');detail=c.get('detail',{})
        d=detail if isinstance(detail,dict) else {};frames=d.get('bad_frames',[])
        issues=d.get('issues') or d.get('errors') or []
        if k=='motion' and arm_keys:continue
        if k in ('motion_vlm_review','yolo_matching','visual_matching'):continue
        if k.startswith('posture_') and d.get('channels'):
            for channel in d['channels']:
                add(k,label+' · '+channel['joint'],channel['status'],
                    f"均值 {number(channel.get('mean_rad'))} rad；P1/P99 {number(channel.get('q01_rad'))}/{number(channel.get('q99_rad'))} rad",channel.get('bad_frames',[]),standard='均值及P1/P99均在±0.020 rad内')
            continue
        if k=='lift_height' and 'expected_m' in d:
            reference=('目标高度' if d.get('policy')=='episode_target' else '首帧指令基准')+f" {number(d['expected_m'])} m"
            origin=d.get('source') or d.get('field') or '历史目录参考（请重新质检）'
            for name,v in d.get('channels',{}).items():
                bad=v.get('bad_frames',[])
                text=f"{reference}；实测 {number(v.get('min_m'))}～{number(v.get('max_m'))} m；最大偏差 {number(v.get('max_deviation_m'))} m；超差 {len(bad)} 帧"
                add(k,('升降柱目标' if d.get('policy')=='episode_target' else '升降柱高度保持')+' · '+('状态' if name=='state' else '指令'),
                    'fail' if bad else 'pass',text,bad,'最大偏差≤0.020 m',origin)
            if d.get('policy')!='episode_target':add(k,'升降柱参考范围','info','未提供独立任务目标；仅验证相对首帧指令的高度保持',source=origin)
            continue
        if k=='gripper_sequence':
            for hand,v in d.get('channels',{}).items():
                expected=v.get('expected_closures');ac=v.get('action_close_frames',[]);sc=v.get('state_close_frames',[])
                feedback=not any(x.get('key')=='gripper_feedback' and x.get('status')=='na' for x in checks)
                text=f"指令闭合 {len(ac)} 次，要求 {expected} 次"+(f"；状态闭合 {len(sc)} 次" if feedback else '；无独立物理反馈')
                add(k,('左' if hand=='left' else '右')+'夹爪闭合次数','warn' if len(ac)!=expected or (feedback and len(sc)!=expected) else 'pass',text,ac+sc,'双手各1次；单手任务非操作手0次')
            for issue in issues:add(k,label,'review' if status=='warn' else status,issue,frames)
            if not issues:add(k,'夹爪阶段与反馈','pass','闭合位于对应操作阶段'+('，可用反馈延迟正常' if not any(x.get('key')=='gripper_feedback' for x in checks) else '；无独立反馈'),standard='反馈相对指令允许−2～15帧')
            continue
        standard='';text=''
        if k in ('state','action'):
            text=f"形状 {d.get('shape','未提供')}；每帧要求23维"
        elif k=='finite':
            invalid=d.get('invalid',{})
            for kind,positions in invalid.items():
                if positions:
                    add(k,label,'fail',f"{kind} 含 {len(positions)} 个NaN/Inf；位置（帧,通道）="+str(positions),[p[0] for p in positions])
            if any(invalid.values()):continue
            text='状态、指令、时间戳均无NaN/Inf' if status=='pass' else '包含NaN/Inf；旧报告未记录具体位置，请重新质检'
        elif k=='timestamps':
            for gap in d.get('gaps',[]):
                add(k,label,'warn',f"第{gap['previous_frame']}→{gap['frame']}帧间隔 {number(gap['interval_seconds']*1000,1)} ms",[gap['frame']],'间隔>0且≤100 ms')
            if d.get('gaps'):continue
            text=f"最大间隔 {number((d.get('max_gap_seconds') or 0)*1000,1)} ms；异常 {len(d.get('gaps',[]))} 处" if 'max_gap_seconds' in d else str(d)
            standard='间隔>0且≤100 ms'
        elif k=='fps':text=f"实际 {number(d.get('actual'),2)} FPS";standard='≥29 FPS'
        elif k=='duration':text=f"实际 {number(d.get('seconds'),2)} s";standard='2～40 s'
        elif k.startswith('arm_'):
            text=f"最大单步变化 {number(d.get('max_step_rad'))} rad；跳变 {len(d.get('jumps',[]))} 处";standard='≤0.800 rad'
            for jump in d.get('jumps',[]):
                add(k,label,'warn',f"{jump['joint']} 第{jump['frame']}帧变化 {number(jump['delta_rad'])} rad（{number(jump['previous'])}→{number(jump['value'])}）",[jump['frame']],standard)
            if d.get('jumps'):continue
        elif k=='motion':text=f"最大单步变化 {number(d.get('max_step'))} rad";standard='≤0.800 rad'
        elif k=='joint':text=f"平均单步变化 {number(d.get('mean_rad'),6)} rad；最大 {number(d.get('max_rad'))} rad";standard='平均≥0.0001 rad；最大≤0.8 rad'
        elif k=='stationary':
            standard=f"最长≤{d.get('threshold','未提供')}帧"
            for span in d.get('intervals',[]):add(k,label,'review',f"第{span['start']}～{span['end']-1}帧，连续静止{span['frames']}帧",[span['start']],standard)
            if d.get('intervals'):continue
            text=f"最长连续静止 {d.get('max_frames','未提供')} 帧"
        elif k=='gripper':
            text='；'.join(f"{'左' if hand=='left' else '右'}爪：状态变化幅度 {number(v.get('state_range'))}，指令 {number(v.get('action_range'))}" for hand,v in d.items() if isinstance(v,dict))
            standard='至少一侧变化幅度>0.05'
        elif k=='prompt':text='原始任务：'+str(d.get('original','未提供'));standard='双手先左后右／单左手／单右手模板'
        elif k=='metadata':text='任务与左右手目标一致' if not issues else ''
        elif k=='stages':text=f"阶段结束边界 {d.get('boundaries',[])}；总帧数 {d.get('frames','未提供')}";standard='边界递增，连续覆盖全部帧'
        elif k=='repair_records':text=(f"最长连续补帧/复用 {d.get('maximum_consecutive')} 帧" if status!='na' else '未提供补帧/复用记录');standard='≤2帧'
        elif k.startswith(('video_','hdf5_','depth_')):
            text=(f"已解码 {d.get('decoded_frames')} / {d.get('expected')} 帧" if k.startswith('video_') else f"{'图像缺失' if 'missing' in frames else f'解码异常 {len(frames)} 帧'}；应与轨迹 {n} 帧一致")
            if status=='na':text=d.get('note','此格式不包含该通道')
        elif k.startswith('quality_'):
            cam=k.removeprefix('quality_');v=raw.get('visual',{}).get(cam,{})
            for kind,name,key,limit in [('dark_or_overexposed','过暗/过曝','unusable_frames',.05),('blur','模糊','blur_frames',.2)]:
                count=d.get(key,0);ratio=count/n if n else 0
                add(k,label+' · '+name,'warn' if count>n*limit else 'pass',f"{count}/{n}帧（{ratio:.1%}）",v.get(kind,[]),f'异常占比≤{limit:.0%}')
            if d.get('possible_freeze'):add(k,label+' · 疑似重复画面','info',f"{d['possible_freeze']}帧；仅供核查，不单独降级",v.get('possible_freeze',[]))
            continue
        elif k=='schema':text='文件结构、帧数和采集模式符合要求' if status=='pass' else str(detail)
        elif k=='gripper_feedback':text=d.get('note','未提供独立夹爪反馈')
        if issues:
            for issue in issues:add(k,label,'review' if d.get('requires_review') else status,issue,frames,standard)
        else:add(k,label,'review' if k=='prompt' and status=='fail' else status,text or d.get('note') or d.get('error') or d.get('shared_text') or str(detail),frames,standard)
        if k=='timestamps' and d.get('timebase'):add(k,'时间轴来源','info',d.get('note',''),source=d['timebase'])
    motion=visual.get('motion_review') or {}
    if motion:
        add('motion_execution','动作复核执行状态','info',{'cached':'复用有效历史结果；本次未重复请求模型','completed':'本轮已完成复核'}.get(motion.get('execution'),'历史报告未记录是否命中缓存'),source='执行状态')
    for finding in motion.get('findings',[]):
        frames=[int(e.split(':')[1]) for e in finding.get('evidence_ids',[]) if re.fullmatch(r'frame:\d+',e)]
        status={'pass':'pass','suspected':'review','not_observable':'na'}.get(finding.get('status'),'review')
        reason=finding.get('reason','未提供原因');criterion=finding.get('criterion','动作复核')
        if status=='pass' and reason.startswith('批量复核通过'):
            if criterion=='指标一致性':reason='模型未发现额外指标矛盾；基础超限与预警分别列在问题清单中'
            elif criterion=='动作连续性':
                steps=[c.get('detail',{}).get('max_step_rad',0) for c in checks if c.get('key','').startswith('arm_')]
                reason=f'已检查左右臂状态与指令；最大单步变化 {number(max(steps))} rad，上限0.800 rad' if steps else '轨迹复核未发现额外连续性问题；具体数值见基础检查'
            elif criterion=='夹爪配合':
                ch=next((c.get('detail',{}).get('channels',{}) for c in checks if c.get('key')=='gripper_sequence'),{})
                reason='；'.join(f"{'左' if h=='left' else '右'}爪指令闭合{len(v.get('action_close_frames',[]))}次，要求{v.get('expected_closures')}次" for h,v in ch.items()) or reason
            elif criterion=='阶段完成情况':reason='已记录阶段覆盖完整、操作顺序符合任务；不代表实际物理任务成功'
        add('motion_vlm',criterion,status,reason,frames,source='Terra · 数值与采样轨迹')
    cat=visual.get('category_review') or {};gate=cat.get('yolo') or visual.get('yolo') or {}
    for hand in gate.get('hands',[]):
        add('yolo','YOLO · '+('左手' if hand.get('hand')=='left' else '右手'),'pass' if hand.get('status')=='pass' else 'review',hand.get('reason',''),
            [m['frame'] for m in hand.get('moments',[])],standard='每操作手≥2/3时刻匹配',source='YOLO')
    for hand in cat.get('hands',[]):
        refs=hand.get('evidence_ids',[]);frames=[int(e.rsplit(':',1)[1]) for e in refs if re.search(r':\d+$',e)]
        add('category','类别复核 · '+('左手' if hand.get('hand')=='left' else '右手'),{'pass':'pass','fail':'review'}.get(hand.get('status'),'review'),hand.get('reason',''),frames,source='Terra · 定点图像')
    if cat.get('status')=='skipped':add('category','类别识别','na','未启用YOLO与图像类别复核；不影响独立动作指标复核')
    for branch,error in visual.get('errors',{}).items():add(branch,'动作复核' if branch=='motion' else '类别复核','review','执行未完成：'+str(error).replace('视觉检查未完成：','模型请求未完成：'),source='执行状态')
    if visual.get('error'):add('analysis','模型复核','review','执行未完成：'+str(visual['error']).replace('视觉检查未完成：','模型请求未完成：'),source='执行状态')
    if report.get('review_required') and not any(i['problem'] for i in items):add('review','待复核原因','review',report.get('reason') or '历史报告未保留具体原因，请重新质检')
    outdated=bool(raw) and raw.get('version')!=RULE_VERSION
    if outdated:add('rules','报告版本','info','历史报告已按当前分级规则展示；升降柱不检查，人工等级保留。重新质检可更新完整报告',source='报告状态')
    items.sort(key=lambda i:{'fail':0,'review':1,'warn':2,'na':4,'info':5,'pass':6}.get(i['status'],6))
    problems=[i for i in items if i['problem']]
    return dict(version='readable_qc_v1',items=items,problem_count=len(problems),
                counts={s:sum(i['status']==s for i in items) for s in STATUS},outdated=outdated,
                summary='；'.join(i['label']+'：'+i['text'] for i in problems[:2]) if problems else '已完成的检查未发现异常' if items else '尚无质检报告',
                task=raw.get('task',''),frames=n)
