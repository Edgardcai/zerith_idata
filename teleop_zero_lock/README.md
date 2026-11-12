# 数据采集遥操扩展 1.1：自定义固定姿态

适用于本机 ZERITH-H1 1.3.9，与独立采集网站 `/home/robot/collection_web` 配套，网页地址 `http://172.16.18.43:8090`。不依赖 `/home/robot/control`，不修改厂商安装文件、Motion_Control、SDKService 或原始采集数据。

## 当前功能

- 腰 pitch/yaw、头 yaw/pitch 可独立设置固定目标，单位 rad，默认全部为 0；反馈保留真实测量值。
- **角度偏差只预警，不阻止启动、不暂停遥操、不取消录制。** 原厂急停、电机/通信/VR 故障保护仍保留。归零过程不适合作为正式训练数据，页面会提示等待稳定。
- 网站醒目显示“未初始化 / 待标定 / 标定成功 / 遥操作中”，标定成功弹出提示；点击“开启语音提示”可由当前浏览器播报，需电脑/浏览器声音可用。
- 可选固定升降柱高度，范围 0–0.8 m，沿用已有配置。先结束采集会话并反初始化，再保存姿态；下次长按 A 初始化时平滑到指定腰部角度及高度，之后手柄、身体高度变化和 X 锁定/解锁不能覆盖设定。腰部初始化峰值角速度不超过 0.15 rad/s，头部随后按不超过 0.03 rad/s 平滑到指定角度。
- 升降柱的运动设置与采集目录中的“高度标签”独立，标签不会发送运动指令。
- 原厂初始化、反初始化与录制键保留。反初始化后不再主动保持升降柱，位置可能随原厂流程改变。

## 使用

1. 打开 8090 网页。结束采集会话并反初始化，输入腰 pitch、腰 yaw、头 yaw、头 pitch，点击“保存姿态”。例如腰 pitch 填 0.5，其余填 0。需要固定高度时保留“固定升降柱”及原高度。
2. 长按 A 初始化，再短按 A 标定。网站明确显示“标定成功 · 短按 A 启动遥操”。
3. 再短按 A 启动。位置偏差仅在网页显示预警，不会再由本扩展触发暂停。
4. 网页启动采集会话后，按原厂方式长按 Y 开始、短按 Y 完成阶段/结束本条（以任务阶段配置为准）。等待保存完成后再反初始化。
5. 录制时页面的 A/B/F 等级是人工评级，不代表头腰误差检查已自动通过。

头部角度是相对上身的关节角。腰部前倾 0.5 rad、头 pitch 为 0 时，头部随上身前倾。范围取模型与原厂控制器的交集：腰 pitch 0～1.309、腰 yaw −0.9～0.9、头 yaw −1.5708～1.5708、头 pitch −0.7854～0.5236 rad。模型限位不能替代现场避碰及实际保持误差验收。

## 实现与接口

`patch/zero_lock.py` 在唯一 teleop 进程内修改已核验的厂商控制方法，不创建第二个机器人控制发布者。

- IK 模型先将四个目标角对应的变换折入父变换，再冻结旋转轴；固定升降柱同样折入指定高度。真实及身高缩放模型、FK/Jacobian 与控制输出使用同一姿态，重标定不累积变换。
- 控制器中用于发布实际末端反馈的模型保持原样，HIGH_LEVEL 模型不参与约束。
- 最终 `head_control`/`waist_control` 输出仍经过约束；固定高度模式同时去除升降柱速度和遥操高度偏移。
- 头部仍使用已测的缓慢重力前馈：KP/KD 保持原厂值，总补偿 ±0.6 N·m、变化率 0.05 N·m/s、积分 ±0.15 N·m。角度存在实际伺服误差，不能承诺数学意义上的绝对零值。
- `config.json` 保存 `lift_enabled`、`lift_height_m`、`waist_pitch_rad`、`waist_yaw_rad`、`head_yaw_rad`、`head_pitch_rad`，仅在下次初始化读取。旧配置缺少的角度默认为零。`patch/pose_config.py` 为网站与遥操共用的纯标准库配置校验。网站接口为 `POST /api/teleop/config`，沿用页面令牌和来源校验。
- H1 PRO 头部反馈原始顺序为 yaw、pitch，修正原厂内部 `head_pos` 的交换，保证非零姿态时内部测量与模型一致。总线反馈及采集 state 不被改写。
- 每条数据的 `collection_task.json` 的 `targets.fixed_pose` 保存当次实际应用配置、目标角和会话开始实测角。核验工具按该目标计算误差，兼容旧的零位数据。
- `runtime/status.json` 约 5 Hz 更新，含 `operator`、`calibration_seq`、`warnings`、实际角度、升降柱目标/实测高度、当前应用配置。`ready` 表示位置稳定，不再控制角度超差暂停；故障保护独立。
- 状态接口由网站读文件接入，不导入遥操运行依赖。平常超过 3 秒未更新显示状态中断，原厂阻塞初始化期间最长允许 15 秒。

