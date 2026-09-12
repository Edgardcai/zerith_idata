    const gradeDrafts=new Map(),gradeFeedback=new Map();
    let gradeSaving=false,reportFilter='all',reportDataset='';
    const isReviewPending=item=>Boolean(item.review_pending??item.grade_review_required);
    function qualityWarningText(item){
      const primary=String(item.quality_description||'').trim();
      let detail=String(item.warning_text||'');
      if(primary)detail=detail.split(primary).join('');
      return detail.replace(/^[\s；;，,·]+|[\s；;，,·]+$/g,'');
    }
    function applyReportFilter(){
      const dataset=latestEpisodes[0]?.episode_dir?.split('/').slice(0,-1).join('/')||'';
      if(dataset!==reportDataset){reportDataset=dataset;reportFilter='all'}
      let shown=0;
      document.querySelectorAll('#episodeRows tr').forEach(row=>{
        const id=row.querySelector('.episode-check')?.dataset.episode,item=latestEpisodes.find(e=>e.episode_id===id);
        row.hidden=!!item&&!(reportFilter==='all'||(reportFilter==='changed'?item.grade_changed:isReviewPending(item)));
        if(item&&!row.hidden)shown++;
      });
      document.querySelectorAll('[data-report-filter]').forEach(b=>{
        const kind=b.dataset.reportFilter,count=latestEpisodes.filter(r=>kind==='all'||(kind==='changed'?r.grade_changed:isReviewPending(r))).length;
        b.textContent=({all:'全部',changed:'等级变化',pending:'待复核'})[kind]+` ${count}`;
        b.setAttribute('aria-pressed',String(kind===reportFilter));
      });
      document.getElementById('reportFilterCount').textContent=shown?`显示 ${shown} / ${latestEpisodes.length} 条`:'没有符合条件的记录';
      document.getElementById('selectPendingReview').disabled=!latestEpisodes.some(isReviewPending);
      updateSelectionInfo();
    }
    document.querySelectorAll('[data-report-filter]').forEach(b=>b.onclick=()=>{reportFilter=b.dataset.reportFilter;applyReportFilter()});
    document.getElementById('selectPendingReview').onclick=()=>{reportFilter='pending';applyReportFilter();selectEpisodesByPredicate(isReviewPending)};
    function qualityGradeControl(item) {
      const id=String(item.episode_id||''),key=String(item.episode_dir||id);
      const draft=gradeDrafts.get(key),grade=draft?.grade||item.manual_quality_grade||item.quality_grade||'';
      const options=['','A','B','C','F'].map(g=>`<option value="${g}" ${g===grade?'selected':''}>${g||'请选择'}</option>`).join('');
      return `<div class="grade-editor"><select class="quality-grade-select" data-episode="${escapeHtml(id)}" data-root="${escapeHtml(key)}" aria-label="${escapeHtml(id)} 人工等级">${options}</select><button type="button" class="grade-save" data-episode="${escapeHtml(id)}" ${gradeSaving?'disabled':''}>保存</button><small role="status">${escapeHtml(gradeFeedback.get(key)||'')}</small></div>`;
    }
    async function saveQualityGrade(select) {
      const item=latestEpisodes.find(r=>r.episode_id===select.dataset.episode);if(!item||!select.value||gradeSaving)return;
      gradeSaving=true;select.nextElementSibling.disabled=true;
      const key=select.dataset.root,previous=item.quality_grade||'未记录';
      try {
        await postJson('/api/grade-choices',payload({source:'manual',episodes:[{episode_name:item.episode_id,grade:select.value,revision:gradeDrafts.get(key)?.revision||item.grade_revision}]}));
        gradeFeedback.set(key,`${previous} → ${select.value}，已保存`);gradeDrafts.delete(key);
        gradeSaving=false;await refreshStatus();
      } catch(err){gradeFeedback.set(key,String(err));select.parentElement.querySelector('small').textContent=String(err);}
      finally{gradeSaving=false;select.nextElementSibling.disabled=false;}
    }
    function bindQualityGradeControls() {
      document.querySelectorAll('.quality-grade-select').forEach(select=>{
        select.onchange=()=>{const item=latestEpisodes.find(r=>r.episode_id===select.dataset.episode);gradeDrafts.set(select.dataset.root,{grade:select.value,revision:item.grade_revision});select.parentElement.querySelector('small').textContent='未保存'};
        select.nextElementSibling.onclick=()=>saveQualityGrade(select);
      });
    }
    function renderGradeComparison(data){
      applyReportFilter();
      const summary=data.grade_comparison||{},changes=summary.changes||[];
      document.getElementById('gradeComparisonSummary').textContent=`已质检 ${summary.reviewed_count||0} 条 · 等级变化 ${changes.length} 条`+(summary.pending_count?` · 待复核 ${summary.pending_count} 条`:'')+(summary.missing_collection_count?` · 采集等级未记录 ${summary.missing_collection_count} 条`:'');
      document.querySelectorAll('[data-grade-source]').forEach(b=>b.disabled=gradeSaving||data.grade_busy||!data.episodes?.length);
      document.querySelectorAll('.grade-save').forEach(b=>b.disabled=gradeSaving||data.grade_busy);
    }
    document.querySelectorAll('[data-grade-source]').forEach(button=>button.onclick=async()=>{
      if(gradeSaving)return;gradeSaving=true;
      document.querySelectorAll('[data-grade-source]').forEach(b=>b.disabled=true);
      try{
        const result=await postJson('/api/grade-choices',payload({source:button.dataset.gradeSource,episodes:latestEpisodes.map(r=>({episode_name:r.episode_id,revision:r.grade_revision}))}));
        gradeDrafts.clear();gradeFeedback.clear();
        document.getElementById('gradeBulkFeedback').textContent=`已采用 ${result.saved_count} 条`+(result.preserved_manual?.length?`，保留 ${result.preserved_manual.length} 条人工复核等级`:'')+(result.skipped.length?`，${result.skipped.length} 条无对应等级，保持原选择`:'');
      }catch(err){document.getElementById('gradeBulkFeedback').textContent=String(err)}
      finally{gradeSaving=false;await refreshStatus()}
    });
