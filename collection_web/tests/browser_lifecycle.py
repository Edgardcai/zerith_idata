"""Browser regressions on isolated data: restart token, discard retry, stale polling."""
import json
from pathlib import Path
import secrets
import sys
import tempfile
import threading
from playwright.sync_api import sync_playwright
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import Application,CollectionHTTPServer,Handler
from test_core import episode

with tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp)/'data';root.mkdir();app=Application(root,Path(tmp)/'runtime',start_devices=False)
    dataset=root/'batch';dataset.mkdir();app.store.new_session('browser',{}, {},dataset)
    for uid in ('a'*32,'b'*32):
        episode(dataset,uid);app.store.observe({'id':'browser','dataset':str(dataset),'baseline':[]})
    rows=sorted(app.store.list(),key=lambda r:r['id']);actual=dataset/'episode99';Path(rows[0]['path']).rename(actual)
    server=CollectionHTTPServer(('127.0.0.1',0),Handler);server.app=app
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path=str(Path.home()/'.cache/ms-playwright/chromium-1234/chrome-linux64/chrome'),headless=True,args=['--no-sandbox','--proxy-server=direct://'])
            page=browser.new_page(viewport={'width':1366,'height':768});errors=[];ratings=[]
            page.on('pageerror',lambda e:errors.append(str(e)))
            page.on('response',lambda r:ratings.append(r.status) if r.url.endswith('/api/episode/rate') else None)
            page.goto(f'http://127.0.0.1:{server.server_port}');page.wait_for_selector('[data-id="1"][data-grade="B"]')
            page.locator('#prompt').fill('Preserve this edited prompt')
            # A service restart replaces the CSRF token. The denied POST did no work.
            app.csrf=secrets.token_urlsafe(32)
            page.locator('[data-id="1"][data-grade="B"]').click()
            page.wait_for_function("document.getElementById('toast').textContent==='已评为 B' && !document.querySelector('[data-id=\"1\"][data-grade=B]').disabled")
            assert ratings==[403,200],ratings
            assert page.locator('#prompt').input_value()=='Preserve this edited prompt'
            assert app.store.db.execute("select count(*) from audit where action='grade'").fetchone()[0]==1
            assert json.loads((actual/'review.json').read_text())['grade']=='B'
            # Delay an old group response until after a newer response has rendered.
            stale=json.dumps({'groups':app.store.groups()});held=[]
            page.route('**/api/episode-groups',lambda route:held.append(route),times=1)
            page.evaluate('void poll()');page.wait_for_timeout(200);assert held
            app.store.rate(2,'F');page.evaluate('poll()')
            assert page.locator('#gradeCounts').inner_text()=='A 0 · B 1 · F 1'
            held[0].fulfill(status=200,content_type='application/json',body=stale)
            page.wait_for_timeout(200)
            assert page.locator('#gradeCounts').inner_text()=='A 0 · B 1 · F 1'
            # The first deletion fails before removal; UI must let the user retry.
            complete=app.store._complete_delete;calls=[]
            def fail_once(row,intent):
                calls.append(row['id'])
                if len(calls)==1:raise OSError('temporary filesystem failure')
                return complete(row,intent)
            app.store._complete_delete=fail_once
            page.locator('[data-delete="1"]').click();page.evaluate('poll()');page.locator('#confirmAccept').click()
            page.wait_for_function("document.querySelector('[data-delete=\"1\"]').textContent==='重试放弃'")
            assert app.store.get(1)['state']=='deleting';assert actual.exists()
            page.locator('[data-delete="1"]').click();page.locator('#confirmAccept').click()
            page.wait_for_function("document.querySelector('.dataset-total').textContent==='1 条保留记录' && document.querySelector('[data-delete=\"1\"]').disabled")
            assert not actual.exists();assert page.locator('tbody tr').count()==2
            assert app.store.list()[-1]['name']=='episode_000001'
            assert app.store.get(1)['state']=='deleted'
            page.reload();page.wait_for_selector('[data-id="2"][data-grade="F"].selected')
            assert not errors,errors
            print(json.dumps({'restart_token':'one rejected POST, one successful rating','form_input':'preserved','out_of_order_poll':'old totals ignored','renamed_discard':'failure shown, retry completes','list_and_numbering':'unchanged','console_errors':errors},ensure_ascii=False))
            browser.close()
    finally:server.shutdown();server.server_close();thread.join();app.close()
