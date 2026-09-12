# 具身数据质检与标注流水线

这个仓库只保留数据处理、质检、视觉/VLM 标注和回放相关代码。默认本地数据放在仓库内 `data/` 目录，包括原始 MCAP、生成的 HDF5 episode、LeRobot 数据集和质检报告；网页控制台也支持直接输入 `/opt/shared_assets/...` 这类仓库外 MCAP 路径，输出会默认生成到 MCAP 路径的同级数据目录下。

下方命令默认从仓库根目录运行。先设置数据根目录：

```bash
DATA_ROOT=data
```

然后在同一个 shell 中执行 ALOHA 或 G2 其中一个配置块。后续命令都直接沿用这些变量，不再重复设置默认值。

```bash
# ALOHA
DATA_ROOT=data
DATASET_NAME=aloha
ROBOT_TYPE=aloha
PROFILE_HOST=robot_profiles/aloha.yaml
PROFILE_CONTAINER=/workspace/robot_profiles/aloha.yaml
TASK_TEXT="grasp the bottle"
INPUT_REL="raw_mcap/$DATASET_NAME"
OUTPUT_REL="hdf5_episodes/$DATASET_NAME"
LEROBOT_REPO_ID="$DATASET_NAME"
```

```bash
# G2：示例为单个 MCAP 文件；也可以把多个 MCAP 放到 data/raw_mcap/g2/ 后把 INPUT_REL 改成 raw_mcap/g2
DATA_ROOT=data
DATASET_NAME=g2
ROBOT_TYPE=g2
PROFILE_HOST=robot_profiles/g2.yaml
PROFILE_CONTAINER=/workspace/robot_profiles/g2.yaml
TASK_TEXT="grasp the bottle"
INPUT_REL="raw_mcap/Coca_Cola"
OUTPUT_REL="hdf5_episodes/$DATASET_NAME"
LEROBOT_REPO_ID="$DATASET_NAME"
```

`ROBOT_TYPE=aloha` 会使用 `mcap_conversion/topic_configs/aloha_data_params.yaml`；`ROBOT_TYPE=g2` 会使用
`mcap_conversion/topic_configs/g2_data_params.yaml`。
网页端的 `LeRobot repo id` 留空时会自动使用 `数据集名`。

任务文本优先级为：

```text
网页/命令行传入的任务文本（TASK_TEXT 或 --text）
> MCAP 同名 *_info.json 中 mark.full-instructions-en
> robot profile 中的 tasks.default
```

如果 MCAP 旁存在同名 `_info.json`，转换时还会读取 `mark.segment-instructions`，把其中的 `start_time/end_time` 按 HDF5 `main_timestamp` 映射成帧号，写入 `meta/episode_meta.json` 的 `segment_instructions` 和 `subtask_segments`。原始秒级时间保存在 `raw_start_time/raw_end_time`。

`data/` 已加入 `.gitignore`，不会被提交到 Git。

## 目录结构

```text
Embodied_data_pipeline/
├── mcap_conversion/        # MCAP 转 HDF5 episode、缺帧修复、网页回放
│   ├── scripts/            # 转换、修复、回放脚本
│   └── topic_configs/      # ROS2 topic 到 ALOHA/G2 数据字段的映射
├── lerobot_conversion/     # HDF5 episode 转 LeRobot v2 数据集
├── quality_pipeline/       # HDF5 episode 读取、质检规则、视觉/VLM 标注逻辑
│   └── annotations/vision/ # YOLO 检测和 VLM 场景描述/左右手目标判断
├── robot_profiles/         # 机器人 profile 和质检阈值
├── scripts/                # 质检入口脚本
├── models/                 # 本地视觉模型权重
└── data/                   # 本地数据目录，不提交 Git
    ├── raw_mcap/           # 原始 MCAP 数据
    ├── hdf5_episodes/      # MCAP 转出的 HDF5 episode
    ├── qc_reports/         # 质检报告，按 数据集名_时间后缀 保存
    └── lerobot/            # LeRobot v2 输出数据集
```

`models/` 中的视觉模型权重会随仓库提交，profile 中配置的相对路径可以直接使用。

主要入口：

```text
scripts/run_quality_pipeline.py
scripts/run_vision_vlm_annotations.py
scripts/convert_mcap_dataset.py
scripts/pipeline_web_app.py
mcap_conversion/scripts/mcap_to_icra_episode.py
mcap_conversion/scripts/repair_need_repair_episodes.py
mcap_conversion/scripts/replay_icra_episode.py
lerobot_conversion/scripts/convert_hdf5_to_lerobot_v2.py
```

## 0. 网页端流水线控制台

网页端覆盖 MCAP 转 HDF5、批量质检、选择 episode 修复、选择 episode 删除、HDF5 可视化回放、LeRobot 可视化回放、LeRobot 连续编号计划和 LeRobot 生成。视觉/VLM 标注仍然按第 3 节单独运行。

启动网页：

```bash
python scripts/pipeline_web_app.py --host 0.0.0.0 --port 8890
```

浏览器打开：

```text
http://127.0.0.1:8890/
```

其他机器访问时使用服务器 IP，例如：

```text
http://<服务器IP>:8890/
```

如果外部机器无法访问，先确认脚本使用 `--host 0.0.0.0` 启动，且服务器防火墙没有拦截 8890 端口。

### 独立三相机服务与可选三/四相机服务

从 `/home/caizj/agilex_idata` 启动时，通用三相机兼容入口保留原有数据格式，默认端口调整为 `8012`：

```bash
bash scripts/collection/collect_mobile_pipeline_qc_web.sh
```

开机默认启动可选三/四相机入口，默认端口为 `8001`：

