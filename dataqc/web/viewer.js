const rawNavSelect=document.getElementById('raw-nav-select');
let rawNavDataset='';
function updateRawNavButtons(){document.getElementById('raw-prev').disabled=rawNavSelect.selectedIndex<=0;document.getElementById('raw-next').disabled=!rawNavSelect.options.length||rawNavSelect.selectedIndex>=rawNavSelect.options.length-1}
async function loadRawRecord(root){const revision=++rawBrowseRevision;rawGradeBar.setRoot('');if(!root)return;try{const record=await api('/hdf5/replay?root='+encodeURIComponent(root));if(revision===rawBrowseRevision)populateEpisode(record)}catch(err){toast(err.message)}}
const rawReviewFilter=window.NodaReview.mount(document.getElementById('raw-review-filter'),rawNavSelect,loadRawRecord,updateRawNavButtons);
const rawGradeBar=window.NodaGrade.mount(document.getElementById('raw-grade-bar'),s=>rawReviewFilter.update(s));
async function prepareRawNav(root){
 const dataset=root.split('/').slice(0,-1).join('/');
 if(dataset===rawNavDataset){rawReviewFilter.setCurrent(root);return}
 rawNavDataset=dataset;rawReviewFilter.setRecords([{root,value:root,label:root.split('/').pop()}],root);
 try{const records=await api('/hdf5/episodes?root='+encodeURIComponent(dataset));if(dataset===rawNavDataset)rawReviewFilter.setRecords(records.map(r=>({root:r.root,value:r.root,label:r.name})),currentEp?.root||root)}catch(err){toast(err.message)}
}
rawNavSelect.onchange=()=>loadRawRecord(rawNavSelect.value);
for(const [id,delta] of [['raw-prev',-1],['raw-next',1]])document.getElementById(id).onclick=()=>{const index=rawNavSelect.selectedIndex+delta;if(index>=0&&index<rawNavSelect.options.length)loadRawRecord(rawNavSelect.options[index].value)};
const partColumns={left:[0,1,2,3,4,5,6],right:[8,9,10,11,12,13,14],grippers:[7,15],lift:[16],waist:[17,18],head:[19,20],base:[21,22]};
function jointLabel(name){return String(name).replace('left_joint_','左臂关节 ').replace('right_joint_','右臂关节 ').replace('left_gripper','左夹爪').replace('right_gripper','右夹爪').replace('lift_m','升降柱 / m').replace('waist_pitch','腰 Pitch / rad').replace('waist_yaw','腰 Yaw / rad').replace('head_pitch','头 Pitch / rad').replace('head_yaw','头 Yaw / rad').replace('base_vx','底盘线速度').replace('base_wz','底盘角速度')}
function activeStages(){if(currentEp?.kind==='lerobot')return currentEp.stages||[];return $$('.stage-row').map(row=>({hand:row.dataset.hand,start:Number(row.querySelector('[data-field=start]').value),end:Number(row.querySelector('[data-field=end]').value),item:row.querySelector('[data-field=item]').value}))}
function populateEpisode(e){
 currentEp=e;tab='raw';const t=e.trajectory||{},d=e.data||{},decision=d.manual_decision||d.decision||d.visual?.decision||{};
 const raw=!e.id&&e.kind!=='lerobot';rawGradeBar.setRoot(raw?e.root:'');document.getElementById('episode-dialog').classList.toggle('raw-replay',raw);
 document.getElementById('raw-replay-nav').hidden=!raw;if(raw)prepareRawNav(e.root);
 const lr=e.kind==='lerobot';$('#episode-title').textContent=(lr?`episode_${String(e.index).padStart(6,'0')}`:e.root.split('/').pop())+' · '+(lr?'LeRobot 回放':labels[e.status]||'HDF5 回放');
 $('#episode-source').textContent=e.root+(t.source_format==='zerith_sim_v1'?' · 仿真：合成时间轴；State 夹爪复制 Action；曲线为标准向量，腰头质检使用 raw 实测值':'');$('#suggestion').textContent=decision.reason||e.reason||'';$('#episode-error').textContent='';$('#video-error').textContent='';
 $('#review-form').hidden=lr;$('#lr-review').hidden=!lr;$('#report-tabs').hidden=lr;$('#report-content').hidden=lr;$('#edit-title').textContent=lr?'人工复筛':'分级与阶段标注';
 $('#prompt').value=decision.corrected_prompt||t.task||'';$('#reason').value=decision.reason||e.reason||'';$('#actor').value=localStorage.getItem('qc.actor')||'';$('#grade').value=['A','B','F'].includes(e.grade)?e.grade:['A','B','F'].includes(decision.grade)?decision.grade:'B';
 let stages=decision.stages||e.stages;
 if(!stages?.length){const txt=(t.task||'').replace(/\s+/g,' '),m=txt.match(/^Grasp (.+) with the left hand and then grasp (.+) with the right hand$/),n=t.total_frames||0,b=t.transitions?.find(v=>v>0&&v<n);if(m&&b!=null)stages=[{hand:'left',start:0,end:b,item:m[1]},{hand:'right',start:b,end:n,item:m[2]}];else if(m)stages=[];else{const single=txt.match(/^Grasp (.+) with the (left|right) hand$/);stages=single?[{hand:single[2],start:0,end:n,item:single[1]}]:[]}}
 if(lr&&!e.stages?.length)currentEp.stages=stages||[];
 $('#stage-edit').innerHTML=(stages||[]).map(st=>`<div class="stage-row" data-hand="${esc(st.hand)}"><b>${st.hand==='left'?'左手':'右手'}</b><div class="range"><label>开始帧<input type="number" min="0" data-field="start" value="${st.start}"></label><label>结束帧<input type="number" min="1" data-field="end" value="${st.end}"></label></div><label>物品<input data-field="item" value="${esc(st.item)}"></label></div>`).join('');
 const intervals=d.raw_report?.checks?.find(c=>c.key==='stationary')?.detail.intervals||[];
 $('#trim-edit').innerHTML=intervals.map((st,i)=>`<label class="trim-row"><input type="checkbox" value="${i}" ${(decision.safe_trim_ids||[]).includes(i)?'checked':''}>${st.start}–${st.end-1} 帧 <button type="button" data-seek="${st.start}">定位</button></label>`).join('')||'<p class="note">无待剔除静止区间</p>';
 $$('#trim-edit [data-seek]').forEach(b=>b.onclick=()=>seek(Number(b.dataset.seek)));
 $$('#review-form input,#review-form select,#review-form textarea,#review-form button').forEach(el=>el.disabled=!e.id&&!lr);
 $('#source-fields').textContent=JSON.stringify(t.attrs||e.mapping||{},null,2);$('#current-task').textContent=t.task||'无提示词';
 const cameras=lr?e.cameras:['cam_high','cam_left_wrist','cam_right_wrist'];
 for(let i=0;i<3;i++){const v=$('#v'+i);v.pause();v.onloadedmetadata=null;v.removeAttribute('src');v.load();v.closest('figure').hidden=!cameras[i];if(cameras[i]){v.src=lr?`/auto/api/library/${e.id}/episodes/${e.index}/video/${encodeURIComponent(cameras[i])}`:e.id?`/auto/api/episodes/${e.id}/video/${cameras[i]}`:`/auto/api/hdf5/video/${cameras[i]}?root=${encodeURIComponent(e.root)}`;v.closest('figure').querySelector('figcaption').textContent=cameras[i].includes('left')?'左腕':cameras[i].includes('right')?'右腕':'头部 / 主视角';}}
 if(lr){$('#lr-grade').value=e.review?.grade||e.episode.quality_grade||'B';$('#lr-excluded').checked=!!e.review?.excluded;$('#lr-reason').value=e.review?.reason||'';$('#lr-actor').value=localStorage.getItem('qc.actor')||'';}
 $('#seek').max=Math.max(0,(t.total_frames||1)-1);$('#seek').value=0;$('#curve-start').value=0;$('#curve-end').value=t.total_frames||1;
 $('#curve-end').max=t.total_frames||1;$('#curve-start').max=Math.max(0,(t.total_frames||1)-1);
 if(t.state?.[0]?.length!==23)$('#joint-group').value='all';
 renderTrajectory(t);if(!lr)showReport();if(!$('#episode-dialog').open)$('#episode-dialog').showModal();seek(0);
 if(e.trajectory_error)$('#episode-error').textContent=e.trajectory_error;
}
function renderTrajectory(t){
 if(!t?.state?.length){$('#chart').innerHTML='<p class="note">轨迹不可用</p>';return}
 const n=t.total_frames,start=Math.max(0,Math.min(n-1,Number($('#curve-start').value)||0)),end=Math.max(start+1,Math.min(n,Number($('#curve-end').value)||n));$('#curve-start').value=start;$('#curve-end').value=end;
 const columns=($('#joint-group').value==='all'?t.state[0].map((_,i)=>i):partColumns[$('#joint-group').value]||[0]).filter(i=>i<t.state[0].length);
 const selected=$('#curve-source').value,channels=selected==='both'?['state','action']:[selected];
 $('#chart').innerHTML=columns.map(col=>{let values=[];channels.forEach(k=>{for(let f=start;f<end;f++){const v=t[k]?.[f]?.[col];if(v!=null&&Number.isFinite(v))values.push(v)}});let min=values.length?Math.min(...values):0,max=values.length?Math.max(...values):1;if(max-min<.001){min-=.01;max+=.01}const padding=(max-min)*.08;min-=padding;max+=padding;
 const x=f=>55+(f-start)/Math.max(1,end-start-1)*725,y=v=>75-(v-min)/(max-min)*60;
 const step=Math.max(1,Math.ceil((end-start)/900)),fs=[];for(let f=start;f<end;f+=step)fs.push(f);if(fs.at(-1)!==end-1)fs.push(end-1);
 return `<div class="joint-chart"><b>${esc(jointLabel(t.names?.[col]||'维度 '+col))}</b><svg viewBox="0 0 800 100" data-curve-start="${start}" data-curve-end="${end}"><text x="0" y="18">${max.toFixed(3)}</text><text x="0" y="78">${min.toFixed(3)}</text><path d="M55 15 V78 H780" fill="none" stroke="#d7dee9"/>${activeStages().slice(1).filter(st=>st.start>=start&&st.start<end).map(st=>`<line x1="${x(st.start)}" x2="${x(st.start)}" y1="10" y2="80" stroke="#8d9bad" stroke-dasharray="4"/>`).join('')}${channels.map(k=>`<polyline fill="none" stroke="${k==='state'?'#2878b8':'#cf8b25'}" stroke-width="1.5" points="${fs.filter(f=>Number.isFinite(t[k]?.[f]?.[col])).map(f=>x(f).toFixed(2)+','+y(t[k][f][col]).toFixed(2)).join(' ')}"/>`).join('')}<line class="frame-cursor" y1="8" y2="80" stroke="#d84755"/><text x="55" y="96">${start} 帧</text><text x="730" y="96">${end-1} 帧</text></svg></div>`}).join('');
 $$('#chart svg').forEach(svg=>svg.onclick=e=>{const r=svg.getBoundingClientRect(),fraction=Math.max(0,Math.min(1,((e.clientX-r.left)/r.width*800-55)/725));seek(Math.round(start+fraction*(end-start-1)))});updateFrame(Number($('#seek').value));
}
function updateFrame(n){
 if(!currentEp)return;const t=currentEp.trajectory||{},fps=t.fps||30;n=Math.max(0,Math.min((t.total_frames||1)-1,Math.round(n)));
 const stamp=t.timestamps?.[n];$('#frame-label').textContent=`第 ${n} 帧 · 播放 ${(n/fps).toFixed(3)} s${stamp!=null?' · 采集 '+Number(stamp).toFixed(3)+' s':''}`;
 const st=activeStages().find(s=>s.start<=n&&n<s.end);$('#current-stage').textContent=st?`${st.hand==='left'?'阶段 1 · 左手':'右手阶段'} · ${st.item||''} · ${st.start}–${st.end-1} 帧`:'当前帧没有阶段标注';
 $$('#chart svg').forEach(svg=>{const start=Number(svg.dataset.curveStart),end=Number(svg.dataset.curveEnd),line=svg.querySelector('.frame-cursor'),x=55+(n-start)/Math.max(1,end-start-1)*725;line.setAttribute('x1',x);line.setAttribute('x2',x);line.style.display=n>=start&&n<end?'':'none'});
 const fmt=v=>Number.isFinite(v)?Number(v).toFixed(6):'—';$('#values-table').innerHTML=`<table><thead><tr><th>维度 / 字段</th><th>State</th><th>Action</th></tr></thead><tbody>${(t.names||[]).map((name,i)=>`<tr><td>${i} · ${esc(jointLabel(name))}</td><td>${fmt(t.state?.[n]?.[i])}</td><td>${fmt(t.action?.[n]?.[i])}</td></tr>`).join('')}</tbody></table>`;
}
function seek(n){if(!currentEp)return;n=Math.max(0,Math.min(Number($('#seek').max),Math.round(n)));for(let i=0;i<3;i++){const v=$('#v'+i);v.pause();if(v.getAttribute('src'))v.currentTime=n/(currentEp.trajectory?.fps||30)}$('#seek').value=n;updateFrame(n)}
let rawBrowseRevision=0;
async function browseRaw(root){try{const records=await api('/hdf5/episodes?root='+encodeURIComponent(root));let panel=$('#raw-browser');if(!panel){panel=document.createElement('section');panel.id='raw-browser';panel.className='panel';$('#content').prepend(panel)}panel.innerHTML=`<div class="toolbar"><h3>整组回放 · ${records.length} 条</h3><button id="close-raw-browser">收起</button></div><label>选择记录<select id="raw-episode-select"><option value="">请选择记录</option>${records.map(r=>`<option value="${esc(r.root)}">${esc(r.name)}</option>`).join('')}</select></label>`;$('#close-raw-browser').onclick=()=>panel.remove();$('#raw-episode-select').onchange=async e=>{const revision=++rawBrowseRevision;rawGradeBar.setRoot('');if(e.target.value)try{const record=await api('/hdf5/replay?root='+encodeURIComponent(e.target.value));if(revision===rawBrowseRevision)populateEpisode(record)}catch(err){toast(err.message)}}}catch(e){toast(e.message)}}
window.addEventListener('DOMContentLoaded',()=>{
 for(const id of ['joint-group','curve-source','curve-start','curve-end'])$('#'+id).onchange=()=>renderTrajectory(currentEp?.trajectory);
 $('#curve-reset').onclick=()=>{$('#curve-start').value=0;$('#curve-end').value=currentEp.trajectory.total_frames;renderTrajectory(currentEp.trajectory)};
 $('#prompt').oninput=()=>$('#current-task').textContent=$('#prompt').value;
 $('#stage-edit').oninput=()=>{renderTrajectory(currentEp?.trajectory);updateFrame(Number($('#seek').value))};
 $('#v0').ontimeupdate=()=>{if(!currentEp)return;const time=$('#v0').currentTime,n=Math.min(Number($('#seek').max),Math.round(time*(currentEp.trajectory?.fps||30)));$('#seek').value=n;updateFrame(n);for(let i=1;i<3;i++)if(Math.abs($('#v'+i).currentTime-time)>.12)$('#v'+i).currentTime=time};
 for(let i=0;i<3;i++){const v=$('#v'+i);v.onerror=()=>{if(v.getAttribute('src'))$('#video-error').textContent='部分视频无法播放，请检查相机文件；仍可查看其余视角和数值。'};const b=document.createElement('button');b.type='button';b.textContent='放大';b.className='expand-video';b.onclick=()=>v.requestFullscreen?.().catch(()=>{});v.closest('figure').append(b)}
 $('#lr-review').onsubmit=async e=>{e.preventDefault();const ep=currentEp;try{await api(`/library/${ep.id}/review`,{method:'POST',body:JSON.stringify({annotations:[{episode_index:ep.index,grade:$('#lr-grade').value,excluded:$('#lr-excluded').checked,reason:$('#lr-reason').value,actor:$('#lr-actor').value,revision:ep.review?.revision||0}]})});localStorage.setItem('qc.actor',$('#lr-actor').value);toast('人工复筛已保存');closeDialog();renderLibrary()}catch(err){$('#episode-error').textContent=err.message}};
});
