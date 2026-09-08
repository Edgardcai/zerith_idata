#!/usr/bin/env python3
"""Read-only 8090 + trial status recorder; no command publisher."""
import argparse
import datetime
import json
import time
from preflight import ROOT, snapshot


def main():
    p=argparse.ArgumentParser();p.add_argument('--seconds',type=float,default=300)
    args=p.parse_args()
    if not 0 < args.seconds <= 3600:p.error('--seconds must be in (0, 3600]')
    output=ROOT/'runtime'/('trial_'+datetime.datetime.now().strftime('%Y%m%d_%H%M%S')+'.jsonl')
    start=time.monotonic();count=0;ready_count=0;max_abs={}
    with output.open('x') as log:
        while time.monotonic()-start<args.seconds:
            now=time.time()
            try:
                state=snapshot()
                try:
                    policy=json.loads((ROOT/'runtime/status.json').read_text())
                    # The status writer can update while the HTTP request runs.
                    # Compare against read completion, not request start time.
                    if not 0 <= time.time()-policy['timestamp'] < 2:policy={'ready':False,'reason':'stale'}
                except (OSError,ValueError,KeyError):policy={'ready':False,'reason':'not_running'}
                row={'timestamp':now,'device':state['device'],'phase':state['collection']['phase'],'zero_lock':policy}
                if policy.get('ready'):
                    ready_count+=1
                    for j in state['device']['joints']:
                        if j['motor_id'] in (3,4,5,6) and j.get('ok') and isinstance(j.get('value'),(int,float)):
                            max_abs[j['label']]=max(max_abs.get(j['label'],0),abs(j['value']))
            except Exception as e:row={'timestamp':now,'error':str(e)}
            log.write(json.dumps(row,ensure_ascii=False,allow_nan=False)+'\n');log.flush();count+=1
            time.sleep(min(.25,max(0,args.seconds-(time.monotonic()-start))))
    result={'file':str(output),'samples':count,'ready_samples':ready_count,'max_abs_rad_while_ready':max_abs}
    output.with_suffix('.summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
