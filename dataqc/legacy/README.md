# 运行兼容实现

这里只保留工作台实际加载的 `scripts/embodied_data_pipeline-main/`。
它承担原有回放、数值处理、LeRobot 可视化、跨平台比较与部分转换入口。

`workbench.py` 和 `integrations/` 通过固定的相对路径加载这些模块；不要只因目录名为
legacy 就删除或移动它。无关 ROS、采集控制、推理工程、旧开发文档和缓存已从本发布包移除。
