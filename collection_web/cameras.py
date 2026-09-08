"""On-demand RGB preview through the vendor camera service, without motor control."""
import sys
import threading
import time
from pathlib import Path
import cv2

NAMES={'head':'cam_high','left':'cam_left_wrist','right':'cam_right_wrist'}

class Cameras:
    def __init__(self,target='127.0.0.1:50051',factory=None):
        self.target=target;self.factory=factory;self.lock=threading.RLock();self.operation=threading.Lock()
        self.client=None;self.enabled=False;self.error=None;self.frames={};self.stop=threading.Event();self.thread=None
    def set_enabled(self,enabled):
        with self.operation:
            if enabled==self.enabled:return self.status()
            if not enabled:
                self.stop.set()
                if self.thread:self.thread.join(3)
                client=self.client;self.client=None;self.enabled=False
                if client:client.stop()
                with self.lock:self.frames={}
                return self.status()
            if self.factory is None:
                sys.path.insert(0,'/home/robot/H1_SDK_1.3.9/camera_sdk_python')
                from camera_client import CameraClient
                factory=CameraClient
            else:factory=self.factory
            client=factory(grpc_target=self.target,connect_timeout=8,enable_depth=False)
            try:
                client.start()
                self.client=client;self.error=None;self.stop=threading.Event();self.enabled=True
                self.thread=threading.Thread(target=self._loop,daemon=True);self.thread.start()
            except Exception:
                client.stop();raise
            return self.status()
    def _loop(self):
        seen={}
        while not self.stop.wait(.1):
            try:
                for logical,name in NAMES.items():
                    result=self.client.get_latest_frame('rs/'+name)
                    if result is None:continue
                    frame,stamp=result
                    if frame is None or seen.get(logical)==stamp:continue
                    ok,jpeg=cv2.imencode('.jpg',frame,[cv2.IMWRITE_JPEG_QUALITY,75])
                    if ok:
                        with self.lock:self.frames[logical]=(jpeg.tobytes(),time.monotonic(),frame.shape[1],frame.shape[0])
                        seen[logical]=stamp
            except Exception as exc:
                with self.lock:self.error=str(exc)
    def frame(self,name):
        with self.lock:value=self.frames.get(name)
        if not value or time.monotonic()-value[1]>3:raise ValueError('相机尚未出图或画面已超时')
        return value[0]
    def status(self):
        with self.lock:
            return {'enabled':self.enabled,'error':self.error,'streams':{k:{'fresh':time.monotonic()-v[1]<3,'age_s':round(time.monotonic()-v[1],2),'width':v[2],'height':v[3]} for k,v in self.frames.items()}}
    def close(self):self.set_enabled(False)
