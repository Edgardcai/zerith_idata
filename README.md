# ZERITH 数据采集工作台

`data_collection_new` 维护 ZERITH 机器人的采集工作台、录制扩展和开机启动。工作台通过 `http://<机器人地址>:8090/` 提交任务，Meta Quest 手柄控制每条录制。完成的 HDF5 和三路视频保存在 `/data/zerith_data`，支持按日期和场景浏览、回放和评级。深度图按会话选择，默认关闭。

## 目录

| 目录 | 用途 |
| --- | --- |
| [`collection_web/`](collection_web/README.md) | 网页、设备状态、任务配置、采集会话和数据复核。入口为 `app.py`，会话逻辑在 `collector.py`，数据整理在 `episodes.py`。 |
| [`collection_depth_toggle/`](collection_depth_toggle/README.md) | 录制服务扩展，含相机时序对齐、音频缓存及按会话切换 RGB / RGB-D。 |
| [`collection_autostart/`](collection_autostart/README.md) | 开机时校验并启动采集与遥操组件。 |
| [`teleop_zero_lock/`](teleop_zero_lock/README.md) | 遥操扩展和固定头腰、升降柱配置。 |
| [`collection_rgb_only/`](collection_rgb_only/README.md)、[`collection_storage_fast/`](collection_storage_fast/README.md) | 录制服务回滚链中的历史组件。 |
| [`web_control/`](web_control/README.md)、[`voice_assistant/`](voice_assistant/README.md)、[`chinese_speech/`](chinese_speech/README.md) | 控制台和语音工具，入口见各目录说明。 |

## 采集流程

1. 连接 Meta Quest 并完成遥操初始化。
2. 在工作台应用日期和场景，选择货架抓取、桌面取放、货架放置或冰箱抓取。冰箱抓取使用单手。
3. 完成设备检测后启动采集会话。用手柄长按 Y 开始一条录制，短按 Y 切换阶段或保存。
4. 确认文件保存后可回放三路视频和调整 A/B/F 评级。结束录制并等待保存后，再结束网页会话。

## 开发与验证

- 网页运行于 Python 3.10。依赖、用户服务和测试命令见 [`collection_web/README.md`](collection_web/README.md)。
- 录制服务构建需要匹配版本的厂商 `server`，离线自测需要一条本地已完成的 HDF5 样本。步骤见 [`collection_depth_toggle/README.md`](collection_depth_toggle/README.md)。
- 机器人控制接口的说明见 [`CORE_GUIDE.md`](CORE_GUIDE.md) 和 [`CONTROL_README.md`](CONTROL_README.md)。

采集文件、SQLite 索引、厂商二进制、运行时回滚包和机器配置由部署主机管理。
