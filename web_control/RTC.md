# 8080 控制服务的 RTC 推理

默认仍为 `sync`：30 Hz，每次执行 30 步后请求。新增 `scheduling_mode=rtc`，与已有的任务分类 `inference_mode=custom/single/dual_continuous/dual_separate` 独立。

网页刷新后，在“推理配置”选择“RTC 异步推理”，连接目标服务器，然后执行 dry-run。RTC 固定为 30 Hz，使用完整 50 帧 buffer，“每包执行步数”仅用于同步模式。真机启动仍需要原有控制租约、初始化状态、dry-run 和确认操作。

## 修改位置

| 文件 | 改动 |
|---|---|
| `pi05_protocol.py` | 沿用 JSON WebSocket，新增 RTC observation/response 适配，校验物理动作及模型历史 |
| `pi05_rtc.py` | metadata、参数、标识符、历史后缀、延迟估计与完整 chunk 游标 |
| `pi05_rtc_runtime.py` | 单网络工作线程、30 Hz 控制周期、预热、dry-run、按实际延迟接入与故障处理 |
| `pi05_executor.py` | 接入现有运行/停止/租约状态机，保留同步执行入口及动作范围/限速规则 |
| `server.py` | dry-run/start API 透传 `scheduling_mode` 与 `rtc_options` |
| `pi05_cli.py` | RTC 参数及预热 HTTP 超时；正常释放租约但保留已初始化 SDK 时明确提示 |
| `static/index.html`、`static/app.js` | sync/rtc 选择器、RTC 状态和游标显示 |
| `tests/test_pi05_rtc.py` | 协议、时钟、线程、故障、假 SDK/WebSocket 和 CLI 回归测试 |

## 时序及动作历史

一个网络工作线程独占 metadata/inference 的 send/recv，最多一个待处理请求/结果，不积压观测。图像采集、编码、状态读取及网络推理都在工作线程进行。采集 guided 观测时验证采集前后游标、generation 和控制 tick 一致；若无法在最大观测年龄内取得一致快照则停止。

新响应完整保留 50 帧；实际控制交接时重新计算 `k=handoff_tick-observation_tick`，从索引 k 执行。接下来以完整 chunk 原始游标 s 发 `executed_steps=execution_horizon=s` 和 `[s:]` 后缀。若采样时已从触发阈值 15 前进到 16，则发送 s=16 和 34 行历史。

客户端已有执行范围、分阶段手臂保持及关节限速：这些处理提前应用于完整待执行 buffer，后续统一发送对应的 **physical previous_actions**（50-s × 23）。`actions_model` 只做存储与协议校验，绝不用于下发电机。没有增加平滑算法，也不对二值夹爪插值。

使用单调时钟检测漏周期；发生漏周期时暂停/故障，不快速补发过期命令。超时、旧 session/epoch/generation、错误形状、NaN/Inf、缓冲耗尽或不可行延迟均进入现有停止/保持和故障锁存流程。停止关闭在途网络连接，重连/恢复必须重新 dry-run 和 bootstrap。双手分阶段执行沿用原有阶段完成与归位流程，切换阶段时丢弃旧响应、递增 phase_epoch、清空 buffer。

## 实际 metadata 兼容性

支持已确认的 Zerith 23 维物理状态/动作、50 帧 horizon、32 维模型历史，训练维度允许 16 或 17；补零起点必须与 metadata 中的训练维度相同。

2026-09-19 实测 `ws://192.168.1.154:9979` 为 **16 维训练**，16:32 为补零，升降柱由服务端保持。明确识别 metadata 别名 `lift.target_height`、`base.linear_velocity`、`base.angular_velocity`，响应仍使用 `lift.height` 和 `speed.linear/angular`。其余顺序变化会被拒绝。动作空间 ID 或夹爪模式在 dry-run 后改变时要求重新验证。

## CLI 示例

在 `/home/robot/control` 中使用 `/home/robot/miniconda3/envs/zerith/bin/python`。

先连接（只连接推理服务器）：

```bash
python inference_executor.py reconnect --web-url http://127.0.0.1:8080 --host 192.168.1.154 --port 9979
```

