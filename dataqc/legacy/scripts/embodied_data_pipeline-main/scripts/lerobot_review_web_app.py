#!/usr/bin/env python3
"""Lightweight LeRobot review console.

This page intentionally excludes MCAP/HDF5 conversion controls. It only shows a
compact LeRobot episode overview and embeds the existing LeRobot replay UI.
"""

from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import pipeline_web_app as pipeline  # noqa: E402


DEFAULT_PORT = 8892


def json_response(handler: BaseHTTPRequestHandler, payload: Any, status: int = 200) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0:
        return {}
    try:
        payload = json.loads(handler.rfile.read(length).decode("utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def path_from_payload(payload: dict[str, Any]) -> Path | None:
    text = str(payload.get("lerobot_parent") or payload.get("lerobot_path") or payload.get("lerobot_root") or "").strip()
    if not text:
        return None
    return Path(text).expanduser().resolve()


def discover_lerobot_datasets(path: Path | None) -> list[Path]:
    if path is None or not path.exists():
        return []
    if pipeline.is_lerobot_dataset_dir(path):
        return [path]

    datasets: list[Path] = []
    for info_path in sorted(path.rglob("meta/info.json"), key=lambda item: pipeline.natural_key(str(item))):
        dataset_dir = info_path.parent.parent
        if pipeline.is_lerobot_dataset_dir(dataset_dir):
            datasets.append(dataset_dir)

    unique: list[Path] = []
    seen: set[Path] = set()
    for dataset_dir in datasets:
        resolved = dataset_dir.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(resolved)
    return unique


def dataset_label(dataset_dir: Path, root: Path | None) -> str:
    if root is not None:
        try:
            return str(dataset_dir.relative_to(root))
        except ValueError:
            pass
    return dataset_dir.name


def cfg_for_dataset(dataset_dir: Path) -> dict[str, Any]:
    return pipeline.derive_paths(
        {
            "lerobot_root": str(dataset_dir),
            "robot_type": "aloha",
            "repo_id": dataset_dir.name,
        }
    )


def browser_groups(payload: dict[str, Any]) -> list[dict[str, Any]]:
    parent = path_from_payload(payload)
    if parent is None:
        return []
    return pipeline.discover_lerobot_browser_datasets(parent)


def selected_browser_group(payload: dict[str, Any], groups: list[dict[str, Any]]) -> dict[str, Any] | None:
    selected = str(payload.get("selected_dataset") or "").strip()
    if not groups:
        return None
    if selected:
        for group in groups:
            if selected in {str(group.get("name") or ""), str(group.get("path") or "")}:
                return group
    return groups[0]


def selected_browser_grade(payload: dict[str, Any], group: dict[str, Any] | None) -> dict[str, Any] | None:
    if group is None:
        return None
    grades = group.get("grades") if isinstance(group.get("grades"), list) else []
    if not grades:
        return None
    selected = str(payload.get("selected_grade") or "").strip()
    if selected:
        for grade in grades:
            if selected in {
                str(grade.get("grade") or ""),
                str(grade.get("label") or ""),
                str(grade.get("path") or ""),
                str(grade.get("repo_id") or ""),
            }:
                return grade
    return grades[0]


def reason_text(meta: dict[str, Any]) -> str:
    labels = meta.get("reason_labels_zh")
    if isinstance(labels, list):
        return "；".join(str(item).strip() for item in labels if str(item).strip())
    labels = meta.get("reason_labels")
    if isinstance(labels, list):
        return "；".join(str(item).strip() for item in labels if str(item).strip())
    value = meta.get("quality_description") or meta.get("manual_review_reason")
    return str(value or "").strip()


def overview_rows_for_dataset(dataset_dir: Path, root: Path | None) -> list[dict[str, Any]]:
    info = pipeline.load_json_file(dataset_dir / "meta" / "info.json")
    mapping = pipeline.load_json_file(dataset_dir / "meta" / "episode_name_mapping.json")
    mapping_rows = mapping.get("episodes", []) if isinstance(mapping.get("episodes"), list) else []
    mapping_by_index = {
        int(item.get("lerobot_episode_index")): item
        for item in mapping_rows
        if isinstance(item, dict) and str(item.get("lerobot_episode_index", "")).isdigit()
    }
    episodes_meta = {
        int(item.get("episode_index")): item
        for item in pipeline.load_jsonl(dataset_dir / "meta" / "episodes.jsonl")
        if str(item.get("episode_index", "")).isdigit()
    }
    indices = sorted(set(mapping_by_index) | set(episodes_meta))
    if not indices:
        for parquet in sorted(dataset_dir.glob("data/chunk-*/episode_*.parquet")):
            stem = parquet.stem.replace("episode_", "")
            if stem.isdigit():
                indices.append(int(stem))

    label = dataset_label(dataset_dir, root)
    rows: list[dict[str, Any]] = []
    for index in sorted(set(indices)):
        meta = episodes_meta.get(index, {})
        mapping_row = mapping_by_index.get(index, {})
        grade = pipeline.normalise_quality_grade(
            meta.get("quality_grade") or mapping_row.get("quality_grade") or dataset_dir.name
        )
        episode_name = str(
            mapping_row.get("source_episode_name")
            or mapping_row.get("source_mcap_episode_name")
            or mapping_row.get("hdf5_episode_name")
            or mapping_row.get("lerobot_episode_name")
            or f"episode_{index:06d}"
        )
        rows.append(
            {
                "episode": episode_name,
                "frames": int(meta.get("length") or mapping_row.get("num_frames") or 0),
                "status": "采集失败" if grade == "F" else ("采集成功" if grade else "未标注"),
                "quality_grade": grade,
                "quality_description": reason_text(meta),
                "dataset": label,
                "dataset_dir": str(dataset_dir),
                "episode_index": index,
                "fps": info.get("fps"),
            }
        )
    return rows


def review_status(payload: dict[str, Any]) -> dict[str, Any]:
    root = path_from_payload(payload)
    groups = browser_groups(payload)
    selected_group = selected_browser_group(payload, groups)
    selected_grade = selected_browser_grade(payload, selected_group)
    rows: list[dict[str, Any]] = []
    if selected_grade is not None:
        rows = overview_rows_for_dataset(Path(str(selected_grade.get("path"))).expanduser().resolve(), root)

    rows.sort(key=lambda item: (pipeline.natural_key(str(item["dataset"])), pipeline.natural_key(str(item["episode"]))))
    return {
        "root": str(root) if root else "",
        "datasets": groups,
        "selected_dataset": str(selected_group.get("name") or "") if selected_group else "",
        "selected_grade": str(selected_grade.get("grade") or selected_grade.get("label") or "") if selected_grade else "",
        "selected_grade_path": str(selected_grade.get("path") or "") if selected_grade else "",
        "episodes": rows,
    }


def start_lerobot_replay(payload: dict[str, Any]) -> dict[str, Any]:
    groups = browser_groups(payload)
    selected_group = selected_browser_group(payload, groups)
    selected_grade = selected_browser_grade(payload, selected_group)
    if selected_grade is None:
        raise FileNotFoundError("No LeRobot dataset found")
    dataset_dir = Path(str(selected_grade.get("path"))).expanduser().resolve()
    cfg = cfg_for_dataset(dataset_dir)
    summary = pipeline.build_lerobot_replay_summary(cfg)
    key = pipeline.now_id()
    with pipeline.JOBS_LOCK:
        pipeline.LEROBOT_REPLAY_CONFIGS[key] = cfg
    return {"url": f"/lerobot-replay/?key={key}", "key": key, "summary": summary}


HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>LeRobot 数据回放</title>
  <style>
    :root {
      --bg: #f6f7f9;
      --panel: #ffffff;
      --text: #101828;
      --muted: #667085;
      --line: #d0d5dd;
      --accent: #2563eb;
      --danger: #dc2626;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      background: var(--bg);
      color: var(--text);
      font-family: Inter, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      letter-spacing: 0;
    }
    header {
      height: 54px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 0 18px;
      background: #111827;
      color: #fff;
    }
    header h1 { margin: 0; font-size: 16px; font-weight: 650; }
    main {
      min-height: calc(100vh - 54px);
      padding: 12px;
      display: grid;
      grid-template-columns: 340px minmax(0, 1fr);
      gap: 12px;
    }
    section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      min-width: 0;
    }
    label { display: block; margin: 12px 0 5px; color: var(--muted); font-size: 12px; font-weight: 600; }
    input, select, button {
      width: 100%;
      min-height: 36px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 0 10px;
      background: #fff;
      color: var(--text);
      font: inherit;
      font-size: 13px;
    }
    button { cursor: pointer; }
    button.primary {
      margin-top: 14px;
      background: var(--accent);
      border-color: var(--accent);
      color: #fff;
    }
    button:disabled { opacity: .55; cursor: wait; }
    .hint {
      margin-top: 8px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.45;
      overflow-wrap: anywhere;
    }
    .meta {
      margin-top: 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
      font-size: 12px;
    }
    .meta div { display: grid; grid-template-columns: 82px minmax(0, 1fr); border-bottom: 1px solid #eef1f5; }
    .meta div:last-child { border-bottom: 0; }
    .meta b { background: #f9fafb; padding: 7px; color: var(--muted); }
    .meta span { padding: 7px; white-space: pre-wrap; overflow-wrap: anywhere; }
    .log {
      margin-top: 12px;
      min-height: 108px;
      max-height: 220px;
      overflow: auto;
      padding: 10px;
      border-radius: 8px;
      background: #111827;
      color: #d1fae5;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 12px;
      white-space: pre-wrap;
    }
    .viewer { padding: 8px; display: grid; grid-template-rows: minmax(0, 1fr); }
    iframe {
      width: 100%;
      height: calc(100vh - 86px);
      min-height: 820px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
    }
    @media (max-width: 980px) {
      main { grid-template-columns: 1fr; }
      iframe { height: 82vh; min-height: 720px; }
    }
  </style>
</head>
<body>
  <header>
    <h1>LeRobot 数据回放</h1>
    <span>8892</span>
  </header>
  <main>
    <section>
      <label>LeRobot 父级目录</label>
      <input id="parentPath" autocomplete="off" placeholder="例如 data/lerobot" />
      <div class="hint">该目录下可以包含多个 dataset，例如：父级目录 / aloha_0703 / A、B、F。</div>

      <label>Dataset</label>
      <select id="datasetSelect">
        <option value="">请先填写父级目录</option>
      </select>

      <label>质量等级</label>
      <select id="gradeSelect">
        <option value="">请先选择 dataset</option>
      </select>

      <button id="replayBtn" class="primary" type="button">打开回放</button>
      <div class="meta" id="metaBox">
        <div><b>状态</b><span>等待输入 LeRobot 父级目录</span></div>
      </div>
      <div class="log" id="log">填写父级目录后会自动检索 dataset。</div>
    </section>
    <section class="viewer">
      <iframe id="replayFrame"></iframe>
    </section>
  </main>
  <script>
    const $ = id => document.getElementById(id);
    let datasets = [];

    function escapeHtml(value) {
      return String(value ?? "").replace(/[&<>"']/g, ch => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
      }[ch]));
    }

    async function postJson(url, payload) {
      const res = await fetch(url, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok || data.error) throw new Error(data.error || res.statusText);
      return data;
    }

    function selectedDataset() {
      return datasets[Number($("datasetSelect").value)] || null;
    }

    function selectedGrade() {
      const dataset = selectedDataset();
      if (!dataset) return null;
      return (dataset.grades || [])[Number($("gradeSelect").value)] || null;
    }

    function gradeCountText(dataset) {
      const grades = Array.isArray(dataset?.grades) ? dataset.grades : [];
      if (!grades.length) return "(无等级数据)";
      return grades.map(grade => {
        const label = grade.label || grade.grade || "空";
        const count = grade.episode_count === null || grade.episode_count === undefined ? "-" : grade.episode_count;
        return `${label}: ${count}条`;
      }).join(" / ");
    }

    function payload(extra = {}) {
      const dataset = selectedDataset();
      const grade = selectedGrade();
      return {
        lerobot_parent: $("parentPath").value.trim(),
        selected_dataset: dataset ? dataset.name : "",
        selected_grade: grade ? (grade.grade || grade.label || grade.path) : "",
        ...extra,
      };
    }

    function renderMeta(root, dataset, grade) {
      const rows = [
        ["父级目录", root || "(未填写)"],
        ["Dataset", dataset ? dataset.name : "(未选择)"],
        ["总数量", dataset ? `${dataset.episode_count ?? "-"}条` : "(未选择)"],
        ["等级数量", dataset ? gradeCountText(dataset) : "(未选择)"],
        ["回放等级", grade ? (grade.label || grade.grade || "空") : "(未选择)"],
        ["回放目录", grade ? grade.path : "(未选择)"],
      ];
      $("metaBox").innerHTML = rows
        .map(([key, value]) => `<div><b>${escapeHtml(key)}</b><span>${escapeHtml(value)}</span></div>`)
        .join("");
    }

    function renderGrades() {
      const dataset = selectedDataset();
      const grades = dataset?.grades || [];
      $("gradeSelect").innerHTML = grades.length
        ? grades.map((grade, index) => {
            const count = grade.episode_count === null || grade.episode_count === undefined ? "" : ` · ${grade.episode_count}条`;
            return `<option value="${index}">${escapeHtml(grade.label || grade.grade || "空")}${count}</option>`;
          }).join("")
        : `<option value="">没有可用等级</option>`;
      $("gradeSelect").value = grades.length ? "0" : "";
      renderMeta($("parentPath").value.trim(), dataset, selectedGrade());
    }

    function renderDatasets(data) {
      datasets = Array.isArray(data.datasets) ? data.datasets : [];
      $("datasetSelect").innerHTML = datasets.length
        ? datasets.map((item, index) => {
            const count = item.episode_count === null || item.episode_count === undefined ? "" : ` · ${item.episode_count}条`;
            const grades = gradeCountText(item);
            return `<option value="${index}">${escapeHtml(item.name)}${count} · ${escapeHtml(grades)}</option>`;
          }).join("")
        : `<option value="">未找到 dataset</option>`;
      $("datasetSelect").value = datasets.length ? "0" : "";
      renderGrades();
    }

    async function refreshDatasets() {
      const parent = $("parentPath").value.trim();
      if (!parent) {
        datasets = [];
        renderDatasets({datasets: []});
        $("log").textContent = "请填写 LeRobot 父级目录。";
        return;
      }
      try {
        const data = await postJson("/api/status", {lerobot_parent: parent});
        renderDatasets(data);
        $("log").textContent = datasets.length
          ? `扫描完成: ${data.root}\n发现 ${datasets.length} 个 dataset。`
          : `未找到 LeRobot dataset: ${data.root}`;
      } catch (err) {
        datasets = [];
        renderDatasets({datasets: []});
        $("log").textContent = String(err);
      }
    }

    async function startReplay() {
      const dataset = selectedDataset();
      const grade = selectedGrade();
      if (!dataset || !grade) {
        $("log").textContent = "请先选择 dataset 和质量等级。";
        return;
      }
      $("replayBtn").disabled = true;
      try {
        const data = await postJson("/api/lerobot-replay/start", payload());
        $("replayFrame").src = data.url;
        $("log").textContent = `LeRobot 回放已启动: ${dataset.name} / ${grade.label}\n${grade.path}`;
      } catch (err) {
        $("replayFrame").removeAttribute("src");
        $("log").textContent = String(err);
      } finally {
        $("replayBtn").disabled = false;
      }
    }

    let refreshTimer = null;
    $("parentPath").addEventListener("input", () => {
      clearTimeout(refreshTimer);
      refreshTimer = setTimeout(refreshDatasets, 350);
    });
    $("parentPath").addEventListener("change", refreshDatasets);
    $("datasetSelect").addEventListener("change", renderGrades);
    $("gradeSelect").addEventListener("change", () => renderMeta($("parentPath").value.trim(), selectedDataset(), selectedGrade()));
    $("replayBtn").addEventListener("click", startReplay);
  </script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_HEAD(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/lerobot-video/"):
            pipeline.serve_lerobot_video(self, parsed.path, send_body=False)
            return
        self.send_response(200)
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            body = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/lerobot-replay/":
            body = pipeline.LEROBOT_REPLAY_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path.startswith("/lerobot-video/"):
            pipeline.serve_lerobot_video(self, parsed.path, send_body=True)
            return
        if parsed.path == "/api/lerobot-replay/episodes":
            query = parse_qs(parsed.query)
            key = str(query.get("key", [""])[0])
            json_response(self, pipeline.build_lerobot_replay_summary(pipeline.lerobot_cfg_for_key(key)))
            return
        if parsed.path == "/api/lerobot-replay/episode":
            query = parse_qs(parsed.query)
            key = str(query.get("key", [""])[0])
            episode_index = int(query.get("episode_index", ["0"])[0])
            json_response(self, pipeline.load_lerobot_episode_payload(pipeline.lerobot_cfg_for_key(key), episode_index))
            return
        if parsed.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        json_response(self, {"error": "not found"}, 404)

    def do_POST(self) -> None:
        try:
            payload = read_json(self)
            parsed = urlparse(self.path)
            if parsed.path == "/api/status":
                json_response(self, review_status(payload))
                return
            if parsed.path == "/api/lerobot-replay/start":
                json_response(self, start_lerobot_replay(payload))
                return
            json_response(self, {"error": "not found"}, 404)
        except Exception as exc:
            json_response(self, {"error": str(exc)}, 500)


def main() -> int:
    parser = argparse.ArgumentParser(description="Lightweight LeRobot review web app.")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"LeRobot review console: http://127.0.0.1:{args.port}/", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
