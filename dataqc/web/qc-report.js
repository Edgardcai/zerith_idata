/* Complete issue list shared by the report table and both replay contexts. */
window.QCReport=(()=>{
 const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 let dialog,report,options,filter='problems',search='',previousFocus;
 function close(){dialog?.close();previousFocus?.focus?.()}
 function render(){
  const all=report.items||[],shown=all.filter(i=>(filter==='all'||i.problem)&&(!search||(i.label+' '+i.text+' '+i.source).toLowerCase().includes(search.toLowerCase())));
  dialog.querySelector('.qc-report-count').textContent=`${report.problem_count||0} 项问题 · 显示 ${shown.length}/${all.length} 项 · 帧号从0开始`;
  dialog.querySelectorAll('[data-qc-filter]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.qcFilter===filter)));
  dialog.querySelector('.qc-report-list').innerHTML=shown.length?shown.map(i=>{
   const index=all.indexOf(i);
   return `<article class="qc-finding" data-severity="${esc(i.status)}"><div class="qc-finding-title"><b>${esc(i.label)}</b><span>${esc(i.status_label)}</span><small>${esc(i.source)}</small></div><p>${esc(i.text)}</p>${i.standard?`<p class="qc-standard">标准：${esc(i.standard)}</p>`:''}${i.frames?.length?`<div class="qc-locations" data-qc-item="${index}" data-position="0"><button type="button" data-qc-prev disabled>上一处</button><button type="button" data-qc-seek>${options.onFrame?'回放':'定位'} 第${i.frames[0]}帧</button><button type="button" data-qc-next ${i.frames.length===1?'disabled':''}>下一处</button><small>1/${i.frames.length} 处</small></div>`:''}</article>`;
  }).join(''):'<p class="qc-empty">'+(search?'没有匹配项目':all.length?'没有异常项目，可查看全部检查':'尚无质检报告')+'</p>';
  dialog.querySelectorAll('.qc-locations').forEach(row=>{
   const frames=all[Number(row.dataset.qcItem)].frames;
   const navigate=delta=>{const p=Math.max(0,Math.min(frames.length-1,Number(row.dataset.position)+delta));row.dataset.position=p;row.querySelector('[data-qc-prev]').disabled=p===0;row.querySelector('[data-qc-next]').disabled=p===frames.length-1;row.querySelector('[data-qc-seek]').textContent=`回放 第${frames[p]}帧`;row.querySelector('small').textContent=`${p+1}/${frames.length} 处`};
   row.querySelector('[data-qc-prev]').onclick=()=>navigate(-1);row.querySelector('[data-qc-next]').onclick=()=>navigate(1);
   row.querySelector('[data-qc-seek]').onclick=()=>{if(options.onFrame){const frame=frames[Number(row.dataset.position)];close();options.onFrame(frame)}};
  });
 }
 return {open(value,opts={}){
  if(!dialog){
   dialog=document.createElement('dialog');dialog.className='qc-report-dialog';
   dialog.innerHTML='<header><strong class="qc-report-title"></strong><button type="button" class="qc-report-close">关闭</button></header><p class="qc-report-task"></p><nav><button type="button" data-qc-filter="problems">全部问题</button><button type="button" data-qc-filter="all">全部检查</button><input type="search" placeholder="搜索部位、异常或数值" aria-label="搜索质检项目"><button type="button" class="qc-report-download">下载完整报告</button></nav><p class="qc-report-count"></p><section class="qc-report-list"></section>';
   document.body.append(dialog);dialog.querySelector('.qc-report-close').onclick=close;
   dialog.querySelectorAll('[data-qc-filter]').forEach(b=>b.onclick=()=>{filter=b.dataset.qcFilter;render()});
   dialog.querySelector('input').oninput=e=>{search=e.target.value;render()};
   dialog.querySelector('.qc-report-download').onclick=()=>{const url=URL.createObjectURL(new Blob([JSON.stringify(report,null,2)],{type:'application/json'})),link=document.createElement('a');link.href=url;link.download='qc-report.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000)};
  }
  previousFocus=document.activeElement;report=value||{};options=opts;filter=report.problem_count?'problems':'all';search='';dialog.querySelector('input').value='';
  dialog.dataset.theme=opts.dark?'dark':'light';dialog.querySelector('.qc-report-title').textContent=(opts.episode||'当前 Episode')+' · 质检详情';dialog.querySelector('.qc-report-task').textContent=report.task||'';
  render();if(!dialog.open)dialog.showModal();dialog.querySelector('.qc-report-close').focus();
 },close};
})();
