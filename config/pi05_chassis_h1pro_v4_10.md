# H1 Pro V4.10 推理底盘参数

配置文件：`pi05_chassis_h1pro_v4_10.json`。

本机 8080 服务通过 `PI05_CHASSIS_CONFIG` 加载此文件。仅全 23 维模式将
action[21] 线速度（m/s）和 action[22] 角速度（rad/s）换算成 LOW_LEVEL 左右轮速度。
16/17 维模式仍将底盘目标置零。

## 参数依据

- SDK 文档：`/home/robot/ZERITH H1 PRO系列SDK开发指南 V4.0.pdf`。
  第 2.2.3 节规定电机方向参考 URDF；`setChassis_low` 的枚举索引 0 为左轮、
  1 为右轮，`Speed` 单位为 rad/s。`setChassis_high` 仅在 HIGH_LEVEL 可用，
  不能与当前 LOW_LEVEL 的逐关节执行混用。
- SDK 示例：`/home/robot/H1_SDK_1.3.9/h1_sdk_v1.3.9_python3.10/h1_sdk_v1.3.9_python3.10/example/h1_low_level.py`
  的 `chassis_test`：前进给左右轮同时 +1，后退同时 -1，因此 `left_sign=right_sign=1`。
- URDF：`/data/urdf/Zerith_URDF_2026.08.12/urdf/ZR_H1PRO-1.2.00.H.V4.10_URDF_2026.08.12.urdf`。
  驱动轮为 `left_middle_wheel_joint` 和 `right_middle_wheel_joint`，共同父节点
  `dipan_link`，中心坐标分别为 `(0.0544, 0.1895, -0.0965)` 和
  `(0.0544, -0.1895, -0.0965)` 米，故轮距为 0.379 米；两侧轴均为 `(0,1,0)`。
- URDF 引用的左右 `middle_wheel_link.STL` 未设置额外缩放或视觉原点偏移。
  对二进制 STL 顶点计算到 Y 轴的最大距离 `sqrt(x*x+z*z)`，两侧均为
  `0.0835000036` 米（网格浮点误差），取名义半径 0.0835 米。前后小轮不用于驱动轮半径。

以上尺寸来自用户指定模型，不是实机滚动标定值。实机轮胎压缩/滑移造成的里程误差
仍需后续标定；此次不运行真机运动测试。

## 换算与验证例子

约定 v 正为前进，w 正为绕 +Z 左转（俯视逆时针）：

```text
left_rad_s  = (v - w * 0.379 / 2) / 0.0835
right_rad_s = (v + w * 0.379 / 2) / 0.0835
```

- v=+0.0835，w=0：左右轮均 +1 rad/s。
- v=-0.0835，w=0：左右轮均 -1 rad/s。
- v=0，w=+0.167/0.379：左轮 -1，右轮 +1 rad/s，原地左转。
- v=+0.0835，w=+0.167/0.379：左轮 0，右轮 +2 rad/s，前进左转。

软件验证覆盖配置加载、以上正反方向、SDK 左右电机实际收到的速度和停止时两轮归零。
硬件急停、故障及控制会话检查继续保留；全维度动作不增加限速裁剪或姿态覆盖。
