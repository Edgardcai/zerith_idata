"""Compact management page for all manual-screening issue records."""

MANUAL_SCREENING_RECORDS_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>人工筛查问题记录</title>
  <style>
    :root {
      --bg: #f5f7fb; --panel: #fff; --line: #dce4f0; --text: #17233b;
      --muted: #69788f; --blue: #3157ee; --danger: #b42318; --ok: #087443;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0; color: var(--text); background: var(--bg);
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    button, input, select { font: inherit; }
    button { cursor: pointer; }
    .page { max-width: 1880px; margin: 0 auto; padding: 14px; }
    .topbar {
      display: flex; align-items: center; justify-content: space-between; gap: 12px;
      margin-bottom: 10px;
    }
    h1 { margin: 0; font-size: 20px; }
    .subtitle { margin: 3px 0 0; color: var(--muted); font-size: 12px; }
    .actions { display: flex; align-items: center; gap: 7px; }
    .btn {
      display: inline-flex; align-items: center; justify-content: center; min-height: 34px;
      padding: 0 11px; border: 1px solid #cbd6e6; border-radius: 8px;
      color: #40516d; background: #fff; text-decoration: none; font-size: 12px; font-weight: 700;
    }
    .btn.primary { color: #fff; border-color: var(--blue); background: var(--blue); }
    .btn.danger { color: #fff; border-color: var(--danger); background: var(--danger); }
    .btn:disabled { opacity: .48; cursor: not-allowed; }
    .summary {
      display: grid; grid-template-columns: repeat(5, minmax(110px, 1fr)); gap: 8px;
      margin-bottom: 9px;
    }
    .stat { padding: 9px 11px; border: 1px solid var(--line); border-radius: 10px; background: var(--panel); }
    .stat span { display: block; color: var(--muted); font-size: 10px; }
    .stat strong { display: block; margin-top: 2px; font-size: 18px; }
    .toolbar {
      display: grid; grid-template-columns: minmax(260px, 1fr) 150px 130px 100px auto auto;
      gap: 7px; padding: 9px; border: 1px solid var(--line); border-radius: 10px 10px 0 0;
      background: var(--panel);
    }
    input, select {
      width: 100%; min-height: 34px; padding: 6px 9px; border: 1px solid #cbd6e6;
      border-radius: 8px; color: var(--text); background: #fff; font-size: 12px;
    }
    .result-count { align-self: center; color: var(--muted); text-align: right; font-size: 11px; }
    .table-wrap {
      max-height: calc(100vh - 206px); overflow: auto; border: 1px solid var(--line);
      border-top: 0; border-radius: 0 0 10px 10px; background: var(--panel);
    }
    table { width: 100%; border-collapse: collapse; table-layout: fixed; font-size: 11px; }
    th {
      position: sticky; top: 0; z-index: 2; padding: 7px 8px; border-bottom: 1px solid #cbd6e6;
      color: #43536c; background: #f2f5fa; text-align: left; white-space: nowrap;
    }
    td { padding: 6px 8px; border-bottom: 1px solid #e7ecf3; vertical-align: top; line-height: 1.35; }
    tbody tr:hover { background: #f8faff; }
    .c-num { width: 48px; color: var(--muted); text-align: right; }
    .c-select { width: 38px; text-align: center; }
    .c-grade { width: 52px; }
    .c-dataset { width: 24%; }
    .c-episode { width: 116px; }
    .c-issue { width: 120px; }
    .c-info { width: 17%; }
    .c-prompt { width: 24%; }
    .c-time { width: 132px; }
    .c-action { width: 68px; }
    .c-select input { width: 15px; min-height: 15px; }
    .row-delete { min-height: 26px; padding: 0 7px; font-size: 10px; }
    .badge {
      display: inline-flex; padding: 2px 6px; border-radius: 999px;
      color: #34445d; background: #edf1f7; font-size: 10px; font-weight: 800;
    }
    .badge.grade { color: #6d28d9; background: #f3e8ff; }
    .badge.issue { margin: 0 3px 2px 0; color: var(--danger); background: #fff0ee; }
    .dataset-name, .episode-name { font-weight: 750; }
    .path, .prompt, .line {
      display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    }
    .path { margin-top: 2px; color: var(--muted); font-size: 10px; }
    .old { color: #7a3e0b; }
    .new { color: var(--ok); font-weight: 750; }
    .arrow { color: #9aa5b5; }
    .empty { padding: 60px 20px; color: var(--muted); text-align: center; font-size: 13px; }
    .pagination {
      display: flex; align-items: center; justify-content: flex-end; gap: 7px;
      margin-top: 8px; color: var(--muted); font-size: 11px;
    }
    .pagination .btn { min-height: 30px; }
    .file-note { min-width: 0; margin-right: auto; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    @media (max-width: 900px) {
      .page { padding: 9px; }
      .topbar { align-items: flex-start; }
      .summary { grid-template-columns: repeat(2, 1fr); }
      .toolbar { grid-template-columns: 1fr 1fr; }
      .toolbar input { grid-column: 1 / -1; }
      .result-count { text-align: left; }
      .table-wrap { max-height: calc(100vh - 292px); }
      table { min-width: 1120px; }
    }
  </style>
</head>
<body>
  <main class="page">
    <header class="topbar">
      <div>
        <h1>人工筛查问题记录</h1>
        <p class="subtitle">实时读取统一 JSON，只展示操作员已记录的待改进项。</p>
      </div>
      <div class="actions">
        <button id="refreshBtn" class="btn primary" type="button">刷新记录</button>
        <a class="btn" href="/manual-screening/">返回人工筛查</a>
      </div>
    </header>

    <section class="summary">
      <div class="stat"><span>记录总数</span><strong id="totalStat">0</strong></div>
      <div class="stat"><span>涉及数据集</span><strong id="datasetStat">0</strong></div>
      <div class="stat"><span>左右手错误</span><strong id="armStat">0</strong></div>
      <div class="stat"><span>物品错误</span><strong id="objectStat">0</strong></div>
      <div class="stat"><span>轨迹不好</span><strong id="trajectoryStat">0</strong></div>
    </section>

    <section class="toolbar">
      <input id="searchInput" type="search" placeholder="搜索数据集、目录、Episode、Prompt、物品或更正内容" />
      <select id="typeFilter">
        <option value="all">全部问题类型</option>
        <option value="wrong_arm">左右手错误</option>
        <option value="wrong_object">物品错误</option>
        <option value="bad_trajectory">轨迹不好</option>
      </select>
      <select id="gradeFilter"><option value="all">全部等级</option></select>
      <select id="pageSizeSelect">
        <option value="50">50 条/页</option>
        <option value="100" selected>100 条/页</option>
        <option value="200">200 条/页</option>
      </select>
      <button id="deleteSelectedBtn" class="btn danger" type="button" disabled>删除所选 (0)</button>
      <span id="resultCount" class="result-count">正在读取…</span>
    </section>

    <div class="table-wrap">
      <table>
        <thead><tr>
          <th class="c-select"><input id="selectAll" type="checkbox" title="全选当前筛选结果" /></th>
          <th class="c-num">#</th><th class="c-grade">等级</th><th class="c-dataset">数据集目录</th>
          <th class="c-episode">Episode</th><th class="c-issue">问题类型</th>
          <th class="c-info">原信息 → 更正信息</th><th class="c-prompt">Prompt / 备注</th><th class="c-time">更新时间</th><th class="c-action">操作</th>
        </tr></thead>
        <tbody id="recordBody"></tbody>
      </table>
      <div id="emptyState" class="empty" hidden>没有符合当前筛选条件的问题记录。</div>
    </div>

    <footer class="pagination">
      <span id="recordFile" class="file-note"></span>
      <button id="prevPage" class="btn" type="button">上一页</button>
      <span id="pageInfo">1 / 1</span>
      <button id="nextPage" class="btn" type="button">下一页</button>
    </footer>
  </main>

  <script>
    let records = [];
    let page = 1;
    const selectedRecordIds = new Set();
    const $ = id => document.getElementById(id);
    const escapeHtml = value => String(value ?? "")
      .replaceAll("&", "&amp;").replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#39;");

    function gradeOf(record) {
      const annotated = String(record?.quality_grade || record?.source_quality_grade || "").toUpperCase();
      if (["A", "B", "C", "F"].includes(annotated)) return annotated;
      const parts = String(record?.dataset_path || "").split("/").filter(Boolean);
      const last = String(parts.at(-1) || "").toUpperCase();
      return ["A", "B", "C", "F"].includes(last) ? last : "-";
    }

    function datasetLabel(record) {
      const parts = String(record?.dataset_path || "").split("/").filter(Boolean);
      if (["A", "B", "C", "F"].includes(String(parts.at(-1) || "").toUpperCase())) parts.pop();
      return parts.at(-1) || record?.dataset_name || "未知数据集";
    }

    function handLabel(value) {
      if (value === "left") return "左手";
      if (value === "right") return "右手";
      if (value === "both") return "双手";
      return "-";
    }

    function formatTime(value) {
      const date = new Date(value || "");
      return Number.isNaN(date.getTime()) ? String(value || "-") : date.toLocaleString("zh-CN", {hour12: false});
    }

    function searchableText(record) {
      return [
        record.dataset_name, record.dataset_path, record.episode_name, record.prompt,
        ...(record.error_labels || []), record.original?.arm, ...(record.original?.objects || []),
        ...(record.original?.objects_zh || []), record.corrections?.arm, record.corrections?.object,
        ...(record.original?.objects_by_arm?.left || []), ...(record.original?.objects_by_arm?.right || []),
        record.corrections?.objects_by_arm?.left, record.corrections?.objects_by_arm?.right,
        record.review_note, record.quality_grade, record.source_quality_grade,
      ].join(" ").toLocaleLowerCase();
    }

    function filteredRecords() {
      const query = $("searchInput").value.trim().toLocaleLowerCase();
      const type = $("typeFilter").value;
      const grade = $("gradeFilter").value;
      return records.filter(record =>
        (!query || searchableText(record).includes(query)) &&
        (type === "all" || (record.error_types || []).includes(type)) &&
        (grade === "all" || gradeOf(record) === grade)
      );
    }

    function correctionHtml(record) {
      const rows = [];
      const types = new Set(record.error_types || []);
      if (types.has("wrong_arm")) {
        rows.push(`<span class="line"><span class="old">${escapeHtml(handLabel(record.original?.arm))}</span> <span class="arrow">→</span> <span class="new">${escapeHtml(handLabel(record.corrections?.arm))}</span></span>`);
      }
      if (types.has("wrong_object")) {
        const correctedByArm = record.corrections?.objects_by_arm || {};
        const originalByArm = record.original?.objects_by_arm || {};
        if (correctedByArm.left || correctedByArm.right) {
          for (const [arm, label] of [["left", "左手"], ["right", "右手"]]) {
            const original = (originalByArm[arm] || []).join("、") || "-";
            const corrected = correctedByArm[arm] || "-";
            rows.push(`<span class="line" title="${label}: ${escapeHtml(original)} → ${escapeHtml(corrected)}"><b>${label}</b> <span class="old">${escapeHtml(original)}</span> <span class="arrow">→</span> <span class="new">${escapeHtml(corrected)}</span></span>`);
          }
        } else {
          const original = (record.original?.objects || []).join("、") || "-";
          rows.push(`<span class="line" title="${escapeHtml(original)} → ${escapeHtml(record.corrections?.object || "-")}"><span class="old">${escapeHtml(original)}</span> <span class="arrow">→</span> <span class="new">${escapeHtml(record.corrections?.object || "-")}</span></span>`);
        }
      }
      return rows.join("");
    }

    function syncSelection(filtered) {
      const ids = filtered.map(item => String(item.record_id || "")).filter(Boolean);
      const selectedCount = ids.filter(id => selectedRecordIds.has(id)).length;
      $("selectAll").checked = ids.length > 0 && selectedCount === ids.length;
      $("selectAll").indeterminate = selectedCount > 0 && selectedCount < ids.length;
      const totalSelected = records.filter(item => selectedRecordIds.has(String(item.record_id || ""))).length;
      $("deleteSelectedBtn").disabled = totalSelected === 0;
      $("deleteSelectedBtn").textContent = `删除所选 (${totalSelected})`;
    }

    function render() {
      const filtered = filteredRecords();
      const pageSize = Number($("pageSizeSelect").value || 100);
      const pages = Math.max(1, Math.ceil(filtered.length / pageSize));
      page = Math.min(page, pages);
      const start = (page - 1) * pageSize;
      const visible = filtered.slice(start, start + pageSize);
      $("recordBody").innerHTML = visible.map((record, offset) => `
        <tr>
          <td class="c-select"><input class="row-select" type="checkbox" data-record-id="${escapeHtml(record.record_id || "")}" ${selectedRecordIds.has(String(record.record_id || "")) ? "checked" : ""} /></td>
          <td class="c-num">${start + offset + 1}</td>
          <td class="c-grade"><span class="badge grade">${escapeHtml(gradeOf(record))}</span></td>
          <td class="c-dataset"><span class="dataset-name">${escapeHtml(datasetLabel(record))}</span><span class="path" title="${escapeHtml(record.dataset_path)}">${escapeHtml(record.dataset_path)}</span></td>
          <td class="c-episode"><span class="episode-name">${escapeHtml(record.episode_name || `episode_${record.episode_index}`)}</span><span class="path">闭合帧 ${escapeHtml(record.closure_frame ?? "-")}</span></td>
          <td class="c-issue">${(record.error_labels || []).map(label => `<span class="badge issue">${escapeHtml(label)}</span>`).join("")}</td>
          <td class="c-info">${correctionHtml(record)}</td>
          <td class="c-prompt"><span class="prompt" title="${escapeHtml(record.prompt)}">${escapeHtml(record.prompt || "-")}</span>${record.review_note ? `<span class="path" title="${escapeHtml(record.review_note)}">备注：${escapeHtml(record.review_note)}</span>` : ""}</td>
          <td class="c-time">${escapeHtml(formatTime(record.updated_at))}</td>
          <td class="c-action"><button class="btn danger row-delete" type="button" data-record-id="${escapeHtml(record.record_id || "")}">删除</button></td>
        </tr>`).join("");
      $("emptyState").hidden = visible.length > 0;
      $("resultCount").textContent = `筛选结果 ${filtered.length} / ${records.length}`;
      $("pageInfo").textContent = `${page} / ${pages}`;
      $("prevPage").disabled = page <= 1;
      $("nextPage").disabled = page >= pages;
      document.querySelectorAll(".row-select").forEach(input => input.addEventListener("change", () => {
        if (input.checked) selectedRecordIds.add(input.dataset.recordId);
        else selectedRecordIds.delete(input.dataset.recordId);
        syncSelection(filteredRecords());
      }));
      document.querySelectorAll(".row-delete").forEach(button => button.addEventListener("click", () => deleteRecords([button.dataset.recordId])));
      syncSelection(filtered);
    }

    async function loadRecords() {
      $("refreshBtn").disabled = true;
      $("resultCount").textContent = "正在读取…";
      try {
        const response = await fetch("/api/manual-screening/records", {cache: "no-store"});
        const data = await response.json();
        if (!response.ok || data.error) throw new Error(data.error || `读取失败 (${response.status})`);
        records = (data.records || []).slice().sort((a, b) => String(b.updated_at || "").localeCompare(String(a.updated_at || "")));
        selectedRecordIds.clear();
        $("recordFile").textContent = `JSON：${data.record_file || ""}`;
        $("recordFile").title = data.record_file || "";
        $("totalStat").textContent = records.length;
        $("datasetStat").textContent = new Set(records.map(item => item.dataset_path)).size;
        $("armStat").textContent = records.filter(item => (item.error_types || []).includes("wrong_arm")).length;
        $("objectStat").textContent = records.filter(item => (item.error_types || []).includes("wrong_object")).length;
        $("trajectoryStat").textContent = records.filter(item => (item.error_types || []).includes("bad_trajectory")).length;
        const grades = [...new Set(records.map(gradeOf).filter(value => value !== "-"))].sort();
        $("gradeFilter").innerHTML = '<option value="all">全部等级</option>' + grades.map(value => `<option value="${escapeHtml(value)}">${escapeHtml(value)} 等级</option>`).join("");
        page = 1;
        render();
      } catch (error) {
        records = [];
        $("recordBody").innerHTML = "";
        $("emptyState").hidden = false;
        $("emptyState").textContent = `读取问题记录失败：${String(error)}`;
        $("resultCount").textContent = "读取失败";
      } finally {
        $("refreshBtn").disabled = false;
      }
    }

    async function deleteRecords(recordIds) {
      const ids = [...new Set((recordIds || []).filter(Boolean))];
      if (!ids.length || !confirm(`确定删除选中的 ${ids.length} 条记录？只删除 JSON 记录，不会删除数据集。`)) return;
      $("deleteSelectedBtn").disabled = true;
      try {
        const response = await fetch("/api/manual-screening/records/delete", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({record_ids: ids}),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok || data.error) throw new Error(data.error || `删除失败 (${response.status})`);
        await loadRecords();
        $("resultCount").textContent = `已删除 ${Number(data.deleted_count || 0)} 条记录`;
      } catch (error) {
        $("resultCount").textContent = `删除失败：${String(error)}`;
        $("deleteSelectedBtn").disabled = selectedRecordIds.size === 0;
      }
    }

    ["searchInput", "typeFilter", "gradeFilter", "pageSizeSelect"].forEach(id => {
      $(id).addEventListener(id === "searchInput" ? "input" : "change", () => { page = 1; render(); });
    });
    $("prevPage").addEventListener("click", () => { page -= 1; render(); });
    $("nextPage").addEventListener("click", () => { page += 1; render(); });
    $("refreshBtn").addEventListener("click", loadRecords);
    $("deleteSelectedBtn").addEventListener("click", () => deleteRecords([...selectedRecordIds]));
    $("selectAll").addEventListener("change", event => {
      for (const record of filteredRecords()) {
        const id = String(record.record_id || "");
        if (!id) continue;
        if (event.target.checked) selectedRecordIds.add(id);
        else selectedRecordIds.delete(id);
      }
      render();
    });
    loadRecords();
  </script>
</body>
</html>
"""
