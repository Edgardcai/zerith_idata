'use strict';
// One controller owns all media, including cancellation when switching episodes.
const reviewPlayer=(()=>{
 const el=id=>document.getElementById(id),names={left:'左腕',head:'头部',right:'右腕'};
 let generation=0,operation=0,controller=null,timer=null,deadline=null,items=[],intent=false,starting=false,id=null,title='';
 const available=()=>items.filter(i=>!i.error&&i.video.src);
 const duration=()=>Math.min(...available().map(i=>i.video.duration).filter(Number.isFinite));
 const clock=t=>`${String(Math.floor((t||0)/60)).padStart(2,'0')}:${String(Math.floor((t||0)%60)).padStart(2,'0')}`;
 function message(text){el('reviewStatus').textContent=text;}
 function pause(keepIntent=false){operation++;starting=false;if(!keepIntent)intent=false;items.forEach(i=>{i.video.pause();i.video.playbackRate=Number(el('reviewSpeed').value);});}
 function ready(){const a=available();return a.length>0&&a.every(i=>i.video.readyState>=3&&!i.video.seeking);}
 function update(){
  const a=available(),d=duration(),t=a[0]?.video.currentTime||0;
  el('reviewPlay').disabled=!ready();el('reviewSeek').disabled=!Number.isFinite(d);
  el('reviewSeek').max=Number.isFinite(d)?d:0;el('reviewSeek').value=t;
  el('reviewTime').textContent=`${clock(t)} / ${clock(Number.isFinite(d)?d:0)}`;
  for(const i of items)i.status.textContent=i.error||((i.video.readyState>=3&&!i.video.seeking)?'已就绪':'加载中…');
 }
 function fail(i,text){i.error=text;i.video.pause();i.video.removeAttribute('src');i.video.load();update();message('部分录像加载失败，见对应画面提示。可重试或播放已就绪的画面。');}
 async function play(){
  intent=true;if(starting)return;
  if(!ready()){message('正在等待视频缓冲…');return;}
  const a=available(),d=duration();
  if(a.some(i=>i.video.ended)||a[0].video.currentTime>=d-.04){seek(0);return;}
  const op=++operation;starting=true;
  // All play calls happen together; muted playback also works on Edge autoplay policies.
  const result=await Promise.allSettled(a.map(i=>i.video.play()));
  if(op!==operation)return;starting=false;
  const failed=result.find(r=>r.status==='rejected');
  if(failed){pause();message('播放未能启动，请点击三路播放重试；仍失败时点击重新加载。');return;}
  message(a.length===3?'三路同步播放中':`${a.length} 路播放中，其余录像暂不可用`);
 }
 function seek(t){const resume=intent;pause(true);for(const i of available())if(Number.isFinite(i.video.duration))i.video.currentTime=Math.min(t,i.video.duration);intent=resume;update();}
 function tick(){
  update();if(!intent||starting)return;
  const a=available();if(!a.length){intent=false;return;}
  if(a.some(i=>i.video.ended)||a[0].video.currentTime>=duration()-.04){pause();message('播放结束');return;}
  if(!ready()){pause(true);message('正在缓冲，三路暂停等待…');return;}
  if(a.some(i=>i.video.paused)){play();return;}
  const master=a[0].video,rate=Number(el('reviewSpeed').value);
  for(const {video:v} of a.slice(1)){
   const drift=v.currentTime-master.currentTime;
   if(Math.abs(drift)>.25)v.currentTime=master.currentTime;
   v.playbackRate=rate*(Math.abs(drift)>.04?(drift>0?.97:1.03):1);
  }
 }
 function cleanup(){
  generation++;pause();controller?.abort();controller=null;clearInterval(timer);clearTimeout(deadline);
  for(const i of items){i.video.removeAttribute('src');i.video.load();}
  items=[];el('reviewGrid').replaceChildren();
 }
 async function open(ident,label){
  cleanup();id=ident;title=label;const gen=generation;controller=new AbortController();
  el('reviewTitle').textContent=`${label} · 三路录像`;el('reviewPlay').disabled=true;el('reviewSeek').disabled=true;
  if(!el('review').open)el('review').showModal();message('正在定位录像…');
  for(const [name,label] of Object.entries(names)){
   const box=document.createElement('div'),heading=document.createElement('div'),status=document.createElement('span'),video=document.createElement('video');
   heading.textContent=label;status.className='review-camera-status';heading.append(status);
   video.dataset.review=name;video.muted=true;video.defaultMuted=true;video.playsInline=true;video.preload='auto';video.playbackRate=Number(el('reviewSpeed').value);
   box.append(heading,video);el('reviewGrid').append(box);items.push({name,video,status,error:null});
  }
  const fetchTimeout=setTimeout(()=>{if(gen===generation)controller?.abort();},15000);
  try{
   const response=await fetch(`/api/review/${ident}`,{signal:controller.signal,cache:'no-store'});
   const data=await response.json();if(gen!==generation)return;
   if(!response.ok)throw Error(data.error||'请求失败');
   for(const i of items){
    const stream=data.streams[i.name];
    if(!stream?.url){i.error=stream?.error||'未找到此相机录像';continue;}
    i.video.addEventListener('error',()=>{if(gen===generation&&!i.error)fail(i,'录像加载或解码失败，请重新加载');});
    i.video.addEventListener('waiting',()=>{if(gen===generation&&intent)pause(true);});
    i.video.addEventListener('ended',()=>{if(gen===generation){pause();message('播放结束');}});
    i.video.src=stream.url;i.video.load();
   }
   update();message(items.some(i=>i.error)?'部分录像不可用，见对应画面提示。':'正在加载三路录像，准备好后点击三路播放');
   timer=setInterval(tick,150);
   deadline=setTimeout(()=>{if(gen!==generation)return;for(const i of items)if(!i.error&&i.video.readyState<2)fail(i,'录像加载超时，请重新加载');},30000);
  }catch(error){if(gen===generation){items.forEach(i=>i.error=error.name==='AbortError'?'加载超时，请重新加载':error.message);update();message('录像加载失败，请重新加载');}}
  finally{clearTimeout(fetchTimeout);}
 }
 el('reviewPlay').onclick=play;
 el('reviewPause').onclick=()=>{pause();message('已暂停');};
 el('reviewSeek').oninput=e=>seek(Number(e.target.value));
 el('reviewSpeed').onchange=()=>items.forEach(i=>i.video.playbackRate=Number(el('reviewSpeed').value));
 el('reviewRetry').onclick=()=>open(id,title);
 el('closeReview').onclick=()=>el('review').close();
 el('review').addEventListener('close',()=>{if(!el('review').open)cleanup();});
 return {open};
})();
