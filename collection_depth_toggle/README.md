# 网站可选深度采集（默认关闭）

已部署并验证三路真实相机关闭 → 开启 → 关闭，最后保持默认 color。

8090 任务表单增加“记录深度图”。默认不勾选；勾选后点击启动采集，该会话采集 color+depth。会话期间不可修改，结束后回到不记录深度。刷新正在进行的会话显示实际选择，历史已结束会话不会把新会话的开关恢复为开启。

## 实现

`record_depth` 使用严格布尔值，缺省为 false，经网站验证、RPC、会话配置、每条 collection_task.json 和 HDF5 模式属性传递。网站等待 task_meta.json 中模式匹配才认定配置已接受。最终文件检查在选择深度时要求三路深度数据完整。

相机启动默认仅开启 color，保留原始深度配置作为备选。MetaTransfer 在录制线程启动前，异步切换三路相机并等待所选流匹配且已产生画面；会话结束、取消或启动失败后恢复 color。切换过程独占，禁止第二会话并发更改模式。真实关节控制不在本模块内。

录制时根据会话选择获取图像，写入相同选择的流；深度沿用已验证的三线程 zlib 等级 3 无损压缩，HDF5 单线程按序写入。关闭时没有深度采集或写入。IR 不记录，原始数据不改写。8080 保持彩色预览，避免额外深度预览的传输、解码负担。

`_probe_depth_mode=true` 为直接 RPC 诊断入口：仅切换相机、确认真实帧并返回结果，然后恢复 color；不创建采集线程、任务目录或录制文件。它也遵循会话互斥。

## 验证

真实厂商 writer 两种模式离线比较 40 帧：RGB 的关节/时间戳/JPEG/MP4 与基线一致，RGB-D 额外深度压缩字节也一致。两种模式分别测试 200 帧、30 Hz 持续入队，无超过 50 ms 阻塞。还验证相机选择、严格布尔值、会话互斥、取消后的默认恢复。网站 37 项测试通过，JS 语法通过。完整性能与精确包摘要见 runtime/offline_benchmark.json、runtime/validated.json。

## 部署及恢复

部署命令为 `sudo python3 /home/robot/collection_depth_toggle/tools/deploy.py`，需要操作员已结束会话并反初始化。脚本只替换 camera/collection server，不重启 Motion_Control、SDKService 或 teleop。系统启动覆盖配置继续位于 `/etc/systemd/system/zerith-storage-fast.service.d/rgb-only.conf`，实际入口指向本目录的 tools/boot.py；8080 彩色预览环境设置保留。

需恢复固定 RGB-only 时：结束会话并反初始化，使用本目录 tools/switch.py restore 恢复原厂，然后执行 `/home/robot/collection_rgb_only/tools/switch.py start`；将系统启动覆盖入口恢复为 `/home/robot/collection_rgb_only/tools/boot.py` 并 daemon-reload。网站开关需同时禁用，避免显示与运行版本不一致。原 RGB-only 和 RGB-D 优化包均保留。

部署与实机相机验证结果记录在 runtime/deployment_status.json 和 runtime/live_camera_modes.json。真实运动与新录制效果仍需由操作员录制后验收。
