#!/usr/bin/env python3
"""Independent data-collection console; no imports from the 8080 control project."""
import argparse
import fcntl
import json
import mimetypes
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from cameras import Cameras, NAMES
from collector import Collector
from device_monitor import DeviceMonitor
from episodes import EpisodeStore
from tasks import DEFAULT_PROMPT, parse_targets
from teleop_status import TeleopStatus

ROOT=Path(__file__).resolve().parent

class Application:
    def __init__(self,data_root='/data/zerith_data',runtime=None,start_devices=True):
        self.runtime=Path(runtime or ROOT/'runtime');self.runtime.mkdir(parents=True,exist_ok=True)
        self.csrf=secrets.token_urlsafe(32)
        self.store=EpisodeStore(self.runtime,data_root)
        self.monitor=DeviceMonitor(self.runtime)
        self.cameras=Cameras()
        self.teleop=TeleopStatus() if start_devices else TeleopStatus(self.runtime/'teleop')
        self.collector=Collector(self.store,self.monitor,self.runtime)
        self.worker=None;self.worker_log=None
        if start_devices:
            self.monitor.start()
            self.worker_log=(self.runtime/'sdk_observer.log').open('ab')
            self.worker=subprocess.Popen([str(ROOT/'runtime'/'joint_observer'),str(self.runtime/'joints.json')],stdout=self.worker_log,stderr=subprocess.STDOUT)
    def close(self):
        self.collector.close();self.cameras.close();self.monitor.close()
        if self.worker:
            self.worker.terminate()
            try:self.worker.wait(4)
            except subprocess.TimeoutExpired:self.worker.kill();self.worker.wait()
        if self.worker_log:self.worker_log.close()
        self.store.close()
    def status(self):
        return {'device':self.monitor.snapshot(),'collection':self.collector.status(),'camera':self.cameras.status(),'teleop':self.teleop.snapshot()}

