# ZERITH H1 PRO · 推理执行端

该执行端融合在现有 8080 网页服务内。`RobotService` 继续是唯一的 `H1Robot`
owner，`CameraService` 继续是唯一的 CameraClient owner；推理代码只处理图像、JSON、
WebSocket 和安全状态机，不导入交接包原生 SDK，也不构造 `Real_Env()`。

默认推理服务器：`192.168.1.154:9973`。

## 文件与环境

正式代码：

```text
web_control/pi05_protocol.py   WebSocket JSON、JPEG、metadata/action 严格校验
web_control/pi05_executor.py   probe/dry-run/连续 chunk 执行/fault 状态机
web_control/pi05_cli.py        metadata、dry-run、连续真机执行和停止 CLI
inference_executor.py          无需浏览器的正式代码入口（复用 8080）
web_control/robot_service.py   唯一 SDK owner 内的最终动作安全桥
web_control/server.py          8080 API 和共享相机/机器人生命周期
web_control/static/*           网页“推理”页签
```

使用机器人现有 CPython 3.10：

```bash
/home/robot/miniconda3/envs/zerith/bin/python --version
# Python 3.10.19

/home/robot/miniconda3/envs/zerith/bin/python -m pip install \
  -r /home/robot/control/web_control/requirements-pi05.txt

# 仅开发/测试需要
/home/robot/miniconda3/envs/zerith/bin/python -m pip install pytest==8.4.2
```

不要使用服务端项目的 Python 3.11 uv 环境，不要复制或覆盖本机 H1/Camera SDK 的
`.so`。交接包中的二进制与本机正式 1.3.9 SDK API/ABI 不同。

## 启动 8080

```bash
cd /home/robot/control
/home/robot/miniconda3/envs/zerith/bin/python -m web_control.server \
  --host 0.0.0.0 --port 8080 \
  --pi05-server-host 192.168.1.154 \
  --pi05-server-port 9973 \
  --pi05-inference-timeout 10 \
  --allow-unauthenticated-lan
```

`0.0.0.0` 让同一服务同时通过无线 `172.16.18.43` 和有线 `192.168.3.43`
访问，不会创建第二个 SDK owner。启动服务器本身不会接管、初始化或运动机器人。
现场目前启用了免认证 LAN，可信网络
以外应去掉 `--allow-unauthenticated-lan` 并配置 `H1_WEB_CONTROL_TOKEN`。

## 网页和代码双入口

网页页签标题为“推理”。左侧集中设置推理服务器 host/port、任务模式、商品、prompt、
关节速度、发送频率和每个 Chunk 执行步数；右侧是状态与安全区，提供急停、机械臂归位、断开
推理连接和显式重新连接。“机械臂归位”保持按键时的升降柱实测高度和底盘零速，
将双臂、腰、头归零并完全张开夹爪；推理运行期间禁止使用。参数语义为：

| 参数 | 默认 | 有效值 |
|---|---:|---:|
| 双臂关节速度限幅 | 30 deg/s | 大于 0 的有限数 |
| 发送/控制频率 | 30 Hz | 大于 0 的有限数 |
| 每个 Chunk 执行步数 N | 30 | 1..50 的整数 |

服务端每次固定返回 50 步。执行端执行每包的前 N 步，完成后重新采集最新状态和三路
图片，再同步请求并执行下一包；推理期间保持上一条位置目标，不提前预取旧状态对应的
下一包。
N 不是总步数，也不限制 Chunk 数量。启动后会连续执行，直到操作员显式停止、发生
故障、控制 lease 失效，或服务端返回严格布尔值 `is_success=true`。

任务模式：

- 单手：`Grasp {item} with the {left|right} hand`，未选中的手保持推理启动时姿态。
- 双手连续：`Target: {left_item} and {right_item}. Grasp {left_item} with the left hand and then grasp {right_item} with the right hand from the shelf.`
- 双手分开：先发送左手模板，再发送右手模板。只有实际成功下发的动作中左夹爪
  `action[7] > 0.2` 连续达到 150 步才切换；中间任一步不满足都会把计数清零。
  切换时在同一个 policy session 内按当前关节速度把双臂目标逐步归零，左夹爪每步
  固定发送 `1.5`、右夹爪发送 `0`，升降柱保持切换时高度；右手阶段继续锁定左臂
  关节为零、左夹爪为 `1.5`。零位命令发出后仍持续发送并检查实测反馈：14 个关节
  需在 `0.05 rad` 内连续稳定 5 个控制周期才进入右手阶段；20 秒未收敛会锁存停止。

