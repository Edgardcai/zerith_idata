# 数据采集遥操扩展 1.0

适用于本机 ZERITH-H1 1.3.9，与独立采集网站 `/home/robot/collection_web` 配套，网页地址 `http://172.16.18.43:8090`。不依赖 `/home/robot/control`，不修改厂商安装文件、Motion_Control、SDKService 或原始采集数据。

## 当前功能

- 腰 pitch/yaw、头 yaw/pitch 的控制目标持续为 0；原始反馈保持真实值。
- **角度偏差只预警，不阻止启动、不暂停遥操、不取消录制。** 原厂急停、电机/通信/VR 故障保护仍保留。归零过程不适合作为正式训练数据，页面会提示等待稳定。
- 网站醒目显示“未初始化 / 待标定 / 标定成功 / 遥操作中”，标定成功弹出提示；点击“开启语音提示”可由当前浏览器播报，需电脑/浏览器声音可用。
- 可选固定升降柱高度，范围 0–0.8 m。当前配置默认启用 0.4 m。先反初始化，再在网页保存设置；下一次长按 A 初始化时沿原厂平滑轨迹到指定高度，之后手柄、身体高度变化和 X 锁定/解锁不能覆盖设定。
- 升降柱的运动设置与采集目录中的“高度标签”独立，标签不会发送运动指令。
- 原厂初始化、反初始化与录制键保留。反初始化后不再主动保持升降柱，位置可能随原厂流程改变。

## 使用

1. 打开 8090 网页。需要固定高度时，在“固定升降柱”勾选并输入数值，反初始化状态下点击“保存高度”。不需要固定时取消勾选并保存。
2. 长按 A 初始化，再短按 A 标定。网站明确显示“标定成功 · 短按 A 启动遥操”。
3. 再短按 A 启动。位置偏差仅在网页显示预警，不会再由本扩展触发暂停。
4. 网页启动采集会话后，按原厂方式长按 Y 开始、短按 Y 完成阶段/结束本条（以任务阶段配置为准）。等待保存完成后再反初始化。
5. 录制时页面的 A/B/F 等级是人工评级，不代表头腰误差检查已自动通过。

## 实现与接口

`patch/zero_lock.py` 在唯一 teleop 进程内修改已核验的厂商控制方法，不创建第二个机器人控制发布者。

- IK 模型四个旋转轴的运动变换被冻结。固定升降柱时，将指定高度折入升降柱父变换，再冻结该自由度，保持 FK/Jacobian 和输出约束一致。
- 控制器中用于发布实际末端反馈的模型保持原样，HIGH_LEVEL 模型不参与约束。
- 最终 `head_control`/`waist_control` 输出仍经过约束；固定高度模式同时去除升降柱速度和遥操高度偏移。
- 头部仍使用已测的缓慢重力前馈：KP/KD 保持原厂值，总补偿 ±0.6 N·m、变化率 0.05 N·m/s、积分 ±0.15 N·m。角度存在实际伺服误差，不能承诺数学意义上的绝对零值。
- `config.json` 保存 `lift_enabled` 和 `lift_height_m`，仅在下次初始化读取。网站接口为 `POST /api/teleop/config`，沿用页面令牌和来源校验。
- `runtime/status.json` 约 5 Hz 更新，含 `operator`、`calibration_seq`、`warnings`、实际角度、升降柱目标/实测高度、当前应用配置。`ready` 表示位置稳定，不再控制角度超差暂停；故障保护独立。
- 状态接口由网站读文件接入，不导入遥操运行依赖。平常超过 3 秒未更新显示状态中断，原厂阻塞初始化期间最长允许 15 秒。

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

恢复同一窗口的 `/opt/robot/teleop`。开机启动配置没有被替换；机器人服务重启后仍使用厂商原程序，需要按上述步骤重新启用扩展。试验日志和缓存均在本目录 runtime 中。
