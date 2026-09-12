    $('closeRecords').onclick=()=>$('screeningToggle').click();
    $('datasetPicker').addEventListener('change',()=>{
      if($('datasetPicker').value)openDataset($('datasetPicker').value);
    });
    $('rootPath').addEventListener('keydown',e=>{if(e.key==='Enter')scan();});
    $('batchExtractBtn').addEventListener('click',async()=>{
      if(batchRunning||!selectedPath)return;
      const path=selectedPath;
      batchRunning=true;
      for(const id of ['batchExtractBtn','datasetPicker','scanBtn','rootPath'])$(id).disabled=true;
      const status=$('batchStatus');status.className='';
      status.textContent='正在准备所选数据集的全部 episode…';
      try{
        const found=await fetchJson('/api/manual-screening/datasets?root='+encodeURIComponent(path));
        const group=found.datasets.find(g=>g.sources?.length===1&&g.sources[0].path===path);
        if(!group)throw Error('没有找到与所选目录一致的数据集，未启动处理');
        status.textContent=`正在截帧并识别 ${group.total_episodes} 个 episode…`;
        const started=await fetchJson('/api/manual-screening/extract',{
          method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({dataset_group_id:group.id})
        });
        if(!started.job?.id)throw Error('服务端未返回任务 ID');
        let cursor=0;
        while(true){
          await new Promise(r=>setTimeout(r,900));
          const job=await fetchJson('/api/jobs/'+encodeURIComponent(started.job.id)+'?cursor='+cursor);
          cursor=Number(job.log_cursor||cursor);
          if(job.log?.length)status.textContent=job.log[job.log.length-1];
          if(['completed','failed','stopped'].includes(job.status)){
            if(job.status!=='completed')throw Error(job.status==='stopped'?'任务已停止':'截帧或识别失败，请查看任务日志');
            break;
          }
        }
        const result=await fetchJson('/api/manual-screening/result?dataset_group_id='+encodeURIComponent(group.id));
        status.textContent=`已完成：截帧 ${result.manifest.successful_episode_count}/${result.manifest.episode_count} · YOLO 正确 ${result.yolo_report?.correct_count||0} / 预警 ${result.yolo_report?.warning_count||0}${result.yolo_report?.error_count?' / 识别异常 '+result.yolo_report.error_count:''}`;
        status.className='ok';
        if(path===selectedPath)$('replayFrame').contentWindow.postMessage({type:'unified-screening-refresh'},location.origin);
      }catch(error){status.textContent=error.message||String(error);status.className='bad';}
      finally{
        batchRunning=false;
        for(const id of ['batchExtractBtn','datasetPicker','scanBtn','rootPath'])$(id).disabled=false;
      }
    });
