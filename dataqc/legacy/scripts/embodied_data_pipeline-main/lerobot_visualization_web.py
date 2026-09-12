"""Independent LeRobot dataset browser and replay workspace."""

from __future__ import annotations


LEROBOT_VISUALIZATION_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>LeRobot 数据可视化</title>
  <style>
    :root {
      --bg: #eef3f8;
      --surface: #fff;
      --line: #d5dfe9;
      --text: #172b3f;
      --muted: #65778a;
      --blue: #1769aa;
      --blue-soft: #eaf4fc;
      --ok: #087c55;
      --bad: #b42318;
      font-family: Inter, "PingFang SC", "Microsoft YaHei", system-ui, sans-serif;
      color: var(--text);
      background: var(--bg);
    }
    * { box-sizing: border-box; }
    body { margin: 0; background: var(--bg); }
    button, input, select, textarea { font: inherit; }
    button {
      min-height: 36px;
      padding: 0 14px;
      border: 0;
      border-radius: 8px;
      color: #fff;
      background: var(--blue);
      font-size: 12px;
      font-weight: 750;
      cursor: pointer;
    }
    button:disabled { opacity: .48; cursor: wait; }
    button.danger { color: #fff; background: var(--bad); }
    button.ghost { color: #35536c; background: #e8eff5; }
    input {
      width: 100%;
      height: 36px;
      padding: 0 10px;
      border: 1px solid #b9c8d6;
      border-radius: 8px;
      color: var(--text);
      background: #fff;
    }
    input:focus { outline: 3px solid rgba(23,105,170,.12); border-color: #3b82c4; }
    .app {
      height: calc(100vh - 1px);
      min-height: 760px;
      display: grid;
      grid-template-columns: minmax(330px, 390px) minmax(700px, 1fr);
      gap: 12px;
      padding: 12px;
      overflow: hidden;
    }
    .sidebar, .viewer {
      min-width: 0;
      min-height: 0;
      border: 1px solid var(--line);
      border-radius: 12px;
      background: var(--surface);
      box-shadow: 0 5px 18px rgba(31,55,78,.05);
    }
    .sidebar { display: grid; grid-template-rows: auto auto minmax(0,1fr); overflow: hidden; }
    .sidebar-head { padding: 15px; border-bottom: 1px solid var(--line); }
    .sidebar-head h1 { margin: 0; font-size: 18px; }
    .sidebar-head p { margin: 5px 0 0; color: var(--muted); font-size: 11px; line-height: 1.5; }
    .source { display: grid; grid-template-columns: minmax(0,1fr) auto; gap: 8px; padding: 13px 15px; border-bottom: 1px solid var(--line); }
    .source label { grid-column: 1 / -1; color: var(--muted); font-size: 11px; font-weight: 750; }
    .source .status { grid-column: 1 / -1; min-height: 17px; color: var(--muted); font-size: 10px; line-height: 1.5; overflow-wrap: anywhere; }
    .source .status.ok { color: var(--ok); }
    .source .status.bad { color: var(--bad); }
    .browser { min-height: 0; display: grid; grid-template-rows: auto auto minmax(0,1fr); padding: 12px; overflow: hidden; }
    .selected-dataset {
      min-width: 0;
      margin-bottom: 10px;
      padding: 9px 10px;
      border: 1px solid #9bc1de;
      border-radius: 9px;
      background: linear-gradient(135deg, #edf7ff, #f6fbff);
    }
    .selected-dataset-label { color: #1769aa; font-size: 9px; font-weight: 850; }
    .selected-dataset-name {
      margin-top: 4px;
      overflow: hidden;
      color: #173b57;
      font-size: 12px;
      font-weight: 850;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .selected-dataset-path {
      margin-top: 3px;
      overflow: hidden;
      color: #526f85;
      font: 9px/1.4 ui-monospace, Consolas, monospace;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .selected-dataset-meta { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 6px; }
    .selected-dataset-meta span { padding: 3px 6px; border-radius: 5px; color: #476277; background: #dfeef8; font-size: 9px; font-weight: 750; }
    .selected-dataset-meta .ready { color: #087443; background: #d9f4e8; }
    .selected-dataset-meta .loading { color: #1769aa; background: #dceefa; }
    .selected-dataset-meta .error { color: #b42318; background: #fee4e2; }
    .browser-toolbar { display: grid; grid-template-columns: minmax(0,1fr) auto; gap: 8px; align-items: center; padding-bottom: 10px; }
    .count { color: var(--muted); font-size: 10px; white-space: nowrap; }
    .datasets { min-height: 0; display: grid; align-content: start; gap: 7px; overflow-y: auto; }
    .dataset {
      width: 100%;
      min-height: 0;
      display: block;
      padding: 10px;
      border: 1px solid var(--line);
      border-radius: 9px;
      color: var(--text);
      background: #fbfcfe;
      text-align: left;
      font-weight: 400;
    }
    .dataset:hover { border-color: #8fb4d0; background: #f5faff; }
    .dataset.active { border-color: var(--blue); background: var(--blue-soft); box-shadow: 0 0 0 2px rgba(23,105,170,.09); }
    .dataset.invalid { border-color: #f1b8b2; background: #fff8f7; cursor: not-allowed; }
    .dataset-name { font-size: 12px; font-weight: 800; overflow-wrap: anywhere; }
    .dataset-path { margin-top: 5px; color: var(--muted); font: 9px/1.45 ui-monospace, Consolas, monospace; overflow-wrap: anywhere; }
    .dataset-meta { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 7px; }
    .dataset-meta span { padding: 3px 6px; border-radius: 5px; color: #526679; background: #edf2f6; font-size: 9px; font-weight: 750; }
    .dataset-error { margin-top: 7px; color: var(--bad); font-size: 9px; line-height: 1.45; overflow-wrap: anywhere; }
    .empty { padding: 35px 12px; color: var(--muted); font-size: 12px; text-align: center; }
    .viewer { display: grid; grid-template-rows: auto minmax(0,1fr) auto; overflow: hidden; background: #0f1114; }
    .current-dataset {
      min-width: 0;
      display: grid;
      grid-template-columns: auto minmax(0,1fr) auto auto;
      gap: 10px;
      align-items: center;
      min-height: 70px;
      padding: 10px 14px;
      border-bottom: 1px solid #333b44;
      color: #edf1f5;
      background: linear-gradient(135deg, #1d2935, #171b20);
    }
    .current-dataset-label {
      padding: 5px 8px;
      border-radius: 6px;
      color: #d9efff;
      background: #1769aa;
      font-size: 10px;
      font-weight: 850;
      white-space: nowrap;
    }
    .current-dataset-main { min-width: 0; }
    .current-dataset-name {
      overflow: hidden;
      color: #fff;
      font-size: 15px;
      font-weight: 850;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .current-dataset-path {
      margin-top: 4px;
      overflow: hidden;
      color: #aab4bf;
      font: 10px/1.4 ui-monospace, Consolas, monospace;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .current-dataset-meta { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 5px; max-width: 300px; }
    .current-dataset-meta span { padding: 4px 7px; border-radius: 5px; color: #c9d4df; background: #2a333c; font-size: 9px; font-weight: 750; white-space: nowrap; }
    .current-dataset-meta .ready { color: #8ce3b5; background: #153c2d; }
    .current-dataset-meta .loading { color: #9ed7ff; background: #163a56; }
    .current-dataset-meta .error { color: #ffb4ad; background: #56221d; }
    .screening-toggle { min-height: 32px; padding: 0 12px; background: #087c55; }
    .screening-toggle.active { color: #173b57; background: #d9f4e8; }
    .replay-stage { position: relative; min-width: 0; min-height: 0; overflow: hidden; }
    #replayFrame { display: block; width: 100%; height: 100%; border: 0; background: #0f1114; }
    .viewer-empty { position: absolute; inset: 0; display: grid; place-items: center; padding: 30px; color: #aab4bf; text-align: center; background: #0f1114; }
    .viewer-empty b { display: block; margin-bottom: 7px; color: #edf1f5; font-size: 17px; }
    .screening-panel {
      max-height: 390px;
      min-height: 245px;
      display: grid;
      grid-template-rows: auto auto minmax(0,1fr);
      color: var(--text);
      background: #f7fafc;
      border-top: 1px solid #3c4650;
      overflow: hidden;
    }
    .screening-head {
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 8px 10px;
      border-bottom: 1px solid var(--line);
      background: #fff;
    }
    .screening-head strong { font-size: 13px; }
    .screening-head span { min-width: 0; overflow: hidden; color: var(--muted); font-size: 10px; text-overflow: ellipsis; white-space: nowrap; }
    .screening-head .record-path { margin-left: auto; max-width: 45%; font-family: ui-monospace, Consolas, monospace; }
    .screening-form {
      display: grid;
      grid-template-columns: 110px 200px 120px minmax(140px,1fr) minmax(150px,1.1fr) auto;
      gap: 7px;
      align-items: end;
      padding: 8px 10px;
      border-bottom: 1px solid var(--line);
      background: #f2f6f9;
    }
    .screen-field { min-width: 0; }
    .screen-field > label { display: block; margin-bottom: 3px; color: var(--muted); font-size: 9px; font-weight: 800; }
    .screen-field select, .screen-field input {
      width: 100%; height: 31px; padding: 0 7px; border: 1px solid #b9c8d6;
      border-radius: 6px; color: var(--text); background: #fff; font-size: 11px;
    }
    .issue-choices { display: flex; flex-wrap: wrap; gap: 5px 9px; min-height: 31px; align-items: center; }
    .issue-choices label { display: inline-flex; align-items: center; gap: 4px; color: #354c60; font-size: 10px; white-space: nowrap; }
    .issue-choices input { width: 14px; height: 14px; margin: 0; }
    .screen-actions { display: flex; gap: 5px; }
    .screen-actions button { min-height: 31px; padding: 0 9px; }
    .screening-status { grid-column: 1 / -1; min-height: 14px; color: var(--muted); font-size: 9px; }
    .screening-status.ok { color: var(--ok); }
    .screening-status.bad { color: var(--bad); }
    .screening-table-wrap { min-height: 0; overflow: auto; background: #fff; }
    .screening-table { width: 100%; border-collapse: collapse; table-layout: fixed; font-size: 10px; }
    .screening-table th, .screening-table td { padding: 6px 7px; border-bottom: 1px solid #e5ebf0; text-align: left; vertical-align: top; }
    .screening-table th { position: sticky; top: 0; z-index: 1; color: #496075; background: #edf3f7; white-space: nowrap; }
    .screening-table tr.current { background: #edf8f3; }
    .screening-table tr:hover { background: #f6faff; }
    .screening-table .select-col { width: 32px; text-align: center; }
    .screening-table .grade-col { width: 48px; }
    .screening-table .episode-col { width: 118px; }
    .screening-table .time-col { width: 128px; }
    .screening-table .action-col { width: 92px; }
    .screening-table input[type=checkbox] { width: 14px; height: 14px; }
    .screening-table button { min-height: 25px; padding: 0 7px; font-size: 9px; }
    .screening-table .danger { margin-left: 4px; }
    .record-empty { padding: 25px; color: var(--muted); text-align: center; }
    .badge { display: inline-block; margin: 0 3px 2px 0; padding: 2px 5px; border-radius: 999px; color: #8d2d26; background: #feecea; font-weight: 750; }
    .badge.grade { color: #5b21b6; background: #ede9fe; }
    .hidden { display: none !important; }
    @media (max-width: 1400px) {
      .screening-form { grid-template-columns: repeat(3, minmax(0,1fr)); }
    }
    @media (max-width: 1050px) {
      body { overflow: auto; }
      .app { height: auto; grid-template-columns: 1fr; overflow: visible; }
      .sidebar { min-height: 520px; }
      .viewer { min-height: 900px; }
      .screening-form { grid-template-columns: repeat(2, minmax(0,1fr)); }
    }
  </style>
</head>
<body>
  <main class="app">
    <aside class="sidebar">
      <div class="sidebar-head">
        <h1>LeRobot 数据可视化</h1>
        <p>递归扫描输入目录；点击数据集后直接回放对应 Episode 的视频、Prompt、State 与 Action。</p>
      </div>
      <div class="source">
        <label for="rootPath">LeRobot 数据目录</label>
        <input id="rootPath" value="/srv/data/datasets/public/stage2_datasets" autocomplete="off" />
        <button id="scanBtn" type="button">递归读取</button>
        <div id="scanStatus" class="status">已填入默认路径；不会自动读取，请点击“递归读取”。</div>
      </div>
      <div class="browser">
        <div id="selectedDataset" class="selected-dataset">
          <div class="selected-dataset-label">当前选中数据集</div>
          <div id="selectedDatasetName" class="selected-dataset-name">尚未选择</div>
          <div id="selectedDatasetPath" class="selected-dataset-path">请从下方列表点击数据集</div>
          <div id="selectedDatasetMeta" class="selected-dataset-meta"><span>等待选择</span></div>
        </div>
        <div class="browser-toolbar">
          <input id="searchInput" placeholder="筛选数据集名称或路径" />
          <span id="datasetCount" class="count">0 个</span>
        </div>
        <div id="datasetList" class="datasets"><div class="empty">尚未读取。确认路径后点击“递归读取”。</div></div>
      </div>
    </aside>
    <section class="viewer">
      <header id="currentDataset" class="current-dataset">
        <span class="current-dataset-label">当前浏览</span>
        <div class="current-dataset-main">
          <div id="currentDatasetName" class="current-dataset-name">尚未选择数据集</div>
          <div id="currentDatasetPath" class="current-dataset-path">请从左侧列表选择</div>
        </div>
        <div id="currentDatasetMeta" class="current-dataset-meta"><span>等待选择</span></div>
        <button id="screeningToggle" class="screening-toggle" type="button" disabled>数据筛查</button>
      </header>
      <div class="replay-stage">
        <div id="viewerEmpty" class="viewer-empty"><div><b>请选择一个 LeRobot 数据集</b><span>选择后将在这里打开原有可视化回放界面。</span></div></div>
        <iframe id="replayFrame" class="hidden" title="LeRobot 视频、Prompt 与动作回放"></iframe>
      </div>
      <section id="screeningPanel" class="screening-panel hidden">
        <div class="screening-head">
          <strong>当前 Episode 数据筛查</strong>
          <span id="screeningEpisode">请先等待回放加载 Episode</span>
          <span id="screeningRecordPath" class="record-path"></span>
        </div>
        <div class="screening-form">
          <div class="screen-field">
            <label for="screeningGrade">筛选等级</label>
            <select id="screeningGrade"><option value="">未设置</option><option>A</option><option>B</option><option>C</option><option>F</option></select>
          </div>
          <div class="screen-field">
            <label>问题类型</label>
            <div class="issue-choices">
              <label><input id="screeningWrongArm" type="checkbox" />左右手错误</label>
              <label><input id="screeningWrongObject" type="checkbox" />物品错误</label>
              <label><input id="screeningBadTrajectory" type="checkbox" />轨迹不好</label>
            </div>
          </div>
          <div class="screen-field">
            <label for="screeningCorrectArm">正确操作手</label>
            <select id="screeningCorrectArm" disabled><option value="">请选择</option><option value="left">左手</option><option value="right">右手</option><option value="both">双手</option></select>
          </div>
          <div class="screen-field">
            <label for="screeningCorrectObject">正确物品</label>
            <input id="screeningCorrectObject" list="screeningObjectOptions" placeholder="请选择或输入正确物品" disabled />
            <datalist id="screeningObjectOptions"></datalist>
          </div>
          <div class="screen-field">
            <label for="screeningNote">备注</label>
            <input id="screeningNote" placeholder="可选：补充轨迹或其他说明" />
          </div>
          <div class="screen-actions">
            <button id="screeningSave" type="button" disabled>保存记录</button>
            <button id="screeningDeleteCurrent" class="danger" type="button" disabled>删除当前</button>
            <button id="screeningDeleteSelected" class="danger" type="button" disabled>删除所选</button>
          </div>
          <div id="screeningStatus" class="screening-status">筛查结果会写入与人工筛查页面相同的统一 JSON 文件。</div>
        </div>
        <div class="screening-table-wrap">
          <table class="screening-table">
            <thead><tr>
              <th class="select-col"><input id="screeningSelectAll" type="checkbox" title="全选当前数据集记录" /></th>
              <th class="grade-col">等级</th><th class="episode-col">Episode</th><th>问题类型</th>
              <th>更正信息</th><th>备注</th><th class="time-col">更新时间</th><th class="action-col">操作</th>
            </tr></thead>
            <tbody id="screeningRecordBody"></tbody>
          </table>
          <div id="screeningEmpty" class="record-empty">当前数据集暂无筛查记录。</div>
        </div>
      </section>
    </section>
  </main>
  <script>
    const $ = id => document.getElementById(id);
    let datasets = [];
    let selectedPath = "";
    let scanRequest = 0;
    let openRequest = 0;
    const replaySessions = new Map();
    let currentEpisode = null;
    let screeningRecords = [];
    let screeningRecordFile = "";
    const selectedRecordIds = new Set();

    const escapeHtml = value => String(value ?? "").replace(/[&<>"']/g, ch => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch]));

    async function fetchJson(url, options = {}) {
      const response = await fetch(url, options);
      const body = await response.json().catch(() => ({}));
      if (!response.ok || body.error) throw new Error(body.error || `HTTP ${response.status}`);
      return body;
    }

    function setStatus(text, kind = "") {
      $('scanStatus').textContent = text;
      $('scanStatus').className = `status ${kind}`.trim();
    }

    function visibleDatasets() {
      const term = $('searchInput').value.trim().toLowerCase();
      if (!term) return datasets;
      return datasets.filter(item => `${item.name} ${item.relative_path} ${item.path}`.toLowerCase().includes(term));
    }

    function renderCurrentDataset(item = null, state = "idle", message = "") {
      if (!item) {
        $('currentDatasetName').textContent = '尚未选择数据集';
        $('currentDatasetPath').textContent = '请从左侧列表选择';
        $('currentDatasetName').title = '';
        $('currentDatasetPath').title = '';
        $('currentDatasetMeta').innerHTML = '<span>等待选择</span>';
        return;
      }
      const name = item.relative_path && item.relative_path !== '.' ? item.relative_path : item.name;
      $('currentDatasetName').textContent = name;
      $('currentDatasetPath').textContent = item.path;
      $('currentDatasetName').title = name;
      $('currentDatasetPath').title = item.path;
      const stateLabel = state === 'ready' ? '正在浏览' : state === 'error' ? '打开失败' : '正在打开';
      const stateClass = state === 'ready' ? 'ready' : state === 'error' ? 'error' : 'loading';
      $('currentDatasetMeta').innerHTML = `<span>${Number(item.episode_count || 0)} Episodes</span><span>${Number(item.video_view_count || 0)} 个视角</span><span class="${stateClass}" title="${escapeHtml(message)}">${stateLabel}</span>`;
    }

    function renderSelectedDataset(item = null, state = "idle", message = "") {
      renderCurrentDataset(item, state, message);
      if (!item) {
        $('selectedDatasetName').textContent = '尚未选择';
        $('selectedDatasetPath').textContent = '请从下方列表点击数据集';
        $('selectedDatasetName').title = '';
        $('selectedDatasetPath').title = '';
        $('selectedDatasetMeta').innerHTML = '<span>等待选择</span>';
        return;
      }
      const name = item.relative_path && item.relative_path !== '.' ? item.relative_path : item.name;
      $('selectedDatasetName').textContent = name;
      $('selectedDatasetPath').textContent = item.path;
      $('selectedDatasetName').title = name;
      $('selectedDatasetPath').title = item.path;
      const stateLabel = state === 'ready' ? '正在浏览' : state === 'error' ? '打开失败' : '正在打开';
      const stateClass = state === 'ready' ? 'ready' : state === 'error' ? 'error' : 'loading';
      $('selectedDatasetMeta').innerHTML = `<span>${Number(item.episode_count || 0)} Episodes</span><span>${Number(item.video_view_count || 0)} 个视角</span><span class="${stateClass}" title="${escapeHtml(message)}">${stateLabel}</span>`;
    }

    function setActiveDatasetCard(path) {
      document.querySelectorAll('.dataset').forEach(card => {
        card.classList.toggle('active', card.dataset.path === path);
      });
    }

    function renderDatasets() {
      const visible = visibleDatasets();
      const validCount = datasets.filter(item => !item.error).length;
      $('datasetCount').textContent = `${visible.length}/${datasets.length} 个 · 可回放 ${validCount}`;
      $('datasetList').innerHTML = visible.length ? visible.map(item => {
        const invalid = Boolean(item.error);
        const active = item.path === selectedPath;
        const views = Number(item.video_view_count || 0);
        return `<button class="dataset ${invalid ? 'invalid' : ''} ${active ? 'active' : ''}" type="button" data-path="${escapeHtml(item.path)}" ${invalid ? 'disabled' : ''}>
          <div class="dataset-name">${escapeHtml(item.name)}</div>
          <div class="dataset-path">${escapeHtml(item.relative_path || item.path)}</div>
          <div class="dataset-meta"><span>${Number(item.episode_count || 0)} Episodes</span><span>${Number(item.fps || 0)} FPS</span><span>${views} 个视角</span><span>映射已校验</span></div>
          ${invalid ? `<div class="dataset-error">不可回放：${escapeHtml(item.error)}</div>` : ''}
        </button>`;
      }).join('') : '<div class="empty">没有匹配的数据集</div>';
      document.querySelectorAll('.dataset:not(:disabled)').forEach(button => button.addEventListener('click', () => openDataset(button.dataset.path)));
    }

    function formatTime(value) {
      const date = new Date(value || '');
      return Number.isNaN(date.getTime()) ? String(value || '-') : date.toLocaleString('zh-CN', {hour12: false});
    }

    function currentScreeningRecord() {
      if (!currentEpisode) return null;
      return screeningRecords.find(item => Number(item.episode_index) === Number(currentEpisode.episode_index)) || null;
    }

    function setScreeningStatus(text, kind = '') {
      $('screeningStatus').textContent = text;
      $('screeningStatus').className = `screening-status ${kind}`.trim();
    }

    function syncScreeningIssueControls() {
      $('screeningCorrectArm').disabled = !$('screeningWrongArm').checked || !currentEpisode;
      $('screeningCorrectObject').disabled = !$('screeningWrongObject').checked || !currentEpisode;
    }

    function renderScreeningForm() {
      const record = currentScreeningRecord();
      const types = new Set(record?.error_types || []);
      const enabled = Boolean(selectedPath && currentEpisode);
      $('screeningEpisode').textContent = currentEpisode
        ? `${currentEpisode.episode_name || `episode_${Number(currentEpisode.episode_index).toString().padStart(6, '0')}`} · ${currentEpisode.task || '未提供 Prompt'}`
        : '请先等待回放加载 Episode';
      $('screeningEpisode').title = $('screeningEpisode').textContent;
      $('screeningGrade').value = record?.quality_grade || currentEpisode?.quality_grade || '';
      $('screeningWrongArm').checked = types.has('wrong_arm');
      $('screeningWrongObject').checked = types.has('wrong_object');
      $('screeningBadTrajectory').checked = types.has('bad_trajectory');
      $('screeningCorrectArm').value = record?.corrections?.arm || '';
      const byArm = record?.corrections?.objects_by_arm || {};
      $('screeningCorrectObject').value = record?.corrections?.object || [byArm.left, byArm.right].filter(Boolean).join(' / ');
      $('screeningNote').value = record?.review_note || '';
      for (const id of ['screeningGrade', 'screeningWrongArm', 'screeningWrongObject', 'screeningBadTrajectory', 'screeningNote']) {
        $(id).disabled = !enabled;
      }
      $('screeningSave').disabled = !enabled;
      $('screeningDeleteCurrent').disabled = !record;
      syncScreeningIssueControls();
    }

    function correctionText(record) {
      const parts = [];
      if (record?.corrections?.arm) parts.push(`操作手→${record.corrections.arm}`);
      if (record?.corrections?.object) parts.push(`物品→${record.corrections.object}`);
      const byArm = record?.corrections?.objects_by_arm || {};
      if (byArm.left) parts.push(`左手物品→${byArm.left}`);
      if (byArm.right) parts.push(`右手物品→${byArm.right}`);
      return parts.join('；') || '-';
    }

    function syncScreeningSelection() {
      const ids = screeningRecords.map(item => String(item.record_id || '')).filter(Boolean);
      const selectedCount = ids.filter(id => selectedRecordIds.has(id)).length;
      $('screeningSelectAll').checked = Boolean(ids.length) && selectedCount === ids.length;
      $('screeningSelectAll').indeterminate = selectedCount > 0 && selectedCount < ids.length;
      $('screeningDeleteSelected').disabled = selectedCount === 0;
    }

    function renderScreeningRecords() {
      const currentIndex = Number(currentEpisode?.episode_index);
      $('screeningRecordBody').innerHTML = screeningRecords.map(record => {
        const id = String(record.record_id || '');
        const current = Number(record.episode_index) === currentIndex;
        return `<tr class="${current ? 'current' : ''}">
          <td class="select-col"><input class="screening-row-select" type="checkbox" data-record-id="${escapeHtml(id)}" ${selectedRecordIds.has(id) ? 'checked' : ''} /></td>
          <td class="grade-col"><span class="badge grade">${escapeHtml(record.quality_grade || record.source_quality_grade || '-')}</span></td>
          <td class="episode-col">${escapeHtml(record.episode_name || `episode_${record.episode_index}`)}</td>
          <td>${(record.error_labels || []).map(label => `<span class="badge">${escapeHtml(label)}</span>`).join('') || '-'}</td>
          <td title="${escapeHtml(correctionText(record))}">${escapeHtml(correctionText(record))}</td>
          <td title="${escapeHtml(record.review_note || '')}">${escapeHtml(record.review_note || '-')}</td>
          <td class="time-col">${escapeHtml(formatTime(record.updated_at))}</td>
          <td class="action-col"><button class="ghost screening-locate" type="button" data-episode-index="${Number(record.episode_index)}">定位</button><button class="danger screening-delete-one" type="button" data-record-id="${escapeHtml(id)}">删除</button></td>
        </tr>`;
      }).join('');
      $('screeningEmpty').classList.toggle('hidden', screeningRecords.length > 0);
      $('screeningRecordPath').textContent = screeningRecordFile ? `JSON：${screeningRecordFile}` : '';
      $('screeningRecordPath').title = screeningRecordFile;
      document.querySelectorAll('.screening-row-select').forEach(input => input.addEventListener('change', () => {
        if (input.checked) selectedRecordIds.add(input.dataset.recordId);
        else selectedRecordIds.delete(input.dataset.recordId);
        syncScreeningSelection();
      }));
      document.querySelectorAll('.screening-delete-one').forEach(button => button.addEventListener('click', () => deleteScreeningRecords([button.dataset.recordId])));
      document.querySelectorAll('.screening-locate').forEach(button => button.addEventListener('click', () => {
        $('replayFrame').contentWindow?.postMessage({type: 'lerobot-visualization-select-episode', episode_index: Number(button.dataset.episodeIndex)}, location.origin);
      }));
      syncScreeningSelection();
    }

    async function loadScreeningRecords() {
      if (!selectedPath) {
        screeningRecords = [];
        screeningRecordFile = '';
        renderScreeningRecords();
        renderScreeningForm();
        return;
      }
      const datasetPath = selectedPath;
      try {
        const data = await fetchJson(`/api/manual-screening/records?dataset_path=${encodeURIComponent(datasetPath)}`, {cache: 'no-store'});
        if (datasetPath !== selectedPath) return;
        screeningRecords = Array.isArray(data.records) ? data.records.slice().sort((a, b) => Number(a.episode_index) - Number(b.episode_index)) : [];
        screeningRecordFile = data.record_file || '';
        for (const id of [...selectedRecordIds]) {
          if (!screeningRecords.some(item => item.record_id === id)) selectedRecordIds.delete(id);
        }
        renderScreeningRecords();
        renderScreeningForm();
      } catch (error) {
        setScreeningStatus(`记录读取失败：${error.message || error}`, 'bad');
      }
    }

    async function saveScreeningRecord() {
      if (!selectedPath || !currentEpisode) return;
      const errorTypes = [];
      if ($('screeningWrongArm').checked) errorTypes.push('wrong_arm');
      if ($('screeningWrongObject').checked) errorTypes.push('wrong_object');
      if ($('screeningBadTrajectory').checked) errorTypes.push('bad_trajectory');
      $('screeningSave').disabled = true;
      setScreeningStatus('正在保存当前 Episode 的筛查结果…');
      try {
        const data = await fetchJson('/api/lerobot-visualization/records', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({
            dataset_path: selectedPath,
            episode_index: currentEpisode.episode_index,
            quality_grade: $('screeningGrade').value,
            error_types: errorTypes,
            corrections: {arm: $('screeningCorrectArm').value, object: $('screeningCorrectObject').value.trim()},
            review_note: $('screeningNote').value.trim(),
          }),
        });
        screeningRecordFile = data.record_file || screeningRecordFile;
        setScreeningStatus('已写入统一 JSON，并同步更新下方记录表。', 'ok');
        await loadScreeningRecords();
      } catch (error) {
        setScreeningStatus(`保存失败：${error.message || error}`, 'bad');
      } finally {
        $('screeningSave').disabled = !currentEpisode;
      }
    }

    async function deleteScreeningRecords(recordIds) {
      const ids = [...new Set((recordIds || []).filter(Boolean))];
      if (!ids.length || !confirm(`确定删除选中的 ${ids.length} 条筛查记录？只删除 JSON 记录，不会删除数据集。`)) return;
      try {
        const data = await fetchJson('/api/manual-screening/records/delete', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({record_ids: ids}),
        });
        ids.forEach(id => selectedRecordIds.delete(id));
        setScreeningStatus(`已删除 ${Number(data.deleted_count || 0)} 条记录。`, 'ok');
        await loadScreeningRecords();
      } catch (error) {
        setScreeningStatus(`删除失败：${error.message || error}`, 'bad');
      }
    }

    async function openDataset(path) {
      if (!path) return;
      const item = datasets.find(value => value.path === path);
      if (!item || item.error) return;
      const requestId = ++openRequest;
      selectedPath = path;
      currentEpisode = null;
      screeningRecords = [];
      selectedRecordIds.clear();
      $('screeningToggle').disabled = true;
      renderScreeningForm();
      renderScreeningRecords();
      setActiveDatasetCard(path);
      renderSelectedDataset(item, 'loading');
      setStatus(`正在校验并打开：${path}`);
      try {
        let url = replaySessions.get(path);
        if (!url) {
          const data = await fetchJson('/api/lerobot-visualization/replay/start', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({dataset_path: path}),
          });
          url = data.url;
          replaySessions.set(path, url);
        }
        if (requestId !== openRequest || selectedPath !== path) return;
        $('replayFrame').src = url;
        $('replayFrame').classList.remove('hidden');
        $('viewerEmpty').classList.add('hidden');
        $('screeningToggle').disabled = false;
        renderSelectedDataset(item, 'ready');
        setStatus(`正在回放：${path}`, 'ok');
        await loadScreeningRecords();
      } catch (error) {
        if (requestId !== openRequest || selectedPath !== path) return;
        $('replayFrame').removeAttribute('src');
        $('replayFrame').classList.add('hidden');
        $('viewerEmpty').classList.remove('hidden');
        $('viewerEmpty').innerHTML = `<div><b>数据集校验失败</b><span>${escapeHtml(error.message || error)}</span></div>`;
        renderSelectedDataset(item, 'error', error.message || String(error));
        $('screeningToggle').disabled = true;
        setStatus(`打开失败：${error.message || error}`, 'bad');
      }
    }

    async function scan() {
      const root = $('rootPath').value.trim();
      if (!root) { setStatus('请输入需要扫描的目录。', 'bad'); return; }
      const requestId = ++scanRequest;
      $('scanBtn').disabled = true;
      setStatus(`正在递归扫描：${root}`);
      try {
        const data = await fetchJson('/api/lerobot-visualization/discover', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({root}),
        });
        if (requestId !== scanRequest) return;
        datasets = Array.isArray(data.datasets) ? data.datasets : [];
        openRequest += 1;
        selectedPath = '';
        currentEpisode = null;
        screeningRecords = [];
        screeningRecordFile = '';
        selectedRecordIds.clear();
        $('screeningToggle').disabled = true;
        $('screeningPanel').classList.add('hidden');
        $('screeningToggle').classList.remove('active');
        replaySessions.clear();
        $('replayFrame').removeAttribute('src');
        $('replayFrame').classList.add('hidden');
        $('viewerEmpty').classList.remove('hidden');
        $('viewerEmpty').innerHTML = '<div><b>请选择一个 LeRobot 数据集</b><span>Episode、Prompt、动作和视频将在打开前进行一致性校验。</span></div>';
        renderSelectedDataset();
        renderScreeningRecords();
        renderScreeningForm();
        renderDatasets();
        const invalid = datasets.filter(item => item.error).length;
        setStatus(`扫描完成：发现 ${datasets.length} 个 LeRobot 数据集，可回放 ${datasets.length - invalid} 个${invalid ? `，校验异常 ${invalid} 个` : ''}。`, invalid ? '' : 'ok');
        const first = datasets.find(item => !item.error);
        if (first) await openDataset(first.path);
      } catch (error) {
        if (requestId !== scanRequest) return;
        datasets = [];
        renderDatasets();
        setStatus(`扫描失败：${error.message || error}`, 'bad');
      } finally {
        if (requestId === scanRequest) $('scanBtn').disabled = false;
      }
    }

    $('scanBtn').addEventListener('click', scan);
    $('searchInput').addEventListener('input', renderDatasets);
    $('screeningToggle').addEventListener('click', async () => {
      const show = $('screeningPanel').classList.contains('hidden');
      $('screeningPanel').classList.toggle('hidden', !show);
      $('screeningToggle').classList.toggle('active', show);
      $('screeningToggle').textContent = show ? '收起筛查' : '数据筛查';
      if (show) await loadScreeningRecords();
    });
    $('screeningWrongArm').addEventListener('change', syncScreeningIssueControls);
    $('screeningWrongObject').addEventListener('change', syncScreeningIssueControls);
    $('screeningSave').addEventListener('click', saveScreeningRecord);
    $('screeningDeleteCurrent').addEventListener('click', () => {
      const record = currentScreeningRecord();
      if (record) deleteScreeningRecords([record.record_id]);
    });
    $('screeningDeleteSelected').addEventListener('click', () => deleteScreeningRecords([...selectedRecordIds]));
    $('screeningSelectAll').addEventListener('change', event => {
      for (const record of screeningRecords) {
        const id = String(record.record_id || '');
        if (!id) continue;
        if (event.target.checked) selectedRecordIds.add(id);
        else selectedRecordIds.delete(id);
      }
      renderScreeningRecords();
    });
    window.addEventListener('message', event => {
      if (event.origin !== location.origin || event.source !== $('replayFrame').contentWindow) return;
      if (event.data?.type !== 'lerobot-visualization-episode') return;
      currentEpisode = event.data.episode || null;
      renderScreeningForm();
      renderScreeningRecords();
    });
    fetchJson('/api/manual-screening/options').then(data => {
      $('screeningObjectOptions').innerHTML = (data.objects || []).map(item => `<option value="${escapeHtml(item)}"></option>`).join('');
    }).catch(() => {});
    renderScreeningForm();
    renderScreeningRecords();
  </script>
</body>
</html>
"""
