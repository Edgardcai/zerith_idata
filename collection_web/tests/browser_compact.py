"""Compact layout, record rating and polling regression on isolated fixtures."""
import json,sys,tempfile,threading,time
from pathlib import Path
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from datetime import datetime
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import Application,Handler
from tasks import validate_task
from test_core import episode
from playwright.sync_api import sync_playwright

with tempfile.TemporaryDirectory() as tmp,patch('tasks.datetime') as clock:
    clock.now.return_value=datetime(2026,9,14)
    root=Path(tmp)/'data';root.mkdir()
    app=Application(root,Path(tmp)/'runtime',start_devices=False)
    scene=app.collector.apply_scene({'task_id':20260914,'scene_id':1});dataset=Path(scene['dataset'])
    cfg,targets=validate_task({'task_id':20260914,'scene_id':1,'task_name':'Grasp Yili Peach Yogurt with the left hand and then grasp Coca-Cola with the right hand.'})
    baseline=app.store.new_session('fixture',cfg,targets,dataset)
    for i in range(1,9):episode(dataset,f'{i:032x}')
    session={'id':'fixture','started':time.time(),'dataset':str(dataset),'baseline':baseline,'config':cfg,'targets':targets}
    app.store.observe(session);app.store.set_session('fixture','closed')
    app.collector.session=session;app.collector.phase='closed'
    snapshot=app.monitor.snapshot();snapshot.update(battery=86,vr_connected=True,ready=True)
    for check in snapshot['checks']:check['ok']=True
    app.monitor.snapshot=lambda:snapshot
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.app=app
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch(executable_path='/home/robot/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome',headless=True,args=['--no-sandbox'])
            page=browser.new_page(viewport={'width':1440,'height':1100});errors=[];requests=[]
            page.on('pageerror',lambda e:errors.append(str(e)))
            page.on('request',lambda r:requests.append(r.url))
            page.goto(f'http://127.0.0.1:{server.server_port}')
            page.wait_for_selector('[data-id="8"][data-grade="A"].selected')
            assert not page.locator('.telemetry').evaluate('e=>e.open')
            assert not page.locator('#taskOptions').evaluate('e=>e.open')
            assert page.locator('.episodes-card').bounding_box()['y']<720
            before=len([u for u in requests if '/api/episodes?' in u])
            page.wait_for_timeout(3200)
            after=len([u for u in requests if '/api/episodes?' in u])
            assert after==before,(before,after)
            assert any('/api/status?compact=1' in u for u in requests)
            # While a session is open, saved episodes can still be rated.
            app.collector.phase='waiting'
            page.wait_for_function("document.querySelector('#sceneId').disabled")
            for grade in ('B','F','A'):
                page.locator(f'[data-id="8"][data-grade="{grade}"]').click()
                page.wait_for_selector(f'[data-id="8"][data-grade="{grade}"].selected')
                page.wait_for_function("!document.querySelector('[data-id=\"8\"][data-grade=A]').disabled")
                assert app.store.list()[0]['grade']==grade
            app.collector.phase='closed'
            page.wait_for_function("!document.querySelector('#sceneId').disabled")
            # A change arriving from another collector browser also refreshes.
            app.store.rate(8,'B')
            page.wait_for_selector('[data-id="8"][data-grade="B"].selected')
            page.locator('[data-review="8"]').click()
            assert page.locator('#review').is_visible()
            page.locator('#closeReview').click()
            page.locator('#taskOptions summary').click()
            assert page.locator('#liftHeight').is_visible()
            page.locator('#taskOptions summary').click()
            out=Path('/home/robot/collection_web/runtime/compact_ui_verification');out.mkdir(exist_ok=True)
            page.screenshot(path=str(out/'desktop.png'),full_page=True)
            page.set_viewport_size({'width':390,'height':844})
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            page.screenshot(path=str(out/'mobile.png'),full_page=True)
            page.reload();page.wait_for_selector('[data-id="8"][data-grade="B"].selected')
            assert not errors,errors
            print(json.dumps({'compact_layout':True,'default_A':True,'manual_ABF_during_session':True,'grade_persists_after_reload':True,'external_grade_refresh':True,'unchanged_records_not_refetched':True,'mobile_layout':True,'page_errors':errors}))
            browser.close()
    finally:
        server.shutdown();worker.join();server.server_close();app.close()
