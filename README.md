# ZERITH H1 PRO 数值控制与实时状态读取

整套网页控制、机器人语音、中文 ASR/TTS、启动命令和安全边界的快速入口见：

- [CORE_GUIDE.md](CORE_GUIDE.md)

本目录提供两套独立的 Python 3.10 工具：

- `read_robot_state.py`：只连接并读取状态，不切换控制模式、不执行初始化、不发送运动命令。
- `send_robot_command.py`：对关节、末端位姿和夹爪数值进行离线校验；默认是 dry-run，只有显式授权参数齐全时才会连接并运动。

这些工具直接使用当前交付的 ZERITH H1 SDK 1.3.9，适合作为接口验证、现场单项验收和后续驱动开发的参考代码。它们不是持续运行的生产运动控制器：生产链路还需要单写入者、命令 watchdog、状态新鲜度、可取消轨迹和明确的故障恢复策略。

机械臂逐关节/末端控制、完整状态字段、三路 RGB-D 相机、深度反投影、Livox Mid-360 原生 SDK/ROS 2 读取、坐标系、时间同步、标定和故障排查的综合说明见：

- [H1_ARM_CAMERA_LIDAR_GUIDE.md](H1_ARM_CAMERA_LIDAR_GUIDE.md)

本机网页控制台（双臂、机身、底盘、初始化/反初始化和三路 RGB-D 实时画面）见：

- [web_control/README.md](web_control/README.md)

“小达”离线唤醒、中文语音识别、GPT-5.5 流式多轮对话和自然语音合成见：

- [voice_assistant/README.md](voice_assistant/README.md)

语音服务使用独立的 Python 3.12 环境，不加载 H1 SDK。语音运动默认关闭；只有网页当前
页面已接管、完成初始化并显式开启开关后，受限动作才会交给网页进程中的唯一 SDK 持有者。

## 1. 文件说明

| 文件 | 用途 | 是否可能运动 |
|---|---|---|
| `h1_sdk_common.py` | SDK延迟加载、关节表、限位、错误码和校验函数 | 否 |
| `read_robot_state.py` | 读取23个电机和机器人综合状态 | 否；只调用 `robot_connect()` |
| `send_robot_command.py` | 关节角、末端位姿、夹爪数值控制 | 默认否；带完整执行参数时会运动 |
| `tests/test_validation.py` | 限位、四元数、错误码和dry-run离线测试 | 否 |

SDK位置默认为：

```text
/home/robot/workspace/robot_station/sdk/zerith_h1/1.3.9/
  h1_sdk_v1.3.9_python3.10
```

可以使用 `--sdk-root` 或环境变量 `ZERITH_H1_PYTHON_SDK_ROOT` 覆盖。

## 2. 环境准备

### 2.1 开机自动修复ZCM共享内存权限

机器人端的 `SDKService` 以root身份运行时，可能把
`/dev/shm/zcm/ipcshm/default` 创建为普通用户不可写。目录中的systemd unit会在
`robotd.service` 启动后等待共享内存出现，修复当前文件权限，并给目录设置默认ACL，
使SDKService后续重建的文件也允许 `robot` 用户读写：

```bash
sudo install -o root -g root -m 0644 \
  systemd/zerith-zcm-permissions.service \
  /etc/systemd/system/zerith-zcm-permissions.service
sudo systemctl daemon-reload
sudo systemctl enable --now zerith-zcm-permissions.service
```

检查结果：

```bash
systemctl status zerith-zcm-permissions.service
getfacl /dev/shm/zcm/ipcshm/default
```

H1 Python SDK 是 CPython 3.10 二进制，不能用系统默认 Python 3.12。

```bash
source /home/robot/miniconda3/etc/profile.d/conda.sh
conda activate zerith

cd /home/robot/control
python --version
```

预期：

```text
Python 3.10.x
```

在机器人主机上运行时，不需要传 `--robot-address`。工具会使用：

```python
robot = H1Robot()
```

SDK文档、类型桩和厂商示例对远程构造参数的写法并不完全一致，因此本工具虽然保留 `--robot-address`，但远程方式在现场确认前不要作为默认用法。

## 3. 读取机器人实时状态

### 3.1 读取一次

```bash
python read_robot_state.py --count 1
```

这段程序只执行：

```text
加载SDK → H1Robot() → robot_connect() → 调用get*状态接口
```

不会执行：

```text
switchControlMode()
robot_init()
setArm_low/high()
robot_deinit()
```