```bash
bash scripts/collection/collect_mobile_pipeline_qc_web_four_camera.sh
```

页面左侧的“保存摄像头数量”可选择三路或四路；三路模式还可选择普通头部 `/camera_f` 或广角头部 `/camera_h`。也可以用启动参数设置页面初值：

```bash
# 默认：四路，普通头部 + 广角全局视角
bash scripts/collection/collect_mobile_pipeline_qc_web_four_camera.sh

# 三路，使用普通头部
bash scripts/collection/collect_mobile_pipeline_qc_web_four_camera.sh --camera-count 3 --head-camera front

# 三路，使用广角作为头部
bash scripts/collection/collect_mobile_pipeline_qc_web_four_camera.sh --camera-count 3 --head-camera global
```

四路模式映射如下：

| 原始相机名 | 输出视频 | LeRobot key |
|---|---|---|
| `front`（`/camera_f/color/image_raw`） | `head_color.mp4` | `observation.images.hand_head_color` |
| `left` | `hand_left_color.mp4` | `observation.images.hand_left_color` |
| `right` | `hand_right_color.mp4` | `observation.images.hand_right_color` |
| `head`（`/camera_h/color/image_raw`） | `head.mp4` | `observation.images.global_color` |

三路模式始终输出 `observation.images.hand_left_color`、`observation.images.hand_right_color` 和 `observation.images.hand_head_color`。选择 `front` 时头部视频为 `head_color.mp4`，选择 `global` 时头部视频为 `head.mp4`；未选中的头部相机不参与该模式的保存和导出。

第四路相机的 `/camera_h/color/camera_info` 是不可信的免驱动数据，因此四相机配置既不读取它，也不创建 `camera/colorIntrinsic/head` 或 `camera/colorExtrinsic/head`。四相机模式要求四个视频都存在，并由同一时间轴同步；任一路缺失或缺帧比例超过 profile 阈值都会质检失败。

服务按模式把派生结果写入 `<data_root>/four_camera/`、`<data_root>/three_camera_front/` 或 `<data_root>/three_camera_global/` 下对应的 `hdf5_episodes`、`qc_reports` 和 `lerobot` 目录。页面中显式填写输出路径时仍以填写值为准。若 `8001` 已被占用，可以通过 `WEB_PORT=18012` 或 `--port 18012` 临时改用其他端口。

页面左侧参数：

```text
MCAP 数据集路径（H200）  必填；可以是单个 .mcap、包含多个 .mcap 的目录，或包含多个 episode 目录的目录
数据集名                 留空时从 MCAP 路径自动推导
HDF5 路径                留空时生成到 <data_root>/hdf5_episodes/<数据集名>
质检报告根目录           留空时使用 <data_root>/qc_reports，报告会自动带 数据集名_时间 后缀
LeRobot 输出根目录       留空时使用 <data_root>/lerobot
机器人                   aloha、zerith 或 g2；选择后自动填入 robot_profiles/<机器人>.yaml
并行转换数               同时转换的 MCAP 数量，建议先用 2
任务文本                 非空时写入 HDF5 task；ALOHA 后续转换严格沿用该 HDF5 值
Profile                  默认随机器人自动切换，也可以手动指定
LeRobot repo id          输出到 <LeRobot 输出根目录>/<repo id>
GPU 编号                 留空使用 CPU；填写 0/1/2... 时启用对应物理 GPU
```

常用按钮：

```text
转换并质检          先 MCAP -> HDF5，再生成批量质检报告
仅批量质检          只对现有 HDF5 episode 重新质检
选择episode剔除静止帧 对勾选的 HDF5 episode 剔除超长静止段；零次方可选 20/40/60 帧档位，然后重新质检
选择episode删除     只删除勾选的 HDF5 episode，不删除 raw MCAP
打开/刷新 HDF5 回放 启动 replay_icra_episode.py，并在页面中显示视频、曲线、字段表、task/subtask 和质检状态
打开/刷新 LeRobot 回放 读取已生成的 LeRobot parquet/video，并显示 state/action 曲线、task/subtask 和源 HDF5 路径
生成编号计划        按 HDF5 episode 目录名排序，写出 renumber_plan.json
生成 LeRobot        按排序后的 episode 连续生成 LeRobot，并写出 meta/episode_name_mapping.json
按左右手阶段切分 LeRobot 仅零次方可用；阶段 1 写入独立 left_hand 根目录，阶段 2 写入独立 righthand 根目录
```

页面右侧有 4 个视图：

```text
质检报告             显示最新 batch_summary.json/Markdown 的状态，并支持勾选 episode
编号映射             显示 renumber_plan.json 或 LeRobot meta/episode_name_mapping.json
HDF5 可视化回放      回放 HDF5 episode 的视频、state/action 曲线和字段值
LeRobot 可视化回放   回放 LeRobot 数据集中的 parquet 和视频
```

路径默认规则：

```text
MCAP 在 <项目>/data/raw_mcap/<数据集名> 下：
  data_root = <项目>/data
  HDF5      = <项目>/data/hdf5_episodes/<数据集名>
  QC        = <项目>/data/qc_reports
  LeRobot   = <项目>/data/lerobot

MCAP 在 /opt/shared_assets/assets/g2_test 下：
  data_root = /opt/shared_assets/assets
  HDF5      = /opt/shared_assets/assets/hdf5_episodes/g2_test
  QC        = /opt/shared_assets/assets/qc_reports
  LeRobot   = /opt/shared_assets/assets/lerobot
```

网页端默认使用 `data-tools-ros2:jazzy` Docker 镜像做 MCAP 转换；如果宿主机已经具备 ROS2/MCAP 依赖，可以取消页面里的 `Docker 转换`。

