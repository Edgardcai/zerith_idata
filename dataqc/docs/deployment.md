# 部署与运维

在项目根目录完成 README 中的环境和配置准备后，可生成适用于当前路径的 systemd 用户服务。

```bash
# 仅生成预览，不改变现有服务
.venv/bin/python deploy/install_services.py
# 安装并启动
.venv/bin/python deploy/install_services.py --apply
```

可以通过 `--python /path/to/python` 指定已有环境，通过 `--runtime /path/to/state` 指定运行目录。
`--port` 配置监听端口；`--real-root`、`--sim-root` 配置真机和仿真扫描目录；
`--binary-path` 可指定 ffmpeg/ffprobe 所在目录。默认值仍适用于本机 8091 服务。
生成的服务固定使用当前项目路径，不依赖开发机器原目录。
如需退出登录后继续运行，由机器管理员为部署用户配置 systemd linger。

```bash
systemctl --user status dataqc-web dataqc-worker
journalctl --user -u dataqc-web -u dataqc-worker -n 80
curl http://127.0.0.1:8091/healthz
curl http://127.0.0.1:8091/auto/api/health
```

更新代码后：

```bash
.venv/bin/python deploy/restart_when_idle.py --check-only
.venv/bin/python deploy/restart_when_idle.py
```

工具等待活动任务完成后再重启两个服务，不重写配置。`--wait-seconds` 控制最长等待时间，
`DATAQC_SERVICE_URL` 可以覆盖检查地址。

## 本地文件与备份

- `runtime/config/`：模型配置及 API 文件，按敏感配置管理。
- `runtime/var/grade-selections/`：人工等级与采集等级基线，必须随状态备份。
- `runtime/var/db/`、`runtime/var/runs/`：任务与审查结果。
- `runtime/var/manual/`：传统指标及 VLM 结果缓存。
- `runtime/exports/`、用户设置的外部输出目录：转换结果。

这些内容及 `.venv/`、原始 HDF5/视频、YOLO 权重都不进入 Git。备份数据库时先确保无运行任务，
使用数据库一致性备份或停止服务后复制完整运行目录。恢复源码与恢复运行状态是两件独立的操作。

## 配置

模型名称默认 `gpt-5.6-terra`，以部署方 API 实际支持的名称为准。
`api_file` 只从指定文件读取 API 地址与密钥，不读取其他应用的登录凭据。
开启类别识别前配置 `yolo_path`；未启用时不会因此阻断动作指标复核。

## H200 部署

项目目录 `/srv/projects/caizj/dataqc`，监听 `9990`。该机器通过 `gpu-shell`
管理预约与设备隔离，禁止绕过调度器屏蔽的用户服务总线。使用监督进程在调度器
允许的 scope 内运行网页和 worker，退出 SSH 后继续运行，子进程异常退出会自动重启。

```bash
cd /srv/projects/caizj/dataqc
# 通过服务器批准的入口刷新预约环境，再启动服务。
gpu-shell r-d8b61c44 --command '/srv/projects/caizj/dataqc/runtime/config/start-service.sh'
```

部署时的预约允许 H200-7 和 H200-0；掩码 `7,0` 在当前隔离环境中无法初始化 CUDA，
选择已获准的子集 `0` 后正常。启动脚本检查预约是否仍允许 GPU 0；不允许时保留
调度器的掩码，不扩大设备访问范围。预约变更、到期或服务器重启后，遵循调度器策略重新启动。

日志与 PID 位于 `runtime/var/services/`。检查：

```bash
curl http://127.0.0.1:9990/healthz
curl http://127.0.0.1:9990/auto/api/health
cat runtime/var/services/supervisor.pid
tail -n 60 runtime/var/services/web.log runtime/var/services/worker.log
```

`DATAQC_REAL_ROOT`、`DATAQC_SIM_ROOT` 分别设为
`/srv/data/datasets/public/zerith_data`、`/srv/data/datasets/public/zerith_sim_data`。
API 凭据与 YOLO 权重只放在该部署的 `runtime/` 下，不提交 Git。
更新时先确认没有活动任务，向监督进程发送 SIGTERM，更新源码后重新运行上述启动命令。

