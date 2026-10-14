'use strict';
const $=id=>document.getElementById(id);
const grades=['A','B','C','F','UNRATED'];
const chartGrades=['A','B','F','C','UNRATED'];
const gradeColors={A:'#299d83',B:'#7594d3',F:'#df827b',C:'#ad96cb',UNRATED:'#bbc9d0'};
const n=value=>Number(value||0).toLocaleString('zh-CN');
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const gradeLabel=g=>!g||g==='UNRATED'?'未分级':g;
const sourceLabel=s=>({manual:'人工',qc:'自动/VLM',collection:'采集'}[s]||'—');
const badge=g=>`<span class="badge ${esc(g)}">${gradeLabel(g)}</span>`;
const today=()=>new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Shanghai'}).format(new Date());
const shift=(date,days)=>{const d=new Date(date+'T12:00:00Z');d.setUTCDate(d.getUTCDate()+days);return d.toISOString().slice(0,10);};
let mode='dates',dateRange='today',dateData=null,planData=null,state=null,selectedBatch='batch3',planBasis='all';
let requestId=0,recordRequestId=0,timer=null,recordScope=null,recordOffset=0,recordRows=[];
const recordLimit=25;
updatePresetDates();
if(new URLSearchParams(location.search).has('embedded'))$('back').hidden=true;
async function api(path,options){const res=await fetch(path,options),data=await res.json();if(!res.ok)throw new Error(data.detail||res.statusText);return data;}
function error(message){$('error').hidden=!message;$('error').textContent=message;}
function updatePresetDates(){if(dateRange==='all'||dateRange==='custom')return;const day=today(),end=dateRange==='yesterday'?shift(day,-1):day;$('dateInput').value=dateRange==='last7'?shift(day,-6):end;$('dateEnd').value=end;}
function setDateRange(value){dateRange=value;updatePresetDates();$('dateRange').hidden=value==='all';document.querySelectorAll('[data-range]').forEach(b=>{const active=b.dataset.range===value;b.classList.toggle('active',active);b.setAttribute('aria-pressed',String(active));});reload();}
function dateQuery(){const start=dateRange==='all'?'':$('dateInput').value,end=dateRange==='all'?'':$('dateEnd').value;if(dateRange!=='all'&&(!start||!end))throw new Error('请选择起止日期');if(start>end)throw new Error('开始日期不能晚于结束日期');return {start,end,grade:$('dateGrade').value};}
function dateRangeLabel(filters){return !filters.start&&!filters.end?'全部日期':filters.start===filters.end?filters.start:filters.start+' 至 '+filters.end;}
function clearDate(){dateData=null;$('dateMetrics').innerHTML='';$('dateTable').querySelector('tbody').innerHTML='';$('dateProductCount').textContent='';$('dailyChart').innerHTML='';$('gradeChart').innerHTML='';$('chartRange').textContent='';$('dailyTooltip').hidden=true;}
function showState(value){
 state=value;
 const format=timestamp=>new Date(timestamp*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false});
 $('updated').textContent=value?.updated_at?'数据时间 '+format(value.updated_at):'';
 const remote=value?.refresh_mode==='hourly';
 $('refresh').disabled=!!value?.scanning;$('refresh').classList.toggle('updating',!!value?.scanning);
 $('refresh').querySelector('span').textContent=value?.scanning?'正在更新':remote?'刷新显示':'更新统计';
 $('syncStatus').hidden=!remote;
 $('syncStatus').textContent=remote?'每小时同步一次 · '+(value.synced_at?'上次成功 '+format(value.synced_at):'等待首次成功同步')+(value.next_update_at?' · 下次更新 '+format(value.next_update_at):''):'';
 if(value?.error)error(value.error);
}
function setMode(next){mode=next;$('viewTitle').textContent=next==='dates'?'按日期':'按计划';document.querySelectorAll('[data-panel]').forEach(p=>p.hidden=p.dataset.panel!==next);document.querySelectorAll('[data-mode]').forEach(b=>{const active=b.dataset.mode===next;b.classList.toggle('active',active);b.setAttribute('aria-current',active?'page':'false');});window.scrollTo({top:0});reload();}
async function reload(){
 const current=++requestId;clearTimeout(timer);error('');
 try{
  if(mode==='dates'){
   updatePresetDates();
   const filters=dateQuery();
   if(!dateData||dateData.filters.start!==filters.start||dateData.filters.end!==filters.end||dateData.filters.grade!==filters.grade){clearDate();$('dateMetrics').innerHTML='<div class="loading">正在读取…</div>';$('dateTable').querySelector('tbody').innerHTML='<tr><td colspan="5" class="empty">正在读取…</td></tr>';}
   const data=await api('/api/collection-dashboard/summary?'+new URLSearchParams(filters));if(current!==requestId)return;dateData=data;showState(data.state);renderDate();
  }else{
   if(!planData||planData.basis!==planBasis){planData=null;$('planCards').innerHTML='<div class="loading">正在读取…</div>';$('planContent').hidden=true;$('planPending').hidden=true;}
   const data=await api('/api/collection-dashboard/plans?'+new URLSearchParams({basis:planBasis}));if(current!==requestId)return;planData=data;showState(data.state);renderPlans();
  }
 }catch(e){if(current===requestId){if(mode==='dates')clearDate();error(e.message);}}
 finally{if(current===requestId)timer=setTimeout(reload,state?.scanning?2000:60000);}
}
function metric(title,value){return `<div class="kpi"><div class="label">${title}</div><strong>${n(value)}</strong></div>`;}
function renderDate(){const m=dateData.metrics;$('dateMetrics').innerHTML=metric('采集数量',m.total)+metric('涉及商品',m.products)+metric('左手数量',m.left)+metric('右手数量',m.right);renderCharts();renderDateTable();}
function chartDays(){
 const daily=dateData.daily.filter(d=>/^\d{4}-\d{2}-\d{2}$/.test(d.date));
 const begin=dateData.filters.start||daily[0]?.date,finish=dateData.filters.end||daily.at(-1)?.date;
 if(!begin||!finish)return [];
 const count=Math.round((new Date(finish+'T12:00:00Z')-new Date(begin+'T12:00:00Z'))/86400000)+1;
 if(count>730)return daily;
 const lookup=new Map(daily.map(d=>[d.date,d]));
 return Array.from({length:count},(_,i)=>{const date=shift(begin,i);return lookup.get(date)||{date,total:0,grades:{}};});
}
function positionDailyTooltip(clientX,clientY){
 const tip=$('dailyTooltip'),gap=16,padding=8;
 const viewportWidth=document.documentElement.clientWidth,viewportHeight=document.documentElement.clientHeight;
 const {width,height}=tip.getBoundingClientRect();
 let x=clientX+gap,y=clientY+gap;
 if(x+width>viewportWidth-padding)x=clientX-width-gap;
 if(y+height>viewportHeight-padding)y=clientY-height-gap;
 tip.style.left=Math.max(padding,Math.min(x,viewportWidth-width-padding))+'px';
 tip.style.top=Math.max(padding,Math.min(y,viewportHeight-height-padding))+'px';
}
function renderCharts(){
 const m=dateData.metrics,days=chartDays();
 $('chartRange').textContent=dateRangeLabel(dateData.filters)+' · '+n(m.total)+' 条';
 $('dailyLegend').innerHTML=chartGrades.map(g=>`<span><i style="background:${gradeColors[g]}"></i>${gradeLabel(g)}</span>`).join('');
 $('dailyTooltip').hidden=true;
 if(!days.length)$('dailyChart').innerHTML='<div class="empty">暂无采集日期</div>';
 else{
  const available=$('dailyChart').clientWidth;
  const width=available>=600?available:Math.max(560,days.length*30+64),height=288,left=50,right=16,top=32,bottom=40,plotHeight=height-top-bottom;
  const max=Math.max(1,...days.map(d=>d.total)),step=Math.pow(10,Math.floor(Math.log10(max))),ceiling=Math.ceil(max/step)*step;
  const slot=(width-left-right)/days.length,barWidth=Math.min(44,slot*.62);
  const grid=Array.from({length:5},(_,i)=>{const y=top+plotHeight*i/4;return `<line x1="${left}" x2="${width-right}" y1="${y}" y2="${y}"/><text x="${left-10}" y="${y+4}" text-anchor="end">${n(Math.round(ceiling*(1-i/4)))}</text>`;}).join('');
  const bars=days.map((d,i)=>{
   const x=left+slot*(i+.5),parts=[];let y=height-bottom;
   for(const g of chartGrades){const h=(d.grades[g]||0)/ceiling*plotHeight;if(!h)continue;y-=h;parts.push(`<rect x="${x-barWidth/2}" y="${y}" width="${barWidth}" height="${h}" fill="${gradeColors[g]}"/>`);}
   const description=d.date+'，共 '+n(d.total)+' 条，'+chartGrades.map(g=>gradeLabel(g)+' '+n(d.grades[g])).join('，');
   const label=i%Math.ceil(days.length/12)===0||i===days.length-1;
   return `<g class="day-bar" data-day="${d.date}" tabindex="0" role="img" aria-label="${esc(description)}"><rect class="bar-hit" x="${x-slot/2}" y="${top}" width="${slot}" height="${plotHeight}"/>${parts.join('')}<text class="bar-total" x="${x}" y="${height-bottom-d.total/ceiling*plotHeight-8}" text-anchor="middle">${d.total&&(slot>=34||d.total===max)?n(d.total):''}</text><text class="bar-date" x="${x}" y="${height-12}" text-anchor="middle">${label?d.date.slice(5):''}</text></g>`;
  }).join('');
  $('dailyChart').innerHTML=`<svg class="daily-plot" viewBox="0 0 ${width} ${height}" style="min-width:${width}px" aria-label="每日采集数量柱状图"><g class="chart-gridlines">${grid}</g>${bars}</svg>`;
  let tooltipDay=null;
  const hide=()=>$('dailyTooltip').hidden=true;
  const show=(element,event)=>{
   const tip=$('dailyTooltip');
   if(tooltipDay!==element.dataset.day||tip.hidden){
    const d=days.find(v=>v.date===element.dataset.day);
    tip.innerHTML=`<strong>${d.date} · ${n(d.total)} 条</strong>`+chartGrades.map(g=>`<span><i style="background:${gradeColors[g]}"></i>${gradeLabel(g)}<b>${n(d.grades[g])} · ${d.total?((d.grades[g]||0)/d.total*100).toFixed(1):'0.0'}%</b></span>`).join('');
    tooltipDay=element.dataset.day;
   }
   tip.hidden=false;
   const rect=element.getBoundingClientRect();
   positionDailyTooltip(event?.clientX??rect.left+rect.width/2,event?.clientY??rect.top+rect.height/2);
  };
  $('dailyChart').querySelectorAll('.day-bar').forEach(b=>{b.onmouseenter=e=>show(b,e);b.onmousemove=e=>show(b,e);b.onmouseleave=hide;b.onfocus=()=>show(b);b.onblur=hide;});
  $('dailyChart').onscroll=()=>{const focused=$('dailyChart').querySelector('.day-bar:focus');if(focused)show(focused);else hide();};
 }
 let angle=0;
 const segments=chartGrades.map(g=>{const start=angle;angle+=m.total?(m.grades[g]||0)/m.total*100:0;return `${gradeColors[g]} ${start}% ${angle}%`;});
 const ab=m.total?((m.grades.A+m.grades.B)/m.total*100).toFixed(1):'—';
 const visible=chartGrades.filter(g=>['A','B','F'].includes(g)||m.grades[g]);
 $('gradeChart').innerHTML=`<div class="grade-donut" style="background:${m.total?'conic-gradient('+segments.join(',')+')':'#edf2f4'}" role="img" aria-label="${esc(chartGrades.map(g=>gradeLabel(g)+' '+n(m.grades[g])+' 条').join('，'))}"><div><strong>${ab}${m.total?'<small>%</small>':''}</strong><span>A/B 占比</span></div></div><div class="grade-breakdown">${visible.map(g=>`<div data-grade="${g}"><span><i style="background:${gradeColors[g]}"></i>${gradeLabel(g)}</span><b>${n(m.grades[g])}</b><span>${m.total?((m.grades[g]||0)/m.total*100).toFixed(1)+'%':'—'}</span></div>`).join('')}</div>`;
}
function renderDateTable(){
 if(!dateData)return;
 const search=$('dateSearch').value.trim().toLocaleLowerCase(),sort=$('dateSort').value;
 const rows=dateData.products.filter(r=>r.name.toLocaleLowerCase().includes(search));
 rows.sort((a,b)=>sort==='name'?a.name.localeCompare(b.name):sort==='left'||sort==='right'?a[sort]-b[sort]||b.total-a.total:b.total-a.total||a.name.localeCompare(b.name));
 $('dateProductCount').textContent=rows.length;
 $('dateTable').querySelector('tbody').innerHTML=rows.map(r=>`<tr><td><button class="cell-link date-product" data-name="${esc(r.name)}">${esc(r.name)}</button></td><td class="num">${n(r.total)}</td><td class="num">${n(r.left)}</td><td class="num">${n(r.right)}</td><td><button class="cell-link date-records" data-name="${esc(r.name)}">查看记录 ↗</button></td></tr>`).join('')||'<tr><td colspan="5" class="empty">暂无采集记录</td></tr>';
 document.querySelectorAll('.date-product').forEach(b=>b.onclick=()=>productDetail(b.dataset.name));
 document.querySelectorAll('.date-records').forEach(b=>b.onclick=()=>openRecords(b.dataset.name,false));
}
function batchCard(p){
 const active=selectedBatch===p.id,heading=`<div class="batch-heading"><span class="batch-title">${esc(p.name)}</span><span class="batch-products">${p.status==='ready'?p.product_count+' 个商品':''}</span></div>`;
 if(p.status!=='ready')return `<button class="batch-card" data-batch="${p.id}">${heading}<div class="batch-pending">计划文档待更新</div></button>`;
 return `<button class="batch-card ${active?'active':''}" data-batch="${p.id}" aria-pressed="${active}">${heading}<div class="batch-progress-heading"><span class="batch-percent">${(p.completion*100).toFixed(1)}<span class="unit">%</span></span><span>配额进度</span></div><div class="progress-track"><i style="width:${p.completion*100}%"></i></div><div class="batch-counts"><span>计划<b>${n(p.episode_target)}</b></span><span>${planBasis==='ab'?'A/B':'实际'}<b>${n(p.actual_episodes)}</b></span></div><div class="batch-foot"><span>达标 <b>${p.completed_products}</b> / ${p.product_count}</span><span class="gap">单手缺口 ${n(p.remaining)}</span></div></button>`;
}
function activePlan(){return planData?.plans.find(p=>p.id===selectedBatch);}
function renderPlans(){
 $('planCards').innerHTML=planData.plans.map(batchCard).join('');document.querySelectorAll('[data-batch]').forEach(b=>b.onclick=()=>{selectedBatch=b.dataset.batch;renderPlans();});
 document.querySelectorAll('[data-basis]').forEach(b=>{const active=b.dataset.basis===planBasis;b.classList.toggle('active',active);b.setAttribute('aria-pressed',String(active));});
 const p=activePlan();$('planContent').hidden=p.status!=='ready';$('planPending').hidden=p.status==='ready';if(p.status!=='ready'){$('planPending').textContent=p.error||'计划文档待更新';return;}
 $('planTitle').textContent=p.name+' · 商品配额';$('planDocument').href=p.document_url;
 const item=(name,value,unit)=>`<div class="plan-metric"><span>${name}</span><strong>${n(value)}<span class="unit">${unit}</span></strong></div>`;
 $('planSummary').innerHTML=item('计划采集',p.episode_target,'次')+item(planBasis==='ab'?'A/B 采集':'实际采集',p.actual_episodes,'条')+item('商品达标',p.completed_products,'/ '+p.product_count)+item('单手缺口',p.remaining,'条');
 renderPlanTable();
}
function quota(actual,target,hand){return `<td class="quota-cell ${hand}"><div class="quota-numbers"><strong>${n(actual)}</strong><span>/ ${target?n(target):'无配额'}</span></div><div class="progress-track"><i style="width:${target?Math.min(100,100*actual/target):0}%"></i></div></td>`;}
function renderPlanTable(){
 const p=activePlan();if(!p||p.status!=='ready')return;
 const search=$('planSearch').value.trim().toLocaleLowerCase(),sort=$('planSort').value;
 const rows=p.products.filter(r=>r.name.toLocaleLowerCase().includes(search)&&(!$('planMissingOnly').checked||!r.complete));
 rows.sort((a,b)=>sort==='name'?a.name.localeCompare(b.name):sort==='completion'?b.completion-a.completion||a.name.localeCompare(b.name):b.gap-a.gap||a.name.localeCompare(b.name));
 $('planTable').querySelector('tbody').innerHTML=rows.map(r=>`<tr><td><button class="cell-link plan-product" data-name="${esc(r.name)}">${esc(r.name)}</button></td>${quota(r.left_actual,r.left_target,'left')}${quota(r.right_actual,r.right_target,'right')}<td class="completion-cell">${(r.completion*100).toFixed(1)}%<div class="progress-track"><i style="width:${r.completion*100}%"></i></div></td><td class="num"><span class="${r.gap?'gap-value':'muted'}">${r.gap?n(r.gap):'—'}</span></td><td><span class="${r.complete?'complete-mark':'incomplete-mark'}">${r.complete?'已达标':'待补齐'}</span></td><td><button class="cell-link plan-records" data-name="${esc(r.name)}" ${!r.actual_records?'disabled':''}>查看记录 ↗</button></td></tr>`).join('')||'<tr><td colspan="7" class="empty">暂无商品</td></tr>';
 document.querySelectorAll('.plan-product').forEach(b=>b.onclick=()=>planProductDetail(b.dataset.name));document.querySelectorAll('.plan-records').forEach(b=>b.onclick=()=>openRecords(b.dataset.name,true));
}
function detail(title,body){$('detailTitle').textContent=title;$('detailBody').innerHTML=body;$('detail').showModal();}
function pairs(values){return `<dl class="detail-list">${values.map(([name,value])=>`<dt>${esc(name)}</dt><dd>${esc(value)}</dd>`).join('')}</dl>`;}
function productDetail(name){const r=dateData.products.find(p=>p.name===name);detail(name+' · '+dateRangeLabel(dateData.filters),`<div class="table-scroll"><table><thead><tr><th></th><th>数量</th>${grades.map(g=>`<th>${gradeLabel(g)}</th>`).join('')}</tr></thead><tbody>${['left','right'].map(h=>`<tr><td>${h==='left'?'左手':'右手'}</td><td>${n(r[h])}</td>${grades.map(g=>`<td>${n(r[h+'_grades'][g])}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`);}
function planProductDetail(name){const p=activePlan(),r=p.products.find(v=>v.name===name);detail(name,pairs([['批次',p.name],['左手实际 / 计划',n(r.left_actual)+' / '+n(r.left_target)],['右手实际 / 计划',n(r.right_actual)+' / '+n(r.right_target)],['左手缺口',n(r.left_gap)],['右手缺口',n(r.right_gap)],['配额进度',(r.completion*100).toFixed(1)+'%']])+`<div class="detail-grades">${grades.filter(g=>r.grades[g]).map(g=>badge(g)+' '+n(r.grades[g])).join(' ')}</div>`);}
function openRecords(name,fromPlan){
 recordOffset=0;recordScope=fromPlan?{start:'',end:'',product:name,basis:planBasis}:{...dateData.filters,product:name,basis:'all'};
 $('recordListTitle').textContent=name+' · '+(fromPlan?'全部日期'+(planBasis==='ab'?' · A/B':''):dateRangeLabel(dateData.filters));
 $('recordList').showModal();loadRecords();
}
async function loadRecords(){
 const current=++recordRequestId;recordRows=[];$('recordCount').textContent='';$('pageInfo').textContent='';$('prev').disabled=true;$('next').disabled=true;$('recordTable').querySelector('tbody').innerHTML='<tr><td colspan="5" class="empty">正在读取…</td></tr>';try{
  const data=await api('/api/collection-dashboard/records?'+new URLSearchParams({...recordScope,offset:recordOffset,limit:recordLimit}));if(current!==recordRequestId)return;
  recordRows=data.rows;$('recordCount').textContent=n(data.total)+' 条';
  $('recordTable').querySelector('tbody').innerHTML=data.rows.map(r=>`<tr><td class="record-path">${esc(r.path)}</td><td>${esc(r.left||'—')}</td><td>${esc(r.right||'—')}</td><td>${badge(r.grade)}<span class="record-grade-source">${sourceLabel(r.grade_source)}</span></td><td><button class="cell-link record-detail" data-id="${r.id}">详情 ↗</button></td></tr>`).join('')||'<tr><td colspan="5" class="empty">暂无记录</td></tr>';
  $('pageInfo').textContent=data.total?`${n(recordOffset+1)}–${n(Math.min(recordOffset+recordLimit,data.total))} / ${n(data.total)}`:'0 条';$('prev').disabled=recordOffset===0;$('next').disabled=recordOffset+recordLimit>=data.total;
  document.querySelectorAll('.record-detail').forEach(b=>b.onclick=()=>recordDetail(b.dataset.id));
 }catch(e){if(current===recordRequestId)$('recordTable').querySelector('tbody').innerHTML='<tr><td colspan="5" class="empty">'+esc(e.message)+'</td></tr>';}
}
function recordDetail(id){
 const r=recordRows.find(v=>v.id===id);
 detail('记录详情',pairs([['记录路径',r.path],['采集日期',r.date||'—'],['左手 / 右手',(r.left||'—')+' / '+(r.right||'—')],['采用等级',gradeLabel(r.grade)+' · '+sourceLabel(r.grade_source)],['人工等级',r.manual_quality_grade||'—'],['自动/VLM等级',r.qc_execution==='incomplete'?'未完成':r.qc_grade||'—'],['采集等级',r.collection_grade||'—'],['人工备注',r.manual_note||'—'],['问题类型',r.manual_problem||'—'],['任务',r.prompt||'—']])+`<div class="video-tabs"><button data-camera="cam_high">头部视频</button><button data-camera="cam_left_wrist">左腕视频</button><button data-camera="cam_right_wrist">右腕视频</button></div><video id="recordVideo" controls preload="none" hidden></video><div id="videoStatus" hidden></div>`);
 document.querySelectorAll('[data-camera]').forEach(b=>b.onclick=()=>{const video=$('recordVideo'),status=$('videoStatus');video.hidden=false;status.hidden=false;status.textContent='正在加载';video.onerror=()=>status.textContent='视频不可用';video.onloadedmetadata=()=>status.hidden=true;video.src=`/api/collection-dashboard/video/${r.id}/${b.dataset.camera}`;video.load();});
}
document.querySelectorAll('[data-mode]').forEach(b=>b.onclick=()=>setMode(b.dataset.mode));
$('dateInput').onchange=$('dateEnd').onchange=()=>setDateRange('custom');$('dateGrade').onchange=reload;document.querySelectorAll('[data-range]').forEach(b=>b.onclick=()=>setDateRange(b.dataset.range));
$('dateSearch').oninput=renderDateTable;$('dateSort').onchange=renderDateTable;
document.querySelectorAll('[data-basis]').forEach(b=>b.onclick=()=>{planBasis=b.dataset.basis;reload();});
$('planSearch').oninput=renderPlanTable;$('planSort').onchange=renderPlanTable;$('planMissingOnly').onchange=renderPlanTable;
$('refresh').onclick=async()=>{try{$('refresh').disabled=true;await api('/api/collection-dashboard/refresh',{method:'POST'});reload();}catch(e){error(e.message);$('refresh').disabled=false;}};
$('exportDate').onclick=()=>{try{location.href='/api/collection-dashboard/export.csv?'+new URLSearchParams({...dateQuery(),kind:'products',search:$('dateSearch').value});}catch(e){error(e.message);}};
$('exportDaily').onclick=()=>{try{location.href='/api/collection-dashboard/export.csv?'+new URLSearchParams({...dateQuery(),kind:'daily'});}catch(e){error(e.message);}};
$('exportPlan').onclick=()=>location.href='/api/collection-dashboard/export.csv?'+new URLSearchParams({kind:'plans',batch:selectedBatch,basis:planBasis});
$('exportRecords').onclick=()=>location.href='/api/collection-dashboard/export.csv?'+new URLSearchParams({...recordScope,kind:'records'});
$('prev').onclick=()=>{recordOffset=Math.max(0,recordOffset-recordLimit);loadRecords();};$('next').onclick=()=>{recordOffset+=recordLimit;loadRecords();};
$('closeRecordList').onclick=()=>$('recordList').close();$('recordList').addEventListener('close',()=>{++recordRequestId;recordRows=[];});
$('closeDetail').onclick=()=>$('detail').close();$('detail').addEventListener('close',()=>{const video=$('recordVideo');if(video){video.pause();video.removeAttribute('src');video.load();}$('detailBody').innerHTML='';});
reload();
let chartResizeTimer=null;
window.addEventListener('resize',()=>{clearTimeout(chartResizeTimer);chartResizeTimer=setTimeout(()=>{if(mode==='dates'&&dateData)renderCharts();},100);});