class Handler(BaseHTTPRequestHandler):
    server_version='CollectionWeb/1.0'
    def log_message(self,*args):
        # Access requests are intentionally quiet; errors remain in responses/service logs.
        return
    @property
    def app(self):return self.server.app
    def json(self,value,status=200):
        data=json.dumps(value,ensure_ascii=False,allow_nan=False).encode()
        self.send_response(status);self.send_header('Content-Type','application/json; charset=utf-8');self.send_header('Content-Length',str(len(data)));self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.end_headers();self.wfile.write(data)
    def do_GET(self):
        try:
            path=urlparse(self.path).path
            if path=='/api/bootstrap':return self.json({'csrf':self.app.csrf,'default_prompt':DEFAULT_PROMPT,'depth_recording_supported':True})
            if path=='/api/status':return self.json(self.app.status())
            if path=='/api/episode-groups':return self.json({'groups':self.app.store.groups()})
            if path=='/api/episodes':
                from urllib.parse import parse_qs
                dataset=parse_qs(urlparse(self.path).query).get('dataset',[None])[0]
                return self.json({'episodes':self.app.store.list(dataset=dataset)})
            if path.startswith('/api/camera/'):
                name=path.rsplit('/',1)[1]
                if name not in NAMES:raise ValueError('未知相机')
                data=self.app.cameras.frame(name)
                self.send_response(200);self.send_header('Content-Type','image/jpeg');self.send_header('Content-Length',str(len(data)));self.send_header('Cache-Control','no-store');self.end_headers();return self.wfile.write(data)
            if path.startswith('/api/video/'):
                _,_,_,ident,name=path.split('/')
                if name not in NAMES:raise ValueError('未知相机')
                row=self.app.store.get(int(ident))
                if not row or row['state']!='completed':raise ValueError('数据不存在或尚未保存')
                directory=self.app.store.safe(row['path'])
                target=directory/'videos'/'rs'/(NAMES[name]+'.mp4')
                if target.is_symlink() or not target.resolve().is_relative_to(directory):raise ValueError('视频路径无效')
                return self.file(target,video=True)
            if path=='/':return self.file(ROOT/'static'/'index.html')
            if path.startswith('/static/'):
                target=(ROOT/'static'/path.removeprefix('/static/')).resolve()
                if not target.is_relative_to(ROOT/'static'):raise ValueError('路径无效')
                return self.file(target)
            self.json({'error':'接口不存在'},404)
        except (BrokenPipeError,ConnectionResetError):pass
        except Exception as exc:self.json({'error':str(exc)},400)
    def file(self,path,video=False):
        if not path.is_file():return self.json({'error':'文件不存在'},404)
        size=path.stat().st_size;start=0;end=size-1;status=200
        value=self.headers.get('Range')
        if video and value:
            import re
            match=re.fullmatch(r'bytes=(\d+)-(\d*)',value)
            if not match:return self.json({'error':'不支持的分段请求'},416)
            start=int(match[1]);end=min(int(match[2]) if match[2] else end,end)
            if start>end:return self.json({'error':'分段越界'},416)
            status=206
        self.send_response(status);self.send_header('Content-Type',mimetypes.guess_type(str(path))[0] or 'application/octet-stream');self.send_header('Content-Length',str(end-start+1));self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
        if video:self.send_header('Accept-Ranges','bytes')
        if status==206:self.send_header('Content-Range',f'bytes {start}-{end}/{size}')
        self.end_headers()
        with path.open('rb') as f:
            f.seek(start);remaining=end-start+1
            while remaining:
                chunk=f.read(min(remaining,256*1024))
                if not chunk:break
                self.wfile.write(chunk);remaining-=len(chunk)
    def do_POST(self):
        try:
            origin=self.headers.get('Origin')
            if origin and urlparse(origin).netloc!=self.headers.get('Host'):return self.json({'error':'请求来源不匹配'},403)
            if self.headers.get('X-Collection-Token')!=self.app.csrf:return self.json({'error':'页面连接已更新，请刷新'},403)
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=65536:raise ValueError('请求长度无效')
            body=json.loads(self.rfile.read(length))
            if not isinstance(body,dict):raise ValueError('请求必须是 JSON 对象')
            path=urlparse(self.path).path
            if path=='/api/task/parse':return self.json(parse_targets(str(body.get('prompt',''))))
            if path=='/api/task/directory':return self.json(self.app.collector.task_paths(body))
            if path=='/api/preflight':return self.json(self.app.collector.preflight(body))
            if path=='/api/session/start':return self.json(self.app.collector.start(body))
            if path=='/api/session/end':return self.json(self.app.collector.end())
            if path=='/api/teleop/config':
                return self.json(self.app.teleop.save(body,self.app.monitor.snapshot(),self.app.collector.status()))
            if path=='/api/cameras':
                if not isinstance(body.get('enabled'),bool):raise ValueError('enabled 必须为布尔值')
                return self.json(self.app.cameras.set_enabled(body['enabled']))
            if path=='/api/episode/rate':
                self.app.store.rate(int(body['id']),body['grade']);return self.json({'ok':True})
            if path=='/api/episode/delete':
                if body.get('confirm')!='delete':raise ValueError('请确认放弃本条数据')
                self.app.store.delete(int(body['id']));return self.json({'ok':True})
            return self.json({'error':'接口不存在'},404)
        except (BrokenPipeError,ConnectionResetError):pass
        except (ValueError,KeyError,TypeError) as exc:self.json({'error':str(exc)},400)
        except Exception as exc:self.json({'error':str(exc)},500)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--host',default='0.0.0.0');parser.add_argument('--port',type=int,default=8090);parser.add_argument('--data-root',default='/data/zerith_data');parser.add_argument('--runtime',default=str(ROOT/'runtime'));args=parser.parse_args()
    runtime=Path(args.runtime);runtime.mkdir(parents=True,exist_ok=True)
    lock=(runtime/'server.lock').open('w')
    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:raise SystemExit('采集网站已运行')
    server=ThreadingHTTPServer((args.host,args.port),Handler);server.daemon_threads=True
    app=Application(args.data_root,runtime);server.app=app
    def shutdown(*_):threading.Thread(target=server.shutdown,daemon=True).start()
    signal.signal(signal.SIGTERM,shutdown);signal.signal(signal.SIGINT,shutdown)
    print(f'Collection website: http://{args.host}:{args.port}',flush=True)
    try:server.serve_forever(poll_interval=.2)
    finally:server.server_close();app.close();lock.close()

if __name__=='__main__':main()
