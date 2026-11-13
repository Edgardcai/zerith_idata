"""Exercise the actual picker, restored drafts and all batch-4 prompt variants."""
import json
from pathlib import Path
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer

sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parent)]
from playwright.sync_api import sync_playwright
from app import Application,Handler

expected=json.loads((Path(__file__).parent/'fixtures/catalog_batch4.json').read_text())
with tempfile.TemporaryDirectory() as temp:
    root=Path(temp);(root/'data').mkdir()
    app=Application(root/'data',root/'runtime',start_devices=False)
    old_prompt=('Place the item held in the left hand on the table, then place the item held in the right hand on the table. '
                'Grasp Monster Blue from the table with the left hand, then grasp Monster Pink with the right hand.')
    old_cfg={'task_id':20261113,'scene_id':22,'task_name':old_prompt,'subtask_num':4,
             'frequency':30,'max_episode_time':60,'record_depth':False}
    old_targets={'left':'Monster Blue','right':'Monster Pink','prompt_mode':'tabletop','hand_mode':'both'}
    app.collector.session={'id':'closed-legacy-pink','config':old_cfg,'targets':old_targets,'dataset':str(root/'data/old')}
    app.collector.phase='closed'
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.app=app
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with sync_playwright() as pw:
            paths=list((Path.home()/'.cache/ms-playwright').glob('chromium-*/chrome-linux*/chrome'))
            browser=pw.chromium.launch(executable_path=str(max(paths,key=lambda p:p.stat().st_mtime)),headless=True,args=['--no-sandbox'])
            page=browser.new_page(viewport={'width':1440,'height':1100});errors=[]
            page.on('pageerror',lambda e:errors.append(str(e)))
            page.goto(f'http://127.0.0.1:{server.server_port}')
            page.wait_for_function("document.querySelector('#rightProduct').value==='Monster Red' && document.querySelector('#left').value==='Monster Blue'")
            assert 'Monster Red' in page.locator('#prompt').input_value()
            assert app.collector.session['targets']['right']=='Monster Pink'
            for side in ['left','right']:
                actual=page.locator('#'+side+'Product option').evaluate_all('(options)=>Object.fromEntries(options.map(o=>[o.value,o.textContent]))')
                assert 'Monster Pink' not in actual
                for name,label in expected.items():assert actual[name]==label+' · '+name,(name,actual[name])
            page.locator('#rightProductSearch').fill('魔爪红')
            matches=page.locator('.product-picker-option:visible')
            assert matches.count()==1
            assert matches.inner_text()=='魔爪热带水果味 · Monster Red'
            matches.click()
            for mode in ['shelf_grasp','tabletop','shelf_place']:
                page.locator('[data-prompt-mode="'+mode+'"]').click()
                for hand in ['left','right','both']:
                    page.locator('#handMode').select_option(hand)
                    actual=page.evaluate('task()')
                    assert 'Monster Pink' not in actual['task_name']
                    assert actual['left']==('Monster Blue' if hand!='right' else '')
                    assert actual['right']==('Monster Red' if hand!='left' else '')
            checked=page.evaluate('''async names=>{
                let count=0;
                for(const name of names)for(const mode of ['shelf_grasp','tabletop','shelf_place'])for(const hand of ['left','right','both']){
                    const c=TaskPrompts.compose(mode,hand,name,'Monster Red');
                    const result=await api('/api/task/directory',{...c,prompt_mode:mode,hand_mode:hand});
                    if(result.config.task_name!==c.task_name||result.config.subtask_num!==c.subtask_num||result.targets.left!==c.left||result.targets.right!==c.right)throw Error(JSON.stringify({name,mode,hand,result}));
                    count++;
                }
                return count;
            }''',list(expected))
            assert checked==297
            # A running historical session remains locked and keeps its actual label.
            active=app.status(compact=True)
            active['collection'].update(phase='waiting',display_phase='waiting',accepted=True,connected=True)
            page.route('**/api/status*',lambda route:route.fulfill(json=active))
            page.reload()
            page.wait_for_function("document.querySelector('#rightProduct').value==='Monster Pink' && document.querySelector('#rightProductSearch').disabled")
            assert page.locator('#prompt').input_value()==old_prompt
            page.unroute('**/api/status*');page.reload()
            page.wait_for_function("document.querySelector('#rightProduct').value==='Monster Red'")
            page.screenshot(path='/tmp/catalog-batch4-red-desktop.png',full_page=True)
            page.set_viewport_size({'width':390,'height':844})
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            assert not errors,errors
            browser.close()
            print(json.dumps({'batch4_names_checked':len(expected),'mode_hand_product_cases':checked,'red_chinese_search':True,'closed_legacy_draft_migrated':True,'historical_metadata_unchanged':True,'active_legacy_session_preserved':True,'page_errors':errors}))
    finally:
        server.shutdown();thread.join();server.server_close();app.close()
