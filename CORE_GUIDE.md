# ZERITH H1 PRO 控制与语音系统核心说明

本文档说明本仓库的整体结构、启动方式、安全边界和常用维护命令。更细的 SDK、相机、
语音协议和模型说明分别位于各模块 README。

> **安全警告**：本项目可以控制真实机器人。任何实机动作前必须清空工作空间、确认实体
> 急停可达，并检查所有电机错误码。网页“停止”只是软件停止，不能代替实体急停。

## 1. 系统架构

```text
浏览器 :8080
  -> web_control.server
     -> RobotService：唯一 H1 SDK 持有者、租约、初始化、轨迹和 watchdog
     -> CameraService：左腕 / 头部 / 右腕 RGB-D，相机原始帧 640x480
     -> VoiceMotionInternalServer :8766（仅回环）
     -> VoiceGateway -> voice_assistant :8765（仅回环）

机器人独立麦克风 -> voice_assistant
  -> 中文：Silero VAD -> FunASR Paraformer -> Qwen3-ASR 句末复核
     -> 对话/固定动作白名单 -> Qwen3-TTS Serena
  -> English：保留原有远程 STT/TTS 和声音链路

中文 ASR :8770（仅回环）
中文 TTS :8771（仅回环）
```

语音进程不加载 H1 SDK。所有语音动作最终仍通过网页进程中的单一 `RobotService` 串行
执行，避免多个 SDK 客户端同时控制机器人。

## 2. 目录

| 路径 | 作用 |
|---|---|
| `web_control/` | 网页、机器人控制服务、三路 RGB-D、语音动作桥接 |
| `voice_assistant/` | 机器人麦克风、VAD、对话、英文语音链路、动作意图 |
| `chinese_speech/` | 本地中文 Paraformer/Qwen3-ASR 与 Qwen3-TTS 服务 |
| `h1_sdk_common.py` | SDK 加载、关节表、错误码与公共校验 |
| `read_robot_state.py` | 只读机器人状态工具 |
| `send_robot_command.py` | 默认 dry-run 的安全数值控制 CLI |
| `systemd/`、`*/systemd/` | 系统服务和用户服务模板 |
| `tests/`、`*/tests/` | 不连接实机的自动化测试 |

厂商 H1 SDK、Qwen/FunASR 模型、API 密钥和运行日志不包含在 Git 仓库中。

## 3. 环境要求

- Ubuntu/Linux、systemd user service、PipeWire。
- NVIDIA GPU 推荐；中文 Qwen3-ASR/TTS 使用 CUDA/bfloat16。
- `/home/robot/miniconda3` 下的 Conda。
- `zerith`：Python 3.10，加载厂商 H1 Python SDK。
- `xiaoda-asr`：Python 3.10。
- `xiaoda-tts`、`xiaoda-voice`：Python 3.12。
- 厂商 SDK 默认路径见 `h1_sdk_common.py`，可用
  `ZERITH_H1_PYTHON_SDK_ROOT` 覆盖。

## 4. 首次安装

```bash
cd /home/robot/control

# 语音助手依赖和离线唤醒/VAD模型
bash voice_assistant/install.sh

# 中文 ASR/TTS 独立环境和 systemd unit
bash chinese_speech/install.sh

# 安装网页与语音用户服务
mkdir -p /home/robot/.config/systemd/user
install -m 0644 web_control/systemd/zerith-h1-web-control-user.service \
  /home/robot/.config/systemd/user/zerith-h1-web-control.service
install -m 0644 voice_assistant/systemd/zerith-xiaoda-voice.service \
  /home/robot/.config/systemd/user/zerith-xiaoda-voice.service
systemctl --user daemon-reload
```

中文模型会在服务第一次启动时下载到用户缓存。详细的手动下载命令见
`chinese_speech/README.md`。

## 5. 私有配置

密钥不要写进仓库。可以继续使用 `/home/robot/api.txt`，也可以从示例创建仅本机可读的
环境文件：

```bash
mkdir -p /home/robot/.config/zerith
install -m 0600 config/voice.env.example /home/robot/.config/zerith/voice.env
# 编辑 voice.env，将 replace_me 替换为真实配置
```

`zerith-xiaoda-voice.service` 会可选读取这个文件；文件不存在时仍可从
`/home/robot/api.txt` 和进程环境读取配置。

## 6. 开机启动与维护

```bash
systemctl --user enable --now \
  zerith-chinese-asr.service \
  zerith-chinese-tts.service \
  zerith-xiaoda-voice.service \
  zerith-h1-web-control.service
```

重启整套服务：

```bash
systemctl --user restart \
  zerith-chinese-asr.service \
  zerith-chinese-tts.service \
  zerith-xiaoda-voice.service \
  zerith-h1-web-control.service
```

查看状态和日志：

```bash
systemctl --user --no-pager status \
  zerith-chinese-asr.service \
  zerith-chinese-tts.service \
  zerith-xiaoda-voice.service \
  zerith-h1-web-control.service

journalctl --user -u zerith-h1-web-control.service -f
journalctl --user -u zerith-xiaoda-voice.service -f
curl -s http://127.0.0.1:8770/health | python -m json.tool
curl -s http://127.0.0.1:8771/health | python -m json.tool
```

默认网页地址为 `http://172.16.18.43:8080/`。更换机器人 IP 时，同时修改网页 systemd
unit 的 `--host` 参数。生产网络建议配置 `H1_WEB_CONTROL_TOKEN`，不要长期使用
`--allow-unauthenticated-lan`。

## 7. 当前语音动作

语音/键盘运动控制默认关闭，只有网页持有活动租约、完成初始化并由操作者显式开启后才
允许动作。

| 指令 | 当前行为 |
|---|---|
| `forward` / 前进 | 左右轮 1.5 rad/s，1 秒 |
| `backward` / 后退 | 左右轮 -1.5 rad/s，1 秒 |
| `turn_left` / 左转 | 原地左转，3.5 秒 |
| `turn_right` / 右转 | 原地右转，3 秒 |
| `turn_around` / 转身 | 固定原地转动，8 秒；不是标定角度 |
| `wave` / 挥手 | 左臂五次后右臂五次，端点停 0.5 秒，最后回零 |
| `handshake` / 握手 | 右肩/肘握手姿态停 10 秒后回零 |
| `stop` / 停止 | 取消软件轨迹并发送底盘零速 |

握手 10 秒静态保持会增加右肩 R1 发热风险。停留阶段会监控右臂错误码并在错误出现时
停止该臂后台保持，但这不能代替温度管理和实体急停。R1 曾出现 `0x0008`（过热）时，
必须先冷却、排除机械卡阻并按厂商流程复位，不能直接反复重试动作。

## 8. 测试

测试使用假 SDK/假相机，不会驱动真实机器人：

```bash
cd /home/robot
/home/robot/miniconda3/envs/zerith/bin/python \
  -m unittest discover -s control/web_control/tests -t . -v

cd /home/robot/control
/home/robot/miniconda3/envs/xiaoda-voice/bin/python \
  -m unittest discover -s voice_assistant/tests -v
/home/robot/miniconda3/envs/xiaoda-asr/bin/python \
  -m unittest discover -s chinese_speech/tests -v
```

## 9. 进一步文档

- `README.md`：H1 状态和安全数值控制完整说明。
- `web_control/README.md`：网页 API、相机和控制安全设计。
- `voice_assistant/README.md`：小达语音、对话和运动白名单。
- `chinese_speech/README.md`：中文模型、协议、下载和性能实测。
- `H1_ARM_CAMERA_LIDAR_GUIDE.md`：机械臂、RGB-D 与激光雷达综合指南。
