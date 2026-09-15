"""Real H.264 playback in Chromium; isolated DB/files, never starts collection."""
import json
from pathlib import Path
import shutil
import sys
import tempfile
import threading
from playwright.sync_api import sync_playwright
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import Application,CollectionHTTPServer,Handler
from test_core import episode

REAL=Path('/data/zerith_data/20260914_scene2/episode_000009/videos/rs')
with tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp)/'data';root.mkdir();app=Application(root,Path(tmp)/'runtime',start_devices=False)
    dataset=root/'20260915_scene1';dataset.mkdir();app.store.new_session('browser',{}, {},dataset)
    for uid in ('a'*32,'b'*32,'c'*32):
        episode(dataset,uid);app.store.observe({'id':'browser','dataset':str(dataset),'baseline':[]})
    rows=sorted(app.store.list(),key=lambda r:r['id'])
    for row in rows:
        for video in REAL.glob('*.mp4'):shutil.copy2(video,Path(row['path'])/'videos/rs'/video.name)
    # Reproduce the actual renamed-directory issue, including missing one camera.
    Path(rows[0]['path']).rename(dataset/'episode99')
    (Path(rows[1]['path'])/'videos/rs/cam_high.mp4').unlink()
    app.store.delete(rows[2]['id'])
    server=CollectionHTTPServer(('127.0.0.1',0),Handler);server.app=app
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path=str(Path.home()/'.cache/ms-playwright/chromium-1234/chrome-linux64/chrome'),headless=True,args=['--no-sandbox','--proxy-server=direct://'])
            page=browser.new_page(viewport={'width':1366,'height':768});errors=[]
            page.on('pageerror',lambda e:errors.append(str(e)))
            page.goto(f'http://127.0.0.1:{server.server_port}')
            page.wait_for_selector('button[data-review="1"]')
            assert page.locator('.dataset-total').inner_text()=='2 条保留记录'
            assert page.locator('tbody tr').count()==3
            assert page.locator('#episodeCount').inner_text()=='2'
            page.locator('button[data-review="1"]').click()
            page.wait_for_function("!document.getElementById('reviewPlay').disabled")
            page.locator('#reviewPlay').click()
            page.wait_for_function("[...document.querySelectorAll('#review video')].every(v=>!v.paused&&v.currentTime>1)")
            times=page.locator('#review video').evaluate_all('(vs)=>vs.map(v=>v.currentTime)')
            assert max(times)-min(times)<.15,times
            page.locator('#reviewPause').click()
            assert page.locator('#review video').evaluate_all('(vs)=>vs.every(v=>v.paused)')
            page.locator('#reviewSeek').evaluate("el=>{el.value=3;el.dispatchEvent(new Event('input'))}")
            page.wait_for_function("[...document.querySelectorAll('#review video')].every(v=>!v.seeking&&Math.abs(v.currentTime-3)<.1)")
            page.locator('#reviewSpeed').select_option('2');page.locator('#reviewPlay').click()
            page.wait_for_function("[...document.querySelectorAll('#review video')].every(v=>!v.paused&&v.currentTime>4)")
            page.locator('#reviewPause').click()
            page.screenshot(path='/tmp/collection-three-video.png')
            page.locator('#reviewSeek').evaluate("el=>{el.value=7.4;el.dispatchEvent(new Event('input'))}")
            page.wait_for_function("!document.getElementById('reviewPlay').disabled")
            page.locator('#reviewPlay').click()
            page.wait_for_function("document.getElementById('reviewStatus').textContent==='播放结束'")
            page.locator('#reviewPlay').click()
            page.wait_for_function("[...document.querySelectorAll('#review video')].every(v=>!v.paused&&v.currentTime>.2&&v.currentTime<3)")
            # Reopen/retry repeatedly to catch stale media events and aborted play promises.
            for _ in range(5):
                page.locator('#reviewRetry').click()
                page.wait_for_function("!document.getElementById('reviewPlay').disabled")
                page.locator('#reviewPlay').click()
                page.wait_for_function("[...document.querySelectorAll('#review video')].every(v=>!v.paused&&v.currentTime>.2)")
                page.keyboard.press('Escape')
                page.wait_for_function("document.querySelectorAll('#review video').length===0")
                page.locator('button[data-review="1"]').click()
                page.wait_for_function("!document.getElementById('reviewPlay').disabled")
            page.locator('#closeReview').click()
            # A network failure must expose a retry, and retry must recover all streams.
            page.route('**/api/video/1/head?*',lambda route:route.abort('failed'),times=1)
            page.locator('button[data-review="1"]').click()
            page.wait_for_function("document.querySelectorAll('.review-camera-status')[1].textContent.includes('失败')")
            page.locator('#reviewRetry').click()
            page.wait_for_function("[...document.querySelectorAll('#review video')].every(v=>v.readyState>=3)")
            page.locator('#closeReview').click()
            page.locator('button[data-review="2"]').click()
            page.wait_for_function("!document.getElementById('reviewPlay').disabled")
            assert '不存在' in page.locator('.review-camera-status').nth(1).inner_text()
            page.locator('#reviewPlay').click()
            page.wait_for_function("[...document.querySelectorAll('#review video')].filter(v=>v.hasAttribute('src')).every(v=>!v.paused&&v.currentTime>.3)")
            page.locator('#closeReview').click()
            # Only count changes after clicking discard: keep all rows and names.
            page.locator('button[data-delete="2"]').click();page.locator('#confirmAccept').click()
            page.wait_for_function("document.querySelector('.dataset-total').textContent==='1 条保留记录'")
            assert page.locator('tbody tr').count()==3
            assert page.locator('#episodeCount').inner_text()=='1'
            assert app.store.list()[0]['name']=='episode_000003'
            page.set_viewport_size({'width':390,'height':844})
            page.locator('button[data-review="1"]').click()
            page.wait_for_function("!document.getElementById('reviewPlay').disabled")
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            assert not errors,errors
            print(json.dumps({'real_video':'three H264 streams decoded','sync_times':times,'seek_pause_speed':'passed','reopen_retry_cycles':5,'missing_camera':'explicit error and remaining streams play','counts':'discard excluded, all rows and numbering retained','console_errors':errors},ensure_ascii=False))
            browser.close()
    finally:server.shutdown();server.server_close();thread.join();app.close()
