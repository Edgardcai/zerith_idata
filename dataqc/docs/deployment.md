# 部署与运维

在项目根目录完成 README 中的环境和配置准备后，可生成适用于当前路径的 systemd 用户服务。

```bash
# 仅生成预览，不改变现有服务
.venv/bin/python deploy/install_services.py
# 安装并启动
.venv/bin/python deploy/install_services.py --apply
```

可以通过 `--python /path/to/python` 指定已有环境，通过 `--runtime /path/to/state` 指定运行目录。
`--port` 配置监听端口；`--real-root`、`--sim-root` 配置真机和仿真扫描目录；
`--binary-path` 可指定 ffmpeg/ffprobe 所在目录。默认值仍适用于本机 8091 服务。
生成的服务固定使用当前项目路径，不依赖开发机器原目录。
如需退出登录后继续运行，由机器管理员为部署用户配置 systemd linger。

```bash
systemctl --user status dataqc-web dataqc-worker
journalctl --user -u dataqc-web -u dataqc-worker -n 80
curl http://127.0.0.1:8091/healthz
curl http://127.0.0.1:8091/auto/api/health
```

更新代码后：

```bash
.venv/bin/python deploy/restart_when_idle.py --check-only
.venv/bin/python deploy/restart_when_idle.py
```

工具等待活动任务完成后再重启两个服务，不重写配置。`--wait-seconds` 控制最长等待时间，
`DATAQC_SERVICE_URL` 可以覆盖检查地址。

## 本地文件与备份

- `runtime/config/`：模型配置及 API 文件，按敏感配置管理。
- `runtime/var/grade-selections/`：人工等级与采集等级基线，必须随状态备份。
- `runtime/var/db/`、`runtime/var/runs/`：任务与审查结果。
- `runtime/var/manual/`：传统指标及 VLM 结果缓存。
- `runtime/exports/`、用户设置的外部输出目录：转换结果。

这些内容及 `.venv/`、原始 HDF5/视频、YOLO 权重都不进入 Git。备份数据库时先确保无运行任务，
使用数据库一致性备份或停止服务后复制完整运行目录。恢复源码与恢复运行状态是两件独立的操作。

## 配置

模型名称默认 `gpt-5.6-terra`，以部署方 API 实际支持的名称为准。
`api_file` 只从指定文件读取 API 地址与密钥，不读取其他应用的登录凭据。
开启类别识别前配置 `yolo_path`；未启用时不会因此阻断动作指标复核。

## H200 部署

项目目录 `/srv/projects/caizj/dataqc`，监听 `9990`：

```bash
cd /srv/projects/caizj/dataqc
export XDG_RUNTIME_DIR=/run/user/$(id -u)
export DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus
.venv/bin/python deploy/install_services.py --apply --port 9990 \
  --real-root /srv/data/datasets/public/zerith_data \
  --sim-root /srv/data/datasets/public/zerith_sim_data \
  --binary-path /home/caizj/miniconda3/bin
```

生成的服务设置 `DATAQC_REAL_ROOT`、`DATAQC_SIM_ROOT` 和 `DATAQC_PORT`。
直接运行时也可设置这三个环境变量；API 凭据与 YOLO 权重放在该部署的 `runtime/` 下，禁止提交 Git。
更新后用 `DATAQC_SERVICE_URL=http://127.0.0.1:9990 .venv/bin/python deploy/restart_when_idle.py` 重启。
