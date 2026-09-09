# ZERITH H1 PRO 网页控制台

这是一个不依赖 Flask/FastAPI 的本机网页控制台。后端运行在 ZERITH 的
Python 3.10 环境中，浏览器端不需要安装任何包。

## 已实现

- 默认不加载机器人 SDK、不连接、不接管、不运动。
- 顶部“接管控制”开关签发唯一的短时控制租约；只有持有该租约的页面才能发运动 POST。
- 每个页面都有独立实例 ID；一个页面接管后，其他页面明确显示“其他页面已接管”并锁定开关。
- 唯一工作线程持有唯一 `H1Robot`；状态读取和全部 setter 都通过同一线程串行执行。
- 双臂 14 关节、双夹爪、升降柱、腰 pitch/yaw、头 yaw/pitch 的反馈和绝对位置控制。
- 单关节动作只改变所选轴，但按 SDK 要求在每个 100 Hz 周期刷新该臂全部 7 轴，
  其余 6 轴保持动作开始时的实测位置。
- 底盘左右轮低层轮速控制；按住运动，松开、页面失焦或 350 ms 命令超时即发零速。
- 生命周期/运动操作采用原子互斥准入；忙时的新动作立即拒绝，排队超时项会被取消，
  不会在页面已报错后迟到执行。
- 左右轮虽然由 SDK 顺序下发，但任一路失败都会立即 best-effort 向双轮补发零速并清除
  watchdog 目标；若补偿回零也失败则进入 `stop_pending`，每 50 ms 重试并拒绝新的
  非零轮速，直到双轮零速成功。
- 厂商 `robot_init()`、`robot_deinit()`，以及指定的作业初始位姿。
- 推理安全区提供独立“机械臂归位”：双臂、腰和头归零，夹爪全开，升降柱保持
  按键时的实测高度，底盘保持零速；推理运行期间禁用，避免控制源冲突。
- 左腕 D405、头部 D435、右腕 D405 的 RGB 和深度实时显示。
- 六路图像共用一条 WebSocket；原始帧和网页 JPEG 均为 640×480，不裁切、不缩放。
- “语音”页签可明确选择中文（默认）或 English，既可用按钮录入单句，也可在对话框中用键盘输入文字；两种输入共用回复与受限运动执行链路。
- “语音运动控制”默认关闭；只有当前页面已接管并完成初始化后才能确认开启。明确的前进、
  后退、左右转、转身、挥手、握手、停止指令走本地快速路径，模糊动作才进入受限大模型分类。
- 相机 WebSocket 使用有界发送缓冲和最新帧背压策略，慢客户端不会造成连接反复重建。
- 目标输入在真实电机反馈到达前保持为空，之后按 SDK 步长填入当前实测位置。
- 单关节与初始位姿支持 `0.2×` 到 `2.0×` 速度倍率，默认值在页面上显示为 `1.0×`。
- 模拟机器人和模拟相机模式，可在完全不触碰硬件的情况下验收 UI/API。
- “推理”页签使用新版 WebSocket JSON 协议，可自定义远程 host/port 和
  prompt；先显式 health/metadata 重连，再做零 setter dry-run，最后才允许
  二次确认真机执行。
- 推理执行端复用本进程唯一 `H1Robot` 和唯一 CameraService；不会构造交接包中的
  `Real_Env()`，也不会启动第二个厂商 SDK 客户端。
- “真机回放”页签可递归扫描数据集目录中的 `.hdf5` / `.h5`，选择单条 episode，
  从 `state` 或 `action` 读取 23 维绝对目标，以 `0.5×`～`2.0×` 回放。

## 运行

```bash
source /home/robot/miniconda3/etc/profile.d/conda.sh
conda activate zerith
cd /home/robot/control

python -m pip install -r web_control/requirements-pi05.txt
python -m web_control.server
```

打开：

```text
http://172.16.18.43:8080
http://192.168.3.43:8080
```

