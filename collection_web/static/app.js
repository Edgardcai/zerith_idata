'use strict';
const $=id=>document.getElementById(id);
let appliedScene=null;
let depthSupported=false, taskIdEdited=false, taskWasEditable=null;
let token='', snapshot=null, busy=false, restored=false, cameraTimer=null, toastTimer=null, directoryPreview='', pendingParse=Promise.resolve();
const labels={idle:'准备采集',connecting:'正在连接采集服务',waiting:'等待 Meta Quest',recording:'正在采集',saving:'正在保存',finalizing:'正在整理',disconnected:'会话连接中断',closed:'会话已结束',closing:'正在结束会话',rejected:'启动未通过'};
const stateLabels={recording:'采集中',saving:'保存中',finalizing:'整理中',completed:'已保存',deleted:'已放弃',deleting:'删除中',error:'待检查'};
let teleopConfigLoaded=false,teleopSequence=null,teleopPid=null,teleopVoice=false;
function sayTeleop(text){if(teleopVoice&&'speechSynthesis' in window){speechSynthesis.cancel();const speech=new SpeechSynthesisUtterance(text);speech.lang='zh-CN';speechSynthesis.speak(speech);}}
function renderTeleop(){const t=snapshot?.teleop||{};const panel=document.querySelector('.teleop-panel');panel.dataset.phase=t.phase||'offline';$('teleopState').textContent=t.title||'等待遥操状态';$('teleopWarning').hidden=!t.warnings?.length;$('teleopWarning').textContent=(t.warnings||[]).join('；');
 if(!teleopConfigLoaded&&t.config){$('fixedLift').checked=t.config.lift_enabled;$('fixedLiftHeight').value=t.config.lift_height_m;teleopConfigLoaded=true;}
 const canEdit=t.available&&t.phase==='uninitialized'&&!snapshot.collection.current&&!busy;['fixedLift','fixedLiftHeight','saveTeleop'].forEach(id=>$(id).disabled=!canEdit);
 $('liftApplied').textContent=t.lift_target_m!=null?`保持 ${t.lift_target_m.toFixed(3)} m`:(t.config?.lift_enabled?`下次初始化到 ${Number(t.config.lift_height_m).toFixed(3)} m`:'未固定 · 反初始化后可设置');
 if(teleopPid===t.pid&&teleopSequence!==null&&t.calibration_seq>teleopSequence){toast('标定成功，短按 A 启动遥操');sayTeleop('标定成功，短按 A 启动遥操');}
 teleopSequence=t.calibration_seq??null;teleopPid=t.pid;
}
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function toast(text,error=false){clearTimeout(toastTimer);$('toast').textContent=text;$('toast').className='toast'+(error?' error':'');$('toast').hidden=false;toastTimer=setTimeout(()=>$('toast').hidden=true,error?5500:2200);}
async function api(path,body){const response=await fetch(path,{method:body?'POST':'GET',headers:body?{'Content-Type':'application/json','X-Collection-Token':token}:{},body:body?JSON.stringify(body):undefined,cache:'no-store'});const result=await response.json();if(!response.ok)throw Error(result.error||'请求失败');return result;}
function task(){return {task_name:$('prompt').value,task_id:appliedScene?.task_id??$('taskId').value,scene_id:appliedScene?.scene_id??$('sceneId').value,subtask_num:$('subtasks').value,frequency:$('frequency').value,max_episode_time:$('maxTime').value,left:$('left').value,right:$('right').value,lift_height:$('liftHeight').value,record_depth:$('recordDepth').checked};}
function confirm(title,text,accept='确认'){return new Promise(resolve=>{$('confirmTitle').textContent=title;$('confirmText').textContent=text;$('confirmAccept').textContent=accept;$('confirm').returnValue='cancel';$('confirm').addEventListener('close',()=>resolve($('confirm').returnValue==='ok'),{once:true});$('confirm').showModal();});}
async function action(callback){if(busy)return;busy=true;renderAvailability();try{await callback();await poll();}catch(error){toast(error.message,true);}finally{busy=false;renderAvailability();}}
async function parse(){const prompt=$('prompt').value, left=$('left').value, right=$('right').value;try{const targets=await api('/api/task/parse',{prompt});if($('prompt').value!==prompt)return;if($('left').value===left)$('left').value=targets.left;if($('right').value===right)$('right').value=targets.right;queueDirectory();}catch(error){toast(error.message,true);}}
function scenePending(){return !appliedScene||$('taskId').value!==String(appliedScene.task_id)||$('sceneId').value!==String(appliedScene.scene_id);}
function renderDirectory(){const c=snapshot?.collection;const editing=['idle','closed','rejected'].includes(c?.phase||'idle');const path=editing?appliedScene?.dataset:c?.session?.dataset;$('directoryLabel').textContent='目录';$('directory').textContent=path||'请先应用场景';$('copyPath').disabled=!path;$('sceneHint').textContent=!editing?'采集中':scenePending()?'待应用':'已应用';}
function queueDirectory(){directoryPreview=appliedScene?.dataset||'';renderDirectory();renderAvailability();if(snapshot){renderEpisodeGroups();refreshOpenGroups();}}