GPU 说明：

```text
GPU 编号留空           MCAP 转换和 LeRobot 预处理都走 CPU
填写 GPU 编号          MCAP 视频转码会尝试 ffmpeg/NVENC；LeRobot 会用 PyTorch CUDA 做 resize
LeRobot CUDA resize    只有填写 GPU 编号且勾选该项时才添加 --preprocess-device cuda
CUDA_VISIBLE_DEVICES   网页会限制子进程可见 GPU；转换脚本会自动把物理 GPU 编号映射为进程内 cuda:0
```

ALOHA 升降/底盘维度说明：

```text
勾选 “ALOHA 包含升降/底盘 state-action（21维/18维）”：
  LeRobot state  = 14 维双臂关节 + 1 维升降高度 + 6 维底盘状态 [x, y, yaw, vx, vy, wz]
  LeRobot action = 14 维双臂关节 + 1 维升降高度 + 3 维底盘速度 [vx, vy, wz]

取消勾选：
  LeRobot state/action 都是原始 14 维双臂关节
```

ALOHA HDF5 额外字段说明：

```text
state/waist/position       1 维升降高度，来自 /LiftMotorStatePub 的 back_height
action/waist/position      1 维升降高度，同上
state/robot/base_state     6 维 [x, y, yaw, vx, vy, wz]
state/robot/pose2d         3 维 [x, y, yaw]
state/robot/velocity       3 维 [vx, vy, wz]
action/robot/velocity      3 维底盘动作速度 [vx, vy, wz]，来自 /action/chassis
```

其中 `x, y, yaw` 优先从 `/localization/pose` 读取，也兼容 `/localization/pos`；这类定位话题通常低频，转换时会按最终 HDF5 帧时间做线性插值，`yaw` 会先展开再插值。`vx, vy, wz` 从 `/odom` 的 `twist.twist.linear.x/y` 和 `twist.twist.angular.z` 读取。

## 1. 批量 MCAP 转 HDF5 Episode

建议在 ROS2 镜像里运行。下面命令假设：

- 当前目录是本仓库根目录
- `INPUT_REL` 指向单个 `.mcap` 文件，或指向包含多个 `.mcap`/episode 目录的目录
- HDF5 episode 输出到 `$DATA_ROOT/$OUTPUT_REL`

```bash
HOST_DATA_ROOT="$(pwd)/$DATA_ROOT"

cat > .tmp_convert_mcap.sh <<'BASH'
set -e

INPUT_ROOT="$DATA_ROOT/$INPUT_REL"
OUTPUT_ROOT="$DATA_ROOT/$OUTPUT_REL"
CONVERT_JOBS="${CONVERT_JOBS:-2}"
case "$CONVERT_JOBS" in
  ""|*[!0-9]*) CONVERT_JOBS=2 ;;
esac
if [ "$CONVERT_JOBS" -lt 1 ]; then
  CONVERT_JOBS=1
fi

mkdir -p "$OUTPUT_ROOT"

if [ -f "$INPUT_ROOT" ]; then
  MCAP_INPUTS=("$INPUT_ROOT")
else
  shopt -s nullglob
  MCAP_INPUTS=("$INPUT_ROOT"/*)
fi

if [ "${#MCAP_INPUTS[@]}" -eq 0 ]; then
  echo "No MCAP inputs found: $INPUT_ROOT"
  exit 1
fi

convert_one() {
  local mcap_path="$1"
  [ -e "$mcap_path" ] || return 0
  if [ -f "$mcap_path" ] && [ "${mcap_path##*.}" != "mcap" ]; then
    echo "==== skip non-mcap file $(basename "$mcap_path") ===="
    return 0
  fi
  if [ -d "$mcap_path" ] && ! find "$mcap_path" -type f -name "*.mcap" -print -quit | grep -q .; then
    echo "==== skip directory without mcap $(basename "$mcap_path") ===="
    return 0
  fi

  local name
  name=$(basename "$mcap_path")
  name="${name%.mcap}"
  local out_dir="$OUTPUT_ROOT/$name"
  local episode_id="${name//[!0-9]/}"
  episode_id="${episode_id:-0}"

  local text_args=()
  if [ -n "$TASK_TEXT" ]; then
    text_args=(--text "$TASK_TEXT")
  fi

  echo "==== convert $name ===="
  python3 mcap_to_icra_episode.py \
    --mcapPath "$mcap_path" \
    --output "$out_dir" \
    --episodeName "$name" \
    --episodeId "$episode_id" \
    --type "$ROBOT_TYPE" \
    --profile "$PROFILE_CONTAINER" \
    "${text_args[@]}" \
    --overwrite \
    --cleanupIntermediate
}

fail=0
running=0

for mcap_path in "${MCAP_INPUTS[@]}"; do
  [ -e "$mcap_path" ] || continue

  convert_one "$mcap_path" &
  running=$((running + 1))

  if [ "$running" -ge "$CONVERT_JOBS" ]; then
    if ! wait -n; then
      fail=1
    fi
    running=$((running - 1))
  fi
done

while [ "$running" -gt 0 ]; do
  if ! wait -n; then
    fail=1
  fi
  running=$((running - 1))
done

exit "$fail"
BASH

docker run --rm -it \
  --user "$(id -u):$(id -g)" \
  -e HOME=/tmp \
  -e DATA_ROOT=/workspace/data \
  -e DATASET_NAME="$DATASET_NAME" \
  -e ROBOT_TYPE="$ROBOT_TYPE" \
  -e PROFILE_CONTAINER="$PROFILE_CONTAINER" \
  -e TASK_TEXT="$TASK_TEXT" \
  -e INPUT_REL="$INPUT_REL" \
  -e OUTPUT_REL="$OUTPUT_REL" \
  -e CONVERT_JOBS="${CONVERT_JOBS:-2}" \
  -e OPENCV_NUM_THREADS=1 \
  -e OMP_NUM_THREADS=1 \
  -e OPENBLAS_NUM_THREADS=1 \
  -e MKL_NUM_THREADS=1 \
  -w /workspace/mcap_conversion/scripts \
  -v /etc/passwd:/etc/passwd:ro \
  -v /etc/group:/etc/group:ro \
  -v "$(pwd)/mcap_conversion:/workspace/mcap_conversion" \
  -v "$(pwd)/robot_profiles:/workspace/robot_profiles:ro" \
  -v "$HOST_DATA_ROOT:/workspace/data" \
  -v "$(pwd)/.tmp_convert_mcap.sh:/tmp/convert_mcap.sh:ro" \
  data-tools-ros2:jazzy \
  bash /tmp/convert_mcap.sh

rm -f .tmp_convert_mcap.sh
```

