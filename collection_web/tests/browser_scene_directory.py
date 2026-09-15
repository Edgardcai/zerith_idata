"""Scene apply, prompt changes, date rollover and responsive UI; no robot RPCs."""
import sys
import tempfile
import threading
import json
from datetime import datetime
from pathlib import Path
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from playwright.sync_api import sync_playwright
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import Application,Handler
from tasks import validate_task

with tempfile.TemporaryDirectory() as temp, patch('tasks.datetime') as clock:
    clock.now.return_value=datetime(2026,9,14)
    root=Path(temp)/'data';root.mkdir()
    app=Application(root,Path(temp)/'runtime',start_devices=False)
    cfg,targets=validate_task({'task_id':8099,'task_name':'old prompt','lift_height':'0.8'})
    app.collector.session={'id':'closed_old','config':cfg,'targets':targets,'dataset':str(root/'old_product_directory')}
    app.collector.phase='closed'
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.app=app
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch(executable_path='/home/robot/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome',headless=True,args=['--no-sandbox'])
            page=browser.new_page();errors=[];starts=[]
            page.on('pageerror',lambda e:errors.append(str(e)))
            page.goto(f'http://127.0.0.1:{server.server_port}')
            page.wait_for_function("document.querySelector('#taskId').value==='20260914' && document.querySelector('#prompt').value==='old prompt'")
            assert page.locator('#start').is_disabled()
            assert not list(root.iterdir())
            # Scene application does not require any prompt or product names.
            page.locator('#prompt').fill('')
            page.locator('#applyScene').click()
            page.wait_for_function("document.querySelector('#directory').textContent.endsWith('20260914_scene1')")
            assert (root/'20260914_scene1').is_dir()
            page.locator('#prompt').fill('Grasp Tea with the left hand and then grasp Coffee with the right hand')
            page.locator('#prompt').blur()
            page.wait_for_function("document.querySelector('#left').value==='Tea'")
            assert page.locator('#directory').inner_text().endswith('20260914_scene1')
            page.locator('#taskOptions summary').click()
            page.locator('#liftHeight').fill('0.7')
            page.locator('#sceneId').fill('2')
            assert page.locator('#start').is_disabled()
            assert not (root/'20260914_scene2').exists()
            assert page.locator('#directory').inner_text().endswith('scene1')
            page.locator('#applyScene').click()
            page.wait_for_function("document.querySelector('#directory').textContent.endsWith('20260914_scene2') && !document.querySelector('#start').disabled")
            assert (root/'20260914_scene2').is_dir()
            assert page.locator('.dataset-group.is-current .dataset-name').inner_text()=='20260914_scene2'
            def intercept(route):
                starts.append(route.request.post_data_json)
                route.fulfill(status=200,content_type='application/json',body='{}')
            page.route('**/api/session/start',intercept)
            page.locator('#start').click();page.locator('#confirmAccept').click()
            page.wait_for_function("!document.querySelector('#start').disabled")
            assert starts[0]['scene_id']==2 and starts[0]['task_id']==20260914
            assert starts[0]['task_name'].startswith('Grasp Tea')
            page.reload()
            page.wait_for_function("document.querySelector('#sceneId').value==='2' && document.querySelector('#directory').textContent.endsWith('20260914_scene2')")
            # Date changes do not silently switch a scene without Apply.
            clock.now.return_value=datetime(2026,9,15)
            page.wait_for_function("document.querySelector('#taskId').value==='20260915' && document.querySelector('#start').disabled")
            assert not (root/'20260915_scene2').exists()
            page.locator('#applyScene').click()
            page.wait_for_function("document.querySelector('#directory').textContent.endsWith('20260915_scene2')")
            app.collector.session={'id':'active','baseline':[],'started':0,'config':{**cfg,'task_id':20260915,'scene_id':2},'targets':targets,'dataset':str(root/'20260915_scene2')}
            app.collector.phase='waiting'
            page.wait_for_function("document.querySelector('#sceneId').disabled && document.querySelector('#applyScene').disabled")
            app.collector.phase='closed'
            page.wait_for_function("!document.querySelector('#sceneId').disabled")
            page.set_viewport_size({'width':390,'height':844})
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            assert not errors,errors
            out=Path('/home/robot/collection_web/runtime/scene_ui_verification');out.mkdir(exist_ok=True)
            page.screenshot(path=str(out/'mobile.png'),full_page=True)
            page.set_viewport_size({'width':1440,'height':1100})
            page.screenshot(path=str(out/'desktop.png'),full_page=True)
            print(json.dumps({'scene_apply_creates_directory':True,'draft_requires_apply':True,'prompt_independent':True,'per_scene_submission':True,'reload_persists_scene':True,'date_rollover_requires_apply':True,'active_scene_locked':True,'mobile_no_overflow':True,'page_errors':errors}))
            browser.close()
    finally:
        server.shutdown();thread.join();server.server_close();app.close()