服务启动后仍不会构造 `H1Robot`。点击并确认“接管控制”时才加载 SDK 和调用
`robot_connect()`；点击“初始化”时才切到 `LOW_LEVEL` 并调用会产生实体运动的
`robot_init()`。

### 完全模拟运行

```bash
cd /home/robot/control

/home/robot/miniconda3/envs/zerith/bin/python \
  -m web_control.server \
  --host 127.0.0.1 \
  --port 18080 \
  --simulate-robot \
  --simulate-cameras
```

模拟模式永远不会加载 H1 运动 SDK，也不会读取真实相机。

## 推理执行端：网页 + 代码

完整部署、命令和安全说明见 [PI05_EXECUTOR.md](PI05_EXECUTOR.md)。网页头部只显示
“推理”；左侧配置远端地址、端口、prompt、关节速度、发送频率和每个 Chunk 执行步数，
右侧集中放置急停、断开推理连接和显式重新连接。参数语义为：

| 参数 | 默认 | 有效值 |
|---|---:|---:|
| 双臂关节速度限幅 | 30 deg/s | 大于 0 的有限数 |
| 控制/发送频率 | 30 Hz | 大于 0 的有限数 |
| 每个 Chunk 执行步数 N | 30 | 1..50 的整数 |

服务端每次固定返回 50 步；执行端每包执行前 N 步，完成后重新采集最新状态和三路
图片，再同步请求下一包。推理等待期间保持上一条位置目标，不提前预取旧状态对应的
下一包。N 不是总执行
步数，不限制 Chunk 数量，也没有累计执行步数上限；连续执行直到操作员停止、发生故障
或控制 lease 失效。双臂 14 个关节按
`radians(关节速度)/发送频率` 限制每周期目标变化；基准是上一条成功下发目标，绝不按
滞后反馈重新起算。夹爪和升降柱不经过该限幅。
网页/API 同时报告新包首步相对推理观测和实际下发前反馈的最大双臂关节差，用于区分
模型首步不连续与执行调度问题；这些诊断值不会修改动作。

除网页外，[/home/robot/control/inference_executor.py](/home/robot/control/inference_executor.py)
是正式代码入口。它不打开第二个 SDK；所有硬件相关命令都通过 8080，与网页
共享唯一 SDK owner、lease、故障锁存和急停路径。它无需浏览器，但作为执行端
使用时 8080 服务必须运行；唯一例外是只读 `metadata`，它不访问机器人或相机。

代码入口的完整基本命令：

```bash
PY310=/home/robot/miniconda3/envs/zerith/bin/python
EXECUTOR=/home/robot/control/inference_executor.py
WEB=http://172.16.18.43:8080

# 只读协议验证
$PY310 $EXECUTOR metadata --policy-host 192.168.1.154 --policy-port 9973

# 8080 中执行器的当前状态
$PY310 $EXECUTOR status --web-url $WEB

# 选择 host/port，并显式 health + metadata 重连
$PY310 $EXECUTOR reconnect --web-url $WEB \
  --host 192.168.1.154 --port 9973

# 停止并断开推理连接，不调用 robot_deinit()
$PY310 $EXECUTOR disconnect --web-url $WEB

# 一次真状态 + 三路真图的零 setter dry-run；CLI 临时取得并释放 lease
$PY310 $EXECUTOR dry-run --web-url $WEB --auto-takeover \
  --prompt '把目标物放入指定位置'

# 推荐的无浏览器完整流程：同 lease 内 reconnect -> dry-run -> start
$PY310 $EXECUTOR run --web-url $WEB --auto-takeover --prepare \
  --policy-host 192.168.1.154 --policy-port 9973 \
  --prompt '把目标物放入指定位置' \
  --steps-per-chunk 30 --control-rate-hz 30 --joint-speed-deg-s 30 \
  --confirm-motion

# 全局软件停止，不需 lease，不调用 robot_deinit()
$PY310 $EXECUTOR stop --web-url $WEB
```

