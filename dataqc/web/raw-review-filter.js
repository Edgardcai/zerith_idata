/* One dataset request supplies the review queue for both HDF5 players. */
window.NodaReview={mount(container,select,onSelect,onRender=()=>{}){
 container.classList.add('noda-review-filter');
 container.innerHTML='<span>回放筛选</span><button type="button" data-review-filter="all" aria-pressed="true">全部</button><button type="button" data-review-filter="changed" aria-pressed="false">等级变化</button><button type="button" data-review-filter="pending" aria-pressed="false">待复核</button><small role="status"></small>';
 let records=[],grades=new Map(),kind='all',current='',generation=0,ready=false,loading=false,serial=0;
 const updates=new Map();
 const message=container.querySelector('small');
 const replay=document.body.classList.contains('noda-dark')?container.closest('.app'):null;
 function showEmpty(empty){
  if(!replay)return;
  replay.classList.toggle('review-queue-empty',empty);
  replay.querySelectorAll('.video-grid,.controls,.metrics,.qc-strip,.chart-wrap,.table-wrap').forEach(e=>e.inert=empty);
  let notice=replay.querySelector('.review-empty-message');
  if(empty){
   replay.querySelectorAll('video').forEach(video=>video.pause());
   if(!notice){notice=document.createElement('p');notice.className='review-empty-message';notice.setAttribute('role','status');replay.append(notice)}
   notice.textContent=kind==='pending'?'当前数据集没有待复核记录。可切换“全部”继续回放。':kind==='changed'?'当前数据集没有等级变化的记录。可切换“全部”继续回放。':'当前数据集没有可回放的记录。';
  }
  if(notice)notice.hidden=!empty;
 }
 const matches=r=>kind==='all'||(kind==='changed'?grades.get(r.root)?.grade_changed:grades.get(r.root)?.review_pending);
 function render(){
  const visible=records.filter(matches);
  select.replaceChildren(...visible.map(r=>{const option=document.createElement('option'),grade=grades.get(r.root);option.value=String(r.value);option.textContent=r.label+(grade?.review_pending?' · 待复核':'');return option}));
  select.value=current;
  select.disabled=!visible.length;
  showEmpty(!visible.length);
  container.querySelectorAll('button').forEach(b=>{const k=b.dataset.reviewFilter,count=records.filter(r=>k==='all'||(k==='changed'?grades.get(r.root)?.grade_changed:grades.get(r.root)?.review_pending)).length;b.textContent=({all:'全部',changed:'等级变化',pending:'待复核'})[k]+(ready||k==='all'?` ${count}`:'');b.disabled=k!=='all'&&!ready;b.setAttribute('aria-pressed',String(k===kind))});
  message.textContent=!ready?'正在读取最新等级…':!visible.length?'没有符合条件的记录':select.selectedIndex<0?'当前记录不在筛选列表中':`共 ${visible.length} 条`;
  onRender();
 }
 async function refresh(){
  if(!records.length||loading||document.hidden)return;
  const epoch=generation,started=serial;loading=true;
  try{
   const res=await fetch('/api/replay-grades?root='+encodeURIComponent(records[0].root),{cache:'no-store'}),payload=await res.json();
   if(epoch!==generation)return;
   if(!res.ok)throw Error(payload.error||'读取复核列表失败');
   grades=new Map(payload.episodes.map(r=>[r.root,r]));
   for(const [root,entry] of updates)if(entry.serial>started)grades.set(root,entry.snapshot);
   ready=true;render();
  }catch(e){if(epoch===generation)message.textContent=e.message}
  finally{loading=false;if(epoch!==generation)refresh()}
 }
 container.querySelectorAll('button').forEach(b=>b.onclick=()=>{
  kind=b.dataset.reviewFilter;
  const visible=records.filter(matches),next=visible.find(r=>String(r.value)===current)||visible[0];
  const changed=next&&String(next.value)!==current;
  if(next)current=String(next.value);
  render();
  if(changed)onSelect(current);
 });
 select.addEventListener('change',()=>{current=select.value});
 const timer=setInterval(()=>{if(container.getClientRects().length)refresh()},15000);
 window.addEventListener('focus',refresh);
 return {
  setRecords(list,value){generation++;records=list;grades.clear();updates.clear();ready=false;kind='all';current=String(value??'');render();refresh()},
  setCurrent(value){current=String(value);render()},
  update(snapshot){if(!records.some(r=>r.root===snapshot.root))return;updates.set(snapshot.root,{serial:++serial,snapshot});grades.set(snapshot.root,snapshot);render()},
  refresh,destroy(){generation++;records=[];clearInterval(timer);window.removeEventListener('focus',refresh)}
 };
}};
