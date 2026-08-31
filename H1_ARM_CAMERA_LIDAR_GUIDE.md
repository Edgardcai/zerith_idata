# ZERITH H1 PRO 机械臂、RGB-D 相机与激光雷达完整开发指南

> 适用对象：当前这台 ZERITH H1 PRO、H1 SDK 1.3.9、Ubuntu 24.04、`zerith` Python 3.10 环境
> 编写/核验日期：2026-08-28
> 资料范围：本机 SDK、厂商示例、两份随机器交付的 PDF、本机实际服务/网络/相机配置，以及 Livox 官方 SDK2/ROS 驱动文档
> 核查方式：除一次只读相机取帧和既有只读机器人状态验证外，没有切换机器人模式，没有调用 `robot_init()`，没有发送运动命令

阅读路线：第 0–7 节是机械臂控制与状态；第 8–11 节是三路 RGB-D 相机；第 12–16 节是 Livox Mid-360；第 17–19 节是坐标、时间同步和软件架构；第 20–24 节是排错、验收、资料与速查。

## 0. 先看结论

这台机器的三类数据/控制链路是**相互独立的**，不能把它们都当成 `H1Robot` 的成员函数：

```text
机械臂/机身状态与运动
  Python/C++ 程序
    → H1 SDK 1.3.9
      → 本机 ZCM 共享内存
        → SDKService / robotd
          → EtherCAT 电机、IMU、电池等

三路 RGB-D 相机
  Python/C++ 程序
    → CameraClient
      → gRPC :50051（发现、信令、相机配置/内参）
      → WebRTC H.264（彩色）+ DataChannel（压缩深度）
        → 相机服务
          → 头部 D435 + 左右腕部 D405

激光雷达
  Livox Viewer 2 / Livox SDK2 / livox_ros_driver2
    → enp3s0，172.31.200.1/24
      → Ethernet/UDP
        → 底盘中心 Livox Mid-360
```

最重要的接口边界如下：

| 需求 | 正确入口 | 是否会运动 |
|---|---|---:|
| 读取 23 个电机、IMU、电池、末端/相机相对位姿 | `H1Robot.get*State()` / `/home/robot/control/read_robot_state.py` | 否 |
| 控制单个机械臂关节 | `setArm_low(motor_id, Motor_Control)` | 是 |
| 控制机械臂末端位姿 | `setArm_high()` 或 `setArmMove_high()` | 是 |
| 获取彩色/深度图与内参 | `CameraClient` | 否 |
| 获取点云/雷达 IMU | 独立 Livox SDK2 或 ROS 驱动 | 否 |
| 获取腕部/头部相机在 SDK 相对零位坐标系中的位姿 | `getHandCameraRelative()` / `getHeadCameraRelative()` | 否；文档要求机器人已初始化 |

请特别区分：

- `robot_init()` 是机器人**运动初始化**，会让升降柱和机械臂运动。
- `robot_deinit()` 是机器人**反初始化/回收运动**，同样会运动，不是“无动作地断开连接”。
- `CameraClient.start()` 只建立相机通信，不初始化机器人，不会让机械臂运动。
- H1 SDK 没有 `getLidar()`、`getPointCloud()` 一类接口；Mid-360 必须走 Livox 自己的链路。

## 1. 本机路径、环境和当前已核实状态

### 1.1 关键路径

| 内容 | 路径 |
|---|---|
| 本文档 | `/home/robot/control/H1_ARM_CAMERA_LIDAR_GUIDE.md` |
| 当前安全控制/状态工具 | `/home/robot/control/` |
| SDK 原始交付目录 | `/home/robot/H1_SDK_1.3.9/` |
| 已安装 Python 运动 SDK | `/home/robot/workspace/robot_station/sdk/zerith_h1/1.3.9/h1_sdk_v1.3.9_python3.10/` |
| Python 相机 SDK | `/home/robot/H1_SDK_1.3.9/camera_sdk_python/` |
| C++ 相机 SDK | `/home/robot/H1_SDK_1.3.9/camera_sdk_cpp/` |
| 产品手册 | `/home/robot/ZERITH H1 PRO产品使用手册 V2.0.pdf` |
| SDK 开发指南 | `/home/robot/ZERITH H1 PRO系列SDK开发指南 V4.0.pdf` |
| 相机服务配置 | `/etc/robot/cams_realsense.yaml` |
| 雷达网口 DHCP 配置 | `/etc/dnsmasq.d/lidar-net.conf` |

### 1.2 Python 环境

H1 运动绑定和相机绑定都是面向 **CPython 3.10 x86_64** 的二进制，不应使用系统的其他 Python 版本：

```bash
source /home/robot/miniconda3/etc/profile.d/conda.sh
conda activate zerith
python --version
```

预期为 Python 3.10.x。后文命令默认已经激活该环境。

### 1.3 当前本机实测摘要

| 项目 | 2026-08-28 核实结果 | 含义 |
|---|---|---|
| H1 Python SDK | 已安装且可导入 | 可读状态、可在满足门禁时控制运动 |
| ZCM 权限服务 | 已配置开机自动修复 | 避免 SDKService 重建共享内存后普通用户不可写 |
| 相机服务 | `*:50051` 正在监听 | 机内应使用 `localhost:50051` |
| RGB-D 设备 | 头部 D435、左右腕部 D405 | 三路均配置 color+depth 640×480@30 |
| 相机帧 | 已只读验证三路 BGR 与深度帧 | BGR 为 `uint8 (480,640,3)`；深度为 `uint16 (480,640)`，单位 mm |
| 雷达主机网口 | `enp3s0=172.31.200.1/24`、1 Gbit/s、链路 up | 符合 Mid-360 独立网段设计 |
| 雷达驱动 | 尚未安装 Livox SDK2/ROS 驱动 | 当前不能直接从 Python/ROS 订阅点云 |
| ROS | 未安装 ROS 1/ROS 2 | 使用 ROS 路径前需要安装 |
| 雷达候选 DHCP 地址 | 曾出现 `172.31.200.232` | **尚不能证明就是雷达，禁止写死** |

## 2. 机械臂控制总览

### 2.1 四种控制模式

| 枚举 | 值 | 用途 | 主要发送接口 |
|---|---:|---|---|
| `UNINITIALIZED` | 0 | 默认 VR/遥操作或未由 SDK 接管 | 不发送 SDK 运动 |
| `LOW_LEVEL` | 1 | 逐电机关节 MIT 混合控制 | `setArm_low()`、`setMotorControl_low()` |
| `HIGH_LEVEL` | 2 | 末端位姿/内置 IK 与轨迹 | `setArm_high()`、`setArmMove_high()` |
| `GRAVITY_COMPENSATION_LEVEL` | 3 | 带重力补偿的低层控制 | 仍使用各类 `set*_low()` |

`GRAVITY_COMPENSATION_LEVEL` 不是自动安全的“零力矩拖动模式”。它仍是低层控制，仍需合理命令、增益、限位和故障处理。

### 2.2 初始化状态

| `InitState` | 值 | 含义 |
|---|---:|---|
| `Uninit` | 0 | 未初始化 |
| `Initializing` | 1 | 初始化运动中 |
| `Init_Complete` | 2 | 初始化完成 |
| `Deinitializing` | 3 | 反初始化运动中 |
| `Deinit_Complete` | 4 | 反初始化完成 |
| `Error_State` | 5 | 初始化状态机错误 |

### 2.3 正确生命周期

对于需要运动的 SDK 程序，可靠顺序是：

```text
构造 H1Robot()
→ robot_connect()
→ 确认连接、电池、电机错误、控制权和工作空间
→ 确认 InitState 仅为 Uninit(0) 或 Deinit_Complete(4)
→ switchControlMode(LOW_LEVEL/HIGH_LEVEL/...)
→ robot_init()                         # 会产生运动，阻塞
→ 重新读取真实起点
→ 插值/规划并下发运动
→ 读取反馈判断是否真正到位
→ 操作者确认回收路径安全
→ robot_deinit()                       # 也会产生运动，阻塞
```

关键约束：

- 模式只应在 `Uninit` 或 `Deinit_Complete` 时切换。
- `robot_init()` 和 `robot_deinit()` 都是阻塞式运动接口，执行期间不要并发调用其他运动接口。
- `robot_init()` 会移动升降机构并抬臂；在 HIGH_LEVEL 下还会把手臂缓慢收至腰侧。
- `robot_deinit()` 会移动升降机构并把手臂收到底盘位置。
- 正常反初始化后 SDK 会回到默认的 `UNINITIALIZED/VR` 模式。
- VR、遥控、厂商示例和自编程序不能同时控制；整个系统只能有一个运动写入者。
- 异常退出时不能机械地在 `finally` 中调用 `robot_deinit()`，因为未知障碍环境下的回收轨迹本身可能碰撞。

只读取普通状态时，通常只需要：

```text
H1Robot() → robot_connect() → get*()
```

不需要切换模式，也不需要 `robot_init()`。厂商文档对末端/相机相对位姿接口写了“需初始化”的前提；即使某些固件在未初始化时偶尔返回数据，也必须检查返回的 `ok`，并把厂商约束当作正式契约。

## 3. 电机 ID、机械臂关节顺序和限位

### 3.1 全部 23 个电机 ID

| ID | 名称 | 子系统 | 常用单位/说明 |
|---:|---|---|---|
| 0 | 左轮 | 底盘 | 速度/电机状态 |
| 1 | 右轮 | 底盘 | 速度/电机状态 |
| 2 | 升降柱 | 升降 | 厂商总表及示例按 m；个别文字章节有 m/rad 冲突 |
| 3 | 腰 pitch | 腰部 | rad |
| 4 | 腰 yaw | 腰部 | rad |
| 5 | 头 yaw | 头部 | rad |
| 6 | 头 pitch | 头部 | rad |
| 7 | 左肩 pitch | 左臂 | rad |
| 8 | 左肩 roll | 左臂 | rad |
| 9 | 左肩 yaw | 左臂 | rad |
| 10 | 左肘 | 左臂 | rad |
| 11 | 左腕 roll | 左臂 | rad |
| 12 | 左腕 yaw | 左臂 | rad |
| 13 | 左腕 pitch | 左臂 | rad |
| 14 | 左夹爪 | 夹爪 | rad，当前实机约 0.02 为闭合端 |
| 15 | 右肩 pitch | 右臂 | rad |
| 16 | 右肩 roll | 右臂 | rad |
| 17 | 右肩 yaw | 右臂 | rad |
| 18 | 右肘 | 右臂 | rad |
| 19 | 右腕 roll | 右臂 | rad |
| 20 | 右腕 yaw | 右臂 | rad |
| 21 | 右腕 pitch | 右臂 | rad |
| 22 | 右夹爪 | 夹爪 | rad，当前实机约 0.02 为闭合端 |

### 3.2 双臂 7 维命令的固定顺序

```text
0 shoulder_pitch
1 shoulder_roll
2 shoulder_yaw
3 elbow
4 wrist_roll
5 wrist_yaw
6 wrist_pitch
```

所以：

```text
左臂 7 维 → ID 7,8,9,10,11,12,13
右臂 7 维 → ID 15,16,17,18,19,20,21
```

夹爪 ID 14/22 不包含在机械臂的 7 维数组里。

### 3.3 关节软限位与厂商硬限位

| 关节 | 左 ID / 本地软限位 rad | 右 ID / 本地软限位 rad | 厂商硬限位约 rad | 参考转轴 |
|---|---|---|---|---|
| 肩 pitch | 7 / `[-2.7, 1.5]` | 15 / `[-2.7, 1.5]` | `[-2.792, 1.571]` | Y |
| 肩 roll | 8 / `[-0.3, 2.0]` | 16 / `[-2.0, 0.3]` | 左 `[-0.524,2.094]`；右 `[-2.094,0.524]` | X |
| 肩 yaw | 9 / `[-2.9, 2.9]` | 17 / `[-2.9, 2.9]` | `[-2.967,2.967]` | Z |
| 肘 | 10 / `[-1.3, 1.5]` | 18 / `[-1.3, 1.5]` | `[-1.518,1.571]` | Y |
| 腕 roll | 11 / `[-2.9, 2.9]` | 19 / `[-2.9, 2.9]` | `[-2.967,2.967]` | X |
| 腕 yaw | 12 / `[-1.0, 1.0]` | 20 / `[-1.0, 1.0]` | `[-1.047,1.047]` | Z |
| 腕 pitch | 13 / `[-1.0, 1.0]` | 21 / `[-1.0, 1.0]` | `[-1.047,1.047]` | Y |

