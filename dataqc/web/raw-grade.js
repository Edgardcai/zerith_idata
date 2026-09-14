/* One live grade bar for real and simulation HDF5 replay. */
window.NodaGrade={mount(container,onUpdate=()=>{}){
 container.classList.add('noda-grade');
 container.innerHTML='<span>采集等级 <strong class="noda-capture">—</strong></span><span>质检后等级 <strong class="noda-qc">—</strong></span><span>采用等级 <strong class="noda-current">读取中…</strong></span><div class="noda-manual-controls"><label>人工复核 <select class="noda-manual"><option value="">请选择</option><option>A</option><option>B</option><option>C</option><option>F</option></select></label><button type="button" class="noda-save" disabled>保存</button><small class="noda-change" role="status"></small></div>';
 const current=container.querySelector('.noda-current'),select=container.querySelector('select'),button=container.querySelector('button'),message=container.querySelector('small');
 let root='',snapshot=null,generation=0,dirty=false,busy=false,request=0;
 const reportBar=document.createElement('div');reportBar.className='raw-qc-report';reportBar.innerHTML='<span></span><button type="button">全部检查</button>';container.after(reportBar);
 reportBar.querySelector('button').onclick=()=>{const target=root;window.QCReport?.open(snapshot?.qc_presentation,{episode:snapshot?.episode_name,dark:true,onFrame:frame=>window.dispatchEvent(new CustomEvent('qc-seek-frame',{detail:{root:target,frame}}))})};
 const show=s=>{snapshot=s;const p=s.qc_presentation||{};reportBar.querySelector('span').textContent=p.summary||'尚无质检报告';reportBar.querySelector('button').textContent=p.problem_count?`全部 ${p.problem_count} 项问题`:'全部检查';container.querySelector('.noda-capture').textContent=s.collection_grade||'未记录';container.querySelector('.noda-qc').textContent=(s.qc_grade_label||s.qc_grade||'待质检')+(s.qc_grade&&s.review_required?' · 待复核':'');current.textContent=s.current_grade||'待复核';current.dataset.grade=s.current_grade;current.title=s.reason||'';button.disabled=busy||s.busy;button.title=s.busy?'数据集正在处理':'';if(!dirty)select.value=s.manual_grade||s.current_grade||'';onUpdate(s)};
 async function refresh(){
  if(!root||busy||container.hidden||document.hidden)return;
  const target=root,epoch=generation,id=++request;
  try{const res=await fetch('/api/replay-grade?root='+encodeURIComponent(target),{cache:'no-store'}),s=await res.json();if(epoch!==generation||target!==root||id!==request)return;if(!res.ok)throw Error(s.error||'读取等级失败');show(s)}catch(e){if(epoch===generation&&id===request){message.textContent=e.message;button.disabled=true}}
 }
 select.onchange=()=>{dirty=true;message.textContent=''};
 button.onclick=async()=>{
  if(!snapshot||!select.value||busy)return;
  const target=root,epoch=generation,grade=select.value,revision=snapshot.revision;request++;busy=true;button.disabled=true;message.textContent='保存中…';
  try{
   const res=await fetch('/api/replay-grade',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({root:target,grade,revision})}),s=await res.json();
   if(!res.ok)throw Error(s.error||'保存失败');
   if(epoch===generation){dirty=false;show(s);message.textContent=`已保存：${s.before_grade||'未质检'} → ${s.current_grade||'未质检'}${s.current_grade!==grade?'（人工 '+grade+'）':''}`}
   window.parent?.postMessage({type:'pipeline-manual-failure-updated'},location.origin);
  }catch(e){if(epoch===generation)message.textContent=e.message}
  finally{busy=false;if(epoch===generation)button.disabled=!!snapshot?.busy;refresh()}
 };
 const timer=setInterval(refresh,5000);
 window.addEventListener('focus',refresh);document.addEventListener('visibilitychange',refresh);
 window.addEventListener('message',e=>{if(e.origin===location.origin&&e.data?.type==='noda-refresh-grade')refresh()});
 return {setRoot(value){if((value||'')!==root)window.QCReport?.close();generation++;request++;root=value||'';snapshot=null;dirty=false;container.hidden=!root;reportBar.hidden=!root;reportBar.querySelector('span').textContent='读取质检报告…';current.textContent='读取中…';container.querySelector('.noda-capture').textContent='—';container.querySelector('.noda-qc').textContent='—';message.textContent='';select.value='';button.disabled=true;refresh()},refresh,destroy(){clearInterval(timer);root='';reportBar.remove()}};
}};