自定义 Prompt 输入框留空时使用模板；单手和双手连续模式可直接输入自定义内容覆盖
模板。单手自定义内容仍执行单手冻结，因此必须只含一个 `left` 或 `right` 且与下拉
选择一致。双手分开需要两个明确的阶段提示词，因此网页固定使用左右商品生成两条模板。

状态接口中的 `chunk_request_mode=after_chunk_sync` 表示包尾同步请求；
`last_chunk_first_arm_delta_from_observation_rad` 和
`last_chunk_first_arm_delta_from_feedback_rad` 分别记录新包首步相对请求观测和实际下发前
反馈的最大双臂关节差。诊断值本身不触发拒绝；实际下发前另按配置的关节速度限制
双臂 14 个目标的变化率。

腰/头诊断按 `[waist.pitch, waist.yaw, head.yaw, head.pitch]` 排列：
`last_chunk_observation_body` 是请求推理时的反馈，`last_chunk_server_body` 是服务端原始
返回值，`last_chunk_effective_body` 是最终下发目标，`last_chunk_feedback_body` 是下发前
实测位置，`last_chunk_body_hold_target` 是 session 启动时锁定的保持目标。

关节限幅的单周期上限为
`radians(joint_speed_deg_s) / control_rate_hz`。限幅基准是上一条**成功下发**的目标，
并在后续周期持续向模型目标推进；绝不使用可能滞后的实时反馈重新建立基准，避免
机械臂因目标被反馈反复拉回而下垂或不动。该限幅只作用于双臂 14 个关节，不作用于
夹爪、升降柱、腰、头或底盘。

[/home/robot/control/inference_executor.py](/home/robot/control/inference_executor.py) 是与网页等价的
代码级备用入口。它不导入厂商 SDK，涉及机器人、相机或动作的命令始终调用
8080 API，因此与网页共享同一个 `RobotService` SDK owner、lease、故障锁存和急停
路径，不会启动第二个 SDK 客户端与网页争用硬件。使用代码入口不需打开浏览器，
但作为执行端使用时 8080 服务必须正在运行；`metadata` 是唯一的例外，它只直连
推理服务器做无机器人访问的协议检查。

常用命令：

```bash
PY310=/home/robot/miniconda3/envs/zerith/bin/python
EXECUTOR=/home/robot/control/inference_executor.py

# 只读 health + metadata，不访问 8080/机器人/相机
$PY310 $EXECUTOR metadata --policy-host 192.168.1.154 --policy-port 9973

# 查看 8080 内唯一执行器的状态
$PY310 $EXECUTOR status --web-url http://172.16.18.43:8080

# 选择新的远端地址并显式执行 health + metadata 后建立连接
$PY310 $EXECUTOR reconnect --web-url http://172.16.18.43:8080 \
  --host 192.168.1.154 --port 9973

# 停止并断开推理 WebSocket；不调用 robot_deinit()
$PY310 $EXECUTOR disconnect --web-url http://172.16.18.43:8080
```

`reconnect` 只在操作员显式调用时执行；网络异常或故障后不会静默自动重连、恢复
或继续动作。

## 检查顺序与命令

### 1. healthz

本机设置了 HTTP/HTTPS/ALL proxy，而该局域网地址不在 `NO_PROXY` 中。必须直连：

```bash
curl --noproxy '*' --connect-timeout 3 \
  http://192.168.1.154:9973/healthz
```

预期输出：`OK`。

### 2. metadata（不访问机器人）

```bash
cd /home/robot/control
/home/robot/miniconda3/envs/zerith/bin/python \
  /home/robot/control/inference_executor.py metadata \
  --policy-host 192.168.1.154 --policy-port 9973
```

客户端显式使用 `proxy=None`，只发送 `{"type":"metadata"}`，严格验证完整的 23 维
state/action order、模型维度 17 和 `status_mode`。状态模式严格允许
`none / left / right / prompt`。metadata 中夹爪输入、输出模式
必须是布尔值，但可为 `true` 或 `false`；二值化或连续值处理完全由服务端负责。

也可在网页“推理”页签点击“重新连接”（显式检查 healthz + metadata），
或执行：