prompt 也可使用 `--prompt-file /path/to/prompt.txt`。独立 `dry-run --auto-takeover`
会在验证后释放 lease；需启动真机时应使用上面的 `run --auto-takeover --prepare`，
使 reconnect、dry-run 和 start 处于同一 lease，并由 CLI 在后台 heartbeat、最终
释放 lease。网页和代码的真机执行都只有在
当前 lease、`Init_Complete(2)`、`LOW_LEVEL`、同 prompt 的当前成功 dry-run 和二次运动确认
全部成立时才能开始。`run` 必须带 `--confirm-motion`；它会跨 50 步 Chunk 连续执行，
直到操作员 stop、Ctrl+C、故障或 lease 失效，Ctrl+C 会请求软件停止。
必须由现场操作员事先人工完成厂商初始化；`--auto-takeover` 只管理 lease，
绝不自动 init/deinit。机器人处于反初始化状态时，dry-run 可完成，但 start
必定被后端拒绝。网络异常也不会自动重连后继运动。

## HDF5 真机回放

网页“真机回放”页按以下顺序操作：输入 HDF5 文件的上一级数据集目录，或从输入框的
`/data` 目录候选中选择，再点击“读取数据”。候选目录由后端递归查找标准
`dataset/episode-id/episode.hdf5` 和 `dataset/demo-id/states/aligned_joints.h5` 布局并去重，只显示数据集目录，不显示文件。
随后选择一条 episode、回放源和模式，再选择 `0.5×`～`2.0×` 速度；默认 `1.0×`。
现场现有样例目录是：

```text
/data/zerith_data/Pepsi_DailyCOrangeJuice2
```

回放前必须由当前页面接管机器人并完成 `LOW_LEVEL` 初始化。点击启动后还会弹出一次真机
确认；回放器先用 3 秒五次平滑曲线从实测姿态对齐到数据首帧。有 `timestamp/t` 的
episode 按实际采集时间在内存中重建接近 `control_frequency` 的均匀轨迹，保持首末位置
和动作时长；关节位置线性插值，夹爪按原采样时刻保持/切换。重复时间戳保留最后一帧，
时间倒退拒绝加载。随后按重建频率乘 `speed` 发送，避免把 167 ms 的动作压成 33 ms。
原始文件不修改；没有时间戳的旧数据及 aligned_joints 仍使用其声明的频率。
这是时间轴修复，不能消除原始轨迹中真实存在的快速运动；需要分别检查其速度连续性。

原有采集格式继续支持：

```text
observation/state/arm/position        action/arm/position
observation/state/effector/position   action/effector/position
observation/state/waist/position      action/waist/position
observation/state/head/position       action/head/position
observation/state/base/velocity       action/base/velocity
```

另外支持根属性 `format=icra_wbc_aligned_joints` 的转换格式，例如
`/data/sim_data/0908_newscene_test1_converted/demo_0`（也可输入其上一级目录）。
读取 `states/aligned_joints.h5` 中的 `<帧号>/state/vector` 或
`<帧号>/action/vector`，每帧 23 维，按从 0 开始的连续数字帧号排序，
使用根属性 `fps` 作为回放频率。已有夹爪值直接使用，不再二次转换。
转换数据距 SDK 边界不超过 `1e-7` 的浮点舍入误差会归一到边界，实际越界仍拒绝。

拼接顺序固定为左臂 7、左夹爪、右臂 7、右夹爪、升降柱、腰 pitch/yaw、头 yaw/pitch、
底盘 linear/angular。`action` 回放只接受根属性 `action_mode=absolute`；相对动作不会被
误当成绝对关节目标。

