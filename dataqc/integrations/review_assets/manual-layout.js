    if(embedded){
      const review=document.querySelector('.review');
      const grade=$('unifiedGrade').closest('section');
      grade.classList.add('grade-editor');
      review.prepend(grade);
      const note=$('unifiedReviewNote');note.placeholder='审核备注';
      $('emptyState').textContent='尚无截帧结果。点击页面顶部“截取图片并识别”，处理所选数据集的全部 episode。';
      $('saveBtn').textContent='保存筛查';$('deleteRecordBtn').textContent='删除记录';
      // Expose the grading and hand choices without dropdowns; keep their data bindings.
      for(const id of ['unifiedGrade','correctArm']){
        const select=$(id), choices=document.createElement('div');choices.className='inline-choices';
        choices.setAttribute('role','group');choices.setAttribute('aria-label',id==='unifiedGrade'?'人工等级':'正确操作手');
        for(const option of select.options){
          const button=document.createElement('button');button.type='button';button.textContent=option.textContent.replace('（left hand）','').replace('（right hand）','').replace('（left + right）','');
          button.dataset.value=option.value;
          button.onclick=()=>{select.value=option.value;select.dispatchEvent(new Event('change'));};
          choices.append(button);
        }
        select.after(choices);select.hidden=true;
        const sync=()=>choices.querySelectorAll('button').forEach(b=>{b.classList.toggle('active',b.dataset.value===select.value);b.setAttribute('aria-pressed',b.dataset.value===select.value);});
        select.addEventListener('change',sync);sync();
        new MutationObserver(sync).observe($('saveStatus'),{childList:true,subtree:true});
      }
    }