### VR 与 9999 切换

`teleop` 进程同时承载模式 3 的重力补偿电机执行循环，使用 9999 时也需要保留它。
2026-10-15 修复了原厂标定等待只检查内部 `is_initialized` 标志的问题：等待前和每次轮询均检查实际 `control_mode == 0`、`init_state == 2`，模式切走或反初始化后立即退出，返回外层模式分派循环。切换期间的 A 键事件不会继续触发 VR 标定。

从 VR 转到 9999 时，先结束采集并完成反初始化，再在 9999 选择重力补偿模式、初始化，最后发送新目标。`runtime/status.json` 的 `operator.control_mode` 和 `operator.motor_control_active` 可用于核对实际模式与电机执行循环是否一致。修复包包含对原厂函数的 12 项离线回归检查，全部使用内存回调，不连接设备。

## 构建与测试

需要本机厂商二进制副本 `vendor/teleop.original`，SHA256 必须为：

`3c807604a116128c27bcf0ca52310a4102e436c1a277af1c0301b1ef452f7282`

```bash
cd /home/robot/teleop_zero_lock
/home/robot/miniconda3/envs/zerith/bin/python -m unittest discover -s tests -p 'test_*.py'
/home/robot/miniconda3/envs/zerith/bin/python tools/build_trial.py --output runtime/teleop_zero_lock_candidate
./runtime/teleop_zero_lock_candidate --offline-self-test
```

离线入口首先禁止真实 ZCM 构造，控制路径使用内存总线；测试真实厂商 IK、初始化、控制输出、标定模型变化。自检通过标记绑定候选二进制 SHA256，不能拿旧自检结果启动新代码。构建仅替换 teleop 入口，其余 643 个归档条目逐字节校验不变。禁止覆盖正在运行的 PyInstaller 文件。

用户已授权切换时，结束采集会话并反初始化后执行：

```bash
python3 tools/switch_trial.py plan
# 当前运行原厂版本时：
sudo python3 tools/switch_trial.py start --operator-ready
# 当前运行本扩展、且候选版已完成自检时：
sudo python3 tools/upgrade_trial.py
```

候选包可以放在独立临时构建目录，使用 `tools/upgrade_trial.py --candidate /绝对路径/teleop_zero_lock_candidate --check` 先只读核验，再去掉 `--check` 切换。工具核对候选包自检、当前运行版本、空闲状态和开机配置，更新匹配的 build/validated/开机哈希，等待新进程实时状态确认；旧程序与元数据分别保留在 `runtime/teleop_zero_lock.previous`、`runtime/previous-release.json`。构建可用 `--vendor` 指定原厂输入、`--source-commit` 记录源码提交。

操作员明确允许后台保存继续时，可在 `plan` 或 `start` 命令后加 `--allow-pending-save`，允许会话等待或保存期间切换。此参数不会取消保存或删除数据；正在录制、未反初始化或设备检查失败仍拒绝切换。

若启动失败后原厂和扩展遥操都已退出，且该 tmux 窗口停在 shell，可执行 `sudo python3 tools/switch_trial.py start --operator-ready --recover-stopped`。切换工具会等待扩展进程对应的实时状态，失败时显示窗口输出。`--allow-pending-save` 已同步传入二进制内部检查，修改内部检查必须重建并重新自检。

只切换 `robot_startup:teleop` 一个 tmux 窗口。切换工具不会强杀机器人进程，不重启整套 robotd，不因额外电量阈值拒绝操作。

## 监测与数据核验

```bash
python3 tools/monitor_trial.py --seconds 600
/home/robot/miniconda3/envs/zerith/bin/python tools/analyze_recording.py \
  /data/zerith_data/数据集/episode_000001 --output runtime/episode_audit.json
```

分析脚本仅打开已结束的 episode，核查全帧数值/时间戳、四轴目标和反馈，抽查图像字段长度与样本非空；不修改训练数据。软件测试和真机验收范围见 `TEST_REPORT.md`。

## 回退

结束采集并反初始化后：

```bash
sudo python3 tools/switch_trial.py restore --operator-ready
```

恢复同一窗口的 `/opt/robot/teleop`。2026-09-10 起，本机已通过 collection_autostart 的 robotd drop-in 默认直接启动锁腰扩展；手动回退只影响当前运行，下次开机仍选择锁腰版。试验日志和缓存均在本目录 runtime 中。