注意：`--episodeId` 必须是整数。上面的命令会把 `episode5` 自动转换成 `5`。
`CONVERT_JOBS` 是同时转换的 MCAP 数量，建议先设为 `2`，磁盘 IO 和内存还有余量时再调到 `4`。

如果输入 MCAP 同级存在 `xxx_info.json`，例如：

```text
episode1_0.mcap
episode1_0_info.json
```

转换会自动读取其中：

```text
mark.full-instructions-en
mark.full-instructions-zh
mark.segment-instructions
```

并写入：

```text
<episode>/meta/episode_meta.json
```

其中 `segment_instructions.start_time/end_time` 会转换为 HDF5 帧号；原始秒级时间保存在 `raw_start_time/raw_end_time`。HDF5 和 LeRobot 回放会根据当前帧显示对应的 `task/subtask`。

ALOHA 数据转 HDF5 时还会保存可用的升降和底盘信息：

```text
/LiftMotorStatePub.back_height -> state/waist/position 和 action/waist/position
/localization/pose 或 /localization/pos -> state/robot/base_state 的 x, y, yaw
/odom.twist.twist -> state/robot/base_state 的 vx, vy, wz
/action/chassis -> action/robot/velocity
```

`/localization/pose` 或 `/localization/pos` 是低频辅助数据，不参与严格 0.03s 同步门限；转换 HDF5 时会按每帧 `main_timestamp` 插值成逐帧 `x, y, yaw`。

如果在宿主机环境已经具备 ROS2/MCAP 依赖，也可以直接用封装脚本，通过同一套参数切换：

```bash
TEXT_ARGS=()
[ -n "$TASK_TEXT" ] && TEXT_ARGS=(--text "$TASK_TEXT")

python scripts/convert_mcap_dataset.py \
  --dataset-name "$DATASET_NAME" \
  --data-root "$DATA_ROOT" \
  --input-root "$DATA_ROOT/$INPUT_REL" \
  --output-root "$DATA_ROOT/$OUTPUT_REL" \
  --type "$ROBOT_TYPE" \
  --profile "$PROFILE_HOST" \
  "${TEXT_ARGS[@]}" \
  --jobs 2 \
  --overwrite \
  --cleanupIntermediate
```

## 2. 批量质检 HDF5 Episode

在宿主机 Python 环境里运行：

```bash
REPORT_TIME="$(date +%Y%m%d_%H%M%S)"
QC_REPORT_DIR="$DATA_ROOT/qc_reports/${DATASET_NAME}_${REPORT_TIME}"

python scripts/run_quality_pipeline.py \
  --profile "$PROFILE_HOST" \
  --input "$DATA_ROOT/$OUTPUT_REL" \
  --output "$QC_REPORT_DIR" \
  --compact \
  --num-workers 4
```

主要输出：

```text
$QC_REPORT_DIR/batch_summary.md
$QC_REPORT_DIR/batch_summary.json
```

质检报告目录会带时间后缀，例如 `data/qc_reports/grasp_bottle2_20260601_153000`，避免覆盖历史报告。
`--num-workers` 按 episode 并行处理；网页端使用左侧“并行转换/质检数”传入该参数。

时间戳连续性的“最大间隔”只根据每帧 `main_timestamp` 计算：

```text
frame[i+1].main_timestamp - frame[i].main_timestamp
```

它不按 state/action 的具体维度计算，也不分别统计相机、底盘、升降等传感器时间戳。

质检报告的 episode 明细包含 `fps` 列。`fps` 根据 `main_timestamp` 计算；当 `fps < 29` 时，该 episode 质检失败，并且状态标记为 `删除`。

ALOHA 三路、四路相机 profile 还会检查轨迹时长：短于 `4.0s` 提示“轨迹过短”，长于 `10.0s` 提示“轨迹过长”。时长异常作为 warning 写入 `qc_report.json`、Markdown 报告和网页 warning 列，不单独造成硬失败。质检报告页下方会按当前 profile 显示全部质检项目及关键阈值。

网页切换三路、四路配置时优先读取 episode sidecar 元数据，只有帧数或 FPS 缺失时才打开 HDF5；同一状态请求会复用已解析的质检报告，并使用按相机配置隔离的短时缓存。数据变更后缓存会自动或主动失效。

### 零次方（Zerith）23 自由度 columnar HDF5

零次方采集目录使用 `UUID/episode.hdf5`、`UUID/episode_meta.json` 和
`UUID/videos/rs/*.mp4`，选择 `robot_profiles/zerith.yaml`。state/action 都按以下顺序写入
LeRobot：左臂 7、左夹爪、右臂 7、右夹爪、升降、腰 pitch/yaw、头 yaw/pitch、
底盘线速度/角速度，共 23 维。

