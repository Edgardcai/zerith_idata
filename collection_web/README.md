# ZERITH 独立采集工作台

网站默认运行在 **8090**。项目、进程、页面和数据索引均独立于 `/home/robot/control`，不会调用 8080 API，也不会初始化机器人或发送关节运动指令。

## 首次部署到机器人

将此 `collection_web` 目录部署至 `/home/robot/collection_web`。当前版本使用机器上已有的 H1 1.3.9 SDK 和 zerith Python 环境；厂商 SDK、虚拟环境及采集数据不包含在仓库中。

```bash
cd /home/robot/collection_web
/home/robot/miniconda3/envs/zerith/bin/python -m venv --system-site-packages .venv
.venv/bin/python -c "import grpc, google.protobuf, h5py, numpy, cv2"
```

手动启动使用下方 `start.sh`。需要用户服务时，先确认 8090 未被手动启动的进程占用，再安装：

```bash
mkdir -p ~/.config/systemd/user
cp deploy/zerith-collection-web.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now zerith-collection-web.service
```

测试报告中的 runtime 证据文件仅保存在验收机器人上，不随代码上传。本分支同时提供配套的 teleop_zero_lock 扩展，详见根目录 README。

## 启动

```bash
/home/robot/collection_web/start.sh
```

已安装用户服务：

```bash
systemctl --user status zerith-collection-web.service
systemctl --user restart zerith-collection-web.service
journalctl --user -u zerith-collection-web.service -n 50
```

默认监听 `0.0.0.0:8090`。浏览器访问 `http://机器人IP:8090`。页面更改刷新生效，Python 更改需要重启；不要在采集中重启。

## 采集操作

1. 结束 Apifox 的采集调用。连接 Meta Quest，并按厂商遥操作流程初始化机器人。
2. 填写 Prompt；网站提取左右物体，可手动修正。填写“升降柱高度 · m”后，保存目录预览为“左商品_右商品_高度”；留空沿用原目录。展开“采集参数”设置任务编号、阶段数、频率和最长时长。
3. 点击“设备检测”，全部通过后点击“启动采集”。配置通过厂商 `MetaTransfer` 提交；以厂商生成的任务元数据确认接受。
4. 用 Meta Quest 控制每条数据的开始、分阶段、结束。页面显示当前阶段、帧数、录制时长及保存状态。
5. 完整保存后自动编号为 `episode_000001` 等，默认评级 A；可改 B/F、查看三路视频、或确认放弃并删除该条数据。
6. 先用手柄结束当前录制并等待保存，然后点击“结束会话”关闭网站持有的采集连接。关闭网页不会自动关闭后端会话。

自动 A 为默认人工标签，不等于机器自动判断抓取成功。实际采样率、写入告警与文件完整性单独显示。序号删除后不复用；只有网站本次会话新生成的数据会自动整理，启动前已有数据不自动改名。

## 独立依赖

- 厂商 gRPC `127.0.0.1:50051`：任务配置、VR/设备状态、相机预览。
- 厂商状态服务 `127.0.0.1:25120`：电机错误、通信、电量、初始化和控制模式。
- 厂商 ZCM `ipcshm`：只读订阅 `waist_state`、`head_state`，明确对应电机 2—6；不构造占用固定 UDP 端口的机器人控制客户端。
- H1 1.3.9 相机 SDK、Python 3.10、grpcio/protobuf/h5py/numpy/opencv-python，以及 g++/pkg-config/zcm。
- `.venv` 使用本机 zerith Python 的系统包；Playwright 只用于开发测试，不是网站运行依赖。

`joint_observer.cpp` 只封装公开状态订阅接口，没有任何控制消息发布。`start.sh` 在源码更新或二进制缺失时编译。

## 权限与文件

Web 服务以 robot 用户运行。厂商 root 写入的新数据需要继承 ACL：

```bash
sudo setfacl -m u:robot:rwx,d:u:robot:rwx /data/zerith_data
```

已有任务目录若不可写，应对**该任务目录**设置相同 ACL，之后创建的 episode 会继承权限。网站启动前会检查目录可写性。无需把 Web 服务以 root 启动，也无需把 sudo 密码写入任何脚本。

- `runtime/collection.sqlite3`：会话、编号、评级、操作记录；不要删除，否则无法保留历史编号映射。
- `runtime/session_*.json`：任务配置快照。
- `runtime/meta_*.jsonl`：厂商响应记录。
- `episode_XXXXXX/review.json`：原 UUID、序号、评级与人工复核标志。

保留 HDF5 原始 episode_id 和采集日志；改的是目录名称。现有训练转换器不会自动读取 review.json，后续可按该文件或网站索引筛选 A/B/F。

仅对明确完成、结构检查通过的文件做自动整理。未完成/异常的文件留在原目录供检查。关闭会话后不再接管后续由其他客户端产生的数据。断线不自动重新提交 MetaTransfer，避免重复任务。

## 测试

```bash
cd /home/robot/collection_web
.venv/bin/python -m unittest discover -s tests -v
node --check static/app.js
```

自动化测试只使用临时目录和本机模拟 gRPC 服务，不触发真机采集、不修改历史数据。现场验证结果见 `TEST_REPORT.md`。

网站面向机器人所在可信局域网；变更接口校验页面令牌和同源请求。不要将 8090 直接转发至公网。

## 按商品与高度命名目录

例如左右目标为 `Dahongpao Milk Tea`、`If coconut`，高度填写 `0.8`，最终目录为：

```text
/data/zerith_data/DahongpaoMilkTea_Ifcoconut_0.8/episode_000001
```

商品名称去除空格，保留大小写；支持文字、数字和短横线。高度使用米数，仅用于目录命名，不调整机器人。`0.80` 与 `0.8` 使用同一目录。高度留空时兼容原来的任务编号加提示词目录。

厂商仍按原任务配置采集到原目录。网站等一条完整保存、校验通过后，再在同一文件系统内移动并编号；正在写入的数据不搬动。页面显示最终保存目录，原始写入位置记录在会话配置中。

同名数据目录继续递增编号，已有文件及 task_meta.json 不覆盖。每条新归档数据另存 collection_task.json，记录本条真实任务配置、左右商品、填写高度及路径；HDF5 内原有提示词和 UUID 不修改。历史会话和历史数据不会自动迁移。评级、查看和放弃按钮使用归档后的路径。

## 遥操提示与固定升降柱（1.0）

配合 `/home/robot/teleop_zero_lock` 扩展，页面显示标定成功、遥操状态和头腰偏差预警，可启用当前浏览器语音提示。头腰位置偏差不再暂停遥操；原厂故障保护仍保留。

“固定升降柱”支持 0–0.8 m，反初始化后保存，下次初始化平滑到位并保持。它与任务区用于目录命名的高度标签分开；此设置不改 Prompt 或采集文件内容。配置通过 `POST /api/teleop/config` 原子保存，不直接发送电机命令。关闭固定后，下次初始化恢复厂商升降柱遥操逻辑。
