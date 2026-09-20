    let skuMatches=[],skuPosition=-1,skuActive='',skuGeneration=0,skuJob='';
    function renderSkuQueue(){
      $('skuEpisode').value=String(skuPosition);
      $('skuEpisode').disabled=!skuMatches.length||batchRunning;
      $('skuPrev').disabled=skuPosition<=0||batchRunning;
      $('skuNext').disabled=skuPosition<0||skuPosition>=skuMatches.length-1||batchRunning;
      const current=skuMatches[skuPosition];
      $('skuEpisode').title=current?`${current.dataset_path}\n${current.episode_name}\n${current.prompt}`:'';
    }
    async function skuNavigate(index){
      if(batchRunning||index<0||index>=skuMatches.length)return;
      skuPosition=index;renderSkuQueue();
      await openDataset(skuMatches[index].dataset_path);
    }
    function clearSkuResults(){
      openRequest++;skuMatches=[];skuPosition=-1;datasets=[];selectedPath='';currentEpisode=null;
      screeningRecords=[];screeningRecordFile='';selectedRecordIds.clear();replaySessions.clear();
      $('replayFrame').removeAttribute('src');$('replayFrame').classList.add('hidden');
      $('viewerEmpty').classList.remove('hidden');$('viewerEmpty').innerHTML='<div><b>尚未选择匹配记录</b><span>选择商品和目录，然后点击“按商品筛选”。</span></div>';
      $('screeningPanel').classList.add('hidden');$('screeningToggle').disabled=true;$('batchExtractBtn').disabled=true;
      $('skuEpisode').innerHTML='<option value="-1">暂无匹配记录</option>';
      renderDatasets();renderSkuQueue();renderScreeningForm();renderScreeningRecords();
    }
    async function skuSearch(){
      if(batchRunning)return;
      const root=$('rootPath').value.trim(),sku=$('skuSelect').value;
      if(!root||!sku){setStatus('请输入数据目录，并从下拉菜单选择商品。','bad');return}
      const generation=++skuGeneration;
      if(skuJob)fetchJson('/api/lerobot-visualization/sku/cancel',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:skuJob})}).catch(()=>{});
      skuJob='';clearSkuResults();skuActive=sku;
      $('scanBtn').disabled=true;$('skuCancel').hidden=false;$('skuWarnings').hidden=true;
      $('skuSummary').textContent=`正在筛选 ${sku}…`;
      try{
        const started=await fetchJson('/api/lerobot-visualization/sku/search',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({root,sku})});
        if(generation!==skuGeneration){fetchJson('/api/lerobot-visualization/sku/cancel',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:started.id})}).catch(()=>{});return}
        skuJob=started.id;
        let state;
        do{
          state=await fetchJson('/api/lerobot-visualization/sku/search?id='+encodeURIComponent(started.id));
          if(generation!==skuGeneration)return;
          const p=state.progress||{};
          setStatus(`已扫描 ${p.scanned_datasets||0} 个数据集、${p.scanned_episodes||0} 条 · 命中 ${p.matched_episodes||0} 条`);
          if(state.status==='running')await new Promise(r=>setTimeout(r,600));
        }while(state.status==='running');
        if(state.status!=='completed')throw Error(state.error||'扫描已取消');
        const result=state.result;skuMatches=result.episodes;datasets=result.datasets;
        renderDatasets();
        $('skuEpisode').replaceChildren(...skuMatches.map((row,index)=>{const o=document.createElement('option');o.value=String(index);o.textContent=`${index+1}/${skuMatches.length} · ${row.relative_path} · ${row.episode_name} · ${row.prompt}`;return o}));
        $('skuSummary').textContent=`${sku}：${skuMatches.length} 条匹配，来自 ${datasets.length} 个目录；已扫描 ${result.scanned_datasets} 个数据集、${result.scanned_episodes} 条`;
        const warnings=result.warnings||[];
        if(warnings.length||result.unparsed_prompts){
          $('skuWarnings').hidden=false;$('skuWarnings').querySelector('summary').textContent=`扫描提示：${warnings.length} 项读取异常，${result.unparsed_prompts||0} 条非标准提示词未纳入精确匹配`;
          $('skuWarnings').querySelector('pre').textContent=warnings.map(w=>`${w.path}：${w.reason}`).join('\n')||'支持 Grasp XXX with the left/right hand 及先左后右模板，也支持提示词直接填写完整商品名。';
        }
        if(skuMatches.length)await skuNavigate(0);
        else{$('viewerEmpty').innerHTML='<div><b>没有匹配记录</b><span>可更换商品或上级目录后重新筛选。</span></div>';setStatus('筛选完成，没有匹配记录');renderSkuQueue()}
      }catch(error){if(generation===skuGeneration){setStatus(error.message||String(error),'bad');$('skuSummary').textContent='筛选未完成，请检查目录后重试'}}
      finally{if(generation===skuGeneration){skuJob='';$('scanBtn').disabled=false;$('skuCancel').hidden=true}}
    }
    $('skuCancel').onclick=()=>{
      skuGeneration++;const id=skuJob;skuJob='';$('scanBtn').disabled=false;$('skuCancel').hidden=true;
      if(id)fetchJson('/api/lerobot-visualization/sku/cancel',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})}).catch(()=>{});
      clearSkuResults();$('skuSummary').textContent='扫描已取消';setStatus('可重新选择目录和商品');
    };
    $('skuPrev').onclick=()=>skuNavigate(skuPosition-1);
    $('skuNext').onclick=()=>skuNavigate(skuPosition+1);
    $('skuEpisode').onchange=()=>skuNavigate(Number($('skuEpisode').value));
    window.addEventListener('message',event=>{
      if(event.origin!==location.origin||event.source!==$('replayFrame').contentWindow)return;
      if(event.data?.type==='unified-screening-select'){
        event.stopImmediatePropagation();
        const index=skuMatches.findIndex(r=>r.dataset_path===event.data.dataset_path&&r.episode_index===Number(event.data.episode_index));
        if(index>=0)skuNavigate(index);else setStatus('该记录不在当前商品筛选结果中','bad');
      }
    },true);
    clearSkuResults();