当前 `/home/robot/control` 会在软限位内再保留默认 `0.02 rad` 余量。夹爪厂商范围是 `[0,1.5] rad`，当前工具允许 `[0.02,1.48] rad`。升降柱使用 `[0,0.8] m`。

硬限位只是机械极限，不能作为日常命令范围；软限位也不能证明某个多关节组合不会自碰撞。

### 3.4 扭矩规格不是命令限值

厂商关节表中的峰值/额定扭矩约为：

- 肩 pitch/roll：75/25 N·m。
- 肩 yaw/肘：27/9 N·m。
- 三个腕关节：9/3 N·m。

这些是电机规格，不是“可以直接写进 `Motor_Control.Torque`”的安全值。前馈力矩需要动力学、负载、姿态、保护策略和厂商调参依据。

## 4. LOW_LEVEL：控制各个关节

### 4.1 单关节接口

机械臂专用接口：

```python
ok = robot.setArm_low(motor_id, command)
```

通用电机接口：

```python
ok = robot.setMotorControl_low(motor_id, command)
```

`command` 是 `Motor_Control`：

```python
command = sdk.Motor_Control()
command.Position = q_des       # rad
command.Speed = dq_des         # rad/s
command.Torque = tau_ff        # N·m，前馈项
command.KP = kp
command.KD = kd
```

它是 MIT 风格的混合控制，而不是位置/速度/力矩三选一。厂商给出的控制关系是：

```text
tau_ref = kp * (q_des - q_actual)
        + kd * (dq_des - dq_actual)
        + tau_ff
```

当前绑定中 `Motor_Control()` 的默认值为：

```text
Position=0, Speed=0, Torque=0, KP=-1, KD=-1
```

`KP=-1`、`KD=-1` 表示不由本次命令覆盖相应参数。当前安全工具显式使用 `Speed=0`、`Torque=0`、`KP=KD=-1`，不向用户开放未经验证的力矩和增益调参。

### 4.2 Python 绑定的一个重要差异

交付的 `.pyi` 把部分电机 ID 标成 `int`，但 SDK 1.3.9 的实际 pybind 二进制要求 `EtherCAT_Motor_Index`：

```python
mid = sdk.EtherCAT_Motor_Index(7)
ok = robot.setArm_low(mid, command)
```

直接传普通 `7` 可能发生类型错误。当前工具通过以下辅助函数统一处理：

```python
from h1_sdk_common import motor_index
mid = motor_index(sdk, 7)
```

### 4.3 没有 7/14 关节原子接口

`setArm_low()` 一次只发一个电机。控制一只手臂必须在每个控制周期内依次调用 7 次；双臂依次调用 14 次。它们在一个周期内“近似同步”，但不是总线层的原子 14 轴命令。

### 4.4 正确的轨迹下发方式

不能把远目标只发送一次，也不能从假定的零位直接跳变。基本方法是：

1. 初始化完成后重新读取全部目标关节的 `Position_Actual`。
2. 检查所有反馈成功、错误位为 0、值有限、时间新鲜。`Motor_Information` 本身没有 timestamp/sequence，新鲜度需用宿主采样单调时间、心跳和额外的停滞检测来判断。
3. 检查目标处于软限位并且相对起点变化不过大。
4. 以 100–500 Hz 从实测起点插值到目标；厂商示例常用 500 Hz。
5. 每个周期持续刷新所有受控关节，而不是只刷新正在变化的一个轴。
6. 周期性检查连接、错误码和命令超时。
7. 到达目标后继续读反馈，按允许误差和稳定时间判断到位。

线性插值的核心是：

```python
alpha = step / steps
q_cmd = [q0 + alpha * (q1 - q0) for q0, q1 in zip(start, target)]
```

现有实现可参考 `/home/robot/control/send_robot_command.py` 的 `send_joint_command()`。Python 的线程调度并不是严格实时；生产级 100–500 Hz 控制应优先使用经过测量的 C++ 控制循环，并加入 watchdog、状态新鲜度、单写入者和可取消轨迹。若确实需要硬实时保证，还要配置 RT 调度/实时内核并实测最坏周期抖动；普通 `std::thread` 本身不等于实时线程。

### 4.5 厂商示例的 KP/KD 只可作线索

厂商 C++ KPKD 示例包含：

```text
肩和肘：KP=250, KD=5
腕 roll：KP=30, KD=1
腕 yaw/pitch：KP=40, KD=1
```

这不是经过当前机器、当前负载和所有姿态验证的安全调参范围。没有厂商明确确认时，不要把这些数值复制到真机业务程序。

## 5. HIGH_LEVEL：按末端位姿控制机械臂

### 5.1 末端位姿结构

```python
pose = sdk.ArmEndPose()
pose.position = [x, y, z]          # m
pose.rotation = [qx, qy, qz, qw]  # 四元数
```

四元数顺序必须是 `[qx,qy,qz,qw]`，且应归一化。也可以构造 `ArmPose`；其中 x/y/z 单位是 m，roll/pitch/yaw 单位是 rad，再调用：

```python
end_pose = robot.armPoseToArmEndPose(arm_pose)
```

### 5.2 两个高层接口

实时/连续跟随接口：

```python
ok = robot.setArm_high(sdk.ArmAction.LEFT_ARM, pose)
```

内置规划运动接口：

```python
ok = robot.setArmMove_high(
    sdk.ArmAction.LEFT_ARM,
    pose,
    eef_velocity,      # m/s；0 使用默认值，指南称默认约 0.25
    eef_acceleration,  # m/s²；0 使用默认值
    duration,          # s；0 自动，指南称默认约 6 s
    block,             # 是否阻塞后续动作/动作队列
)
```

指南把默认末端加速度写成 `1600 m/s²`，数值异常大；它可能是文档/单位问题。在厂商澄清前，使用 `0` 让内部默认处理，不要据此手工设置 1600。

厂商指南还说明：`eef_velocity` 或 `eef_acceleration` 使用非零值时，`duration` 参数无效；当前 CLI 把前两项传 0，所以按 duration 路径调用。

`block` 控制后续动作/动作队列是否被阻塞；厂商资料没有明确保证 Python 函数会一直阻塞到物理到位，不能仅凭 `block=True` 推断返回时已经到位。setter 返回 `True` 也只表示 SDK 接受调用。无论返回值和 `block` 如何，都要结合 `getHighLevelState()`、`getHandRelative()`、超时和误差判断。

### 5.3 末端坐标系

SDK 指南定义：

- 右手系。
- `+X` 朝机器人正前方。
- `+Y` 朝机器人左侧。
- `+Z` 竖直向上。
- 每只手臂的零点是该臂所有关节处于物理零位时的夹爪中心。
- 这是相对机械结构/电机零位的坐标，不是地图或世界坐标。

所以，`x+0.01` 不能未经现场确认就理解成“在世界中向前 1 cm”。完整应用必须明确 `T_base_hand`、底盘位姿以及地图坐标变换。

### 5.4 读取末端反馈

```python
ok, actual_pose = robot.getHandRelative(sdk.ArmAction.LEFT_ARM)
if not ok:
    raise RuntimeError("left hand pose unavailable")

xyz = list(actual_pose.position)     # m
quat = list(actual_pose.rotation)    # qx,qy,qz,qw
```

高层动作还可读取：

```python
ok, state = robot.getHighLevelState()
```

状态大意为：0 未激活、1 激活中、2 空闲、3 执行中、4 完成、5 错误；进度字段为 0–100。最终到位判断应结合高层状态、末端反馈、超时和容差。

## 6. 读取各关节和机器人状态

### 6.1 最推荐的现成只读命令

```bash
cd /home/robot/control

# 读取一次完整状态
python read_robot_state.py --count 1

# 10 Hz 持续读取，Ctrl+C 停止
python read_robot_state.py --rate 10 --count 0 --compact

# 保存 100 帧 JSONL
python read_robot_state.py \
  --rate 10 \
  --count 100 \
  --compact \
  --output /home/robot/control/h1_state.jsonl
```

这个工具只构造 SDK、调用 `robot_connect()` 和状态 getter；不切模式、不初始化、不反初始化、不发送运动。

但是“只读”不等于“可以和另一个 SDK 客户端并发”。它仍会构造一个 `H1Robot` 实例并连接底层服务；按照当前 SDK 的单客户端约束，不要在运动控制程序、VR/遥控控制或其他 `H1Robot` 进程运行时启动它。生产程序应由唯一的机器人进程持有 `H1Robot`，再通过线程安全快照或本机 IPC 把状态分发给只读消费者。

### 6.2 单关节状态接口

机械臂专用：

```python
ok, state = robot.getArmState(motor_id)
```

通用：

```python
ok, state = robot.getMotorState(motor_id)
```

`state` 是 `Motor_Information`：

```python
state.Position_Actual  # 机械臂 rad
state.Speed_Actual     # rad/s
state.Torque_Actual    # N·m
state.KP_Actual
state.KD_Actual
state.Error_flag       # uint16 bitmask；0 才是无错误
```

必须先判断 `ok`，不能把调用失败时的默认对象/旧对象当成零状态。

### 6.3 最小只读 Python 示例

```python
from h1_sdk_common import load_sdk, make_robot, motor_index

sdk = load_sdk()
robot = make_robot(sdk)

if not robot.robot_connect():
    raise RuntimeError("robot_connect() failed")

joint_ids = list(range(7, 14)) + list(range(15, 22))

for motor_id in joint_ids:
    ok, st = robot.getArmState(motor_index(sdk, motor_id))
    if not ok:
        print(motor_id, "read failed")
        continue
    print({
        "id": motor_id,
        "q_rad": float(st.Position_Actual),
        "dq_rad_s": float(st.Speed_Actual),
        "tau_nm": float(st.Torque_Actual),
        "kp": float(st.KP_Actual),
        "kd": float(st.KD_Actual),
        "error": f"0x{int(st.Error_flag):04x}",
    })
```

从 `/home/robot/control` 运行该片段，才能直接导入本地辅助模块。

### 6.4 电机错误位

`Error_flag` 是位集合，不是单一枚举：

| bit | 含义 |
|---:|---|
| 0 | 电机断联 |
| 1 | 过压 |
| 2 | 欠压 |
| 3 | 过热 |
| 4 | 堵转 |
| 5 | 过流 |
| 6 | 通信丢失 |
| 7 | 过载 |
| 8 | 电池低 |
| 9 | 超速 |
| 10 | 编码器错误 |
| 11 | 制动过压 |
| 12 | 驱动器错误 |
| 13 | 线圈过温 |
| 14 | MOS 过温 |
| 15 | 其他错误 |

任何受控关节错误位非零时都应阻止新运动，并保留原始十六进制值用于诊断。

### 6.5 H1Robot 主要状态接口索引

| 接口 | 返回内容 | 备注 |
|---|---|---|
| `isRobotConnected()` | 心跳连接布尔值 | 控制循环中也要周期检查 |
| `getCurrentMode()` | 当前控制模式 | 不会切模式 |
| `getInitState()` | 初始化状态 | 判断能否切模式/是否完成 |
| `getRobotInfo()` | 名称、型号、软硬件/固件版本 | 资产与兼容性记录 |
| `getMotorState(id)` | 任一 0–22 电机状态 | 最通用 |
| `getArmState(id)` | 机械臂电机状态 | id 应是双臂关节 |
| `getGripperState(id)` | 夹爪状态 | 左 14、右 22 |
| `getChassisState(chassis_id)` | 底盘电机/状态 | ID 0/1；不等于世界里程计 |
| `getChassisSpeedState()` | 轮速、线速度、角速度 | 没有 x/y/航向世界位姿 |
| `getWaistState(waist_id)` | 升降/腰部状态 | ID 2/3/4：升降、pitch、yaw |
| `getHeadState(head_id)` | 头部状态 | ID 5/6：yaw/pitch |
| `getHandRelative(arm)` | 左/右末端相对位姿 | m + qx,qy,qz,qw |
| `getHeadRelative()` | 头部相对位姿 | 不是相机图像 |
| `getHandCameraRelative(arm)` | 腕相机相对位姿 | 不是 RGB-depth 标定 |
| `getHeadCameraRelative()` | 头相机相对位姿 | 不是相机内参 |
| `getIMU_State()` | RPY、角速度、加速度、四元数 | IMU 四元数顺序为 w,x,y,z |
| `getPowerChargeState()` | SOC、温度、充放电状态 | 厂商称未充电且 SOC<10% 时控制不可用；当前 CLI 更保守地一律拒绝 <10% |
| `getHighLevelState()` | 高层动作状态/进度 | 高层控制时使用 |
| `getJoystickState()` | 遥控器数据 | 可选读取 |
| `getFixedRodState()` | 固定杆状态 | 视型号配置 |
| `getHandState(arm)` | 灵巧手状态 | 参数为 LEFT/RIGHT；只有安装灵巧手时调用 |
| `getForceSensorState()` | 六维力状态 | H1 PRO 通常无 MAX 版六维力传感器 |