因此它适合先做“无运动状态读取”验收。

### 3.2 持续读取

每秒读取1次，直到按 `Ctrl+C`：

```bash
python read_robot_state.py --count 0 --rate 1 --compact
```

以10 Hz读取100帧并保存为JSONL：

```bash
python read_robot_state.py \
  --count 100 \
  --rate 10 \
  --compact \
  --output /home/robot/workspace/control/h1_state.jsonl
```

输出文件每行是一帧完整JSON，便于后续按时间解析。

### 3.3 可选传感器

读取遥控器：

```bash
python read_robot_state.py --include-joystick
```

只有安装灵巧手时才添加：

```bash
python read_robot_state.py --include-dexterous-hands
```

只有MAX版安装六维力传感器时才添加：

```bash
python read_robot_state.py --include-force
```

H1 PRO通常没有MAX版六维力传感器，因此默认不调用 `getForceSensorState()`，避免反复打印“不支持”错误。

## 4. 状态输出字段说明

### 4.1 生命周期与模式

```json
{
  "connected": true,
  "control_mode": {"value": 0, "name": "UNINITIALIZED/VR"},
  "init_state": {"value": 2, "name": "Init_Complete"}
}
```

控制模式：

| 数值 | 含义 |
|---:|---|
| 0 | `UNINITIALIZED`，默认VR/遥操作控制模式 |
| 1 | `LOW_LEVEL`，SDK底层电机控制 |
| 2 | `HIGH_LEVEL`，SDK高层位姿控制 |
| 3 | `GRAVITY_COMPENSATION_LEVEL`，带重力补偿的低层控制 |

初始化状态：

| 数值 | 含义 |
|---:|---|
| 0 | 未初始化 |
| 1 | 初始化中 |
| 2 | 初始化完成 |
| 3 | 反初始化中 |
| 4 | 反初始化完成 |
| 5 | 错误状态 |

状态读取工具不会改变这些值。

### 4.2 23个电机状态

每个电机都会输出：

```json
{
  "motor_id": 7,
  "position": 0.123,
  "speed": 0.001,
  "torque": 0.02,
  "kp": 0.0,
  "kd": 0.0,
  "error_flag": 0,
  "errors": []
}
```

ID和顺序：

```text
0  左轮                  1  右轮
2  升降                  3  腰pitch             4  腰yaw
5  头yaw                 6  头pitch
7～13   左臂7关节        14 左夹爪
15～21  右臂7关节        22 右夹爪
```

双臂关节位置为 `rad`，速度为 `rad/s`，力矩为 `N·m`。SDK指南的电机参数总表把升降位置标为 `m`，但 `setWaist_low()` 文字章节又写成 `rad`，两处不一致；本工具不做单位换算，原样输出SDK反馈，升降单位仍需现场确认。

`error_flag` 为位标志，工具同时把非零位翻译到 `errors`，包括断联、过压、欠压、过热、堵转、过流、通信丢失、过载、电池低、超速、编码器、驱动器和温度错误等。

### 4.3 底盘速度

```json
{
  "chassis_speed": {
    "wheel_actual": [0.0, 0.0],
    "algorithm": {
      "linear_m_s": 0.0,
      "angular_rad_s": 0.0
    }
  }
}
```

- `wheel_actual[0/1]`：左右轮实际速度。
- `linear_m_s`：底盘前后线速度。
- `angular_rad_s`：底盘偏航角速度。

这里没有底盘世界坐标、x/y位置或里程计位姿。

### 4.4 双臂末端位姿

```json
{
  "arm_relative": {
    "left": {
      "position": [0.0, 0.0, 0.0],
      "rotation_qx_qy_qz_qw": [0.0, 0.0, 0.0, 1.0]
    }
  }
}
```

- `position`：`[x,y,z]`，单位 `m`。
- `rotation_qx_qy_qz_qw`：四元数 `[qx,qy,qz,qw]`。
- 这是SDK的“相对电机零位”坐标，不是世界坐标。

IMU四元数顺序与末端位姿不同：

```text
末端位姿：qx, qy, qz, qw
IMU：     w,  x,  y,  z
```

### 4.5 其他状态

工具还会读取：

- 机器人名称、类型、固件/硬件/软件版本。
- 电池SOC、温度和充放电状态。
- IMU的RPY、角速度、线加速度和四元数。
- 头部、头相机、左右腕相机的相对位姿。
- 夹爪控制模式。
- 底部固定杆状态。
- 高层控制器状态及进度。

