'use strict';
const $=id=>document.getElementById(id),esc=escapeHtml;
const state={root:'',base:'',records:[],filter:'all',page:0,selected:new Set(),job:null,busy:false,target:'',sources:[],scanning:null,polling:false};
const latestStationaryThreshold=40;
// Older installations do not yet have the pagination controls in their template.
if(!$('reportPrev'))document.querySelector('.report-filters').insertAdjacentHTML('beforeend','<button type="button" id="reportPrev">上一页</button><span id="reportPageInfo"></span><button type="button" id="reportNext">下一页</button><button type="button" id="selectFilteredReports">选择全部筛选结果</button>');

const safe=fn=>async(...args)=>{try{return await fn(...args)}catch(e){notice(e.message)}};
function notice(text){$('gradeBulkFeedback').textContent=text;$('log').textContent=text+'\n'+($('log').textContent||'').slice(0,12000)}
async function api(path,body){const r=await fetch('/market/api/'+path,body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{});const d=await r.json();if(!r.ok)throw Error(d.detail||d.error||'请求失败');return d}
function disable(node,reason){if(!node)return;node.disabled=true;node.title=reason;node.setAttribute('aria-description',reason)}
function panel(name){for(const n of ['overview','qc','mapping','replay']){$(n+'Panel').classList.toggle('hidden',n!==name);$(n+'Tab').classList.toggle('active',n===name)}document.body.classList.toggle('replay-mode',name==='replay')}
function workspace(name){for(const n of ['collection','lerobotVisualization','lerobot','manualScreening','collectionDashboard','yolo'].filter(n=>$(n+'Workspace'))){$(n+'Workspace').classList.toggle('hidden',n!==name);$(n+'WorkspaceBtn').classList.toggle('active',n===name)}if(name!=='collection')document.body.classList.remove('replay-mode');else panel(document.querySelector('.tabs .active')?.id.replace('Tab','')||'overview');if(name==='collectionDashboard')$('collectionDashboardFrame').src='/collection-dashboard?embedded=1';if(name==='yolo'&&!$('yoloFrame').getAttribute('src'))$('yoloFrame').src='/yolo/?embedded=1';if($('yoloFrame')?.getAttribute('src'))$('yoloFrame').contentWindow.postMessage({type:'yolo-workspace-visibility',visible:name==='yolo'},location.origin)}
function changed(r){return !!r.source_grade&&!!r.auto_grade&&r.source_grade!==r.auto_grade}
function filtered(){const q=$('episodeIdentityFilter').value.trim().toLowerCase(),g=$('manualGradeFilter').value,n=$('manualNoteFilter').value.trim().toLowerCase();return state.records.filter(r=>(state.filter==='all'||(state.filter==='pending'?r.manual_pending:changed(r)))&&(!g||g===r.effective_grade)&&(!q||`${r.id} ${r.uuid||''} ${r.remote||''}`.toLowerCase().includes(q))&&(!n||(r.manual_note||'').toLowerCase().includes(n)))}
function selection(){const visible=[...document.querySelectorAll('.episode-check')];for(const c of visible)c.checked=state.selected.has(c.dataset.id);$('selectionInfo').textContent=`已选择 ${state.selected.size} 条 episode`;$('selectAllEpisodes').checked=visible.length>0&&visible.every(c=>c.checked);$('selectAllEpisodes').indeterminate=visible.some(c=>c.checked)&&!visible.every(c=>c.checked)}
function render(){const drafts=new Map([...document.querySelectorAll('[data-grade]')].filter(n=>n.value!==(n.dataset.currentGrade||'')).map(n=>[n.dataset.grade,{value:n.value,revision:n.dataset.revision}]));const rows=filtered(),pages=Math.max(1,Math.ceil(rows.length/50));state.page=Math.min(state.page,pages-1);$('episodeRows').innerHTML=rows.slice(state.page*50,(state.page+1)*50).map(r=>`<tr><td><input class="episode-check" type="checkbox" data-id="${esc(r.id)}"></td><td title="${esc(r.path)}">目录 ${esc(r.id)}<small class="grade-source">采集 ${esc(r.uuid||'未记录')}</small><button data-replay="${esc(r.id)}">回放</button></td><td>${esc(r.source_grade||'未记录')}<small class="grade-source">YOLO ${esc(r.yolo_grade||'未评')}</small></td><td>${esc(r.auto_grade||'待质检')}</td><td class="${changed(r)?'grade-changed':''}">${changed(r)?esc(r.source_grade+' → '+r.auto_grade):'—'}</td><td><b>${esc(r.effective_grade||'—')}</b><small class="grade-source">${r.manual_grade?'人工 · 最高优先':'自动 / 阶段人工'}</small></td><td><div class="grade-editor"><select data-grade="${esc(r.id)}" data-current-grade="${esc(r.manual_grade||'')}" data-revision="${esc(r.revision)}" aria-label="人工等级"><option value="">请选择</option>${['A','B','F'].map(g=>`<option ${g===(r.manual_grade||'')?'selected':''}>${g}</option>`).join('')}<option disabled title="本模式使用 A/B/F">C（不适用）</option></select><button class="grade-save" data-save="${esc(r.id)}">保存</button><small>${esc(r.manual_note||'')}</small></div></td><td class="grade-metrics">帧数 ${esc(r.frames??'—')}<span>FPS ${esc(r.metrics?.fps?.toFixed(2)??'—')}</span><span>${(r.phases||[]).map(p=>`${esc(p.label)} ${esc(p.effective_grade||'—')}`).join(' · ')}</span></td><td class="grade-notes">${esc(r.error||r.summary||'尚无全流程质检报告')}<div><button class="qc-report-launch" data-report="${esc(r.id)}">全部检查</button><button data-phases="${esc(r.id)}">各阶段人工质检</button></div></td></tr>`).join('')||'<tr><td colspan="9">没有符合条件的记录</td></tr>';
for(const b of document.querySelectorAll('[data-report-filter]')){const k=b.dataset.reportFilter,count=state.records.filter(r=>k==='all'||(k==='pending'?r.manual_pending:changed(r))).length;b.textContent=({all:'全部',changed:'等级变化',pending:'未人工质检'})[k]+` ${count}`;b.setAttribute('aria-pressed',String(k===state.filter))}
$('reportFilterCount').textContent=`符合条件 ${rows.length} / ${state.records.length} 条`;$('reportPageInfo').textContent=`第 ${state.page+1} / ${pages} 页 · 每页 50 条`;$('reportPrev').disabled=state.page===0;$('reportNext').disabled=state.page===pages-1;$('gradeComparisonSummary').textContent=`已质检 ${state.records.filter(r=>r.qc_status==='completed').length} 条 · 未人工质检 ${state.records.filter(r=>r.manual_pending).length} 条`;for(const node of document.querySelectorAll('[data-grade]')){const draft=drafts.get(node.dataset.grade);if(draft&&draft.value!==(node.dataset.currentGrade||'')){node.value=draft.value;node.dataset.revision=draft.revision}}selection();overview()}
function overview(){const rows=state.records,measured=rows.filter(r=>r.qc_status==='completed').map(r=>({...r.metrics,episode_id:r.name,quality_grade:r.effective_grade}));const chart=(title,field)=>pieChart(title,['A','B','F','未评'].map(g=>({label:g,color:({A:'#12b76a',B:'#f59e0b',F:'#ef4444','未评':'#98a2b3'})[g],count:rows.filter(r=>(field(r)||'未评')===g).length})));$('qcOverview').innerHTML=`<div class="qc-overview-head"><strong>数据概览</strong><span>导航 → 抓取 → 运送 → 递交</span></div><div class="overview-kpis">${kpi('总 Episode',rows.length)}${kpi('已质检',measured.length)}${kpi('未人工质检',rows.filter(r=>r.manual_pending).length)}${kpi('平均 FPS',formatChartValue(average(measured.map(r=>r.fps)),'Hz',2,true)||'—')}${kpi('平均时长',formatChartValue(average(measured.map(r=>r.duration_sec)),'s',1)||'—')}</div><div class="overview-charts">${chart('采用等级',r=>r.effective_grade)}${['navigation','grasp','transport','handover'].map((p,i)=>chart(['导航','抓取','运送','递交'][i]+'等级',r=>r.phases?.find(s=>s.phase===p)?.effective_grade)).join('')}${targetValuePieChart(measured,'state_dim','State 维度（23D）',23,'D')}${targetValuePieChart(measured,'action_dim','Action 维度（23D）',23,'D')}${targetValuePieChart(measured,'camera_view_count','相机视角（3路）',3,'路')}${valueBarChart(measured.slice(0,100),'fps','FPS','Hz',2)}${valueBarChart(measured.slice(0,100),'duration_sec','时长','s',2)}${valueBarChart(measured.slice(0,100),'max_state_step','最大 state 跳变','',4)}</div><p class="hint">图表样式沿用原质检页；数值来自本模式报告，未测量项不填默认值。导航和运送的静止等待不套用抓取阶段的剔帧规则。</p>`}
function directoryOptions(){
  const selected=$('customDatasetPath').value.trim();
  const sources=[...new Set([...state.sources,...(selected?[selected]:[])])];
  const options='<option value="">请选择质检 / 转换目录</option>'+sources.map(path=>{
    const rows=state.records.filter(r=>r.path===path||r.path.startsWith(path+'/'));
    const counts=['A','B','F'].map(g=>`${g} ${rows.filter(r=>r.effective_grade===g).length}`).join(' · ');
    return `<option value="${esc(path)}">${esc(path.slice(state.base.length+1))}（${rows.length} 条 · ${counts}）</option>`;
  }).join('');
  if($('sourceDatasetSelect').innerHTML!==options)$('sourceDatasetSelect').innerHTML=options;
  $('sourceDatasetSelect').value=selected;
}
function directoryInfo(){
  $('sourceDatasetScanInfo').textContent=`全部录制目录共 ${state.records.length} 条商超录制；数量和等级每 5 秒自动刷新`;
  $('pathRows').innerHTML=`<div class="dataset-path-row"><span class="dataset-path-label">统计范围</span><span class="dataset-path-value">全部录制目录</span></div><div class="dataset-path-row"><span class="dataset-path-label">质检 / 转换</span><span class="dataset-path-value">${esc(state.target||'尚未选择目录')}</span></div><div class="dataset-path-row"><span class="dataset-path-label">分级输出</span><span class="dataset-path-value">${esc(state.base)}/lerobot/market_all_&lt;任务编号&gt;/&lt;等级&gt;/</span></div>`;
}
async function scan(){
  if(state.scanning)return state.scanning;
  state.scanning=(async()=>{
    const d=await api('records');
    const changed=JSON.stringify(state.records)!==JSON.stringify(d.records);
    state.root=d.root;state.records=d.records;state.sources=d.sources||state.sources;
    state.selected=new Set([...state.selected].filter(id=>d.records.some(r=>r.id===id)));
    directoryOptions();directoryInfo();
    if(changed||!$('episodeRows').children.length)render();
    return d;
  })();
  try{return await state.scanning}finally{state.scanning=null}
}
async function chooseDirectory(){
  const requested=$('customDatasetPath').value.trim();
  if(!requested){state.target='';directoryOptions();directoryInfo();await mapping();return null}
  if(requested===state.base)throw Error('请先选择要质检或转换的具体录制目录');
  $('applyCustomDatasetBtn').disabled=true;
  try{
    const d=await api('records?'+new URLSearchParams({root:requested}));
    if(requested!==$('customDatasetPath').value.trim())return null;
    if(d.root===state.base)throw Error('请先选择要质检或转换的具体录制目录');
    state.target=d.root;$('customDatasetPath').value=d.root;$('hdf5Root').value=d.root;
    directoryOptions();directoryInfo();await mapping();return d;
  }finally{$('applyCustomDatasetBtn').disabled=false}
}
async function detail(id){return api('episode?'+new URLSearchParams({root:state.root,id}))}
async function replay(id,frame=0){const row=state.records.find(r=>r.id===id)||state.records.find(r=>state.selected.has(r.id))||filtered()[0];if(!row)throw Error('没有可回放的商超录制');panel('replay');$('replayFrame').src='/market/replay/?'+new URLSearchParams({root:state.root,qc_root:row.path,qc_frame:frame})}
async function report(id){const r=await detail(id);window.QCReport.open(r.presentation,{episode:r.name,onFrame:f=>safe(()=>replay(id,f))()})}
async function save(id){if(state.busy)return;const r=state.records.find(r=>r.id===id),select=[...document.querySelectorAll('[data-grade]')].find(s=>s.dataset.grade===id);if(!r||!select?.value)throw Error('请选择人工等级');state.busy=true;try{const result=await api('review',{root:state.root,id,uuid:r.uuid,revision:select.dataset.revision||r.revision,grade:select.value,note:r.manual_note||''});state.records=state.records.map(v=>v.id===id?result:v);render();notice('人工等级已保存');await scan()}finally{state.busy=false}}
async function start(kind){
  if(state.starting)return;state.starting=true;
  try{
    if(!$('customDatasetPath').value.trim())throw Error('请先选择要质检或转换的具体录制目录');
    const loaded=await chooseDirectory();
    if(!loaded)throw Error('目录选择已变化，请重新点击');
    const grades=[...document.querySelectorAll('[data-export-grade]:checked')].map(c=>c.value);
    if(kind!=='qc'&&!grades.length)throw Error('请选择转换等级');
    await api('jobs',{root:loaded.root,kind,force:$('qcForce').checked,grades:grades.length?grades:['A','B'],manual_only:$('marketManualOnly').checked});
    await poll();notice('任务已启动，仅处理：'+loaded.root);
  }finally{state.starting=false}
}
async function mapping(){const d=await api('mapping?'+new URLSearchParams({root:state.target||state.root}));$('mappingInfo').textContent=d.path||'尚未完成转换；转换后显示来源与等级内编号映射';$('mappingStats').textContent=d.datasets?.length?d.datasets.map(v=>`${v.grade||'旧混合数据'} ${v.episodes} 条`).join(' · '):'';$('mappingRows').innerHTML=d.episodes.map((r,i)=>`<tr><td>${esc(r.grade||'—')}</td><td>—</td><td>${esc(r.source_relative_path||r.source)}</td><td>${esc(r.dataset_path||d.path)}</td><td>${esc(r.episode_index??i)}</td><td>${esc(r.source_uuid||'')}</td></tr>`).join('')}
async function poll(){
  if(state.polling)return;state.polling=true;
  try{
    const d=await api('jobs'),j=d.jobs[0],prev=state.job;state.job=j;
    const busy=d.jobs.some(v=>['queued','running'].includes(v.status));
    for(const node of document.querySelectorAll('[data-stage="qc"],[data-stage="lerobot"],#qcAllRecordings,#qcAllToLerobot'))node.disabled=busy;
    $('stopJobBtn').disabled=!busy;
    if(j)$('log').textContent=`${j.kind} · ${j.status} · ${j.done||0}/${j.total||0}\n${j.message||''}\n`+JSON.stringify(j.result||j.errors||{},null,2);
    await scan();
    if(prev&&['running','queued'].includes(prev.status)&&!busy)await mapping();
  }finally{state.polling=false}
}
for(const n of ['overview','qc','mapping'])$(n+'Tab').onclick=()=>panel(n);$('replayTab').onclick=$('replayBtn').onclick=safe(()=>replay());
for(const n of ['collection','lerobotVisualization','lerobot','manualScreening','collectionDashboard','yolo'].filter(n=>$(n+'WorkspaceBtn')))$(n+'WorkspaceBtn').onclick=()=>workspace(n);
// Keep the original LeRobot browser, with this mode's unified output root.
$('lerobotVisualizationFrame').addEventListener('load',()=>{const input=$('lerobotVisualizationFrame').contentDocument?.getElementById('rootPath');if(input&&state.base)input.value=state.base+'/lerobot'});
$('sourceDatasetSelect').previousElementSibling.textContent='选择质检 / 转换目录';
$('sourceDatasetSelect').setAttribute('aria-label','选择质检 / 转换目录');
document.querySelector('label[for="customDatasetPath"]').textContent='自定义质检 / 转换目录';
$('customDatasetPath').placeholder='选择或输入要质检 / 转换的录制目录';
$('applyCustomDatasetBtn').textContent='使用此目录';
$('applyCustomDatasetBtn').onclick=safe(chooseDirectory);
$('customDatasetPath').oninput=()=>{state.target='';directoryInfo()};
$('customDatasetPath').onkeydown=e=>{if(e.key==='Enter')safe(chooseDirectory)()};
$('sourceDatasetSelect').onchange=safe(async e=>{$('customDatasetPath').value=e.target.value;state.target='';directoryInfo();await chooseDirectory()});
$('refreshBtn').onclick=safe(async()=>{await poll();await mapping()});
for(const b of document.querySelectorAll('[data-report-filter]'))b.onclick=()=>{state.filter=b.dataset.reportFilter;state.page=0;render()};
for(const id of ['manualGradeFilter','episodeIdentityFilter','manualNoteFilter'])$(id).oninput=()=>{state.page=0;render()};
$('reportPrev').onclick=()=>{state.page--;render()};$('reportNext').onclick=()=>{state.page++;render()};$('clearSelectionBtn').onclick=()=>{state.selected.clear();selection()};$('selectFilteredReports').onclick=()=>{filtered().forEach(r=>state.selected.add(r.id));selection()};$('selectPendingReview').textContent='一键筛选未人工质检';$('selectPendingReview').onclick=()=>{state.filter='pending';state.page=0;render()};$('selectAllEpisodes').onchange=e=>{for(const c of document.querySelectorAll('.episode-check')){if(e.target.checked)state.selected.add(c.dataset.id);else state.selected.delete(c.dataset.id)}selection()};
$('episodeRows').onchange=e=>{if(e.target.matches('.episode-check')){if(e.target.checked)state.selected.add(e.target.dataset.id);else state.selected.delete(e.target.dataset.id);selection()}};
$('episodeRows').onclick=safe(async e=>{const b=e.target.closest('button');if(!b)return;if(b.dataset.save)return save(b.dataset.save);if(b.dataset.report)return report(b.dataset.report);if(b.dataset.replay||b.dataset.phases)return replay(b.dataset.replay||b.dataset.phases)});
for(const b of document.querySelectorAll('[data-stage]')){const stage=b.dataset.stage;if(stage==='qc')b.onclick=safe(()=>start('qc'));else if(stage==='lerobot')b.onclick=safe(()=>start('qc_export'));else disable(b,({repair:'各阶段含导航与等待，不适用抓取静止剔帧',delete:'全流程数据暂不支持在此删除',split_lerobot_stages:'各阶段按完整流程合并，不适用左右手拆分',lerobot_post_qc:'原 LeRobot 复检规则针对抓取；转换时已执行全流程结构校验',sync_hdf5_grades:'人工结果写入质检记录；暂不改写原始 HDF5'})[stage]||'本模式暂不适用')}
for(const id of ['selectGradeCBtn','selectCameraMissingOver20Btn','selectStationaryOver15Btn','manualProblemFilter'])disable($(id),'本模式没有对应的分级或统计规则');
for(const b of document.querySelectorAll('[data-grade-source]'))disable(b,'全流程默认人工最高优先；请逐条人工确认等级');
for(const id of ['hostMachine','vlmEnabled','taskText','robotType','convertJobs','lerobotWorkers','cameraCount','headCameraSource','profile','repoId','gpuDevice','useDocker','overwriteHdf5','lerobotCuda','alohaIncludeBaseAction'])disable($(id),'本模式沿用录制的各阶段任务与相机，使用统一转换配置；此原页面设置暂不适用');
$('taskText').value='导航 → 抓取 → 运送 → 递交';$('taskTextHint').textContent='每条记录保留各阶段原始提示词；YOLO 结果独立参考，人工等级优先。';
const settings=document.createElement('div');settings.className='collection-qc-options market-export-options';settings.innerHTML='<span>转换等级</span>'+['A','B','F'].map(g=>`<label><input type="checkbox" data-export-grade value="${g}" ${g!=='F'?'checked':''}>${g}</label>`).join('')+'<label><input id="marketManualOnly" type="checkbox">仅人工确认</label><small>先增量质检，再按等级分别输出；每个 A/B/F 目录独立包含 data、meta、videos。</small>';$('lerobotActionsTitle').parentElement.append(settings);
if($('qcAllRecordings')){$('qcAllRecordings').textContent='一键质检当前目录';$('qcAllRecordings').onclick=safe(()=>start('qc'));$('qcAllToLerobot').textContent='当前目录质检并转 LeRobot';$('qcAllToLerobot').onclick=safe(()=>start('qc_export'));$('viewAllRecordingReports').textContent='查看全部目录报告';$('viewAllRecordingReports').onclick=safe(async()=>{await scan();panel('qc')});const card=$('qcAllRecordings').closest('section');card.querySelector('h3').textContent='选择目录后处理';for(const p of card.querySelectorAll('p'))p.remove();}
$('stopJobBtn').onclick=safe(async()=>{const d=await api('jobs'),j=d.jobs.find(v=>['queued','running'].includes(v.status));if(j)await api('jobs/'+j.id+'/cancel',{});notice('已请求停止；已完成的质检报告保留')});
$('qualityCheckItems').innerHTML=['实际阶段边界与帧覆盖','导航 / 运送底盘数据','抓取闭合与递交释放','三路视频与 HDF5 完整性','State / Action 与时间戳','YOLO 参考与独立人工评级'].map(v=>`<div class="quality-check-item"><div class="quality-check-name">${v}</div></div>`).join('');
window.addEventListener('message',safe(async e=>{if(e.origin===location.origin&&e.source===$('replayFrame').contentWindow&&e.data?.type==='pipeline-manual-failure-updated')await scan()}));
safe(async()=>{
  const cfg=await api('config');state.base=state.root=cfg.root;state.sources=cfg.sources;
  $('customDatasetPath').value='';directoryOptions();directoryInfo();
  try{localStorage.removeItem('market-qc-source-directory')}catch{}
  setInterval(()=>safe(poll)(),cfg.refresh_interval_seconds*1000||5000);
  await poll();await mapping();
})();