function checks(items){$('checks').innerHTML=items.map(c=>`<button class="check ${c.ok?'good':'bad'}" type="button" title="${esc(c.detail)}" data-detail="${esc(c.detail)}"><i></i>${esc(c.label)}<span>${c.ok?'✓':'!'}</span></button>`).join('');}
function renderAvailability(){const phase=snapshot?.collection?.phase||'idle';const editable=['idle','closed','rejected'].includes(phase);$('start').disabled=busy||!editable||scenePending();['taskId','sceneId','applyScene'].forEach(id=>$(id).disabled=busy||!editable);$('end').disabled=busy||editable||Boolean(snapshot?.collection?.current);$('check').disabled=busy;$('cameraToggle').disabled=busy;document.querySelectorAll('#taskForm input,#taskForm textarea').forEach(el=>el.disabled=busy||!editable);$('recordDepth').disabled=busy||!editable||!depthSupported;document.querySelectorAll('[data-grade]').forEach(el=>el.disabled=busy||el.dataset.editable!=='1');}
function duration(seconds){seconds=Math.max(0,Math.floor(Number(seconds)||0));return `${String(Math.floor(seconds/60)).padStart(2,'0')}:${String(seconds%60).padStart(2,'0')}`;}
function renderJoints(joints){const defaults=[{motor_id:2,label:'升降柱',unit:'m'},{motor_id:3,label:'腰部俯仰',unit:'rad'},{motor_id:4,label:'腰部旋转',unit:'rad'},{motor_id:5,label:'头部旋转',unit:'rad'},{motor_id:6,label:'头部俯仰',unit:'rad'}];$('joints').innerHTML=defaults.map(d=>{const j=joints.find(j=>j.motor_id===d.motor_id);let value='—',unit=d.unit;if(j?.ok&&Number.isFinite(j.value))value=j.value.toFixed(3);return `<div class="joint"><span>${d.label}</span><strong>${value}<small>${unit}</small></strong></div>`;}).join('');}
function episodeRowsHTML(rows){return rows.map(r=>{
 const done=r.state==='completed',detail=r.detail||{},progress=r.progress||{};
 const warnings=detail.warnings?.length?detail.warnings:detail.error?[detail.error]:[];
 return `<tr><td class="episode-name" title="${esc(r.uuid)}">${esc(r.name)}</td><td><div class="grades" role="group" aria-label="${esc(r.name)} 评级">${['A','B','F'].map(g=>`<button class="grade ${r.grade===g?'selected':''}" data-id="${r.id}" data-grade="${g}" data-editable="${done?'1':'0'}" aria-label="${esc(r.name)} 评为 ${g}" aria-pressed="${r.grade===g}" ${done&&!busy?'':'disabled'}>${g}</button>`).join('')}</div></td><td><span class="badge ${done?'done':''}">${stateLabels[r.state]||esc(r.state)}</span></td><td>${duration(detail.duration_s??progress.elapsed_s)} · ${detail.frames??progress.frames??'—'} 帧</td><td class="quality">${warnings.length?`<details data-quality="${r.id}"><summary>${warnings.length} 项提示</summary><p>${warnings.map(esc).join('<br>')}</p></details>`:'—'}</td><td><div class="row-actions"><button class="text-button" data-review="${r.id}" ${done?'':'disabled'}>查看</button><button class="text-button discard" data-delete="${r.id}" ${done?'':'disabled'}>放弃</button></div></td></tr>`;
 }).join('');}

