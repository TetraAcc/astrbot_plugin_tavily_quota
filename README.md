# astrbot_plugin_tavily_quota

AstrBot 的 Tavily 搜索引擎 API 额度与使用量查询插件。

## 功能特性
- 支持指令 `/tavily` 或 `/tavily额度`、`/查额度` 查询。
- 自动读取 AstrBot 全局配置中的 `websearch_tavily_key`，无需重复配置。
- 支持多个 API Key 轮询查询。
- 展示套餐计划、月度总配额、已用次数、剩余额度、用量进度条，以及搜索/抓取/抽取细分用量。
- 提供大模型工具（LLM Tool）支持自然语言对话查询。
- 支持管理员权限限制开关。