- “双臂动作 + 夹爪”只下发前 16 维，升降柱、腰、头不改动，底盘明确保持零速。
- “全部 23 维”下发前 21 个位置目标。当前唯一 SDK owner 必须保持 `LOW_LEVEL`，而
  数据的底盘末两维是 `(m/s, rad/s)`、低层接口需要左右轮 `(rad/s, rad/s)`；由于厂商
  没有公开/标定轮径和轮距，任何非零底盘数据会被明确拒绝，绝不做错误单位直传。
  当前样例的 `action` 底盘为全零，因此可使用 action + 全部 23 维；其 `state` 包含
  非零底盘反馈，应选择双臂模式。

加载整条数据后、发送任何目标前，后端会检查字段形状、23 维、有限数、帧数一致、
SDK 原始位置限位和底盘条件。回放期间控制租约、电机错误、连接状态持续有效；网页
推理、语音运动、手动关节和回放互斥。回放页的“紧急停止”与顶部停止都会立即设置取消
标志、双轮发零速并保持最新位置反馈，且不会调用 `robot_deinit()`。它们是软件停止，
不能替代机器人的实体急停。

## 控制生命周期

```text
页面打开
  → 只读取网页配置；H1Robot 尚不存在
  → 确认接管
  → H1Robot() + robot_connect()，只读状态
  → 确认初始化
  → switchControlMode(LOW_LEVEL) + robot_init()
  → 关节/机身/底盘控制
  → 确认反初始化
  → robot_deinit()
  → 关闭接管并销毁 H1Robot
```

如果机器人仍是 `Init_Complete`，关闭接管会被拒绝；网页不会把断网、关页或后端
异常当作自动执行反初始化轨迹的授权。浏览器租约失效时会取消尚未完成的普通插值、
保持最新关节反馈并停止底盘。

“停止”按钮会：

- 取消网页正在执行的低层插值；
- 将左右轮命令设为 0；
- 对已经由网页控制的位置电机保持最新反馈。

它不是 SDK/硬件急停，不能代替机器人实体急停。厂商说明机械臂没有制动器；紧急
断电可能导致手臂因重力下落。

## 限位策略

网页只使用 SDK V4.0 第 2.2.3 节给出的软限位，不增加 CLI 原有的 `0.02 rad`
margin，也不设置 `max-start-delta`，超界时拒绝而不是静默截断。

```text
升降       0.0 … 0.8 m
腰 pitch   0.0 … 1.3 rad
腰 yaw    -0.7 … 0.7 rad
头 yaw    -1.5 … 1.5 rad
头 pitch  -0.5 … 0.75 rad
夹爪       0.0 … 1.5 rad
双臂       逐关节使用 SDK 表中的左右非对称软限位
```

所有具体数值由 `GET /api/config` 动态生成到网页，前端不维护第二份限位表。后端仍会
拒绝 NaN/Inf、错误 ID、错误模式、未初始化、电机错误和 SDK 明确禁止的电池状态；
这些是接口前置条件，不是额外关节限位。

### 底盘为何显示左右轮轮速

双臂单关节控制只在 `LOW_LEVEL` 可用，而 SDK 的底盘线速度/角速度接口
`setChassis_high()` 只在 `HIGH_LEVEL` 可用。初始化完成时不能切模式，也不能用第二个
`H1Robot` 绕过单客户端约束。

因此本控制台长期保持 `LOW_LEVEL`，调用：

```text
setChassis_low(left,  Speed=left_rad_s)
setChassis_low(right, Speed=right_rad_s)
```

SDK 对低层轮速明确标注“无限位”，且未公开轮径/轮距，因此网页不伪造 m/s、rad/s
换算或数值上限。方向键只做等幅左右轮组合，操作者应从低轮速开始；短 watchdog 独立
保证松手/失联停车。

## 初始化、反初始化和初始位姿

“初始化/反初始化”是厂商全身生命周期动作，不是只动双臂：升降柱和双臂都会运动。

“初始位姿”在唯一 SDK 对象内直接实现，没有启动 `send_robot_command.py` 子进程：

