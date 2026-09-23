# 本地中文语音链路

英文 STT/TTS 保留 `voice_assistant.asr.OpenAICompatibleASR`、
`OpenAICompatibleTTS` 和原有整段播放逻辑。中文默认也使用现有云端语音 API，
无需加载本地模型；需要联网，使用 `XIAODA_AUDIO_*` 配置及其默认凭据。
在网页“语音对话”中点击“启动本地语音”后，只有两个服务都通过健康检查，
中文会话才路由到本地 ASR/TTS。加载期间仍使用云端，加载超过 180 秒会请求停止。
点击“停止并释放显存”恢复云端模式。启动按钮只启动已安装的服务，不会启用开机启动。
对话大模型仍走原接口，本地 ASR/TTS 不等于整套对话离线。

## 架构

```text
机器人独立讯飞 XFM-DP 麦克风（网页默认）
  -> PipeWire / 16 kHz mono PCM16 -> Silero VAD（500 ms 句末）
  -> 127.0.0.1:8770 FunASR Paraformer online（按 240 ms 音频块识别）
  -> FSMN-VAD（500 ms 句末）+ CT-Punc + ITN + hotwords.txt
  -> 句末 Qwen3-ASR-0.6B 复核（1.5 s 超时，失败使用 Paraformer）
  -> 原对话、动作白名单与网页事件结构

可选浏览器实时接口（保持兼容）
  -> AudioWorklet 下采样为 16 kHz / mono / PCM16（每包 60 ms）
  -> /api/voice/asr/ws -> Paraformer partial -> Qwen3-ASR final

中文 LLM 流式文本（首段 12～40 字优先按语义边界输出，后续 18～64 字）
  -> 127.0.0.1:8771 Qwen3-TTS-0.6B CustomVoice / Serena
  -> 每 8 个 codec frame 增量解码，HTTP chunked PCM16
  -> 独立合成预取队列 -> 单个长驻 aplay 连续播放
```

当前官方 `qwen-tts` 0.1.1 的 `generate_custom_voice()` 会在一个文本段全部生成后
才返回 waveform；它的 `non_streaming_mode=False` 文档也明确说明只是模拟流式文本输入，
并不提供真正的 token/audio generation streaming。本项目现在通过 `qwen_streaming.py`
适配该固定版本：从 talker forward hook 取得新生成的 codec frame，利用 12 Hz tokenizer
的因果解码器解码累计前缀，只输出尚未发送的采样点，最后补齐尾部。每段保留解码上下文，
内部音频块不做反复淡入淡出；已由主程序分段的请求不在服务端二次拆句。

LLM 文本读取、TTS 合成与设备播放分别执行，文本队列最多 8 段，音频队列最多 48 个
8192-byte 块（约 8 秒）。播放设备的写入阻塞不会阻止下一段开始生成。取消会在下一个
codec step 检查并终止生成；forward hook 在取消、异常和客户端断连后均移除。
模型启动时先预热，再通过健康检查，避免第一轮对话额外承担首次 kernel 初始化开销。

这项适配只对固定的 `qwen-tts==0.1.1` 启用；版本不匹配时回退为整段生成。
`CHINESE_TTS_STREAMING=false` 可关闭适配，`CHINESE_TTS_CHUNK_FRAMES=8` 控制音频块
大小（4～32），`CHINESE_TTS_WARMUP=false` 可关闭启动预热。不修改第三方包和模型权重。
网页音频控件仍是整段回复的回放入口；上述低延迟链路用于机器人本体扬声器。

## 播放优化实测（2026-09-07）

同一组三句中文，使用真实 PipeWire 播放设备并写入等长静音，保留实际播放速度与管道
阻塞；不包含大模型生成文字的等待时间。随机语音生成的音频时长略有不同。

| 指标 | 优化前 | 增量解码 + 独立预取 |
|---|---:|---:|
| 首段文字提交到首音频进入播放器 | 3.615 s | 0.502 s |
| 播放中的额外等待（总耗时 − 首包等待 − 音频时长） | 3.461 s | 0.133 s |

另外一条完整中文句子的 HTTP 首音频为 0.489 s；共生成 6.24 s 音频，Qwen3-ASR
回识别文字与原句一致。取消正在生成的音频流耗时 0.053 s。以上是本机样本结果，
不是所有文本、GPU 负载和网络情况下的延迟保证。

可复测（先通过网页启动本地语音；该命令不会播放实际人声）：

```bash
/home/robot/miniconda3/envs/xiaoda-voice/bin/python -m chinese_speech.benchmark_tts
```

## 安装、下载和启动

```bash
cd /home/robot/control
chmod +x chinese_speech/install.sh
./chinese_speech/install.sh

# 可选：目标机器在国内时可用 ModelScope 提前下载（本机采用这一方式）
HTTP_PROXY= HTTPS_PROXY= ALL_PROXY= \
  /home/robot/miniconda3/envs/xiaoda-asr/bin/modelscope download \
  --model Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice \
  --cache_dir /home/robot/.cache/modelscope
HTTP_PROXY= HTTPS_PROXY= ALL_PROXY= \
  /home/robot/miniconda3/envs/xiaoda-asr/bin/modelscope download \
  --model Qwen/Qwen3-ASR-0.6B \
  --cache_dir /home/robot/.cache/modelscope

# 或从 Hugging Face 提前下载，而不是第一次启动时自动下载
HF_HOME=/home/robot/.cache/huggingface \
  /home/robot/miniconda3/envs/xiaoda-tts/bin/hf download \
  Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice
HF_HOME=/home/robot/.cache/huggingface \
  /home/robot/miniconda3/envs/xiaoda-asr/bin/hf download \
  Qwen/Qwen3-ASR-0.6B

# 更新语音主服务 unit，去掉它对两个 GPU 服务的 Wants/After 依赖。
# 已有自定义配置时只修改依赖，保留原 Environment 等配置。
systemctl --user daemon-reload
systemctl --user disable --now zerith-chinese-asr.service zerith-chinese-tts.service
systemctl --user restart zerith-xiaoda-voice.service zerith-h1-web-control.service
# 按需在网页启动；命令行等效操作（不设置开机启动）：
# systemctl --user start zerith-chinese-asr.service zerith-chinese-tts.service
```

