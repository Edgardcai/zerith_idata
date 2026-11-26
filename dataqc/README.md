# 数据质检工作台

统一处理零次方真机／仿真 HDF5 的质检、人工复核、回放和 LeRobot 转换。默认端口 **9990**。

## 功能入口

1. **采集数据质检与转换**：默认首页；真机扫描 `/data/zerith_data`，仿真扫描 `/data/sim_data`。支持 HDF5 质检、静止帧处理、episode 删除、三视角回放、按等级转换和左右手切分。
2. **LeRobot 可视化与人工筛查**：递归选择目录，查看视频、数值和阶段，并记录人工筛查结果。
3. **仿真与真机比较**：跨平台数据分析与对照。
4. **plumo筛查**：整组自动质检、批量动作指标复核与人工确认。
5. **YOLO 实时检测**：访问 `http://192.168.10.45:9990/#yolo`，开启本机相机后点击“开始检测”，查看目标框、类别、置信度和中心坐标；也可上传图片、筛选类别及导出标注画面和 JSON。切换其他分栏时自动停止检测并释放相机连接。

实时检测页面由 `/yolo/` 转发到本机 `yolo-camera-studio.service`（8787），再调用 `http://127.0.0.1:7881` 的 `product-yolo-sku71.service`；模型为 `/data/yolo/sku71_service_20261014/model.pt`（`capreplay_slow_epoch8`，71 类）。无需访问原来的远端推理服务器。机器人头部、左腕、右腕 RGB 相机通过已有相机 SDK 共享读取；“浏览器”来源需要在本机使用 `http://localhost:9990/#yolo` 或通过 HTTPS 访问。

**等级冲突固定采用：人工复核 > VLM／自动质检 > 采集原始记录。**
人工等级跨刷新、重启与复检保留；报告保留各来源等级。详见 [等级优先级](docs/grade-priority.md)。

动作复核默认使用 **GPT-6.1 Sol** 分析数值指标与采样轨迹，不发送视频。相同数据与配置复用已有结果。
类别识别默认关闭；开启时才执行 YOLO + 所选 VLM 的图像类别复核。

## 安装与启动

需要 Linux、Python **3.11**、系统 `ffmpeg` / `ffprobe`。GPU 编码需要可用的 NVIDIA 驱动和支持 `h264_nvenc` 的 FFmpeg；无可用硬件编码时使用 CPU。

以下命令在本 README 所在的项目目录执行。新安装使用独立虚拟环境：

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[test]'
mkdir -p runtime/config runtime/models
cp -n config/settings.example.json runtime/config/settings.json
cp -n config/api.example.txt runtime/config/api.txt
chmod 600 runtime/config/api.txt
```

编辑 `runtime/config/api.txt`，填写自己的模型服务地址与密钥；在 `runtime/config/settings.json`
配置模型名称、API 文件及 YOLO 权重路径，当前默认 `gpt-6.1-sol`。开启类别识别前提供实际商品模型文件。
示例不包含密钥、模型权重或采集数据。已有部署可沿用自己的配置文件和环境。

分别在两个终端启动：

```bash
.venv/bin/python workbench.py --host 0.0.0.0 --port 9990
```

```bash
.venv/bin/python -m dataqc.worker
```

访问 `http://<服务器地址>:9990/`。网页“设置”也可调整模型与质检选项。
用 `DATAQC_HOME=/绝对路径` 可以把所有运行状态放到源码目录以外。

多网卡机器可在 `runtime/config/settings.json` 设置 `api_local_address` 为本机的出口 IP，
让 VLM 请求使用该地址对应的路由；留空时使用系统默认路由。该配置只影响模型请求。

长期运行使用 [systemd 部署说明](docs/deployment.md)。

## 数据流与默认转换参数

```text
真机 /data/zerith_data    仿真 /data/sim_data
                  ↓
        数值质检 → Terra 批量动作复核
                  ↓
       可选类别识别 → 等级报告 / 人工复核
                  ↓
          A/B LeRobot 转换与完整复检
                  ↓
             左手 / 右手阶段切分
```

转换默认 **4 进程、GPU 0、每进程线程 1**；需要重新编码时使用 NVENC（p4 / qp18），
最多 8 个硬件编码会话。合格视频直接复用。质检并发与转换并发是独立设置。

模型 HTTP 请求由同一系统用户的所有质检进程共用单路队列，锁位于
`~/.cache/dataqc/vlm-gateway.lock`；批次并发不再增加实际模型请求并发。
网关返回 429 时保留队列名额，默认按 30、60、120、120、120、120 秒等待并重试，
优先遵循 `Retry-After`（30～3600 秒）。进度显示排队与重试状态；暂停检查每 5 秒一次。
明确的额度不足不自动重试。连续 6 次重试仍失败时标记复核未完成，稍后重新运行质检；
已完成的结果仍可复用。此队列不能约束其他机器或非质检程序的调用。

真机默认输出：

```text
/data/zerith_data/lerobot/twohands/<数据集>/<等级>/
/data/zerith_data/lerobot/lefthand/<数据集>/<等级>/
/data/zerith_data/lerobot/righthand/<数据集>/<等级>/
```

仿真按所选数据根目录生成对应输出。F 不导出；未人工确认的待复核记录不提前导出。
详细输入格式见 [数据格式](docs/data-formats.md)。

## 目录

| 目录 / 文件 | 内容 |
|---|---|
| `workbench.py` | Web 服务入口与模块组合 |
| `dataqc/` | 数值、模型复核、队列、转换、数据适配核心 |
| `integrations/` | 页面布局、等级机制、旧模块接入 |
| `web/` | plumo筛查及共享回放前端 |
| `legacy/` | 实际被调用的兼容实现；保留内部路径以避免破坏回放、比较功能 |
| `vendor/` | 固定 LeRobot 0.3.3 源码及许可证 |
| `config/` | 可公开的配置示例 |
| `deploy/` | 可迁移的服务模板、安装与空闲重启工具 |
| `tests/` | 自动回归测试，使用生成数据和本地模型服务 |
| `docs/` | 当前版本使用、部署、规则与验收说明 |
| `runtime/` | 本地状态、复核等级、VLM 缓存、日志和导出，**不提交 Git** |

`runtime/var/grade-selections/` 保存人工等级；`runtime/var/manual/` 和任务目录保存可复用的模型结果。
清理缓存前应明确用途；升级源码不需要删除这些目录。

## 验证

```bash
.venv/bin/python -m pytest -q
.venv/bin/python deploy/restart_when_idle.py --check-only
```

测试自动隔离运行目录，模型请求使用本地测试服务，不消耗生产 API 额度。
[发布验收](docs/release.md) 记录最终检查结果；[第三方说明](THIRD_PARTY.md) 记录随项目分发的依赖。

商超全流程质检入口：`/market/`，默认汇总 `robot_station/data` 下商超录制，四阶段复核，跨端口统一 LeRobot 输出。详见 `docs/market-qc.md`。