```text
升降柱先到 0.40 m
→ 100 Hz、8 秒同步插值
  双臂 14 关节 = 0 rad
  腰 pitch/yaw、头 yaw/pitch = 0 rad
→ 双夹爪 = 0 rad（完全张开），hold_torque=True
→ 持续保持，直到操作者单独确认反初始化
```

该动作保留 `--hold-until-enter` 的持续保持语义，但目标已经改为上述全零初始姿态；
Web 页面不会把关闭连接解释为 Enter，也不会在到位后立即离开该姿态。

推理页“机械臂归位”与“初始位姿”是两套不同动作：它先把底盘双轮明确置零，再读取并
保持当时的升降柱高度，只将双臂、腰和头插值到 0，并把双夹爪张开到 0。它要求当前
页面持有控制租约、机器人已完成初始化并处于 `LOW_LEVEL`；推理运行/停止或故障锁存
期间均拒绝执行，必须先结束相应状态，防止手动归位与推理同时下发。

## 相机链路

相机开关默认关闭。开启第一路画面时才创建：

```text
CameraClient(localhost:50051, enable_depth=True)
```

关闭最后一路画面后释放 CameraClient。稳定逻辑名为：

| 网页位置 | CameraClient 实际名 | RGB | Depth |
|---|---|---|---|
| 左腕 | `rs/cam_left_wrist` | BGR → JPEG | uint16 mm → JET JPEG |
| 头部 | `rs/cam_high` | BGR → JPEG | uint16 mm → JET JPEG |
| 右腕 | `rs/cam_right_wrist` | BGR → JPEG | uint16 mm → JET JPEG |

网页只把深度副本转成伪彩色，后台保留的原始深度仍是 `uint16` 毫米数据。RGB 与深度
当前配置是 `align_to: no align`，画面并排显示不表示两个像素天然对应。

六路 JPEG 使用一条 `/api/cameras/ws` WebSocket，避免六条永久 MJPEG 连接占满常见
浏览器的 HTTP/1.1 每源连接池。后端仍保留单路 MJPEG 路由供诊断。

## 本机和远程访问

手动启动默认监听：

```text
http://172.16.18.43:8080
```

当前现场部署按操作者要求默认启用了免认证局域网访问，可直接打开上述地址。
同一局域网内任何能访问该地址的设备都可以读取相机并尝试接管机器人，因此不要把
8080 端口暴露到公网或不可信网络。

如果不希望开放局域网，也可以改回 localhost 并通过 SSH 隧道：

```bash
ssh -L 8080:127.0.0.1:8080 robot@ROBOT_IP
```

然后在操作电脑打开 `http://127.0.0.1:8080`。

其他部署若要监听局域网，后端同样强制要求 token：

```bash
H1_WEB_CONTROL_TOKEN='使用随机长字符串' \
  /home/robot/miniconda3/envs/zerith/bin/python \
  -m web_control.server --host 0.0.0.0 --port 8080
```

该服务器本身不终止 TLS；不要直接暴露到互联网。

## systemd

仓库内提供 [zerith-h1-web-control.service](systemd/zerith-h1-web-control.service)。部署后：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now zerith-h1-web-control.service
systemctl status zerith-h1-web-control.service
```

当前 unit 绑定 `0.0.0.0:8080`，同一个服务可从无线 `172.16.18.43` 和有线
`192.168.3.43` 访问，并显式启用免认证局域网访问；它只启动网页，不会自动接管或
初始化机器人，也不会为第二个地址创建另一个 SDK owner。

当前机器还安装了无需 root 的用户服务版本
`systemd/zerith-h1-web-control-user.service`。日常重启四个语音/网页服务可直接使用：

```bash
systemctl --user restart zerith-xiaoda-voice.service zerith-h1-web-control.service
systemctl --user --no-pager status zerith-chinese-asr.service \
  zerith-chinese-tts.service zerith-xiaoda-voice.service zerith-h1-web-control.service