注意：末端位姿四元数是 `[qx,qy,qz,qw]`，而 `getIMU_State().quat` 是 `[w,x,y,z]`。保存统一数据集时必须在 schema 中明确顺序，不能直接拼接后假定相同。

## 7. 当前 `/home/robot/control` 的运动命令怎么调用

### 7.1 默认只做 dry-run

`send_robot_command.py` 默认不加载 SDK、不连接机器人、不运动：

```bash
cd /home/robot/control

python send_robot_command.py joint \
  --arm left \
  --delta 0 0 0 0 0 0 0.02 \
  --duration 3 \
  --rate 100
```

输出末尾应明确显示：

```text
DRY-RUN ONLY: no SDK was loaded and no robot connection or movement occurred.
```

真实运动必须同时提供：

```text
--execute
--confirm-motion I_UNDERSTAND_H1_WILL_MOVE
```

这两个参数只是软件授权门，不能替代现场清场、急停检查、控制权检查和目标姿态审核。

### 7.2 当前 CLI 能力

| 命令 | 模式 | 功能 | 内部接口 |
|---|---|---|---|
| `joint` | LOW_LEVEL | 一只手臂 7 个绝对关节角或相对增量 | 每周期 7 次 `setArm_low()` |
| `dual-joint` | LOW_LEVEL | 双臂 14 轴 + 双夹爪 + 可选升降 | 每周期依次下发 |
| `initial-pose` | LOW_LEVEL | 本机保存的作业初始姿态 | `dual-joint` 预设 |
| `pose` | HIGH_LEVEL | 单臂绝对/相对末端位姿 | `setArmMove_high()` |
| `gripper` | LOW/HIGH | 单夹爪绝对/相对位置 | `setGripper_low/high()` |
| `deinit` | 当前 SDK 生命周期 | 单独执行反初始化 | `robot_deinit()`，会运动 |

当前 CLI 有意没有暴露：

- 用户任意设置 `Torque/KP/KD`。
- 纯速度/纯力矩实时控制。
- `GRAVITY_COMPENSATION_LEVEL`。
- 多关节原子写入。

### 7.3 本机已保存的双臂作业姿态

关节顺序为 `[肩 pitch, 肩 roll, 肩 yaw, 肘, 腕 roll, 腕 yaw, 腕 pitch]`：

```text
左臂：[0, 0, 0, -1.20, 0, 0, 0.98] rad
右臂：[0, 0, 0, -1.20, 0, 0, 0.98] rad
双夹爪：0.02 rad（当前实机已核对为闭合附近）
升降柱：0.40 m
```

只校验：

```bash
python send_robot_command.py initial-pose
```

与其等价的详细 dry-run：

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

`--hold-until-enter` 会持续占有运动控制权并刷新目标；按 Enter 后才会调用会产生运动的 `robot_deinit()`。终端断开、Ctrl+C 或 SDK 异常时，工具不会盲目自动回收。

### 7.4 真实运动前的门禁

当前工具会检查：

- 所有输入维度、有限数、软限位、四元数和幅度。
- 当前心跳是否连接。
- 初始化状态是否为 0 或 4。
- 电池 SOC 是否不低于 10%。
- 受控电机反馈是否成功且错误位为 0。
- 初始状态与目标的差是否超过本地门限。
- LOW_LEVEL 插值频率是否为 100–500 Hz。
- HIGH_LEVEL 目标是否足够靠近当前实测末端位姿。
- 真实执行前至少 3 秒倒计时。

如果 `max-start-delta` 阻止执行，正确处理不是调大门限，而是查看当前关节值、审核碰撞路径并拆成经确认的中间姿态。

### 7.5 常用命令例子

单臂绝对 7 关节目标的离线校验；先把占位符替换成依据当前反馈审核过的 rad 值：

```bash
python send_robot_command.py joint \
  --arm left \
  --target Q1 Q2 Q3 Q4 Q5 Q6 Q7 \
  --duration 3 \
  --rate 100
```

在当前末端坐标系中做 1 cm 相对平移的 dry-run：

```bash
python send_robot_command.py pose \
  --arm left \
  --relative-position 0.01 0 0 \
  --duration 3
```

夹爪相对增加 0.05 rad 的 dry-run：

```bash
python send_robot_command.py gripper \
  --arm left \
  --delta 0.05
```

只有完成现场清场和全文安全清单后，才把两个全局授权参数放在子命令之前，例如：

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

单独反初始化：

```bash
python send_robot_command.py \
  --execute \
  --confirm-motion I_UNDERSTAND_H1_WILL_MOVE \
  deinit
```

最后一条不是“清理连接”，而是会移动升降柱和双臂的实体回收动作；必须独立审核回收路径。

该 `deinit` 子命令只适用于：SDK 已在非 `UNINITIALIZED` 模式完成初始化，但此前运动程序异常退出后仍保持 `Init_Complete` 的恢复场景。它不能接管并回收由 VR/遥控模式初始化的机器人；若当前 `control_mode=UNINITIALIZED(0), init_state=Init_Complete(2)`，应使用对应的 VR/遥控批准流程反初始化。当前 CLI 的独立 `deinit` 路径只预检状态 2，模式不适用时会调用后由 SDK 拒绝，因此运行前必须先读模式并判断场景。

## 8. RGB-D 相机：硬件、链路与当前配置

### 8.1 三路硬件

| 位置 | 型号 | 当前配置序列号 | 服务配置名 | 当前 CameraClient 返回名 | 流 |
|---|---|---:|---|---|---|
| 左腕 | Intel RealSense D405 | 261022273375 | `cam_left_wrist` | `rs/cam_left_wrist` | color + depth |
| 头部高位 | Intel RealSense D435 | 261822070479 | `cam_high` | `rs/cam_high` | color + depth |
| 右腕 | Intel RealSense D405 | 261022277043 | `cam_right_wrist` | `rs/cam_right_wrist` | color + depth |

本机 `/etc/robot/cams_realsense.yaml` 配置的三路都是：

```text
color: 640×480 @ 30 FPS, bgr8
depth: 640×480 @ 30 FPS, z16
align_to: no align
```

服务端启动时会扫描 `/etc/robot/*.yaml`，只激活实际检测成功的设备。配置名和客户端返回名可能有 `rs/` 前缀差异，因此业务代码必须先 `get_state()`，再把返回的 `camera_name` 原样传给 getter，不要自己拼接名称。

### 8.2 相机通信结构

`CameraClient` 同时使用：

- gRPC：默认 TCP `50051`，用于已激活相机配置/内参查询和 WebRTC 信令；当前 proto 不提供完整健康状态。
- WebRTC H.264：传输彩色图像。
- WebRTC DataChannel：传输压缩/分片的 16 位深度图。

机内调用首选：

```text
localhost:50051
```

远程调用使用“机器人当前可达的 IP:50051”。本机地址会随网络变化，不应把某次 DHCP 地址硬编码进程序。远程网络还要允许 gRPC 和 WebRTC 实际协商的数据通道流量；只开放 50051 并不必然保证媒体链路完整。

### 8.3 Python SDK 位置和实际接口

```text
/home/robot/H1_SDK_1.3.9/camera_sdk_python/
  camera_client.cpython-310-x86_64-linux-gnu.so
  proto/
  example/
```

实际公开接口：

```text
CameraClient(
    grpc_target="localhost:50051",
    connect_timeout=10.0,
    enable_depth=False,
)

start() -> None
stop() -> None

get_latest_frame(cam_name)
    -> None | tuple[np.ndarray, float]

get_latest_depth(cam_name)
    -> None | tuple[np.ndarray, float]

get_state(camera_names=None, timeout=5.0)
    -> RecorderStateResponse
```

`start()` 可能等待最初的握手或超时，但成功返回后由后台线程持续收流，不是需要用户自己永久阻塞的取流函数。

## 9. 相机 Python 调用方式

### 9.1 发现所有相机、流和内参

以下代码不让机器人运动：

```python
import sys

CAMERA_SDK = "/home/robot/H1_SDK_1.3.9/camera_sdk_python"
sys.path.insert(0, CAMERA_SDK)

from camera_client import CameraClient

client = CameraClient(
    grpc_target="localhost:50051",
    connect_timeout=10.0,
    enable_depth=False,
)

try:
    client.start()
    state = client.get_state(timeout=5.0)

    for camera in state.camera_configs:
        print("camera:", camera.camera_name)
        for stream in camera.streams:
            print(
                " ", stream.type,
                stream.width, stream.height, stream.fps,
            )
            if stream.HasField("intrinsics"):
                intr = stream.intrinsics
                print({
                    "fx": intr.fx,
                    "fy": intr.fy,
                    "ppx": intr.ppx,
                    "ppy": intr.ppy,
                    "model": intr.model,
                    "coeffs": list(intr.coeffs),
                })
finally:
    client.stop()
```

当前 1.3.9 返回的是 Protobuf 对象，而不是 dict：

```text
RecorderStateResponse
└── camera_configs[]
    ├── camera_name
    └── streams[]
        ├── type
        ├── width, height, fps
        └── intrinsics
            ├── width, height
            ├── fx, fy, ppx, ppy
            ├── model
            └── coeffs[]
```

当前 proto 没有 `health/online/error/ok/message/fmt` 字段。RPC 失败通过异常表示。因此 `get_state()` 更准确地说是“已激活相机配置和内参查询”，不是完整健康监控。

### 9.2 读取彩色和深度帧

```python
import sys
import time

CAMERA_SDK = "/home/robot/H1_SDK_1.3.9/camera_sdk_python"
sys.path.insert(0, CAMERA_SDK)

from camera_client import CameraClient

client = CameraClient("localhost:50051", enable_depth=True)

try:
    client.start()
    state = client.get_state(timeout=5.0)

    rgb_names = []
    depth_names = []
    for cfg in state.camera_configs:
        stream_types = {stream.type for stream in cfg.streams}
        if "color" in stream_types:
            rgb_names.append(cfg.camera_name)
        if "depth" in stream_types:
            depth_names.append(cfg.camera_name)

    if not rgb_names and not depth_names:
        raise RuntimeError("get_state() returned no active camera streams")

    print("color:", rgb_names)
    print("depth:", depth_names)

    last_rgb_ts = {}
    last_depth_ts = {}
    delivered = {
        **{f"rgb:{name}": 0 for name in rgb_names},
        **{f"depth:{name}": 0 for name in depth_names},
    }
    first_seen = set()
    report_started = time.monotonic()

    while True:
        for name in rgb_names:
            rgb = client.get_latest_frame(name)
            if rgb is not None:
                bgr, t_rgb = rgb
                if t_rgb != last_rgb_ts.get(name):
                    last_rgb_ts[name] = t_rgb
                    key = f"rgb:{name}"
                    delivered[key] += 1
                    if key not in first_seen:
                        first_seen.add(key)
                        print(key, bgr.shape, bgr.dtype, t_rgb)

        for name in depth_names:
            depth = client.get_latest_depth(name)
            if depth is not None:
                depth_mm, t_depth = depth
                if t_depth != last_depth_ts.get(name):
                    last_depth_ts[name] = t_depth
                    key = f"depth:{name}"
                    delivered[key] += 1
                    v = depth_mm.shape[0] // 2
                    u = depth_mm.shape[1] // 2
                    if key not in first_seen:
                        first_seen.add(key)
                        print(
                            key, depth_mm.shape, depth_mm.dtype,
                            "center_mm=", int(depth_mm[v, u]), t_depth,
                        )

        now = time.monotonic()
        if now - report_started >= 1.0:
            print("unique decoded frames in interval:", delivered)
            delivered = {key: 0 for key in delivered}
            report_started = now

        # 当前 getter 每次都会复制数组；按相机帧率附近轮询，避免 200 Hz 重复拷贝。
        time.sleep(0.03)
finally:
    client.stop()
```

