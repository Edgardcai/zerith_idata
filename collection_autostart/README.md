# 采集和遥操的开机启动

机器人 `192.168.10.45` 上，启动源码位于 `/home/robot/collection_autostart/`。`robotd.service` 运行 `/usr/local/lib/zerith-collection-autostart/direct_boot.py`，启动结果在 `/run/zerith-collection-autostart/status.json`。

`direct_boot.py` 是 `robotd.service` 的启动入口。它校验厂商文件和已通过离线验证的采集及遥操软件包，再生成启动脚本。`boot_guard.py` 由两个扩展的构建脚本嵌入，校验开机启动身份。

目标机器人需要已验证的 `collection_depth_toggle` 与 `teleop_zero_lock` 运行包，以及系统侧的 `robot-wifi-client-only` 组件。`install_direct.py` 安装 `direct-boot.conf` 和启动配置，下次启动 `robotd.service` 时生效。

本目录的 `tests/` 使用模拟启动配置和进程状态，可以通过 `python -m pytest collection_autostart/tests` 离线执行。
