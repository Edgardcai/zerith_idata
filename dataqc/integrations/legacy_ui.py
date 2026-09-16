"""Compose the original UI without changing the separately installed legacy app."""
from pathlib import Path


def replace(text, old, new):
    if old not in text:
        raise RuntimeError('Legacy integration anchor missing: ' + old[:90])
    return text.replace(old, new)


def integrate(app):
    import os
    from html import escape
    from dataqc.config import REAL_SOURCE_ROOT, SIM_SOURCE_ROOT
    app.CROSS_PLATFORM_HTML = app.CROSS_PLATFORM_HTML.replace('/srv/data/datasets/public/market_sim_data', escape(str(SIM_SOURCE_ROOT))).replace('/srv/data/datasets/public/stage2_datasets', escape(str(REAL_SOURCE_ROOT)))
    app.DATA_ROOT = Path(os.environ["DATAQC_HOME"])
    app.HTML = replace(app.HTML, 'LeRobot 数据可视化</button>', '① LeRobot 可视化与人工筛查</button>')
    app.HTML = replace(app.HTML, '采集数据质检与转换</button>', '② 采集数据质检与转换</button>')
    old = '''      <button id="lerobotWorkspaceBtn" class="workspace-btn" type="button">跨平台 LeRobot 质检</button>
      <button id="manualScreeningWorkspaceBtn" class="workspace-btn" type="button">数据人工筛查模块</button>'''
    new = '''      <button id="manualScreeningWorkspaceBtn" class="workspace-btn" type="button">③ VLM 自动质检</button>
      <button id="lerobotWorkspaceBtn" class="workspace-btn" type="button">④ 仿真与真机比较</button>'''
    app.HTML = replace(app.HTML, old, new)
    app.HTML = app.HTML.replace('服务端 9988', '服务端 ' + str(int(os.environ.get('DATAQC_PORT', '9990')))).replace('src="/manual-screening/" title="数据人工筛查模块"', 'src="/auto/hdf5" title="VLM 自动质检"')
    app.HTML = replace(app.HTML, '<span class="server-pill">', '<a href="/auto/settings" target="_blank" class="server-pill">设置</a><span class="server-pill">')
    app.HTML = replace(app.HTML, '<span class="step-badge">先选数据，再执行</span>', '<span class="step-badge">手工执行</span>')
    app.HTML = replace(app.HTML, '    setWorkspace("collection");', '    setWorkspace("lerobot-visualization");')

    # Replay owns the playback nodes. Only the information area changes tabs.
    replay = app.LEROBOT_REPLAY_HTML
    replay = replace(replay, '    <aside class="panel">', '''    <aside class="panel">
      <nav class="unified-tabs"><button id="basicTab" class="selected">基本信息</button><button id="manualTab">人工筛查</button></nav>''')
    replay = replace(replay, '      <div class="meta-strip">', '      <div id="basicInfoPanel"><div class="meta-strip">')
    replay = replace(replay, '    </aside>', '''      </div>
      <section id="manualInfoPanel" hidden><button id="openGradeRecords">分级、问题记录与批量管理</button><iframe id="manualInfoFrame" title="当前 Episode 人工筛查"></iframe></section>
    </aside>''')
    replay = replace(replay, '  </style>', '''    .unified-tabs{display:flex;gap:8px;padding:10px 0;border-bottom:1px solid #dbe3ed}.unified-tabs button{flex:1}.unified-tabs .selected{background:#2563eb;color:white}#basicInfoPanel{min-height:0;display:flex;flex:1;flex-direction:column;gap:12px}#basicInfoPanel[hidden],#manualInfoPanel[hidden]{display:none}#manualInfoPanel{display:flex;flex:1;min-height:500px;flex-direction:column;gap:8px}#manualInfoFrame{width:100%;flex:1;min-height:520px;border:0}
  </style>''')
    replay = replace(replay, '    function sendVisualizationEpisode(episode = null) {', '''    let screeningEpisode = null;
    let manualLoaded = false;
    function syncManualContext() {
      if (!manualLoaded || !screeningEpisode) return;
      $("manualInfoFrame").contentWindow.postMessage({type:"unified-screening-context",dataset_path:params.get("dataset_path"),episode_index:Number(screeningEpisode.episode_index)},location.origin);
    }
    function infoTab(manual) {
      $("basicInfoPanel").hidden=manual; $("manualInfoPanel").hidden=!manual;
      $("basicTab").classList.toggle("selected",!manual); $("manualTab").classList.toggle("selected",manual);
      if(manual && !manualLoaded){
        manualLoaded=true;
        $("manualInfoFrame").src="/manual-screening/?embedded=1&dataset_path="+encodeURIComponent(params.get("dataset_path")||"")+"&episode_index="+Number(screeningEpisode?.episode_index||0);
      }
      if(manual) syncManualContext(); else requestAnimationFrame(drawChart);
    }
    $("basicTab").onclick=()=>infoTab(false); $("manualTab").onclick=()=>infoTab(true);
    $("manualInfoFrame").onload=syncManualContext;
    $("openGradeRecords").onclick=()=>parent.postMessage({type:"unified-open-records"},location.origin);
    window.addEventListener("message",event=>{
      if(event.origin!==location.origin||event.source!==$("manualInfoFrame").contentWindow)return;
      if(event.data?.type==="unified-screening-records-changed")parent.postMessage(event.data,location.origin);
      if(event.data?.type!=="unified-screening-select")return;
      if(event.data.dataset_path!==params.get("dataset_path")){
        parent.postMessage(event.data,location.origin);return;
      }
      const index=Number(event.data.episode_index);
      if(index!==Number(screeningEpisode?.episode_index)&&episodes.some(e=>Number(e.episode_index)===index))loadEpisode(index);
    });
    function sendVisualizationEpisode(episode = null) {
      screeningEpisode=episode; syncManualContext();''')
    # The chart takes the flexible space inside the basic tab as before.
    replay = replace(replay, '.unified-tabs{', '.panel{display:flex;flex-direction:column;overflow:hidden}.panel-head,.controls,.metrics,.unified-tabs{flex-shrink:0}#manualInfoPanel{min-height:0}#manualInfoFrame{min-height:0}.data-split{flex:1;min-height:0}.unified-tabs{')
    replay=replace(replay,"  </style>","    #manualInfoPanel,#manualInfoFrame{min-height:0}#manualInfoPanel{overflow:hidden}\n  </style>")
    app.LEROBOT_REPLAY_HTML = replay

    outer = app.LEROBOT_VISUALIZATION_HTML
    import os
    from html import escape
    outer=outer.replace('value="/srv/data/datasets/public/stage2_datasets"', 'value="'+escape(os.environ['DATAQC_HOME'],quote=True)+'"')
    outer = replace(outer, '          url = data.url;', '          url = data.url + "&dataset_path=" + encodeURIComponent(path);')
    outer = replace(outer, "corrections: {arm: $('screeningCorrectArm').value, object: $('screeningCorrectObject').value.trim()},", "corrections: {arm: $('screeningCorrectArm').value, object: $('screeningCorrectObject').value.trim(), objects_by_arm:screeningRecords.find(r=>Number(r.episode_index)===Number(currentEpisode.episode_index))?.corrections?.objects_by_arm||{}},")
    outer = replace(outer, "      if (event.data?.type !== 'lerobot-visualization-episode') return;", '''      if(event.data?.type==='unified-open-records'){$('screeningToggle').click();return;}
      if(event.data?.type==='unified-screening-records-changed'){loadScreeningRecords();return;}
      if(event.data?.type==='unified-screening-select'){
        openDataset(event.data.dataset_path).then(()=>setTimeout(()=>$('replayFrame').contentWindow.postMessage({type:'lerobot-visualization-select-episode',episode_index:event.data.episode_index},location.origin),500));return;
      }
      if (event.data?.type !== 'lerobot-visualization-episode') return;''')
    app.LEROBOT_VISUALIZATION_HTML = outer

    manual = app.MANUAL_SCREENING_HTML
    manual = replace(manual, '    const $ = id => document.getElementById(id);', '''    const $ = id => document.getElementById(id);
    const embedding=new URLSearchParams(location.search);
    const embedded=embedding.get("embedded")==="1";
    let contextPath=embedding.get("dataset_path")||"";
    let contextEpisode=Number(embedding.get("episode_index")||0);
    let applyingContext=false;
    if(embedded)document.documentElement.classList.add("unified-embedded");
    function applyContext(){
      if(!embedded||!manifest)return;
      const episode=manifest.episodes.find(e=>e.dataset_path===contextPath&&Number(e.episode_index)===contextEpisode);
      if(!episode)return;
      episodeFilter="all";refreshEpisodeOptions(episode.episode_key);
      applyingContext=true;renderEpisode();applyingContext=false;
    }
    window.addEventListener("message",event=>{
      if(event.origin!==location.origin||event.source!==parent||event.data?.type!=="unified-screening-context")return;
      const changed=contextPath!==event.data.dataset_path;
      contextPath=event.data.dataset_path;contextEpisode=Number(event.data.episode_index);
      if(changed)discover();else applyContext();
    });''')
    manual = replace(manual, 'fetchJson("/api/manual-screening/datasets")', 'fetchJson("/api/manual-screening/datasets"+(embedded?"?root="+encodeURIComponent(contextPath):""))')
    manual = replace(manual, '      const episodes = manifest?.episodes || [];', '      const episodes = (manifest?.episodes || []).filter(e=>!embedded||e.dataset_path===contextPath);')
    manual = replace(manual, '<div class="right-column">', '''<div class="right-column">
        <section class="panel" style="padding:12px;display:grid;gap:8px"><label>人工等级 <select id="unifiedGrade"><option value="">保持原等级</option><option>A</option><option>B</option><option>C</option><option>F</option></select></label><label><input id="unifiedBadTrajectory" type="checkbox"> 轨迹问题</label><label>审核备注 <input id="unifiedReviewNote" placeholder="可选：补充分级或轨迹原因"></label></section>''')
    manual = replace(manual, '      $("wrongArm").checked = types.has("wrong_arm");', '      $("unifiedGrade").value=record?.quality_grade||""; $("unifiedBadTrajectory").checked=types.has("bad_trajectory"); $("unifiedReviewNote").value=record?.review_note||"";\n      $("wrongArm").checked = types.has("wrong_arm");')
    manual = replace(manual, '      if ($("wrongArm").checked) errorTypes.push("wrong_arm");', '      if ($("unifiedBadTrajectory").checked) errorTypes.push("bad_trajectory");\n      if ($("wrongArm").checked) errorTypes.push("wrong_arm");')
    manual = replace(manual, 'postJson("/api/manual-screening/records", {', 'postJson("/api/lerobot-visualization/records", {\n          quality_grade:$("unifiedGrade").value,review_note:$("unifiedReviewNote").value,')
    manual = replace(manual, '      $("workspace").classList.remove("hidden");\n      renderEpisode();', '      $("workspace").classList.remove("hidden");\n      if(embedded)applyContext();else renderEpisode();')
    manual = replace(manual, '      if (!episode || !manifest) return;', '''      if (!episode || !manifest) return;
      if(embedded&&!applyingContext){
        contextPath=episode.dataset_path;contextEpisode=Number(episode.episode_index);
        parent.postMessage({type:"unified-screening-select",dataset_path:contextPath,episode_index:contextEpisode},location.origin);
      }''')
    manual = replace(manual, '        records.set(recordKey(data.record), data.record);', '        records.set(recordKey(data.record), data.record);\n        if(embedded)parent.postMessage({type:"unified-screening-records-changed"},location.origin);')
    manual = replace(manual, '        records.delete(recordKey(episode));', '        records.delete(recordKey(episode));\n        if(embedded)parent.postMessage({type:"unified-screening-records-changed"},location.origin);')
    manual = replace(manual, '  </style>', '''    .unified-embedded body{padding:0;margin:0}.unified-embedded .workspace{grid-template-columns:1fr}.unified-embedded .image-grid{grid-template-columns:repeat(3,minmax(0,1fr))}.unified-embedded header,.unified-embedded #scanRoot{display:none}.unified-embedded main{padding:8px}.unified-embedded .page{padding:8px}.unified-embedded figure img{max-height:150px;object-fit:contain}
  </style>''')
    manual=replace(manual,'  </style>', '''    .unified-embedded .intro{display:none}.unified-embedded .dataset-grid{display:flex;gap:8px}.unified-embedded .dataset-grid>div,.unified-embedded .dataset-meta{display:none}.unified-embedded .dataset-panel{padding:8px;margin-bottom:8px}.unified-embedded .dataset-grid button{flex:1}.unified-embedded .viewer-head{grid-template-columns:auto minmax(100px,1fr) auto auto auto;padding:8px}.unified-embedded .viewer-body{padding:8px}.unified-embedded .right-column{position:static}.unified-embedded .page{gap:8px}.unified-embedded .workspace{gap:8px}
  </style>''')
    app.MANUAL_SCREENING_HTML = manual

    # The manual panel follows the visualization's selected physical dataset.
    original_discover = app.manual_screening.discover_datasets
    registered = {}
    original_find = app.manual_screening.find_dataset_group
    def discover(root=None):
        if root:
            root=Path(root).expanduser().resolve()

        result=original_discover(root)
        if root and (root / 'meta' / 'info.json').is_file():
            for group in result.get('datasets', []):
                group.update(id=app.manual_screening.dataset_id(root), path=str(root), logical_path=str(root), name=root.name)
        for group in result.get('datasets', []): registered[group['id']]=group
        return result
    def find(group_id, root=None):
        return registered[group_id] if group_id in registered else original_find(group_id,root)
    app.manual_screening.discover_datasets=discover
    app.manual_screening.find_dataset_group=find

    from .compact_review import integrate_compact
    integrate_compact(app)

    from .collection_defaults import integrate_collection
    integrate_collection(app)

    from .simulation import integrate_simulation
    integrate_simulation(app)

    from .overview import integrate_overview
    integrate_overview(app)

    from .collection_layout import integrate_layout
    integrate_layout(app)
    from .direct_controls import integrate_direct_controls
    integrate_direct_controls(app)
    from .replay_grades import integrate_grades
    integrate_grades(app)
