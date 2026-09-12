"""Compact, simultaneous replay and screening view for the unified workbench."""
from pathlib import Path
import re

ASSETS = Path(__file__).with_name('review_assets')


def asset(name):
    return (ASSETS / name).read_text()


def between(text, start, end, value):
    a = text.index(start)
    b = text.index(end, a)
    return text[:a] + value + text[b:]


def integrate_compact(app):
    from .legacy_ui import replace
    app.HTML = replace(app.HTML, '</style>', '''
    #lerobotVisualizationFrame{min-height:0;height:calc(100dvh - 74px)}
    </style>''')
    outer = app.LEROBOT_VISUALIZATION_HTML
    root = re.search(r'<input id="rootPath"[^>]+>', outer).group(0)
    toolbar = '''<header class="directory-toolbar">
      <label for="rootPath">数据目录</label>ROOT
      <button id="scanBtn">递归读取</button>
      <select id="datasetPicker" aria-label="选择 LeRobot 数据集"><option value="">请先递归读取</option></select>
      <button id="batchExtractBtn" disabled>截取图片并识别</button>
      <button id="screeningToggle" hidden disabled>问题记录</button>
      <div class="toolbar-status"><span id="scanStatus" role="status">输入目录，递归读取后选择 LeRobot 数据集</span><span id="batchStatus" role="status"></span></div>
    </header>'''.replace('ROOT', root)
    outer = between(outer, '    <aside class="sidebar">', '      <div class="replay-stage">', '    <section class="viewer">\n' + toolbar + '\n')
    outer = between(outer, '    function visibleDatasets()', '    function formatTime', asset('directory-functions.js') + '\n')
    outer = outer.replace("    $('searchInput').addEventListener('input', renderDatasets);", '')
    outer = replace(outer, '</style>', asset('directory.css') + '</style>')
    outer = replace(outer, '<strong>当前 Episode 数据筛查</strong>', '<strong>问题记录与批量管理</strong><button id="closeRecords" class="ghost">关闭</button>')
    outer = replace(outer, "setStatus(`正在回放：${path}`, 'ok');", "setStatus(`已加载 ${Number(item.episode_count||0)} 条 episode · 可回放或整目录截帧识别`, 'ok');")
    outer = replace(outer, "        await loadScreeningRecords();\n      } catch (error) {\n        setScreeningStatus", "        await loadScreeningRecords();\n        $('replayFrame').contentWindow.postMessage({type:'unified-screening-refresh'},location.origin);\n      } catch (error) {\n        setScreeningStatus")
    outer = replace(outer, '    let datasets = [];', '    let batchRunning = false;\n    let datasets = [];')
    outer = replace(outer, '      const root =', '      if (batchRunning) return;\n      const root =')
    outer = replace(outer, '    async function openDataset(path) {', '    async function openDataset(path) {\n      if (batchRunning) return;')
    outer = replace(outer, '    renderScreeningForm();\n    renderScreeningRecords();\n  </script>', asset('directory-events.js') + '\n    renderScreeningForm();\n    renderScreeningRecords();\n  </script>')
    app.LEROBOT_VISUALIZATION_HTML = outer

    replay = app.LEROBOT_REPLAY_HTML
    replay = re.sub(r'      <nav class="unified-tabs">.*?</nav>', '', replay)
    replay = replace(replay, '<section id="manualInfoPanel" hidden>', '<section id="manualInfoPanel">')
    replay = between(replay, '    function infoTab(manual)', '    $("manualInfoFrame").onload', '')
    replay = replace(replay, '      if (!manualLoaded || !screeningEpisode) return;', '''      if (!screeningEpisode) return;
      if(!manualLoaded){
        manualLoaded=true;
        $("manualInfoFrame").src="/manual-screening/?embedded=1&dataset_path="+encodeURIComponent(params.get("dataset_path")||"")+"&episode_index="+Number(screeningEpisode.episode_index);
        return;
      }''')
    replay = replace(replay, '    let screeningEpisode = null;', '''    document.querySelector(".app").append($("manualInfoPanel"));
    $("manualInfoPanel").classList.add("panel");
    $("openGradeRecords").outerHTML='<div class="screening-title"><strong>人工筛查</strong><span id="screeningEpisodeLabel"></span><button id="openGradeRecords">问题记录 / 批量管理</button></div>';
    window.addEventListener("message", event=>{
      if(event.origin===location.origin && event.source===parent && event.data?.type==="unified-screening-refresh" && manualLoaded){
        $("manualInfoFrame").contentWindow.postMessage({type:"unified-screening-reload"},location.origin);
      }
    });
    let screeningEpisode = null;''')
    replay = replace(replay, '      screeningEpisode=episode; syncManualContext();', '''      screeningEpisode=episode;
      $("screeningEpisodeLabel").textContent=episode ? `Episode ${episode.episode_index}` : "";
      syncManualContext();''')
    replay = replace(replay, '    async function loadEpisode(index) {', '    let episodeRequest=0;\n    async function loadEpisode(index) {\n      const request=++episodeRequest;')
    replay = replace(replay, '      const ep = payload.episode || {};', '      if(request!==episodeRequest)return;\n      const ep = payload.episode || {};')
    replay = replace(replay, '</style>', asset('replay.css') + '</style>')
    app.LEROBOT_REPLAY_HTML = replay

    manual = app.MANUAL_SCREENING_HTML
    manual = replace(manual, '      if(!episode)return;', '''      if(!episode){
        $("workspace").classList.add("hidden");$("emptyState").classList.remove("hidden");
        $("emptyState").textContent=`Episode ${contextEpisode} 尚无截帧结果，请使用顶部“截取图片并识别”。`;return;
      }
      $("workspace").classList.remove("hidden");$("emptyState").classList.add("hidden");''')
    manual = replace(manual, '      contextPath=event.data.dataset_path;contextEpisode=Number(event.data.episode_index);', '''      if(contextEpisode!==Number(event.data.episode_index)){
        yoloRequestToken+=1;$("retryYoloBtn").disabled=false;
      }
      contextPath=event.data.dataset_path;contextEpisode=Number(event.data.episode_index);''')
    manual = replace(manual, '    function applyContext(){', '''    window.addEventListener("message",event=>{
      if(event.origin===location.origin&&event.source===parent&&event.data?.type==="unified-screening-reload")loadCachedResult();
    });
    let renderedRecordKey="";
    function applyContext(){''')
    manual = replace(manual, '      renderSavedRecord();\n      renderStoredYoloResult();', '''      const nextRecordKey=recordKey(episode);
      if(nextRecordKey!==renderedRecordKey){renderSavedRecord();renderedRecordKey=nextRecordKey;}
      renderStoredYoloResult();''')
    manual = replace(manual, '      records = new Map((savedRecords', '      renderedRecordKey="";\n      records = new Map((savedRecords')
    manual = replace(manual, '        records.delete(recordKey(episode));', '        renderedRecordKey="";\n        records.delete(recordKey(episode));')
    manual = replace(manual, '    discover();\n  </script>', asset('manual-layout.js') + '\n    discover();\n  </script>')
    manual = replace(manual, '</style>', asset('manual.css') + '</style>')
    app.MANUAL_SCREENING_HTML = manual