每个SDK调用失败时会写入顶层 `errors`，而不是用零值冒充有效数据。

## 5. 数值控制的安全门禁

`send_robot_command.py` 默认只校验，不加载SDK：

```bash
python send_robot_command.py joint \
  --arm left \
  --delta 0 0 0 0 0 0 0.02
```

输出必须包含：

```text
DRY-RUN ONLY: no SDK was loaded and no robot connection or movement occurred.
```

真实执行需要同时写：

```text
--execute
--confirm-motion I_UNDERSTAND_H1_WILL_MOVE
```

程序还有以下门禁：

- `--execute` 前必须通过离线维度、有限数、限位和幅度校验。
- 当前机器人必须处于 `Uninit` 或 `Deinit_Complete`，否则拒绝自动接管。
- 电池SOC低于文档规定的10%时拒绝控制。
- 被控制的关节/夹爪反馈必须成功且 `error_flag == 0`。
- 必须经过至少3秒倒计时。
- 关节目标必须位于文档软限位内，并额外留出本地margin。
- 目标与初始化后实测位置差不能超过 `--max-start-delta`。
- 低层关节控制强制使用文档建议的100～500 Hz插值。
- 高层末端目标必须靠近当前实测位姿。
- 正常完成后调用 `robot_deinit()`；异常中断不会擅自执行回收轨迹。

## 6. 发送7个关节角

### 6.1 关节顺序

左臂：

```text
left_shoulder_pitch
left_shoulder_roll
left_shoulder_yaw
left_elbow
left_wrist_roll
left_wrist_yaw
left_wrist_pitch
```

右臂顺序相同，只是名称前缀换成 `right_`。

文档软限位：

| 关节 | 左臂ID/范围(rad) | 右臂ID/范围(rad) |
|---|---|---|
| 肩pitch | 7：`-2.7～1.5` | 15：`-2.7～1.5` |
| 肩roll | 8：`-0.3～2.0` | 16：`-2.0～0.3` |
| 肩yaw | 9：`-2.9～2.9` | 17：`-2.9～2.9` |
| 肘 | 10：`-1.3～1.5` | 18：`-1.3～1.5` |
| 腕roll | 11：`-2.9～2.9` | 19：`-2.9～2.9` |
| 腕yaw | 12：`-1.0～1.0` | 20：`-1.0～1.0` |
| 腕pitch | 13：`-1.0～1.0` | 21：`-1.0～1.0` |

程序默认再向内留 `0.02 rad`。

### 6.2 推荐首次测试：相对移动一个腕关节

先dry-run：

```bash
python send_robot_command.py joint \
  --arm left \
  --delta 0 0 0 0 0 0 0.02 \
  --duration 3 \
  --rate 100
```

现场满足安全条件并明确授权后，真实执行命令是：

```bash
python send_robot_command.py \
  --execute \
  --confirm-motion I_UNDERSTAND_H1_WILL_MOVE \
  joint \
  --arm left \
  --delta 0 0 0 0 0 0 0.02 \
  --duration 3 \
  --rate 100
```

这表示读取初始化后的7个实际关节角，仅给左腕pitch增加 `0.02 rad`，其他关节目标保持当前值。

### 6.3 发送绝对7维目标

先把下面占位值替换成你根据当前反馈确认过的目标：

```bash
python send_robot_command.py joint \
  --arm left \
  --target Q1 Q2 Q3 Q4 Q5 Q6 Q7 \
  --duration 3 \
  --rate 100
```

SDK的 `setArm_low()` 一次只接收一个电机ID，不接收整条7维数组。程序内部每个周期按以下逻辑拆分：

```python
for motor_id, position in seven_joint_targets:
    command = Motor_Control()
    command.Position = position
    command.Speed = 0.0
    command.Torque = 0.0
    command.KP = -1.0
    command.KD = -1.0
    robot.setArm_low(motor_id, command)
```

`KP/KD=-1` 表示不在本工具中覆盖厂商参数。不要在没有厂家调参依据时直接修改KP、KD或Torque。

### 6.4 双臂到指定关节姿态、闭合双夹爪并保持升降柱0.40米

本次确认的两臂目标相同：

```text
[肩pitch, 肩roll, 肩yaw, 肘, 腕roll, 腕yaw, 腕pitch]
[0,       0,      0,       -1.20, 0,      0,      0.98]
```

