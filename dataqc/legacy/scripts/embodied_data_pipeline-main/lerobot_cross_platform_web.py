"""Static operator UI for the independent cross-platform LeRobot workspace."""

from __future__ import annotations


CROSS_PLATFORM_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>跨平台 LeRobot 质检</title>
  <style>
    :root {
      --bg: #f3f6fa;
      --surface: #fff;
      --line: #dce4ec;
      --text: #172b3f;
      --muted: #64768a;
      --blue: #1769aa;
      --sim: #6f52c7;
      --real: #078064;
      --pass: #087c55;
      --warn: #b26a00;
      --fail: #b42318;
      font-family: Inter, "PingFang SC", "Microsoft YaHei", system-ui, sans-serif;
      color: var(--text);
      background: var(--bg);
    }
    * { box-sizing: border-box; }
    body { margin: 0; background: var(--bg); }
    button, input, select, textarea { font: inherit; }
    button {
      min-height: 36px;
      padding: 0 13px;
      border: 0;
      border-radius: 8px;
      color: #fff;
      background: var(--blue);
      font-size: 12px;
      font-weight: 750;
      cursor: pointer;
    }
    button.secondary { color: #344a5e; border: 1px solid #c9d5df; background: #fff; }
    button.danger { background: var(--fail); }
    button:disabled { opacity: .48; cursor: not-allowed; }
    input, select, textarea {
      width: 100%;
      border: 1px solid #b9c8d6;
      border-radius: 8px;
      color: var(--text);
      background: #fff;
    }
    input, select { height: 36px; padding: 0 9px; }
    textarea { min-height: 78px; padding: 9px; resize: vertical; line-height: 1.45; }
    input:focus, select:focus, textarea:focus { outline: 3px solid rgba(23,105,170,.12); border-color: #3b82c4; }
    .app { width: min(1920px, 100%); margin: auto; padding: 14px; }
    .workspace-grid { display: grid; grid-template-columns: minmax(330px, 390px) minmax(720px, 1fr); gap: 12px; align-items: start; }
    .left-column, .right-column { min-width: 0; }
    .left-column { position: sticky; top: 14px; max-height: calc(100vh - 28px); overflow-y: auto; }
    .left-column .panel { margin-bottom: 12px; }
    .right-column .panel { min-height: calc(100vh - 28px); }
    .panel { margin-bottom: 12px; padding: 15px; border: 1px solid var(--line); border-radius: 12px; background: var(--surface); box-shadow: 0 5px 18px rgba(31,55,78,.05); }
    .panel-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; margin-bottom: 12px; }
    .panel-head h2, .panel-head h3 { margin: 0; }
    .panel-head h2 { font-size: 18px; }
    .panel-head h3 { font-size: 15px; }
    .panel-head p { margin: 4px 0 0; color: var(--muted); font-size: 12px; line-height: 1.45; }
    .source-grid { display: grid; grid-template-columns: 1fr; gap: 10px; }
    .source-box { padding: 12px; border: 1px solid var(--line); border-radius: 10px; background: #fafbfd; }
    .source-title { display: flex; align-items: center; justify-content: space-between; margin-bottom: 7px; font-size: 13px; font-weight: 800; }
    .source-title.sim { color: var(--sim); }
    .source-title.real { color: var(--real); }
    .check-line { display: flex; align-items: center; gap: 6px; color: var(--muted); font-size: 11px; font-weight: 600; }
    .check-line input { width: 15px; height: 15px; }
    .toolbar { display: grid; grid-template-columns: 1fr 1fr; align-items: end; gap: 9px; margin-top: 11px; }
    .toolbar label { display: grid; gap: 4px; min-width: 0; color: var(--muted); font-size: 11px; font-weight: 700; }
    .toolbar .grow, .toolbar .wide { grid-column: 1 / -1; min-width: 0; }
    .toolbar .hint { grid-column: 1 / -1; }
    .hint { color: var(--muted); font-size: 11px; line-height: 1.5; }
    .kpis { display: grid; grid-template-columns: repeat(5, minmax(105px,1fr)); gap: 8px; margin-bottom: 12px; }
    .kpi { padding: 11px; border: 1px solid var(--line); border-radius: 9px; background: #fff; }
    .kpi b { display: block; font-size: 21px; line-height: 1.1; }
    .kpi span { display: block; margin-top: 5px; color: var(--muted); font-size: 10px; }
    .status-pass { color: var(--pass); }
    .status-warn { color: var(--warn); }
    .status-fail { color: var(--fail); }
    .pill { display: inline-flex; align-items: center; min-height: 23px; padding: 0 8px; border-radius: 999px; font-size: 10px; font-weight: 850; white-space: nowrap; }
    .pill.pass { color: #076443; background: #ddf5eb; }
    .pill.warn { color: #8c5400; background: #fff0cf; }
    .pill.fail { color: #9d1c13; background: #fee4e2; }
    .pill.simulation { color: #5e43af; background: #ede8ff; }
    .pill.real { color: #066b55; background: #dcf5ed; }
    .table-wrap { max-height: 410px; overflow: auto; border: 1px solid var(--line); border-radius: 9px; }
    table { width: 100%; border-collapse: collapse; font-size: 11px; }
    th, td { padding: 8px; border-bottom: 1px solid #e7edf2; text-align: left; vertical-align: top; }
    th { position: sticky; top: 0; z-index: 2; color: #526679; background: #f4f7fa; font-size: 10px; white-space: nowrap; }
    td.path { min-width: 260px; max-width: 520px; overflow-wrap: anywhere; }
    td.warning { min-width: 250px; color: #8a5200; white-space: pre-wrap; }
    td.range { min-width: 150px; font-family: ui-monospace, Consolas, monospace; white-space: nowrap; }
    .dataset-check, .episode-exclude { width: 15px; height: 15px; }
    .dataset-platform { min-width: 78px; height: 30px; font-size: 11px; }
    .dataset-list { display: grid; gap: 8px; max-height: calc(100vh - 635px); min-height: 180px; overflow: auto; }
    .dataset-card { padding: 10px; border: 1px solid var(--line); border-radius: 9px; background: #fbfcfe; }
    .dataset-card.focused { border-color: #3b82c4; box-shadow: 0 0 0 3px rgba(23,105,170,.10); background: #f5faff; }
    .dataset-card-top { display: grid; grid-template-columns: 20px minmax(0,1fr) 82px; gap: 8px; align-items: center; }
    .dataset-card-name { min-width: 0; font-size: 12px; overflow-wrap: anywhere; }
    .dataset-card-path { margin: 7px 0; color: var(--muted); font-size: 10px; line-height: 1.4; overflow-wrap: anywhere; }
    .dataset-card-meta { display: flex; flex-wrap: wrap; gap: 5px; }
    .dataset-card-meta span { padding: 3px 6px; border-radius: 5px; color: #526679; background: #edf2f6; font-size: 9px; font-weight: 750; }
    .dataset-card-issues { margin-top: 7px; color: #8a5200; font-size: 10px; line-height: 1.4; }
    .dataset-grade-row { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 7px; align-items: center; margin-top: 8px; }
    .dataset-grade-row span { color: var(--muted); font-size: 10px; font-weight: 750; }
    .dataset-grade-mode { height: 30px; font-size: 10px; }
    .tabs { display: flex; gap: 6px; overflow-x: auto; padding-bottom: 1px; margin-bottom: 12px; }
    .tab { min-width: 150px; color: #516578; border: 1px solid #cad6df; background: #fff; white-space: nowrap; }
    .tab.active { color: #fff; border-color: var(--blue); background: var(--blue); }
    .tab-panel.hidden, .hidden { display: none !important; }
    .report-empty { display: grid; place-items: center; min-height: 420px; padding: 30px; color: var(--muted); text-align: center; }
    .report-empty b { display: block; margin-bottom: 8px; color: #334e68; font-size: 17px; }
    .report-section { margin-top: 16px; padding-top: 15px; border-top: 1px solid var(--line); }
    .report-section:first-child { margin-top: 0; padding-top: 0; border-top: 0; }
    .report-section-head { display: flex; justify-content: space-between; gap: 12px; margin-bottom: 9px; }
    .report-section-head h3 { margin: 0; font-size: 14px; }
    .report-section-head p { margin: 3px 0 0; color: var(--muted); font-size: 10px; }
    .qc-check-grid { display: grid; grid-template-columns: repeat(5, minmax(130px, 1fr)); gap: 8px; margin-bottom: 12px; }
    .qc-check-card { padding: 11px; border: 1px solid var(--line); border-radius: 9px; background: #fbfcfe; }
    .qc-check-card-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
    .qc-check-card h3 { margin: 0; font-size: 13px; }
    .qc-check-card p { margin: 6px 0 0; color: var(--muted); font-size: 10px; line-height: 1.45; }
    .qc-results { margin-top: 10px; border-top: 1px solid var(--line); padding-top: 12px; }
    .qc-results-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-bottom: 9px; }
    .qc-results-head h3 { margin: 0; font-size: 14px; }
    .qc-result-toolbar { display: grid; grid-template-columns: 150px 150px minmax(220px,1fr) auto; gap: 8px; align-items: end; margin-bottom: 9px; }
    .qc-result-toolbar label { display: grid; gap: 3px; color: var(--muted); font-size: 10px; font-weight: 700; }
    .qc-result-toolbar input, .qc-result-toolbar select { height: 32px; font-size: 10px; }
    .qc-result-count { align-self: center; color: var(--muted); font-size: 11px; white-space: nowrap; }
    .qc-result-table { max-height: none; min-height: 0; }
    .qc-result-table td { line-height: 1.6; }
    .qc-result-table td.warning { min-width: 220px; max-width: 380px; overflow-wrap: anywhere; }
    .qc-kind { min-width: 95px; font-weight: 800; }
    .qc-target { min-width: 85px; max-width: 180px; overflow-wrap: anywhere; }
    .qc-standard { min-width: 120px; max-width: 160px; color: #526679; }
    .qc-value { min-width: 90px; max-width: 125px; white-space: pre-wrap; overflow-wrap: anywhere; }
    .qc-value.platform-fail { color: #9d1c13; background: #fee4e2; box-shadow: inset 3px 0 #d92d20; font-weight: 700; }
    .prompt-source-list { white-space: pre-wrap; line-height: 1.5; }
    .issue-dataset-path { display: block; overflow-wrap: anywhere; user-select: text; margin: 4px 0 8px; }
    .qc-issue-datasets { min-width: 240px; max-width: 360px; }
    .issue-dataset-ref { display: grid; grid-template-columns: auto minmax(0,1fr); gap: 6px; align-items: start; margin-bottom: 6px; padding: 6px; border-radius: 7px; background: #f8fafc; }
    .issue-dataset-ref:last-child { margin-bottom: 0; }
    .issue-dataset-ref .pill { min-height: 20px; padding: 0 6px; }
    .issue-dataset-name { color: #334e68; font-size: 10px; font-weight: 800; }
    .issue-dataset-path { margin-top: 2px; color: #64768a; font: 9px/1.4 ui-monospace, Consolas, monospace; overflow-wrap: anywhere; }
    .qc-check-card { cursor: pointer; }
    .qc-check-card:hover { border-color: #9fbad0; background: #f5faff; }
    .template { font-family: ui-monospace, Consolas, monospace; color: #334e68; }
    .filters { display: grid; grid-template-columns: repeat(5,minmax(120px,1fr)); gap: 8px; margin-bottom: 9px; }
    .filters label { display: grid; gap: 3px; color: var(--muted); font-size: 10px; font-weight: 700; }
    .review-cell { min-width: 145px; }
    .review-cell select, .review-cell input { height: 29px; font-size: 10px; }
    tr.pending { background: #fff9e9; }
    tr.excluded { background: #fff0ef; opacity: .82; }
    .review-bar { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin: 10px 0; padding: 10px; border: 1px solid #f0d89b; border-radius: 9px; background: #fff9e9; }
    .review-bar b { color: #8a5600; }
    .review-outputs { display: grid; gap: 7px; margin: 9px 0; }
    .review-output { display: grid; grid-template-columns: minmax(140px,.5fr) minmax(300px,1.5fr); align-items: center; gap: 8px; }
    .review-output span { color: #526679; font-size: 11px; font-weight: 700; }
    .review-log { min-height: 90px; max-height: 220px; overflow: auto; padding: 9px; border-radius: 8px; color: #d5f5e3; background: #111827; font: 11px/1.5 ui-monospace, Consolas, monospace; white-space: pre-wrap; }
    .visual-console { min-height: calc(100vh - 130px); }
    .replay-shell { min-width: 0; min-height: 0; overflow: hidden; border: 1px solid #333b44; border-radius: 9px; background: #0f1114; }
    .replay-shell .report-section-head { min-height: 34px; align-items: center; margin-bottom: 6px; }
    #replayFrame { display: block; width: 100%; height: calc(100vh - 145px); min-height: 850px; border: 0; border-radius: 0; background: #111; }
    .empty { padding: 40px 12px; color: var(--muted); text-align: center; }
    @media (max-width: 1250px) {
      .workspace-grid { grid-template-columns: minmax(300px, 350px) minmax(650px, 1fr); }
      .kpis { grid-template-columns: repeat(3,1fr); }
      .qc-check-grid { grid-template-columns: repeat(3,1fr); }
      .filters { grid-template-columns: repeat(2,1fr); }
    }
    @media (max-width: 1020px) {
      .workspace-grid { grid-template-columns: 1fr; }
      .left-column { position: static; max-height: none; overflow: visible; }
      .right-column .panel { min-height: 720px; }
      .dataset-list { max-height: 420px; }
      .qc-result-toolbar { grid-template-columns: repeat(2,minmax(0,1fr)); }
    }
    @media (max-width: 720px) {
      .app { padding: 7px; }
      .source-grid, .contract, .toolbar { grid-template-columns: 1fr; }
      .toolbar .grow, .toolbar .wide, .toolbar .hint { grid-column: auto; }
      .kpis { grid-template-columns: repeat(2,1fr); }
      .qc-check-grid { grid-template-columns: repeat(2,1fr); }
      .filters { grid-template-columns: 1fr; }
      .review-output { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
<div class="app">
  <div class="workspace-grid">
    <aside class="left-column">
      <section class="panel">
        <div class="panel-head">
          <div><h2>数据源与分析</h2></div>
          <span id="sidebarMode" class="pill pass">LeRobot 质检</span>
        </div>
        <div class="toolbar">
          <label>机器人本体<select id="qcRobotType"><option value="zerith" selected>零次方机器人（Zerith）</option><option value="aloha">松灵机器人（ALOHA）</option></select></label>
          <label id="zerithThresholdControl">连续静止帧上限<select id="qcStationaryThreshold"><option>20</option><option>40</option><option selected>60</option></select></label>
          <span id="robotRuleHint" class="hint">零次方：Action / State 各按 8 个部位展示完整 23D；腰、头各 2 维，升降单位 m。其他检查分项汇总，不检查视频完整性。</span>
        </div>
        <div class="source-grid">
          <div class="source-box">
            <div class="source-title sim"><span>仿真数据集路径</span><label class="check-line"><input id="simRecursive" type="checkbox" checked />递归读取</label></div>
            <textarea id="simPaths" placeholder="每行一个服务端目录，例如：&#10;/srv/data/datasets/public/sim_task">/srv/data/datasets/public/market_sim_data</textarea>
          </div>
          <div class="source-box">
            <div class="source-title real"><span>真机数据集路径</span><label class="check-line"><input id="realRecursive" type="checkbox" checked />递归读取</label></div>
            <textarea id="realPaths" placeholder="每行一个服务端目录，例如：&#10;/home/agilex/data/stage2_new/scene13/three_camera_global/lerobot">/srv/data/datasets/public/stage2_datasets</textarea>
          </div>
        </div>
        <div class="toolbar">
          <button id="discoverBtn">读取数据集</button>
          <button id="analyzeBtn" disabled>执行全部质检</button>
          <label>每数据集采样 episode<input id="sampleEpisodes" type="number" min="1" max="200" value="20" /></label>
          <label>每 episode 最大帧数<input id="sampleFrames" type="number" min="50" max="5000" value="1000" /></label>
          <label class="grow">筛选已发现目录<input id="datasetSearch" placeholder="输入数据集名或路径" /></label>
          <span id="sourceHint" class="hint">直接粘贴 LeRobot 数据集目录时只读取该目录。</span>
        </div>
      </section>

      <section class="panel">
        <div class="panel-head">
          <div><h3>已发现数据集</h3></div>
          <span id="datasetCount" class="hint">0 个</span>
        </div>
        <div id="datasetRows" class="dataset-list"><div class="empty">请先粘贴目录并读取</div></div>
      </section>
    </aside>

    <main class="right-column">
      <section id="reportPanel" class="panel">
        <div class="panel-head">
          <div><h2>跨平台 LeRobot 质检</h2></div>
          <span id="globalStatus" class="pill warn">等待分析</span>
        </div>
        <div class="tabs" aria-label="LeRobot 工作区">
          <button class="tab active" data-tab="qc">LeRobot 质检</button>
        </div>
        <div id="reportEmpty" class="report-empty"><div><b>请先执行全部质检</b></div></div>

        <div id="reportContent" class="hidden">
          <div id="qcPanel" class="tab-panel">
            <div id="kpis" class="kpis"></div>
            <div id="qcCheckCards" class="qc-check-grid"></div>
            <section class="qc-results">
              <div class="qc-results-head"><h3>分项质检结果</h3><span class="hint">Action / State 按部位分组 · 其他检查按项目汇总 · 不展开单条轨迹</span></div>
              <div class="qc-result-toolbar">
                <label>质检项目<select id="qcResultCategory"><option value="all" selected>全部项目</option><option value="dataset">数据集结构</option><option value="prompt">Prompt 格式</option><option value="action">Action</option><option value="state">State</option><option value="gripper">夹爪一致性</option><option value="lift">升降柱</option><option value="temporal">时序与运动</option></select></label>
                <label>结果<select id="qcResultStatus"><option value="all" selected>全部结果</option><option value="issues">仅异常/预警</option><option value="fail">失败</option><option value="warn">预警</option><option value="pass">通过</option></select></label>
                <label>搜索<input id="qcResultSearch" placeholder="数据集、维度或问题" /></label>
                <span id="qcResultCount" class="qc-result-count">0 项</span>
              </div>
              <div class="table-wrap qc-result-table"><table><thead><tr><th>结果</th><th>质检项目</th><th>维度 / 检查对象</th><th>标准</th><th>仿真数值 / 统计</th><th>真机数值 / 统计</th><th>异常原因</th><th>涉及数据集</th></tr></thead><tbody id="qcResultRows"></tbody></table></div>
            </section>
          </div>

          <div id="visualPanel" class="tab-panel hidden">
            <div class="visual-console">
              <section id="replayShell" class="replay-shell">
                <div id="visualController" class="hidden" aria-hidden="true">
                  <select id="episodeDataset"><option value="all">全部</option></select>
                  <select id="visualEpisodeSelect"><option value="">没有可回放 episode</option></select>
                  <select id="visualReviewGrade"><option value="">保持原等级</option><option>A</option><option>B</option><option>C</option><option>F</option></select>
                  <input id="visualReason" />
                  <input id="visualExclude" type="checkbox" />
                  <span id="replayHint">请选择 episode。</span>
                  <span id="pendingCount">0 项待处理</span>
                  <button id="prevVisualEpisodeBtn"></button><button id="nextVisualEpisodeBtn"></button><button id="openReplayBtn"></button>
                  <button id="clearReviewBtn"></button><button id="applyReviewBtn" disabled></button>
                  <div id="reviewOutputs"></div><div id="reviewLog">尚未执行批量审核。</div>
                </div>
                <iframe id="replayFrame" title="LeRobot 视频与动作轨迹回放"></iframe>
              </section>
            </div>
          </div>
        </div>
      </section>
    </main>
  </div>
</div>
<script>
  let datasets = [];
  let report = null;
  let activeTab = "qc";
  let pending = new Map();
  let reviewJob = null;
  let reviewCursor = 0;
  let reviewTimer = null;
  let selectedEpisodeKey = "";
  let replayUrls = new Map();
  let focusedDatasetPath = "";

  const $ = id => document.getElementById(id);
  const escapeHtml = value => String(value ?? "").replace(/[&<>"']/g, ch => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch]));
  const statusLabel = value => ({pass:"通过",warn:"预警",fail:"失败"}[value] || value || "-");
  const platformLabel = value => value === "simulation" ? "仿真" : "真机";
  const statusPill = value => `<span class="pill ${escapeHtml(value)}">${escapeHtml(statusLabel(value))}</span>`;
  const platformPill = value => `<span class="pill ${escapeHtml(value)}">${platformLabel(value)}</span>`;
  const formatNumber = value => Number.isFinite(Number(value)) ? Number(value).toPrecision(5).replace(/\.?0+$/, "") : "-";

  async function postJson(path, payload) {
    const response = await fetch(path, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(payload)});
    const data = await response.json();
    if (!response.ok || data.error) throw new Error(data.error || `HTTP ${response.status}`);
    return data;
  }

  function pathLines(id) {
    return $(id).value.split(/\r?\n/).map(value => value.trim()).filter(Boolean);
  }

  function sourcePayload() {
    return [
      ...pathLines("simPaths").map(path => ({path, platform:"simulation", recursive:$('simRecursive').checked})),
      ...pathLines("realPaths").map(path => ({path, platform:"real", recursive:$('realRecursive').checked})),
    ];
  }

  function setBusy(busy, message="") {
    $('discoverBtn').disabled = busy;
    $('qcRobotType').disabled = busy;
    $('qcStationaryThreshold').disabled = busy;
    $('analyzeBtn').disabled = busy || !datasets.length;
    if (message) $('sourceHint').textContent = message;
  }

  function groupDiscoveredDatasets(items) {
    const groups = new Map();
    const standalone = [];
    for (const item of items || []) {
      const grade = String(item.source_grade || '').toUpperCase();
      if (!['A','B','F'].includes(grade)) {
        standalone.push({...item, grade_group:false, selected:true});
        continue;
      }
      const groupPath = item.grade_group_path || item.path;
      const key = `${item.platform}:${groupPath}`;
      if (!groups.has(key)) groups.set(key, {path:groupPath, platform:item.platform, items:{}});
      groups.get(key).items[grade] = item;
    }
    const grouped = [...groups.values()].map(group => {
      const base = group.items.A || group.items.B || group.items.F;
      const available = ['A','B','F'].filter(grade => group.items[grade]);
      return {
        ...base,
        id:`grade-group:${base.id}`,
        name:base.grade_group_name || String(group.path).split('/').filter(Boolean).pop() || base.name,
        path:group.path,
        platform:group.platform,
        grade_group:true,
        grade_items:group.items,
        available_grades:available,
        grade_mode:'all',
        selected:true,
        total_episodes:available.reduce((total, grade) => total + Number(group.items[grade].total_episodes || 0), 0),
        issues:[...new Set(available.flatMap(grade => group.items[grade].issues || []))],
      };
    });
    return [...grouped, ...standalone].sort((a,b) => a.path.localeCompare(b.path, undefined, {numeric:true}));
  }

  async function discover() {
    const sources = sourcePayload();
    if (!sources.length) { $('sourceHint').textContent = "请至少粘贴一个仿真或真机目录。"; return; }
    setBusy(true, "正在扫描目录…");
    try {
      const data = await postJson("/api/cross-platform/discover", {sources, robot_type:$('qcRobotType').value});
      datasets = groupDiscoveredDatasets(data.datasets || []);
      renderDatasets();
      const errors = (data.sources || []).filter(item => item.error).map(item => item.error);
      $('sourceHint').textContent = `发现 ${datasets.length} 个 LeRobot 数据集。${errors.length ? ` ${errors.join("；")}` : ""}`;
    } catch (error) {
      $('sourceHint').textContent = `读取失败：${error}`;
    } finally { setBusy(false); }
  }

  function visibleDatasets() {
    const query = $('datasetSearch').value.trim().toLowerCase();
    return datasets.filter(item => !query || `${item.name} ${item.path}`.toLowerCase().includes(query));
  }

  function highlightDataset(path) {
    focusedDatasetPath = String(path || '');
    document.querySelectorAll('.dataset-card').forEach(card => card.classList.toggle('focused', card.dataset.path === focusedDatasetPath));
  }

  function focusDataset(path) {
    highlightDataset(path);
    if (!report || !focusedDatasetPath) return;
    const item = report.datasets.find(value => value.logical_path === focusedDatasetPath || value.path === focusedDatasetPath);
    if (!item) return;
    const option = [...$('episodeDataset').options].find(value => value.value === (item.logical_id || item.id));
    if (!option) return;
    $('episodeDataset').value = option.value;
    renderEpisodes();
    if (activeTab === 'visual' && selectedEpisodeKey) startReplay(selectedEpisodeKey);
  }

  function renderDatasets() {
    const rows = visibleDatasets();
    $('datasetCount').textContent = `${datasets.length} 个（显示 ${rows.length}）`;
    $('datasetRows').innerHTML = rows.length ? rows.map(item => `<div class="dataset-card ${focusedDatasetPath === item.path ? 'focused' : ''}" data-path="${escapeHtml(item.path)}">
      <div class="dataset-card-top">
        <input class="dataset-check" type="checkbox" data-id="${item.id}" ${item.selected !== false ? 'checked' : ''} />
        <b class="dataset-card-name">${escapeHtml(item.name)}</b>
        <select class="dataset-platform" data-id="${item.id}"><option value="simulation" ${item.platform === "simulation" ? "selected" : ""}>仿真</option><option value="real" ${item.platform === "real" ? "selected" : ""}>真机</option></select>
      </div>
      <div class="dataset-card-path">${escapeHtml(item.path)}</div>
      <div class="dataset-card-meta"><span>${escapeHtml(item.robot_type || "未知机器人")}</span><span>${item.total_episodes ?? "-"} episodes</span><span>state/action ${item.state_dim ?? "?"}/${item.action_dim ?? "?"}</span><span>${(item.video_keys || []).length} 路视频</span></div>
      ${item.grade_group ? `<div class="dataset-grade-row"><span>质量等级</span><select class="dataset-grade-mode" data-id="${item.id}"><option value="all" ${item.grade_mode === 'all' ? 'selected' : ''}>A/B/F 合并（默认，${item.available_grades.join('/')} 可用）</option><option value="A" ${item.grade_mode === 'A' ? 'selected' : ''} ${item.grade_items.A ? '' : 'disabled'}>仅 A</option></select></div>` : ''}
      ${(item.issues || []).length ? `<div class="dataset-card-issues">${escapeHtml(item.issues.join("；"))}</div>` : ""}
    </div>`).join("") : `<div class="empty">没有匹配的数据集</div>`;
    document.querySelectorAll('.dataset-check').forEach(input => input.addEventListener('change', () => {
      const item = datasets.find(value => value.id === input.dataset.id);
      if (item) item.selected = input.checked;
    }));
    document.querySelectorAll('.dataset-platform').forEach(select => select.addEventListener('change', () => {
      const item = datasets.find(value => value.id === select.dataset.id);
      if (item) item.platform = select.value;
    }));
    document.querySelectorAll('.dataset-grade-mode').forEach(select => select.addEventListener('change', () => {
      const item = datasets.find(value => value.id === select.dataset.id);
      if (item) item.grade_mode = select.value;
    }));
    document.querySelectorAll('.dataset-card').forEach(card => card.addEventListener('click', event => {
      if (event.target.closest('input, select')) return;
      focusDataset(card.dataset.path || '');
    }));
    $('analyzeBtn').disabled = !datasets.length;
  }

  function selectedDatasets() {
    return datasets.filter(item => item.selected !== false).flatMap(item => {
      const sources = item.grade_group
        ? (item.grade_mode === 'all' ? item.available_grades.map(grade => item.grade_items[grade]) : [item.grade_items.A].filter(Boolean))
        : [item];
      return sources.map(source => ({
        path:source.path,
        platform:item.platform,
        selected:true,
        logical_name:item.name,
        logical_path:item.path,
        source_grade:source.source_grade || '',
      }));
    });
  }

  async function analyze() {
    const selected = selectedDatasets();
    if (!selected.length) { $('sourceHint').textContent = "请至少勾选一个数据集。"; return; }
    setBusy(true, "正在读取 parquet 并分析，请稍候…");
    try {
      report = await postJson("/api/cross-platform/analyze", {
        datasets:selected,
        robot_type:$('qcRobotType').value,
        stationary_threshold:Number($('qcStationaryThreshold').value),
        max_episodes:Number($('sampleEpisodes').value || 20),
        max_frames:Number($('sampleFrames').value || 1000),
      });
      pending.clear();
      selectedEpisodeKey = '';
      replayUrls.clear();
      $('replayFrame').removeAttribute('src');
      renderReport();
      $('sourceHint').textContent = `分析完成：${report.summary.dataset_count} 个数据集，${report.summary.episode_count} 条 episode。` + (report.sampling?.temporal_checks_full_episode ? ` 本次完整检查 ${report.sampling.checked_episodes} 条 episode；帧数上限仅用于分布采样。` : '');
    } catch (error) {
      $('sourceHint').textContent = `分析失败：${error}`;
    } finally { setBusy(false); }
  }

  function renderKpis() {
    const s = report.summary;
    const values = [
      [statusLabel(s.status), "总体结果", `status-${s.status}`],
      [s.dataset_count, "数据集", ""], [s.simulation_count, "仿真数据集", ""], [s.real_count, "真机数据集", ""],
      [s.episode_count, "episode", ""],
    ];
    $('kpis').innerHTML = values.map(([value,label,cls]) => `<div class="kpi"><b class="${cls}">${value}</b><span>${label}</span></div>`).join('');
    $('globalStatus').className = `pill ${s.status}`;
    $('globalStatus').textContent = statusLabel(s.status);
  }

  function worstStatus(rows) {
    const rank = {pass:0, warn:1, fail:2};
    return (rows || []).reduce((worst, row) => (rank[row.status] > rank[worst] ? row.status : worst), 'pass');
  }

  function renderQcCheckCards() {
    const promptStatus = report.prompts.status;
    const actionChecks = [...report.dimensions.filter(item => item.vector === 'action'), ...report.grippers.filter(item => item.vector === 'action'), ...report.lift.filter(item => item.vector === 'action')];
    const stateChecks = [...report.dimensions.filter(item => item.vector === 'state'), ...report.grippers.filter(item => item.vector === 'state'), ...report.lift.filter(item => item.vector === 'state')];
    const gripperStatus = worstStatus(report.grippers);
    const liftStatus = worstStatus(report.lift);
    const cards = [
      {category:'prompt', name:'Prompt 格式', status:promptStatus, text:`${report.prompts.simulation_templates.length} 种仿真模板 / ${report.prompts.real_templates.length} 种真机模板`},
      {category:'action', name:'Action', status:worstStatus(actionChecks), text:report.contract.robot_type === 'zerith' ? '23 维：双臂、夹爪、升降、腰、头、底盘' : '6 组汇总：双臂、夹爪、升降柱、底盘'},
      {category:'state', name:'State', status:worstStatus(stateChecks), text:report.contract.robot_type === 'zerith' ? '23 维：双臂、夹爪、升降、腰、头、底盘' : '7 组汇总：双臂、夹爪、升降柱、底盘'},
      {category:'gripper', name:'夹爪一致性', status:gripperStatus, text:`${report.summary.gripper_failures} 失败 / ${report.grippers.filter(item => item.status === 'warn').length} 预警`},
      {category:'lift', name:'升降柱', status:liftStatus, text:report.contract.lift_standard || 'Action / State 均应为 200±0.5 mm'},
    ];
    if (report.qc_rows) {
      cards.push(...['dataset', 'temporal'].map(category => ({category,
        name:({dataset:'数据集结构', temporal:'时序与运动'})[category],
        status:worstStatus(report.qc_rows.filter(row => row.category === category)),
        text:'按项目汇总，不展开单条轨迹'})));
      cards.find(row => row.category === 'prompt').status = worstStatus([{status:promptStatus}, ...report.qc_rows.filter(row => row.category === 'prompt')]);
      cards.find(row => row.category === 'gripper').status = worstStatus(report.qc_rows.filter(row => row.category === 'gripper'));
    }
    const summaries = qcResultItems();
    for (const card of cards) {
      const rows = summaries.filter(item => (item.tags || [item.category]).includes(card.category));
      if (rows.length) card.status = worstStatus(rows);
      if (card.category === 'action' || card.category === 'state') card.text = `${rows.length} 组：分别显示维度、两侧范围和异常原因`;
      if (card.category === 'gripper') card.text = '左右夹爪分别比较；活动情况单独统计';
    }
    $('qcCheckCards').innerHTML = cards.map(item => `<div class="qc-check-card" data-category="${item.category}" title="在质检结果中查看该项目"><div class="qc-check-card-head"><h3>${item.name}</h3>${statusPill(item.status)}</div><p>${escapeHtml(item.text)}</p></div>`).join('');
    document.querySelectorAll('.qc-check-card').forEach(card => card.addEventListener('click', () => {
      $('qcResultCategory').value = card.dataset.category || 'all';
      $('qcResultStatus').value = 'all';
      renderQcResults();
    }));
  }

  function issueDetailsForProject(project) {
    return (report?.issue_details || []).filter(item => item.project === project);
  }

  function issueDatasetsHtml(details) {
    const datasets = new Map();
    for (const detail of (details || []).filter(item => item.status !== 'pass')) {
      for (const dataset of detail.datasets || []) {
        const key = `${dataset.platform}:${dataset.path || dataset.dataset_id}`;
        if (!datasets.has(key)) datasets.set(key, {...dataset, statuses:[]});
        datasets.get(key).statuses.push({status:detail.status});
      }
    }
    if (!datasets.size) return '—';
    return [...datasets.values()].map(dataset =>
      `<div>${statusPill(worstStatus(dataset.statuses))} ${escapeHtml(platformLabel(dataset.platform))} · ${escapeHtml(dataset.dataset || '')}${dataset.source_grade ? ` · ${escapeHtml(dataset.source_grade)}` : ''}<code class="issue-dataset-path">${escapeHtml(dataset.path || dataset.logical_path || '目录未知')}</code></div>`
    ).join('');
  }

  function platformStatusClass(item, platform) {
    const status = item.platform_statuses?.[platform] || 'pass';
    return status === 'fail' ? 'platform-fail' : '';
  }

  function gripperText(profile) {
    if (!profile?.count) return '无数据';
    if (profile.binary) {
      return `${formatNumber(profile.min)}～${formatNumber(profile.max)} / 二值档位 ${formatNumber(profile.low_level)}、${formatNumber(profile.high_level)}`;
    }
    return `${formatNumber(profile.min)}～${formatNumber(profile.max)} / 连续值`;
  }

  const DIMENSION_GROUPS = [
    {vector:'action', label:'Action 左臂', indices:[0,1,2,3,4,5], target:'action[0–5]', standard:'左臂 6 关节', tags:['action'], showRange:true},
    {vector:'action', label:'Action 左夹爪', indices:[6], target:'action[6]', standard:'二值 0/0.1；连续 0～0.1（允许 -0.05～0.15）', tags:['action','gripper'], special:'gripper', side:'left'},
    {vector:'action', label:'Action 右臂', indices:[7,8,9,10,11,12], target:'action[7–12]', standard:'右臂 6 关节', tags:['action'], showRange:true},
    {vector:'action', label:'Action 右夹爪', indices:[13], target:'action[13]', standard:'二值 0/0.1；连续 0～0.1（允许 -0.05～0.15）', tags:['action','gripper'], special:'gripper', side:'right'},
    {vector:'action', label:'Action 升降柱', indices:[14], target:'action[14]', standard:'升降柱 200±0.5 mm', tags:['action','lift'], special:'lift'},
    {vector:'action', label:'Action 底盘', indices:[15,16,17], target:'action[15–17]', standard:'底盘 vx / vy / wz', tags:['action']},
    {vector:'state', label:'State 左臂', indices:[0,1,2,3,4,5], target:'state[0–5]', standard:'左臂 6 关节', tags:['state'], showRange:true},
    {vector:'state', label:'State 左夹爪', indices:[6], target:'state[6]', standard:'二值 0/0.1；连续 0～0.1（允许 -0.05～0.15）', tags:['state','gripper'], special:'gripper', side:'left'},
    {vector:'state', label:'State 右臂', indices:[7,8,9,10,11,12], target:'state[7–12]', standard:'右臂 6 关节', tags:['state'], showRange:true},
    {vector:'state', label:'State 右夹爪', indices:[13], target:'state[13]', standard:'二值 0/0.1；连续 0～0.1（允许 -0.05～0.15）', tags:['state','gripper'], special:'gripper', side:'right'},
    {vector:'state', label:'State 升降柱', indices:[14], target:'state[14]', standard:'升降柱 200±0.5 mm', tags:['state','lift'], special:'lift'},
    {vector:'state', label:'State 底盘定位', indices:[15,16,17], target:'state[15–17]', standard:'底盘 x / y / yaw', tags:['state']},
    {vector:'state', label:'State 底盘轮速', indices:[18,19,20], target:'state[18–20]', standard:'底盘 vx / vy / wz', tags:['state']},
  ];

  function groupedWarningReason(value) {
    const text = String(value || '');
    if (text.includes('缺少仿真或真机')) return '缺少仿真或真机数据';
    if (text.includes('缺少第') && text.includes('升降柱')) return '缺少升降柱数据';
    if (text.includes('位置不正确')) return '维度位置不正确';
    return text;
  }

  function dimensionRangeText(item, platform) {
    const stats = item?.[platform];
    if (!stats?.count) return '无数据';
    return `${formatNumber(stats.p01)}～${formatNumber(stats.p99)}`;
  }

  function dimensionProblem(item) {
    const warnings = [...new Set((item.warnings || []).map(groupedWarningReason).filter(Boolean))];
    if (!warnings.length) return '';
    return `${item.name}：仿真 ${dimensionRangeText(item, 'simulation')}，真机 ${dimensionRangeText(item, 'real')}；${warnings.join('；')}`;
  }

  function groupedProblem(dimensions, special) {
    const badRows = dimensions.filter(item => item.status !== 'pass');
    const badDimensions = badRows.length;
    if (!badDimensions && (!special || special.status === 'pass')) return '';
    const parts = [];
    const definitionBad = badRows.filter(item => item.definition_status !== 'pass');
    if (definitionBad.length) parts.push(`${definitionBad.length}/${dimensions.length} 维名称或位置异常`);
    const comparable = badRows.filter(item => item.simulation?.count && item.real?.count);
    if (badRows.some(item => !item.simulation?.count || !item.real?.count)) parts.push('缺少另一平台采样，无法比较分布；不代表已有数据结构错误');
    const dimensionReasons = [...new Set([...definitionBad, ...comparable])].map(dimensionProblem).filter(Boolean);
    if (dimensionReasons.length) {
      parts.push(dimensionReasons.slice(0, 3).join('；'));
      if (dimensionReasons.length > 3) parts.push(`另 ${dimensionReasons.length - 3} 维异常`);
    }
    const specialReasons = [...new Set((special?.warnings || []).map(groupedWarningReason).filter(Boolean))];
    if (specialReasons.length) parts.push(specialReasons.join('；'));
    return parts.join('；');
  }

  function groupedPlatformValue(group, dimensions, special, platform) {
    if (group.special === 'gripper') return gripperText(special?.[platform]);
    if (group.special === 'lift') {
      const stats = special?.[platform];
      return stats?.count ? `${formatNumber(stats.min)}～${formatNumber(stats.max)} ${special?.unit || 'mm'}` : '无数据';
    }
    const stats = dimensions.map(item => item[platform]).filter(item => item?.count);
    if (!stats.length) return '无数据';
    if (group.splitRanges) {
      const labels = {waist_pitch:'pitch', waist_yaw:'yaw', head_yaw:'yaw', head_pitch:'pitch', base_vx:'线速度', base_wz:'角速度'};
      return dimensions.map(item => `${labels[item.name] || item.name}：${dimensionRangeText(item, platform)} ${item.name === 'base_vx' ? 'm/s' : item.name === 'base_wz' ? 'rad/s' : 'rad'}`).join('\n');
    }
    const coverage = `${stats.length}/${dimensions.length} 维有数据`;
    if (!group.showRange) return coverage;
    const lows = stats.map(item => Number(item.p01)).filter(Number.isFinite);
    const highs = stats.map(item => Number(item.p99)).filter(Number.isFinite);
    return lows.length && highs.length ? `${coverage} · P01–P99 ${formatNumber(Math.min(...lows))}～${formatNumber(Math.max(...highs))} ${group.range_unit || ''}` : coverage;
  }

  function groupedPlatformStatus(dimensions, special, platform) {
    const checks = dimensions.map(item => ({status:item.platform_statuses?.[platform] || 'pass'}));
    if (special) checks.push({status:special.platform_statuses?.[platform] || 'pass'});
    return worstStatus(checks);
  }

  function groupedDimensionRows() {
    return (report.contract.dimension_groups || DIMENSION_GROUPS).map(group => {
      const dimensions = report.dimensions.filter(item => item.vector === group.vector && group.indices.includes(item.index));
      const special = group.special === 'gripper'
        ? report.grippers.find(item => item.vector === group.vector && item.side === group.side)
        : group.special === 'lift' ? report.lift.find(item => item.vector === group.vector) : null;
      const checks = [...dimensions, ...(special ? [special] : [])];
      return {
        category:group.vector, tags:group.tags, status:worstStatus(checks), kind:group.label,
        target:group.target, standard:group.standard,
        simulation:groupedPlatformValue(group, dimensions, special, 'simulation'),
        real:groupedPlatformValue(group, dimensions, special, 'real'),
        platform_statuses:{
          simulation:groupedPlatformStatus(dimensions, special, 'simulation'),
          real:groupedPlatformStatus(dimensions, special, 'real'),
        },
        issue_details:issueDetailsForProject(group.label),
        problem:groupedProblem(dimensions, special),
      };
    });
  }

  function promptPlatformStatuses(prompts) {
    if (prompts.platform_statuses) return prompts.platform_statuses;
    const onlySimulation = prompts.only_simulation || [];
    const onlyReal = prompts.only_real || [];
    const simulationTemplates = prompts.simulation_templates || [];
    const realTemplates = prompts.real_templates || [];
    if (!report.summary.simulation_count || !report.summary.real_count) {
      return {simulation:report.summary.simulation_count ? 'pass' : 'warn', real:report.summary.real_count ? 'pass' : 'warn'};
    }
    return {
      simulation:onlyReal.length ? 'fail' : !simulationTemplates.length && realTemplates.length ? 'warn' : 'pass',
      real:onlySimulation.length ? 'fail' : !realTemplates.length && simulationTemplates.length ? 'warn' : 'pass',
    };
  }

  function promptSourcesText(sources) {
    if (!sources.length) return '缺少';
    const count = new Set(sources.map(item => item.dataset_id || item.path)).size;
    return `${sources.length} 条任务文本 / ${count} 个数据集\n示例：${sources[0]?.task || ''}`;
  }

  function qcResultItems() {
    const rows = [];
    const grouped = new Map();
    for (const item of report.datasets || []) {
      const key = `${item.platform}:${item.logical_id || item.id}`;
      if (!grouped.has(key)) grouped.set(key, {base:item, members:[]});
      grouped.get(key).members.push(item);
    }
    for (const group of grouped.values()) {
      const item = group.base;
      const issues = [...new Set(group.members.flatMap(member => [...(member.issues || []), ...(member.sample_errors || [])]))];
      const badShape = group.members.some(member => member.state_dim !== report.contract.state_dim || member.action_dim !== report.contract.action_dim);
      if (badShape) issues.unshift(`维度应为 state ${report.contract.state_dim}D / action ${report.contract.action_dim}D，实际 ${item.state_dim ?? '?'}D / ${item.action_dim ?? '?'}D`);
      const grades = group.members.map(member => member.source_grade).filter(Boolean).join('/');
      const memberIds = new Set(group.members.map(member => member.id));
      const issueDetails = issueDetailsForProject('数据集结构').filter(detail =>
        (detail.datasets || []).some(dataset => memberIds.has(dataset.dataset_id))
      );
      const sampledEpisodes = group.members.reduce((sum, member) => sum + Number(member.sampled_episodes || 0), 0);
      const sampledFrames = group.members.reduce((sum, member) => sum + Number(member.sampled_frames || 0), 0);
      rows.push({
        category:'dataset', status:badShape || issues.length ? 'fail' : 'pass', kind:'数据集结构',
        target:`${item.name}${grades ? ` · ${grades}` : ''}`, standard:`State ${report.contract.state_dim}D / Action ${report.contract.action_dim}D`,
        simulation:item.platform === 'simulation' ? `${sampledEpisodes} 条 / ${sampledFrames} 帧` : '-',
        real:item.platform === 'real' ? `${sampledEpisodes} 条 / ${sampledFrames} 帧` : '-',
        platform_statuses:{simulation:item.platform === 'simulation' && (badShape || issues.length) ? 'fail' : 'pass', real:item.platform === 'real' && (badShape || issues.length) ? 'fail' : 'pass'},
        issue_details:issueDetails.length ? issueDetails : (badShape || issues.length ? [{status:'fail', datasets:group.members.filter(member => member.state_dim !== report.contract.state_dim || member.action_dim !== report.contract.action_dim || (member.issues || []).length || (member.sample_errors || []).length)}] : []),
        problem:badShape ? 'State / Action 维度与所选机器人约定不一致' : issues.length ? '存在元数据或 Parquet 读取异常' : '',
      });
    }

    const prompts = report.prompts || {};
    rows.push({
      category:'prompt', status:prompts.status || 'warn', kind:'Prompt 格式', target:prompts.validation_mode === 'fixed_templates' ? '固定模板合规性' : '总体格式一致性',
      standard:report.contract.prompt_standard || '忽略左右手及物品后，其余格式一致',
      simulation:`${(prompts.simulation_templates || []).length} 种标准模板`, real:`${(prompts.real_templates || []).length} 种标准模板`,
      platform_statuses:promptPlatformStatuses(prompts),
      issue_details:prompts.issue_details || issueDetailsForProject('Prompt 格式'),
      problem:(prompts.warnings || []).join('；'),
    });
    for (const item of prompts.formats || []) {
      const simulation = item.simulation || [], real = item.real || [];
      const status = item.status || (simulation.length && real.length ? 'pass' : !report.summary.simulation_count || !report.summary.real_count ? 'warn' : 'fail');
      rows.push({category:'prompt', status, kind:'Prompt 模板', target:item.template || '未识别模板',
        standard:prompts.standard || '仅比较格式，商品名称、左右手和任务条数无需一一对应',
        simulation:promptSourcesText(simulation), real:promptSourcesText(real),
        platform_statuses:item.platform_statuses || {simulation:simulation.length ? 'pass' : status, real:real.length ? 'pass' : status},
        issue_details:issueDetailsForProject('Prompt 格式').filter(detail => detail.target === item.template),
        problem:status === 'pass' ? '' : item.warnings ? item.warnings.join('；') : `${simulation.length ? '真机' : '仿真'}侧缺少该模板；具体差异见总体格式一致性`});
    }
    rows.push(...groupedDimensionRows());
    rows.push(...(report.qc_rows || []));
    return rows.map(item => item.status === 'pass' ? {...item, problem:''} : item);
  }

  function renderQcResults() {
    if (!report) return;
    const category = $('qcResultCategory').value;
    const status = $('qcResultStatus').value;
    const query = $('qcResultSearch').value.trim().toLowerCase();
    const allRows = qcResultItems();
    const rows = allRows.filter(item =>
      (category === 'all' || (item.tags || [item.category]).includes(category))
      && (status === 'all' || status === 'issues' && item.status !== 'pass' || item.status === status)
      && (!query || `${item.kind} ${item.target} ${item.standard} ${item.simulation} ${item.real} ${item.problem} ${(item.issue_details || []).flatMap(detail => (detail.datasets || []).map(dataset => `${dataset.dataset} ${dataset.source_grade} ${dataset.path}`)).join(' ')}`.toLowerCase().includes(query))
    );
    $('qcResultCount').textContent = `${rows.length} / ${allRows.length} 项`;
    $('qcResultRows').innerHTML = rows.map(item => {
      const promptClass = item.category === 'prompt' ? 'prompt-source-list' : '';
      return `<tr><td>${statusPill(item.status)}</td><td class="qc-kind">${escapeHtml(item.kind)}</td><td class="qc-target">${escapeHtml(item.target)}</td><td class="qc-standard">${escapeHtml(item.standard)}</td><td class="qc-value ${platformStatusClass(item, 'simulation')}"><div class="${promptClass}">${escapeHtml(item.simulation)}</div></td><td class="qc-value ${platformStatusClass(item, 'real')}"><div class="${promptClass}">${escapeHtml(item.real)}</div></td><td class="warning">${escapeHtml(item.problem)}</td><td class="qc-issue-datasets">${issueDatasetsHtml(item.issue_details || [])}</td></tr>`;
    }).join('') || `<tr><td colspan="8" class="empty">没有匹配的质检结果</td></tr>`;
  }

  function episodeFilters() {
    return {dataset:$('episodeDataset').value};
  }

  function filteredEpisodes() {
    const f = episodeFilters();
    return report.episodes.filter(item => {
      return f.dataset === 'all' || item.logical_id === f.dataset;
    });
  }

  function pushEmbeddedReviewState() {
    const frame = $('replayFrame');
    if (!frame?.contentWindow) return;
    const groups = new Map();
    for (const item of report?.datasets || []) groups.set(item.logical_id || item.id, item);
    const item = selectedEpisode();
    const change = item ? pendingValue(item) : {quality_grade:'', exclude:false, reason:''};
    const logLines = $('reviewLog').textContent.trim().split(/\r?\n/).filter(Boolean);
    frame.contentWindow.postMessage({
      type:'lerobot-review-state',
      datasets:[...groups].map(([id,value]) => ({id, label:`${value.name} · ${platformLabel(value.platform)}`})),
      selected_dataset:$('episodeDataset').value,
      episodes:(report ? filteredEpisodes() : []).map(value => ({
        key:value.key,
        label:`${value.dataset}${value.source_grade ? ` · ${value.source_grade}` : ''} · episode ${value.episode_index}${value.quality_grade ? ` · ${value.quality_grade}` : ''}`,
      })),
      selected_key:selectedEpisodeKey,
      review:change,
      pending_count:pending.size,
      apply_disabled:pending.size === 0 || Boolean(reviewJob),
      status_text:logLines.at(-1) || $('replayHint').textContent || '',
    }, location.origin);
  }

  function pendingValue(item) { return pending.get(item.key) || {episode_index:item.episode_index, quality_grade:'', exclude:false, reason:''}; }

  function renderEpisodes() {
    const rows = filteredEpisodes();
    const select = $('visualEpisodeSelect');
    const preferred = rows.some(item => item.key === selectedEpisodeKey) ? selectedEpisodeKey : rows[0]?.key || '';
    select.innerHTML = rows.length
      ? rows.map(item => `<option value="${item.key}">${escapeHtml(item.dataset)}${item.source_grade ? ` · ${item.source_grade}` : ''} · episode ${item.episode_index}${item.quality_grade ? ` · ${item.quality_grade}` : ''}</option>`).join('')
      : `<option value="">没有匹配 episode</option>`;
    select.value = preferred;
    selectedEpisodeKey = preferred;
    select.disabled = !rows.length;
    $('prevVisualEpisodeBtn').disabled = !rows.length || select.selectedIndex <= 0;
    $('nextVisualEpisodeBtn').disabled = !rows.length || select.selectedIndex >= rows.length - 1;
    $('openReplayBtn').disabled = !rows.length;
    renderSelectedEpisode();
    pushEmbeddedReviewState();
  }

  function selectedEpisode() {
    return report?.episodes.find(item => item.key === selectedEpisodeKey) || null;
  }

  function moveVisualEpisode(delta) {
    const select = $('visualEpisodeSelect');
    if (!select.options.length) return;
    const next = Math.max(0, Math.min(select.options.length - 1, select.selectedIndex + delta));
    if (next === select.selectedIndex) return;
    select.selectedIndex = next;
    selectedEpisodeKey = select.value;
    renderSelectedEpisode();
    $('prevVisualEpisodeBtn').disabled = select.selectedIndex <= 0;
    $('nextVisualEpisodeBtn').disabled = select.selectedIndex >= select.options.length - 1;
    startReplay(selectedEpisodeKey);
  }

  function renderSelectedEpisode() {
    const item = selectedEpisode();
    const disabled = !item;
    highlightDataset(item?.logical_path || item?.dataset_path || '');
    const change = item ? pendingValue(item) : {quality_grade:'', exclude:false, reason:''};
    $('visualReviewGrade').value = change.quality_grade || '';
    $('visualExclude').checked = Boolean(change.exclude);
    $('visualReason').value = change.reason || '';
    for (const control of [$('visualReviewGrade'), $('visualExclude'), $('visualReason')]) control.disabled = disabled;
  }

  function updatePendingFromVisual() {
    const item = selectedEpisode(); if (!item) return;
    const grade = $('visualReviewGrade').value;
    const exclude = $('visualExclude').checked;
    const reason = $('visualReason').value.trim();
    if (grade || exclude || reason) pending.set(item.key, {episode_index:item.episode_index, quality_grade:grade, exclude, reason});
    else pending.delete(item.key);
    renderPendingSummary();
  }

  function renderPendingSummary() {
    $('pendingCount').textContent = `${pending.size} 项待处理`;
    $('applyReviewBtn').disabled = pending.size === 0 || Boolean(reviewJob);
    const datasetIds = [...new Set([...pending.keys()].map(key => key.split(':')[0]))];
    $('reviewOutputs').innerHTML = datasetIds.map(id => {
      const dataset = report.datasets.find(item => item.id === id);
      return `<div class="review-output"><span>${escapeHtml(dataset.name)}${dataset.source_grade ? ` · ${dataset.source_grade}` : ''} 审核后输出</span><input class="review-output-path" data-id="${id}" value="${escapeHtml(dataset.default_output_path)}" /></div>`;
    }).join('');
    pushEmbeddedReviewState();
  }

  async function startReplay(key) {
    const item = report.episodes.find(row => row.key === key); if (!item) return;
    setTab('visual');
    selectedEpisodeKey = item.key;
    $('visualEpisodeSelect').value = item.key;
    renderSelectedEpisode();
    $('replayHint').textContent = `正在打开 ${item.dataset} / episode ${item.episode_index}…`;
    try {
      let replayUrl = replayUrls.get(item.dataset_path);
      if (!replayUrl) {
        const data = await postJson('/api/cross-platform/replay/start', {dataset_path:item.dataset_path, episode_index:item.episode_index});
        replayUrl = data.url;
        replayUrls.set(item.dataset_path, replayUrl);
      }
      $('replayFrame').src = `${replayUrl}&episode_index=${item.episode_index}&embedded_review=1`;
      $('replayHint').textContent = `${item.dataset} · ${platformLabel(item.platform)} · episode ${item.episode_index}`;
    } catch (error) { $('replayHint').textContent = `回放打开失败：${error}`; }
  }

  function reviewPayload() {
    const grouped = new Map();
    for (const [key, change] of pending) {
      const item = report.episodes.find(row => row.key === key); if (!item) continue;
      if (!grouped.has(item.dataset_id)) grouped.set(item.dataset_id, []);
      grouped.get(item.dataset_id).push(change);
    }
    return [...grouped].map(([id, annotations]) => {
      const dataset = report.datasets.find(item => item.id === id);
      const output = document.querySelector(`.review-output-path[data-id="${CSS.escape(id)}"]`);
      return {source_path:dataset.path, output_path:output?.value.trim() || dataset.default_output_path, annotations};
    });
  }

  async function applyReview() {
    const groups = reviewPayload(); if (!groups.length) return;
    if (!confirm(`将一次性处理 ${pending.size} 项标注并生成 ${groups.length} 个新数据集。源数据不会修改，是否继续？`)) return;
    $('applyReviewBtn').disabled = true;
    $('reviewLog').textContent = '正在创建批量审核任务…';
    pushEmbeddedReviewState();
    try {
      const data = await postJson('/api/cross-platform/review/apply', {datasets:groups});
      reviewJob = data.job.id; reviewCursor = 0; $('reviewLog').textContent = '';
      pushEmbeddedReviewState();
      pollReviewJob();
    } catch (error) { $('reviewLog').textContent = `启动失败：${error}`; renderPendingSummary(); }
  }

  async function pollReviewJob() {
    if (!reviewJob) return;
    try {
      const response = await fetch(`/api/jobs/${encodeURIComponent(reviewJob)}?cursor=${reviewCursor}`);
      const job = await response.json();
      if (!response.ok || job.error) throw new Error(job.error || response.statusText);
      if (job.log_truncated) $('reviewLog').textContent = '';
      if (job.log?.length) $('reviewLog').textContent += `${job.log.join('\n')}\n`;
      $('reviewLog').scrollTop = $('reviewLog').scrollHeight;
      pushEmbeddedReviewState();
      if (Number.isFinite(Number(job.log_cursor))) reviewCursor = Number(job.log_cursor);
      if (job.status === 'running' || job.status === 'queued') reviewTimer = setTimeout(pollReviewJob, 1000);
      else {
        $('reviewLog').textContent += `任务结束：${job.status}\n`;
        if (job.status === 'completed') pending.clear();
        reviewJob = null; renderPendingSummary(); renderEpisodes();
      }
    } catch (error) { $('reviewLog').textContent += `日志刷新失败：${error}\n`; pushEmbeddedReviewState(); reviewTimer = setTimeout(pollReviewJob, 1800); }
  }

  function populateEpisodeFilters() {
    const groups = new Map();
    for (const item of report.datasets) groups.set(item.logical_id || item.id, item);
    $('episodeDataset').innerHTML = `<option value="all">全部</option>${[...groups].map(([id,item]) => `<option value="${id}">${escapeHtml(item.name)} · ${platformLabel(item.platform)}</option>`).join('')}`;
  }

  function renderReport() {
    $('reportEmpty').classList.add('hidden');
    $('reportContent').classList.remove('hidden');
    renderKpis(); renderQcCheckCards(); renderQcResults(); populateEpisodeFilters(); renderEpisodes(); renderPendingSummary(); setTab(activeTab);
  }

  function setTab(name) {
    activeTab = name === 'visual' ? 'visual' : 'qc';
    document.querySelectorAll('.tab').forEach(button => button.classList.toggle('active', button.dataset.tab === activeTab));
    $('qcPanel').classList.toggle('hidden', activeTab !== 'qc');
    $('visualPanel').classList.toggle('hidden', activeTab !== 'visual');
    $('sidebarMode').textContent = activeTab === 'visual' ? '可视化回放' : 'LeRobot 质检';
  }

  function resetRobotReport(clearDatasets = true) {
    report = null;
    pending.clear();
    selectedEpisodeKey = '';
    replayUrls.clear();
    $('replayFrame').removeAttribute('src');
    $('reportContent').classList.add('hidden');
    $('reportEmpty').classList.remove('hidden');
    $('globalStatus').textContent = '待质检';
    $('globalStatus').className = 'pill';
    const zerith = $('qcRobotType').value === 'zerith';
    $('zerithThresholdControl').classList.toggle('hidden', !zerith);
    $('robotRuleHint').textContent = zerith
      ? '零次方：Action / State 各按 8 个部位展示完整 23D；腰、头各 2 维，升降单位 m。其他检查分项汇总，不检查视频完整性。'
      : '松灵：State 21D / Action 18D，升降柱 200±0.5 mm。';
    if (clearDatasets) { datasets = []; renderDatasets(); }
    $('sourceHint').textContent = clearDatasets ? '机器人已切换，请读取所需数据集后质检。' : '静止帧阈值已改变，请重新执行质检。';
  }
  $('qcRobotType').addEventListener('change', () => resetRobotReport());
  $('qcStationaryThreshold').addEventListener('change', () => resetRobotReport(false));
  $('discoverBtn').addEventListener('click', discover);
  $('analyzeBtn').addEventListener('click', analyze);
  $('datasetSearch').addEventListener('input', renderDatasets);
  document.querySelectorAll('.tab').forEach(button => button.addEventListener('click', () => {
    setTab(button.dataset.tab);
    if (button.dataset.tab === 'visual' && selectedEpisodeKey && !$('replayFrame').getAttribute('src')) startReplay(selectedEpisodeKey);
  }));
  ['qcResultCategory','qcResultStatus'].forEach(id => $(id).addEventListener('change', renderQcResults));
  $('qcResultSearch').addEventListener('input', renderQcResults);
  $('episodeDataset').addEventListener('change', renderEpisodes);
  $('visualEpisodeSelect').addEventListener('change', () => {
    selectedEpisodeKey = $('visualEpisodeSelect').value;
    renderSelectedEpisode();
    $('prevVisualEpisodeBtn').disabled = $('visualEpisodeSelect').selectedIndex <= 0;
    $('nextVisualEpisodeBtn').disabled = $('visualEpisodeSelect').selectedIndex >= $('visualEpisodeSelect').options.length - 1;
    if (selectedEpisodeKey) startReplay(selectedEpisodeKey);
  });
  $('prevVisualEpisodeBtn').addEventListener('click', () => moveVisualEpisode(-1));
  $('nextVisualEpisodeBtn').addEventListener('click', () => moveVisualEpisode(1));
  $('openReplayBtn').addEventListener('click', () => selectedEpisodeKey && startReplay(selectedEpisodeKey));
  $('visualReviewGrade').addEventListener('change', updatePendingFromVisual);
  $('visualExclude').addEventListener('change', updatePendingFromVisual);
  $('visualReason').addEventListener('input', updatePendingFromVisual);
  $('clearReviewBtn').addEventListener('click', () => { pending.clear(); renderPendingSummary(); renderEpisodes(); });
  $('applyReviewBtn').addEventListener('click', applyReview);
  $('replayFrame').addEventListener('load', pushEmbeddedReviewState);
  discover();
  window.addEventListener('message', event => {
    if (event.origin !== location.origin || event.source !== $('replayFrame').contentWindow) return;
    const message = event.data;
    if (!message || message.type !== 'lerobot-review-event') return;
    if (message.action === 'ready') {
      pushEmbeddedReviewState();
      return;
    }
    if (message.action === 'dataset') {
      const value = String(message.dataset_id || 'all');
      if ([...$('episodeDataset').options].some(option => option.value === value)) $('episodeDataset').value = value;
      renderEpisodes();
      if (selectedEpisodeKey) startReplay(selectedEpisodeKey);
      return;
    }
    if (message.action === 'select') {
      const key = String(message.key || '');
      if (report?.episodes.some(item => item.key === key)) startReplay(key);
      return;
    }
    if (message.action === 'change') {
      $('visualReviewGrade').value = String(message.quality_grade || '');
      $('visualExclude').checked = Boolean(message.exclude);
      $('visualReason').value = String(message.reason || '');
      updatePendingFromVisual();
      return;
    }
    if (message.action === 'clear') {
      pending.clear();
      renderPendingSummary();
      renderEpisodes();
      return;
    }
    if (message.action === 'apply') applyReview();
  });
</script>
</body>
</html>"""
