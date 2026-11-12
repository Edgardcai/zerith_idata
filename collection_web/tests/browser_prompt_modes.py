"""Real browser and HTTP/session/archive tests on temporary data; no robot RPCs."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer

sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parent)]
import h5py
from playwright.sync_api import sync_playwright
from app import Application,Handler
from episodes import atomic_json
from tasks import compose_task,validate_task
from test_core import episode


class FakeStream:
    def __init__(self):self.cancelled=threading.Event()
    def __iter__(self):
        from types import SimpleNamespace
        yield SimpleNamespace(json_data='{"message":"accepted"}')
        while not self.cancelled.wait(.05):pass
    def cancel(self):self.cancelled.set()
    def is_active(self):return not self.cancelled.is_set()


with tempfile.TemporaryDirectory() as temp:
    root=Path(temp);(root/'data').mkdir()
    app=Application(root/'data',root/'runtime',start_devices=False)
    device=app.monitor.snapshot();device.update(ready=True,battery=80,vr_connected=True)
    for check in device['checks']:check['ok']=True
    app.monitor.snapshot=lambda:device
    submitted=[]
    class FakeVendor:
        def meta(self,config):
            submitted.append(config.copy())
            source=root/'data'/f"{config['task_id']}_{config['task_name']}";source.mkdir(exist_ok=True)
            atomic_json(source/'task_meta.json',config)
            return FakeStream()
        def close(self):pass
    app.collector.vendor_factory=FakeVendor
    cfg,targets=validate_task({'task_name':'Grasp Safeguard Soap with the left hand and then grasp HandFan with the right hand.'})
    app.collector.session={'id':'old-closed','config':cfg,'targets':targets,'dataset':str(root/'data/old')}
    app.collector.phase='closed'
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.app=app
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    try:
        with sync_playwright() as pw:
            executables=list((Path.home()/'.cache/ms-playwright').glob('chromium-*/chrome-linux*/chrome'))
            browser=pw.chromium.launch(executable_path=str(max(executables,key=lambda p:p.stat().st_mtime)),headless=True,args=['--no-sandbox'])
            page=browser.new_page(viewport={'width':1440,'height':1100});errors=[]
            page.on('pageerror',lambda e:errors.append(str(e)))
            page.goto(f'http://127.0.0.1:{server.server_port}')
            page.wait_for_function("document.querySelector('#prompt').value.startsWith('Place the item held') && document.querySelector('#left').value==='Safeguard Soap'")
            assert page.locator('[data-prompt-mode="tabletop"]').get_attribute('aria-pressed')=='true'
            assert page.locator('#subtasks').input_value()=='4'
            assert page.locator('#subtasks').get_attribute('readonly') is not None
            catalog=page.locator('#leftProduct option').all_text_contents()
            # Exercise the unchanged visible product search control.
            page.locator('#leftProductSearch').fill('Safeguard')
            page.locator('.product-picker-option:visible').filter(has_text='Safeguard Soap').click()
            for mode in ('shelf_grasp','tabletop','shelf_place'):
                page.locator(f'[data-prompt-mode="{mode}"]').click()
                for hand in ('left','right','both'):
                    page.locator('#handMode').select_option(hand)
                    expected=compose_task(mode,hand,'Safeguard Soap','HandFan')
                    assert page.locator('#prompt').input_value()==expected['task_name'],(mode,hand)
                    assert page.locator('#subtasks').input_value()==str(expected['subtask_num'])
                    assert page.locator('#stagePlan li>span:last-child').all_text_contents()==expected['stage_prompts']
                    assert page.locator('#prompt').input_value().isascii()
                    draft=page.evaluate('task()')
                    from urllib.request import Request,urlopen
                    request=Request(f'http://127.0.0.1:{server.server_port}/api/task/directory',data=json.dumps(draft).encode(),
                        headers={'Content-Type':'application/json','X-Collection-Token':app.csrf})
                    with urlopen(request) as r:report=json.load(r)
                    assert report['config']['task_name']==expected['task_name'] and report['config']['subtask_num']==expected['subtask_num']
            assert page.locator('#leftProduct option').all_text_contents()==catalog
            page.locator('[data-prompt-mode="tabletop"]').click();page.locator('#handMode').select_option('both')
            page.locator('#applyScene').click()
            page.wait_for_function("!document.querySelector('#start').disabled")
            page.locator('#start').click();page.locator('#confirmAccept').click()
            page.wait_for_function("document.querySelector('#phase').textContent==='等待 Meta Quest'")
            assert page.locator('[data-prompt-mode=tabletop]').is_disabled() and page.locator('#handMode').is_disabled()
            assert submitted[-1]['subtask_num']==4 and submitted[-1]['step_list']==[]
            assert 'prompt_mode' not in submitted[-1]
            page.reload();page.wait_for_function("document.querySelector('[data-prompt-mode=tabletop]').getAttribute('aria-pressed')==='true' && document.querySelector('#subtasks').value==='4'")
            path=episode(Path(app.collector.session['source_dataset']),finished=False)
            (path/'collection.log').write_text('采集会话启动\n')
            with h5py.File(path/'episode.hdf5','a') as f:f.attrs.update(total_subtasks=4,completed_subtasks=0)
            page.wait_for_function("document.querySelector('#stage').textContent==='1 / 4'")
            uid=app.collector.status()['current']['uuid']
            for stage in (1,2,3):
                with app.collector.lock:app.collector.responses.append({'at':time.time(),'value':{
                    'event_type':'EPISODE_PROGRESS','uid':uid,'completed_subtask_index':stage}})
                page.wait_for_function(f"document.querySelector('#stage').textContent==='{stage+1} / 4'")
                assert page.locator('#stagePlan li.complete').count()==stage
            assert '保存' in page.locator('#phaseHint').inner_text()
            with h5py.File(path/'episode.hdf5','a') as f:f.attrs['completed_subtasks']=4
            atomic_json(path/'episode_meta.json',{'episode_id':uid,'step_index':[{'start_frame_id':2*i,'end_frame_id':2*i+1} for i in range(4)]})
            (path/'collection.log').write_text('子任务 4 保存完毕 | 分界帧: 8\n采集流结束\n')
            page.wait_for_function("document.querySelector('#end').disabled===false")
            saved=app.store.list()[0];metadata=json.loads((Path(saved['path'])/'collection_task.json').read_text())
            assert metadata['config']['subtask_num']==4 and metadata['targets']['prompt_mode']=='tabletop'
            assert len(metadata['targets']['stage_prompts'])==4
            page.locator('#end').click();page.locator('#confirmAccept').click()
            page.wait_for_function("!document.querySelector('[data-prompt-mode=shelf_place]').disabled")
            page.locator('[data-prompt-mode="shelf_place"]').click();page.locator('#handMode').select_option('right')
            page.locator('#start').click();page.locator('#confirmAccept').click()
            page.wait_for_function("document.querySelector('#phase').textContent==='等待 Meta Quest'")
            page.reload();page.wait_for_function("document.querySelector('[data-prompt-mode=shelf_place]').getAttribute('aria-pressed')==='true' && document.querySelector('#subtasks').value==='1'")
            assert page.locator('#prompt').input_value()=='Place HandFan on the shelf with the right hand.'
            page.locator('#end').click();page.locator('#confirmAccept').click()
            page.wait_for_function("!document.querySelector('#handMode').disabled")
            page.reload();page.wait_for_function("document.querySelector('[data-prompt-mode=tabletop]').getAttribute('aria-pressed')==='true'")
            assert page.locator('#subtasks').input_value()=='2'
            page.locator('#handMode').select_option('both')
            page.screenshot(path='/tmp/collection-prompt-desktop.png',full_page=True)
            page.set_viewport_size({'width':390,'height':844})
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            page.screenshot(path='/tmp/collection-prompt-mobile.png',full_page=True)
            assert not errors,errors
            browser.close()
            print(json.dumps({'nine_mode_hand_choices':True,'default_after_closed_legacy_session':'tabletop',
                'catalog_unchanged':True,'active_session_locked':True,'active_reload_preserved':True,
                'four_stage_progress_and_archive':True,'closed_reload_default':'tabletop','mobile_no_overflow':True,'page_errors':errors}))
    finally:
        server.shutdown();worker.join();server.server_close();app.close()