```

语音页依赖独立的 `zerith-xiaoda-voice.service`。网页进程通过
`127.0.0.1:8765` 访问它，不导入 `xiaoda-voice` 环境的依赖，也不会再占用一次机器人
麦克风。当前服务以 `--web-only` 运行：小达唤醒词监听已关闭，仅在网页点击“录入一句”时打开麦克风。
中文和英文现在都默认打开机器人端 PipeWire 输入；当前默认设备是独立的讯飞
`XFM-DP-V0.0.18` 麦克风。中文默认走已配置的云端识别与合成 API，需要联网。
“启动本地语音”会按需加载 Paraformer + Qwen3-ASR / Qwen3-TTS，两个服务都健康
后自动切换；加载期间继续使用云端。“停止并释放显存”恢复云端，按钮不会启用开机启动。
英文模型、接口和声音保持原样。

反方向的运动调用通过网页进程在 `127.0.0.1:8766` 上的内部接口完成：语音进程不加载
H1 SDK，网页进程仍是唯一 SDK 持有者。内部控制器只接受固定动作白名单，并绑定网页的
短时控制租约；语音刷新动作不会替网页续租。底盘采用固定短时脉冲且有原有 350 ms
watchdog。前进/后退使用 1.5 rad/s、1 秒，左转使用 1.5 rad/s、3.5 秒，右转使用
1.5 rad/s、3 秒，转身使用 1.5 rad/s、8 秒且未做角度标定。挥手会先左臂后右臂，采用五次多项式缓入缓出轨迹
驱动肩旋转关节在 -0.4～0.4 rad 间以每段 1.2 秒往复五次，并在两端各停 0.5 秒。握手默认使用右臂，
依次将肩俯仰移动到 -0.4 rad、肘移动到 0.6 rad、肩俯仰移动到 -0.85 rad，停留
10 秒后平滑回到右臂七轴零位。手臂其他七轴位置关节按动作计划保持为 0，夹爪保持
原位置，底盘、腰和头部不参与这两个动作。停留阶段持续监控右臂错误标志；关节出现
错误时会立即中止，并停止向右臂继续发送后台保持目标。由于 R1 曾报告过热，10 秒
静态保持只能在故障已复位、机械无卡阻且实体急停可达时使用。
挥手每一段都会重新下发完整七轴目标，肩俯仰固定为 -0.3 rad、肘固定为 -0.9 rad；
不会再把关节跟随误差读取成下一段保持目标，因此多次摆动不会累积肩俯仰漂移。

当前部署启用了免认证局域网访问，因此同一局域网中的访问者也能启动语音会话、查看
本轮文字和回放回复。网络不完全可信时应启用 `H1_WEB_CONTROL_TOKEN`，不要继续使用
`--allow-unauthenticated-lan`。

## API 摘要

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/config` | SDK 原始限位和控制元数据 |
| GET | `/api/state` | 23 电机、模式、初始化、电池、底盘和相机状态 |
| GET | `/api/pi05/status` | 推理 phase、连接、远端、故障、延迟、chunk 与安全门禁状态 |
| POST | `/api/pi05/probe` | 只读 healthz + metadata；不读取机器人、不推理、不运动 |
| POST | `/api/pi05/reconnect` | 选择 host/port 并显式 healthz + metadata 重连；不自动恢复动作 |
| POST | `/api/pi05/disconnect` | 软件停止并关闭推理连接；不调用 deinit |
| POST | `/api/pi05/dry-run` | 三路真图 + 23 维 state 请求一次 50 步 chunk；零 setter |
| POST | `/api/pi05/start` | 同 prompt dry-run、lease、关节速度、频率、每 Chunk 步数及精确确认后开始连续真机执行 |
| POST | `/api/pi05/stop` | 全局停止推理；不要求启动页面仍持有租约，不调用 deinit |
| POST | `/api/pi05/reset-fault` | 只清故障锁存；仍须重新 probe、dry-run 和真机确认 |
| GET | `/api/replay/status` | 回放 phase、帧进度、所选 episode 和故障 |
| GET | `/api/replay/directories` | 在 `/data` 下发现标准 episode 的上一级数据集目录；不运动 |
| POST | `/api/replay/scan` | 递归检查数据集目录并返回可用 HDF5 列表；不运动 |
| POST | `/api/replay/start` | 使用当前 lease 和精确确认启动所选 state/action 回放 |
| POST | `/api/replay/stop` | 无需原 lease 的软件停止；底盘零速、位置保持、不反初始化 |
| GET | `/api/voice/status` | 小达状态与本轮对话文字 |
| GET | `/api/voice/audio/{id}.wav` | 回放一条小达回复 |
| POST | `/api/voice/start` | 按 `zh` / `en` 使用机器人本体麦克风录入单句 |
| POST | `/api/voice/finish-input` | 手动结束当前录音并立即识别 |
| POST | `/api/voice/text` | 按 `zh` / `en` 提交一条键盘文字，进入同一对话/动作链路 |
| POST | `/api/voice/cancel` | 停止当前录音、合成和本地播放并清空队列 |
| POST | `/api/voice/local-speech` | `{enabled: boolean}` 按需启停两个本地 GPU 语音服务，状态见 voice/status 的 local_speech |
| POST | `/api/voice/motion` | 使用当前控制租约显式开启/关闭语音运动；默认关闭 |
| POST | `/api/takeover` | 开启/关闭控制租约 |
| POST | `/api/heartbeat` | 续约；运动请求使用 `X-Control-Lease` |
| POST | `/api/motion/joint` | 单位置电机绝对目标；支持 `speed_scale` |
| POST | `/api/motion/chassis` | 左右轮 rad/s |
| POST | `/api/actions/init` | 厂商初始化动作 |
| POST | `/api/actions/deinit` | 厂商反初始化动作 |
| POST | `/api/actions/home` | 双臂/腰/头归零、夹爪全开、升降柱到 0.40 m；支持 `speed_scale` |
| POST | `/api/actions/arm-home` | 双臂/腰/头归零、夹爪全开、保持当前升降柱高度；支持 `speed_scale` |
| POST | `/api/stop` | 取消插值、底盘零速、当前位置保持 |
| WS | `/api/cameras/ws` | 六路图像单连接传输 |
| WS | `/api/voice/asr/ws` | 浏览器 16 kHz PCM 中文实时 partial/final |

