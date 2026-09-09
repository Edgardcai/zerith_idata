import concurrent.futures
import copy
import json
import math
import shutil
import threading
import time
import urllib.request
from pathlib import Path
from protocol import Vendor

class DeviceMonitor:
    def __init__(self, runtime, target='127.0.0.1:50051', http='http://127.0.0.1:25120'):
        self.runtime, self.target, self.http = Path(runtime), target, http
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.values = {}
        self.vendor = Vendor(target)
        self.threads = []

    def start(self):
        for target in (self._devices, self._http):
            t = threading.Thread(target=target, daemon=True); t.start(); self.threads.append(t)

    def close(self):
        self.stop_event.set(); self.vendor.close()
        for t in self.threads: t.join(3)

    def _put(self, key, data=None, error=None):
        with self.lock: self.values[key] = {'data':data,'error':error,'received':time.monotonic()}

    def _devices(self):
        while not self.stop_event.is_set():
            stream = None
            try:
                stream = self.vendor.devices(timeout=30)
                for message in stream:
                    self._put('device', json.loads(message.json_data))
                    if self.stop_event.is_set(): break
            except Exception as exc:
                if not self.stop_event.is_set(): self._put('device', error=str(exc))
            finally:
                if stream: stream.cancel()
            self.stop_event.wait(.5)

    def _fetch(self, name):
        try:
            with urllib.request.urlopen(self.http+'/'+name, timeout=1.5) as response:
                self._put(name, json.load(response))
        except Exception as exc: self._put(name,error=str(exc))

    def _http(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            while not self.stop_event.is_set():
                list(pool.map(self._fetch,['motor','communication_board','system_init','control_mode','battery']))
                self.stop_event.wait(1)

    def snapshot(self):
        with self.lock: values = copy.deepcopy(self.values)
        now = time.monotonic()
        def fresh(key):
            v=values.get(key,{})
            d=v.get('data')
            return isinstance(d,dict) and not v.get('error') and now-v.get('received',0)<4 and d.get('fresh',True) is not False
        def data(key): return values.get(key,{}).get('data') or {}
        device=data('device'); vr=device.get('others',{}).get('vr')
        vr_ok = fresh('device') and isinstance(vr,dict) and all(k in vr for k in ['head_world','left_joystick_world','right_joystick_world'])
        motor_items=[item for group in data('motor').values() if isinstance(group,list) for item in group if isinstance(item,dict)]
        bad=[f"{m.get('name','电机')}: {m.get('code', '?')}" for m in motor_items if m.get('code')!=0 or m.get('status')!='ok']
        motor_ok=fresh('motor') and len(motor_items)==23 and not bad
        comm=data('communication_board'); codes=comm.get('codes',[])
        camera=device.get('camera',{}); camera_ok=fresh('device') and all(camera.get('rs/'+c)=='正常' for c in ['cam_high','cam_left_wrist','cam_right_wrist'])
        checks=[
            {'key':'service','label':'采集服务','ok':fresh('device'),'detail':'状态流已连接' if fresh('device') else '设备状态流未连接或已超时'},
            {'key':'motors','label':'电机','ok':motor_ok,'detail':'23 个电机正常' if motor_ok else '；'.join(bad) or '电机反馈不完整或已超时'},
            {'key':'vr','label':'Meta Quest / VR','ok':vr_ok,'detail':'头显与双手柄已接入' if vr_ok else '未收到有效 VR 位姿'},
            {'key':'communication','label':'通信','ok':fresh('communication_board') and len(codes)==5 and all(x==0 for x in codes),'detail':'通信板状态检查'},
            {'key':'cameras','label':'相机','ok':camera_ok,'detail':'头部、左腕、右腕' if camera_ok else '相机状态异常或超时'},
            {'key':'mode','label':'遥操作模式','ok':fresh('control_mode') and data('control_mode').get('code')==0,'detail':data('control_mode').get('text','状态未知')},
            {'key':'init','label':'机器人就绪','ok':fresh('system_init') and data('system_init').get('system_init')==2,'detail':data('system_init').get('text','状态未知')},
        ]
        battery=data('battery').get('percent') if fresh('battery') else None
        # Display the reported charge without imposing a collection percentage threshold.
        if battery is not None:checks.append({'key':'battery','label':'电量','ok':True,'detail':f'{battery}%'})
        else:checks.append({'key':'battery','label':'电量','ok':False,'detail':'电量状态未知'})
        joints=[]
        try:
            j=json.loads((self.runtime/'joints.json').read_text())
            if time.time()-j['timestamp']<3:
                joints=j.get('joints',[])
        except (OSError,ValueError,KeyError): pass
        return {'checks':checks,'ready':all(c['ok'] for c in checks),'battery':battery,'joints':joints,
            'camera_report':camera,'vr_connected':vr_ok,'last_error':values.get('device',{}).get('error')}