图片中的腕pitch `1.05 rad` 超过SDK软限位 `1.00 rad`，并略高于文档硬限位约 `1.047 rad`，因此这里使用已确认的 `0.98 rad`。

2026-08-27已在当前H1 PRO实机读取并由操作员核对物理状态：左右夹爪反馈约为 `0.02～0.03 rad` 时处于闭合状态。因此当前机器的闭合目标使用 `0.02 rad`；数值增大朝张开方向运动。这个实机标定结论优先于此前引用的示例方向。

先执行不会连接机器人的dry-run：

```bash
python send_robot_command.py dual-joint \
  --left-target  0 0 0 -1.20 0 0 0.98 \
  --right-target 0 0 0 -1.20 0 0 0.98 \
  --gripper-position 0.02 \
  --lift-position 0.40 \
  --duration 8 \
  --rate 100 \
  --hold-until-enter
```

现场安全条件、控制权和当前状态全部确认后，真实执行命令是：

```bash
python send_robot_command.py \
  --execute \
  --confirm-motion I_UNDERSTAND_H1_WILL_MOVE \
  dual-joint \
  --left-target  0 0 0 -1.20 0 0 0.98 \
  --right-target 0 0 0 -1.20 0 0 0.98 \
  --gripper-position 0.02 \
  --lift-position 0.40 \
  --duration 8 \
  --rate 100 \
  --hold-until-enter
```

执行逻辑：

```text
读取左右臂、双夹爪和升降柱实际位置
→ 检查错误码、软限位和各目标相对起点的距离
→ 将升降柱移动到0.40 m并检查是否在±0.01 m内到位
→ 在8秒内以100 Hz插值左右臂
→ 每个周期依次下发左臂7关节和右臂7关节
→ 双臂到位后闭合左右夹爪到0.02 rad
→ 持续以100 Hz保持双臂目标，并周期性刷新升降柱和夹爪目标
→ 操作员按Enter后执行robot_deinit()
```

`--hold-until-enter` 只允许在交互式终端使用。保持期间必须让该进程持续运行，且不能切回VR或启动另一个运动控制程序。终端显示“Targets reached”后，按 `Enter` 才会正常退出保持并调用 `robot_deinit()`；反初始化同样会移动升降柱和双臂。若终端意外关闭、SDK断连或按 `Ctrl+C`，程序不会自动执行这段恢复运动，此时应按现场安全流程处理；紧急情况使用实体急停。

升降目标单位按SDK电机状态和厂家示例解释为米。程序直接采用厂家文档范围 `0–0.8 m`（包含两个端点），不再增加端点余量或单次高度变化限制。目标下发前仍会读取升降电机错误码；运动过程中会持续检查连接并等待实际反馈进入目标的 `±0.01 m` 范围。

两臂的14条SDK关节指令是在同一个100 Hz周期中依次发送，时间上近似同步，但SDK没有提供14关节原子下发接口。

该命令默认允许每个关节相对初始化后反馈最多移动 `1.5 rad`。如果程序因 `max-start-delta` 拒绝执行，说明实际起点离目标过远；不要继续增大门限，应先检查实时关节值、碰撞路径并拆分成经过审核的中间姿态。

本命令的目标接口只控制双臂、双夹爪和升降柱，不发送腰部俯仰、腰部旋转、头部或底盘指令。但 `robot_init()` 和 `robot_deinit()` 的厂商生命周期动作仍会带动升降机构和手臂。

### 6.5 保存的作业初始姿态

SDK的 `robot_init()` 只有无参数接口，不能把厂商内置初始化轨迹改写成自定义关节姿态。本工具将上面的目标保存为“作业初始姿态”：先完成厂商 `robot_init()`，再运动到双臂目标、闭合双夹爪并将升降柱保持在 `0.40 m`。

只做离线校验：

```bash
python send_robot_command.py initial-pose
```

真实执行：

```bash
python send_robot_command.py \
  --execute \
  --confirm-motion I_UNDERSTAND_H1_WILL_MOVE \
  initial-pose
```

该预设固定使用双臂 `[0, 0, 0, -1.20, 0, 0, 0.98] rad`、双夹爪 `0.02 rad`、升降柱 `0.40 m`、8秒轨迹和100 Hz下发。终端显示 `Targets reached` 后持续保持；按 Enter 才会执行厂商 `robot_deinit()` 并离开该姿态。

## 7. 发送机械臂末端位姿

