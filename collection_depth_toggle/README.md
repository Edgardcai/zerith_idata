# 网站可选深度采集（默认关闭）

机器人 `192.168.10.45` 上，源码位于 `/home/robot/collection_depth_toggle/`，常驻程序 `/home/robot/collection_depth_toggle/runtime/server_fast` 提供 `192.168.10.45:50051`。采样逻辑在 `/home/robot/collection_depth_toggle/timing.py`。

模块在采集系统中的位置见[仓库架构图](../README.md)，采样匹配、拒帧和落盘指标见[在线采样质检](../docs/collection-quality.md#在线采样质检)。

已部署并验证三路真实相机关闭 → 开启 → 关闭，最后保持默认 color。

8090 任务表单增加“记录深度图”。默认不勾选；勾选后点击启动采集，该会话采集 color+depth。会话期间不可修改，结束后回到不记录深度。刷新正在进行的会话显示实际选择，历史已结束会话不会把新会话的开关恢复为开启。

## 实现

`record_depth` 使用严格布尔值，缺省为 false，经网站验证、RPC、会话配置、每条 collection_task.json 和 HDF5 模式属性传递。网站等待 task_meta.json 中模式匹配才认定配置已接受。最终文件检查在选择深度时要求三路深度数据完整。

相机启动默认仅开启 color，保留原始深度配置作为备选。MetaTransfer 在录制线程启动前，异步切换三路相机并等待所选流匹配且已产生画面；会话结束、取消或启动失败后恢复 color。切换过程独占，禁止第二会话并发更改模式。真实关节控制不在本模块内。

录制时根据会话选择获取图像，写入相同选择的流；深度沿用已验证的三线程 zlib 等级 3 无损压缩，HDF5 单线程按序写入。关闭时没有深度采集或写入。IR 不记录，原始数据不改写。8080 保持彩色预览，避免额外深度预览的传输、解码负担。

`_probe_depth_mode=true` 为直接 RPC 诊断入口：仅切换相机、确认真实帧并返回结果，然后恢复 color；不创建采集线程、任务目录或录制文件。它也遵循会话互斥。

## 构建与离线验证

录制扩展以头部 RGB 新帧为参考，按主机接收时间对齐三路相机和状态，并把原始设备时间及对齐证据写入 HDF5。录制过程预处理常用提示音，减少阶段切换时的合成开销。

在包含 `teleop_zero_lock/` 和 `collection_autostart/` 的源码目录执行以下命令。使用 Python 3.10、匹配版本的厂商 `server` 二进制，以及本机一条已完成的 HDF5 录制作为只读测试样本。

```bash
python3.10 collection_depth_toggle/tools/build.py --vendor-server /path/to/factory/server
ZERITH_TEST_EPISODE=/path/to/completed/episode.hdf5 \
  collection_depth_toggle/runtime/server_fast_candidate --offline-self-test
```

自测比较 RGB 和 RGB-D 的 HDF5、MP4、帧时序及缓存行为，结果和候选包摘要写入候选包所在的 `runtime/`。构建和测试期间使用独立目录。

## 部署及恢复

当前直接开机启动由 [`collection_autostart`](../collection_autostart/README.md) 接入 `robotd.service`。运行包和验证摘要由部署主机管理。`tools/switch.py` 用于维护时切换采集服务，并校验候选、在用和回滚版本。维护前需结束采集并反初始化。旧版固定 RGB 回滚链见 [`collection_rgb_only`](../collection_rgb_only/README.md)。
