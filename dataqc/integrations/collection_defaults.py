"""Local Zerith machine defaults and bounded batch concurrency."""
from pathlib import Path
import json
from dataqc.config import settings


def integrate_collection(app):
    from .legacy_ui import replace
    html=app.HTML
    html=replace(html, '<div id="customDatasetControls" class="custom-dataset hidden">',
                 '<label for="customDatasetPath">自定义目录输入</label><div id="customDatasetControls" class="custom-dataset">')
    html=html.replace('customControls.classList.add("hidden");', 'customControls.classList.remove("hidden");')
    html=html.replace('document.getElementById("customDatasetControls").classList.add("hidden");',
                      'document.getElementById("customDatasetControls").classList.remove("hidden");')
    html=replace(html,'''      <select id="hostMachine">
        <option value="agilex">AgileX</option>
        <option value="h200" selected>H200</option>
      </select>''','''      <select id="hostMachine" aria-label="数据来源">
        <option value="zerith" selected>真机</option>
        <option value="simulation">仿真</option>
      </select>''')
    html=replace(html,'<option value="aloha" selected>松灵机器人（ALOHA）</option>', '<option value="aloha">松灵机器人（ALOHA）</option>')
    html=replace(html,'<option value="zerith">零次方机器人（23 自由度）</option>', '<option value="zerith" selected>零次方机器人（23 自由度）</option>')
    html=replace(html,'id="convertJobs" type="number" min="1" value="6"','id="convertJobs" type="number" min="1" value="2"')
    html=html.replace('填写 GPU 时 LeRobot CUDA resize','LeRobot GPU 视频编码（不可用时自动回退 CPU）')
    html=html.replace('HDF5: /srv/data/datasets/public；MCAP: /mnt/nas/agilex_raw_datasets_mcap/stage2_datasets','/data/zerith_data（递归读取 HDF5）')
    html=html.replace('fieldValue("hostMachine") || "h200"','fieldValue("hostMachine") || "zerith"')
    html=html.replace('String(event.target.value || "h200")','String(event.target.value || "zerith")')
    html=html.replace('currentMachine === "h200" ? "H200" : "AgileX"','currentMachine === "simulation" ? "仿真" : currentMachine === "zerith" ? "真机" : currentMachine === "h200" ? "H200" : "AgileX"')
    html=replace(html,'      currentMachine = String(event.target.value || "zerith");','''      currentMachine = String(event.target.value || "zerith");
      if(currentMachine==="zerith" || currentMachine==="simulation"){
        document.getElementById("robotType").value="zerith";
        document.getElementById("profile").value="";
        updateAlohaBaseActionOption();
      }''')
    html=replace(html,'<label>并行转换/质检数</label>','<label>质检并发（最多 2）</label>')
    html=html.replace('let currentMachine = "h200";', 'let currentMachine = "zerith";')
    html=html.replace('currentMachine === "h200" ? "递归 HDF5 数据集目录" : "两层数据集目录"', '"递归数据集目录"')
    html=replace(html,'      currentMachine = machine;','''      currentMachine = machine;
      sourceDatasetScanRoot = machine === "simulation" ? "/data/sim_data" : "/data/zerith_data";
      discoveredSourceDatasets = [];
      select.replaceChildren(new Option("正在扫描…", ""));
      updateSourceDatasetScanInfo("正在读取 " + sourceDatasetScanRoot);
      updateAlohaBaseActionOption();''')
    html=replace(html,'root === value || (root && value.startsWith(`${root}/`)) || sourceChoiceIncludesHdf5(item, hdf5Path)',
                 'root === value || root === String(hdf5Path || "").replace(/\\/+$/, "") || (root && value.startsWith(`${root}/`)) || sourceChoiceIncludesHdf5(item, hdf5Path)')
    html=replace(html,'      await loadSourceDatasetChoices(currentMachine);','''      const scanId = datasetScanRequestId + 1;
      await loadSourceDatasetChoices(currentMachine);
      if (scanId !== datasetScanRequestId) return;''')
    html=replace(html,'      const isZerith = document.getElementById("robotType").value === "zerith";','''      const isZerith = document.getElementById("robotType").value === "zerith";
      const hdf5Only = currentMachine === "simulation" || (!!fieldValue("hdf5Root") && !fieldValue("mcapPath"));
      for (const button of document.querySelectorAll('button[data-stage="convert"],button[data-stage="convert_qc"]')) {
        button.hidden = hdf5Only;
      }''')
    html=replace(html,'pathRows.map(([label, value]) =>',
                 'pathRows.filter(([label]) => currentMachine !== "simulation" || !label.startsWith("MCAP")).map(([label, value]) =>')
    html=html.replace('MCAP 中未发现任务文本，当前显示已有 HDF5/LeRobot task；可人工修改。','任务文本来自当前数据，可编辑。')
    checked=' checked' if settings().get('vlm_enabled',False) else ''
    html=replace(html,'      <label>运行机器</label>',f'''      <label style="display:flex;align-items:center;gap:8px"><input id="vlmEnabled" type="checkbox"{checked} style="width:auto">类别识别（YOLO + Terra）</label>
      <label>数据来源</label>''')
    html=replace(html,'        robot_type: document.getElementById("robotType").value,','        robot_type: document.getElementById("robotType").value,\n        vlm_enabled: document.getElementById("vlmEnabled").checked,')
    html=replace(html,'    function payload(extra = {}) {','''    const categoryToggle=document.getElementById("vlmEnabled");
    categoryToggle.disabled=true;
    fetch('/auto/api/settings').then(r=>{if(!r.ok)throw Error('settings');return r.json()})
      .then(c=>{categoryToggle.checked=c.vlm_enabled===true})
      .catch(()=>{}).finally(()=>{categoryToggle.disabled=false});
    function payload(extra = {}) {''')
    html=replace(html,'<input id="convertJobs" type="number" min="1" value="2" />',
                 '<input id="convertJobs" type="number" min="1" max="2" value="2" /><label>LeRobot 转换进程</label><input id="lerobotWorkers" type="number" min="1" value="4" />')
    html=replace(html,'        vlm_enabled: document.getElementById("vlmEnabled").checked,',
                 '        vlm_enabled: document.getElementById("vlmEnabled").checked,\n        lerobot_workers: Number(document.getElementById("lerobotWorkers").value),')
    app.HTML=html
    old_discover=app.discover_machine_datasets
    def discover(machine):
        machine=str(machine or 'zerith').lower()
        if machine not in ('zerith','simulation'):return old_discover(machine)
        root=Path('/data/sim_data' if machine=='simulation' else '/data/zerith_data')
        datasets=app.discover_recursive_hdf5_dataset_choices(root)
        return dict(machine=machine,scan_root=str(root),scan_depth=app.H200_RECURSIVE_SCAN_MAX_DEPTH,
                    scan_recursive=True,scan_root_exists=root.is_dir(),
                    scan_roots=[dict(type='HDF5',path=str(root),exists=root.is_dir())],datasets=datasets)
    app.discover_machine_datasets=discover
    old_config=app.derive_paths
    def build_config(payload):
        payload=dict(payload)
        payload.setdefault('robot_type','zerith')
        payload.setdefault('convert_jobs',2)
        payload.setdefault('gpu_device','0')
        workers=payload.get('lerobot_workers',4)
        if isinstance(workers,bool) or not str(workers).isdigit() or int(workers)<1:
            raise ValueError('LeRobot 转换进程数必须是正整数')
        cfg=old_config(payload)
        cfg['lerobot_workers']=int(workers)
        # The LeRobot directory contains sibling buckets for full and split episodes.
        if cfg.get('robot_type')=='zerith' and cfg['lerobot_root'].name.lower()=='lerobot':
            cfg['lerobot_root']=cfg['lerobot_root']/'twohands'
        current=settings()
        enabled=payload.get('vlm_enabled',current.get('vlm_enabled',False))
        if type(enabled) is not bool:
            raise ValueError('vlm_enabled 必须是 JSON 布尔值 true 或 false')
        cfg.update(vlm_enabled=enabled,api_model=current['api_model'],
                   motion_batch_size=current.get('motion_batch_size',10),
                   motion_batch_concurrency=current.get('motion_batch_concurrency',2))
        return cfg
    app.derive_paths=build_config
    old_qc=app.qc_command
    def qc_command(cfg):
        cmd,cwd,env=old_qc(cfg)
        if cfg.get('robot_type')=='zerith':
            cmd[cmd.index('--num-workers')+1]=str(max(1,min(2,int(cfg.get('convert_jobs',2)))))
            env=dict(env)
            env['DATAQC_CATEGORY_POLICY']=json.dumps({k:cfg[k] for k in ('vlm_enabled','api_model','motion_batch_size','motion_batch_concurrency')})
        return cmd,cwd,env
    app.qc_command=qc_command
