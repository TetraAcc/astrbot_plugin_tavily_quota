# API 额度查询（Tavily / OpenRouter / DeepSeek / GitHub）

AstrBot 插件 `astrbot_plugin_tavily_quota`，支持通过命令或自然语言查询 Tavily、OpenRouter、DeepSeek 与 GitHub 的额度和使用情况。

[AstrBot 插件页](https://cloud.astrbot.app/plugin/TetraAcc/astrbot_plugin_tavily_quota?returnTo=/profile&tab=docs)

当前版本：**v1.2.0**。完整版本记录见 [更新日志](CHANGELOG.md)。

## 功能

- Tavily：展示账户套餐、配额、已用与剩余额度，以及搜索、抽取和抓取用量。
- OpenRouter：展示账户充值与消费、余额、日／周／月用量，并在接口提供相应数据时展示 Key 预算和过期时间。
- DeepSeek：展示可用状态、币种、总余额、充值余额和赠送体验金，多币种分别展示。
- GitHub：展示当前 Token 对应的 Core API 剩余次数、总配额和本地时区重置时间。
- `/quota` 聚合已配置的四类服务，按服务和凭据分组；未配置的服务跳过，单项失败不影响其他结果。
- 支持多个 Key 查询，自动清理空值和重复 Key。
- 支持管理员限制，以及独立的 LLM 工具开关。

## 使用方式

| 入口 | 用途 | 别名 |
| --- | --- | --- |
| `/tavily` | 查询 Tavily 额度 | `/tavily额度`、`/查额度`、`/tavily_quota` |
| `/openrouter` | 查询 OpenRouter 额度 | `/open`、`/openrouter额度` |
| `/quota` | 汇总全部已配置服务 | `/配额` |
| `/quota tavily`、`/quota openrouter` | 单独查询原有服务 | 原有指令继续可用 |
| `/quota deepseek` | 查询 DeepSeek 余额 | `/deepseek_quota` |
| `/quota github` | 查询 GitHub API Rate Limit | `/github_quota` |

开启 LLM 工具后，也可以说“查询 Tavily 剩余额度”“查询 OpenRouter 余额”“查询 DeepSeek 余额”或“查询 GitHub 剩余 API 次数”，由模型调用对应工具：

- `query_tavily_quota`
- `query_openrouter_quota`
- `query_deepseek_quota`
- `query_github_quota`

自然语言调用是否触发取决于当前模型和 AstrBot 的工具配置；命令可以直接发起查询。

## 配置项

在 AstrBot WebUI 的插件配置页设置以下选项：

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `api_keys` | `[]` | 自定义 Tavily API Key 列表；留空时自动读取其他来源。 |
| `openrouter_api_keys` | `[]` | 自定义 OpenRouter API Key 列表；留空时自动读取其他来源。 |
| `deepseek_api_keys` | `[]` | DeepSeek Key 列表；留空探查当前会话 Provider，最后回退环境变量。 |
| `github_tokens` | `[]` | GitHub PAT 列表；兼容 `github_personal_access_tokens` 名称，再回退环境变量。 |
| `admin_only` | `false` | 开启后，命令和 LLM 工具均只允许 AstrBot 管理员查询。 |
| `enable_llm_tool` | `true` | 是否启用四个 LLM 额度查询工具；关闭不影响命令查询。 |

管理员身份统一使用 `event.is_admin()` 判断，请在 AstrBot 中配置管理员。插件不再维护个人账号白名单。

## Key 来源

按以下优先级读取，找到后不再合并其他来源：

1. 插件配置的 `api_keys`、`openrouter_api_keys`、`deepseek_api_keys` 或 `github_tokens`。GitHub 还兼容 `github_personal_access_tokens`；两者都填时以 `github_tokens` 为准。
2. 通过 `Context.get_config(umo=event.unified_msg_origin)` 获取当前会话配置。Tavily 优先读取 `provider_settings.websearch_tavily_key`，兼容根层级的同名字段；OpenRouter、DeepSeek 从已启用且 `provider` 或 `type` 包含对应服务名称的 `provider_sources` 中读取 `key`。GitHub 不从框架模型配置或 Git 凭据中探查 Token。
3. 对应环境变量 `TAVILY_API_KEY`、`OPENROUTER_API_KEY`、`DEEPSEEK_API_KEY`；GitHub 依次尝试 `GITHUB_TOKEN`、`GH_TOKEN`。

当前来源中的有效 Key 会去空白、去重；禁用的 OpenRouter、DeepSeek 提供商来源不会参与查询。插件不直接读取配置文件，也不扫描其他会话的配置档。

Tavily、OpenRouter 查询结果继续默认隐藏 Key；DeepSeek、GitHub 只显示首尾脱敏片段，短凭据显示 `***`。完整 Key/Token 不返回聊天或模型，也不写入日志。凭据仅用于对应官方接口认证，不写入配置文件或数据库。

## 工具开关

修改 `enable_llm_tool` 后保存并在 WebUI 重载插件。关闭后，四个 LLM 工具会通过 AstrBot 公开接口停用；重新开启并重载后恢复工具。

工具入口同时检查开关和管理员权限，防止旧工具调用绕过设置。无论工具开关是否开启，命令查询都遵循 `admin_only` 配置。

## GitHub Actions 与接口边界

GitHub 官方已于2025-09-26宣布停用旧产品计费接口，详见[官方公告](https://github.blog/changelog/2025-09-26-product-specific-billing-apis-are-closing-down/)。本插件按照兼容需求尝试 `/user` 和 `/users/{login}/settings/billing/actions`；认证权限不足、接口退役、超时或返回异常时，不显示 Actions 模块，仍保留 Rate Limit。

仅当接口实际返回 `total_minutes_used` 时展示 Actions。旧响应缺少 `included_minutes` 时使用2000分钟的兼容默认值，并明确标注它不是已核实套餐额度；不承诺现代 GitHub 账号均能查询 Actions。Core 配额来自接口，不能固定假定所有 Token 都有5000次。

四类服务的每个 HTTP 请求超时均为10秒，聚合查询顺序执行，总耗时会随已配置服务、凭据及附加接口数量增加。金额不跨币种汇总；不同 Key 可能属于同一账户，因此也不相加账户余额。

参考接口：[DeepSeek 余额](https://api-docs.deepseek.com/api/get-user-balance/)、[GitHub Rate Limit](https://docs.github.com/en/rest/rate-limit/rate-limit)。

## 展示示意

以下为模拟文字示意，非真实账户截图；只显示已配置服务。

```text
📊 API 配额汇总

DeepSeek · 凭据 1
🔑 Key: sk-a…1234
✅ 可用状态: 可用
💱 币种: CNY
✨ 总可用余额: 28.50
💳 充值余额: 18.50
🎁 赠送体验金: 10.00

GitHub · 凭据 1
🔑 Token: ghp_…5678
📊 Core API 剩余 / 总配额: 4,321 / 5,000
⏰ 重置时间: 2026-10-31 21:00:00 JST（本地时间）
```

## 升级到 v1.2.0

1. 更新插件后，在 WebUI 重载插件；保留原有 Tavily、OpenRouter Key、管理员限制及工具开关配置。
2. 使用 DeepSeek 时，填写 `deepseek_api_keys`，或确认当前会话存在已启用、`provider` 或 `type` 包含 `deepseek` 的 Provider；否则使用 `DEEPSEEK_API_KEY` 环境变量。
3. 使用 GitHub 时，在 `github_tokens` 中填写自己的 PAT，或让 AstrBot 进程获得 `GITHUB_TOKEN` / `GH_TOKEN`；插件不使用本机 Git 登录凭据。
4. 发送 `/quota` 查询全部已配置服务，或发送 `/quota deepseek`、`/quota github` 单独查询。未配置新服务不影响原有命令。
5. `enable_llm_tool` 现统一控制四个额度工具。关闭后，所有斜杠命令仍可使用，并继续遵循 `admin_only`。

本次没有数据迁移，也不会自动修改已有凭据配置。GitHub Actions 查询的可用性限制见上文。

### 从 v1.1.0 升级时的额外注意事项

1. 更新插件后，在 WebUI 重载插件。
2. 若启用了 `admin_only`，确认操作账号已被 AstrBot 识别为管理员；旧版的个人账号特例已移除。
3. 若不需要自然语言查询，关闭 `enable_llm_tool`，保存并重载插件。
4. 若升级后提示未检测到 Key，检查当前会话配置或直接填写插件的 Key 列表。新版不再从其他配置档收集 Key。

`/reload_tavily` 已移除，后续统一通过 WebUI 重载。原有查询命令和别名保留。

## 常见问题

- **提示仅管理员可查询**：检查 AstrBot 管理员配置；如需向普通用户开放，可关闭 `admin_only`。
- **提示工具已禁用**：开启 `enable_llm_tool` 并重载插件，或直接使用查询命令。
- **提示未检测到 Key**：按“Key 来源”检查插件配置、当前会话配置及环境变量。修改环境变量后，需要让 AstrBot 进程获得新值。
- **额度查询失败**：检查 Key 有效性以及到对应官方 API 的网络连接；插件不会自动切换第三方接口。

## 本地验证

```sh
python -m unittest discover -s tests -v
```

请在已有 `aiohttp` 的 Python 环境中，于插件目录运行。38项测试覆盖管理员权限、工具启停、Key 来源优先级、聚合与单服务查询、脱敏、请求超时以及 GitHub 附加接口降级，使用模拟框架和网络调用，无需真实 Key，也不会消耗真实查询额度。这些测试不等同于真实账户接口或 AstrBot 完整消息调度的集成验收。