const groupNodes=new Map(),datasetRows=new Map(),datasetRequests=new Map(),datasetVersions=new Map();
let groupSummaries=[],groupCurrent=null;
function currentRecordsDataset(){
 const c=snapshot?.collection;
 const editing=['idle','closed','rejected'].includes(c?.phase||'idle');
 return (editing&&directoryPreview)||c?.session?.dataset||groupSummaries[0]?.dataset||'';
}
function groupCounts(group){return `A ${group.A||0} · B ${group.B||0} · F ${group.F||0}`;}
function groupBody(node){
 const body=node.querySelector('.dataset-body'),path=node.dataset.path;
 if(!node.open){body.replaceChildren();body.dataset.signature='';return;}
 const rows=datasetRows.get(path),group=groupSummaries.find(g=>g.dataset===path)||{};
 const signature=JSON.stringify([rows,group]);
 if(body.dataset.signature===signature)return;
 const openWarnings=[...body.querySelectorAll('details[data-quality][open]')].map(el=>el.dataset.quality);
 body.dataset.signature=signature;
 body.innerHTML=`<div class="dataset-meta"><code>${esc(path)}</code><span>${esc(groupCounts(group))}</span></div>`+(rows===undefined?'<div class="dataset-empty">正在加载记录…</div>':!rows.length?'<div class="dataset-empty">此目录暂无采集记录</div>':`<div class="table-scroll"><table><thead><tr><th>编号</th><th>评级</th><th>状态</th><th>时长 / 帧数</th><th>检查</th><th>操作</th></tr></thead><tbody>${episodeRowsHTML(rows)}</tbody></table></div>`);
 for(const id of openWarnings){const detail=body.querySelector(`[data-quality="${id}"]`);if(detail)detail.open=true;}
}
function renderEpisodeGroups(){
 const selected=currentRecordsDataset(),changed=selected!==groupCurrent;groupCurrent=selected;
 const groups=[...groupSummaries];
 if(selected&&!groups.some(g=>g.dataset===selected))groups.unshift({dataset:selected,total:0,completed:0});
 groups.sort((a,b)=>Number(b.dataset===selected)-Number(a.dataset===selected));
 const paths=new Set(groups.map(g=>g.dataset));
 for(const [path,node] of groupNodes)if(!paths.has(path)){node.remove();groupNodes.delete(path);datasetRows.delete(path);datasetVersions.delete(path);}
 for(const group of groups){
  const path=group.dataset;let node=groupNodes.get(path);
  if(!node){
   node=document.createElement('details');node.className='dataset-group';node.dataset.path=path;
   node.innerHTML='<summary><span class="dataset-chevron" aria-hidden="true"></span><span class="dataset-name"></span><span class="dataset-current">当前目录</span><span class="dataset-total"></span></summary><div class="dataset-body"></div>';
   node.open=path===selected;
   node.addEventListener('toggle',()=>{groupBody(node);if(node.open)loadDataset(path);});
   groupNodes.set(path,node);
  }
  if(changed)node.open=path===selected;
  node.classList.toggle('is-current',path===selected);
  node.querySelector('.dataset-name').textContent=path.split('/').filter(Boolean).at(-1)||path;
  node.querySelector('.dataset-name').title=path;
  node.querySelector('.dataset-current').hidden=path!==selected;
  node.querySelector('.dataset-total').textContent=`${group.retained??((group.total||0)-(group.deleted||0))} 条保留记录`;
  const index=groups.indexOf(group);
  if($('episodes').children[index]!==node)$('episodes').insertBefore(node,$('episodes').children[index]||null);
  groupBody(node);
 }
 const current=groups.find(g=>g.dataset===selected);
 $('gradeCounts').textContent=current?groupCounts(current):'';
 $('episodeCount').textContent=current?.completed||0;
 $('empty').hidden=groups.length>0;
}
async function loadDataset(path,force=false){
 if(datasetRequests.has(path)){await datasetRequests.get(path);if(!force)return;}
 const version=JSON.stringify(groupSummaries.find(g=>g.dataset===path)||{});
 const previous=datasetVersions.get(path);
 if(!force&&datasetRows.has(path)&&previous?.version===version&&Date.now()-previous.at<15000&&snapshot?.collection?.current?.dataset!==path)return;
 const request=(async()=>{try{
  const rows=(await api('/api/episodes?dataset='+encodeURIComponent(path))).episodes;
  datasetRows.set(path,rows);datasetVersions.set(path,{version,at:Date.now()});const node=groupNodes.get(path);if(node)groupBody(node);
 }catch(error){const node=groupNodes.get(path);if(node?.open){const body=node.querySelector('.dataset-body');body.dataset.signature='';body.textContent='记录加载失败：'+error.message;}}
 finally{datasetRequests.delete(path);}})();
 datasetRequests.set(path,request);return request;
}
async function refreshOpenGroups(){await Promise.all([...groupNodes].filter(([,node])=>node.open).map(([path])=>loadDataset(path)));}

