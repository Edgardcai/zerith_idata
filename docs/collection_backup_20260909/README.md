# 2026-09-09 采集代码备份（最新版待实采验收）

目标仓库 Edgardcai/zerith_idata，沿用 data_collection_new 分支。此快照同步本机正在使用的采集网站、锁定扩展、采集写入优化、可选深度和 8080 回放组件源码。web_control 是当前部署组件的完整源码快照，包含此前已有的回放、推理及页面修改；其他语音项目未同步。

## 流畅度结论和验证范围

此前锁定数据的写入队列占满，采样降至约 24–25 Hz，并产生 133–167 ms 间隔；原回放还把这些间隔压缩为固定 30 Hz，放大动作跳变。修复后实采 episode_000005–000009 共 2256 帧、75.48 秒，约 29.77–29.87 Hz，入队阻塞为 0，没有超过 100 ms 的间隔。仍有 0.622% 的间隔超过 50 ms、最大约 66.7 ms；旧正常组比例为 0.662%。详见 validation 下的复查结果。

这些实采发生在默认关闭深度之前。当前默认 RGB 的最新版截至备份时尚无新录制，也未观察本版本实机回放，不能承诺完全没有顿挫，不能将离线写入速度当作端到端延迟。需操作员用最新版录制并回放后补验收。

已验证：

- 本次采集网站 39 项、锁定扩展 24 项、回放/相机/HTTP 37 项测试通过；两个网页 JS 语法检查通过。测试使用模拟设备，不启动真实运动。
- 最新精确采集二进制此前已通过厂商 writer 离线等价测试，两种模式各持续入队 200 帧、30 Hz，无超过 50 ms 的入队阻塞。RGB 每 10 帧写入 p95 91.1 ms；开启深度为 196.1 ms。
- 最新精确采集二进制此前完成三路真实相机 RGB → RGB+深度 → RGB 切换，各模式画面有效，最后保持 RGB。
- 阶段、目录分组浏览器验证见 validation/directory_groups_20260909；阶段进度为模拟状态测试，未声称已做手柄实采验收。

## 当前行为

- 8090 默认不记录深度；勾选“记录深度图”后仅该会话采集深度，会话结束恢复 RGB。8080 预览保持 RGB。
- 新会话默认两个阶段；长按 Y 超过 2 秒开始，松开后短按 Y 进入第二阶段，再短按 Y 结束并保存。网站直接显示阶段配置和进度。
- 当前目录默认展开全部已登记 episode；历史目录折叠，展开后加载明细。
- 电量仅显示，网站和切换工具不再设置额外的 20% 门槛；厂商保护仍保留。
- 回放按真实时间戳插值关节，夹爪保持事件时序，重复时间戳保留最后一帧。

## 备份内容及恢复依赖

源码及原路径、SHA-256 见 source_manifest.json。collection_depth_toggle 是当前服务，collection_rgb_only 和 collection_storage_fast 保留为恢复依赖；teleop_zero_lock/tools/archive.py 是构建所需工具。运行二进制摘要、模式测试记录见各组件同名目录。这里的验证 JSON 仅作历史记录，不能代替重新构建后的精确二进制自检。

本仓库不包含原厂大二进制、运行中的 SQLite 索引、采集视频/HDF5、环境目录或密码。原始数据与网站 runtime/collection.sqlite3 留在本机；恢复源码时不可覆盖这些状态。固定升降柱 config.json 为本机当前配置（0.4 m），配置不等同于机器人已初始化或已经到位。

构建脚本目前使用本机绝对路径；恢复时将组件放回 /home/robot 下同名目录，web_control 放在 /home/robot/control/web_control。需保留本机 Python 3.10、H1 SDK、原厂运行环境和服务依赖。厂商输入 server.original 路径为 /home/robot/collection_web/research/replay_jitter_20260909/server.original，SHA-256：2e311a08afd94990972258bcf13beb46ea30fdd7f45b0b630ca747268838f054。原厂 teleop 输入和构建方法见 teleop_zero_lock/README.md。

构建采集组件时，先创建该组件 runtime 目录，使用 zerith Python 执行 tools/build.py；生成 runtime/server_fast_candidate，运行该文件的 --offline-self-test，通过后才可将候选文件发布为 runtime/server_fast。如需完整回退链，先构建 collection_storage_fast，再构建 collection_rgb_only，最后构建 collection_depth_toggle，每个组件分别自检。构建不等于切换运行服务。

当前机器已有 zerith-storage-fast.service，系统 drop-in 的 ExecStart 指向 /home/robot/collection_depth_toggle/tools/boot.py；用户 8080 服务 drop-in 设置 ZERITH_CAMERA_DEPTH=0。模板见 deployment。首次恢复缺失服务时需按系统实际情况安装基础 unit 和 drop-in，执行对应 daemon-reload；已有机器不要盲目覆盖服务或在采集中重启。

本次仅备份源码、检查和测试，没有切换服务、初始化或移动机器人。
