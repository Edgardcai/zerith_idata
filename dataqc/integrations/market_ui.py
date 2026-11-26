"""Reuse the installed workbench/replay layouts without its two-stage operations."""
from functools import lru_cache
import importlib.util
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]


def build_page(original):
    # The template is the fully integrated UI on this machine, including its CSS.
    helpers = original[original.index('    function escapeHtml(value)'):original.index('    function renderQualityCheckItems(items)')]
    html = re.sub(r'<script\b[^>]*>.*?</script>', '', original, flags=re.S)
    html = html.replace('<h1>数据质检工作台</h1>', '<h1>商超全流程质检</h1>')
    html = html.replace('① 采集数据质检与转换', '① 商超全流程质检与转换')
    html = html.replace('href="/market/" class="server-pill">商超全流程质检', 'href="/" class="server-pill">返回原质检页')
    if 'id="qcAllRecordings"' not in html:
        html = html.replace('      <section class="action-card hdf5-actions"', '<section class="action-card"><h3>全部商超记录</h3><div class="actions"><button id="qcAllRecordings" class="primary">一键质检全部商超记录</button><button id="qcAllToLerobot" class="primary">全部质检并转 LeRobot</button><button id="viewAllRecordingReports">查看全部报告</button></div></section>      <section class="action-card hdf5-actions"')
    html = html.replace('lerobot-stage-split-control hidden', 'lerobot-stage-split-control')
    html = html.replace('</head>', '<style>.market-export-options{display:flex;flex-wrap:wrap;gap:8px;align-items:center}.market-export-options>span,.market-export-options>label{white-space:nowrap}.market-export-options>small{flex-basis:100%;line-height:1.6}</style></head>')
    html = html.replace('</body>', '<script>'+helpers+'</script><script src="/auto/assets/qc-report.js?v=1"></script><script src="/market/workbench.js?v=6"></script></body>')
    return html


@lru_cache(maxsize=1)
def replay_module():
    path = ROOT/'legacy/scripts/embodied_data_pipeline-main/mcap_conversion/scripts/replay_icra_episode.py'
    spec = importlib.util.spec_from_file_location('market_replay_layout', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def replay_page():
    html = replay_module().build_index_html()
    # Shared live-grade/filter assets use the same response contracts, redirected
    # within this iframe only. No old mutation endpoint is exposed by this mode.
    html = html.replace('<script src="/auto/assets/raw-grade.js', '<script src="/market/replay-bridge.js?v=3"></script><script src="/auto/assets/raw-grade.js')
    html = html.replace('if (!Array.isArray(stages) || stages.length !== 2) return [];', 'if (!Array.isArray(stages)) return [];')
    html = html.replace('return stages.filter(stage => Number(stage.stage_number) === 1 || Number(stage.stage_number) === 2);', 'return stages;')
    html = html.replace('if (stages.length === 2) {\n        const stage2Start = Number(stages[1].start);', 'for (const stage of stages.slice(1)) {\n        const stage2Start = Number(stage.start);')
    html = html.replace('ctx.fillText("阶段 2",', 'ctx.fillText(stage.label || `阶段 ${stage.stage_number}`,')
    html = html.replace('      seekToFrame(0);\n    }', '      window.marketReplayLoaded(data, seekToFrame, pause);\n      seekToFrame(0);\n    }')
    html = html.replace('video.src = info.url;', 'if(info.url)video.src = info.url;')
    html = html.replace('</head>', '<style>.market-phases{display:flex;flex-wrap:wrap;gap:6px}.market-phases button{font-size:12px}.qc-strip{max-height:43vh;overflow:auto}#frameCutControls{display:inline-flex!important}button:disabled{opacity:.45;cursor:not-allowed}</style></head>')
    return html
