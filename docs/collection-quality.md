# 数据采集质检与验收指标

[返回开发者 README 和架构图](../README.md) · [启动前](#启动前质检) · [在线采样](#在线采样质检) · [落盘前后](#落盘完整性与时序质检) · [姿态](#固定姿态验收) · [人工复核](#人工复核与三路回放) · [下游 DataQC](#下游-dataqc)

## 架构图中的模块和证据

| 架构图区域 | 何时执行 | 失败时的行为 | 开发入口和证据 |
| --- | --- | --- | --- |
| 设备与会话 | 操作者选择场景、启动任务，工作台等待原厂接单 | 检查不通过时拒绝启动。连接异常可显示 `disconnected`。 | [`device_monitor.py`](../collection_web/device_monitor.py)、[`collector.py`](../collection_web/collector.py)、`device.checks`、`meta_<会话ID>.jsonl`、原厂 `task_meta.json` |
| 遥操作与机器人链路 | A 初始化、标定、遥操、暂停及反初始化 | 固定姿态偏差显示原因。反馈已丢失或遥操故障时进入保护路径。 | [`zero_lock.py`](../teleop_zero_lock/patch/zero_lock.py)、遥操 `runtime/status.json`、录制里的 Action、State、`targets.fixed_pose` |
| 时间对齐与采样引擎 | 每个新头部 RGB 帧成为参考候选 | 丢弃不符合条件的候选并保留原因。长时间无有效帧会结束该条。 | [`timing.py`](../collection_depth_toggle/timing.py)、`collection.log`、HDF5 `/timing` |
| 完整性与诊断 | 录制流结束后，移动 UUID 目录前 | 结构校验失败时待保存或重试。结构通过的记录会落档，同时保存通过或失败的时序结论。 | [`episodes.py`](../collection_web/episodes.py#L136)、`timing_quality.json`、SQLite 的 `state` 和 `detail` |
| 三视角人工复核 | 归档后回放、评级、放弃 | 可重载失败视角。评级和放弃各有独立记录。 | [`review.js`](../collection_web/static/review.js)、`GET /api/review/<id>`、`review.json`、`audit` |
| 归档与可追溯、示教数据资产 | 采集端归档；之后由 9990 工作台扫描已归档源数据 | 下游执行逐帧解码、数值质检、可选模型复核和转换复检。 | [`EpisodeStore`](../collection_web/episodes.py)、同仓库 [`dataqc` 分支](https://github.com/Edgardcai/zerith_idata/tree/dataqc/dataqc) |

排查一条记录时建议同时保存三个值。`state=completed` 表示已归档，`timing_qc.status` 表示采样诊断，`grade` 与 `reviewed` 表示采集评级及人工复核。新归档的默认等级是 `A`，同时 `reviewed=false`。这些字段可以独立变化。9990 另外保存自己的自动结论与人工结论。

## 启动前质检

[`DeviceMonitor.snapshot()`](../collection_web/device_monitor.py#L58) 汇总设备快照。`DeviceStatusStream(status_format=3)` 请求超时为 `30 s`，退出后相隔 `0.5 s` 重订阅。25120 的五路 HTTP 请求并发执行，每路超时 `1.5 s`，本轮结束后再等 `1 s`。状态项必须是成功读取的 JSON 对象、最近接收时间小于 `4 s`，且厂商字段 `fresh` 不能是 `false`。出现多个检查同时过期时，先排查状态源与主机连接。

| 检查键 | 取值和通过条件 | 现场定位 |
| --- | --- | --- |
| `service` | 采集服务的详细 gRPC 状态流新鲜有效 | 50051、`server_fast` 进程、`last_error`。 |
| `vr` | `device.others.vr` 包含 `head_world`、`left_joystick_world`、`right_joystick_world` | Quest 已连接后比较三个字段。该检查按字段存在性判定。 |
| `motors` | `/motor` 展开后恰好 `23` 项，每项 `code=0` 且 `status="ok"` | 用 `name`、`code` 对应故障电机。少报或重复上报会影响数量检查。 |
| `communication` | `/communication_board.codes` 长度 `5` 且每个值为 `0` | 对照厂商通信板状态，确认报告的新鲜度。 |
| `cameras` | 状态流中的 `rs/cam_high`、`rs/cam_left_wrist`、`rs/cam_right_wrist` 均为 `正常` | 状态源检查通过后，仍应通过预览或 `/timing` 找具体掉帧的相机。 |
| `mode`、`init` | `/control_mode.code=0` 且 `/system_init.system_init=2` | 分别代表 VR 模式和厂商初始化就绪，固定姿态 `ready` 由遥操扩展单独计算。 |
| `battery` | 状态新鲜且 `percent` 有值 | 页面显示上报值。源码没有最小电量百分比阈值。 |
| `storage` | 源/归档目录或其父目录均可写、可遍历，在同一文件系统。剩余空间严格大于 `5 GiB` | [`Collector.preflight()`](../collection_web/collector.py#L62)，用 `stat` 对比设备号、`df` 查看容量。界面以 `GB` 显示按 1024 换算的值。 |
| `exclusive` | 数据根内没有最近 `10 s` 更新且尾部无 `采集流结束` 的 `collection.log` | 检查 `/api/preflight` 的 `external_active`、旧客户端和原厂日志。此为可见写入的侦测，录制 RPC 还有自己的独占门。 |

- **关节只读视图。** [`joint_observer.cpp`](../collection_web/joint_observer.cpp) 在收到反馈时触发写入，最短写入间隔为 `250 ms`，两类反馈都必须在 `1 s` 内。网页只展示 `joints.json` 时间戳小于 `3 s` 的数据。此处用于显示，真实姿态策略使用 `ZeroPolicy` 自己的反馈订阅。
- **接单。** [`Collector.start()`](../collection_web/collector.py#L74) 只接受 `idle`、`closed`、`rejected`，并确认已应用当前日期/场景。`_watch()` 每 `0.5 s` 核对 `task_meta.json`，要求 `mtime >= started - 1 s`，`task_id`、`task_name`、`scene_id`、`subtask_num`、`frequency`、`record_depth` 与本次匹配。连接超过 `15 s` 未见匹配文件会提示检查。内部 `transport=true` 表示已收到 gRPC 响应。网页状态中的 `accepted=true` 表示已核验接单，`connected` 还会参考已接单时的 RPC 活性。
- **停止。** `end()` 在发现任何近期写入或本会话仍有 `recording`、`saving`、`finalizing` 时拒绝结束。排查中优先保留原厂日志和 UUID，避免重发一个仍在写盘的任务。

## 在线采样质检

[`Sampler.pick()`](../collection_depth_toggle/timing.py#L207) 按头部 `cam_high/color` 新帧顺序处理。`reference_receive_ns` 在录制主机收到帧的过程中记录，关联相机和 ZCM 消息都使用同一主机的单调时钟。时间差反映该主机的**接收时间对齐**。曝光时间同步需要另外的设备证据。`timestamp/t` 仍保存头部参考帧的原始设备时间值，跨设备判断使用 `/timing` 中的主机时间。

| 检查 | 条件或参数 | 不满足时的结果 |
| --- | --- | --- |
| 参考帧 | 每个候选必须比上一个已处理主参考更新。处理时最大年龄 `100 ms`。源帧按帧号、设备时间、流会话 epoch 跟踪。 | `waiting_master` 或 `master_stale`。源数据重复、重置和无效时间进入 `source_events`。 |
| 视觉匹配 | RGB 时三路彩色流齐全。RGB-D 时三路分别还要深度。各流 `abs(receive_ns − ref) <= 20 ms`，帧身份严格前进且单条不跨 epoch。 | `camera_missing_or_skew:<相机>/<分量>`、`camera_clock_reset` 或 `camera_reused`，候选整帧跳过。 |
| 前视和等待 | 有 `60 ms` 前视，指定情况下等待到 `70 ms`。当前后都有候选时，优先能为下一头部帧留下有效匹配的组合，再选当前偏差小的帧。 | `waiting_lookahead`、`waiting_camera` 可为中间返回。到期且没有有效匹配才增加 `rejected`。 |
| 五组 State | 双臂、夹爪、底盘、腰、头均选最新的 `receive_ns <= ref` 的反馈，年龄须在 `[0,50] ms`。 | 缺数据、仅有未来数据或状态超龄，记 `state_or_action_unavailable:<通道>`。 |
| 五组 Action | 同样取 `receive_ns <= ref` 的最近控制消息。已有命令可跨帧保留，记录其真实接收时间和序列号。 | 完全没有因果上的上一条命令时拒绝。Action 不套用 State 的 `50 ms` 到期阈值。 |
| 主数值字段 | Action 和 State 的臂、爪、腰、头、底盘列宽分别为 `14, 2, 3, 2, 2`，全部是有限数。 | `invalid_robot_values:<部件>`。可选手/相机位姿缺失时可留空，对应接收时间及序列号为 `-1`。 |
| 目标采样率 | 默认 `30 Hz`。目标低于相机源频率时按节拍跳过候选并计 `intentional_rate_skip`。目标更高时以源帧为上限。 | 计划内跳帧与 `rejected` 分开记录。超高目标可能触发实际帧率或间隔警告。 |
| 活性与缓冲 | 每相机分量保留 `8` 帧历史，每消息通道 `256` 项历史。一次 `prepare()` 最长等待约 `100 ms`，循环间隔约 `2 ms`。无可对齐帧持续超过 `2 s` 后置 `discard_evt`。 | 查日志 `[Timing]`、`wait_timeouts` 和拒帧原因。数据队列的默认最大容量设为 `120` 个样本，消费者仍是原厂单写入线程。 |

**落盘定位。** 录制 `/timing` 的 `version=1`、`sampler_revision=2`。`reference_receive_ns` 为每帧参考。`cameras/rs/<相机>/<color 或 depth>/` 含 `device_timestamp_ms`、`receive_ns`、`frame_number`、`epoch`，设备时钟域保存在该流元数据。`messages/<通道>/` 含 `receive_ns` 与 `seq`。汇总属性 `sample_stats`、`source_events`、`rejection_events` 保留候选数量、拒绝原因和对应的参考帧，`action_semantics` 标明命令是最近接收的保持值。

例如一条 `cam_left_wrist/color` 超出 `20 ms` 的候选会被舍弃。下一条保存样本可能已经与前一条相隔约 `66.7 ms`，因此同一个源问题既可能产生拒帧，也可能产生落盘间隔告警。可用 `rejection_events` 的主参考帧号与相邻落盘帧核对，避免把 MP4 播放漂移当成相机采样问题。

## 落盘完整性与时序质检

[`EpisodeStore.observe()`](../collection_web/episodes.py#L412) 对日志做增量扫描，先合并当前 UUID 的进度，看到 `采集流结束` 才调用 [`validate_finished()`](../collection_web/episodes.py#L136)。归档结构检查和时序诊断具有不同结果字段。

| 层级 | 通过条件 | 常见文件或结果 |
| --- | --- | --- |
| 文件与主时间戳 | `episode_meta.json` 可读，`episode.hdf5` 可打开，主时间戳有 `N >= 1` 个有限值，`total_frames == N`。`control_frequency` 存在时必须为有限正数，属性缺失时取 `30`。 | 缺文件、文件尚未写完和最终帧数不匹配会阻止完成。看 `episodes.detail.error`。 |
| 轨迹形状 | Action 和 State 的全部十个主数组各为 `[N, 对应列宽]`，元素有限。 | `字段不完整：<路径>`。先核对 HDF5 维度和 schema，再排查上游消息。 |
| RGB / 深度 | 三路 RGB 数据集首维等于 `N`。`depth_recorded` 为真时三路深度都必须有 `N` 帧且每帧缓冲非空。 | 8090 检查长度和指定深度缓冲。每帧 RGB/深度可解码性在 9990 验证。 |
| 外部 MP4 | `videos/rs/` 有三路对应 `.mp4`，且每个至少 `32` 字节。 | 8090 检查文件落地。完整解码及 MP4 实际帧数等于 `N` 在 9990 验证。 |
| 阶段信息 | 对 `step_index` 只返回 `end_frame_id >= start_frame_id` 的条目。`completed_subtasks < total_subtasks` 产生告警。 | `completed_steps`、`total_steps`、`steps`。连续覆盖与右、左手归属见下游阶段规则。 |
| 有效帧率 | 有持续时长时 `actual_hz = (N−1)/(t_last−t_first)`，以秒为单位。`N > 1` 且实际低于 `0.9 × 目标频率` 时提示。 | 目标 `30 Hz` 对应 `27 Hz` 告警线。`timestamp` 组属性 `unit=ms` 或主时间中位数大于 `10¹¹` 时先除以 `1000`。 |

[`timing_quality()`](../collection_web/episodes.py#L36) 对时间序列复核如下。

| 指标 | 采集端规则 | 证据 |
| --- | --- | --- |
| 间隔、重复、倒退 | 主时间相邻差必须为正，且应不超过 `1.5 / 目标Hz` 秒。`30 Hz` 对应 `50 ms`，`15 Hz` 对应 `100 ms`。 | `duplicate_count`、`backward_count`、`gap_count`、`max_interval_ms`。 |
| 相机元数据 | `/timing` 相关列为长度 `N` 的有限向量。主机参考、各流帧号和设备时间严格递增，同流 epoch 固定。主参考相机的原始时间向量与 `timestamp/t` 完全一致。 | `cameras/rs/...`、`reference_receive_ns`。 |
| 相机与状态对齐 | 各颜色/深度流相对参考最大差 `<=20 ms`。五组 State 年龄在 `[0,50] ms`。Control 和 State 的消息时间非负、序列号 `>=1`，且都不能使用未来消息。 | `max_camera_skew_ms`、`max_state_age_ms`、`messages/...`。 |
| 源与候选记录 | `sample_stats.rejected > 0`、`clock_or_stream_reset > 0`、`invalid_source_time > 0` 均判为时序不合格。计划跳帧及仅被滤除的重复通知不单独触发失败。 | `sample_stats`、`source_events`、HDF5 `rejection_events`。 |
| 格式兼容 | V1 全部满足为 `passed`，任一不满足为 `failed`。老文件无 `/timing` 且基础时间检查均通过时为 `legacy_unverified`，基础检查有异常仍为 `failed`。 | `timing_quality.json`。未知版本或字段损坏检查 `reasons`。 |

V2 由独立的 8080 推理录制使用，走 `inference_timing_quality()`。该格式检查相机和状态年龄在 `[0,100] ms`、命令时间不晚于参考、相机序号不倒退、采集完整且无开始后缺样，并报告重复相机帧和 `hardware_synchronized=false`。读报告时先确认版本。

### 从文件复跑检查

对已结束并归档的 episode 执行。第一条命令读现存结果，第二条使用同一版本的校验函数重新计算。密集 RGB 或深度检查应在下载或复制后的数据上进行。

```bash
EP='/data/zerith_data/20261126_scene1/episode_000001'  # 换成一条已完成的实际目录
python3.10 -m json.tool "$EP/timing_quality.json"
PYTHONPATH="$PWD/collection_web" python3.10 - "$EP" <<'PY'
import json
import sys
from episodes import validate_finished
print(json.dumps(validate_finished(sys.argv[1]), ensure_ascii=False, indent=2))
PY
```

复跑要求当前 Python 已安装 `h5py` 和 `numpy`，并从仓库根目录执行。先看 `warnings`，再看嵌套的 `timing_qc.reasons` 和 `sample_stats`，最后持原始 HDF5 或 episode 副本定位到相机和帧。要验证保存恢复，使用 `test_episode_lifecycle.py` 的临时文件测试数据。

## 固定姿态验收

头腰轴序为 **腰 pitch、腰 yaw、头 yaw、头 pitch**。在原厂 HDF5 中取 `waist/position[:,1:3]` 与 `head/position[:]`。升降柱占 `waist/position[:,0]`。开发者应分别比较三个量：会话记录的目标、逐帧 Action、逐帧 State。

| 指标 | 遥操在线或离线条件 | 查什么 |
| --- | --- | --- |
| 反馈有效性 | 头和腰分别由总线更新，分量为有限数，电机无错误。年龄在 `[0,0.25] s`。 | 遥操 `status.json` 的 `actual_rad`、`fault`，以及 25120 的 `/motor` 与 [`ZeroPolicy.feedback()`](../teleop_zero_lock/patch/zero_lock.py#L154)。 |
| 初始化允许 | 初始化后，腰两轴距离目标 `<=0.03 rad`。头部起始反馈必须在模型限位扩展 `0.05 rad` 内，随后平滑靠近目标。 | `arm()` 和初始化异常信息。单次偏差可能来自未走完初始化插值。 |
| `ready` | 已启用、无故障、头部目标过渡完成，在打开补偿时完成补偿爬升。四轴误差各自 `<= [0.005, 0.005, 0.005, 0.021] rad`，持续 `>=1 s`。 | `reason` 例如 `moving_head_to_target`、`settling_gravity_compensation`、`checking_stability`、`ready`。厂商模式、机械稳定、VR 标定各自显示。 |
| 升降柱 | 开启固定高度后，实测与目标差 `>0.005 m` 时发出偏差告警。升降偏差不参与四轴 `ready` 计算。 | `lift_target_m`、`lift_actual_m`、`warnings`。网页任务的 `lift_height` 仅是标签。 |
| 故障与偏差 | 过期或电机异常会触发持久化的遥操故障。普通收敛、重新校验或在容差外只显示状态和偏差。 | `after_joystick()` 在原厂保护之后处理故障，必要时暂停。要区分 `phase`、`ready` 和 `fault`。 |
| 录制逐帧命令 | 以 `collection_task.json` 中 `targets.fixed_pose.target_rad` 为参考。所有帧和四轴要求 `abs(Action − 目标) <=1e-6 rad`。没有该快照的旧数据，脚本使用四个零作为默认参考。 | `commands_match_target_within_1e_minus_6`、`command_max_error_rad`。用 `target_rad` 相关字段读取非零任务的结果。 |
| 录制逐帧实测 | 四轴 State 用同一组在线角度阈值，分别报绝对最大误差、超差帧数。 | `actual_max_error_rad`、`actual_outside_target_frames`、`actual_within_limits`。核对其它臂关节是否确实有行程，并结合三路录像确认操作过程。 |
| 验收脚本的附带检查 | 已结束日志、全部发现的 Action/State 数值列长度和有限性、图像长度及首/中/末缓冲非空、阶段索引。间隔单独统计严格 `>50 ms` 的数量。 | [`analyze_recording.py`](../teleop_zero_lock/tools/analyze_recording.py)。`intervals_gt50ms` 始终是固定 50 ms 阈值，与按任务频率变化的采集端告警需分开读。 |

固定姿态验收可使用只读脚本，输出路径放到 episode 目录外。该脚本目前没有升降柱相对目标的专项汇总。复核固定高度时，从 `targets.fixed_pose.config` 读 `lift_enabled` 和 `lift_height_m`，对 Action、State 的 `waist/position[:,0]` 分别计算 `max(abs(samples − target))`，再按任务使用的阈值计算超差帧数。在线告警参考值是 `0.005 m`。

```bash
python3.10 teleop_zero_lock/tools/analyze_recording.py "$EP" --output /tmp/zerith-fixed-pose-report.json
```

2026 年 11 月 12 日留存的三条验收记录包含 `934 / 728 / 445` 帧。该次腰 pitch 设为 `0.3 rad`，其余头腰角设为零，固定升降 `0.8 m`。存档报告的头腰命令最大误差约 `1.2e-8 rad`。其中一条曾因左腕匹配缺失出现约 `66.7 ms` 间隔，姿态验收仍可通过。该组案例的非零目标是腰 pitch，头角非零覆盖见离线测试。可核对[验收记录和当时的实现提交](https://github.com/Edgardcai/zerith_idata/commit/8ebc790)。

## 人工复核与三路回放

1. [`app.py`](../collection_web/app.py) 的 `/api/review/<id>` 按已完成记录与 UUID 解析目录，返回各摄像头的媒体状态及可播放 URL。`/api/video/<id>/<视角>` 支持 Range，单次请求失败可检查 HTTP 状态、`Content-Range`、UUID 和媒体文件。源文件有多个同 UUID 的候选时先解决歧义。
2. [`review.js`](../collection_web/static/review.js) 按左腕、头部、右腕的顺序选第一个可用视角为基准，采用所有可用视频中最短的播放时长。共同就绪条件包含 `readyState >= 3` 且未在 seek。元数据请求超时 `15 s`，播放器加载检查 `30 s`。
3. 页面每 `150 ms` 调整回放。视角之间漂移超过 `40 ms` 时，非主路按基准速率的 `0.97` 或 `1.03` 追赶。超过 `250 ms` 时直接 seek。尾部容差 `40 ms`，某一路暂停或缓冲时协调其它路的状态。该逻辑调节浏览器播放器时间。
4. 人工从头部视角检查流程、遮挡和任务完成情况，从两腕核对实际抓放、对应手和接触过程，再保存 `A`、`B` 或 `F`。代码存储选择、UUID、`reviewed=true` 和时间，没有按画面内容自动推导 A/B/F 的数值阈值。
5. [`EpisodeStore`](../collection_web/episodes.py) 将评级写入 `review.json` 并更新 SQLite 审计。用 `/api/episode-location/<id>` 核对归档位置和 UUID。放弃走 `pending_deletions` 和隔离路径，分组保留数排除 `deleting`、`deleted`，原序号和审计仍能追查。

## 下游 DataQC

9990 是同仓库另一条分支。以下指标对应 `dataqc` 提交 [`ad37c8e4`](https://github.com/Edgardcai/zerith_idata/tree/ad37c8e4c361a0f3303f4eedf4a087532fbdec5c/dataqc)。若设备部署不同版本，先从该实例的工作目录核对 `git rev-parse HEAD`、报告的 `rule_version` 和缓存策略。8090 的 `/timing`、逐帧固定姿态通过线与以下规则各自有版本。

| 入口与范围 | 工程职责 |
| --- | --- |
| [`workbench.py`](https://github.com/Edgardcai/zerith_idata/blob/ad37c8e4c361a0f3303f4eedf4a087532fbdec5c/dataqc/workbench.py)、[`worker.py`](https://github.com/Edgardcai/zerith_idata/blob/ad37c8e4c361a0f3303f4eedf4a087532fbdec5c/dataqc/dataqc/worker.py) | 选择数据集、编排源数据检查、动作/类别复核、转换及复检，记录状态、缓存、输入指纹与报告。 |
| [`io.py`](https://github.com/Edgardcai/zerith_idata/blob/ad37c8e4c361a0f3303f4eedf4a087532fbdec5c/dataqc/dataqc/io.py)、[`checks.py`](https://github.com/Edgardcai/zerith_idata/blob/ad37c8e4c361a0f3303f4eedf4a087532fbdec5c/dataqc/dataqc/checks.py) | 加载标准 23 维输入、时间轴、视频、任务及阶段，运行结构、数值和视觉基础规则。 |
| [`motion.py`](https://github.com/Edgardcai/zerith_idata/blob/ad37c8e4c361a0f3303f4eedf4a087532fbdec5c/dataqc/dataqc/motion.py)、[`recorded_task.py`](https://github.com/Edgardcai/zerith_idata/blob/ad37c8e4c361a0f3303f4eedf4a087532fbdec5c/dataqc/dataqc/recorded_task.py) | 手臂跳变、腰头姿态、夹爪闭合与阶段，含四阶段桌面任务的目标姿态适配。 |
| [`depth.py`](https://github.com/Edgardcai/zerith_idata/blob/ad37c8e4c361a0f3303f4eedf4a087532fbdec5c/dataqc/dataqc/depth.py)、[`assessment.py`](https://github.com/Edgardcai/zerith_idata/blob/ad37c8e4c361a0f3303f4eedf4a087532fbdec5c/dataqc/dataqc/assessment.py) | 深度解码、可选类别与动作复核结论合并。动作细节、批量路径和类别路径分别在 `motion_review.py`、`batch_motion.py`、`yolo_gate.py`。 |
| [`quality_policy.py`](https://github.com/Edgardcai/zerith_idata/blob/ad37c8e4c361a0f3303f4eedf4a087532fbdec5c/dataqc/dataqc/quality_policy.py)、[`grade_policy.py`](https://github.com/Edgardcai/zerith_idata/blob/ad37c8e4c361a0f3303f4eedf4a087532fbdec5c/dataqc/dataqc/grade_policy.py) | 规范化旧报告及预警，保存采集等级、自动等级、人工等级的来源和最终优先级。 |
| [`direct_export.py`](https://github.com/Edgardcai/zerith_idata/blob/ad37c8e4c361a0f3303f4eedf4a087532fbdec5c/dataqc/dataqc/direct_export.py)、[`export.py`](https://github.com/Edgardcai/zerith_idata/blob/ad37c8e4c361a0f3303f4eedf4a087532fbdec5c/dataqc/dataqc/export.py)、[`portable_quality.py`](https://github.com/Edgardcai/zerith_idata/blob/ad37c8e4c361a0f3303f4eedf4a087532fbdec5c/dataqc/dataqc/portable_quality.py) | LeRobot 导出及转换完整性核验。原始来源可读时比对帧映射和来源，便携复检覆盖输出的序列、任务、向量以及视频解码。 |

### 数值、动作和阶段

| 检查 | 指标和阈值 | 结果边界 |
| --- | --- | --- |
| 结构与有限值 | State、Action 为 `[N,23]`，与时间向量行数一致，数值规则要求可计算的时间序列。状态和标准 Action 不接受 NaN/Inf。带 `action_valid` 的兼容格式仅允许明确标为未观测的动作为空，仍拒绝 Inf。 | 缺通道、形状或有效数值按结构故障处理。可选的未知 Action 单独报 `action_coverage`，相应统计可能不可判。 |
| 下游时间、帧率、时长 | `dt <= 0` 或 `dt > 0.1 s`（加 `1e-7` 容差）预警。实际帧率 `<29 fps` 预警，时长不在 `[2,40] s` 预警。 | 与 8090 按目标频率的间隔公式不同。可用具体帧号追查。 |
| 双臂跳变 | 按 Action / State 与左 / 右手分别检查，任一关节相邻步长 `>0.8 rad + 1e-7` 预警。另有 State 运动汇总指标，最大相邻变化 `>0.8 rad` 或双臂平均相邻变化 `<1e-4 rad` 预警。 | `arm_*`、`motion`、`joint` 和相应关节/帧号。 |
| 腰头姿态 | 各轴、各流分别看均值、第 1 与第 99 百分位，三者需在参考值 `±0.02 rad` 内（容差 `1e-7`）。另报逐帧超差位置。 | `posture_state`、`posture_action`。桌面任务适配和其它任务的参考值见下节。 |
| 连续静止 | 相邻两帧 State 和 Action 各 23 维最大绝对变化均 `<=0.01`，同时两帧底盘量均 `<=1e-4`，形成静止段。默认连续严格超过 `40` 帧预警。 | `stationary`、起止帧。动作有未知分量时静止判定可为 `na`。 |
| 夹爪活动 | 任一手任一有效流的活动范围 `max-min >0.05 rad` 即通过活动量检查，否则预警。 | `gripper` 汇总活动范围。活动量与成功抓取需要不同证据。 |
| 夹爪闭合 | Action 开阈 `<=0.1 rad`、闭阈 `>=0.8 rad`。State 开阈 `<=0.08 rad`、闭阈 `>=0.1 rad`。转换须连续 `2` 帧确认，初始已经闭合不计新事件。 | `gripper_sequence.detail.channels` 保留闭合帧。闭合次数仅作统计。 |
| 反馈延迟与手别 | 对同一手，在两流事件数相等时按序比较 `StateFrame−ActionFrame`，须在 `[-2,15]` 帧。阶段范围完整有效时，闭合必须在该只手所属阶段。 | 超界或错阶段预警。缺阶段或覆盖错误也需复核。独立物理反馈不可用时跳过 State 对照。 |
| 阶段边界 | `subtask_transitions` 先筛 `0 < 边界 <= N`。用于基础检查的边界必须递增、唯一且末值为 `N`。用于夹爪归属的阶段还要从 `0` 到 `N` 连续覆盖，符合声明的手别顺序。 | `stages`、`gripper_sequence`。双手桌面任务优先读取记录的四段结构。 |
| 元数据和修复 | 核对 `total_frames == N`、`control_frequency == 30`、`action_mode == absolute` 及非提示词元数据一致性。存在修复记录时连续重用/修复严格超过 `2` 帧告警。自由提示词仅保留原文。 | `metadata`、`repair_records`。日志和缓存用相应检查键索引，`prompt` 为 `na`。 |

### RGB、深度和视频

| 检查 | 指标和阈值 | 结果边界 |
| --- | --- | --- |
| HDF5 RGB | 逐帧 `cv2.imdecode(..., IMREAD_COLOR)`，三路都能完整解码且各有 `N` 帧。 | `hdf5_<相机>` 保留坏帧索引，缺流、解码错或帧数不符为硬失败。 |
| 已启用 RGB-D 的深度 | 三路各 `N` 帧。Zlib 必须能解码为一个结束正常且无多余字节的流，字节数为 `prod(orig_shape) × itemsize(orig_dtype)`。尺寸是两个正整数，类型为数值。图片方式走相应 OpenCV 解码并核对尺寸、类型。 | `depth_<相机>` 的 `stored_frames`、`decoded_frames`、`bad_frames`、`decode_errors`。 |
| 外部视频 | 三路 MP4 逐帧读取，每路成功解码数须刚好是 `N > 0`。 | `video_<相机>`。与源轨迹的帧数不一致会硬失败。 |
| 曝光 | 每帧缩到 `160×120` 灰度，均值 `<5` 或 `>250` 计入。单路超过总帧数 `5%` 预警。 | `quality_<相机>.unusable_frames`，可根据报告帧号定位三视角。 |
| 模糊和疑似冻结 | 灰度 Laplacian 方差 `<8` 的帧超过单路 `20%` 则预警。相邻灰度平均绝对差 `<0.05` 记疑似冻结。 | `blur_frames`、`possible_freeze`。疑似冻结数量记录供复核，当前不单独触发视觉质量预警。 |

### 动作复核、商品复核与等级

- **动作模型。** `motion_review.py` 从全量数值指标、有界采样轨迹、阶段和问题帧构造可引用的文本证据。检查指标一致性、动作连续性、夹爪配合、阶段完成情况、掉落风险、碰撞风险、抓取失败风险和任务未完成风险。返回项须齐全且引用已提交的 `evidence_id`。物体或接触缺少观测时，风险项保留不可观测或待复核。批量路径及签名复用见 `batch_motion.py`。
- **可选商品类别。** 开启图像复核后，每个目标手以该手夹爪的唯一 Action 闭合帧抽取同侧腕相机 `t−40`、`t`、`t+40` 帧，边界裁到有效索引。YOLO 默认置信度 `0.25`，可被逐类阈值覆盖，每手至少 `2` 个不同帧匹配声明的商品，并由图像 VLM 逐手通过。无法解析目标、缺唯一闭合、模型无该商品类别或模型调用失败时，回到缺证据/待复核处理。这个抽样入口对闭合次数有前置要求，数值报告本身不按次数判级。
- **自动与人工等级。** 结构硬失败进入 `F`，时间帧数等历史报告的归类以当前软项规范化规则为准。阶段缺失、过长静止、动作/商品复核冲突或未完成会进入待复核路径。`REVIEW` 的有效结果在界面以 `B` 待复核呈现，执行未完整时可没有自动等级。自动判定原本为 A 或 B 时，普通预警会把结果归为 B，并附上原因。旧报告还会经 `quality_policy.py` 按新规则重新解释。最终展示优先级为下游人工 > 下游自动 > 8090 采集等级。保留原始证据及每一层的等级来源。
- **转换复检。** LeRobot 输出 `meta/`、`data/`、`videos/`，记录原始 UUID、任务、阶段、来源帧映射与等级。复检核对帧索引、任务映射、23 维数据、分路视频长度及规格。源目录可访问时比对来源，只有发布包时便携复检会标明未验证来源对应。相关格式见 [DataQC 输入与输出格式](https://github.com/Edgardcai/zerith_idata/blob/ad37c8e4c361a0f3303f4eedf4a087532fbdec5c/dataqc/docs/data-formats.md)。

### 固定非零角在 9990 的判定

`motion.arm_and_posture_checks()` 的基础参考为零。当前版本中的 `recorded_task.reference()` 识别 `collection_task.json -> targets.prompt_mode=tabletop` 后，会读取 `targets.fixed_pose.config` 的四个有限角度。`recorded_task.correct()` 在内存中从两个 `[N,23]` 数组的 `17:21` 列减去目标，只用相对误差重算原 `±0.02 rad` 均值/分位数规则。报告标注 `recorded_task_policy` 和 `range_reference=deviation_from_recorded_target`。双手桌面任务明确声明左放、右放、左取、右取时还会结合 `stage_prompts` 与 `subtask_transitions` 复核手别。详见[下游桌面任务适配](https://github.com/Edgardcai/zerith_idata/blob/ad37c8e4c361a0f3303f4eedf4a087532fbdec5c/dataqc/docs/recorded-tabletop-qc.md)。

对仍采用零位参考的任务，例如有意设置 `0.3 rad` 的非桌面录制，Action 恒为 `0.3 rad` 可以通过 8090 的目标验收，9990 同一通道的均值约 `0.3 rad` 也会触发零位预警。旧部署或归档时丢失 `collection_task.json` 也可能出现这一现象。还有一种情况是桌面任务已正确读到目标，但 State 分位数超过 `0.02 rad`，而 8090 离线对头 pitch 用 `0.021 rad`，两条线仍可能给出不同结果。排查时先看报告采用的参考、实际部署提交、逐轴误差与提示词模式，分别记录两套规则的结果。当前下游主规则已移除旧真机 `lift_height` 判级，固定升降柱要看前文的高度验收。

### 修改一条质检规则时

| 改动 | 实现位置 | 应覆盖的边界 |
| --- | --- | --- |
| 采样阈值或匹配策略 | `collection_depth_toggle/timing.py` 与 `collection_web/episodes.py`。同步写入时序版本、阈值和报告解释。 | 恰好 `20 ms`、`50 ms` 的边界，超界、重复源通知、流重置、低频请求、RGB-D 缺一路，及旧版本 HDF5。对应 [`timing_self_test.py`](../collection_depth_toggle/tests/timing_self_test.py) 与 [`test_timing_quality.py`](../collection_web/tests/test_timing_quality.py)。 |
| 固定姿态与升降 | `pose_config.py`、`zero_lock.py`、`collector.py` 的快照、`analyze_recording.py`。 | 非零头/腰、固定及自由升降、故障和旧配置、横跨过渡/暂停的命令，另测模型 FK、Jacobian、回退实例。对应 [`test_policy.py`](../teleop_zero_lock/tests/test_policy.py) 和 [`test_recording_pose.py`](../collection_web/tests/test_recording_pose.py)。 |
| 归档、损坏文件和回放 | `episodes.py`、`app.py`、`review.js`。 | 未结束日志、半写 HDF5、错位帧数、重复 UUID、视频 `416` Range 边界、少一路视频、跨场景移动和中断恢复。对应 `collection_web/tests/test_episode_lifecycle.py`、`test_video_review.py` 及浏览器回归脚本。 |
| 让更多任务使用显式角度 | 下游 `recorded_task.py` 与 `motion.py`。以可验证的已应用目标作为输入，定义报告适用范围和缓存版本。当前适配范围是桌面模式。 | 旧零位、非零固定、目标缺失/无效、不同会话、`±0.02 rad` 等号两侧、四轴轴序及手工等级的保留。 |
| 下游新增规则或调整判级 | 下游 `checks.py` / `assessment.py` / `quality_policy.py` 与快照版本、报告、批量签名。 | 越界和等号、旧缓存、缺模型/缺源文件、导出后帧映射、人工与自动等级并存、元数据向后兼容。参考 [等级优先级和复核规则](https://github.com/Edgardcai/zerith_idata/blob/ad37c8e4c361a0f3303f4eedf4a087532fbdec5c/dataqc/docs/grade-priority.md)。 |
