> 后续更新：当前运行版本已改为 `/home/robot/collection_depth_toggle`，8090 可按会话选择是否记录深度，默认关闭。本目录固定 RGB-only 包保留用于恢复。

# 只采彩色图像（2026-09-09）

状态：操作员结束会话并反初始化后，已切换上线；8080、8090 已重启加载配置。GetRecorderState 已确认三路相机均仅启用 640×480、30 Hz color；8080 的 depth_enabled=false、healthy=true，各相机仅有 RGB 且画面新鲜。采集状态流正常，机器人保持反初始化。没有执行机器人动作或自动新录制。开机覆盖配置已安装，整机重启尚未实测。

## 行为

- 三路 RealSense 配置保留原来的 color 分辨率、帧率和格式，只在内存移除 depth/IR，关闭深度对齐。原始厂商 YAML 不改写；重新初始化相机时仍过滤非 color 请求。
- Real_Env 的采集输入只请求 color，写入线程也只允许 color。保留完整关节 state/action、时间戳、夹爪、JPEG 和 MP4。文件属性标记 `image_streams=color`、`depth_recorded=false`。不会创建空深度数据集或伪造深度帧。
- 8080 相机服务增加可选 `enable_depth`，部署时通过用户服务环境变量 `ZERITH_CAMERA_DEPTH=0` 关闭深度传输、解码、轮询和健康检查要求；网页禁用深度开关。默认行为仍支持 RGB-D。
- 不修改此前采集数据，不调整轨迹控制或人为平滑关节动作。该改动降低相机和存储负担，不保证全部真机顿挫都会消失。

## 验证

精确运行包 `runtime/server_fast` 的 SHA256：`d65bb44f786b09bead78607bcba14244ad160b0b941814974266fe5cac1a877d`。只替换原厂归档入口，其余 1751 个条目字节保持不变。

使用真实厂商 writer、40 帧已保存数据离线对照：原串行 RGB-D 每 10 帧 356–406 ms，RGB-only 99–116 ms。除主动去掉 depth 数据集及新增模式属性外，关节数据、时间戳、JPEG 字节、各数据集元数据一致，三路 MP4 解码图像一致。连续 200 帧按 30 Hz 入队，未发生超过 50 ms 的入队阻塞，批次总耗时 p95 112.44 ms。测试中的真实环境、ZCM 和相机构造器已禁止。

额外覆盖相机配置过滤、输入只请求 color、写入不压缩 depth、网页相机关闭深度仍正常健康。相机服务、管理器和 HTTP 测试共 20 项通过，JavaScript 语法检查通过。实机相机启动与三路 RGB 预览已验证；尚未进行新录制或运动回放验收。

## 部署

操作员结束 8090 采集会话并完成反初始化后执行：

```bash
sudo python3 /home/robot/collection_rgb_only/tools/deploy.py
```

部署脚本再次检查采集空闲、反初始化及 8080 无执行中的任务，验证包摘要，替换已验证 RGB-D 采集服务，安装开机覆盖配置和网页 RGB-only 环境设置，重启两项网页服务。保留 Motion_Control、SDKService 和 teleop。启动失败则尝试恢复此前 RGB-D 优化服务。

部署完成后需验证三路相机仅报告 color、网页 RGB 正常、没有深度流，随后由操作员新录一条样本检查 HDF5 无深度数据集、实际 Hz、帧间隔与关节动作。离线测试不代替实机验收。

## 恢复 RGB-D 优化服务

结束采集并反初始化后，先用本目录 `tools/switch.py restore` 恢复原厂服务，再用 `/home/robot/collection_storage_fast/tools/switch.py start` 启动原先 RGB-D 优化包。移除本次专属覆盖文件 `/etc/systemd/system/zerith-storage-fast.service.d/rgb-only.conf` 和 `/home/robot/.config/systemd/user/zerith-h1-web-control.service.d/rgb-only.conf`，重新加载系统及用户 systemd 配置并重启两个网页服务。

之前已验证的 RGB-D 包、校验清单和原厂备份均保留。旧数据中的深度图不受影响。
