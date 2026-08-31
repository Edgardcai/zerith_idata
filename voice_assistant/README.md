# 小达语音助手

这是 ZERITH H1 PRO 的独立语音进程。当前部署以网页单轮语音对话为主，并支持由网页
操作者显式开启的受限语音运动控制。运动功能默认关闭。

## 当前链路

```text
会话语言路由
  → 中文：机器人独立讯飞麦克风 → Silero VAD → FunASR Paraformer
     → 句末 Qwen3-ASR-0.6B 复核 → 本地 Qwen3-TTS-0.6B Serena
  → English：原机器人麦克风、原远程 STT、原模型/声音/接口/整段 TTS（完全保留）
  → 明确的受支持动作：本地规则直接下发，不等待大模型
  → 其他疑似动作：GPT-5.5 只做严格白名单分类；不能安全映射就拒绝
  → 普通问答：GPT-5.5 Responses API 流式生成、多轮上下文
     首选实测低延迟的 AIHubMix，失败时自动切换局域网接口
  → 内置 C-Media 扬声器
```

中文服务的安装、协议、热词和降级说明见 `chinese_speech/README.md`。

小达说话期间暂停录音，回答结束后再开始下一轮倾听。这是为了在尚未标定声学回声消除
之前避免扬声器声音回灌。它目前是流畅的半双工对话，不支持用户在播报中途插话。

`api.txt` 只在运行时读取，密钥不会复制到本目录，也不会写入日志。局域网 LLM 请求
明确绕过系统 HTTP 代理，避免私网地址被代理成 502。当前实测 AIHubMix 的 GPT-5.5
约 2 秒完成短回复，而局域网网关波动到约 20 秒，所以默认远端优先；设置
`XIAODA_PREFER_LOCAL=1` 可改为局域网优先。

## 安装

安装使用独立的 `xiaoda-voice` Conda 环境，不修改机器人原有的 `zerith` 环境：

```bash
cd /home/robot/control
bash voice_assistant/install.sh
```

安装脚本会下载约 40 MB 的离线唤醒模型和约 2 MB 的 VAD 模型。

## 验收顺序

```bash
cd /home/robot/control
VOICE_PY=/home/robot/miniconda3/envs/xiaoda-voice/bin/python

# 配置、依赖、模型和设备名
$VOICE_PY -m voice_assistant doctor

# 不使用麦克风，先验证 GPT-5.5；加 --no-speak 可静音
$VOICE_PY -m voice_assistant text "你好，请介绍一下自己" --no-speak

# 只验证唤醒词，说“小达”后程序退出
$VOICE_PY -m voice_assistant wake-test

# 跳过唤醒，只录一轮并播报，便于验证 ASR/VAD/TTS
$VOICE_PY -m voice_assistant run --without-wake --one-turn

# 完整常驻模式
$VOICE_PY -m voice_assistant run

# 网页专用模式：不启动唤醒词麦克风监听
$VOICE_PY -m voice_assistant run --web-only
```

完整模式下说“小达”，听到双音提示后开始提问。回答后可直接继续追问；15 秒不说话会
退出本次会话并恢复唤醒监听。说“再见”“不用了”或“结束对话”也会退出会话。

完整模式还会在 `127.0.0.1:8765` 启动只允许本机访问的网页桥接接口。网页控制台的
“语音”页签通过该接口选择中文或 English，触发单轮语音输入或直接提交键盘文字、显示识别/回复文字并回放回复音频；它
不会启动第二个麦克风进程，也不会创建机器人 SDK 客户端。需要临时关闭桥接时可加
`--no-web-api`。

## 配置

默认直接读取 `/home/robot/api.txt`。常用覆盖项：

```text
XIAODA_ROBOT_NAME              默认 小达
XIAODA_REASONING_EFFORT        默认 none；复杂问答可设 low
XIAODA_LLM_MODEL               默认读取 api.txt（当前 gpt-5.5）
XIAODA_PREFER_LOCAL             设为 1 时优先使用局域网 LLM
XIAODA_TTS_VOICE               默认 coral
XIAODA_TTS_SPEED               默认 1.35；接口允许 0.25～4.0
XIAODA_END_SILENCE_SECONDS     句尾自动断句静音，代码默认 0.42 秒；当前服务固定为 0.5 秒
XIAODA_KWS_THRESHOLD           默认 0.38；误唤醒多时调高
XIAODA_KWS_THREADS             默认 1；实测 2 线程会异常占用约 3 个 CPU 核心
XIAODA_MIC_DEVICE              默认 pipewire
XIAODA_SPEAKER_DEVICE          默认 pipewire
XIAODA_FIRST_TURN_TIMEOUT      唤醒后首句等待，默认 12 秒
XIAODA_FOLLOWUP_TIMEOUT        回答后追问等待，默认 15 秒
XIAODA_ROBOT_CONTROL_HOST      运动桥接地址，固定为回环地址，默认 127.0.0.1
XIAODA_ROBOT_CONTROL_PORT      运动桥接端口，默认 8766
```