## 2026-09-15 同步版（20260915.2）

目录命名不参与高度判定：读取每条数据内的目标；无独立目标时检查首帧 Action 高度保持。
旧报告因目录高度规则失败、已有人工 A/B 的记录，在人工确认或转换入口按当前规则重新做数值检查，
通过后使用新的确认快照继续转换，不修改目录名、不改写原始 HDF5、不直接删除失败项。
真实数值异常仍需要处理；其他旧规则报告仍按原有版本检查要求执行。
本版也包含 HDF5 回放窄屏布局、筛选空结果显示修复。

### 192.168.6.47

项目 `/srv/projects/caizj/dataqc`，端口 `9990`，同样通过 GPU 调度器启动：

```bash
gpu-shell --command /srv/projects/caizj/dataqc/runtime/config/start-service.sh
```

真机扫描根目录 `/srv/data/shared/zerith_data/real`；仿真扫描根目录
`/srv/data/shared/zerith_data/sim`。当前这里主要是已转换的 LeRobot 数据，HDF5 列表为空时需载入源数据目录。
API 使用该用户已有 `/srv/projects/caizj/api.txt`；类别识别默认关闭，权重路径保持服务器已有模型。
FFmpeg/ffprobe 使用 `/srv/projects/caizj/agilex_qc_env/bin`。

该节点通过已分配的 GPU 物理 minor 与 NVML UUID 映射设置 CUDA_VISIBLE_DEVICES，避免隔离后索引重排导致设备不可见；不会扩大预约设备范围。

## 20260915.3：直接转换与可选转换后质检

采集数据页的 LeRobot 默认直接转换全部源 episode，按现有人工等级优先分组，未评级放入 UNRATED。
不要求 HDF5 质检报告，不运行派生裁剪、数值规则、模板审核或转换后质检；保留原始动作、状态、视频、任务文本和帧数。
写出 Parquet/视频所需的读取、编码和字段一致性要求仍适用。已有同名输出保留 .previous 备份。
输出 meta/conversion.json 标注 quality_check=not_run，不生成虚假的通过报告。

新增“LeRobot 转换后质检”按钮，对当前 LeRobot 输出按原有规则检查，报告写入各输出目录的 qc_report.json；
日志列出失败项和报告路径。质检失败不删除输出、不改写原始 HDF5 或人工等级。
来源一致性和源 HDF5 元数据/内嵌图像核对仍需保留来源文件。自动质检流水线仍维持显式质检语义。

## 20260915.4：阶段切分不再强制质检

“按左右手阶段切分”默认不运行完整数据复检，也不运行切分输出的质量复检。
已有失败报告不影响切分；输出标注 quality_check=not_run，不声称通过质检。
按现有阶段边界提取 LeRobot Parquet 和视频，质量检查仍由独立质检入口执行。
保留可执行切分所需的阶段位置、区间和文件可读性要求；已有发布目录仍先备份后更新。

## 20260915.5：全部等级、逐条处理、独立 LeRobot 切分

手动按钮明确分为“直接转换 LeRobot”“直接切分左右手”“LeRobot 质检（可选）”。
按钮不读取质检结果，不要求旧 HDF5 的 total_subtasks/completed_subtasks 属性，支持 A/B/C/F/UNRATED。
单条读取、视频处理或阶段标注错误会记录并继续处理其他数据；成功输出重新连续编号。
每个输出包含 batch_report.json，工作台任务日志和 runtime/var/manual-last-export.json、manual-last-split.json
给出成功数量和未完成原因；部分完成不会宣称全部完成。没有成功记录时不替换原有输出。

切分以当前 LeRobot Parquet、视频和阶段映射为准，不依赖原 HDF5；旧映射缺少阶段时，才尝试从来源 HDF5 补充边界。
源文件缺失时，可选质检仍检查 LeRobot 的数值和视频，并明确标记来源核对未执行。
手动处理不会执行质检；单独选择的自动质检流水线保留其显式检查和中断恢复语义。