返回格式：

| 接口 | 返回数组 | 当前实测 | 单位/通道 |
|---|---|---|---|
| `get_latest_frame()` | `(frame, timestamp)` 或 `None` | `(480,640,3) uint8` | H.264 解码后的 **BGR**，有损链路，不是原始 RGB |
| `get_latest_depth()` | `(depth, timestamp)` 或 `None` | `(480,640) uint16` | zlib 解压后的原始深度，文档约定 mm |

只有需要深度时才设置 `enable_depth=True`，否则会增加 CPU 和网络负载。

### 9.3 “latest” 语义

两个 getter 都是非阻塞的“最新帧快照”：

- 链路刚启动、首帧尚未到达时返回 `None`。
- 轮询快于 30 FPS 时，会多次拿到相同 timestamp 的同一最新帧。
- 消费者太慢时可能跳过旧帧，不保证无损地逐帧排队。
- 它适合低延迟在线感知；严格无损录制应使用专门录制服务或另行验证缓冲语义。
- 当前 Python getter 每次调用已经返回数组副本，过快轮询会产生大量重复内存复制；教程使用约 30–60 Hz，生产接口最好增加 callback/阻塞 wait/sequence 能力。
- 同一个 getter 返回的数组若再共享给多个会修改它的消费者，才需要在消费者之间额外 `frame.copy()`；不必无条件再复制一次。

### 9.4 深度可视化和量测是两件事

用于显示：

```python
import cv2
import numpy as np

depth_8 = (depth_mm * 0.03).clip(0, 255).astype(np.uint8)
depth_color = cv2.applyColorMap(depth_8, cv2.COLORMAP_JET)
```

用于几何/避障/测距时，必须使用原始 `uint16`，不能从伪彩图反推距离。当前实测深度中存在 `0`，也可能出现 `65535` 或超出传感器可靠量程的值；应按相机量程和业务阈值建立有效掩码，例如：

```python
min_mm = 100       # 示例阈值；按 D405/D435 场景重新确定
max_mm = 5000      # 示例阈值；不是厂商统一硬编码

valid = (
    (depth_mm >= min_mm)
    & (depth_mm <= max_mm)
    & (depth_mm != 65535)
)
```

这些阈值应针对相机型号、曝光、工作距离和目标任务标定，示例值不能直接当产品规格。

## 10. 相机内参、深度反投影和配准

### 10.1 针孔反投影

若某个深度像素 `(u,v)` 有有效深度 `d_mm`，并且使用的是**该深度流自己的内参**：

```text
Z = d_mm / 1000                         # m
X = (u - ppx) * Z / fx
Y = (v - ppy) * Z / fy
```

得到的是深度相机光学坐标系中的点。还需要按 `intrinsics.model` 和 `coeffs` 处理畸变；仅用上述公式相当于忽略畸变或假设输入已去畸变。

不要未经标定就假设这个相机光学坐标系的 X/Y/Z 方向与 H1 机械臂坐标系完全相同。

### 10.2 当前动态查询到的内参快照

以下值是 2026-08-28 对本机服务的一次只读查询结果，只用于记录和诊断。程序每次启动仍应调用 `get_state()`，不要把它们永久写死。

| 相机/流 | fx | fy | ppx | ppy | 模型 |
|---|---:|---:|---:|---:|---|
| 左腕 depth | 404.6728 | 404.6728 | 323.5834 | 239.1656 | brown_conrady |
| 左腕 color | 394.3099 | 394.0519 | 310.4661 | 234.0564 | inverse_brown_conrady |
| 头部 depth | 386.5041 | 386.5041 | 317.8239 | 239.7880 | brown_conrady |
| 头部 color | 603.9926 | 603.9687 | 320.5727 | 252.2173 | inverse_brown_conrady |
| 右腕 depth | 395.2621 | 395.2621 | 326.3102 | 234.2162 | brown_conrady |
| 右腕 color | 393.1610 | 392.8731 | 320.3065 | 229.6661 | inverse_brown_conrady |

三路深度畸变系数当次返回均为全 0；彩色流返回各自系数。不能由此推断未来换机、重标定或服务升级后仍相同。

当次彩色流系数快照：

```text
left wrist:  [-0.0486792, 0.0520779, 0.0005443, 0.0006731, -0.0174098]
head high:   [0, 0, 0, 0, 0]
right wrist: [-0.0482118, 0.0515835, 0.0001581, 0.0008994, -0.0176719]
```

### 10.3 当前 RGB 与 Depth 没有声明像素对齐

本机三路配置都是：

```yaml
align_to: "no align"
```

因此不能默认：

```text
rgb[v, u] 与 depth[v, u] 来自同一条空间射线
```

当前 `CameraClient` proto 只公开每条流自己的内参，不公开 color↔depth 外参，也没有原子 `get_rgbd_pair()`。若要生成彩色点云或给 RGB 像素找对应深度，需要：

1. 在服务端明确启用并验证对齐；或
2. 取得 RealSense color↔depth 外参，自行做去畸变、反投影、刚体变换和重投影；
3. 当前公开 timestamp 只能做“客户端解码/解压完成时间”的近邻匹配；它不是采集同步。严格 RGB-D 配对需要服务端/协议暴露传感器 PTS/硬件时间，并设置最大容许采集时间差。

`getHandCameraRelative()` / `getHeadCameraRelative()` 返回的是相机随机器人结构运动的相对位姿，不是 color↔depth 内外参，不能替代上述标定。

### 10.4 当前 Python 1.3.9 的 timestamp 是客户端解码完成时间

交付 README 称返回“服务端传感器硬件时间戳”，但这与当前二进制实现不符。对扩展做的完全离线合成帧测试确认：

- 彩色帧忽略输入 `VideoFrame.pts`，在 H.264 解码完成并写入 latest buffer 时，以**客户端本机 asyncio event-loop monotonic 秒值**打戳。
- 深度帧也在 zlib 解压/重组完成并写入 latest buffer 时，用同一类客户端 monotonic 秒值打戳。
- 因此它不是 Unix epoch、不是服务端时间、不是 RealSense 硬件采集时间，也不是原始 socket 收包时间。

这意味着：

- 可以在同一客户端进程内用于判断该路是否出现一张新解码帧。
- 用 timestamp 去重统计的是“客户端成功解码/交付的 unique-frame FPS”，不是传感器曝光 FPS。
- H.264 和深度 zlib 管线的网络、排队、解码/解压延迟都会改变返回 timestamp，而且两条管线延迟不同。
- 用两个返回 timestamp 做近邻，只是 decode/arrival-time approximate sync，不能证明 RGB 和 Depth 同时采集。
- 不能用它直接定位相机曝光时刻，也不能据此给快速运动的腕相机插值精确机械臂姿态。
- 不能直接与 ROS time、Livox 设备时间或墙钟相减做严格融合。

业务代码应把字段命名为类似 `sdk_postdecode_monotonic_s`，同时在 getter 返回后立即记录 `host_getter_monotonic_ns`。如果任务需要真正采集时间，必须修改/升级相机服务和协议，把 RealSense/H.264 PTS 或硬件时间随帧传到客户端。升级 SDK 后也要重新做时间戳回归测试，不能假定实现不变。

## 11. 相机依赖、C++ 接口和交付差异

### 11.1 当前可用 Python 依赖

本机 `zerith` 环境已经能导入相机扩展。当前核实的主要版本包括：

```text
Python 3.10.19
aiortc 1.14.0
av 16.0.1
grpcio 1.76.0
protobuf 6.33.1
numpy 1.26.0
OpenCV 4.10.0
```

交付 `requirements.txt` 固定 `grpcio==1.74.0`，但生成的 `robot_pb2_grpc.py` 要求 `grpcio>=1.75.1`，两者冲突。不要为追随 README 而降级当前正常环境；若新建环境，至少应满足 `grpcio>=1.75.1`，并在隔离环境里验证二进制 ABI。

README 所称“Python 3.x”也过宽，因为文件名明确是：

```text
camera_client.cpython-310-x86_64-linux-gnu.so
```

### 11.2 C++ 相机接口

C++ 头文件位于：

```text
/home/robot/H1_SDK_1.3.9/camera_sdk_cpp/include/camera_client.h
```

接口对应关系：

```cpp
CameraClient client("localhost:50051", true);
client.start();

auto frame = client.getLatestFrame(camera_name);
// optional<pair<cv::Mat,double>>, color 为 BGR

auto depth = client.getLatestDepth(camera_name);
// optional<pair<cv::Mat,double>>, CV_16UC1, mm

auto state = client.get_state({}, 5.0);
client.stop();
```

当前交付目录实际包含约 651 MB 的 ffmpeg、gRPC、libdatachannel、OpenCV、OpenSSL 等 bundled 三方库；问题不是三方目录缺失。对独立构建目录做 CMake 配置测试时，当前首先失败在系统缺少 `gtk+-2.0` 的 pkg-config/开发包，这是 `CMakeLists.txt` 的强制依赖。安装或提供 GTK2 开发包后仍需继续验证最终静态链接。当前 Python 3.10 路径已经验证，优先用 Python 接通功能；需要生产 C++ 时再补依赖并完成全量构建/运行测试。

## 12. 激光雷达：接口边界和当前状态

### 12.1 型号与位置

随机器交付的 SDK 指南说明：

- 机器人底盘中心安装一台 **Livox Mid-360**。
- 雷达使用独立 Livox 接口，不属于 H1Robot 电机/状态 API。
- 出厂网络位于 `172.31.200.x/24`。
- 可先用 Livox Viewer 2 做设备发现、点云查看和基础诊断。

对整个 H1 SDK 1.3.9 的公开头文件、Python 类型桩和示例检索后，没有发现 `lidar`、`point cloud` 或 Livox 读取接口。因此以下写法不存在：

```python
robot.getLidar()       # 不存在
robot.getPointCloud()  # 不存在
```

正确选择是三种之一：

| 路径 | 适合场景 | 输出 |
|---|---|---|
| Livox Viewer 2 | 首次发现、看点云、诊断网络/固件 | GUI 可视化/设备管理 |
| Livox SDK2 | 不想引入 ROS、需要 C/C++ 原始回调 | UDP 点云包与 IMU 回调 |
| livox_ros_driver2 | ROS 2 建图、定位、记录、生态集成 | `/livox/lidar`、`/livox/imu` 等 topic |

### 12.2 当前本机网络

```text
interface: enp3s0
host IP:   172.31.200.1/24
link:      up, 1 Gbit/s, full duplex（2026-08-28 动态快照）
DHCP pool: 172.31.200.200–172.31.200.250
```

DHCP 配置在 `/etc/dnsmasq.d/lidar-net.conf`。曾观察到租约 `172.31.200.232`，但其 MAC OUI 指向 ASIX，且检查时 ARP 邻居不完整、没有收到 ping 回应。它可能是某个以太网外围设备，但当前证据不足以认定是 Mid-360。

当前路由还有一个待核实项：NetworkManager 曾产生 `default via 172.31.200.1`，而该地址正是本机自己的 `enp3s0` 地址。直连 `172.31.200.0/24` 路由仍可工作，但这个 default 没有正常网关意义，可能抢占其他默认路由。专用雷达网卡通常不设置 gateway/使用 never-default；在修改前必须先确认 ZERITH 网络设计和其他服务依赖。

如果最终确认 Mid-360 使用 DHCP 池 `.200–.250` 内的静态地址，应在 dnsmasq 中为其做保留或从动态池排除，避免另一客户端获得相同 IP。

因此任何配置中都必须使用：

```text
<VERIFIED_LIDAR_IP>
```

