# 数据质检工作台 · 最终版

统一处理零次方真机／仿真 HDF5 的质检、人工复核、回放和 LeRobot 转换。默认端口 **9990**。

## 功能入口

1. **采集数据质检与转换**：默认首页；真机扫描 `/data/zerith_data`，仿真扫描 `/data/sim_data`。支持 HDF5 质检、静止帧处理、episode 删除、三视角回放、按等级转换和左右手切分。
2. **LeRobot 可视化与人工筛查**：递归选择目录，查看视频、数值和阶段，并记录人工筛查结果。
3. **仿真与真机比较**：跨平台数据分析与对照。
4. **plumo筛查**：整组自动质检、批量动作指标复核与人工确认。

**等级冲突固定采用：人工复核 > VLM／自动质检 > 采集原始记录。**
人工等级跨刷新、重启与复检保留；报告保留各来源等级。详见 [等级优先级](docs/grade-priority.md)。

动作复核使用 **Terra** 分析数值指标与采样轨迹，不发送视频。相同数据与配置复用已有结果。
类别识别默认关闭；开启时才执行 YOLO + Terra 图像类别复核。

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
配置 Terra 模型名称、API 文件及 YOLO 权重路径。开启类别识别前提供实际商品模型文件。
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
