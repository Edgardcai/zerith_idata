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
    html=html.replace('按等级生成会覆盖已存在的同名 A/B/C/F LeRobot 等级目录，旧目录不会备份；各等级独立生成，中途失败不会回滚已经完成的等级。确定继续吗？','直接转换全部 HDF5，不执行质检；保留已有等级，未评级单独保存。已有同名输出会备份后更新。确定继续吗？')
    html=html.replace('pathRows.filter(([label]) => currentMachine !== "simulation" || !label.startsWith("MCAP"))',
                      'pathRows.filter(([label]) => !["zerith","simulation"].includes(currentMachine) || !label.startsWith("MCAP"))')
    start=html.index('      <div class="action-card">')
    end=html.index('      <details class="advanced-settings" open>',start)
    controls=(Path(__file__).parent/'review_assets/collection-controls.html').read_text()
    html=html[:start]+controls+'\n'+html[end:]
    html=html.replace('<details class="advanced-settings" open>','<details class="advanced-settings">')
    html=html.replace('      optimize.disabled = !isZerith || isSimulation;',
                      '      optimize.disabled = !isZerith;\n      optimize.dataset.stage = "qc";')
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
    html=html.replace('      bindEpisodeSelection();\n      bindQualityGradeControls();','      bindEpisodeSelection();\n      bindQualityGradeControls();\n      renderGradeComparison(data);')
    anchor='<tbody id="episodeRows">'
    table=html.rfind('<div class="table-wrap">',0,html.index(anchor))
    if table<0:raise RuntimeError('grade table wrapper missing')
    html=html[:table]+'''<section class="grade-comparison"><div class="grade-toolbar"><b id="gradeComparisonSummary"></b><button type="button" data-grade-source="qc">全部采用质检后等级</button><button type="button" data-grade-source="collection">全部采用质检前等级</button><small id="gradeBulkFeedback" role="status"></small></div><div class="report-filters" aria-label="质检报告筛选"><span>质检报告</span><button type="button" data-report-filter="all" aria-pressed="true">全部</button><button type="button" data-report-filter="changed" aria-pressed="false">等级变化</button><button type="button" data-report-filter="pending" aria-pressed="false">待复核</button><button type="button" id="selectPendingReview">批量选择待复核</button><label>采用等级 <select id="manualGradeFilter"><option value="">全部</option><option>A</option><option>B</option><option>C</option><option>F</option></select></label><label>问题类型 <select id="manualProblemFilter"><option value="">全部</option><option>视觉异常</option><option>动作异常</option><option>时序异常</option><option>标注问题</option><option>其他</option></select></label><input id="episodeIdentityFilter" placeholder="搜索采集编号或当前目录" aria-label="搜索采集编号或当前目录"><input id="manualNoteFilter" placeholder="搜索人工备注" aria-label="搜索人工备注"><small id="reportFilterCount" role="status"></small></div></section>'''+html[table:]
    # Keep provenance columns readable; group related measurements into one column.
    table_start=html.index('<thead><tr><th><input id="selectAllEpisodes"')
    table_end=html.index('</thead>',table_start)+len('</thead>')
    html=html[:table_start]+'''<thead><tr><th><input id="selectAllEpisodes" type="checkbox"></th><th>当前目录 / 采集编号</th><th>采集等级</th><th>质检后等级</th><th>等级变化</th><th>采用等级</th><th>人工筛选等级</th><th>数值指标</th><th>质检说明</th></tr></thead>'''+html[table_end:]
    html=html.replace('<td>${escapeHtml(item.episode_id || "")}</td>', '<td title="${escapeHtml(item.episode_dir || "")}">目录 ${escapeHtml(item.episode_id || "")}<small class="grade-source">采集 ${escapeHtml(item.collection_episode_name || "编号未记录")}</small><small class="grade-source">${escapeHtml(item.collection_status || "")}</small></td>')
    html=re.sub(r'          <td>\$\{escapeHtml\(formatChartValue\(item.fps.*?<td>\$\{badge\(item.collection_status\)\}</td>','',html,count=1,flags=re.S)
    html=re.sub(r'          <td>\$\{escapeHtml\(String\(item.quality_description.*?<td>\$\{escapeHtml\(item.longest_stationary_display \?\? ""\)\}</td>',
        lambda m:'''          <td class="grade-metrics">FPS ${escapeHtml(formatChartValue(item.fps,"",2,true))} · 帧数 ${escapeHtml(item.frame_count ?? '—')} · 缺帧 ${escapeHtml(cameraMissing ?? '—')} · 最长静止 ${escapeHtml(item.longest_stationary_display || '—')} · 预警 ${escapeHtml(item.warning_count ?? '—')}</td>
          <td class="grade-notes">${qualityReportSummary(item)}</td>''',html,count=1,flags=re.S)
    html=html.replace('colspan="15"','colspan="9"')
    html=html.replace('Array.from(document.querySelectorAll(".episode-check"))', 'Array.from(document.querySelectorAll(".episode-check")).filter(input=>!input.closest("tr").hidden)')
    html=html.replace('          input.checked = selectAll.checked;', '          if(input.closest("tr").hidden)return;\n          input.checked = selectAll.checked;')
    html=html.replace('      latestEpisodes = [];', '      latestEpisodes = [];\n      renderGradeComparison({episodes:[]});')
    html=html.replace('    setWorkspace("collection");', '    const workbenchHeader=document.querySelector(\'body>header\');\n    const fitReplayWorkspace=()=>document.documentElement.style.setProperty(\'--workbench-header-height\',`${workbenchHeader?.getBoundingClientRect().height||80}px`);\n    if(workbenchHeader)new ResizeObserver(fitReplayWorkspace).observe(workbenchHeader);\n    window.addEventListener(\'resize\',fitReplayWorkspace);fitReplayWorkspace();\n    setWorkspace("collection");')
    html=html.replace('<script>', '<link rel="stylesheet" href="/auto/assets/qc-report.css?v=1"><script src="/auto/assets/qc-report.js?v=1"></script><script>',1)
    html=html.replace('        hdf5_root: document.getElementById("hdf5Root").value,', '        qc_force: document.getElementById("qcForce").checked,\n        hdf5_root: document.getElementById("hdf5Root").value,')
    html=html.replace('optimize.textContent = "HDF5 质检";', 'optimize.textContent = "增量质检 HDF5";')
    match=re.search(r'      const rows = latestEpisodes.map\(item => \{(.*?)\n      \}\).join\(""\);',html,flags=re.S)
    if not match:raise RuntimeError('episode row renderer missing')
    body=match.group(1)
    html=html[:match.start()]+'      const rows = latestEpisodes.map(renderEpisodeRow).join("");'+html[match.end():]
    html=html.replace('    async function refreshStatus() {','    function renderEpisodeRow(item) {'+body+'\n    }\n    async function refreshStatus() {')
    html=html.replace('按采集时间原地重编号为 episode1…N，不保留旧目录备份','默认复用已完成结果，只检查新增、变化或未完成记录；人工等级和备注保留')
    app.HTML=html