而不能直接把 `172.31.200.232` 写死。某些设备可能不响应 ICMP，所以 ping 失败不能单独证明雷达离线；但 ARP、Livox 发现协议、驱动日志和 UDP 数据必须至少有一条完整证据链。

### 12.3 安全的只读网络检查

```bash
ip -br address show dev enp3s0
ip link show dev enp3s0
ip neigh show dev enp3s0

# 查看本机是否已有 Livox/ROS 进程占用端口
ss -lunp | grep -E ':(56000|5610[01]|5620[01]|5630[01]|5640[01]|5650[01])\b'

# 查看 DHCP 租约；租约只是一条线索
sudo sed -n '1,200p' /var/lib/misc/dnsmasq.leases
```

需要抓包确认时可临时使用：

```bash
sudo tcpdump -ni enp3s0 \
  'arp or (udp and portrange 56000-56600)'
```

抓包时优先启动 Livox Viewer 的发现功能，或使用经过审阅的 SDK2 广播发现程序。Mid-360 发现涉及固定 UDP 56000；点云/IMU 的主机目标端口应按**当前客户端实际配置**核对，SDK2/ROS 官方默认常为 `56301/56401`，Viewer 可能协商自己的端口。官方 quick-start 会主动改变部分设备工作设置，不能把它当作纯只读探测器。不要在尚未确认设备身份时使用修改 IP、重启、升级固件等写操作。

Mid-360 的 RJ-45 不能连接 PoE 交换机/PoE 注入器，也不要擅改机器人内部供电线或极性；错误供电可能造成不可逆损坏。部署多台激光雷达时也避免让激光出射面长时间正对另一台雷达。H1 已有内部布线，常规软件验收不应改动供电链路。

## 13. Mid-360 网络配置和 UDP 端口

Livox 官方 SDK2 的 MID360 默认配置结构使用：

| 方向/用途 | 雷达端口 | 主机端口 |
|---|---:|---:|
| 广播发现 | 56000 | 由发现客户端选择/临时端口 |
| 命令 | 56100 | 56101 |
| 推送消息 | 56200 | 56201 |
| 点云 | 56300 | 56301 |
| IMU | 56400 | 56401 |
| 固件日志 | 56500 | 56501 |

这些端口在两条官方路径中相同，但 **SDK2 quick-start 与固定版本 ROS 驱动的 JSON schema 不同，不能混用字段**。

### 13.1 SDK2 quick-start 的配置

本文固定核对的 SDK2 commit `08f523c...` 使用数组式 `host_net_info`，没有 `lidar_configs`，也没有要填写的 `lidar_ip`：

```json
{
  "MID360": {
    "lidar_net_info": {
      "cmd_data_port": 56100,
      "push_msg_port": 56200,
      "point_data_port": 56300,
      "imu_data_port": 56400,
      "log_data_port": 56500
    },
    "host_net_info": [
      {
        "host_ip": "172.31.200.1",
        "multicast_ip": "224.1.1.5",
        "cmd_data_port": 56101,
        "push_msg_port": 56201,
        "point_data_port": 56301,
        "imu_data_port": 56401,
        "log_data_port": 56501
      }
    ]
  }
}
```

设备通过 SDK2 的 UDP 56000 广播发现。若只允许某一台雷达，业务代码应在设备上线回调中按 handle 后对应的 IP/SN 做白名单过滤，而不是向这个版本的 quick-start JSON 硬塞不存在的 `lidar_ip` 字段。

### 13.2 固定版本 ROS 驱动的配置

本文固定核对的 `livox_ros_driver2` commit `4a1def9...` 使用对象式 `host_net_info`，并通过 `lidar_configs[].ip` 指定设备：

```json
{
  "lidar_summary_info": {
    "lidar_type": 8
  },
  "MID360": {
    "lidar_net_info": {
      "cmd_data_port": 56100,
      "push_msg_port": 56200,
      "point_data_port": 56300,
      "imu_data_port": 56400,
      "log_data_port": 56500
    },
    "host_net_info": {
      "cmd_data_ip": "172.31.200.1",
      "cmd_data_port": 56101,
      "push_msg_ip": "172.31.200.1",
      "push_msg_port": 56201,
      "point_data_ip": "172.31.200.1",
      "point_data_port": 56301,
      "imu_data_ip": "172.31.200.1",
      "imu_data_port": 56401,
      "log_data_ip": "",
      "log_data_port": 56501
    }
  },
  "lidar_configs": [
    {
      "ip": "REPLACE_WITH_VERIFIED_LIDAR_IP",
      "pcl_data_type": 1,
      "pattern_mode": 0,
      "extrinsic_parameter": {
        "roll": 0.0,
        "pitch": 0.0,
        "yaw": 0.0,
        "x": 0,
        "y": 0,
        "z": 0
      }
    }
  ]
}
```

必须把 `lidar_configs[0].ip` 替换为 Viewer/SDK 实际确认的地址。`log_data_ip` 为空表示不指定日志目标；只有明确需要固件日志时再按对应版本文档设置。

### 13.3 外参只能选择一种应用方式

ROS 驱动源码定义 `roll/pitch/yaw` 为 degree，`x/y/z` 为**整数 mm**；`0.0` 形式的平移可能触发 RapidJSON 类型错误。更重要的是，驱动会把 JSON 外参直接乘到点坐标，但不会自动改变点云 `frame_id`。

因此必须二选一：

- **推荐 ROS 做法**：JSON 外参保持整数零，点坐标保持 `livox_frame`；发布经过标定的 `base_link→livox_frame` TF。零值在这里只表示“不在驱动内烘焙变换”，绝不表示安装外参等于 identity。
- **烘焙做法**：把真实外参写进 JSON，并把点云 launch 的 `frame_id` 同步改成变换后的目标 frame；绝不能再通过 TF 重复应用同一外参。固定版本的 IMU header 仍硬编码为 `livox_frame`，要单独处理。

不要把未测量的零外参解释成真实 `base_link↔lidar` 标定，也不要同时在 JSON 和 TF 中应用同一变换。

Livox SDK2 还允许一主多从：只有一个 SDK 主机应作为 `master_sdk=true` 发送控制命令；其他主机只能作为从接收点云。不要同时让 Viewer、原生 SDK 和 ROS 驱动都争夺主控制角色。

## 14. 路径 A：Livox Viewer 2 验收

建议首次验收顺序：

1. 保持 H1 运动程序、VR 与雷达无关；只处理 `enp3s0`。
2. 确认主机地址为 `172.31.200.1/24`，且没有另一个接口使用重叠路由。
3. 启动 Livox Viewer 2，选择该有线网卡。
4. 查看是否发现型号 Mid-360、序列号、IP、固件和错误状态。
5. 只开启实时点云查看并观察是否连续；雷达 IMU 再用 SDK2/ROS topic 独立验证。
6. 记录实际 IP/序列号/MAC，再生成 SDK/ROS 配置。
7. 不要把首次验收与固件升级、改静态 IP、改扫描模式同时进行。

Viewer 是最直观的发现工具，但业务程序最终仍应走 SDK2 或 ROS 驱动，并自行做状态监控和超时处理。

官方 Viewer 2 下载与手册入口：<https://www.livoxtech.com/cn/mid-360/downloads>。Ubuntu 包是否完全兼容这台 Ubuntu 24.04 主机仍需现场验证；若 GUI 不兼容，优先用经过审阅的 SDK2 广播发现程序识别未知 IP。默认 ROS 驱动配置本身要求目标设备 IP，不应把它当作保证可用的未知 IP 发现器。

## 15. 路径 B：安装和调用 Livox SDK2（无 ROS）

### 15.1 安装

当前机器尚未安装 Livox SDK2。官方项目支持 Mid-360，要求 Ubuntu 18.04+、C++11 和 CMake 3.0+。安装步骤为：

```bash
sudo apt update
sudo apt install -y git cmake build-essential

cd /home/robot/workspace
git clone https://github.com/Livox-SDK/Livox-SDK2.git
cd /home/robot/workspace/Livox-SDK2
git checkout 08f523c930b2f0ba1e98a6afaa8d7476bf479908

mkdir build
cd build
cmake ..
make -j"$(nproc)"
sudo make install
sudo ldconfig
```

官方默认把库装到 `/usr/local/lib`，头文件装到 `/usr/local/include`。若网络很慢，可使用可信的 GitHub 镜像/代理或国内 apt 镜像，但应校验仓库来源和提交版本，不要下载来源不明的二进制包。

### 15.2 先跑官方 quick-start

在固定版本官方样例 `mid360_config.json` 中只需要把主机地址改为：

```text
MID360.host_net_info[].host_ip → 172.31.200.1
```

该 SDK2 quick-start schema 没有 `lidar_ip`/`lidar_configs`，设备靠广播发现；不要套用 ROS 驱动的 JSON 字段。

然后从构建产物中运行 quick-start。官方 README 给出的调用形式是：

```bash
cd /home/robot/workspace/Livox-SDK2/build/samples/livox_lidar_quick_start
./livox_lidar_quick_start /absolute/path/to/mid360_config.json
```

构建版本的目录可能稍有不同，应以 `find build -type f -name livox_lidar_quick_start` 的结果为准。

**重要：官方 quick-start 不是完全配置中立的只读查看器。** 当前样例在设备上线回调中会设置 ESC 转速、NORMAL 工作模式和 DoubleEcho 点类型，并查询状态；改 IP 代码虽默认注释，但仍应先审阅对应版本源码，再在生产机器人上运行。只想看设备身份和点云时，Viewer 2 或经过删减/审核的自有 SDK2 程序更稳妥。

### 15.3 原生 C/C++ 调用主线

核心生命周期是：

```cpp
if (!LivoxLidarSdkInit(config_path)) {
  // 记录错误并退出
}

SetLivoxLidarPointCloudCallBack(PointCloudCallback, user_data);
SetLivoxLidarImuDataCallback(ImuCallback, user_data);
SetLivoxLidarInfoCallback(InfoCallback, user_data);
SetLivoxLidarInfoChangeCallback(DeviceChangeCallback, user_data);

// 后台线程持续回调；主线程管理退出、队列、统计与故障

LivoxLidarSdkUninit();
```

在本文固定的 SDK2 commit 中，`LivoxLidarSdkStart()` 只是返回 `true` 的空操作，官方样例也不调用它；因此这里按该版本的真实样例省略。若以后升级到 Start 具有实际语义的版本，应以新版本 API/样例为准并重新审计生命周期。

设备变化回调拿到目标 `handle` 后，官方样例会把工作模式切到 NORMAL，随后才正常出点。点云 callback 收到的是网络数据批次，不应假设“一次 callback 就是一整帧”；业务层应按时间戳/发布周期累积、去畸变和成帧。

原生包还带 `data_type`、`time_type`、timestamp 和点数等字段。SDK2 定义的高精度笛卡尔点坐标单位为 mm，低精度格式可能为 cm；必须按 `data_type` 分支解析并分别除以 1000 或 100 转成 m，不能统一强转。timestamp 也必须依据 `time_type` 和 Mid-360 协议解释，不能默认是 Unix epoch。

回调函数内不要做磁盘慢写、可视化或重计算。应立即把带时间戳的数据移动到有界队列，由工作线程消费；队列满时按业务明确选择丢旧帧、丢新帧或报警，不能无限堆积内存。

## 16. 路径 C：ROS 2 Jazzy + livox_ros_driver2

### 16.1 当前前提

本机是 Ubuntu 24.04，官方驱动当前列出的对应版本是 ROS 2 Jazzy。但本机目前没有 `/opt/ros`、`ros2`、Livox SDK2 或 `livox_ros_driver2`。

先按 ROS 2 官方 Jazzy 文档安装 ROS。安装 ROS 是较大的系统变更，本指南不把随时间变化的 apt 仓库密钥步骤复制成固定脚本；完成后应存在：

```bash
source /opt/ros/jazzy/setup.bash
ros2 --help
command -v colcon
```

官方推荐 Desktop-Full；若只安装最小 ROS，还必须补齐 colcon、ament_cmake_auto、PCL/pcl_conversions、sensor_msgs 等驱动构建依赖。无论哪种方式，都要在专用工作空间用 `rosdep` 验证依赖，而不能仅凭 `ros2 --help` 判断环境完整。

