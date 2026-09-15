"""Isolated browser regression: temporary dataset, no robot task submitted."""
import sys
import tempfile
import threading
import json
from pathlib import Path
from http.server import ThreadingHTTPServer
import h5py
from playwright.sync_api import sync_playwright
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import Application, Handler
from test_core import episode

with tempfile.TemporaryDirectory() as temp:
    root=Path(temp)/'data';root.mkdir()
    app=Application(root,Path(temp)/'runtime',start_devices=False)
    dataset=root/'timing_browser';dataset.mkdir()
    app.store.new_session('s',{}, {},dataset)
    p=episode(dataset)
    with h5py.File(p/'episode.hdf5','a') as f:
        f['timestamp/t'][3]=f['timestamp/t'][2]
    app.store.observe({'id':'s','dataset':str(dataset),'baseline':[]})
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.app=app
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch(
                executable_path='/home/robot/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome',
                headless=True,args=['--no-sandbox'])
            page=browser.new_page(viewport={'width':1440,'height':1000});errors=[]
            page.on('pageerror',lambda e:errors.append(str(e)))
            page.goto(f'http://127.0.0.1:{server.server_port}')
            page.wait_for_selector('.dataset-group')
            for node in page.locator('.dataset-group').all():
                if 'timing_browser' in node.inner_text() and not node.evaluate('e => e.open'):
                    node.locator('summary').click()
            page.wait_for_selector('[data-id="1"][data-grade="A"].selected')
            assert page.locator('[data-id="1"][data-grade="A"]').is_enabled()
            for grade in ('F','B','A'):
                page.locator(f'[data-id="1"][data-grade="{grade}"]').click()
                page.wait_for_selector(f'[data-id="1"][data-grade="{grade}"].selected')
                page.wait_for_function("!document.querySelector('[data-grade=A]').disabled")
                assert app.store.list()[0]['grade']==grade
            page.locator('[data-quality="1"] summary').click()
            assert '时序不合格' in page.locator('tr').last.inner_text()
            assert '重复时间戳' in page.locator('tr').last.inner_text()
            page.set_viewport_size({'width':390,'height':844})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert not errors,errors
            print(json.dumps({'quality_label':True,'default_A_with_warning':True,'manual_ABF':True,
                              'mobile_layout':True,'page_errors':errors}))
            browser.close()
    finally:
        server.shutdown();worker.join();server.server_close();app.close()
