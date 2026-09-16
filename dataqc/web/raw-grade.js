/* One live grade bar for real and simulation HDF5 replay. */
window.NodaGrade={mount(container,onUpdate=()=>{}){
 container.classList.add('noda-grade');
 container.innerHTML='<span class="noda-identity"></span><span>采集等级 <strong class="noda-capture">—</strong></span><span>质检后等级 <strong class="noda-qc">—</strong></span><span>采用等级 <strong class="noda-current">读取中…</strong></span><div class="noda-manual-controls"><label>人工复核 <select class="noda-manual"><option value="">请选择</option><option>A</option><option>B</option><option>C</option><option>F</option></select></label><button type="button" class="noda-save" disabled>保存</button><label class="noda-note-controls" hidden>问题类型 <select class="noda-problem"><option value="">可选</option><option>视觉异常</option><option>动作异常</option><option>时序异常</option><option>标注问题</option><option>其他</option></select><input class="noda-note" maxlength="2000" placeholder="原因备注（可选）"></label><small class="noda-change" role="status"></small></div>';
 const current=container.querySelector('.noda-current'),select=container.querySelector('select'),button=container.querySelector('button'),message=container.querySelector('small');
 const note=container.querySelector('.noda-note'),problem=container.querySelector('.noda-problem'),noteControls=container.querySelector('.noda-note-controls');
 let draftRevision=null;
 const noteVisible=()=>{noteControls.hidden=!['B','F'].includes(select.value)&&!note.value&&!problem.value};
 let root='',snapshot=null,generation=0,dirty=false,busy=false,request=0;
 const reportBar=document.createElement('div');reportBar.className='raw-qc-report';reportBar.innerHTML='<span></span><button type="button" disabled>全部检查</button>';container.after(reportBar);
 reportBar.querySelector('button').onclick=()=>{if(!snapshot)return;const target=root;window.QCReport?.open(snapshot?.qc_presentation,{episode:snapshot?.episode_name,dark:true,onFrame:frame=>window.dispatchEvent(new CustomEvent('qc-seek-frame',{detail:{root:target,frame}}))})};
 const show=s=>{snapshot=s;const identity=container.querySelector('.noda-identity');identity.textContent=`采集 ${s.collection_episode_name||'编号未记录'} → 目录 ${s.current_episode_name||s.episode_name}`;identity.title=s.root||'';reportBar.querySelector('button').disabled=false;const p=s.qc_presentation||{};reportBar.querySelector('span').textContent=p.summary||'尚无质检报告';reportBar.querySelector('button').textContent=p.problem_count?`全部 ${p.problem_count} 项问题`:'全部检查';container.querySelector('.noda-capture').textContent=s.collection_grade||'未记录';container.querySelector('.noda-qc').textContent=(s.qc_grade_label||s.qc_grade||'待质检')+(s.qc_grade&&s.review_required?' · 待复核':'');current.textContent=s.current_grade||'待复核';current.dataset.grade=s.current_grade;current.title=s.reason||'';button.disabled=busy||s.busy;button.title=s.busy?'数据集正在处理':'';if(!dirty){select.value=s.manual_grade||s.current_grade||'';note.value=s.manual_note||'';problem.value=s.manual_problem||''}noteVisible();onUpdate(s)};
 async function refresh(){
  if(!root||busy||container.hidden||document.hidden)return;
  const target=root,epoch=generation,id=++request;
  try{const res=await fetch('/api/replay-grade?root='+encodeURIComponent(target),{cache:'no-store'}),s=await res.json();if(epoch!==generation||target!==root||id!==request)return;if(!res.ok)throw Error(s.error||'读取等级失败');show(s)}catch(e){if(epoch===generation&&id===request){message.textContent=e.message;button.disabled=true}}
 }
 const changed=()=>{if(!dirty)draftRevision=snapshot?.revision;dirty=true;message.textContent='未保存';noteVisible()};select.onchange=changed;note.oninput=changed;problem.onchange=changed;
 button.onclick=async()=>{
  if(!snapshot||!select.value||busy)return;
  const target=root,epoch=generation,grade=select.value,revision=draftRevision||snapshot.revision;request++;busy=true;button.disabled=true;[select,note,problem].forEach(c=>c.disabled=true);message.textContent='保存中…';
  try{
   const res=await fetch('/api/replay-grade',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({root:target,grade,revision,note:note.value,problem:problem.value})}),s=await res.json();
   if(!res.ok)throw Error(s.error||'保存失败');
   if(epoch===generation){dirty=false;draftRevision=null;show(s);message.textContent=`已保存：${s.before_grade||'未质检'} → ${s.current_grade||'未质检'}${s.current_grade!==grade?'（人工 '+grade+'）':''}`}
   window.parent?.postMessage({type:'pipeline-manual-failure-updated'},location.origin);
  }catch(e){if(epoch===generation)message.textContent=e.message}
  finally{busy=false;[select,note,problem].forEach(c=>c.disabled=false);if(epoch===generation)button.disabled=!!snapshot?.busy;else refresh()}
 };
 const timer=setInterval(refresh,5000);
 window.addEventListener('focus',refresh);document.addEventListener('visibilitychange',refresh);
 window.addEventListener('message',e=>{if(e.origin===location.origin&&e.data?.type==='noda-refresh-grade')refresh()});
 return {setRoot(value){if((value||'')!==root)window.QCReport?.close();generation++;request++;root=value||'';snapshot=null;container.querySelector('.noda-identity').textContent='';dirty=false;draftRevision=null;note.value='';problem.value='';noteVisible();container.hidden=!root;reportBar.hidden=!root;reportBar.querySelector('button').disabled=true;reportBar.querySelector('span').textContent='读取质检报告…';current.textContent='读取中…';container.querySelector('.noda-capture').textContent='—';container.querySelector('.noda-qc').textContent='—';message.textContent='';select.value='';button.disabled=true;refresh()},refresh,destroy(){clearInterval(timer);root='';reportBar.remove()}};
}};
