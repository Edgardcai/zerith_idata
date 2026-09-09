"""Read finalized datasets; write diagnostics only beside this script."""
import json
import re
import sys
from pathlib import Path
import h5py
import numpy as np

sys.path.insert(0, '/home/robot/control')
from web_control.replay_timing import resample_recorded_frames

OUT = Path(__file__).resolve().parent
DATA = Path('/data/zerith_data')
rows = []
for dataset in ('DailyCGrapeJuice_DailyCOrangeJuice_0.4', 'Pepsi_DailyCOrangeJuice2'):
    for path in sorted((DATA/dataset).glob('*/episode.hdf5')):
        log = (path.parent/'collection.log').read_text()
        if '采集流结束' not in log:
            print('SKIP unfinished', path)
            continue
        group = ('reference' if dataset.startswith('Pepsi') else
                 'new' if int(path.parent.name.split('_')[-1]) >= 5 else 'before')
        with h5py.File(path, 'r') as f:
            raw_t = f['timestamp/t'][:]
            t = (raw_t-raw_t[0])/(1000 if np.median(raw_t)>1e11 else 1)
            dt = np.diff(t)
            parallel = [(float(ms),int(n)) for ms,n in re.findall(
                r'\[StorageParallel\] 批次总耗时 ([\d.]+) ms .*?帧数 (\d+)',log)]
            full = [ms for ms,n in parallel if n == 10]
            if not parallel:
                full = [float(v) for v in re.findall(r'批次写入完成 \| 帧数: 10 .*?耗时: ([\d.]+)ms',log)]
            queues = [int(v) for v in re.findall(r'queue=(\d+)/30',log)]
            row = dict(group=group,episode=path.parent.name,frames=len(t),duration_s=float(t[-1]),
                       hz=float((len(t)-1)/t[-1]),dt_median_ms=float(np.median(dt)*1000),
                       dt_p99_ms=float(np.percentile(dt,99)*1000),dt_max_ms=float(dt.max()*1000),
                       gaps50=int((dt>.05).sum()),gaps100=int((dt>.1).sum()),duplicates=int((dt==0).sum()),
                       backwards=int((dt<0).sum()),blocking=log.count('producer 入队被阻塞'),
                       parallel_active=bool(parallel),queue_max=max(queues,default=0),
                       batch_median_ms=float(np.median(full)),batch_p95_ms=float(np.percentile(full,95)),
                       batch_max_ms=max(full),batch_over_budget=sum(v>1000/3 for v in full),
                       error_lines=[s for s in log.splitlines() if ' - ERROR - ' in s],
                       gap_locations=[dict(frame=int(i+1),t_s=float(t[i+1]),dt_ms=float(dt[i]*1000)) for i in np.where(dt>.05)[0]])
            for source,key in [('action','action'),('state','observation/state')]:
                x=f[key+'/arm/position'][:]
                packed=np.column_stack((x[:,:7],np.zeros(len(x)),x[:,7:],np.zeros(len(x))))
                y,rate,timing=resample_recorded_frames(packed,raw_t,30,100000)
                y=y[:,[0,1,2,3,4,5,6,8,9,10,11,12,13,14]]
                d=np.diff(x,axis=0); dy=np.diff(y,axis=0)
                step=np.max(np.abs(d),axis=1)
                largest=np.argsort(step)[-5:][::-1]
                speed=np.abs(d[dt>0]/dt[dt>0,None])
                row[source]=dict(finite=bool(np.isfinite(x).all()),raw_max_step_deg=float(np.rad2deg(step.max())),
                    replay_max_step_deg=float(np.rad2deg(np.abs(dy).max())),
                    replay_step_p99_deg=float(np.percentile(np.rad2deg(np.max(np.abs(dy),axis=1)),99)),
                    replay_second_diff_rms_deg=float(np.rad2deg(np.sqrt(np.mean(np.diff(y,n=2,axis=0)**2)))),
                    peak_speed_deg_s=float(np.rad2deg(speed.max())),
                    largest_steps=[dict(frame=int(i+1),t_s=float(t[i+1]),dt_ms=float(dt[i]*1000),
                        joint=int(np.argmax(np.abs(d[i]))),step_deg=float(np.rad2deg(step[i]))) for i in largest])
                for body in ('waist','head'):
                    b=f[key+'/'+body+'/position'][:]
                    row[source][body+'_range']=np.ptp(b,axis=0).tolist()
                    row[source][body+'_median']=np.median(b,axis=0).tolist()
            rows.append(row)
(OUT/'metrics.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2)+'\n')
for r in rows:
    if r['group']!='reference':
        print(json.dumps({k:r[k] for k in ['group','episode','frames','duration_s','hz','dt_max_ms','gaps50','gaps100','duplicates','blocking','queue_max','batch_median_ms','batch_p95_ms','batch_max_ms','parallel_active']}))
        print(' arm:',json.dumps({s:{k:r[s][k] for k in ['raw_max_step_deg','replay_max_step_deg','replay_second_diff_rms_deg','peak_speed_deg_s']} for s in ['action','state']}))
for group in ('before','new','reference'):
    rs=[r for r in rows if r['group']==group]
    print('GROUP',group,'n',len(rs),'median',json.dumps({k:float(np.median([r[k] for r in rs])) for k in ['hz','dt_max_ms','gaps50','blocking','batch_median_ms']}))
    for source in ('action','state'):
        print(source,json.dumps({k:[float(v) for v in np.percentile([r[source][k] for r in rs],[0,50,100])] for k in ['replay_max_step_deg','replay_second_diff_rms_deg','peak_speed_deg_s']}))
