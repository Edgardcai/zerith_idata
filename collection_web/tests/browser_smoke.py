"""Browser interaction test on temporary data; never submits a robot task."""
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
from test_core import episode

with tempfile.TemporaryDirectory() as temp:
    root = Path(temp) / 'data'
    root.mkdir()
    app = Application(root, Path(temp) / 'runtime', start_devices=False)
    dataset = root / '1_browser_test'
    dataset.mkdir()
    app.store.new_session('browser', {'subtask_num': 2}, {}, dataset)
    episode(dataset)
    app.store.observe({'id': 'browser', 'dataset': str(dataset), 'baseline': []})
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.app = app
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                executable_path=str(Path.home() / '.cache/ms-playwright/chromium-1234/chrome-linux64/chrome'),
                headless=True, args=['--no-sandbox'])
            page = browser.new_page(viewport={'width': 1440, 'height': 1000})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(f'http://127.0.0.1:{server.server_port}')
            page.wait_for_function("document.querySelector('#left').value==='Dahongpao Milk Tea'")
            page.locator('#liftHeight').fill('0.80')
            page.wait_for_function("document.querySelector('#directory').textContent.endsWith('DahongpaoMilkTea_Ifcoconut_0.8')")
            page.locator('#left').fill('Test Tea')
            page.wait_for_function("document.querySelector('#directory').textContent.endsWith('TestTea_Ifcoconut_0.8')")
            assert app.collector.session is None
            page.locator('#history').check()
            page.wait_for_selector('[data-id="1"][data-grade="A"].selected')
            for grade in ('B', 'F', 'A'):
                page.locator(f'[data-id="1"][data-grade="{grade}"]').click()
                page.wait_for_selector(f'[data-id="1"][data-grade="{grade}"].selected')
            page.locator('[data-delete="1"]').click()
            page.locator('#confirmAccept').click()
            page.wait_for_function("document.querySelector('#episodes').textContent.includes('已放弃')")
            assert not (dataset / 'episode_000001').exists()
            assert app.store.list()[0]['state'] == 'deleted'
            teleop_root=app.teleop.root
            (teleop_root/'runtime').mkdir(parents=True)
            state={'timestamp':time.time(),'version':'collection-1.0','active':True,'ready':True,
                   'operator':{'initialized':True,'calibrated':True,'state':'STOP_TELEOP'},
                   'calibration_seq':1,'pid':123,'warnings':[]}
            status_path=teleop_root/'runtime/status.json'
            status_path.write_text(json.dumps(state))
            page.wait_for_function("document.querySelector('#teleopState').textContent.includes('标定成功')")
            state['operator']['state']='DECOUPLED';state['warnings']=['头 yaw 偏差 0.00600 rad'];state['timestamp']=time.time()
            status_path.write_text(json.dumps(state))
            page.wait_for_function("document.querySelector('#teleopState').textContent==='遥操作中' && !document.querySelector('#teleopWarning').hidden")
            assert page.locator('#fixedLiftHeight').is_disabled()
            page.set_viewport_size({'width': 390, 'height': 844})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.reload()
            page.wait_for_selector('#connection')
            assert not errors, errors
            browser.close()
            print(json.dumps({'grade_buttons': 'A/B/F passed', 'delete_confirmation': 'passed',
                              'files_removed': True, 'mobile_overflow': False, 'page_errors': errors}))
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
        app.close()
