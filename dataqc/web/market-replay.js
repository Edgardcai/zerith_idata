/* Route the original replay components to the full-process review model. */
(()=>{
'use strict';
const params=new URLSearchParams(location.search),root=params.get('root'),originalFetch=window.fetch.bind(window);
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let episodes=[],current=null,generation=0,seekFrame,pauseReplay,phaseBusy=false;
async function api(path,body){const r=await originalFetch('/market/api/'+path,body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{});const d=await r.json();if(!r.ok)throw Error(d.detail||d.error||'请求失败');return d}
window.fetch=async(input,options)=>{
 const url=new URL(typeof input==='string'?input:input.url,location.href),method=options?.method||'GET';
 if(url.origin!==location.origin)return originalFetch(input,options);
 const name=url.pathname.split('/').pop();
 if(name==='episodes'){
   const d=await api('replay-episodes?'+new URLSearchParams({root}));episodes=d.episodes;return Response.json(d);
 }
 if(name==='episode'){
   const index=Number(url.searchParams.get('index')),row=episodes[index];if(!row)return Response.json({error:'记录不存在'},{status:404});
   return originalFetch('/market/api/replay-data?'+new URLSearchParams({root,id:row.id,index}));
 }
 if(name==='replay-grades')return originalFetch('/market/api/replay-grades?'+new URLSearchParams({root}));
 if(name==='replay-grade'){
   if(method==='POST'){
     const body=JSON.parse(options.body),{root:path,...review}=body;
     const response=await originalFetch('/market/api/replay-grade',{...options,body:JSON.stringify({...review,root,path})});
     if(response.ok&&current?.path===path){current=await api('episode?'+new URLSearchParams({root,id:current.id}));renderPhases()}
     // Existing live-grade component reports errors from `error`.
     if(!response.ok){const d=await response.json();return Response.json({error:d.detail||d.error},{status:response.status})}
     return response;
   }
   return originalFetch('/market/api/replay-grade?'+new URLSearchParams({root,path:url.searchParams.get('root')}));
 }
 if(method!=='GET')return Response.json({error:'此原页面操作不适用于各阶段数据'},{status:400});
 return originalFetch(input,options);
};
function renderPhases(){
 if(!current)return;
 let bar=document.getElementById('marketPhaseButtons');if(!bar){bar=document.createElement('div');bar.id='marketPhaseButtons';bar.className='market-phases';document.getElementById('raw-grade-bar').after(bar)}
 bar.innerHTML=current.phases.map(p=>`<button type="button" data-seek-phase="${p.index}" ${p.valid?'':'disabled'}>${esc(p.label)} · ${esc(p.effective_grade||'未评')}</button>`).join('')+'<button type="button" id="marketReviewPhases">各阶段人工质检</button><small>YOLO 抓取参考：'+esc(current.yolo_grade||'未评')+'</small>';
 bar.onclick=e=>{const p=current.phases.find(p=>String(p.index)===e.target.dataset.seekPhase);if(p?.valid){pauseReplay();seekFrame(p.start)}if(e.target.id==='marketReviewPhases')showPhaseDialog()};
}
async function media(id,epoch){
 try{const d=await api('media?'+new URLSearchParams({root,id}));if(epoch!==generation)return;
 const cameras=['cam_high','cam_left_wrist','cam_right_wrist'];
 const videos=[...document.querySelectorAll('#videoGrid video')];
 for(const [i,c] of cameras.entries()){const v=d.videos.find(v=>v.camera===c),node=videos[i];if(node&&v?.url&&node.getAttribute('src')!==v.url)node.src=v.url;if(node){const status=node.closest('.card')?.querySelector('.card-head span:last-child');if(status)status.textContent=v?.status==='ready'?'就绪':v?.error||'视频准备中'}}
 if(d.videos.some(v=>v.status!=='ready'&&!v.error))setTimeout(()=>media(id,epoch),2000);
 }catch(e){if(epoch===generation)document.getElementById('frameCutState').textContent='视频：'+e.message}
}
let dialog;
function showPhaseDialog(){
 if(!dialog){dialog=document.createElement('dialog');dialog.style.cssText='max-width:850px;width:90vw;background:#191d21;color:#eef2f5;border:1px solid #555;border-radius:12px';document.body.append(dialog)}
 dialog.innerHTML='<div style="display:flex;justify-content:space-between"><b>各阶段人工质检</b><button id="closePhaseDialog">关闭</button></div><p>各阶段独立保存，整体人工等级保持最高优先级。</p>'+current.phases.map(p=>`<section style="padding:12px;border-top:1px solid #555"><b>${esc(p.label)}</b> · 自动 ${esc(p.auto_grade||'未评')} · 人工 ${esc(p.manual_grade||'未评')}<p>${esc(p.prompt)}</p><p>${p.valid?`帧 ${p.start}–${p.end-1}`:'阶段范围无效或缺失'}</p><select data-phase-grade="${p.index}" ${p.valid?'':'disabled'}><option value="">请选择</option>${['A','B','F'].map(g=>`<option ${g===p.manual_grade?'selected':''}>${g}</option>`).join('')}</select><input data-phase-note="${p.index}" maxlength="1000" placeholder="阶段备注" value="${esc(p.note||'')}" style="width:45%;margin:0 6px"><button data-phase-save="${p.index}" ${p.valid?'':'disabled'}>保存阶段评级</button><button data-phase-jump="${p.index}" ${p.valid?'':'disabled'}>跳转回放</button></section>`).join('')+'<p id="phaseFeedback" role="status"></p>';
 dialog.querySelector('#closePhaseDialog').onclick=()=>dialog.close();
 dialog.onclick=async e=>{const index=e.target.dataset.phaseSave,jump=e.target.dataset.phaseJump;if(jump!==undefined){const p=current.phases.find(p=>String(p.index)===jump);dialog.close();pauseReplay();seekFrame(p.start)}if(index===undefined||phaseBusy)return;
  const before=current,epoch=generation,grade=dialog.querySelector(`[data-phase-grade="${index}"]`).value;if(!grade){dialog.querySelector('#phaseFeedback').textContent='请选择人工等级';return}phaseBusy=true;e.target.disabled=true;
  try{const result=await api('review',{root,id:before.id,uuid:before.uuid,revision:before.revision,phase_index:Number(index),grade,note:dialog.querySelector(`[data-phase-note="${index}"]`).value});if(epoch!==generation)return;current=result;renderPhases();showPhaseDialog();dialog.querySelector('#phaseFeedback').textContent='已保存';window.postMessage({type:'noda-refresh-grade'},location.origin);parent.postMessage({type:'pipeline-manual-failure-updated'},location.origin)}catch(err){dialog.querySelector('#phaseFeedback').textContent=err.message}finally{phaseBusy=false;e.target.disabled=false}
 };
 if(!dialog.open)dialog.showModal();
}
window.marketReplayLoaded=(data,seek,pause)=>{
 current=data.market;generation++;seekFrame=seek;pauseReplay=pause;dialog?.close();renderPhases();
 const disabled=['frameCutEnabled','executeFrameCutBtn','cutStartFrame','cutEndFrame','stage2StartFrame','useCurrentStageFrameBtn','applyStageBoundaryBtn'];
 for(const id of disabled){const n=document.getElementById(id);if(n){n.disabled=true;n.title='原两阶段裁剪 / 边界修改不适用于各阶段全流程'}}
 document.getElementById('frameCutState').textContent='各阶段暂不支持原始数据裁剪';
 document.getElementById('stageBoundaryState').textContent='各阶段边界只读，可点击阶段按钮定位';
 for(const o of document.querySelectorAll('.noda-manual option'))if(o.value==='C'){o.disabled=true;o.textContent='C（不适用）'}
 media(current.id,generation);
};
})();