Paraformer-online、FSMN-VAD 和 CT-Punc 由 FunASR 首次启动时下载到
`/home/robot/.cache/modelscope`。两个服务都只监听回环地址：ASR 8770、TTS 8771。

当前 ModelScope 版本会把模型目录名中的 `.` 编码为 `___`；本机 unit 已指向实际目录，
不要手工把目录改回带小数点的名字。

网页“录入一句”现在无论通过 HTTP、HTTPS 还是 localhost 访问，都默认打开机器人上的
独立讯飞 XFM-DP 麦克风。这样识别质量不再取决于操作电脑的浏览器麦克风。系统当前
PipeWire 默认输入已固定为 `XFM-DP-V0.0.18`，service 显式使用 `pipewire`；浏览器
WebSocket 实时 partial 接口仍保留，供调试或后续增加输入源切换时使用。

如需调试保留的浏览器麦克风接口，可建立 SSH 本地端口转发，然后访问
`http://localhost:8080`（localhost 属于安全上下文）：

```bash
ssh -N -L 8080:172.16.18.43:8080 robot@172.16.18.43
```

健康检查和日志：

```bash
curl -s http://127.0.0.1:8770/health | python -m json.tool
curl -s http://127.0.0.1:8771/health | python -m json.tool
journalctl --user -u zerith-chinese-asr -u zerith-chinese-tts -f
```

## 配置和降级

主应用支持以下配置，英文的 `XIAODA_ASR_*` / `XIAODA_TTS_*` 不受影响：

```text
CHINESE_SPEECH_ENABLED=true
CHINESE_ASR_STREAM_MODEL=paraformer-online
CHINESE_ASR_FINAL_MODEL=Qwen/Qwen3-ASR-0.6B
CHINESE_ASR_FINAL_TIMEOUT_MS=1500
CHINESE_ASR_ENDPOINT_MS=500
CHINESE_TTS_MODEL=Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice
CHINESE_TTS_SPEAKER=Serena
CHINESE_TTS_DEVICE=cuda
CHINESE_TTS_DTYPE=bfloat16
```

`CHINESE_TTS_SPEAKER` 也可设为 `Vivian`、`Uncle_Fu` 等 CustomVoice 内置声音。
GPU 不足时给 ASR 服务增加 `CHINESE_ASR_FINAL_ENABLED=false`，Paraformer 实时识别仍
可用。Qwen 复核超时或异常会自动使用 Paraformer final。本地服务未就绪时，中文
识别和合成使用现有云端 API（中文识别参数为 `zh`）；本地识别请求失败也会重试云端。
已经开始的本地 TTS 音频流若中途失败会报错，避免重复播报；下一次请求根据健康状态选路。

热词逐行编辑 `hotwords.txt`；常见同音误识别可编辑 `corrections.json`。服务重启后加载。

## 本机实测（2026-08-31）

机器为 RTX 4090 Laptop 16 GB。STT 用同一条本地合成中文指令重复 10 次，并以 20 ms
能量帧确定真正开口和停止说话时刻；TTS 在热机后用 10 条 15～20 字中文句子测试：

| 项目 | P50 | P95 | 结果 |
|---|---:|---:|---|
| 首次 partial（从有效语音开始） | 422.9 ms | 427.5 ms | 通过 `<600 ms` |
| final（从最后有效语音帧开始） | 688.7 ms | 704.7 ms | 通过 `<1.5 s` |
| 中文 TTS 首音频 | 2692.5 ms | 3052.8 ms | 未达到 `<800 ms` |
| 停止本地播放 | 1.14 ms | 7.34 ms | 通过 `<200 ms` |

10/10 次 final 均由 Qwen3-ASR 返回并精确识别测试句。ASR PyTorch 实际分配约
3.45 GiB，TTS 约 2.07 GiB；`nvidia-smi` 显示两个进程分别约 4052/2522 MiB，整卡
当时共使用 6707/16376 MiB。热重启加载 ASR 约 46.8 秒，TTS 约 1.21 秒。

上述为旧版整段生成链路的历史结果，当时未达到 `<800 ms`。本机使用 bf16 / SDPA，
未安装 FlashAttention 2。2026-09-07 增加的 codec 增量解码适配及样本延迟见前面的
“播放优化实测”；它不是官方 `generate_custom_voice()` 自带的流式接口。

英文真实回归仍使用原 `gpt-4o-mini-tts / coral` 和
`gpt-4o-transcribe-diarize`：测试英文句合成 2839.7 ms、回识别 3055.9 ms，内容正确。

## 测试

```bash
cd /home/robot/control
/home/robot/miniconda3/envs/xiaoda-voice/bin/python -m unittest discover -s voice_assistant/tests -v
/home/robot/miniconda3/envs/zerith/bin/python -m unittest discover -s chinese_speech/tests -v
PYTHONPATH=/home/robot /home/robot/miniconda3/envs/zerith/bin/python \
  -m unittest discover -s web_control/tests -v
```
