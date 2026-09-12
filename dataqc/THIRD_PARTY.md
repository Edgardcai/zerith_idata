# 第三方组件

`vendor/lerobot/` 固定为 LeRobot 0.3.3，供数据格式兼容与读取使用。原项目：
https://github.com/huggingface/lerobot 。Apache-2.0 许可证保留于
`vendor/lerobot-0.3.3.dist-info/licenses/LICENSE`，版本元数据保留于同目录。
该目录作为源码依赖随项目使用，不包含安装器缓存与指向开发机器的入口脚本。

`legacy/scripts/embodied_data_pipeline-main/` 是本系统实际调用的原有质检、回放与
跨平台分析兼容实现；相关代码已在此副本中调整。原机器上的独立采集工程不属于本发布包。

其余 Python 依赖与固定版本见 `pyproject.toml`，各组件遵循自己的许可证。
