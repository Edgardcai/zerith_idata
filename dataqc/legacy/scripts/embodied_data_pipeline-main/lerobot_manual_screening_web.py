"""Standalone UI for the LeRobot manual screening workspace."""

MANUAL_SCREENING_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>数据人工筛查</title>
  <style>
    :root {
      --bg: #f5f7fb;
      --panel: #fff;
      --line: #dce4f0;
      --text: #17233b;
      --muted: #69788f;
      --blue: #3157ee;
      --blue-soft: #edf2ff;
      --violet: #7447e9;
      --danger: #b42318;
      --danger-soft: #fff0ee;
      --ok: #087443;
      --shadow: 0 10px 28px rgba(27, 55, 109, .08);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background:
        radial-gradient(circle at 8% 0%, rgba(49, 87, 238, .10), transparent 27rem),
        var(--bg);
      color: var(--text);
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    button, select, input { font: inherit; }
    button { cursor: pointer; }
    .page { max-width: 1760px; margin: 0 auto; padding: 18px; }
    .intro {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 20px;
      margin-bottom: 14px;
    }
    h1 { margin: 0; font-size: 20px; }
    .intro p { margin: 5px 0 0; color: var(--muted); font-size: 13px; }
    .record-file {
      max-width: 620px;
      color: var(--muted);
      font-size: 11px;
      word-break: break-all;
      text-align: right;
    }
    .intro-actions { display: flex; align-items: center; gap: 9px; }
    .records-link {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 34px;
      padding: 0 11px;
      border: 1px solid #b8c7ff;
      border-radius: 9px;
      color: #2345bd;
      background: #edf2ff;
      text-decoration: none;
      font-size: 12px;
      font-weight: 750;
      white-space: nowrap;
    }
    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 14px;
      box-shadow: var(--shadow);
    }
    .dataset-panel { padding: 15px; margin-bottom: 14px; }
    .dataset-grid {
      display: grid;
      grid-template-columns: minmax(260px, 1.5fr) minmax(220px, .7fr) auto auto;
      gap: 10px;
      align-items: end;
    }
    label { display: block; margin-bottom: 5px; color: var(--muted); font-size: 12px; }
    input, select {
      width: 100%;
      min-height: 38px;
      border: 1px solid #cbd6e6;
      border-radius: 9px;
      background: #fff;
      color: var(--text);
      padding: 8px 10px;
    }
    input[readonly] { color: #50617a; background: #f7f9fc; }
    .btn {
      min-height: 38px;
      border: 1px solid #cbd6e6;
      border-radius: 9px;
      padding: 0 14px;
      background: #fff;
      color: #42536d;
      font-weight: 650;
      white-space: nowrap;
    }
    .btn.primary {
      border-color: transparent;
      color: #fff;
      background: linear-gradient(135deg, var(--blue), var(--violet));
      box-shadow: 0 6px 14px rgba(49, 87, 238, .22);
    }
    .btn.danger { color: #fff; border-color: var(--danger); background: var(--danger); }
    .btn:disabled { opacity: .55; cursor: wait; }
    .dataset-meta {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      min-height: 27px;
      margin-top: 10px;
      align-items: center;
      color: var(--muted);
      font-size: 12px;
    }
    .pill {
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      padding: 3px 8px;
      border: 1px solid #d7e0ee;
      border-radius: 999px;
      background: #f8faff;
      color: #4b5d77;
    }
    .pill.real { color: #8f3d12; background: #fff5eb; border-color: #ffd7b0; }
    .pill.saved { color: var(--ok); background: #ecfdf3; border-color: #abefc6; }
    .status { margin-left: auto; text-align: right; }
    .status.error { color: var(--danger); }
    .progress {
      height: 5px;
      margin: 10px -15px -15px;
      border-radius: 0 0 14px 14px;
      overflow: hidden;
      background: #edf1f7;
    }
    .progress.hidden, .hidden { display: none !important; }
    .progress > div {
      height: 100%;
      width: 38%;
      border-radius: inherit;
      background: linear-gradient(90deg, var(--blue), #9d6df5, var(--blue));
      background-size: 200% 100%;
      animation: loading 1.2s linear infinite;
    }
    @keyframes loading { to { background-position: -200% 0; } }
    .workspace {
      display: grid;
      grid-template-columns: minmax(0, 1.55fr) minmax(450px, .85fr);
      gap: 14px;
      align-items: start;
    }
    .right-column {
      display: grid;
      gap: 10px;
      position: sticky;
      top: 12px;
    }
    .viewer { overflow: hidden; }
    .viewer-head {
      display: grid;
      grid-template-columns: auto minmax(220px, 1fr) auto auto auto;
      align-items: center;
      gap: 9px;
      padding: 13px 15px;
      border-bottom: 1px solid var(--line);
    }
    .viewer-head .btn { width: 38px; padding: 0; }
    .grade-pill {
      color: #6d28d9;
      background: #f3e8ff;
      border-color: #d8b4fe;
      font-weight: 750;
    }
    .viewer-body { padding: 15px; }
    .viewer-controls { display: flex; align-items: center; gap: 7px; flex-wrap: wrap; }
    .grasp-tabs {
      display: inline-flex;
      gap: 3px;
      padding: 3px;
      margin-bottom: 12px;
      border: 1px solid #cfd9ff;
      border-radius: 10px;
      background: #f4f7ff;
    }
    .grasp-btn {
      min-height: 34px;
      padding: 0 12px;
      border: 0;
      border-radius: 7px;
      color: #53627a;
      background: transparent;
      font-weight: 750;
    }
    .grasp-btn.active { color: #fff; background: var(--blue); }
    .moment-tabs {
      display: inline-flex;
      gap: 4px;
      padding: 4px;
      margin-bottom: 12px;
      background: #eef2f8;
      border-radius: 10px;
    }
    .moment-btn {
      min-height: 34px;
      padding: 0 14px;
      border: 0;
      border-radius: 7px;
      background: transparent;
      color: #596980;
      font-weight: 650;
    }
    .moment-btn.active { background: #fff; color: var(--blue); box-shadow: 0 2px 8px rgba(37, 63, 108, .11); }
    .frame-note { margin-left: 7px; color: var(--muted); font-size: 12px; }
    .image-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
    }
    figure {
      margin: 0;
      min-width: 0;
      border: 1px solid var(--line);
      border-radius: 11px;
      overflow: hidden;
      background: #101828;
    }
    figure img {
      display: block;
      width: 100%;
      aspect-ratio: 1 / 1;
      object-fit: contain;
      background: #0d1524;
    }
    figcaption { padding: 7px 9px; background: #fff; color: #526179; font-size: 12px; }
    .empty-image {
      display: grid;
      place-items: center;
      min-height: 260px;
      border: 1px dashed #cbd6e6;
      border-radius: 11px;
      color: var(--muted);
      background: #f9fbfe;
    }
    .prompt-inspector {
      overflow: hidden;
      border-color: #aebeff;
      box-shadow: 0 13px 32px rgba(49, 87, 238, .15);
    }
    .prompt-inspector::before {
      content: "";
      display: block;
      height: 5px;
      background: linear-gradient(90deg, var(--blue), var(--violet));
    }
    .prompt-inspector-body { padding: 12px; }
    .prompt-heading {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 8px;
    }
    .prompt-heading h2 { margin: 0; font-size: 17px; }
    .prompt-heading span {
      padding: 3px 8px;
      border-radius: 999px;
      color: var(--blue);
      background: var(--blue-soft);
      font-size: 11px;
      font-weight: 700;
    }
    .prompt-label { color: #53627a; font-size: 11px; font-weight: 700; letter-spacing: .04em; }
    .prompt-text {
      margin-top: 5px;
      max-height: 74px;
      padding: 9px;
      overflow: auto;
      border: 1px solid #cfd9ff;
      border-radius: 10px;
      color: #16295e;
      background: linear-gradient(135deg, #f4f7ff, #fbf8ff);
      font-size: 14px;
      font-weight: 720;
      line-height: 1.4;
      word-break: break-word;
    }
    .prompt-facts {
      display: grid;
      grid-template-columns: .8fr 1fr 1fr;
      gap: 6px;
      margin-top: 7px;
    }
    .prompt-fact {
      min-width: 0;
      padding: 8px;
      border: 1px solid #dce4f0;
      border-radius: 9px;
      background: #f9fbff;
    }
    .fact-label { display: block; margin-bottom: 4px; color: var(--muted); font-size: 11px; }
    .fact-value {
      display: block;
      color: #243a60;
      font-size: 14px;
      font-weight: 750;
      line-height: 1.4;
      word-break: break-word;
    }
    .hand-badge {
      display: inline-flex;
      width: fit-content;
      padding: 5px 11px;
      border: 1px solid #b8c7ff;
      border-radius: 999px;
      color: #153fc2;
      background: #e9efff;
    }
    .hand-badge.right { color: #7b2cbf; background: #f5ebff; border-color: #dcc1f6; }
    .source-line {
      max-height: 42px;
      margin-top: 7px;
      padding-top: 6px;
      overflow: auto;
      border-top: 1px dashed #d6deeb;
      color: #5d6d84;
      font-size: 11px;
      line-height: 1.4;
    }
    .yolo-inspector {
      padding: 10px;
      border-color: #d2dbea;
    }
    .yolo-heading {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 9px;
      margin-bottom: 6px;
    }
    .yolo-heading h2 { margin: 0; font-size: 16px; }
    .yolo-heading .btn { min-height: 30px; padding: 0 9px; font-size: 11px; }
    .yolo-tools { display: flex; align-items: center; gap: 5px; }
    .yolo-filter {
      width: 142px;
      min-height: 30px;
      padding: 4px 7px;
      border-radius: 8px;
      font-size: 11px;
    }
    .yolo-status {
      padding: 7px 9px;
      border: 1px solid #d9e1ee;
      border-radius: 10px;
      color: #53627a;
      background: #f7f9fc;
      font-size: 13px;
      font-weight: 720;
      line-height: 1.35;
    }
    .yolo-status.loading {
      color: #2647aa;
      border-color: #b9c8f7;
      background: #edf2ff;
    }
    .yolo-status.correct {
      color: #067647;
      border-color: #86d3ad;
      background: #ecfdf3;
    }
    .yolo-status.incorrect,
    .yolo-status.error {
      color: var(--danger);
      border-color: #f1aaa3;
      background: var(--danger-soft);
    }
    .yolo-rule { margin: 5px 1px 0; color: var(--muted); font-size: 10px; line-height: 1.35; }
    .yolo-moments {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 5px;
      margin-top: 6px;
    }
    .yolo-moment {
      min-width: 0;
      padding: 6px 7px;
      border: 1px solid #dce4f0;
      border-left-width: 3px;
      border-radius: 9px;
      background: #fbfcfe;
    }
    .yolo-moment.matched { border-left-color: #12a66a; }
    .yolo-moment.unmatched { border-left-color: #d92d20; }
    .yolo-moment-head {
      display: flex;
      justify-content: space-between;
      gap: 4px;
      color: #34445d;
      font-size: 11px;
      font-weight: 750;
    }
    .yolo-moment-result {
      margin-top: 3px;
      overflow: hidden;
      color: #617087;
      font-size: 10px;
      line-height: 1.35;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .yolo-match-text { color: #087443; }
    .yolo-miss-text { color: var(--danger); }
    .yolo-model { margin-top: 5px; color: #8793a5; font-size: 9px; word-break: break-all; }
    .warnings {
      margin-top: 10px;
      padding: 9px 11px;
      border-radius: 9px;
      color: #9a4b09;
      background: #fff8e8;
      border: 1px solid #f8d893;
      font-size: 12px;
      line-height: 1.55;
    }
    .review { padding: 12px; }
    .review h2 { margin: 0 0 5px; font-size: 16px; }
    .review-lead { margin: 0 0 9px; color: var(--muted); font-size: 11px; line-height: 1.4; }
    .error-choice {
      display: flex;
      align-items: center;
      gap: 8px;
      min-height: 36px;
      margin: 5px 0;
      padding: 7px 9px;
      border: 1px solid #d8e0ec;
      border-radius: 9px;
      color: #32435e;
      cursor: pointer;
    }
    .error-choice input { width: 17px; min-height: 17px; margin: 0; }
    .correction { margin-top: 7px; }
    .bimanual-object-correction { display: grid; grid-template-columns: 1fr 1fr; gap: 7px; }
    .bimanual-object-correction label { margin-bottom: 3px; }
    .review-actions { display: grid; grid-template-columns: minmax(0,1fr) auto; gap: 7px; margin-top: 9px; }
    .review-actions .btn { width: 100%; }
    .save-status { min-height: 26px; margin-top: 7px; color: var(--muted); font-size: 11px; line-height: 1.4; }
    .save-status.ok { color: var(--ok); }
    .save-status.error { color: var(--danger); }
    .empty-state {
      padding: 70px 20px;
      text-align: center;
      color: var(--muted);
    }
    @media (max-width: 1050px) {
      .dataset-grid { grid-template-columns: 1fr 1fr; }
      .workspace { grid-template-columns: 1fr; }
      .right-column { position: static; }
    }
    @media (max-width: 700px) {
      .page { padding: 10px; }
      .intro { flex-direction: column; }
      .intro-actions { width: 100%; justify-content: space-between; }
      .record-file { text-align: left; }
      .dataset-grid { grid-template-columns: 1fr; }
      .viewer-head { grid-template-columns: auto minmax(0, 1fr) auto; }
      .episode-count { grid-column: 1 / -1; }
      .image-grid { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class="page">
    <div class="intro">
      <div>
        <h1>数据人工筛查</h1>
        <p>按 Prompt 自动进入单手或双手质检，分别核对夹爪闭合前后 40 帧、三个视角及目标物品。</p>
      </div>
      <div class="intro-actions">
        <div id="recordFile" class="record-file"></div>
        <a class="records-link" href="/manual-screening-records/">查看全部问题记录</a>
      </div>
    </div>

    <section class="panel dataset-panel">
      <div class="dataset-grid">
        <div>
          <label for="datasetSelect">LeRobot 数据集（递归扫描）</label>
          <select id="datasetSelect"><option value="">正在扫描...</option></select>
        </div>
        <div>
          <label for="scanRoot">默认扫描目录</label>
          <input id="scanRoot" readonly value="/srv/data/datasets/public/stage2_datasets" />
        </div>
        <button id="refreshBtn" class="btn" type="button">重新扫描</button>
        <button id="extractBtn" class="btn primary" type="button">截取图片并识别</button>
      </div>
      <div id="datasetMeta" class="dataset-meta"></div>
      <div id="progress" class="progress hidden"><div></div></div>
    </section>

    <div id="emptyState" class="panel empty-state">选择数据集后点击“截取图片并识别”，系统才会处理该目录下的全部 episode。</div>
    <div id="workspace" class="workspace hidden">
      <section class="panel viewer">
        <div class="viewer-head">
          <button id="prevBtn" class="btn" type="button" aria-label="上一个 episode">‹</button>
          <select id="episodeSelect"></select>
          <button id="nextBtn" class="btn" type="button" aria-label="下一个 episode">›</button>
          <span id="episodeGrade" class="pill grade-pill hidden"></span>
          <span id="episodeCount" class="episode-count pill"></span>
        </div>
        <div class="viewer-body">
          <div class="viewer-controls">
            <span id="graspTabs" class="grasp-tabs hidden">
              <button class="grasp-btn active" data-grasp-arm="left" type="button">左手抓取</button>
              <button class="grasp-btn" data-grasp-arm="right" type="button">右手抓取</button>
            </span>
            <span class="moment-tabs">
              <button class="moment-btn" data-moment="before" type="button">前 40 帧</button>
              <button class="moment-btn active" data-moment="close" type="button">夹取时刻</button>
              <button class="moment-btn" data-moment="after" type="button">后 40 帧</button>
            </span>
            <span id="frameNote" class="frame-note"></span>
          </div>
          <div id="imageGrid" class="image-grid"></div>
          <div id="warnings" class="warnings hidden"></div>
        </div>
      </section>

      <div class="right-column">
        <section class="panel prompt-inspector">
          <div class="prompt-inspector-body">
            <div class="prompt-heading">
              <h2>Prompt 核对信息</h2>
              <span>快速筛选</span>
            </div>
            <div class="prompt-label">完整提示词</div>
            <div id="promptText" class="prompt-text"></div>
            <div class="prompt-facts">
              <div class="prompt-fact hand">
                <span class="fact-label">左右手名称</span>
                <strong id="promptHand" class="fact-value hand-badge"></strong>
              </div>
              <div class="prompt-fact">
                <span class="fact-label">物品英文名</span>
                <strong id="promptObjectEn" class="fact-value"></strong>
              </div>
              <div class="prompt-fact">
                <span class="fact-label">物品中文名</span>
                <strong id="promptObjectZh" class="fact-value"></strong>
              </div>
            </div>
            <div id="sourceLine" class="source-line"></div>
          </div>
        </section>

        <section class="panel yolo-inspector">
          <div class="yolo-heading">
            <h2>YOLO 物品自动识别</h2>
            <div class="yolo-tools">
              <select id="yoloFilter" class="yolo-filter" aria-label="YOLO Episode 筛选">
                <option value="all">全部 Episode</option>
                <option value="warning" disabled>仅 YOLO 预警</option>
              </select>
              <button id="retryYoloBtn" class="btn" type="button">重新识别</button>
            </div>
          </div>
          <div id="yoloStatus" class="yolo-status">截帧后将自动识别全部 episode</div>
          <div id="yoloRule" class="yolo-rule">每只抓取手分别检查前 40 帧、闭合帧、后 40 帧；每只手至少 2 个时刻匹配才判定正确。</div>
          <div id="yoloMoments" class="yolo-moments"></div>
          <div id="yoloModel" class="yolo-model"></div>
        </section>

        <aside class="panel review">
          <h2>错误记录</h2>
          <p class="review-lead">仅在发现 Prompt 中操作手或物品有误时记录。相同数据集、相同 episode 再次保存会更新原记录。</p>
          <label class="error-choice">
            <input id="wrongArm" type="checkbox" />
            <span>左右手错误</span>
          </label>
          <div id="armCorrection" class="correction hidden">
            <label for="correctArm">正确的操作手</label>
            <select id="correctArm">
              <option value="">请选择</option>
              <option value="left">左手（left hand）</option>
              <option value="right">右手（right hand）</option>
              <option value="both">双手（left + right）</option>
            </select>
          </div>

          <label class="error-choice">
            <input id="wrongObject" type="checkbox" />
            <span>物品错误</span>
          </label>
          <div id="objectCorrection" class="correction hidden">
            <div id="singleObjectCorrection">
              <label for="correctObject">正确的物品</label>
              <input id="correctObject" list="objectOptions" autocomplete="off" placeholder="请选择或输入正确物品" />
            </div>
            <div id="bimanualObjectCorrection" class="bimanual-object-correction hidden">
              <div>
                <label for="correctLeftObject">左手正确物品</label>
                <input id="correctLeftObject" list="objectOptions" autocomplete="off" placeholder="左手实际应抓物品" />
              </div>
              <div>
                <label for="correctRightObject">右手正确物品</label>
                <input id="correctRightObject" list="objectOptions" autocomplete="off" placeholder="右手实际应抓物品" />
              </div>
            </div>
            <datalist id="objectOptions"></datalist>
          </div>

          <div class="review-actions">
            <button id="saveBtn" class="btn primary" type="button">记录这个 episode</button>
            <button id="deleteRecordBtn" class="btn danger" type="button" disabled>删除当前记录</button>
          </div>
          <div id="saveStatus" class="save-status"></div>
        </aside>
      </div>
    </div>
  </div>

  <script>
    let datasets = [];
    let manifest = null;
    let records = new Map();
    let yoloReport = null;
    let yoloResults = new Map();
    let episodeFilter = "all";
    let activeGraspArm = "left";
    let activeMoment = "close";
    let yoloRequestToken = 0;

    const $ = id => document.getElementById(id);
    const escapeHtml = value => String(value ?? "")
      .replaceAll("&", "&amp;").replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#39;");

    async function fetchJson(url, options, retries = 2) {
      for (let attempt = 0; ; attempt += 1) {
        try {
          const response = await fetch(url, options);
          const data = await response.json().catch(() => ({}));
          if (!response.ok || data.error) throw new Error(data.error || `请求失败 (${response.status})`);
          return data;
        } catch (error) {
          const transient = error instanceof TypeError || /Failed to fetch|NetworkError/i.test(String(error));
          if (!transient || attempt >= retries) {
            if (transient) throw new Error("网络连接暂时中断，自动重试后仍未恢复；后台任务可能已经启动，请稍后刷新查看");
            throw error;
          }
          await new Promise(resolve => setTimeout(resolve, 600 * (attempt + 1)));
        }
      }
    }

    async function postJson(url, payload) {
      return fetchJson(url, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(payload),
      });
    }

    function selectedDataset() {
      return datasets.find(item => item.id === $("datasetSelect").value) || null;
    }

    function recordKey(item) {
      if (!item) return "";
      return `${item.dataset_id || item.dataset_path || ""}:${Number(item.episode_index)}`;
    }

    function activeEpisode() {
      if (!manifest) return null;
      const key = $("episodeSelect").value;
      return manifest.episodes.find(item => item.episode_key === key) || null;
    }

    function episodeGraspArms(episode) {
      const grasps = episode?.grasps || {};
      const arms = ["left", "right"].filter(arm => grasps[arm]?.frames);
      if (arms.length) return arms;
      const legacyArm = episode?.closure?.arm || episode?.original?.arm;
      return ["left", "right"].includes(legacyArm) ? [legacyArm] : [];
    }

    function activeGrasp(episode) {
      const arms = episodeGraspArms(episode);
      const displayArm = arms.includes(activeGraspArm) ? activeGraspArm : (arms[0] || "left");
      const grasp = episode?.grasps?.[displayArm];
      if (grasp) return grasp;
      return {arm: displayArm, closure: episode?.closure || {}, frames: episode?.frames || {}};
    }

    function syncGraspTabs(episode) {
      const arms = episodeGraspArms(episode);
      const bimanual = arms.length === 2;
      const displayArm = arms.includes(activeGraspArm) ? activeGraspArm : arms[0];
      $("graspTabs").classList.toggle("hidden", !bimanual);
      document.querySelectorAll(".grasp-btn").forEach(button => {
        button.classList.toggle("active", button.dataset.graspArm === displayArm);
      });
    }

    function filteredEpisodes() {
      const episodes = manifest?.episodes || [];
      if (episodeFilter !== "warning") return episodes;
      return episodes.filter(episode => {
        const result = yoloResults.get(episode.episode_key);
        return result && result.status !== "correct";
      });
    }

    function updateYoloFilter() {
      const total = manifest?.episodes?.length || 0;
      const warningCount = Array.from(yoloResults.values()).filter(item => item?.status !== "correct").length;
      $("yoloFilter").innerHTML = `
        <option value="all">全部 Episode (${total})</option>
        <option value="warning" ${warningCount ? "" : "disabled"}>仅 YOLO 预警 (${warningCount})</option>`;
      if (episodeFilter === "warning" && !warningCount) episodeFilter = "all";
      $("yoloFilter").value = episodeFilter;
    }

    function refreshEpisodeOptions(preferredKey = "") {
      const episodes = filteredEpisodes();
      $("episodeSelect").innerHTML = episodes.map(episode => {
        const ordinal = (manifest?.episodes || []).indexOf(episode) + 1;
        const frameWarning = (episode.warnings || []).length ? " 截帧⚠" : "";
        const yolo = yoloResults.get(episode.episode_key);
        const yoloMark = yolo ? (yolo.status === "correct" ? " ✓YOLO" : " ⚠YOLO") : "";
        const saved = records.has(recordKey(episode)) ? " ✓已记录" : "";
        const grade = episode.source_grade ? `[${episode.source_grade}] ` : "";
        return `<option value="${escapeHtml(episode.episode_key)}">${ordinal}. ${grade}${escapeHtml(episode.episode_name)}${frameWarning}${yoloMark}${saved}</option>`;
      }).join("");
      if (preferredKey && episodes.some(item => item.episode_key === preferredKey)) {
        $("episodeSelect").value = preferredKey;
      }
    }

    function setStatus(message, error = false) {
      let node = $("datasetMeta").querySelector(".status");
      if (!node) {
        node = document.createElement("span");
        node.className = "status";
        $("datasetMeta").append(node);
      }
      node.classList.toggle("error", error);
      node.textContent = message;
    }

    function renderDatasetMeta(message = "") {
      const item = selectedDataset();
      if (!item) {
        $("datasetMeta").innerHTML = `<span>${escapeHtml(message || "没有选择数据集")}</span>`;
        return;
      }
      const cameras = (item.cameras || []).map(camera => camera.label).join(" / ") || "无视频视角";
      const gradeLabel = item.grade_label ? `${item.grade_label} 合并` : "未分级";
      $("datasetMeta").innerHTML = `
        <span class="pill real">真机</span>
        <span class="pill grade-pill">${escapeHtml(gradeLabel)}</span>
        <span class="pill">${item.physical_dataset_count} 个物理目录</span>
        <span class="pill">${item.total_episodes} 个 episode</span>
        <span class="pill">${escapeHtml(cameras)}</span>
        <span title="${escapeHtml(item.path)}">${escapeHtml(item.path)}</span>
        <span class="status">${escapeHtml(message)}</span>`;
    }

    async function discover() {
      $("refreshBtn").disabled = true;
      try {
        const data = await fetchJson("/api/manual-screening/datasets");
        datasets = data.datasets || [];
        $("scanRoot").value = data.root || $("scanRoot").value;
        $("recordFile").textContent = `统一记录文件：${data.record_file || ""}`;
        const select = $("datasetSelect");
        select.innerHTML = datasets.length
          ? datasets.map(item => {
              const grades = item.grade_label ? `${item.grade_label} 合并` : "未分级";
              return `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)} · ${escapeHtml(grades)} · ${item.total_episodes} episodes · ${escapeHtml(item.path)}</option>`;
            }).join("")
          : '<option value="">未发现 LeRobot 数据集</option>';
        renderDatasetMeta(`递归发现 ${datasets.length} 个逻辑数据集，A/B/F 已合并显示`);
        await loadCachedResult();
      } catch (error) {
        datasets = [];
        $("datasetSelect").innerHTML = '<option value="">扫描失败</option>';
        renderDatasetMeta(String(error));
      } finally {
        $("refreshBtn").disabled = false;
      }
    }

    async function loadCachedResult() {
      yoloRequestToken += 1;
      manifest = null;
      records = new Map();
      yoloReport = null;
      yoloResults = new Map();
      episodeFilter = "all";
      $("workspace").classList.add("hidden");
      $("emptyState").classList.remove("hidden");
      renderDatasetMeta();
      const item = selectedDataset();
      if (!item) return;
      try {
        const data = await fetchJson(`/api/manual-screening/result?dataset_group_id=${encodeURIComponent(item.id)}`);
        if (data.manifest) await showManifest(data.manifest, data.records || [], data.yolo_report);
      } catch (error) {
        if (!String(error).includes("尚未截取")) setStatus(String(error), true);
      }
    }

    async function extract() {
      const item = selectedDataset();
      if (!item) return setStatus("请先选择数据集", true);
      $("extractBtn").disabled = true;
      $("refreshBtn").disabled = true;
      $("datasetSelect").disabled = true;
      $("retryYoloBtn").disabled = true;
      $("yoloFilter").disabled = true;
      $("progress").classList.remove("hidden");
      setStatus(`准备截帧并识别 ${item.total_episodes} 个 episode...`);
      try {
        const data = await postJson("/api/manual-screening/extract", {dataset_group_id: item.id});
        const jobId = data.job?.id;
        if (!jobId) throw new Error("服务端没有返回截帧任务 ID");
        if (data.reused) setStatus("该数据集已有运行中的截帧与 YOLO 任务，已自动接续进度");
        let cursor = 0;
        while (true) {
          await new Promise(resolve => setTimeout(resolve, 700));
          const job = await fetchJson(`/api/jobs/${encodeURIComponent(jobId)}?cursor=${cursor}`);
          cursor = Number(job.log_cursor || cursor);
          if (Array.isArray(job.log) && job.log.length) setStatus(job.log[job.log.length - 1]);
          if (["completed", "failed", "stopped"].includes(job.status)) {
            if (job.status !== "completed") throw new Error(`截帧及 YOLO 任务${job.status === "stopped" ? "已停止" : "失败"}`);
            break;
          }
        }
        const result = await fetchJson(`/api/manual-screening/result?dataset_group_id=${encodeURIComponent(item.id)}`);
        await showManifest(result.manifest, result.records || [], result.yolo_report);
        const yolo = result.yolo_report;
        setStatus(
          `截帧 ${result.manifest.successful_episode_count}/${result.manifest.episode_count}；` +
          `YOLO 正确 ${yolo?.correct_count || 0}、预警 ${yolo?.warning_count || 0}`
        );
      } catch (error) {
        setStatus(String(error), true);
      } finally {
        $("extractBtn").disabled = false;
        $("refreshBtn").disabled = false;
        $("datasetSelect").disabled = false;
        $("retryYoloBtn").disabled = false;
        $("yoloFilter").disabled = false;
        $("progress").classList.add("hidden");
      }
    }

    async function showManifest(value, savedRecords, savedYoloReport = null) {
      manifest = value;
      records = new Map((savedRecords || []).map(record => [recordKey(record), record]));
      yoloReport = savedYoloReport;
      yoloResults = new Map(
        (savedYoloReport?.results || []).map(result => [result.episode_key, result])
      );
      episodeFilter = "all";
      $("recordFile").textContent = `统一记录文件：${manifest.record_file || ""}`;
      const options = await fetchJson("/api/manual-screening/options").catch(() => ({objects: []}));
      $("objectOptions").innerHTML = (options.objects || [])
        .map(item => `<option value="${escapeHtml(item)}"></option>`).join("");
      updateYoloFilter();
      refreshEpisodeOptions();
      activeGraspArm = "left";
      activeMoment = "close";
      document.querySelectorAll(".moment-btn").forEach(button => button.classList.toggle("active", button.dataset.moment === "close"));
      $("emptyState").classList.add("hidden");
      $("workspace").classList.remove("hidden");
      renderEpisode();
    }

    function armLabel(value) {
      if (value === "left") return "左手（left hand）";
      if (value === "right") return "右手（right hand）";
      if (value === "both") return "双手（left + right）";
      return "未从 Prompt 识别";
    }

    function promptObjectsForGrasp(episode, arm) {
      const names = episode?.original?.objects_by_arm?.[arm];
      if (!Array.isArray(names) || !names.length) {
        return {
          names: episode?.original?.objects || [],
          namesZh: episode?.original?.objects_zh || [],
        };
      }
      const details = episode?.original?.object_details || [];
      return {
        names,
        namesZh: names.map(name => details.find(item => item?.name === name)?.name_zh || "未提供中文名"),
      };
    }

    function renderEpisode() {
      const episode = activeEpisode();
      if (!episode || !manifest) return;
      const ordinal = manifest.episodes.indexOf(episode) + 1;
      const visibleEpisodes = filteredEpisodes();
      const visibleOrdinal = visibleEpisodes.findIndex(item => item.episode_key === episode.episode_key) + 1;
      $("episodeCount").textContent = episodeFilter === "warning"
        ? `预警 ${visibleOrdinal} / ${visibleEpisodes.length} · 总 ${manifest.episodes.length}`
        : `${ordinal} / ${manifest.episodes.length}`;
      $("episodeGrade").textContent = episode.source_grade ? `${episode.source_grade} 等级` : "";
      $("episodeGrade").classList.toggle("hidden", !episode.source_grade);
      $("prevBtn").disabled = visibleOrdinal <= 1;
      $("nextBtn").disabled = visibleOrdinal >= visibleEpisodes.length;
      const grasp = activeGrasp(episode);
      syncGraspTabs(episode);
      const frame = grasp.frames?.[activeMoment] || {};
      $("frameNote").textContent = Number.isFinite(Number(frame.frame_index)) ? `第 ${frame.frame_index} 帧` : "无可用帧";
      const images = frame.images || [];
      $("imageGrid").innerHTML = images.length
        ? images.map(image => `<figure><img src="${escapeHtml(image.url)}?v=${encodeURIComponent(manifest.generated_at || "")}" alt="${escapeHtml(image.camera_label)}" /><figcaption>${escapeHtml(image.camera_label)}<br><small>${escapeHtml(image.camera_key)}</small></figcaption></figure>`).join("")
        : '<div class="empty-image">该时刻没有可显示的截帧，请查看下方告警。</div>';
      $("promptText").textContent = episode.prompt || "（没有读取到 Prompt）";
      const promptObjects = promptObjectsForGrasp(episode, grasp.arm);
      const originalObjects = promptObjects.names.join("、") || "未识别";
      const originalObjectsZh = promptObjects.namesZh.join("、") || "未提供中文名";
      const isBimanual = episodeGraspArms(episode).length === 2;
      $("promptHand").textContent = isBimanual
        ? `双手任务 · 当前${grasp.arm === "left" ? "左手" : "右手"}`
        : armLabel(episode.original?.arm);
      $("promptHand").className = `fact-value hand-badge ${grasp.arm === "right" ? "right" : ""}`;
      $("promptObjectEn").textContent = originalObjects;
      $("promptObjectZh").textContent = originalObjectsZh;
      const detectedArm = grasp.closure?.arm ? armLabel(grasp.closure.arm) : "未定位";
      const closureFrame = Number.isFinite(Number(grasp.closure?.frame_index)) ? grasp.closure.frame_index : "-";
      const gradeText = episode.source_grade ? `${episode.source_grade} 等级；` : "";
      const modeText = isBimanual ? "双手质检；" : "单手质检；";
      $("sourceLine").textContent = `${gradeText}${modeText}当前动作：${detectedArm}，闭合帧 ${closureFrame}；物理目录：${episode.dataset_path || ""}`;
      const warnings = episode.warnings || [];
      $("warnings").classList.toggle("hidden", !warnings.length);
      $("warnings").innerHTML = warnings.map(item => `• ${escapeHtml(item)}`).join("<br>");
      renderSavedRecord();
      renderStoredYoloResult();
    }

    function detectionText(moment) {
      const detections = Array.isArray(moment?.detections) ? moment.detections : [];
      if (!detections.length) return "未识别到超过类别阈值的商品";
      return detections.slice(0, 6).map(item => {
        const confidence = Number.isFinite(Number(item.confidence)) ? `${(Number(item.confidence) * 100).toFixed(1)}%` : "";
        return `${item.class_name || "未知类别"} ${confidence}`.trim();
      }).join("；");
    }

    function renderYoloResult(result) {
      const graspResult = (result?.grasp_results || []).find(item => item?.arm === activeGraspArm) || result;
      const correct = graspResult?.status === "correct";
      const resultClass = graspResult?.status === "error" ? "error" : (correct ? "correct" : "incorrect");
      $("yoloStatus").className = `yolo-status ${resultClass}`;
      const side = graspResult?.arm === "right" ? "右手" : "左手";
      const overall = result?.grasp_mode === "bimanual"
        ? `；双手总体${result.status === "correct" ? "正确" : "预警"}`
        : "";
      $("yoloStatus").textContent = `${result?.grasp_mode === "bimanual" ? `${side}：` : ""}${graspResult?.message || (correct ? "物品正确" : "物品不正确")}${overall}`;
      const expected = (graspResult?.expected_objects || []).join("、") || "未识别目标物品";
      $("yoloRule").textContent = `目标：${expected}；${graspResult?.camera_label || "腕部视角"}；规则：当前手 3 个时刻至少 ${graspResult?.required_matches || 2} 个匹配。`;
      $("yoloMoments").innerHTML = (graspResult?.moments || []).map(moment => `
        <div class="yolo-moment ${moment.matched ? "matched" : "unmatched"}">
          <div class="yolo-moment-head">
            <span>${escapeHtml(moment.label || moment.moment)} · 第 ${escapeHtml(moment.frame_index)} 帧</span>
            <span class="${moment.matched ? "yolo-match-text" : "yolo-miss-text"}">${moment.matched ? "匹配" : "未匹配"}</span>
          </div>
          <div class="yolo-moment-result" title="${escapeHtml(detectionText(moment))}">${escapeHtml(detectionText(moment))}</div>
        </div>`).join("");
      const latency = Number.isFinite(Number(result?.latency_ms)) ? `${Number(result.latency_ms).toFixed(1)} ms` : "";
      const summary = yoloReport
        ? ` · 全量：正确 ${yoloReport.correct_count || 0} / 预警 ${yoloReport.warning_count || 0}`
        : "";
      $("yoloModel").textContent = `模型：${result?.model?.release || "v34"} · GPU ${result?.model?.device ?? "0"}${latency ? ` · ${latency}` : ""}${summary}`;
    }

    function renderStoredYoloResult() {
      const episode = activeEpisode();
      const result = episode ? yoloResults.get(episode.episode_key) : null;
      if (result) {
        renderYoloResult(result);
        return;
      }
      $("yoloStatus").className = "yolo-status";
      $("yoloStatus").textContent = "当前 episode 尚无 YOLO 批量识别结果";
      $("yoloRule").textContent = "重新点击“截取图片并识别”可对全部 episode 执行检测。";
      $("yoloMoments").innerHTML = "";
      $("yoloModel").textContent = "";
    }

    function updateYoloReportFromResults() {
      const values = Array.from(yoloResults.values());
      yoloReport = {
        ...(yoloReport || {}),
        results: values,
        episode_count: values.length,
        correct_count: values.filter(item => item?.status === "correct").length,
        warning_count: values.filter(item => item?.status !== "correct").length,
        error_count: values.filter(item => item?.status === "error").length,
      };
    }

    async function runYoloDetection() {
      const item = selectedDataset();
      const episode = activeEpisode();
      if (!item || !episode) return;
      const token = ++yoloRequestToken;
      $("retryYoloBtn").disabled = true;
      $("yoloStatus").className = "yolo-status loading";
      $("yoloStatus").textContent = `正在识别 ${episode.source_grade ? `${episode.source_grade} · ` : ""}${episode.episode_name} 的抓取物品...`;
      $("yoloRule").textContent = "正在调用 v34 YOLO，检测抓取手腕部相机的三个时刻。";
      $("yoloMoments").innerHTML = "";
      $("yoloModel").textContent = "";
      try {
        const result = await postJson("/api/manual-screening/yolo-detect", {
          dataset_group_id: item.id,
          episode_key: episode.episode_key,
        });
        if (token !== yoloRequestToken) return;
        yoloResults.set(episode.episode_key, result);
        updateYoloReportFromResults();
        updateYoloFilter();
        refreshEpisodeOptions(episode.episode_key);
        renderEpisode();
      } catch (error) {
        if (token !== yoloRequestToken) return;
        $("yoloStatus").className = "yolo-status error";
        $("yoloStatus").textContent = `YOLO 识别失败：${String(error)}`;
        $("yoloRule").textContent = "可点击“重新识别”；识别失败不代表物品错误。";
        $("yoloMoments").innerHTML = "";
      } finally {
        if (token === yoloRequestToken) $("retryYoloBtn").disabled = false;
      }
    }

    function renderSavedRecord() {
      const episode = activeEpisode();
      const record = records.get(recordKey(episode));
      const types = new Set(record?.error_types || []);
      $("wrongArm").checked = types.has("wrong_arm");
      $("wrongObject").checked = types.has("wrong_object");
      $("correctArm").value = record?.corrections?.arm || "";
      $("correctObject").value = record?.corrections?.object || "";
      $("correctLeftObject").value = record?.corrections?.objects_by_arm?.left || "";
      $("correctRightObject").value = record?.corrections?.objects_by_arm?.right || "";
      syncCorrectionVisibility();
      $("saveStatus").className = `save-status${record ? " ok" : ""}`;
      $("deleteRecordBtn").disabled = !record;
      $("saveStatus").textContent = record
        ? `已记录，最后更新：${record.updated_at || ""}`
        : "当前 episode 尚未记录错误。";
    }

    function syncCorrectionVisibility() {
      const isBimanual = episodeGraspArms(activeEpisode()).length === 2;
      $("armCorrection").classList.toggle("hidden", !$("wrongArm").checked);
      $("objectCorrection").classList.toggle("hidden", !$("wrongObject").checked);
      $("singleObjectCorrection").classList.toggle("hidden", isBimanual);
      $("bimanualObjectCorrection").classList.toggle("hidden", !isBimanual);
    }

    async function saveRecord() {
      const item = selectedDataset();
      const episode = activeEpisode();
      if (!item || !episode) return;
      const errorTypes = [];
      if ($("wrongArm").checked) errorTypes.push("wrong_arm");
      if ($("wrongObject").checked) errorTypes.push("wrong_object");
      $("saveBtn").disabled = true;
      try {
        const data = await postJson("/api/manual-screening/records", {
          dataset_path: episode.dataset_path,
          episode_index: episode.episode_index,
          error_types: errorTypes,
          corrections: {
            arm: $("correctArm").value,
            object: $("correctObject").value.trim(),
            objects_by_arm: {
              left: $("correctLeftObject").value.trim(),
              right: $("correctRightObject").value.trim(),
            },
          },
        });
        records.set(recordKey(data.record), data.record);
        $("deleteRecordBtn").disabled = false;
        $("recordFile").textContent = `统一记录文件：${data.record_file || ""}`;
        const option = $("episodeSelect").selectedOptions[0];
        if (option && !option.textContent.includes("✓已记录")) option.textContent += " ✓已记录";
        $("saveStatus").className = "save-status ok";
        $("saveStatus").textContent = "已保存：数据集目录、episode、原信息和更正信息均已写入统一 JSON。";
      } catch (error) {
        $("saveStatus").className = "save-status error";
        $("saveStatus").textContent = String(error);
      } finally {
        $("saveBtn").disabled = false;
      }
    }

    async function deleteCurrentRecord() {
      const episode = activeEpisode();
      const record = records.get(recordKey(episode));
      if (!record || !confirm("确定删除当前 Episode 的筛查记录？只删除 JSON 记录，不会删除数据集。")) return;
      $("deleteRecordBtn").disabled = true;
      try {
        const data = await postJson("/api/manual-screening/records/delete", {record_ids: [record.record_id]});
        records.delete(recordKey(episode));
        refreshEpisodeOptions(episode.episode_key);
        renderEpisode();
        $("saveStatus").className = "save-status ok";
        $("saveStatus").textContent = `已删除 ${Number(data.deleted_count || 0)} 条记录。`;
      } catch (error) {
        $("saveStatus").className = "save-status error";
        $("saveStatus").textContent = String(error);
        $("deleteRecordBtn").disabled = false;
      }
    }

    function moveEpisode(delta) {
      const select = $("episodeSelect");
      const target = Math.max(0, Math.min(select.options.length - 1, select.selectedIndex + delta));
      select.selectedIndex = target;
      renderEpisode();
    }

    $("refreshBtn").addEventListener("click", discover);
    $("extractBtn").addEventListener("click", extract);
    $("datasetSelect").addEventListener("change", loadCachedResult);
    $("episodeSelect").addEventListener("change", renderEpisode);
    $("yoloFilter").addEventListener("change", () => {
      const currentKey = activeEpisode()?.episode_key || "";
      episodeFilter = $("yoloFilter").value;
      refreshEpisodeOptions(currentKey);
      renderEpisode();
    });
    $("prevBtn").addEventListener("click", () => moveEpisode(-1));
    $("nextBtn").addEventListener("click", () => moveEpisode(1));
    $("wrongArm").addEventListener("change", syncCorrectionVisibility);
    $("wrongObject").addEventListener("change", syncCorrectionVisibility);
    $("saveBtn").addEventListener("click", saveRecord);
    $("deleteRecordBtn").addEventListener("click", deleteCurrentRecord);
    $("retryYoloBtn").addEventListener("click", runYoloDetection);
    document.querySelectorAll(".grasp-btn").forEach(button => button.addEventListener("click", () => {
      activeGraspArm = button.dataset.graspArm;
      renderEpisode();
    }));
    document.querySelectorAll(".moment-btn").forEach(button => button.addEventListener("click", () => {
      activeMoment = button.dataset.moment;
      document.querySelectorAll(".moment-btn").forEach(item => item.classList.toggle("active", item === button));
      renderEpisode();
    }));
    discover();
  </script>
</body>
</html>
"""