末端控制使用：

```python
robot.setArmMove_high(
    arm,
    target_pose,
    0.0,
    0.0,
    duration,
    True,
)
```

### 7.1 推荐首次测试：相对平移1厘米

dry-run：

```bash
python send_robot_command.py pose \
  --arm left \
  --relative-position 0.01 0 0 \
  --duration 3
```

真实执行时在命令前加入：

```text
--execute --confirm-motion I_UNDERSTAND_H1_WILL_MOVE
```

程序会先调用 `getHandRelative()` 读取当前位姿，在当前 `x` 上增加 `0.01 m`，并保持当前四元数。XYZ正方向必须结合机器人坐标系和现场无障碍方向确认，不能把“x增加”默认理解为世界坐标前进。

### 7.2 发送绝对XYZ和四元数

应先从 `read_robot_state.py` 取得当前 `arm_relative`，再基于它生成附近目标：

```bash
python send_robot_command.py pose \
  --arm right \
  --position X Y Z \
  --quaternion QX QY QZ QW \
  --duration 3
```

程序默认限制：

- 与当前位置距离不超过 `0.05 m`。
- 与当前姿态角距离不超过 `0.25 rad`。
- 四元数模长必须接近1。

文档没有给出完整可达工作空间，因此不能只因XYZ是有限数就认为目标一定可达。

## 8. 发送夹爪数值

夹爪ID：

```text
左夹爪：14
右夹爪：22
```

文档给出的硬范围是 `0～1.5 rad`。本工具保留 `0.02 rad` margin，只允许 `0.02～1.48 rad`，并且单次变化默认不超过 `0.15 rad`。

推荐先做相对小步dry-run：

```bash
python send_robot_command.py gripper \
  --arm left \
  --delta 0.05
```

真实执行：

```bash
python send_robot_command.py \
  --execute \
  --confirm-motion I_UNDERSTAND_H1_WILL_MOVE \
  gripper \
  --arm left \
  --delta 0.05
```

工具固定使用 `is_hold_torque=True`，不开放无限制MIT夹爪控制。当前H1 PRO实机已确认约 `0.02 rad` 为闭合端、数值增大朝张开方向；更换机器人或夹爪后仍须重新小步标定。

## 9. 程序真实执行顺序

关节、位姿或夹爪命令的生命周期是：

```text
离线校验
→ H1Robot()
→ robot_connect()
→ 读取模式、初始化状态、电池
→ 安全倒计时
→ switchControlMode(LOW_LEVEL/HIGH_LEVEL)
→ robot_init()
→ 重新读取当前关节或末端状态
→ 校验目标与当前状态的差
→ 下发目标
→ 读取最终反馈
→ robot_deinit()
```

必须特别注意：

- `robot_init()` 会让升降机构运动并抬起手臂；HIGH_LEVEL还会把手臂缓慢收至腰部两侧。
- `robot_deinit()` 也会让升降机构运动并把手臂收到底盘位置。
- SDK控制时不能同时使用VR遥操作；VR控制时不能同时使用SDK。
- 如果当前状态是 `control_mode=0`、`init_state=2`，程序不会自动抢控制权或自动反初始化，而是拒绝执行。应先通过经批准的操作流程进入反初始化完成状态。

正常命令结束会执行 `robot_deinit()`。如果运动中发生SDK异常或按 `Ctrl+C`，程序不会自动执行反初始化，因为回收轨迹同样可能造成碰撞；此时应先根据现场情况使用物理急停，再按批准的恢复流程处理。

如果确认工作空间安全，需要单独反初始化：

```bash
python send_robot_command.py \
  --execute \
  --confirm-motion I_UNDERSTAND_H1_WILL_MOVE \
  deinit
```

这条命令本身也会造成运动。

## 10. 与采集/训练数据的对应关系

当前转换后的23维state/action顺序是：

```text
0～6    左臂7关节
7       左夹爪
8～14   右臂7关节
15      右夹爪
16      升降
17      腰pitch
18      腰yaw
19      头yaw
20      头pitch
21      底盘线速度
22      底盘角速度
```

23维数组不是一个SDK函数的参数，必须拆分：

```text
左臂7维  → 7次 setArm_low(7～13, ...)
左夹爪   → setGripper_low/high(14, ...)
右臂7维  → 7次 setArm_low(15～21, ...)
右夹爪   → setGripper_low/high(22, ...)
腰/头/底盘 → 各自SDK接口
```