所有产生运动的端点都是 POST，并要求当前页面的 `X-Control-Lease`。服务器不开放 CORS。

## 验证

离线测试：

```bash
cd /home/robot
/home/robot/miniconda3/envs/zerith/bin/python \
  -m unittest discover -s control/web_control/tests -v
```

覆盖 SDK 延迟加载、全部位置限位端点、生命周期、指定初始位姿、停止/保持、底盘
watchdog、语音运动开关/租约/固定时长/挥腕限位、相机生命周期与 640×480 JPEG、
推理 JSON 协议/故障锁存、HDF5 映射/回放/停止、唯一 owner、动作安全覆盖、HTTP API
和多流 WebSocket。

真实硬件已做无运动验证：

- 新后端成功加载 SDK、连接、读取 23/23 电机、模式、电池并干净释放。
- 当时实测 `UNINITIALIZED/VR`、`Deinit_Complete`、23 个电机错误码均为 0。
- 三台相机六路帧和网页 JPEG 均为 640×480；RGB 约 29.7 FPS，深度约
  28.2–29.7 FPS；关闭无错误。

实体初始化、反初始化、关节、腰头和底盘运动只能在操作者到场、2 m 清场、急停可达、
确认无 VR/遥控并逐项观察的条件下验收；自动化测试不会在无人看护时擅自移动机器人。
