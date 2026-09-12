    function renderSelectedDataset(item=null,state="idle",message="") {
      $('datasetPicker').value=item?.path||'';
      $('datasetPicker').title=item?.path||'';
      $('batchExtractBtn').disabled=batchRunning||state!=='ready';
      if(state==='loading')$('batchStatus').textContent='';
    }
    function setActiveDatasetCard(path){$('datasetPicker').value=path;}
    function renderDatasets(){
      $('datasetPicker').innerHTML='<option value="">选择 LeRobot 数据集</option>'+datasets.map(item=>
        `<option value="${escapeHtml(item.path)}" ${item.error?'disabled':''}>${escapeHtml(item.relative_path&&item.relative_path!=='.'?item.relative_path:item.name)} · ${Number(item.episode_count||0)} episodes${item.error?' · 校验失败：'+escapeHtml(item.error):''}</option>`).join('');
      $('datasetPicker').value=selectedPath;
    }