function render(){if(!snapshot)return;const d=snapshot.device,c=snapshot.collection,camera=snapshot.camera;const online=d.checks.find(x=>x.key==='service')?.ok;$('connection').textContent=online?'已连接':'未连接';$('connection').classList.toggle('online',Boolean(online));$('battery').textContent=`电量 ${d.battery??'—'}${d.battery===null?'':'%'}`;checks(d.checks);
 $('phase').textContent=labels[c.display_phase]||c.display_phase;$('episodeCount').textContent=c.counts.completed;const current=c.current;const total=c.session?.config?.subtask_num||2;$('stage').textContent=current?`${Math.min((current.progress.completed_steps||0)+1,total)} / ${total}`:'—';$('elapsed').textContent=duration(current?.progress?.elapsed_s);$('phaseHint').textContent=current?`${current.name} · ${current.state==='recording'?((current.progress.completed_steps||0)>=total?'全部阶段已完成，等待保存':Math.min((current.progress.completed_steps||0)+1,total)<total?'短按 Y 进入下一阶段':'短按 Y 完成本条并保存'):'等待保存完成'}`:(c.phase==='waiting'?'长按 Y 开始下一条':'');
 $('alert').hidden=!c.error;$('alert').textContent=c.error||'';renderDirectory();$('sessionLabel').textContent=['idle','closed','rejected'].includes(c.phase)?'':'会话进行中';
 if(!restored&&c.session){const t=c.session.config;$('recordDepth').checked=!['idle','closed','rejected'].includes(c.phase)&&t.record_depth===true;$('prompt').value=t.task_name;if(!['idle','closed','rejected'].includes(c.phase)){$('taskId').value=t.task_id;$('sceneId').value=t.scene_id||1;}$('subtasks').value=['idle','closed','rejected'].includes(c.phase)?2:t.subtask_num;$('frequency').value=t.frequency;$('maxTime').value=t.max_episode_time;$('left').value=c.session.targets.left;$('right').value=c.session.targets.right;$('liftHeight').value=c.session.targets.lift_height||'';restored=true;queueDirectory();}
 const editableTask=['idle','closed','rejected'].includes(c.phase);if(editableTask&&taskWasEditable===false)taskIdEdited=false;if(editableTask&&!taskIdEdited&&snapshot.default_task_id&&$('taskId').value!==String(snapshot.default_task_id)){$('taskId').value=snapshot.default_task_id;queueDirectory();}taskWasEditable=editableTask;
 renderJoints(d.joints);$('vrStatus').textContent=d.vr_connected?'VR 已连接':'等待 VR';renderEpisodeGroups();
 $('cameraGrid').hidden=!camera.enabled;$('cameraToggle').textContent=camera.enabled?'关闭预览':'打开预览';$('cameraHint').textContent=camera.error|| (camera.enabled?'实时':'');document.querySelectorAll('[data-camera-state]').forEach(el=>{const v=camera.streams[el.dataset.cameraState];el.textContent=v?.fresh?'实时':'等待画面';});
 if(camera.enabled&&!cameraTimer){loadImages();cameraTimer=setInterval(loadImages,350);}else if(!camera.enabled&&cameraTimer){clearInterval(cameraTimer);cameraTimer=null;document.querySelectorAll('[data-camera]').forEach(img=>img.removeAttribute('src'));}
 $('diagnostics').textContent=JSON.stringify({checks:d.checks,accepted:c.accepted,connected:c.connected,error:c.error,responses:c.responses.slice(-5),events:c.events.slice(-10)},null,2);renderAvailability();renderTeleop();renderStages();}
