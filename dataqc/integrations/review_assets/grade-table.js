    const gradeDrafts=new Map(),gradeFeedback=new Map();
    let gradeSaving=false,reportFilter='all',reportDataset='';
    function qualityReportSummary(item){
      const p=item.qc_presentation||{},count=p.problem_count||0;
      return `<span class="qc-row-summary">${escapeHtml(p.summary||item.quality_description||'尚无质检报告')}</span><button type="button" class="qc-report-launch" data-problems="${count>0}" data-qc-episode="${escapeHtml(item.episode_id)}">${count?`查看全部 ${count} 项问题`:'查看全部检查'}</button>${qualityNoteControl(item)}`;
    }
    async function openQcFrame(item,frame){
      try{
        const data=await postJson('/api/replay/start',payload()),url=new URL(data.url||'/replay/',location.origin);
        url.searchParams.set('qc_root',item.episode_dir);url.searchParams.set('qc_frame',frame);
        setPanel('replay');document.getElementById('replayFrame').src=url.href;
      }catch(err){document.getElementById('gradeBulkFeedback').textContent='回放定位失败：'+String(err)}
    }
    const isReviewPending=item=>Boolean(item.review_pending??item.grade_review_required);
    function qualityWarningText(item){
      const primary=String(item.quality_description||'').trim();
      let detail=String(item.warning_text||'');
      if(primary)detail=detail.split(primary).join('');
      return detail.replace(/^[\s；;，,·]+|[\s；;，,·]+$/g,'');
    }
    function applyReportFilter(){
      const dataset=latestEpisodes[0]?.episode_dir?.split('/').slice(0,-1).join('/')||'';
      if(dataset!==reportDataset){reportDataset=dataset;reportFilter='all';for(const id of ['manualGradeFilter','manualProblemFilter','manualNoteFilter','episodeIdentityFilter'])document.getElementById(id).value=''}
      let shown=0;
      document.querySelectorAll('#episodeRows tr').forEach(row=>{
        const id=row.querySelector('.episode-check')?.dataset.episode,item=latestEpisodes.find(e=>e.episode_id===id);
        const identityQuery=document.getElementById('episodeIdentityFilter').value.trim().toLowerCase(),identityNames=[item?.episode_id,item?.collection_episode_name].filter(Boolean);
        const identityMatch=!identityQuery||identityNames.some(name=>/^\d+$/.test(identityQuery)?Number(name.match(/\d+$/)?.[0])===Number(identityQuery):name.toLowerCase().includes(identityQuery));
        row.hidden=!!item&&(!identityMatch||!(reportFilter==='all'||(reportFilter==='changed'?item.grade_changed:isReviewPending(item)))|| (document.getElementById('manualGradeFilter').value&&item.quality_grade!==document.getElementById('manualGradeFilter').value) || (document.getElementById('manualProblemFilter').value&&item.manual_problem!==document.getElementById('manualProblemFilter').value) || (document.getElementById('manualNoteFilter').value&&!String(item.manual_note||'').toLowerCase().includes(document.getElementById('manualNoteFilter').value.toLowerCase())));
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
    for(const id of ['manualGradeFilter','manualProblemFilter','manualNoteFilter','episodeIdentityFilter'])document.getElementById(id).oninput=applyReportFilter;
    document.getElementById('selectPendingReview').onclick=()=>{reportFilter='pending';applyReportFilter();selectEpisodesByPredicate(isReviewPending)};
    function qualityGradeControl(item) {
      const id=String(item.episode_id||''),key=String(item.episode_dir||id);
      const draft=gradeDrafts.get(key),grade=draft?.grade||item.manual_quality_grade||item.quality_grade||'';
      const options=['','A','B','C','F'].map(g=>`<option value="${g}" ${g===grade?'selected':''}>${g||'请选择'}</option>`).join('');
      return `<div class="grade-editor"><select class="quality-grade-select" data-episode="${escapeHtml(id)}" data-root="${escapeHtml(key)}" aria-label="${escapeHtml(id)} 人工等级">${options}</select><button type="button" class="grade-save" data-episode="${escapeHtml(id)}" ${gradeSaving?'disabled':''}>保存</button><small role="status">${escapeHtml(gradeFeedback.get(key)||'')}</small></div>`;
    }
    function qualityNoteControl(item){
      const key=item.episode_dir||item.episode_id,draft=gradeDrafts.get(key),grade=draft?.grade||item.manual_quality_grade||item.quality_grade;
      const note=draft?.note??item.manual_note??'',problem=draft?.problem??item.manual_problem??'';
      return `<div class="manual-note-editor" ${!['B','F'].includes(grade)&&!note&&!problem?'hidden':''}><select class="manual-problem" aria-label="问题类型（可选）">${['','视觉异常','动作异常','时序异常','标注问题','其他'].map(v=>`<option value="${v}" ${v===problem?'selected':''}>${v||'问题类型（可选）'}</option>`).join('')}</select><textarea class="manual-note" maxlength="2000" rows="2" placeholder="原因备注（可选，空白也可保存）" aria-label="原因备注（可选）">${escapeHtml(note)}</textarea></div>`;
    }
    function applyGradeUpdate(result){
      const updates=new Map(result.updated_rows.map(r=>[r.episode_dir,r]));
      latestEpisodes=latestEpisodes.map(r=>updates.get(r.episode_dir)||r);
      for(const old of [...document.querySelectorAll('#episodeRows tr')]){
        const id=old.querySelector('.episode-check')?.dataset.episode,item=latestEpisodes.find(r=>r.episode_id===id);
        if(!item||!updates.has(item.episode_dir))continue;
        const holder=document.createElement('tbody');holder.innerHTML=renderEpisodeRow(item);
        const row=holder.firstElementChild;old.replaceWith(row);
        row.querySelector('.episode-check')?.addEventListener('change',e=>{if(e.target.checked)selectedEpisodes.add(id);else selectedEpisodes.delete(id);updateSelectionInfo()});
      }
      bindQualityGradeControls();renderGradeComparison(result.status);
      if(result.status.qc_overview)renderQcOverview(result.status.qc_overview);
    }
    async function saveQualityGrade(select) {
      const item=latestEpisodes.find(r=>r.episode_id===select.dataset.episode);if(!item||!select.value||gradeSaving)return;
      gradeSaving=true;select.nextElementSibling.disabled=true;
      latestStatusRequestId++;statusAbortController?.abort();
      const key=select.dataset.root,previous=item.quality_grade||'未记录',dataset=fieldValue('hdf5Root'),grade=select.value;
      const row=select.closest('tr'),draft=gradeDrafts.get(key);
      const controls=[select,...row.querySelectorAll('.manual-note,.manual-problem')];controls.forEach(c=>c.disabled=true);
      select.parentElement.querySelector('small').textContent='保存中…';
      try {
        const result=await postJson('/api/grade-choices',payload({source:'manual',episodes:[{episode_name:item.episode_id,grade,revision:draft?.revision||item.grade_revision,note:row.querySelector('.manual-note')?.value||'',problem:row.querySelector('.manual-problem')?.value||''}]}));
        gradeFeedback.set(key,`${previous} → ${grade}，已保存`);gradeDrafts.delete(key);
        gradeSaving=false;
        if(fieldValue('hdf5Root')===dataset)applyGradeUpdate(result);
      } catch(err){gradeFeedback.set(key,String(err));select.parentElement.querySelector('small').textContent=String(err);}
      finally{gradeSaving=false;controls.forEach(c=>c.disabled=false);select.nextElementSibling.disabled=false;}
    }
    function bindQualityGradeControls() {
      document.querySelectorAll('[data-qc-episode]').forEach(button=>button.onclick=async()=>{
        const item=latestEpisodes.find(r=>r.episode_id===button.dataset.qcEpisode);if(!item)return;
        button.disabled=true;
        try{
          const res=await fetch('/api/replay-grade?root='+encodeURIComponent(item.episode_dir),{cache:'no-store'}),snapshot=await res.json();
          if(!res.ok)throw Error(snapshot.error||'报告读取失败');
          if(!latestEpisodes.some(r=>r.episode_dir===item.episode_dir))return;
          window.QCReport.open(snapshot.qc_presentation,{episode:item.episode_id,onFrame:frame=>openQcFrame(item,frame)});
        }catch(err){document.getElementById('gradeBulkFeedback').textContent='报告读取失败：'+String(err)}
        finally{button.disabled=false}
      });
      document.querySelectorAll('.quality-grade-select').forEach(select=>{
        const row=select.closest('tr');
        const changed=()=>{const item=latestEpisodes.find(r=>r.episode_id===select.dataset.episode),old=gradeDrafts.get(select.dataset.root);gradeDrafts.set(select.dataset.root,{grade:select.value,revision:old?.revision||item.grade_revision,note:row.querySelector('.manual-note')?.value||'',problem:row.querySelector('.manual-problem')?.value||''});select.parentElement.querySelector('small').textContent='未保存';row.querySelector('.manual-note-editor').hidden=!['B','F'].includes(select.value)&&!row.querySelector('.manual-note').value&&!row.querySelector('.manual-problem').value};
        select.onchange=changed;row.querySelector('.manual-note').oninput=changed;row.querySelector('.manual-problem').onchange=changed;
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
      const dataset=fieldValue('hdf5Root');latestStatusRequestId++;statusAbortController?.abort();
      document.querySelectorAll('[data-grade-source]').forEach(b=>b.disabled=true);
      try{
        const result=await postJson('/api/grade-choices',payload({source:button.dataset.gradeSource,episodes:latestEpisodes.map(r=>({episode_name:r.episode_id,revision:r.grade_revision}))}));
        for(const row of result.updated_rows){gradeDrafts.delete(row.episode_dir);gradeFeedback.delete(row.episode_dir)}
        gradeSaving=false;if(fieldValue('hdf5Root')===dataset)applyGradeUpdate(result);
        document.getElementById('gradeBulkFeedback').textContent=`已采用 ${result.saved_count} 条`+(result.preserved_manual?.length?`，保留 ${result.preserved_manual.length} 条人工复核等级`:'')+(result.skipped.length?`，${result.skipped.length} 条无对应等级，保持原选择`:'');
      }catch(err){document.getElementById('gradeBulkFeedback').textContent=String(err)}
      finally{gradeSaving=false;document.querySelectorAll('[data-grade-source]').forEach(b=>b.disabled=false)}
    });
