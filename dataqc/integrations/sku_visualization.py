"""SKU module reuses the normal LeRobot player and original review storage."""
from html import escape
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from dataqc.config import REAL_SOURCE_ROOT
from dataqc.sku_index import PRODUCTS, Searches, dataset_episodes, matches


def integrate_sku(app):
    from .legacy_ui import replace
    from .compact_review import between, asset
    jobs=Searches()
    app.sku_searches=jobs
    app.HTML=replace(app.HTML,'④ plumo筛查','④ 按 SKU 可视化')
    app.HTML=replace(app.HTML,'src="/auto/hdf5" title="plumo筛查"','src="/sku-visualization/" title="按 SKU 可视化 LeRobot"')
    app.HTML=replace(app.HTML,'</style>','''#manualScreeningFrame{min-height:0;height:calc(100dvh - 74px)}\n</style>''')
    options=''.join(f'<option value="{escape(p,quote=True)}">{escape(p)}</option>' for p in PRODUCTS)
    toolbar='''<header class="directory-toolbar sku-toolbar">
      <label for="rootPath">数据目录</label><input id="rootPath" autocomplete="off" value="ROOT" placeholder="输入包含多个 LeRobot 数据集的上级目录">
      <label for="skuSelect">商品名称</label><select id="skuSelect"><option value="">请选择商品（24 种）</option>OPTIONS</select>
      <button id="scanBtn">按商品筛选</button><button id="skuCancel" hidden>取消扫描</button>
      <div class="sku-queue"><button id="skuPrev" disabled>上一条匹配</button><select id="skuEpisode" aria-label="跨目录匹配记录" disabled><option>尚未筛选</option></select><button id="skuNext" disabled>下一条匹配</button>
      <select id="datasetPicker" aria-label="匹配的数据集"><option value="">尚未筛选</option></select>
      <button id="batchExtractBtn" disabled title="处理当前目录全部 episode；其他目录不受影响">当前目录截帧识别</button><button id="screeningToggle" hidden disabled>问题记录</button></div>
      <div id="skuSummary" role="status">按提示词中的完整商品名筛选，不区分大小写。回放及人工记录仍对应原数据集。</div>
      <details id="skuWarnings" hidden><summary></summary><pre></pre></details>
      <div class="toolbar-status"><span id="scanStatus" role="status">选择商品并输入目录后开始筛选</span><span id="batchStatus" role="status"></span></div>
    </header>'''.replace('ROOT',escape(str(REAL_SOURCE_ROOT),quote=True)).replace('OPTIONS',options)
    outer=between(app.LEROBOT_VISUALIZATION_HTML,'<header class="directory-toolbar">','</header>',toolbar.removesuffix('</header>'))
    outer=between(outer,'    async function scan() {',"    $('scanBtn').addEventListener",'    async function scan(){await skuSearch()}\n\n')
    outer=replace(outer,"        $('replayFrame').src = url;",'''        const target=skuMatches[skuPosition];
        if(!target || target.dataset_path!==path)return;
        const filteredUrl=new URL(url,location.origin);
        filteredUrl.searchParams.set('sku',skuActive);
        filteredUrl.searchParams.set('episode_index',String(target.episode_index));
        $('replayFrame').src=filteredUrl.href;''')
    outer=replace(outer,'      if (batchRunning) return;\n      if (!path) return;', '''      if (batchRunning) return;
      if(path && skuMatches[skuPosition]?.dataset_path!==path){
        const next=skuMatches.findIndex(r=>r.dataset_path===path);if(next<0)return;
        skuPosition=next;renderSkuQueue();
      }
      if (!path) return;''')
    outer=outer.replace("['batchExtractBtn','datasetPicker','scanBtn','rootPath']","['batchExtractBtn','datasetPicker','scanBtn','rootPath','skuSelect','skuEpisode','skuPrev','skuNext']")
    outer=replace(outer,"      setStatus(`正在校验并打开：${path}`);", """      $('replayFrame').removeAttribute('src');$('replayFrame').classList.add('hidden');
      $('viewerEmpty').classList.remove('hidden');
      $('viewerEmpty').textContent='正在打开所选匹配记录…';
      setStatus(`正在校验并打开：${path}`);""")
    outer=replace(outer,"'skuNext'])$(id).disabled=false;", "'skuNext'])$(id).disabled=false;renderSkuQueue();")
    outer=replace(outer,'</style>',asset('sku.css')+'\n  </style>')
    outer=replace(outer,'</script>',asset('sku.js')+'\n  </script>')
    app.SKU_VISUALIZATION_HTML=outer

    replay=app.LEROBOT_REPLAY_HTML
    replay=replace(replay,'      episodes = recordsPayload.episodes || [];','''      episodes = recordsPayload.episodes || [];
      if(params.get('sku')){
        const response=await fetch('/api/lerobot-visualization/sku/episodes?'+new URLSearchParams({dataset_path:params.get('dataset_path')||'',sku:params.get('sku')}));
        const filtered=await response.json();if(!response.ok)throw Error(filtered.error||'商品列表读取失败');
        const allowed=new Set(filtered.indices);episodes=episodes.filter(ep=>allowed.has(Number(ep.episode_index)));
        $('episodeSelect').closest('.episode-row').hidden=true;
        $('gradeSelect').closest('.grade-nav').hidden=true;
        if(preferredEpisodeIndex!==null&&!episodes.some(ep=>Number(ep.episode_index)===Number(preferredEpisodeIndex)))
          throw Error('所选记录已变化或不再匹配该商品，请重新按商品筛选');
      }''')
    replay=replace(replay,'    async function loadEpisode(index) {','''    async function loadEpisode(index) {
      if(params.get('sku')&&!episodes.some(ep=>Number(ep.episode_index)===Number(index)))return;''')
    # HTML [hidden] must beat the legacy display:flex/grid rules.
    replay=replace(replay,'</style>','.episode-row[hidden],.grade-nav[hidden]{display:none!important}\n</style>')
    app.LEROBOT_REPLAY_HTML=replay

    old_get=app.Handler.do_GET;old_post=app.Handler.do_POST
    def get(handler):
        parsed=urlparse(handler.path);query=parse_qs(parsed.query)
        if parsed.path=='/sku-visualization/':return app.text_response(handler,app.SKU_VISUALIZATION_HTML)
        if parsed.path not in ('/api/lerobot-visualization/sku/search','/api/lerobot-visualization/sku/episodes'):return old_get(handler)
        try:
            if parsed.path.endswith('/search'):result=jobs.get(query.get('id',[''])[0])
            else:
                sku=query.get('sku',[''])[0]
                if sku not in PRODUCTS:raise ValueError('请选择有效商品')
                episodes=dataset_episodes(Path(query.get('dataset_path',[''])[0]))
                result=dict(indices=[ep['episode_index'] for ep in episodes if not ep.get('error') and any(matches(p,sku) for p in ep['prompts'])])
            app.json_response(handler,result)
        except (ValueError,OSError,KeyError) as exc:app.json_response(handler,dict(error=str(exc)),400)
    def post(handler):
        path=urlparse(handler.path).path
        if path not in ('/api/lerobot-visualization/sku/search','/api/lerobot-visualization/sku/cancel'):return old_post(handler)
        try:
            payload=app.read_json_body(handler)
            result=jobs.start(payload.get('root',''),payload.get('sku','')) if path.endswith('/search') else jobs.get(payload.get('id',''),cancel=True)
            app.json_response(handler,result)
        except (ValueError,OSError,KeyError) as exc:app.json_response(handler,dict(error=str(exc)),400)
    app.Handler.do_GET=get;app.Handler.do_POST=post