```bash
python scripts/run_quality_pipeline.py \
  --profile robot_profiles/zerith.yaml \
  --input /srv/data/datasets/public/zerith_data/market_data1 \
  --output /srv/data/datasets/public/zerith_data/qc_reports/market_data1_$(date +%Y%m%d_%H%M%S) \
  --compact --num-workers 6

python lerobot_conversion/scripts/convert_hdf5_to_lerobot_v2.py \
  --profile robot_profiles/zerith.yaml \
  --data-dir /srv/data/datasets/public/zerith_data/market_data1 \
  --output-root /srv/data/datasets/public/zerith_data/lerobot \
  --repo-id market_data1 \
  --num-workers 2 --resume

# 仅预览 UUID -> episode1..N，不修改源数据
python scripts/optimize_hdf5_episode_names.py \
  --input /srv/data/datasets/public/zerith_data/market_data1 --dry-run
```

9988 网页的机器人下拉框可切换“松灵机器人（ALOHA）”和“零次方机器人（23 自由度）”。
零次方数据可直接使用“仅批量质检”、“打开 HDF5 回放”、“选择 episode 剔除静止帧”和
“按等级生成 LeRobot”。“HDF5 优化（重编号并复检）”按 `collection_ts` 将 UUID 目录安全地
改为 `episode1..N`，同步 sidecar/HDF5 当前 ID。该操作为原地重命名，成功后不保留
旧 episode 目录或数据备份；仅在 `hdf5_episode_rename_manifest.json` 和 `source_episode_id`
保留轻量 UUID 映射以便溯源，重复执行不会再次改号。操作期间的临时回滚日志会在
成功后自动删除。

零次方静止帧裁剪会同步处理全部逐帧 action/state、内嵌 RGB/depth、毫秒时间戳、
`subtask_transitions`、`episode_meta.json:step_index` 和 `videos/rs/*.mp4`。修改前会建立同文件系统
备份链接和持久化事务日志；校验失败、网页停止任务或进程中断时自动回滚，硬中断残留会在下次
进入时自动恢复。LeRobot 当前转换三路 RGB，不写入 depth。
零次方质检会独立完整解码这三路外部 MP4，并要求每路解码帧数与 state/action 完全一致；
因此内嵌 HDF5 图像存在但转换所需 MP4 缺失或损坏时也会硬失败。
LeRobot 视频转换会先产生临时 PNG 帧；每路 MP4 可完整解码且帧数与 PNG 一致后默认删除对应 PNG，
并在全部帧目录清空后删除 `images/`，不在最终数据集中保留重复图片。

零次方双阶段真值以 HDF5 `/subtask_transitions=[stage2_start,total_frames]` 为准，并与
`episode_meta.json:step_index` 交叉校验；JSON 中 `[N,N-1]` 的空项只是采集器结束哨兵，
不会显示为第三阶段。HDF5 回放会在曲线上画出唯一的阶段分界线，播放、拖动和逐帧时同步显示
当前阶段。回放中的“应用阶段订正”会原地同步修改 HDF5 与 sidecar，不创建持久备份；非法边界、
陈旧页面提交、路径或帧数不一致都会拒绝，并在部分写入失败时恢复原边界。

零次方上传数据默认放在 `<场景目录>/hdf5/<数据集>`；质检报告写入同级 `qc_reports/`，
双手 LeRobot 写入同级 `lerobot/twohands/`（兼容旧目录 `twohand`），左右手输出分别写入 `lerobot/left_hand/`
与 `lerobot/righthand/`。场景目录的上级路径可以不同。

先通过网页“按等级生成 LeRobot”生成 `twohands/<repo_id>/A|B|C|F`，且所有映射 episode 都通过严格
两阶段预检后，零次方页面才会启用“按左右手阶段切分 LeRobot”。输出为：

```text
<场景目录>/lerobot/twohands/<repo_id>/<等级>   # 完整双手数据
<场景目录>/lerobot/left_hand/<repo_id>/<等级>  # 只含阶段 1
<场景目录>/lerobot/righthand/<repo_id>/<等级>  # 只含阶段 2
```

切分会重建每条 episode 的 Parquet 索引、时间戳、任务、统计、mapping 和对应区间 MP4；两个输出
都完成格式与帧数校验后才替换旧结果。也可以直接调用：

```bash
python lerobot_conversion/scripts/split_lerobot_by_stage.py \
  --hdf5-root /path/to/hdf5_dataset \
  --source-dataset /path/to/lerobot/twohands/<repo_id>/A \
  --left-output /path/to/lerobot/left_hand/<repo_id>/A \
  --right-output /path/to/lerobot/righthand/<repo_id>/A \
  --overwrite
```

9988 的“跨平台 LeRobot 质检”也可选择零次方或松灵本体，仅读取 LeRobot 元数据与
Parquet，不检查视频完整性，不依赖 ROS 或源 HDF5；零次方 Action / State 各按 8 个部位
展示维度与数值范围，其他检查分项显示阈值和统计，不展开每个 episode。
有重合的动作范围差异只预警，提示词差异概述任务步骤、动作顺序和措辞原因。检查规则和采样范围见
[零次方 LeRobot 质检说明](../../docs/zerith_lerobot_qc.md)。

## 3. 批量生成视觉/VLM 标注

如果只需要生成视觉检测、场景描述、左右手目标分配、Subtask 明细等标注信息，使用独立脚本：

