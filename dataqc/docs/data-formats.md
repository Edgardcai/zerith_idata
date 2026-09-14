# 输入与输出格式

## 真机

默认数据根目录 `/data/zerith_data`，选择一组数据后递归发现 episode。
每条记录包含 HDF5、三路相机视频和采集元信息。常见主文件为 `episode.hdf5`，
核心字段包括 `observation/state`、`action`、`timestamp/t`、`subtask_transitions`。
采集等级读取 `review.json` 等原始记录，并保存独立基线。

## 仿真

默认数据根目录 `/data/sim_data`。兼容形如：

```text
<数据集>/demo_0/
  states/...
  meta/episode_meta.json
  ...相机数据...
```

由 `dataqc/simulation.py` 适配原始结构。每条记录的目标升降高度从自己的
`meta/episode_meta.json` 读取，随任务变化。合成时间轴及无独立夹爪反馈时的语义会在报告中标明；
不能把复制的指令当成真实反馈来证明物理动作成功。

## 质检与复核

先完成全组数值检查，再批量提交 Terra 动作指标复核，默认每批 10 条、2 批并发。
腰头 State 与 Action 的均值及第 1/99 百分位零位容差均为 ±0.02 rad。
时间间隔、腰头等预警保留原有分级规则。图像类别识别默认关闭，开启后每手要求
YOLO 三个时刻至少 2/3 匹配，并由 Terra 图像复核通过。

仅靠数值与轨迹不能确认掉落、碰撞、真实抓取成功等缺少观测信号的物理结果；报告保留
不可观测或待复核状态。[人工等级优先级](grade-priority.md) 独立于原始报告与转换完整性检查。

报告提供全部、等级变化、待复核筛选；回放显示采集等级、自动等级、采用等级及人工保存。
保存人工结果不覆盖原始采集和自动质检结论。

真机和仿真共用嵌入式 HDF5 回放界面，显示三路视频、23 维曲线、阶段与等级复核栏。
自动扫描下方的“自定义目录输入”可直接载入服务端数据集目录。
仿真阶段按原始标注展示，原地删帧和阶段订正仍限真机，仿真截取使用派生副本。

数据概览显示左右夹爪各自“闭合 1 次 / 非 1 次 / 无数据”的占比与每条数据的次数。
次数直接读取数值质检的 Action 闭合事件：连续两帧确认，初始已闭合不计作新闭合；
仿真复制的 State 不作为独立反馈。该概览不修改现有分级规则。

## LeRobot

输出包含 `meta/`、`data/`、`videos/`，保留来源 episode、等级、任务、帧映射与左右手阶段。
转换发布前执行完整复检；左右手阶段切分分别写入 `lefthand`、`righthand`，双手源保留在 `twohands`。
LeRobot 0.3.3 的读取兼容实现随源码固定在 `vendor/` 中。

零次方比较模块的 Prompt 格式检测使用以下三种模板，包含句末英文句号：

- 双手：`Grasp XXX with the left hand and then grasp XXX with the right hand.`
- 左手：`Grasp XXX with the left hand.`
- 右手：`Grasp XXX with the right hand.`

物体名称按 `legacy/scripts/embodied_data_pipeline-main/lerobot_cross_platform.py` 的
`KNOWN_ITEMS` 商品列表精确匹配；不会自动把待检查数据中的名称加入列表。
格式、目录手别或商品名称不符合要求时，报告定位到对应数据集并显示原因。