### 16.2 准备固定版本源码和依赖

先按上一节安装固定版本 Livox SDK2，再克隆驱动；此时先不要运行 `build.sh`：

```bash
mkdir -p /home/robot/workspace/ws_livox/src
cd /home/robot/workspace/ws_livox/src
git clone https://github.com/Livox-SDK/livox_ros_driver2.git

cd /home/robot/workspace/ws_livox/src/livox_ros_driver2
git checkout 4a1def929e5b59c7a8122d19fce6efba581ce9f7
```

检查 ROS 依赖：

```bash
source /opt/ros/jazzy/setup.bash
cd /home/robot/workspace/ws_livox
sudo rosdep init  # 仅首次初始化；若提示 already initialized 则跳过
rosdep update
rosdep install --from-paths src --ignore-src -r -y
```

### 16.3 先修改 Mid-360 配置，再构建

驱动仓库的默认文件通常为：

```text
/home/robot/workspace/ws_livox/src/livox_ros_driver2/config/MID360_config.json
```

固定 commit 的准确字段是对象式 `host_net_info`。构建前至少要改：

```text
MID360.host_net_info.cmd_data_ip   = 172.31.200.1
MID360.host_net_info.push_msg_ip   = 172.31.200.1
MID360.host_net_info.point_data_ip = 172.31.200.1
MID360.host_net_info.imu_data_ip   = 172.31.200.1
lidar_configs[0].ip                = 已验证雷达 IP
```

端口保持第 13 节的官方默认值，外参按 13.3 节二选一；推荐保持整数零并用真实 TF。然后构建：

```bash
source /opt/ros/jazzy/setup.bash
cd /home/robot/workspace/ws_livox/src/livox_ros_driver2
./build.sh jazzy
```

官方 `build.sh` 会清理工作空间上层的 `build/devel/install` 等生成目录，并改写部分构建文件，所以它只能在上面这种**专用、干净的 `ws_livox`** 中运行。不要在已有多个项目、包含未提交产物的混合 ROS 工作空间里直接执行。

该构建不是 `--symlink-install`；配置会复制进 install。以后修改 source 下的 JSON 后必须重新构建，并确认运行时安装副本：

```bash
find /home/robot/workspace/ws_livox/install \
  -path '*/share/livox_ros_driver2/config/MID360_config.json' \
  -print
```

再检查找到的文件确实包含 `172.31.200.1` 和已验证的雷达 IP。构建完成后加载：

```bash
source /home/robot/workspace/ws_livox/install/setup.bash
```

若找不到 `liblivox_lidar_sdk_shared.so`：

```bash
sudo ldconfig
ldconfig -p | grep livox
```

不建议长期依赖手工 `LD_LIBRARY_PATH`；优先确认 `/usr/local/lib` 已进入动态链接器配置。

### 16.4 启动

发布自定义 Livox 消息：

```bash
source /opt/ros/jazzy/setup.bash
source /home/robot/workspace/ws_livox/install/setup.bash

ros2 launch livox_ros_driver2 msg_MID360_launch.py
```

发布 `sensor_msgs/msg/PointCloud2` 并打开 RViz：

```bash
ros2 launch livox_ros_driver2 rviz_MID360_launch.py
```

不要只靠文件名猜测输出格式；启动后必须实查：

```bash
ros2 topic list -t | grep livox
ros2 topic info -v /livox/lidar
ros2 topic info -v /livox/imu
ros2 topic hz /livox/lidar
ros2 topic hz /livox/imu
ros2 topic echo /livox/imu --once
```

短时记录原始 ROS 数据：

```bash
ros2 bag record /livox/lidar /livox/imu
```

用 Ctrl+C 正常结束后检查：

```bash
ros2 bag info /absolute/path/to/bag_directory
```

应确认 topic、消息类型、消息数、持续时间和大小。磁盘吞吐不足时不要静默继续录制，应监控 dropped/队列积压并降低其他负载。

默认 `multi_topic=0` 时常见 topic 为：

```text
/livox/lidar
/livox/imu
```

`multi_topic=1` 时名称附带设备 IP，例如：

```text
/livox/lidar_172_31_200_X
/livox/imu_172_31_200_X
```

### 16.5 ROS 消息格式

`xfer_format`：

| 值 | ROS 2 类型 | 点字段/用途 |
|---:|---|---|
| 0 | `sensor_msgs/msg/PointCloud2` | `x,y,z,intensity,tag,line,timestamp`；保留驱动生成的逐点时间字段 |
| 1 | `livox_ros_driver2/msg/CustomMsg` | `timebase` + 每点 `offset_time,x,y,z,reflectivity,tag,line` |
| 2 | PCL `PointXYZI` | 官方注明仅 ROS 1；ROS 2 不应使用 |

默认发布频率通常为 10 Hz，可设置 5、10、20、50 Hz 等，官方给出的最大值是 100 Hz。发布频率是把连续点流组织成 ROS 消息的频率，不是激光器的物理扫描频率；频率越高，每条消息通常点越少、调度开销越大。

自定义消息结构：

```text
std_msgs/Header header
uint64 timebase          # 第一个点的基准时间
uint32 point_num
uint8 lidar_id
uint8[3] rsvd
CustomPoint[] points

CustomPoint:
  uint32 offset_time     # 相对 timebase
  float32 x, y, z        # m
  uint8 reflectivity     # 0–255
  uint8 tag
  uint8 line
```

IMU topic 类型为：

```text
sensor_msgs/msg/Imu
```

**固定版本 IMU 单位陷阱：不要直接送入标准 ROS 融合器。** Mid-360 协议的 gyro 是 rad/s、acc 是 g；该固定版本驱动把 acc 原值直接填入 `linear_acceleration`，没有乘 `9.80665`，但 ROS `sensor_msgs/Imu` 规范要求 m/s²。该驱动也没有提供有效 orientation 或 covariance，并把 IMU `header.frame_id` 硬编码为 `livox_frame`，不随 launch 的点云 frame 参数改变。因此：

- `angular_velocity` 可按 rad/s 使用，但仍需偏置/噪声标定。
- `linear_acceleration` 经实测确认后需乘标准重力常数并处理轴向/偏置。
- orientation 视为不可用；covariance 应依据标定显式设置/标记未知。
- 不要把原始 `/livox/imu` 直接喂给 `robot_localization` 等标准融合器。
- 若选择把点云外参烘焙到 `base_link`，IMU 仍在 `livox_frame`，不能跟着误标。

### 16.6 ROS 2 Python 最小订阅器

对于 `xfer_format=0` 的 PointCloud2：

```python
import rclpy
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import PointCloud2, Imu
from sensor_msgs_py import point_cloud2


class Mid360Reader(Node):
    def __init__(self):
        super().__init__("mid360_reader")
        self.create_subscription(
            PointCloud2,
            "/livox/lidar",
            self.on_cloud,
            qos_profile_sensor_data,
        )
        self.create_subscription(
            Imu,
            "/livox/imu",
            self.on_imu,
            qos_profile_sensor_data,
        )

    def on_cloud(self, msg):
        field_names = [field.name for field in msg.fields]
        print(
            "cloud", msg.header.frame_id,
            msg.width * msg.height,
            field_names,
        )

        # 这里只演示读取少量点；生产程序不要把整帧逐点 print。
        points = point_cloud2.read_points(
            msg,
            field_names=("x", "y", "z", "intensity"),
            skip_nans=True,
        )
        for index, point in enumerate(points):
            if index >= 3:
                break
            print(point)

    def on_imu(self, msg):
        # 本固定驱动把 Mid-360 的 g 原值放进 linear_acceleration；
        # 在完成量纲/轴向/偏置实测后才换算为标准 ROS m/s²。
        ax_m_s2 = msg.linear_acceleration.x * 9.80665
        print(
            "imu",
            msg.angular_velocity.x,
            msg.angular_velocity.y,
            msg.angular_velocity.z,
            "ax_m_s2=", ax_m_s2,
        )


rclpy.init()
node = Mid360Reader()
try:
    rclpy.spin(node)
finally:
    node.destroy_node()
    rclpy.shutdown()
```

若 `msg_MID360_launch.py` 使用 `xfer_format=1`，订阅类型要改为：

```python
from livox_ros_driver2.msg import CustomMsg
```

点的相对组织关系可由 `timebase + offset_time` 表示，但不能由此断言它是设备绝对时间。固定驱动仅在 PTP/gPTP/GPS 同步类型下使用设备 timestamp；NoSync 时会回退到主机 `high_resolution_clock::now()`。PointCloud2/CustomMsg 又不暴露原始 `time_type`，所以必须旁路记录驱动版本、雷达同步模式和 `source_clock_domain`，不能只看 `timebase/header.stamp` 做跨传感器融合。

雷达 `/livox/imu` 是 Mid-360 内部 IMU；`H1Robot.getIMU_State()` 是机器人本体 IMU。它们的安装位置、姿态、时钟和误差模型都不同，数据集中必须使用不同字段和 frame，不能混成同一个“imu”。

### 16.7 ROS 驱动的定位

Livox 官方明确把 ROS Driver 2 定位为调试/测试工具，而不是未经修改即可大规模量产的生产驱动。生产机器人至少要补：

- 网络/设备断联检测和自动恢复策略。
- 数据新鲜度、包序号/丢包率、点数与发布频率监控。
- 有界队列和背压策略。
- 时间同步诊断。
- 雷达温度、错误码与状态上报。
- 标定版本管理和 TF 健康检查。
- 录包/在线算法不会反向拖慢驱动的进程隔离。

## 17. 机械臂、相机、雷达如何做空间融合

### 17.1 先定义 frame，不要只拼数组

建议至少明确以下坐标系：

```text
map
└── odom
    └── base_link
        ├── lidar_frame / livox_frame       # 静态安装外参
        ├── head chain
        │   └── head_camera_link
        │       └── head_camera_optical
        ├── left arm chain
        │   └── left_hand
        │       └── left_camera_link
        │           └── left_camera_optical
        └── right arm chain
            └── right_hand
                └── right_camera_link
                    └── right_camera_optical
```

这棵树表示概念关系，不代表当前 H1 SDK 已经发布了 ROS TF。特别是：

- `getHandRelative()` 返回厂商定义的相对电机零位末端位姿，不是 `map` 或 `odom`。
- `getHandCameraRelative()` / `getHeadCameraRelative()` 返回 SDK 相对零位坐标系中的相机姿态；公开资料没有充分定义其父 frame、变换方向或是否对应 ROS camera_link/optical frame。它不是 RGB-depth 外参，也不能直接当成 `T_base_camera`。
- Mid-360 模板外参全 0 不是 H1 的真实 `base_link→livox_frame`。
- 当前 H1 状态接口没有底盘世界 x/y/航向；需要轮速里程计/SLAM/融合器单独估计 `odom→base_link`。

### 17.2 正确的深度点到机器人/世界坐标流程

对一个深度点，完整变换应是：

```text
深度像素 (u,v,d)
→ 使用该 depth stream 的 K 和畸变模型反投影
→ P_depth_optical
→ T_camera_link_depth_optical
→ P_camera_link
→ 真正曝光时刻的 T_relativeZero_cameraLink（当前相机 API 无法精确给出该时刻）
→ 经标定/验证的 T_base_relativeZero
→ P_base
→ 同一时刻的 T_odom_base / T_map_base
→ P_odom / P_map
```

如果还要给点着色，则需另外执行：

```text
P_depth_optical
→ T_color_depth
→ 投影到 color stream
→ 检查像素边界、遮挡和两个帧的时间差
→ 读取 BGR
```

当前服务 `no align` 且客户端不公开 `T_color_depth`，所以这一步不能只靠现有 `get_state()` 完成；需要服务端对齐或 RealSense 标定外参。给点着色时还要用 color stream 自己的畸变模型/系数，检查变换后 `Z_color>0`、像素边界，并用 z-buffer/遮挡逻辑处理前后表面。

同样，公开 API 不提供或没有充分定义 `T_cameraLink_depthOptical`、`T_base_relativeZero` 及上述 camera-relative getter 的准确 frame/方向。因此整条 `depth→base` 链目前不能只靠公开接口无歧义实现，必须取得厂商坐标约定并完成固定安装标定。