```bash
/home/robot/miniconda3/envs/zerith/bin/python \
  /home/robot/control/inference_executor.py reconnect \
  --host 192.168.1.154 --port 9973 \
  --web-url http://172.16.18.43:8080
```

### 3. dry-run（真实状态和三路真图，零 setter）

推荐从网页执行。先接管控制以取得短时 lease，但不要为了 dry-run 初始化机器人；
`Deinit_Complete` 状态下可以只读 21 个位置反馈。点击“执行 dry-run”后，后台只读取
状态/相机并请求一包动作，不建立 policy 运动 session，也不调用任何 setter。

CLI 等价命令（`LEASE_ID` 必须来自当前 8080 控制租约，浏览器或另一个客户端需继续
发送 heartbeat）：

```bash
cd /home/robot/control
H1_CONTROL_LEASE='LEASE_ID' \
/home/robot/miniconda3/envs/zerith/bin/python \
  /home/robot/control/inference_executor.py dry-run \
  --web-url http://172.16.18.43:8080 \
  --prompt 'Grasp AD Calcium Milk with the left hand and then grasp Dahongpao Milk Tea with the right hand'
```

不打开浏览器时，也可显式要求 CLI 暂时接管、后台 heartbeat，dry-run 结束后
释放 lease：

```bash
/home/robot/miniconda3/envs/zerith/bin/python \
  /home/robot/control/inference_executor.py dry-run \
  --web-url http://172.16.18.43:8080 \
  --auto-takeover \
  --prompt 'Grasp AD Calcium Milk with the left hand and then grasp Dahongpao Milk Tea with the right hand'
```

这个独立 dry-run 会在验证后释放 lease，不应将它作为稍后独立 `run` 的运动
门禁。不依赖浏览器的真机完整流程应使用下文的 `run --auto-takeover --prepare`，
让重连、dry-run 和 start 保持在同一 lease 中。

dry-run 必须返回：state 23 维且有限、三路 BGR 图存在、chunk `50×23` 且有限、夹爪
数值原样保留、17～20 保持、21～22 为零，并通过
`first_arm_delta_from_observation_rad` 报告首步双臂目标相对观测的最大差值。该值只诊断、
不改写动作。start 的 prompt 必须与当前成功 dry-run
验证过的 prompt 完全一致；重新连接、故障复位或更换 prompt 后需要重新 dry-run。

### 4. 真机执行

网页是首选入口。必须先由现场操作员确认实体急停可达、清空工作区，再显式接管和执行
厂商“初始化”。真机按钮还要求 `Init_Complete(2)`、`LOW_LEVEL`、当前同一 prompt 的
dry-run，并显示第二次危险确认。

CLI 命令同样有独立 `--confirm-motion` 门禁，并在运行期间每 800 ms 续租；Ctrl+C
调用软件停止，不调用 deinit：

```bash
cd /home/robot/control
H1_CONTROL_LEASE='LEASE_ID' \
/home/robot/miniconda3/envs/zerith/bin/python \
  /home/robot/control/inference_executor.py run \
  --web-url http://172.16.18.43:8080 \
  --prompt 'Grasp AD Calcium Milk with the left hand and then grasp Dahongpao Milk Tea with the right hand' \
  --steps-per-chunk 30 \
  --control-rate-hz 30 \
  --joint-speed-deg-s 30 \
  --confirm-motion
```

必须与刚才 dry-run 的完整任务计划完全相同。`--steps-per-chunk 30`
表示每包执行前 30 步，再请求下一包继续执行；它不是总执行步数。服务端没有
`is_success` 时按 false 处理；若响应提供严格布尔值且变为 true，则收到该包后、下发
其中任何动作前正常结束。仍不设置总 Chunk 或总执行步数上限；操作员可随时使用网页停止、
下面的 `stop` 命令或 Ctrl+C 结束。故障和 lease 失效也会终止执行。`--prompt` 可换成
`--prompt-file /path/to/prompt.txt`；两种方式都会去除首尾空白并限制在 1000 字符内。
也可随时执行：

```bash
/home/robot/miniconda3/envs/zerith/bin/python \
  /home/robot/control/inference_executor.py stop \
  --web-url http://172.16.18.43:8080
```

### 无浏览器的完整代码流程

机器人必须由现场操作员**事先人工完成厂商初始化**，并已确认实体急停、
清场、`Init_Complete(2)` 和 `LOW_LEVEL`。之后可用一条命令在同一 lease 内执行
`reconnect → dry-run → start`：