如果使用 `/action/end/position` 的每臂7维末端数据，则应走高层位姿接口；不能同时把同一帧关节目标和末端目标下发给同一只机械臂。

训练转换器目前使用 `action[t] = state[t+1]`，所以23维动作更接近下一帧绝对状态目标，不应直接当成速度或增量。任何模型输出进入真机前还需要固定schema版本、关节名称、单位、限幅、插值和watchdog。

## 11. 安全检查清单

真实执行前逐项确认：

- 操作人员已接受培训，知道物理急停位置。
- 机器人四周至少2 m无人员、货架、线缆和其他障碍。
- 机器人没有持物，夹爪和机械臂不会与腰部/底盘自碰撞。
- VR、遥控、teleop、其他SDK示例和其他运动进程全部停止。
- 只有当前程序是SDK写入者。
- 电机错误码为0，电池SOC不低于10%。
- 首次测试只使用一个腕关节或一个夹爪的小幅相对动作。
- 通过本地终端或稳定的 `tmux` 会话运行，避免SSH断开。
- 已先运行完全相同参数的dry-run并核对输出。

不要直接把厂商的 `h1_low_level.py` 或 `h1_high_level.py` 当成首个验收命令；它们会顺序测试多个子系统。

## 12. 常见问题

### `zsh: command not found: python3.10`

使用conda环境：

```bash
source /home/robot/miniconda3/etc/profile.d/conda.sh
conda activate zerith
python --version
```

### `zcm_create: Assertion ret == ZCM_EOK failed`

先确认没有其他SDK进程，并检查共享内存文件：

```bash
ps -ef | grep -E 'state_monitor|h1_|read_robot_state|send_robot_command'
ls -l /dev/shm/zcm/ipcshm/default
```

如果该文件由root运行的 `SDKService` 创建为 `root:root 0644`，当前用户会缺少写权限。保留root作为所有者，把文件组改成robot并增加组写权限：

```bash
sudo chgrp robot /dev/shm/zcm/ipcshm/default
sudo chmod 664 /dev/shm/zcm/ipcshm/default
ls -l /dev/shm/zcm/ipcshm/default
```

预期权限类似：

```text
-rw-rw-r-- 1 root robot ... /dev/shm/zcm/ipcshm/default
```

`SDKService` 重启后可能重新创建该文件，届时需要重新检查权限。工具会在构造 `H1Robot` 前检查这个文件，权限错误时直接给出说明，避免进入ZCM的C层断言并产生core dump。不要同时启动多个拥有 `H1Robot` 的客户端进程。

### 模式切换被拒绝

控制模式只能在机器人处于未初始化或反初始化完成状态时安全切换。本工具不会为了切模式而自动执行未知的恢复动作。

### PRO版读取六维力报错

H1 PRO没有MAX版六维力传感器。不要使用 `--include-force`。

## 13. 离线验证

语法检查和单元测试不会连接机器人：

```bash
source /home/robot/miniconda3/etc/profile.d/conda.sh
conda activate zerith
cd /home/robot/workspace/control

python -m py_compile \
  h1_sdk_common.py \
  read_robot_state.py \
  send_robot_command.py

python -m unittest discover -s tests -v
```

## 14. 资料依据与未验证事实

代码依据：

- ZERITH H1 PRO系列SDK开发指南 V4.0。
- H1 SDK 1.3.9 Python类型声明 `lib_h1_sdk_python.pyi`。
- H1 SDK 1.3.9 C++头文件 `H1_Robot.hpp`、`config.hpp`。
- 厂商低层、高层和状态读取示例。

已确认的1.3.9绑定差异：类型桩把多个电机ID参数标为 `int`，但实际pybind二进制要求 `EtherCAT_Motor_Index`。本工具在调用 `getMotorState/getArmState/getGripperState/setArm_low/setGripper_*` 前统一把0～22整数转换为该枚举。

仍需真机逐项确认：

- 所有关节方向、零点与实际姿态对应关系。
- 升降状态的最终单位；文档章节存在 `m/rad` 不一致。
- 其他机器人或更换夹爪后的开合方向与零点（当前机器已确认约 `0.02 rad` 为闭合端）。
- 末端相对坐标系的原点、轴方向和完整可达工作空间。
- SDK失联、急停和异常中断后的真实保持/回收行为。
- Python 100～500 Hz在目标主机上的实际调度抖动；生产低层控制应优先由C++驱动承担。
