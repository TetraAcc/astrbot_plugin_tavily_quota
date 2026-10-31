# API 额度查询

当前版本：**v1.2.1** · [更新日志](CHANGELOG.md)

支持多个 Key、自然语言查询和管理员权限控制。

## 指令与功能

| 服务 | 指令 | 查询内容 |
| --- | --- | --- |
| Tavily | `/tav` | 套餐、已用与剩余额度 |
| OpenRouter | `/open`、`/openrouter` | 账户余额、消费和 Key 预算 |
| DeepSeek | `/deep`、`/deepseek` | 可用状态、币种、总余额、充值余额和体验金 |
| GitHub | `/git`、`/github` | Core API 剩余次数、配额和重置时间 |
| 全部服务 | `/quota` | 汇总已配置服务，跳过未配置项 |

开启 LLM 工具后，也可直接询问“查一下 DeepSeek 余额”等，由模型调用查询工具。

## 配置

在 AstrBot WebUI 的插件配置页填写，保存后重载插件。

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `api_keys` | `[]` | Tavily Key 列表 |
| `openrouter_api_keys` | `[]` | OpenRouter Key 列表 |
| `deepseek_api_keys` | `[]` | DeepSeek Key 列表 |
| `github_tokens` | `[]` | GitHub PAT 列表 |
| `admin_only` | `false` | 仅限 AstrBot 管理员查询 |
| `enable_llm_tool` | `true` | 启用四个自然语言查询工具；关闭不影响命令 |

Key 留空时，Tavily、OpenRouter、DeepSeek 优先读取当前会话的对应配置，再读取 `TAVILY_API_KEY`、`OPENROUTER_API_KEY`、`DEEPSEEK_API_KEY` 环境变量。GitHub 依次读取 `GITHUB_TOKEN`、`GH_TOKEN`，不读取本机 Git 凭据。

## 说明

- 完整 Key/Token 不展示，新增服务仅显示脱敏片段。
- 每个请求超时10秒；汇总查询中单项失败不影响其他结果。
- GitHub Actions 旧计费接口已退役，附加信息不可用时只显示 API Rate Limit。
