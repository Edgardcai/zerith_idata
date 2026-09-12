"""Compact collection controls and a stable workspace order."""
from pathlib import Path
import re


def integrate_layout(app):
    from .legacy_ui import replace
    html=app.HTML
    nav=[]
    for id,label in [('collectionWorkspaceBtn','① 采集数据质检与转换'),
                     ('lerobotVisualizationWorkspaceBtn','② LeRobot 可视化与人工筛查'),
                     ('lerobotWorkspaceBtn','③ 仿真与真机比较'),
                     ('manualScreeningWorkspaceBtn','④ plumo筛查')]:
        nav.append(f'<button id="{id}" class="workspace-btn{" active" if id=="collectionWorkspaceBtn" else ""}" type="button">{label}</button>')
    html=re.sub(r'(<nav class="workspace-switch"[^>]*>).*?(</nav>)',lambda m:m[1]+'\n'+'\n'.join(nav)+'\n'+m[2],html,count=1,flags=re.S)
    html=replace(html,'    setWorkspace("lerobot-visualization");','    setWorkspace("collection");')
    html=html.replace('title="VLM 自动质检"','title="plumo筛查"')
    html=html.replace('已递归发现 HDF5 数据集，可直接质检或回放。','')
    html=html.replace('任务文本来自当前数据，可编辑。','')
    html=html.replace('按等级生成会覆盖已存在的同名 A/B/C/F LeRobot 等级目录，旧目录不会备份；各等级独立生成，中途失败不会回滚已经完成的等级。确定继续吗？','转换并完整复检通过后，将更新对应等级目录，已有输出会备份。确定继续吗？')
    html=html.replace('pathRows.filter(([label]) => currentMachine !== "simulation" || !label.startsWith("MCAP"))',
                      'pathRows.filter(([label]) => !["zerith","simulation"].includes(currentMachine) || !label.startsWith("MCAP"))')
    start=html.index('      <div class="action-card">')
    end=html.index('      <details class="advanced-settings" open>',start)
    controls=(Path(__file__).parent/'review_assets/collection-controls.html').read_text()
    html=html[:start]+controls+'\n'+html[end:]
    html=html.replace('<details class="advanced-settings" open>','<details class="advanced-settings">')
    html=html.replace('      optimize.disabled = !isZerith || isSimulation;',
                      '      optimize.disabled = !isZerith;\n      optimize.dataset.stage = isSimulation ? "qc" : "optimize_hdf5";')
    html=html.replace('仿真无需真机 UUID 重编号','仿真 HDF5 质检')
    html=html.replace('模块③','模块④ plumo筛查')
    css=(Path(__file__).parent/'review_assets/collection.css').read_text()
    html=replace(html,'  </style>',css+'\n  </style>')
    # Grade provenance is visible without opening a dropdown or details panel.
    html=html.replace('<th>质量等级</th>', '<th>采集等级</th><th>质检后等级</th><th>等级变化</th><th>采用等级</th><th>人工筛选等级</th>',1)
    html=html.replace('<td>${qualityGradeControl(item)}</td>', """<td>${escapeHtml(item.collection_grade || '未记录')}</td>
          <td title="${escapeHtml(item.qc_reason || '')}">${escapeHtml(item.qc_grade_label || item.qc_grade || '待质检')}${item.qc_grade && item.grade_review_required ? ' · 待复核' : ''}</td>
          <td class="${item.grade_changed ? 'grade-changed' : ''}">${item.grade_changed ? escapeHtml(item.collection_grade+' → '+item.qc_grade) : '—'}</td>
          <td><b>${escapeHtml(item.quality_grade || '—')}</b><small class="grade-source">${({qc:'质检',collection:'采集',manual:'人工'})[item.grade_source]||''}</small></td>
          <td>${qualityGradeControl(item)}</td>""")
    html=html.replace('colspan="11"','colspan="15"')
    html=re.sub(r'    function qualityGradeControl\(item\) \{.*?    function selectEpisodesByPredicate',
                '    function selectEpisodesByPredicate',html,count=1,flags=re.S)
    html=re.sub(r'    function bindQualityGradeControls\(\) \{.*?    function bindEpisodeSelection',
                lambda m:(Path(__file__).parent/'review_assets/grade-table.js').read_text()+'\n    function bindEpisodeSelection',html,count=1,flags=re.S)
    html=html.replace('      bindQualityGradeControls();','      bindQualityGradeControls();\n      renderGradeComparison(data);')
    anchor='<tbody id="episodeRows">'
    table=html.rfind('<div class="table-wrap">',0,html.index(anchor))
    if table<0:raise RuntimeError('grade table wrapper missing')
    html=html[:table]+'''<section class="grade-comparison"><div class="grade-toolbar"><b id="gradeComparisonSummary"></b><button type="button" data-grade-source="qc">全部采用质检后等级</button><button type="button" data-grade-source="collection">全部采用质检前等级</button><small id="gradeBulkFeedback" role="status"></small></div><div class="report-filters" aria-label="质检报告筛选"><span>质检报告</span><button type="button" data-report-filter="all" aria-pressed="true">全部</button><button type="button" data-report-filter="changed" aria-pressed="false">等级变化</button><button type="button" data-report-filter="pending" aria-pressed="false">待复核</button><button type="button" id="selectPendingReview">批量选择待复核</button><small id="reportFilterCount" role="status"></small></div></section>'''+html[table:]
    # Keep provenance columns readable; group related measurements into one column.
    table_start=html.index('<thead><tr><th><input id="selectAllEpisodes"')
    table_end=html.index('</thead>',table_start)+len('</thead>')
    html=html[:table_start]+'''<thead><tr><th><input id="selectAllEpisodes" type="checkbox"></th><th>Episode</th><th>采集等级</th><th>质检后等级</th><th>等级变化</th><th>采用等级</th><th>人工筛选等级</th><th>数值指标</th><th>质检说明</th></tr></thead>'''+html[table_end:]
    html=html.replace('<td>${escapeHtml(item.episode_id || "")}</td>', '<td>${escapeHtml(item.episode_id || "")}<small class="grade-source">${escapeHtml(item.collection_status || "")}</small></td>')
    html=re.sub(r'          <td>\$\{escapeHtml\(formatChartValue\(item.fps.*?<td>\$\{badge\(item.collection_status\)\}</td>','',html,count=1,flags=re.S)
    html=re.sub(r'          <td>\$\{escapeHtml\(String\(item.quality_description.*?<td>\$\{escapeHtml\(item.longest_stationary_display \?\? ""\)\}</td>',
        lambda m:'''          <td class="grade-metrics">FPS ${escapeHtml(formatChartValue(item.fps,"",2,true))} · 帧数 ${escapeHtml(item.frame_count ?? '—')} · 缺帧 ${escapeHtml(cameraMissing ?? '—')} · 最长静止 ${escapeHtml(item.longest_stationary_display || '—')} · 预警 ${escapeHtml(item.warning_count ?? '—')}</td>
          <td class="grade-notes">${escapeHtml(item.quality_description || '')}<small>${escapeHtml(qualityWarningText(item))}</small></td>''',html,count=1,flags=re.S)
    html=html.replace('colspan="15"','colspan="9"')
    html=html.replace('Array.from(document.querySelectorAll(".episode-check"))', 'Array.from(document.querySelectorAll(".episode-check")).filter(input=>!input.closest("tr").hidden)')
    html=html.replace('          input.checked = selectAll.checked;', '          if(input.closest("tr").hidden)return;\n          input.checked = selectAll.checked;')
    html=html.replace('      latestEpisodes = [];', '      latestEpisodes = [];\n      renderGradeComparison({episodes:[]});')
    html=html.replace('    setWorkspace("collection");', '    const workbenchHeader=document.querySelector(\'body>header\');\n    const fitReplayWorkspace=()=>document.documentElement.style.setProperty(\'--workbench-header-height\',`${workbenchHeader?.getBoundingClientRect().height||80}px`);\n    if(workbenchHeader)new ResizeObserver(fitReplayWorkspace).observe(workbenchHeader);\n    window.addEventListener(\'resize\',fitReplayWorkspace);fitReplayWorkspace();\n    setWorkspace("collection");')
    app.HTML=html
