# 本地中文语音链路

英文 STT/TTS 保留 `voice_assistant.asr.OpenAICompatibleASR`、
`OpenAICompatibleTTS` 和原有整段播放逻辑。只有会话语言为 `zh`、`zh-CN`、
`zh-Hans` 或其他 `zh-*` 时，主进程才路由到这里的两个回环服务。

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

中文 LLM 首个完整句子（15～80 字切分）
  -> 127.0.0.1:8771 Qwen3-TTS-0.6B CustomVoice / Serena
  -> HTTP chunked PCM16
  -> 单个长驻 aplay 进程边接收边播放
```

当前官方 `qwen-tts` 0.1.1 的 `generate_custom_voice()` 会在一个文本段全部生成后
才返回 waveform；它的 `non_streaming_mode=False` 文档也明确说明只是模拟流式文本输入，
并不提供真正的 token/audio generation streaming。因此这里没有伪装成逐 token 流式：
第一句一完成就并行进入合成，服务在每个 15～80 字片段生成后以 chunked PCM 发送，
后续句子继续排队。播放器和 HTTP 响应可立即取消；正在执行的单次官方模型调用只能在
返回后释放 GPU 锁。

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

systemctl --user enable --now zerith-chinese-asr.service zerith-chinese-tts.service
systemctl --user restart zerith-chinese-asr.service zerith-chinese-tts.service \
  zerith-xiaoda-voice.service zerith-h1-web-control.service
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
可用。Qwen 复核超时或异常会自动使用 Paraformer final。中文 TTS 不可用时主应用返回
明确错误，绝不会用英文声音读中文。

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

本机支持 bf16，两个 Qwen 模型均按 bf16 运行。当前 PyTorch/CUDA 环境没有匹配的
FlashAttention 2 wheel 且未安装 CUDA 编译器，所以 TTS 健康检查明确报告 `sdpa`。
官方 Python 包又不提供真正的音频生成流，因而 `<800 ms` 不能诚实宣称通过；若后续
官方包加入真实 streamer 或环境获得匹配的 FlashAttention 2，再重新做这项验收。

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