### 17.3 雷达到机器人坐标

Mid-360 点首先处于 `livox_frame`。要用于机器人避障或地图：

```text
P_base = T_base_livox × P_livox
```

该公式假定采用第 13.3 节的推荐方案：JSON 不烘焙外参、点仍在 `livox_frame`。若驱动已经把同一外参烘焙进点，不能再乘一次该 TF。

`T_base_livox` 必须来自厂商交付标定或现场标定，至少包含：

- 精确平移 x/y/z。
- roll/pitch/yaw 或四元数。
- 坐标轴定义。
- 标定日期、机器序列号和雷达序列号。
- 标定残差和适用温度/机械装配状态。

仅凭“安装在底盘中心”无法推导毫米级平移、安装高度和旋转方向。

### 17.4 运动畸变

机器人移动或机械臂摆动时，不能用处理时刻的一个姿态变换整帧数据：

- Mid-360 连续输出点，每点带时间信息；移动底盘上应按逐点时间用 IMU/里程计做 deskew。
- 腕相机随 7 轴机械臂快速运动；理论上应在图像曝光时刻附近插值关节/相机位姿，但当前 Python API 没有暴露曝光/硬件时间，无法做严格运动补偿。
- RGB 与 Depth 分别取最新帧，不是原子配对；当前只能按客户端解码完成时间做近似近邻，不能证明采集同步。
- H.264 解码、深度解压、网络和排队延迟不会改变真实曝光这一物理事件，却会直接改变当前 API 返回的 post-decode timestamp；真实曝光时刻目前不可见。

## 18. 时间同步与统一采集 schema

### 18.1 明确每个时间字段究竟来自哪里

每条传感器数据建议保存：

| 字段 | 含义 |
|---|---|
| `device_capture_timestamp` | 设备采集时间；当前 CameraClient 1.3.9 **不提供**，Livox 是否为设备时间取决于同步模式 |
| `sdk_postdecode_monotonic_s` | 当前 CameraClient 在客户端解码/解压完成时写入的单调秒值 |
| `host_getter_monotonic_ns` | 业务 getter 返回后立刻记录的本机单调时间；不是 socket 收包时刻 |
| `host_callback_monotonic_ns` | ROS/SDK2 callback 进入时记录；仍是到达侧时间，不等于设备采集时间 |
| `host_wall_time` | 用于日志关联的人类可读时间，不用于实时差分 |
| `clock_domain` | 例如 `camera_client_monotonic`、`livox_ptp`、`livox_host_fallback` |
| `local_sequence` | 本地递增序号，只能发现本地重复/处理丢弃；没有上游帧号时不能证明网络前没有丢帧 |

只保存一个浮点 `timestamp` 而不写时钟域，后续几乎无法可靠融合。

### 18.2 建议的数据字段

```text
robot_state:
  host_monotonic_ns
  mode, init_state, connected
  joint_name[14]
  joint_position_rad[14]
  joint_velocity_rad_s[14]
  joint_torque_nm[14]
  motor_error[14]
  hand_pose_left/right
  body_imu

camera_frame:
  camera_name
  stream_type
  sdk_postdecode_monotonic_s
  host_getter_monotonic_ns
  device_capture_timestamp: unavailable_in_current_api
  encoding, dtype, width, height
  calibration_hash
  image/depth payload

ros_lidar_msg:
  frame_id
  source_timebase
  source_clock_domain
  host_callback_monotonic_ns
  point_count
  pointcloud2_fields or custom_msg_schema
  points or message payload

raw_sdk2_packet:
  data_type
  time_type
  raw_timestamp
  host_callback_monotonic_ns
  point_count

calibration:
  robot_serial
  sensor_serial
  intrinsics
  distortion
  static/dynamic transform convention
  calibration_version
```

`calibration_hash/version` 不是 CameraClient 返回字段，应由业务对完整内参、畸变、分辨率、配置名和从本机 YAML/librealsense 取得的序列号计算并管理。远程纯 CameraClient 当前拿不到物理 serial。

### 18.3 同步策略

从易到难：

1. **到达/解码时间近邻**：记录客户端/主机单调时间，按最近邻配对并设最大 `Δt`。它只适合静态或慢速早期验证，不能支撑快速腕部运动的严格同步。
2. **暴露真实设备时间**：修改相机服务/协议传递 RealSense/H.264 PTS 或硬件时间；Livox 侧记录其同步状态和 source clock。
3. **统一设备时间**：Mid-360 支持的模式应准确记录为 PTP（IEEE 1588v2）、gPTP 或 GPS（PPS+GPRMC）；当前相机服务是否支持外触发/统一时钟尚未确认，不能假定已有。
4. **运动补偿**：只有拿到可信采集时刻后，才按该时刻插值机器人姿态；Mid-360 再利用逐点时间做 deskew。

每次设备/服务重启都应重新检查时间是否归零、跳变或改变偏移。

## 19. 推荐的软件架构

### 19.1 进程职责

```text
motion_controller（唯一 H1Robot 所有者和运动写入者）
  ├── 接收经过限幅的目标
  ├── 维护模式/初始化状态机
  ├── 100–500 Hz 插值和 watchdog
  ├── 通过同一个 H1Robot 读取所需状态
  └── 通过 IPC/ROS 发布关节反馈与控制健康状态

robot_state_consumer（不构造第二个 H1Robot）
  └── 订阅 motion_controller 发布的综合状态

camera_reader（只读）
  └── 一个 CameraClient 读取三路 RGB-D，发布最新帧/内参

lidar_driver（只读数据 + 必要设备工作模式管理）
  └── Livox SDK2 或 ROS2，发布点云/雷达 IMU/设备健康

fusion/recorder
  ├── 时间同步
  ├── TF/标定版本
  ├── 有界队列
  └── 算法、记录、回放
```

相机/雷达故障不应阻塞运动控制周期；感知故障是否需要减速或停止，应通过明确的安全状态机传递，而不是让传感器线程直接调用机械臂 setter。

H1 SDK 的 `H1Robot` 客户端必须保持单实例/单所有者。`read_robot_state.py` 适合在运动控制器没有运行时独立只读诊断；运动期间若还要综合状态采集，应作为同一 owner 内的模块/线程，或只订阅 owner 已发布的状态，不能再启动第二个构造 `H1Robot` 的进程。

### 19.2 单写入者和 watchdog

运动进程至少维护：

- 当前控制权 owner/session ID。
- 最近一条有效命令时间。
- 最近一条有效反馈时间。
- 命令序号和 schema 版本。
- 每轴软限位、速度/加速度/jerk 限制。
- 控制周期抖动与连续错过 deadline 次数。
- 连接、错误位、电池和初始化状态。
- 超时后的明确策略：在当前模式内执行经过验证的保持/减速停止，或等待人工恢复。

只有完成批准的状态转换并进入 `Uninit` 或 `Deinit_Complete` 后才能切换模式。“超时就切模式”在 `Init_Complete` 下既违反状态机约束，也可能失败；“超时就 `robot_deinit()`”同样不是通用安全策略，因为反初始化本身会沿预设路径运动。

### 19.3 有界队列

| 数据 | 常见策略 |
|---|---|
| 运动反馈 | 保留最新值；故障/事件另建不可丢队列 |
| 在线相机推理 | 最新帧优先，丢旧帧避免延迟累积 |
| 数据集录制 | 有界队列 + 磁盘带宽监控；过载显式记 dropped |
| 雷达建图 | 保留逐点时间；队列满时报警/降载，不能静默无限堆积 |

## 20. 故障排查

### 20.1 H1 运动/状态 SDK

#### `zcm_create: Assertion ret == ZCM_EOK failed`

```bash
ps -ef | grep -E 'state_monitor|h1_|read_robot_state|send_robot_command'
ls -l /dev/shm/zcm/ipcshm/default
getfacl /dev/shm/zcm/ipcshm/default
systemctl status zerith-zcm-permissions.service
```

如果共享内存被 SDKService 重建且组权限丢失：

```bash
sudo chgrp robot /dev/shm/zcm/ipcshm/default
sudo chmod 664 /dev/shm/zcm/ipcshm/default
```

当前已配置 systemd 服务开机自动处理；若仍复发，应查看该服务和 `robotd.service` 日志，而不是每次只手工 chmod。

#### 导入失败/`python3.10` 不存在

```bash
source /home/robot/miniconda3/etc/profile.d/conda.sh
conda activate zerith
python --version
```

必须是 CPython 3.10。不要尝试把 `.so` 复制成另一个 Python 版本的文件名来绕过 ABI。

#### 模式切换被拒绝

先只读检查：

```bash
cd /home/robot/control
python read_robot_state.py --count 1
```

只有 `init_state=Uninit(0)` 或 `Deinit_Complete(4)` 才应切模式。若当前是模式 0、状态 2，普通 `joint/dual-joint/pose/gripper` 命令会拒绝抢占；独立 `deinit` 子命令的本地预检例外见第 7.5 节，它也不能成功接管 VR 模式。不要用未审核脚本自动反初始化。

#### setter 返回 True 但没有到位

True 只代表调用被接受。检查：

- `getArmState()` 的真实位置/速度/力矩。
- 高层 `getHighLevelState()` 状态和进度。
- `getHandRelative()` 末端误差。
- 是否只发了一次低层命令而没有持续刷新。
- 目标是否不可达、自碰撞或被控制器限幅。
- 是否有其他 writer/VR 同时占用控制权。

### 20.2 相机

#### `ModuleNotFoundError: camera_client`

确认 Python 3.10，并把 SDK 根目录而不是 `.so` 文件路径加入 `sys.path`：

```python
sys.path.insert(0, "/home/robot/H1_SDK_1.3.9/camera_sdk_python")
```

#### `Connection refused`

```bash
ss -ltnp | grep ':50051'
systemctl --type=service --state=running | grep -Ei 'camera|record|robot'
```

机内使用 `localhost:50051`；远程使用当时可达的机器人 IP，并检查防火墙和 WebRTC 协商网络。

#### getter 一直是 `None`

检查：

1. `client.start()` 是否成功返回。
2. `get_state()` 是否真的包含该 `camera_name` 和对应 stream type。
3. 是否错误地去掉/添加了 `rs/` 前缀。
4. 深度是否在构造时设置 `enable_depth=True`。
5. 服务端 YAML 是否检测到物理设备。
6. 等待首帧时是否给了合理超时，而不是立即判故障。

#### RGB 和深度错位

这是当前 `align_to: no align` 下的预期风险，不是简单数组 shape 一致就能解决。必须做 color-depth 外参配准或改服务端对齐并重新验证内参/分辨率。

#### 深度值异常

确认数组仍是原始 `uint16` mm；过滤 0、65535 和量程外值；不要读取伪彩图；检查是否把 BGR/Depth stream 的内参混用。

### 20.3 Mid-360

#### 发现不到设备

```bash
ip -br addr show enp3s0
LIDAR_IP="REPLACE_WITH_VERIFIED_NUMERIC_IP"
ip route get "$LIDAR_IP"
ip neigh show dev enp3s0
```

确认路由走 `enp3s0`，再用 Viewer 2/官方发现协议核对型号、SN 和 IP。DHCP lease 不等于设备身份。

#### 驱动启动但 0 点

最常见原因：

- JSON 的 host IP 仍是官方模板 `192.168.1.5`。
- lidar IP 配错或设备仍向旧 host 发数据。
- Viewer/另一个 SDK 作为 master 占用设备。
- 端口被其他进程占用或防火墙丢包。
- 雷达没有进入正常工作模式。

用 `tcpdump` 查看**当前配置的**点云/IMU 主机端口是否真正收到 UDP，比只看节点是否存在更直接；SDK2/ROS 官方模板常用 `56301/56401`，如果配置改过就必须同步替换抓包端口。

#### RViz 无点

- 使用 `rviz_MID360_launch.py`，确保 `xfer_format=0`。
- Fixed Frame 先设 `livox_frame`。
- `ros2 topic hz /livox/lidar` 验证 topic 真有数据。
- 不要在没有真实外参时为了显示而发布伪造的 identity `base_link→livox_frame`。

#### ROS 类型不匹配

```bash
ros2 topic list -t
ros2 topic type /livox/lidar
```

