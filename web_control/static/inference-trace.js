"use strict";
const $ = id => document.getElementById(id);
const labels = ['左肩俯仰','左肩侧摆','左肩旋转','左肘','左腕侧摆','左腕旋转','左腕俯仰','左夹爪','右肩俯仰','右肩侧摆','右肩旋转','右肘','右腕侧摆','右腕旋转','右腕俯仰','右夹爪','升降柱','腰俯仰','腰旋转','头旋转','头俯仰','底盘线速度','底盘角速度'];
labels.forEach((name,i) => { const o=document.createElement('option');o.value=i;o.textContent=`${i} · ${name}`;$('axis').append(o); });
let data = {events:[],metadata:{units:[]}};
let requestGeneration = 0;
const finite = x => typeof x === 'number' && Number.isFinite(x);
function draw(id, series, unit) {
  const canvas=$(id), rect=canvas.getBoundingClientRect(), dpr=window.devicePixelRatio||1;
  canvas.width=rect.width*dpr;canvas.height=rect.height*dpr;
  const c=canvas.getContext('2d');c.scale(dpr,dpr);
  const w=rect.width,h=rect.height,l=70,r=18,top=30,b=40;
  const points=series.flatMap(s=>s.points).filter(p=>finite(p[0])&&finite(p[1]));
  c.font='12px system-ui';c.fillStyle='#667788';
  if(!points.length){c.fillText('暂无采样。启动推理真机执行后自动记录。',20,60);return;}
  let xmin=Infinity,xmax=-Infinity,ymin=Infinity,ymax=-Infinity;
  for(const [x,y] of points){xmin=Math.min(xmin,x);xmax=Math.max(xmax,x);ymin=Math.min(ymin,y);ymax=Math.max(ymax,y);}
  const pad=Math.max((ymax-ymin)*0.12,0.002);ymin-=pad;ymax+=pad;xmax=Math.max(xmax,xmin+0.1);
  const X=x=>l+(x-xmin)/(xmax-xmin)*(w-l-r), Y=y=>h-b-(y-ymin)/(ymax-ymin)*(h-b-top);
  // Grey bands mark actual request-to-reply intervals, not predicted action time.
  const pending=new Map(), waits=[];
  for(const e of data.events||[]){if(e.kind==='request')pending.set(e.chunk,e.t);else if(e.kind==='chunk'&&pending.has(e.chunk)){waits.push([pending.get(e.chunk),e.t]);pending.delete(e.chunk);}}
  for(const start of pending.values())waits.push([start,xmax]);
  c.fillStyle='#edf0f4';for(const [a,b] of waits){const left=Math.max(a,xmin),right=Math.min(b,xmax);if(right>left)c.fillRect(X(left),top,X(right)-X(left),h-40-top);}
  for(let i=0;i<=4;i++){const y=ymin+(ymax-ymin)*i/4;c.strokeStyle='#e6ebf0';c.beginPath();c.moveTo(l,Y(y));c.lineTo(w-r,Y(y));c.stroke();c.fillStyle='#667788';c.fillText(y.toFixed(3),2,Y(y)+4);const x=xmin+(xmax-xmin)*i/4;c.fillText(x.toFixed(1),X(x)-10,h-16);}
  c.fillText(unit,5,16);c.fillText('时间 (s)',w-65,h-2);
  for(const s of series){c.strokeStyle=s.color;c.lineWidth=1.8;c.beginPath();let last=null;for(const p of s.points){if(!finite(p[1])){last=null;continue;}if(!last)c.moveTo(X(p[0]),Y(p[1]));else{if(s.step)c.lineTo(X(p[0]),Y(last[1]));c.lineTo(X(p[0]),Y(p[1]));}last=p;}c.stroke();if(s.points.length===1){const p=s.points[0];c.fillStyle=s.color;c.fillRect(X(p[0])-2,Y(p[1])-2,4,4);}}
}
function render(){
  const i=Number($('axis').value),unit=data.metadata?.units?.[i]||'rad',events=data.events||[];
  const states=events.filter(e=>e.kind==='state'),steps=events.filter(e=>e.kind==='step');
  const raw=steps.map(e=>[e.t,e.server_action?.[i]]);
  const sent=events.filter(e=>e.kind==='state'||e.kind==='step').map(e=>[e.t,(e.kind==='state'?e.held_action:e.sent_action)?.[i]]).sort((a,b)=>a[0]-b[0]);
  const measured=states.map(e=>[e.t,i>=21&&!e.base_feedback_measured?null:e.state?.[i]]);
  const errors=states.map(e=>[e.t,e.error?.[i]]), values=errors.map(p=>p[1]).filter(finite);
  const format=v=>finite(v)?`${v.toFixed(4)} ${unit}`:'--';
  $('rmse').textContent=format(values.length?Math.sqrt(values.reduce((a,v)=>a+v*v,0)/values.length):null);
  $('peak').textContent=format(values.length?Math.max(...values.map(Math.abs)):null);
  $('latest').textContent=format(values.at(-1));
  const gaps=states.map(e=>e.refresh_gap_ms?.[i]).filter(finite);$('gap').textContent=gaps.length?`${Math.max(...gaps).toFixed(1)} ms`:'--';
  $('chartTitle').textContent=`${labels[i]} · ${unit}`;
  draw('chart',[{points:raw,color:'#b06a14',step:true},{points:sent,color:'#346dd5',step:true},{points:measured,color:'#098367'}],unit);
  draw('errorChart',[{points:errors,color:'#b34c62'}],unit);
  $('details').textContent=data.run_id?`记录 ${data.run_id} · ${data.metadata.execution_mode||''} / ${data.metadata.scheduling_mode||''} · 窗口内 ${states.length} 条 SDK 采样，${steps.length} 个执行步`:'还没有执行记录。Dry-run 不生成真机执行记录。';
  $('warning').textContent=[data.error?`写盘异常：${data.error}`:'',data.dropped_events?`写盘丢失 ${data.dropped_events} 个事件，请检查磁盘负载。`:'',i>=21?'非全身模式底盘没有实测记录，故不显示底盘跟踪误差。':''].filter(Boolean).join(' ');
  for(const [id,file] of [['csv','samples.csv'],['jsonl','events.jsonl']]){$(id).hidden=!data.run_id;if(data.run_id)$(id).href=`/api/pi05/trace/download?run=${encodeURIComponent(data.run_id)}&file=${file}`;}
}
async function json(url){const r=await fetch(url,{cache:'no-store'});if(!r.ok)throw new Error(`HTTP ${r.status}`);return r.json();}
async function refresh(){
  const generation=++requestGeneration, selected=$('runs').value;
  try{
    const [runs,next]=await Promise.all([json('/api/pi05/trace/runs'),json('/api/pi05/trace/data'+(selected?'?run='+encodeURIComponent(selected):''))]);
    if(generation!==requestGeneration)return;
    const existing=new Set([...$('runs').options].map(o=>o.value));
    for(const id of runs.runs){if(!existing.has(id)){const o=document.createElement('option');o.value=id;o.textContent=id;$('runs').append(o);}}
    data=next;$('status').textContent=next.active?'● 正在自动记录':next.writing?'记录写入中':'记录已保存 / 待执行';render();
  }catch(e){if(generation===requestGeneration)$('status').textContent=`读取失败：${e.message}`;}
}
$('axis').addEventListener('change',render);$('runs').addEventListener('change',refresh);window.addEventListener('resize',render);
(async function loop(){await refresh();setTimeout(loop,1000);})();