`keywords_xiaoda.txt` 还给较短的“小达”设置了单独阈值；若实际环境仍有较多误唤醒，
优先删掉该行，只保留“你好小达”和“小达小达”两个更稳的唤醒短语。

PipeWire 当前默认输入已经指向讯飞麦克风，默认输出指向 C-Media 扬声器。通过 PipeWire
访问可以和桌面音频共享设备，也能保留系统已有的音频处理。若在没有桌面会话的纯硬件
环境运行，可分别覆盖成 `hw:CARD=XFMDPV0018,DEV=0` 和
`plughw:CARD=Device,DEV=0`；直接硬件模式不能与正在占用设备的 PipeWire 并存。

网页中文录音默认也使用这里的机器人讯飞麦克风，不再因 HTTPS/localhost 而自动改用
操作电脑的浏览器麦克风。保留的 `/api/voice/asr/ws` 仍支持浏览器 PCM partial/final，
但不是当前界面的默认输入源。

`api.txt` 目前权限允许同组用户读取，里面是明文密钥。部署前建议把它收紧为仅 `robot`
用户可读，但本实现不会擅自修改该文件权限。

## 开机启动

确认前台完整模式工作后再安装服务，以免未验收时持续占用麦克风或产生 API 费用：

```bash
mkdir -p /home/robot/.config/systemd/user
install -m 0644 \
  /home/robot/control/voice_assistant/systemd/zerith-xiaoda-voice.service \
  /home/robot/.config/systemd/user/zerith-xiaoda-voice.service
systemctl --user daemon-reload
systemctl --user enable --now zerith-xiaoda-voice.service
journalctl --user -u zerith-xiaoda-voice.service -f
```

若要在没有图形登录时也启动用户服务，可另行执行 `sudo loginctl enable-linger robot`。
unit 会可选读取 `/home/robot/.config/zerith/voice.env`。代理和 API 密钥应写入这个权限
为 `0600` 的机器私有文件，不要直接写入仓库中的 unit；示例见 `config/voice.env.example`。

## 机器人控制接口

语音进程不创建第二个 SDK 客户端。`WebRobotControl` 只访问网页控制进程在
`127.0.0.1:8766` 上的内部接口，最终动作仍由网页进程中的唯一 `H1Robot` 工作线程
串行执行。

使用前必须在网页上依次完成“接管控制”“初始化”，再在“语音”页确认开启“语音运动
控制”。开关默认关闭，并绑定当前页面的短时控制租约；页面心跳停止、释放接管、反初始化
或服务重启都会使授权失效。语音动作不会自行续租。

本地快速指令只允许：前进、后退、左转、右转、转身、挥手、握手和停止，以及对应英文
指令。底盘动作采用 1.5 rad/s 的固定时长脉冲：前进/后退 1 秒，左转 3.5 秒、右转 3 秒，
转身 8 秒；
结束后显式发零速，并仍受 350 ms 底盘 watchdog 保护。后退和转向会根据方向给左右轮
施加相应负号。没有里程计标定，不能把它理解为精确距离或角度。

“挥手”先把双臂七个位置关节同步移动到 0；左臂肩俯仰依次到 -0.3 rad、肘到
-0.9 rad、肩旋转到 -0.4 rad，再采用五次多项式缓入缓出轨迹，以每段 1.2 秒在 -0.4
与 0.4 rad 间往复五次，并在两端各保持 0.5 秒；左臂归零后右臂执行相同动作五次，最后双臂七轴位置
关节归零。
“握手”默认使用右臂：双臂七轴先归零，右肩俯仰依次到 -0.4 rad，右肘到 0.6 rad，
右肩俯仰再到 -0.85 rad，停留 10 秒后平滑回到右臂七轴零位。夹爪保持原位置，两个
动作均不向底盘、腰和头部发送姿态命令。停留阶段持续监控右臂错误标志；任何关节出现
错误时会立即中止动作并停止该臂后台保持。10 秒静态保持会增加肩关节发热风险，只能在
R1 无错误、机械无卡阻且实体急停可达时使用。

包含目的地、距离、角度、速度、连续多动作、其他手臂姿态或不明确指代的请求不会直接
执行。疑似动作但本地无法确定时，大模型只能在上述动作白名单中做分类；问题、假设或
不能安全映射为一个动作时返回拒绝，不把自由文本或模型生成参数发送给 SDK。网页“停止”
和语音“停止”都是软件停止，不能替代实体急停。

## 测试

```bash
cd /home/robot/control
/home/robot/miniconda3/envs/xiaoda-voice/bin/python \
  -m unittest discover -s voice_assistant/tests -v
```