RTC dry-run（真实相机/状态、两分支预热、3 次模拟交接，不创建机器人动作执行会话）：

```bash
python inference_executor.py dry-run --web-url http://127.0.0.1:8080 --auto-takeover --scheduling-mode rtc --execution-mode arms --prompt 'Grasp Yili Strawberry Yogurt with the left hand and then grasp Sprite with the right hand.'
```

自动接管会在结束时释放租约，因此随后真机启动应使用 `run --prepare` 重新预检；不会自动初始化或反初始化机器人。如果已有网页持有租约，请从网页执行 dry-run，或明确提供自己的 `--lease`。

下列两条是真机执行命令，**本次没有运行**；需要用户已完成原有初始化/控制模式设置，并主动确认执行。

同步执行：

```bash
python inference_executor.py run --web-url http://127.0.0.1:8080 --auto-takeover --prepare --policy-host 192.168.1.154 --policy-port 9979 --scheduling-mode sync --steps-per-chunk 30 --control-rate-hz 30 --execution-mode arms --prompt 'Grasp Yili Strawberry Yogurt with the left hand and then grasp Sprite with the right hand.' --confirm-motion
```

RTC 执行：

```bash
python inference_executor.py run --web-url http://127.0.0.1:8080 --auto-takeover --prepare --policy-host 192.168.1.154 --policy-port 9979 --scheduling-mode rtc --execution-mode arms --rtc-trigger-index 15 --rtc-delay-initial-steps 6 --rtc-num-denoising-steps 10 --prompt 'Grasp Yili Strawberry Yogurt with the left hand and then grasp Sprite with the right hand.' --confirm-motion
```

## 参数和诊断

| rtc_options 字段 | 默认值 |
|---|---:|
| trigger_index | 15 |
| delay_initial_steps | 6 |
| safety_reserve_steps | 2 |
| num_denoising_steps | 10（可选 1～50） |
| max_guidance_weight | 5.0（0～5） |
| delay_window | 20 |
| delay_margin_steps | 1 |
| max_observation_age_s | 0.25 |
| warmup_timeout_s | 120 |
| dry_run_chunks | 3 |

延迟估计使用最近 20 次完整“观测到控制接入”延迟的最大值加 1 步裕量，并保留初始下限。超过约束不会裁小实测值。必须满足 `0 <= d <= s` 且 `s+d+reserve <= 50`；触发阈值根据估计 d 向后调整，再按实际游标检查。预热耗时不计入稳定运行统计。

API `rtc_options` 可覆盖表中参数。`/api/pi05/status` 的 `rtc` 包含 metadata、generation、cursor、session/epochs、延迟样本、最近 chunk 的 s/d/k、历史类型/形状、相机帧年龄、观测时刻、服务端耗时、剩余帧数及异常计数。

## 验证记录

- 实际通过 8080、真实相机与只读机器人反馈，对 9979 完成 bootstrap/guided 预热及 3 次模拟交接。
- 三次实际前缀跳过 k：5、8、7；观测至可接入耗时约 167、266、234 ms。
- 两次 guided 使用实际 s=16、34×23 的 physical history；保守延迟估计更新为 9 步。
- 漏周期、buffer underrun 均为 0；`executed_steps=0`，未创建真机执行 session，未执行初始化/反初始化。
- 第一次 CLI 结束时遇到既有 SDK 释放规则的 409：机器人已初始化，禁止卸载 SDK。dry-run 响应已成功保存，租约已释放。CLI 已补充识别该情形，仅在确认租约已释放、无活动 policy session 后保留 SDK 并提示，不再把已成功的 dry-run 显示成失败。
- **尚未进行 RTC 真机运动验证**。真实 SDK 控制周期是否稳定满足 30 Hz 需后续用户主动验证。
- RTC 与原有推理相关 124 项测试通过，JavaScript 语法检查通过。
- 完整测试集中存在修改前已有的网页“机器人独立麦克风”文案断言失败，与 RTC 无关。

原文件备份、逐文件补丁、metadata、真实 dry-run JSON 和测试日志：`/data/diagnostics/rtc_8080_20260919/`。