```bash
/home/robot/miniconda3/envs/zerith/bin/python \
  /home/robot/control/inference_executor.py run \
  --web-url http://172.16.18.43:8080 \
  --auto-takeover \
  --prepare \
  --policy-host 192.168.1.154 \
  --policy-port 9973 \
  --prompt 'Grasp AD Calcium Milk with the left hand and then grasp Dahongpao Milk Tea with the right hand' \
  --steps-per-chunk 30 \
  --control-rate-hz 30 \
  --joint-speed-deg-s 30 \
  --confirm-motion
```

`--auto-takeover` 是操作员在本次命令中的显式授权：CLI 申请 lease、在阻塞请求和
执行期间持续 heartbeat，并在结束或异常清理时释放 lease。`--prepare` 使它先对
选定 host/port 执行 health + metadata，再用同一 prompt dry-run，通过后才提交真机
start。任一环节失败都请求软件停止；它永远不自动调用 `robot_init()` 或
`robot_deinit()`。机器人仍在反初始化状态时，dry-run 可安全完成，但 start 会被后端
拒绝，不会自行初始化。

CLI 默认 `--inference-mode custom`，因此旧的单 Prompt 命令保持兼容。代码入口也可
显式使用网页相同的编排模式，例如：

```bash
# 单手：非活动侧保持启动姿态
$PY310 $EXECUTOR run --web-url http://172.16.18.43:8080 \
  --auto-takeover --prepare --confirm-motion \
  --inference-mode single --active-hand left \
  --prompt 'Grasp Coca-Cola with the left hand'

# 双手分开：左手 -> 连续150个已执行闭合动作 -> 双臂归零 -> 右手
$PY310 $EXECUTOR run --web-url http://172.16.18.43:8080 \
  --auto-takeover --prepare --confirm-motion \
  --inference-mode dual_separate \
  --prompt 'Grasp Coca-Cola with the left hand' \
  --right-prompt 'Grasp NEVER Coconut Latte with the right hand'
```

若 metadata 为 `status_mode=prompt`，每条 Prompt 必须只含一个方向单词；所以同一条
Prompt 同时包含 `left` 和 `right` 的双手连续模式会在 dry-run 前被拒绝。该模式应使用
`status_mode=none` 或服务端明确实现的双手完成规则。任何模式中收到
`is_success=true` 都按本需求结束整个推理任务；它不会只作为双手分开的阶段切换信号。
单手模式若使用固定 `left/right` 状态服务，方向必须与选中的手一致；双手分开拒绝
固定侧状态服务，只允许 `none` 或能随每条 Prompt 选边的 `prompt`，避免监控错阶段。

## 相机映射

| JSON 协议字段 | CameraService 逻辑名 | CameraClient 实际接口 | 输入 |
|---|---|---|---|
| `cam_high` | `head` | `rs/cam_high` | 640×480 uint8 BGR |
| `cam_left_wrist` | `left_wrist` | `rs/cam_left_wrist` | 640×480 uint8 BGR |
| `cam_right_wrist` | `right_wrist` | `rs/cam_right_wrist` | 640×480 uint8 BGR |

每帧先等比例缩放并黑边填充到 224×224，再直接调用
`cv2.imencode(".jpg", bgr_image)` 并 base64。没有 BGR→RGB 手工交换，也不发送深度图。
Pi consumer 与网页 viewer 共享相机生命周期；不会启动第二个 CameraClient。

## 动作映射

```text
wire 0..6   -> motor 7..13   左臂 7 关节
wire 7      -> motor 14      左夹爪
wire 8..14  -> motor 15..21  右臂 7 关节
wire 15     -> motor 22      右夹爪
wire 16     -> motor 2       升降柱
wire 17..20 -> motor 3..6    腰 pitch/yaw、头 yaw/pitch（只保持）
wire 21..22 -> 不下发        底盘线/角速度（强制零）
```

policy session 建立时，唯一 SDK owner 线程一次性读取并锁定腰/头
`state[17:21]`。每个动作步仍重新读取真实反馈用于状态上传、错误检查和诊断，但最终
始终用该 session 启动快照覆盖 `action[17:21]`，清零 `action[21:23]`，并只向位置
电机发送 `action[:21]`。这样真实反馈即使因重力短暂偏离，也不会被追认为下一步目标。

## 协议与硬件边界

