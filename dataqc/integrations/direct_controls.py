"""Manual operation controls depend on available files, never quality reports."""
from pathlib import Path
from dataqc.io import read_json

GRADES=('A','B','C','F','UNRATED')


def split_sources(app,cfg):
    try:
        configured=app.configured_lerobot_dataset_dir(cfg)
        if (configured/'meta/info.json').is_file():
            grade=configured.name if configured.name in GRADES else 'UNRATED'
            return [(grade,configured)]
    except (KeyError,ValueError,OSError):pass
    sources=[]
    for grade in GRADES:
        source=app.lerobot_grade_dataset_dir(cfg,grade)
        if (source/'meta/info.json').is_file():sources.append((grade,source))
    return sources


def output_config(app,cfg,source,grade):
    # Selecting <repo>/<grade> directly must not create <repo>/<grade>/<grade> outputs.
    repo=Path(str(cfg.get('repo_id') or ''))
    if repo.name==grade:
        cfg=dict(cfg,repo_id='' if str(repo.parent)=='.' else str(repo.parent))
    return cfg


def integrate_direct_controls(app):
    old_choice=app.dataset_choice_entry
    def choice(path,scan_root,group=''):
        result=old_choice(path,scan_root,group)
        path=Path(path)
        children=[path/g for g in GRADES if (path/g/'meta/info.json').is_file()]
        if (path/'meta/info.json').is_file() or children:
            info=read_json((path if not children else children[0])/'meta/info.json')
            result.update(dataset_type='lerobot' if (path/'meta/info.json').is_file() else 'lerobot_group',
                          robot_type=info.get('robot_type') or 'zerith')
        return result
    app.dataset_choice_entry=choice
    old_config=app.derive_paths
    def config(payload):
        cfg=old_config(payload)
        source_text=str(payload.get('lerobot_source_path') or '').strip()
        if source_text:
            source=Path(source_text).expanduser().resolve()
            individual=(source/'meta/info.json').is_file()
            if not individual and not any((source/g/'meta/info.json').is_file() for g in GRADES):
                raise ValueError('所选目录没有 LeRobot 数据')
            base=source.parent if individual and source.name in GRADES else source
            cfg.update(lerobot_root=base.parent,repo_id=base.name,dataset_name=base.name,
                       lerobot_dataset_dir=source if individual else None)
        return cfg
    app.derive_paths=config
    def preflight(cfg,grade,source):
        try:
            info=read_json(Path(source)/'meta/info.json')
            n=int(info.get('total_episodes',0))
            return dict(grade=grade,compatible=n>0,checked_episode_count=n,error_count=0,errors=[],
                        reason=f'发现 {n} 条 LeRobot；阶段边界在执行时逐条读取，不做质检')
        except (OSError,ValueError,TypeError) as exc:
            return dict(grade=grade,compatible=False,checked_episode_count=0,error_count=1,errors=[str(exc)],reason=str(exc))
    def status(cfg):
        supported=cfg.get('robot_type')=='zerith'
        result=dict(supported=supported,available=False,phase_compatible=True,grades=[],source_grades=[],overwrite=True)
        if not supported:return dict(result,reason='当前机器人尚未适配阶段切分')
        try:
            sources=split_sources(app,cfg)
            for grade,source in sources:
                output_cfg=output_config(app,cfg,source,grade)
                check=preflight(cfg,grade,source)
                row=dict(check,source_dataset=str(source),episode_count=check['checked_episode_count'],
                         left_output=str(app.lerobot_stage_split_output_dir(output_cfg,'left_hand',grade)),
                         right_output=str(app.lerobot_stage_split_output_dir(output_cfg,'righthand',grade)))
                result['grades'].append(row)
            result.update(available=bool(sources),source_grades=[g for g,_ in sources],
                          reason='直接切分，不做质检；单条无法处理会记录并继续' if sources else '请选择已有 LeRobot 数据集，或先直接转换')
        except (OSError,ValueError,KeyError,TypeError) as exc:result['reason']=str(exc)
        return result
    app.validate_lerobot_stage_split_grade=preflight
    app.lerobot_stage_split_status=status
    html=app.HTML
    html=html.replace('    function syncSourceDatasetSelectToPath(path, hdf5Path = "") {', '''
    function syncSourceDatasetSelectToPath(path, hdf5Path = "") {
      if(['lerobot','lerobot_group'].includes(selectedSourceDatasetChoice()?.dataset_type))return;''')
    html=html.replace('      const cfg = data.config;', '''
      const cfg = data.config;
      if(['lerobot','lerobot_group'].includes(selectedSourceDatasetChoice()?.dataset_type)){
        document.getElementById('datasetName').value=cfg.dataset_name;
        document.getElementById('repoId').value=cfg.repo_id;
      }''')
    html=html.replace('      button.title = statusReason;', '''
      button.title = statusReason;
      const lerobotSelected=['lerobot','lerobot_group'].includes(selectedSourceDatasetChoice()?.dataset_type);
      const convertButton=document.querySelector('button[data-stage="lerobot"]');
      if(convertButton){convertButton.disabled=lerobotSelected;convertButton.title=lerobotSelected?'已是 LeRobot，可直接切分或按需质检':'直接转换，不做质检';}''')
    html=html.replace('    function applyDirectHdf5Dataset(choice) {', '''
    function applyDirectLerobotDataset(choice) {
      if(!choice || !['lerobot','lerobot_group'].includes(choice.dataset_type))return false;
      for(const id of ['mcapPath','hdf5Root','qcRoot','repoId'])document.getElementById(id).value='';
      document.getElementById('robotType').value=choice.robot_type||'zerith';
      document.getElementById('datasetName').value=choice.name||'';
      document.getElementById('lerobotRoot').value=choice.path;
      clearMcapDatasetPicker('已加载 LeRobot，可直接切分或按需质检');
      updateSourceDatasetScanInfo('已加载 LeRobot：'+choice.path);
      return true;
    }
    function applyDirectHdf5Dataset(choice) {''')
    html=html.replace('      if (applyDirectHdf5Dataset(selected)) {', '''
      if (applyDirectLerobotDataset(selected)) {
        await refreshStatus();return;
      }
      if (applyDirectHdf5Dataset(selected)) {''')
    html=html.replace('        hdf5_root: document.getElementById("hdf5Root").value,', '''
        lerobot_source_path: ['lerobot','lerobot_group'].includes(selectedSourceDatasetChoice()?.dataset_type) ? selectedSourceDatasetChoice().path : '',
        hdf5_root: document.getElementById("hdf5Root").value,''')
    html=html.replace('"lerobot", "split_lerobot_stages", "replay"','"lerobot", "replay"')
    html=html.replace('const selected = Boolean(fieldValue("hdf5Root") && fieldValue("datasetName"));',
                      'const selected = Boolean(fieldValue("lerobotRoot") || fieldValue("hdf5Root"));')
    html=html.replace('两阶段预检通过（${Number(item.checked_episode_count || 0)} 条）','已发现 ${Number(item.checked_episode_count || 0)} 条（未质检）')
    html=html.replace('按左右手阶段切分";', '直接切分左右手";')
    html=html.replace('将使用 --overwrite 覆盖下列输出目录，不保留旧输出备份；源 A/B/C/F 等级目录不会被修改。',
                      '直接按阶段标注切分，不执行质检；已有同名输出会备份后更新。')
    html=html.replace('各等级逐个执行；中途失败不会回滚已成功完成的其他等级。','支持全部等级；单条无法处理会记录原因并继续，最后显示成功和未完成清单。')
    app.HTML=html
