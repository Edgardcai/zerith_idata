# 采集写入并行优化

适用原厂 server SHA256：`2e311a08afd94990972258bcf13beb46ea30fdd7f45b0b630ca747268838f054`，Python 3.10。独立运行包位于 `runtime/server_fast`，不覆盖只读厂商安装目录。

每批深度/红外图像在三线程池中按原等级无损预压缩，原厂 save_batch 从批次局部缓存读取结果。HDF5 写入、时间戳、关节值、JPEG、视频编码和帧顺序继续执行原厂函数。未知转换走原路径，预压缩失败也回退原路径。缓存只存在于当前批次；日志 `StorageParallel` 的“批次总耗时”包含预压缩和写入两部分，原厂内部日志只包含缓存准备后的写入阶段。

## 验证

构建只替换 server 入口，其余 1751 个归档条目字节不变。候选包的离线入口阻止真实环境、ZCM 和语音构造，以真实厂商 writer 对比同一批已保存图像与关节数据；HDF5 数据/元数据和压缩字节一致，三路 MP4 解码帧一致。测试还覆盖预压缩失败回退和 200 帧 30 Hz 持续入队。

本机完整 10 帧处理由约 345–375 ms 降至 205–230 ms；持续测试 p95 约 224 ms，低于 333 ms 预算。详见 `runtime/offline_benchmark.json` 和 `runtime/self_test.log`。这些是读取已保存帧的离线写入实验，不替代用户实际录制与真机回放验收。

## 切换与启动

结束采集并反初始化后执行：

```bash
sudo python3 /home/robot/collection_storage_fast/tools/switch.py start
```

优化服务已经运行时无需重复启动。原厂退出后窗口消失的恢复入口为 `start --recover-stopped`。只切换 `robot_startup:server`，保留 robotd、Motion_Control、SDKService、teleop。若本次停止操作后原厂新打印“所有服务关闭完毕，安全退出”却仍卡在解释器退出阶段，脚本重新检查空闲和反初始化后清理残留进程；没有该完成标记时拒绝强制清理。

`zerith-storage-fast.service` 随 robotd 启动，在网页状态可用且机器人反初始化、采集空闲时应用优化；条件不满足会超时退出并在 journal 报告。不会自动反初始化或启动录制。此服务只管理采集优化，不改变 teleop 锁定扩展的原有启动配置。

## 回退

结束采集并反初始化后：

```bash
sudo systemctl disable --now zerith-storage-fast.service
sudo python3 /home/robot/collection_storage_fast/tools/switch.py restore
```

如果需要重建，必须先把优化服务正常回退；不要覆盖正在运行的二进制：

```bash
/home/robot/miniconda3/envs/zerith/bin/python tools/build.py
./runtime/server_fast_candidate --offline-self-test
```

自检标记与二进制 SHA256 绑定。通过后才可替换停止状态的 `runtime/server_fast`。运行中重新构建会先使清单/校验标记与旧包不同，禁止在此期间做新的启动切换。