`rviz_...` 通常是 PointCloud2，`msg_...` 是 CustomMsg。订阅器类型必须和实际 topic 完全一致。

#### 找不到 Livox 共享库

```bash
ls -l /usr/local/lib/liblivox_lidar_sdk_shared.so
ls -l /usr/local/include/livox_lidar_api.h
sudo ldconfig
ldconfig -p | grep livox
```

#### 性能/输出太大

不要持续 `ros2 topic echo /livox/lidar` 打印整云。使用 `topic hz`、短 rosbag、网卡统计、点数统计和驱动日志；生产环境固定经过验收的 commit/tag。

## 21. 分阶段验收清单

### 21.1 阶段 1：纯只读机器人状态

- 激活 `zerith` Python 3.10。
- `read_robot_state.py --count 1` 成功。
- 23 个电机均有明确的成功/失败状态，无失败被伪装成 0。
- 双臂 14 轴 `Error_flag=0`。
- 记录当前模式、初始化状态、电池、软件/固件版本。
- 核对关节数值变化方向和实际姿态。

### 21.2 阶段 2：相机

- `get_state()` 动态发现三路 camera name。
- color/depth stream 均为预期 640×480@30。
- 三路 BGR shape/dtype 正确。
- 三路 depth shape/dtype/mm 正确。
- 统计 10 秒 timestamp 去重后的客户端成功解码/交付 unique-frame FPS；不要称为传感器曝光 FPS。
- 统计深度 0、65535、量程外比例。
- 保存每条流内参；序列号需从本机 `/etc/robot/cams_realsense.yaml`、librealsense/udev 或未来服务接口取得，纯 CameraClient state 不返回 serial。
- 明确记录 `no align`，不要误做 RGB-D 同像素融合。

### 21.3 阶段 3：雷达网络和驱动

- Viewer/SDK 确认型号 Mid-360、SN、真实 IP。
- 主机路由确认走 `enp3s0`。
- 核对并处理“网关指向本机自身”的异常 default route，且不影响其他网络。
- 只运行一个 master 客户端。
- ROS/SDK2 收到点云和雷达 IMU。
- 点云消息类型、字段、frame、频率与配置一致。
- 记录 60 秒点数、频率、UDP 丢包/网卡错误。
- 获取或标定 `base_link→livox_frame`，绝不使用模板零外参冒充。
- 明确选择“JSON 不变换 + TF”或“JSON 烘焙 + 改 frame”，不重复应用外参。
- 验证 `/livox/imu` 的加速度 g→m/s² 换算、不可用 orientation/covariance 和 frame。
- 记录同步模式及时间实际来自设备还是 NoSync 主机回退。

### 21.4 阶段 4：首次真机运动

- 操作者受过培训，熟悉实体急停。
- 机器人四周至少 2 m 清场，确认上下/后方和线缆。
- 机械臂无持物、无自碰撞风险。
- VR、遥控、厂商示例和其他 SDK writer 全部停止。
- 电池 SOC≥10%，受控电机错误位为 0。
- 在本地终端或稳定 `tmux` 中运行。
- 先使用完全相同参数 dry-run 并逐项审核计划。
- 首次只做一个腕关节或夹爪的小相对动作。
- 即使目标只是 0.02 rad 小动作，当前 CLI 前后的厂商 `robot_init()` / `robot_deinit()` 仍可能执行较大的升降柱和双臂全程轨迹；必须按完整全身运动清场。
- 从初始化后的实测位置插值，不从假定零点下发。
- 明确异常时的实体处理和恢复流程。

产品手册说明机械臂没有制动器；直接急停/断电可能让手臂因重力下落。急停用于人身/设备紧急危险，不能当普通停止按钮。

## 22. 已发现的文档/绑定差异

| 项目 | 冲突/问题 | 本指南采用的做法 |
|---|---|---|
| H1 远程构造参数 | PDF、`.pyi` 和示例写法不完全一致 | 机器人本机使用 `H1Robot()`；远程参数先现场确认 |
| 电机 ID 类型 | `.pyi` 写 `int`，实际绑定要求枚举 | 总是转 `EtherCAT_Motor_Index` |
| 高层厂商 helper | 一个 helper 先 init 后切模式，与正式章节/同文件 main 矛盾 | 采用先切模式、后 init |
| `getHandRelative()` 前提 | 文档要求初始化，个别固件未初始化也可能返回 | 检查 `ok`；正式依赖按文档要求初始化 |
| 升降单位 | 总表/示例按 m，个别文字写 rad | 当前工具按 m；继续保留现场确认项 |
| 高层默认加速度 | 指南写 1600 m/s²，明显可疑 | 参数传 0 走内部默认，不手填 1600 |
| CameraClient Python 版本 | README 泛称 Python 3.x，文件是 cp310 x86_64 | 只用 CPython 3.10 x86_64 |
| 相机 grpc 依赖 | requirements 固定 1.74，生成代码要求 ≥1.75.1 | 保留当前可用 1.76；新环境至少 ≥1.75.1 |
| 相机 `get_state` | 旧说明可能写成 dict/健康状态 | 1.3.9 实际是 `RecorderStateResponse` Protobuf |
| 相机时间戳 | README 称硬件时间，但当前 Python 1.3.9 实际在客户端解码/解压完成时用 event-loop monotonic 秒打戳 | 命名为 post-decode monotonic；不能当采集时间，升级后回归测试 |
| 相机 start | 个别 C++ 文字称阻塞，实际有后台工作线程 | 理解为握手可等待、返回后后台持续收流 |
| `getHeadCameraRelative` PDF 原型 | 对应小节误写成 `getArmTargetState(...)` | 以 1.3.9 头文件/类型桩的 `getHeadCameraRelative()` 为准 |
| RGB-D 对齐 | shape 相同但 YAML 是 `no align` | 不假设同像素对应；取得外参或服务端对齐 |
| 雷达 IP | DHCP 曾有 `.232`，但身份/在线未确认 | 先用 Viewer/SDK 核对 SN/IP，禁止写死 |
| 雷达外参 | 官方 JSON 模板为全 0 | 取得 H1 实际标定；不发布伪造 TF |
| 雷达 ROS topic 类型 | 同一 `/livox/lidar` 随 `xfer_format` 改变 | 每次以 `ros2 topic list -t/type` 为准 |
| SDK2/ROS 雷达 JSON | SDK2 quick-start 使用数组式 `host_net_info`，固定 ROS 驱动使用对象式字段和 `lidar_configs` | 两套模板分别维护，禁止复制字段混用 |
| 雷达外参的应用位置 | ROS 驱动可在 JSON 中烘焙外参，TF 也可表达同一外参 | 只选一种；推荐 JSON 为整数零、用真实 TF 表达 |
| ROS 雷达 IMU 加速度 | 固定驱动把协议中的 g 原值写进要求 m/s² 的 `linear_acceleration` | 实测轴向/量纲后乘 `9.80665`，补 covariance，不能直接送融合器 |
| ROS 雷达时间来源 | PTP/gPTP/GPS 可用设备时间，NoSync 回退主机时钟，消息不暴露原始 `time_type` | 旁路记录同步模式、驱动版本与 `source_clock_domain` |

### `/opt/Roboshop` 的边界

本机 `/opt/Roboshop` 的专有 GUI/标定库字符串中出现 Mid-360 和相机外参标定相关内容，说明厂商软件可能包含内部标定能力。但它没有随附公开调用文档，当前未运行，也不是 H1 SDK 的公开雷达 API。因此不能把它当成可编程 SDK 或假定会发布 ROS topic；需要使用时应向 ZERITH 获取正式操作手册和标定输出格式。

## 23. 资料索引

### 23.1 本机资料

- [ZERITH H1 PRO 产品使用手册 V2.0](</home/robot/ZERITH H1 PRO产品使用手册 V2.0.pdf>)
- [ZERITH H1 PRO 系列 SDK 开发指南 V4.0](</home/robot/ZERITH H1 PRO系列SDK开发指南 V4.0.pdf>)
- [H1_Robot.hpp](/home/robot/H1_SDK_1.3.9/robot_SDK/include/H1_Robot.hpp)
- [config.hpp](/home/robot/H1_SDK_1.3.9/robot_SDK/include/config.hpp)
- [Python 绑定类型桩](/home/robot/H1_SDK_1.3.9/h1_sdk_v1.3.9_python3.10/lib/lib_h1_sdk_python.pyi)
- [相机 Python SDK README](/home/robot/H1_SDK_1.3.9/camera_sdk_python/README.md)
- [相机 Python 示例目录](/home/robot/H1_SDK_1.3.9/camera_sdk_python/example/)
- [相机 C++ 头文件](/home/robot/H1_SDK_1.3.9/camera_sdk_cpp/include/camera_client.h)
- [本机 RealSense 配置](/etc/robot/cams_realsense.yaml)
- [当前控制工具说明](/home/robot/control/README.md)
- [状态读取工具](/home/robot/control/read_robot_state.py)
- [安全运动 CLI](/home/robot/control/send_robot_command.py)
- [SDK 公共辅助代码](/home/robot/control/h1_sdk_common.py)

PDF 页码在本文中按 PDF 文件的物理页序号理解；阅读器显示的印刷页码可能不同。

### 23.2 Livox 官方资料

- [Livox SDK2 官方仓库](https://github.com/Livox-SDK/Livox-SDK2)
- [本次核对的 Livox SDK2 固定版本 README](https://github.com/Livox-SDK/Livox-SDK2/blob/08f523c930b2f0ba1e98a6afaa8d7476bf479908/README.md)
- [固定版本 Livox SDK2 公开 API 头文件](https://github.com/Livox-SDK/Livox-SDK2/blob/08f523c930b2f0ba1e98a6afaa8d7476bf479908/include/livox_lidar_api.h)
- [固定版本 SDK2 Mid-360 quick-start 配置](https://github.com/Livox-SDK/Livox-SDK2/blob/08f523c930b2f0ba1e98a6afaa8d7476bf479908/samples/livox_lidar_quick_start/mid360_config.json)
- [livox_ros_driver2 官方仓库](https://github.com/Livox-SDK/livox_ros_driver2)
- [本次核对的 ROS Driver 2 固定版本 README](https://github.com/Livox-SDK/livox_ros_driver2/blob/4a1def929e5b59c7a8122d19fce6efba581ce9f7/README.md)
- [Mid-360 ROS 配置模板](https://github.com/Livox-SDK/livox_ros_driver2/blob/4a1def929e5b59c7a8122d19fce6efba581ce9f7/config/MID360_config.json)
- [固定版本 ROS 2 launch 文件目录](https://github.com/Livox-SDK/livox_ros_driver2/tree/4a1def929e5b59c7a8122d19fce6efba581ce9f7/launch_ROS2)
- [Livox Viewer 2 / Mid-360 下载与手册](https://www.livoxtech.com/cn/mid-360/downloads)

## 24. 最短调用速查

### 机器人状态（只读）

```bash
source /home/robot/miniconda3/etc/profile.d/conda.sh
conda activate zerith
cd /home/robot/control
python read_robot_state.py --count 1
```

### 机械臂控制计划（只 dry-run）

```bash
python send_robot_command.py joint \
  --arm left \
  --delta 0 0 0 0 0 0 0.02 \
  --duration 3 \
  --rate 100
```

### 三路相机官方综合示例

```bash
source /home/robot/miniconda3/etc/profile.d/conda.sh
conda activate zerith
cd /home/robot/H1_SDK_1.3.9/camera_sdk_python
python example/05_full_demo.py
```

这会打开 GUI 窗口，需要图形会话；无图形环境使用第 9.2 节的无窗口循环。

### 雷达（安装驱动并确认 IP 后）

```bash
source /opt/ros/jazzy/setup.bash
source /home/robot/workspace/ws_livox/install/setup.bash
ros2 launch livox_ros_driver2 rviz_MID360_launch.py
```

另一个终端：

```bash
ros2 topic list -t | grep livox
ros2 topic hz /livox/lidar
ros2 topic hz /livox/imu
```

---

这份文档描述的是已交付 SDK 1.3.9 和当前机器的核验结果。升级 SDK、固件、相机服务、ROS 驱动或更换传感器后，应重新执行接口反射、动态发现、限位/单位核对、时间同步和标定验收，而不是默认所有字段与数值保持不变。