```bash
REPORT_TIME="$(date +%Y%m%d_%H%M%S)"
ANNOTATION_DIR="$DATA_ROOT/qc_reports/${DATASET_NAME}_vision_vlm_${REPORT_TIME}"

# 运行前在当前 shell 中设置真实 key。
export AIHUBMIX_API_KEY="<your-api-key>"

python scripts/run_vision_vlm_annotations.py \
  --profile "$PROFILE_HOST" \
  --input "$DATA_ROOT/$OUTPUT_REL" \
  --output "$ANNOTATION_DIR" \
  --vision --vlm \
  --compact
```

主要输出：

```text
$ANNOTATION_DIR/batch_annotations_summary.md
$ANNOTATION_DIR/batch_annotations_summary.json
$ANNOTATION_DIR/<episode>/annotation_report.md
$ANNOTATION_DIR/<episode>/annotations.json
$ANNOTATION_DIR/<episode>/trajectory.json
$ANNOTATION_DIR/<episode>/segments.jsonl
```

如果 `annotation_report.md` 里的 `YOLO 原始检测` 为空，可以先用更低阈值和更多关键帧调试单个 episode：

```bash
python scripts/run_vision_vlm_annotations.py \
  --profile "$PROFILE_HOST" \
  --input "$DATA_ROOT/$OUTPUT_REL/episode1" \
  --output "$DATA_ROOT/qc_reports/${DATASET_NAME}_episode1_yolo_debug_${REPORT_TIME}" \
  --vision --no-vlm \
  --yolo-conf-threshold 0.05 \
  --yolo-key-frames first,middle,last \
  --compact
```

ALOHA 的 VLM 场景描述模板在 `quality_pipeline/annotations/vision/vlm.py` 中，当前会按桌面饮料场景生成类似：

```text
桌子有<N>瓶饮料，饮料<A>放在左边，饮料<B>放在右边。
```

如果希望视觉/VLM 标注和质检报告一起生成，仍然可以使用 `scripts/run_quality_pipeline.py --vision --vlm`；推荐日常质检与视觉/VLM 标注分开运行，便于控制 VLM 调用成本。

## 4. 剔除静止帧或修复需要补帧的数据

网页端的“选择episode剔除静止帧”只处理页面中勾选的 HDF5 episode，不需要原始 MCAP。逻辑是：

- 静止帧优先使用 HDF5 中存在的完整运动字段判定，包括手臂/夹爪、升降、底盘速度、末端位姿等；不考虑底盘位姿 `base_x/base_y/base_yaw`
- 静止条件由 profile 的 `stationary_requires_action_and_state` 决定：松灵默认 action/state 任一静止即可；零次方要求 action 和 state 同时静止，并且底盘速度不超过 `stationary_base_velocity_epsilon`
- 松灵继续读取 profile 的 `max_stationary_action_frames`，默认 15 帧。零次方网页提供 20/40/60 帧三档，默认 60 帧
- 零次方所选档位同时是质检上限、触发剔除阈值和修复后每段保留上限。例如选 40 帧时，只处理连续静止超过 40 帧的段，并均匀保留最多 40 帧
- 所有超过当前档位的静止段都会处理，不只处理最长静止段；删除的帧不能通过后续改成更大档位恢复
- 同步裁剪 `videos/**/*.mp4`
- 将 HDF5 `main_timestamp` 重写为连续 30Hz，避免删除帧后出现新的时间戳大间隔
- 处理完成后网页会自动重新质检

命令行也可以直接运行：

```bash
python scripts/trim_stationary_hdf5_episodes.py \
  --input "$DATA_ROOT/$OUTPUT_REL" \
  --profile "$PROFILE_HOST" \
  --episodeNames "episode0,episode1" \
  --stationary-threshold 40 \
  --keep-stationary-frames 40 \
  --target-fps 30 \
  --num-workers 2 \
  --video-workers 3
```

`batch_summary.md` 中 `fps < 29` 的 episode 会标记为 `删除`，不建议用静止帧剔除来保留。

`batch_summary.md` 中状态为 `修复` 的 episode，可以用下面脚本重新从 MCAP 生成并补帧。
当前修复脚本主要用于 ALOHA 数据；G2 数据如需重新生成，建议回到第 1 节使用 `ROBOT_TYPE=g2`
重新转换对应 `.mcap`。

补帧逻辑：

- 对每个目标时间戳，优先查找原始 MCAP 转出的 joint / end_pose / camera 数据
- 如果该传感器在时间戳附近有原始数据，就使用原始数据
- 如果没有原始数据，才使用左右相邻帧插值
- `--maxGap 0.25` 表示修复后相邻帧最大间隔目标不超过 0.25 秒

```bash
QC_REPORT_NAME=grasp_bottle2_20260601_153000
HOST_DATA_ROOT="$(pwd)/$DATA_ROOT"

docker run --rm -it \
  --user "$(id -u):$(id -g)" \
  -e HOME=/tmp \
  -e DATA_ROOT=/workspace/data \
  -e DATASET_NAME="$DATASET_NAME" \
  -e QC_REPORT_NAME="$QC_REPORT_NAME" \
  -w /workspace/mcap_conversion/scripts \
  -v /etc/passwd:/etc/passwd:ro \
  -v /etc/group:/etc/group:ro \
  -v "$(pwd)/mcap_conversion:/workspace/mcap_conversion" \
  -v "$HOST_DATA_ROOT:/workspace/data" \
  data-tools-ros2:jazzy \
  bash -lc '
set -e

python3 repair_need_repair_episodes.py \
  --qcSummary "$DATA_ROOT/qc_reports/$QC_REPORT_NAME/batch_summary.md" \
  --mcapRoot "$DATA_ROOT/raw_mcap/$DATASET_NAME" \
  --outputRoot "$DATA_ROOT/hdf5_episodes/$DATASET_NAME" \
  --maxGap 0.25 \
  --rawTimeTolerance 0.03 \
  --overwrite \
  --cleanupIntermediate
'
```