function renderStages(){
 const c=snapshot?.collection;
 const active=c&&!['idle','closed','rejected'].includes(c.phase);
 const total=Number(active?c.session?.config?.subtask_num:$('subtasks').value);
 if(!Number.isInteger(total)||total<1||total>20){$('stagePlan').replaceChildren();$('stageHint').textContent='请输入 1—20 的整数阶段数';return;}
 const completed=c?.current?Number(c.current.progress?.completed_steps||0):0;
 const recording=c?.current?.state==='recording';
 $('stagePlan').innerHTML=Array.from({length:total},(_,index)=>{
  const title=total===2?(index===0?'左手抓取':'右手抓取'):`阶段 ${index+1}`;
  const state=index<completed?'complete':recording&&index===completed?'active':'';
  return `<li class="${state}"><span class="stage-number">${index<completed?'✓':index+1}</span><span>${esc(title)}</span></li>`;
 }).join('');
 $('stageHint').textContent=recording&&completed<total?(completed+1<total?'短按 Y 进入下一阶段':'短按 Y 结束并保存'):(c?.current?'正在保存…':'长按 Y 超过 2 秒开始 · 短按 Y 切换阶段 / 保存');
}

function loadImages(){if($('review').open)return;document.querySelectorAll('[data-camera]').forEach(img=>{if(img.dataset.loading==='1')return;img.dataset.loading='1';img.onload=img.onerror=()=>img.dataset.loading='0';img.src=`/api/camera/${img.dataset.camera}?t=${Date.now()}`;});}
async function poll(){try{const [status,groups]=await Promise.all([api('/api/status?compact=1'),api('/api/episode-groups')]);snapshot=status;groupSummaries=groups.groups;render();await refreshOpenGroups();}catch(error){$('connection').textContent='网站连接中断';$('connection').classList.remove('online');$('alert').hidden=false;$('alert').textContent=error.message;document.querySelectorAll('#start,#end,#check,#cameraToggle,#applyScene').forEach(el=>el.disabled=true);}}
$('taskId').addEventListener('input',()=>{taskIdEdited=true;queueDirectory();});
$('sceneId').addEventListener('input',queueDirectory);
$('sceneId').addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();$('applyScene').click();}});
$('applyScene').onclick=()=>action(async()=>{appliedScene=await api('/api/scene/apply',{task_id:$('taskId').value,scene_id:$('sceneId').value});$('taskId').value=appliedScene.task_id;$('sceneId').value=appliedScene.scene_id;queueDirectory();toast(`已应用场景 ${appliedScene.scene_id}`);});
$('prompt').addEventListener('change',()=>{pendingParse=parse();});$('taskForm').addEventListener('input',()=>{queueDirectory();renderStages();});$('taskForm').addEventListener('submit',e=>e.preventDefault());$('checks').onclick=e=>{const b=e.target.closest('[data-detail]');if(b)toast(b.dataset.detail,b.classList.contains('bad'));};
$('check').onclick=()=>action(async()=>{await pendingParse;const r=await api('/api/preflight',task());checks(r.checks);if(!r.ready){$('alert').hidden=false;$('alert').textContent=r.checks.filter(c=>!c.ok).map(c=>`${c.label}：${c.detail}`).join('；');throw Error('检测未通过：'+r.checks.filter(c=>!c.ok).map(c=>c.label).join('、'));}toast('设备检测通过');});
$('start').onclick=()=>action(async()=>{await pendingParse;if(scenePending())throw Error('请先应用场景');const t=task();await api('/api/task/directory',t);if(!await confirm('启动采集？',`共 ${t.subtask_num} 个阶段，${t.record_depth?'记录彩色和深度图':'仅记录彩色图像'}，由 Meta Quest 控制每条录制。`,'启动'))return;await api('/api/session/start',t);restored=false;toast('采集配置已提交');});
$('end').onclick=()=>action(async()=>{if(!await confirm('结束采集会话？','关闭本次采集连接。','结束会话'))return;await api('/api/session/end',{});$('recordDepth').checked=false;$('subtasks').value=2;toast('会话连接已关闭');});
$('cameraToggle').onclick=()=>action(async()=>{await api('/api/cameras',{enabled:!snapshot?.camera?.enabled});});
$('saveTeleop').onclick=()=>action(async()=>{await api('/api/teleop/config',{lift_enabled:$('fixedLift').checked,lift_height_m:$('fixedLiftHeight').value});teleopConfigLoaded=false;toast('已保存，下次初始化生效');});
$('teleopVoice').onclick=()=>{if(!('speechSynthesis' in window)){toast('当前浏览器不支持语音，页面仍显示标定状态');return;}teleopVoice=!teleopVoice;$('teleopVoice').textContent=teleopVoice?'关闭语音':'语音提示';if(teleopVoice)sayTeleop('语音提示已开启');else speechSynthesis.cancel();};
$('copyPath').onclick=async()=>{try{await navigator.clipboard.writeText($('directory').textContent);toast('目录已复制');}catch(_){toast('请选中目录文字复制');}};
$('episodes').onclick=e=>{const b=e.target.closest('button');if(!b||b.disabled)return;if(b.dataset.grade)return action(async()=>{await api('/api/episode/rate',{id:Number(b.dataset.id),grade:b.dataset.grade});const path=b.closest('.dataset-group').dataset.path;await loadDataset(path,true);toast(`已评为 ${b.dataset.grade}`);});if(b.dataset.delete)return action(async()=>{if(!await confirm('放弃本条数据？','将删除本条 HDF5、视频及相关文件。','放弃并删除'))return;await api('/api/episode/delete',{id:Number(b.dataset.delete),confirm:'delete'});toast('本条数据已删除');});if(b.dataset.review)reviewPlayer.open(b.dataset.review,b.closest('tr').querySelector('.episode-name').textContent);};
(async()=>{renderJoints([]);try{const boot=await api('/api/bootstrap');token=boot.csrf;depthSupported=boot.depth_recording_supported===true;appliedScene=boot.selected_scene;$('taskId').value=boot.default_task_id;$('sceneId').value=appliedScene?.scene_id||1;$('prompt').value=boot.default_prompt;await parse();await poll();}catch(e){toast(e.message,true);}async function tick(){await poll();setTimeout(tick,800);}setTimeout(tick,800);})();
