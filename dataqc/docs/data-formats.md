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

## LeRobot

输出包含 `meta/`、`data/`、`videos/`，保留来源 episode、等级、任务、帧映射与左右手阶段。
转换发布前执行完整复检；左右手阶段切分分别写入 `lefthand`、`righthand`，双手源保留在 `twohands`。
LeRobot 0.3.3 的读取兼容实现随源码固定在 `vendor/` 中。
