"""Exercise fixed-pose settings against temporary files; no robot connections."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from http.server import ThreadingHTTPServer
from playwright.sync_api import sync_playwright
from app import Application, Handler
from episodes import atomic_json
from teleop_status import TeleopStatus


with tempfile.TemporaryDirectory() as temp:
    root=Path(temp)
    (root/'data').mkdir()
    app=Application(root/'data',root/'runtime',start_devices=False)
    (app.teleop.root/'runtime').mkdir(parents=True)
    applied=TeleopStatus.validate({'lift_enabled':True,'lift_height_m':.8})
    atomic_json(app.teleop.root/'config.json',applied)
    state={'version':'collection-1.1-fixed-pose','pose_config_supported':True,
           'active':False,'ready':False,'pid':123,'config':applied,'target_rad':[0,0,0,0],
           'actual_rad':[0,0,.0002,.024], 'calibration_seq':0,
           'operator':{'initialized':False,'calibrated':False,'state':'STOP_TELEOP'},'warnings':[]}
    stop=threading.Event()
    def publish():
        while not stop.is_set():
            atomic_json(app.teleop.root/'runtime/status.json',{**state,'timestamp':time.time()})
            stop.wait(.1)
    publisher=threading.Thread(target=publish,daemon=True);publisher.start()
    app.monitor.snapshot=lambda:{'checks':[{'key':'init','label':'机器人就绪','ok':state['active'],
        'detail':'初始化完成' if state['active'] else '反初始化完成'}],
        'ready':False,'joints':[],'battery':80,'vr_connected':True}
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.app=app
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path=str(Path.home()/'.cache/ms-playwright/chromium-1234/chrome-linux64/chrome'),
                headless=True,args=['--no-sandbox'])
            page=browser.new_page(viewport={'width':1440,'height':1100})
            errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
            page.goto(f'http://127.0.0.1:{server.server_port}')
            page.wait_for_function("!document.querySelector('#fixedWaistPitch').disabled")
            expected={'fixedWaistPitch':'.5','fixedWaistYaw':'-.2','fixedHeadYaw':'.15','fixedHeadPitch':'-.1'}
            for field,value in expected.items():page.locator('#'+field).fill(value)
            page.locator('#saveTeleop').click()
            page.wait_for_function("document.querySelector('#poseApplied').textContent.includes('腰 pitch 0.500')")
            saved=app.teleop.config()
            assert [saved[k] for k in ('waist_pitch_rad','waist_yaw_rad','head_yaw_rad','head_pitch_rad')]==[.5,-.2,.15,-.1]
            assert saved['lift_height_m']==.8
            page.reload();page.wait_for_function("document.querySelector('#fixedWaistPitch').value==='0.5'")
            assert page.locator('#fixedHeadYaw').input_value()=='0.15'
            page.locator('#fixedWaistPitch').fill('1.5');page.locator('#saveTeleop').click()
            assert page.locator('#fixedWaistPitch').evaluate('(el)=>!el.validity.valid')
            assert app.teleop.config()==saved
            page.locator('#fixedWaistPitch').fill('.5')
            result=page.evaluate("""async()=>{const b=await (await fetch('/api/bootstrap')).json();
                const r=await fetch('/api/teleop/config',{method:'POST',headers:{'Content-Type':'application/json','X-Collection-Token':b.csrf},
                body:JSON.stringify({lift_enabled:true,lift_height_m:.8,head_pitch_rad:.6})});return r.status;}""")
            assert result==400 and app.teleop.config()==saved
            app.collector.phase='waiting'
            page.wait_for_function("document.querySelector('#fixedWaistPitch').disabled")
            app.collector.phase='idle'
            page.wait_for_function("!document.querySelector('#fixedWaistPitch').disabled")
            state.update(active=True,ready=True,target_rad=[0,0,0,0])
            state['operator'].update(initialized=True,calibrated=True,state='DECOUPLED')
            page.wait_for_function("document.querySelector('#teleopState').textContent==='遥操作中'")
            assert page.locator('#fixedWaistPitch').is_disabled()
            assert '当前保持：腰 pitch 0.000' in page.locator('#poseApplied').inner_text()
            page.set_viewport_size({'width':390,'height':844})
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            page.locator('.teleop-panel').screenshot(path='/tmp/fixed-pose-browser.png')
            assert not errors,errors
            assert app.collector.session is None
            browser.close()
            print(json.dumps({'save_reload':True,'four_axis_order':True,'range_rejection':True,
                'active_and_session_edit_guards':True,'applied_vs_saved':True,'mobile_overflow':False,
                'robot_connections':False,'page_errors':errors}))
    finally:
        stop.set();publisher.join();server.shutdown();worker.join();server.server_close();app.close()
