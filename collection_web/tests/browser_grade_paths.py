"""Rename and in-flight polling regression; only edits temporary fixture grades."""
import json
from pathlib import Path
import sys
import tempfile
import threading
from playwright.sync_api import sync_playwright
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import Application,CollectionHTTPServer,Handler
from test_core import episode

saved=threading.Event();release=threading.Event()
class SlowRatingHandler(Handler):
    def json(self,value,status=200):
        if self.path=='/api/episode/rate' and status==200:
            saved.set();release.wait(5)
        return super().json(value,status)

with tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp)/'data';root.mkdir();app=Application(root,Path(tmp)/'runtime',start_devices=False)
    dataset=root/'batch';dataset.mkdir();app.store.new_session('browser',{}, {},dataset)
    episode(dataset);app.store.observe({'id':'browser','dataset':str(dataset),'baseline':[]})
    row=app.store.list()[0];renamed=dataset/'episode99';Path(row['path']).rename(renamed)
    server=CollectionHTTPServer(('127.0.0.1',0),SlowRatingHandler);server.app=app
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path=str(Path.home()/'.cache/ms-playwright/chromium-1234/chrome-linux64/chrome'),headless=True,args=['--no-sandbox','--proxy-server=direct://'])
            page=browser.new_page(viewport={'width':1366,'height':768});errors=[]
            page.on('pageerror',lambda e:errors.append(str(e)))
            page.goto(f'http://127.0.0.1:{server.server_port}')
            page.wait_for_selector('[data-grade="F"]')
            page.evaluate("window.previousGradeButton=document.querySelector('[data-grade=F]')")
            page.locator('[data-grade="F"]').click();assert saved.wait(3)
            # Fetch new counts while the successful POST response is delayed.
            page.evaluate('poll()')
            assert page.evaluate('!window.previousGradeButton.isConnected')
            release.set()
            page.wait_for_function("document.getElementById('toast').textContent==='已评为 F' && !document.querySelector('[data-grade=F]').disabled")
            assert page.locator('[data-grade="F"]').get_attribute('aria-pressed')=='true'
            assert page.locator('#gradeCounts').inner_text()=='A 0 · B 0 · F 1'
            page.reload();page.wait_for_selector('[data-grade="F"].selected')
            for grade in ('B','A'):
                page.locator(f'[data-grade="{grade}"]').click()
                page.wait_for_function(f"document.getElementById('toast').textContent==='已评为 {grade}' && !document.querySelector('[data-grade={grade}]').disabled")
                assert json.loads((renamed/'review.json').read_text())['grade']==grade
            assert not Path(row['path']).exists()
            assert app.store.list()[0]['seq']==row['seq']
            assert not errors,errors
            print(json.dumps({'renamed_episode_grades':'F/B/A saved','poll_during_save':'detached button handled','reload':'grade persisted','original_number':'unchanged','console_errors':errors},ensure_ascii=False))
            browser.close()
    finally:release.set();server.shutdown();server.server_close();thread.join();app.close()