其中 `QC_REPORT_NAME` 要替换成上一轮质检实际生成的报告目录名。

修复完成后，需要再跑一遍批量质检，确认补帧后的 HDF5 episode 是否通过：

```bash
REPORT_TIME="$(date +%Y%m%d_%H%M%S)"
RECHECK_REPORT_DIR="$DATA_ROOT/qc_reports/${DATASET_NAME}_recheck_${REPORT_TIME}"

python scripts/run_quality_pipeline.py \
  --profile "$PROFILE_HOST" \
  --input "$DATA_ROOT/$OUTPUT_REL" \
  --output "$RECHECK_REPORT_DIR" \
  --compact \
  --num-workers 4
```

复检结果会写到类似 `data/qc_reports/grasp_bottle2_recheck_20260601_154500` 的目录中。

注意：

```text
修复会直接改写 HDF5 episode。
如果后面再次执行 MCAP -> HDF5 且覆盖同名输出，修复过的 HDF5 会被原始转换结果覆盖，时间戳最大间隔会回到修复前。
网页端读取“最新质检报告”时按 batch_summary.json 的文件修改时间选择，不只看报告目录名中的时间后缀。
```

## 5. 网页回放并查看质检状态

```bash
QC_REPORT_NAME=g2_20260604_170510
QC_REPORT_DIR="$DATA_ROOT/qc_reports/$QC_REPORT_NAME"

python mcap_conversion/scripts/replay_icra_episode.py \
  --episode-dir "$DATA_ROOT/$OUTPUT_REL" \
  --qc-report-dir "$QC_REPORT_DIR" \
  --type "$ROBOT_TYPE" \
  --host 0.0.0.0 \
  --port 8792
```

`QC_REPORT_NAME` 可以填写初检报告目录，也可以填写修复后的复检报告目录。

网页会读取 `batch_summary.md`，显示每条数据的状态：

```text
保留 / 修复 / 删除
```

同时会显示 `error` 备注，方便定位是时间戳连续性、运动稳定性、相机缺帧还是其它检查项不通过。
`--type aloha` 会显示 ALOHA 关节、升降和底盘字段；带升降/底盘时曲线按 21 维 state 和 18 维 action 展示，其中 `waist_height`、`base_vx`、`base_vy`、`base_wz` 的 state/action 会放在同一行对比。`base_x`、`base_y`、`base_yaw` 只有 state，没有对应 action。`--type g2` 显示完整 26 维 state 和 24 维 action。

如果 episode meta 中存在 `task`、`tasks`、`subtask_segments` 或由 `_info.json` 转换得到的 `segment_instructions`，回放界面会显示完整 task，并根据当前 frame 自动切换 subtask。零次方还会自动读取 `/subtask_transitions` 与 `episode_meta.json:step_index`，只显示两个有效采集阶段，并允许原地订正唯一分界点。

## 6. HDF5 Episode 转 LeRobot v2

下面命令参考 G2 的有序转换脚本，支持并行预处理、队列缓存和 LeRobot image writer 参数。
默认会按 `--data-dir` 下的 episode 目录名排序，然后连续写成 `episode_000000`、`episode_000001`。
源 episode 名和转换后的 LeRobot episode 名会记录在 `meta/episode_name_mapping.json`。
再次运行时，脚本默认会根据这个映射文件跳过已经记录过的源 episode，只转换新增数据。
不再生成 `meta/resume_completed_raw_paths.jsonl`。

如果 HDF5 episode 的 `meta/episode_meta.json` 中包含 `_info.json` 转换出的任务和子任务信息，LeRobot 转换会同步写入：

```text
meta/tasks.jsonl
meta/episodes.jsonl
```

LeRobot 回放会从这些 meta 中显示当前 episode/frame 的 task 和 subtask。
零次方转换还会把有效的 `/subtask_transitions` 规范化为 `subtask_segments`，同时写入
`stage_count` 和 `subtask_transitions`，所以常规 HDF5 -> LeRobot 转换不会再丢失采集阶段边界。

### 质量等级一致性

网页质检报告中已经定稿的 `A`、`B`、`C`、`F` 等级是质量等级的唯一权威来源。网页生成 LeRobot 前会先把每条 episode 的最终等级写入对应 HDF5 episode 的 `meta/episode_meta.json`：

```json
{
  "quality_grade": "A",
  "manual_quality_grade": "A",
  "manual_failure": false
}
```

`manual_failure` 仅在等级为 `F` 时为 `true`。转换命令同时通过 `--quality-grade` 显式传入同一等级，并将其写入 LeRobot 的 `meta/episodes.jsonl` 和 `meta/episode_name_mapping.json`。任一 episode 缺少等级、等级不在 `A/B/C/F` 中，或网页、HDF5、LeRobot 三者不一致时，转换任务会直接失败并报告具体 episode，不会静默生成缺少 `quality_grade` 的元数据。

在网页上人工修改并保存质量等级时，对应 HDF5 sidecar 会立即原子更新；只有 HDF5 写入成功，网页保存接口才返回成功。生成 LeRobot 前仍会执行一次全量同步，兼容旧的人工标注并作为兜底校验。

历史 LeRobot 数据可以使用网页流水线中的质量等级同步步骤进行元数据修复。该步骤只原子更新 `meta/episodes.jsonl` 和 `meta/episode_name_mapping.json`，不会重写 `data/` 下的 parquet 或 `videos/` 下的视频；修复后会再次校验网页报告、HDF5 侧车和两份 LeRobot 元数据是否逐条一致。

通用 LeRobot v2 字段名会使用 profile 中的配置：