- state 必须恰好 23 维且全部有限；action chunk 必须恰好 `50×23` 且全部有限。
- 三路输入图片必须存在并是 `HxWx3 uint8 BGR`；直接编码成 JPEG，不做 BGR→RGB 交换。
- 双夹爪观测值原样上传；服务端返回的夹爪动作也不做阈值化、取整或重映射。执行端仅做
  有限值和厂商 SDK `[0, 1.5]` 物理范围检查。
- 每步执行前重新读取最新反馈；腰和头 `action[17:21]` 覆盖为 policy session 启动时
  一次性采集的位置，底盘 `action[21:23]` 强制为零，最终只向 SDK 发送
  `action[:21]`。
- 模型控制的前 17 维目标仍遵守厂商 SDK 软限位；所有位置反馈只要求为有限数，不再
  对反馈设置范围或零点容差阈值。腰和头的 session 启动快照按原始反馈保持，因此
  编码器在标称零点附近出现微小负值不会阻止执行，也不会在运行中逐步追随漂移。
- 仍检查 SDK 返回的电机 error flag 和厂商明确的供电前置条件（未充电时 SOC 不低于
  10%）。非有限数、维度、SDK 范围或硬件错误仍会故障停止。
- 双臂关节速度默认 30 deg/s，只接受大于 0 的有限数；每周期按
  `radians(速度)/发送频率` 限制 14 个双臂关节相对上一条成功下发目标的变化。没有额外
  0.2 rad 锁存阈值，夹爪和升降柱不经过该速度限幅。
- 控制频率默认 30 Hz，只接受大于 0 的有限数；它同时决定发送节拍和关节速度换算出的
  单周期最大目标变化量。
- 服务端固定返回 50 步；`steps_per_chunk` 默认 30，只接受 `1..50`，每包执行 N 步后
  继续下一包，不设总 Chunk 或累计执行步数上限。
- 使用一条 WebSocket、最多一个未完成请求；显式禁用代理，不发送 `rtc.enabled`。
- 网络超时、JSON 错误、metadata 不匹配、动作/图片/状态缺失或非法、SDK 错误、lease
  失效等异常都会停止发送新动作并锁存 `fault`，关闭当前连接；不会静默自动重连、恢复
  或继续运动。
- 真机 start 必须具备当前控制 lease、成功的 metadata 与同 prompt dry-run、机器人
  `Init_Complete(2)`、`LOW_LEVEL` 和操作员明确确认。CLI 运行期间持续维持 lease。
- 推理运行期间由唯一 SDK owner 排除手动、语音及生命周期动作，避免两个控制来源同时
  下发；STOP、heartbeat、状态读取和故障复位路径保留。
- 停止或故障时请求停止推理并结束 policy session；任何路径都不自动调用
  `robot_init()` 或 `robot_deinit()`。故障不会自动清除，重新执行必须由操作员清故障、
  reconnect/probe、dry-run 并再次确认。
- 软件停止不能替代实体急停。

## 验证

```bash
cd /home/robot
PYTHONPATH=/home/robot:/home/robot/control PYTHONDONTWRITEBYTECODE=1 \
/home/robot/miniconda3/envs/zerith/bin/python -m pytest -q \
  control/web_control/tests

node --check /home/robot/control/web_control/static/app.js
```

模拟网页（不会加载真实 SDK/相机）：

```bash
cd /home/robot/control
/home/robot/miniconda3/envs/zerith/bin/python -m web_control.server \
  --host 127.0.0.1 --port 18080 --simulate-robot --simulate-cameras
```

## 现场未替代操作员确认的项目

- 代码和自动化测试没有执行厂商初始化，也没有发送任何真机动作。
- 真机初始化后所选正有限控制频率（默认 30 Hz）连续下发 21 个 setter 的持续时序、
  机械负载、急停距离、跨多个 Chunk 的长期运行和具体任务 prompt 仍需现场有人监护验收。
- 交接包路径实际为 `robot_sdk_reference/utils/real_env_sdk.py` 和
  `robot_sdk_reference/lib/proto/`；服务端核心实际名为
  `_zerith_market_json_core.py`。这些差异已审计，但没有覆盖本机正式 SDK。
- 相机硬件实测三路 RGB/Depth 均为 640×480、约 28～30 FPS。此前网页“看不到”时
  相机处于零订阅者自动关闭状态；连接时持 manager 锁的阻塞问题已修复。