```text
observation.state
action
observation.images.head_color
observation.images.hand_left_color
observation.images.hand_right_color
```

如果要兼容你给的 G2/pi05 参考脚本字段名，G2 会加上 `--key-style pi05`：

```text
state
actions
head_color
hand_left_color
hand_right_color
```

```bash
KEY_STYLE_ARGS=()
if [ "$ROBOT_TYPE" = "g2" ]; then
  KEY_STYLE_ARGS=(--key-style pi05)
fi

ALOHA_ACTION_ARGS=()
if [ "$ROBOT_TYPE" = "aloha" ]; then
  # joints_base: state=21维，action=18维，包含升降和底盘
  # joints:      state/action 都是 14维原始双臂关节
  ALOHA_ACTION_ARGS=(--aloha-action-mode joints_base)
fi

# 可选：GPU 预处理加速。留空则使用 CPU。
GPU_ARGS=()
if [ -n "${GPU_DEVICE:-}" ]; then
  GPU_ARGS=(--preprocess-device cuda --gpu-device "$GPU_DEVICE" --gpu-resize-batch-size 64)
fi

# 如果显卡支持 NVENC，可再打开下面这一行加速输出 mp4 编码。
# GPU_ARGS+=(--gpu-encode-videos --gpu-video-encoder h264_nvenc)

VIDEO_ARGS=(--cpu-video-encoder libx264 --cpu-video-crf 20 --cpu-video-preset fast)

HF_LEROBOT_HOME="$DATA_ROOT/lerobot" \
OPENCV_NUM_THREADS=1 \
OMP_NUM_THREADS=1 \
OPENBLAS_NUM_THREADS=1 \
MKL_NUM_THREADS=1 \
python lerobot_conversion/scripts/convert_hdf5_to_lerobot_v2.py \
  --profile "$PROFILE_HOST" \
  --repo-id "$LEROBOT_REPO_ID" \
  --data-dir "$DATA_ROOT/$OUTPUT_REL" \
  "${KEY_STYLE_ARGS[@]}" \
  "${ALOHA_ACTION_ARGS[@]}" \
  "${GPU_ARGS[@]}" \
  "${VIDEO_ARGS[@]}" \
  --num-workers 2 \
  --queue-size 8 \
  --image-writer-processes 8 \
  --image-writer-threads 16 \
  --resume
```

常用参数：

```text
--image-size 224 224       输出图像尺寸，默认 224x224
--episode-filter 1,3,9     只转换指定 episode
--episode-filter 1-10      只转换指定范围
--quality-grade A          显式写入该批 episode 的网页最终质检等级，只接受 A/B/C/F
ALOHA 的 LeRobot task 严格读取 HDF5 `aligned_joints.h5` 的 `task` 属性；缺失时转换直接报错，不使用 profile 默认值或命令行覆盖值
--task "..."               仅供非 ALOHA profile 覆盖 LeRobot task
--resume                   根据 meta/episode_name_mapping.json 续跑；已记录 episode 会先校验 parquet/视频帧数，异常则从 HDF5 覆盖重写
--validation-retries 1     LeRobot episode 写入后自动校验；失败时从 HDF5 重写的次数，默认 1
--no-skip-existing         不跳过已记录的源 episode
--overwrite                删除已有 LeRobot 输出并重建；会重新生成全部数据。网页按等级生成固定使用此模式，并在执行前二次确认
--unordered                按预处理完成顺序写入，不强制 episode id 顺序
--preserve-episode-index   尝试保留排序后的连续 episode id
--cpu-video-encoder        CPU 输出视频编码器，默认 libx264，避免使用 LeRobot 默认 AV1 慢编码
--cpu-video-crf            CPU H264 质量参数，默认 20，数值越低质量越高、文件越大
--cpu-video-preset         CPU H264 速度/压缩预设，默认 fast
--gpu-accel                同时打开 CUDA resize 和 NVENC 编码；NVENC 失败会自动回退 CPU H264
--preprocess-device cuda   用 PyTorch CUDA 做视频帧 resize
--gpu-encode-videos        用 ffmpeg NVENC 编码输出 mp4；需要显卡硬件支持 NVENC
--gpu-video-encoder        NVENC 编码器，可选 h264_nvenc/hevc_nvenc/av1_nvenc
--aloha-action-mode joints ALOHA 写 14 维 state 和 14 维 action
--aloha-action-mode joints_base ALOHA 写 21 维 state 和 18 维 action，默认推荐用于带升降/底盘的数据
--dry-run                  只打印计划，不写数据
```

`joints_base` 的 ALOHA LeRobot 布局为：

```text
observation.state = 14 维双臂关节 + 1 维升降高度 + 6 维底盘状态 [x, y, yaw, vx, vy, wz]
action            = 14 维双臂关节 + 1 维升降高度 + 3 维底盘速度 [vx, vy, wz]
```

再次运行 `--resume` 时，会根据 `meta/episode_name_mapping.json` 里的源 HDF5、state/action 维度和 ALOHA 模式判断是否跳过。若同一个 repo 之前按 14/14 生成，现在切到 21/18，脚本会检测到 LeRobot feature schema 不一致并重建输出目录。

如果外层环境设置了 `CUDA_VISIBLE_DEVICES=3`，脚本会自动把物理 GPU 3 映射为进程内 `cuda:0`；因此网页或脚本中仍然可以填写物理 GPU 编号。

## 7. 依赖安装

基础质检依赖：

```bash
python -m pip install -r quality_pipeline/requirements.txt
```

视觉/VLM 依赖：

```bash
python -m pip install -r quality_pipeline/requirements-vision.txt
```

ROS2 MCAP 转换相关依赖建议直接使用 `data-tools-ros2:jazzy` 镜像。
