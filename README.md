# API 额度查询（Tavily & OpenRouter）

AstrBot 插件 `astrbot_plugin_tavily_quota`，支持通过命令或自然语言查询 Tavily 与 OpenRouter 的额度和使用情况。

https://cloud.astrbot.app/plugin/TetraAcc/astrbot_plugin_tavily_quota?returnTo=/profile&tab=docs

当前版本：**v1.1.2**。完整版本记录见 [更新日志](CHANGELOG.md)。

## 功能

- Tavily：展示账户套餐、配额、已用与剩余额度，以及搜索、抽取和抓取用量。
- OpenRouter：展示账户充值与消费、余额、日／周／月用量，并在接口提供相应数据时展示 Key 预算和过期时间。
- 支持多个 Key 查询，自动清理空值和重复 Key。
- 支持管理员限制，以及独立的 LLM 工具开关。

## 使用方式

| 入口 | 用途 | 别名 |
| --- | --- | --- |
| `/tavily` | 查询 Tavily 额度 | `/tavily额度`、`/查额度`、`/tavily_quota` |
| `/openrouter` | 查询 OpenRouter 额度 | `/open`、`/openrouter额度` |

开启 LLM 工具后，也可以说“查询 Tavily 剩余额度”或“查询 OpenRouter 余额”，由模型调用对应工具：

- `query_tavily_quota`
- `query_openrouter_quota`

自然语言调用是否触发取决于当前模型和 AstrBot 的工具配置；命令可以直接发起查询。

## 配置项

在 AstrBot WebUI 的插件配置页设置以下选项：

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `api_keys` | `[]` | 自定义 Tavily API Key 列表；留空时自动读取其他来源。 |
| `openrouter_api_keys` | `[]` | 自定义 OpenRouter API Key 列表；留空时自动读取其他来源。 |
| `admin_only` | `false` | 开启后，命令和 LLM 工具均只允许 AstrBot 管理员查询。 |
| `enable_llm_tool` | `true` | 是否启用两个 LLM 额度查询工具；关闭不影响命令查询。 |

管理员身份统一使用 `event.is_admin()` 判断，请在 AstrBot 中配置管理员。插件不再维护个人账号白名单。

## Key 来源

按以下优先级读取，找到后不再合并其他来源：

1. 插件配置的 `api_keys` 或 `openrouter_api_keys`。
2. 通过 `Context.get_config(umo=event.unified_msg_origin)` 获取当前会话配置。Tavily 优先读取 `provider_settings.websearch_tavily_key`，兼容根层级的同名字段；OpenRouter 从已启用的 OpenRouter `provider_sources` 中读取 `key`。
3. 环境变量 `TAVILY_API_KEY` 或 `OPENROUTER_API_KEY`。

当前来源中的有效 Key 会去空白、去重；禁用的 OpenRouter 提供商来源不会参与查询。插件不直接读取配置文件，也不扫描其他会话的配置档。

查询结果默认不展示 Key。Key 仅作为对应服务官方额度接口的认证凭据，不会作为查询结果返回给聊天模型。

## 工具开关

修改 `enable_llm_tool` 后保存并在 WebUI 重载插件。关闭后，两个 LLM 工具会通过 AstrBot 公开接口停用；重新开启并重载后恢复工具。

工具入口同时检查开关和管理员权限，防止旧工具调用绕过设置。无论工具开关是否开启，命令查询都遵循 `admin_only` 配置。

## 从 v1.1.0 升级

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

请在插件目录运行。测试覆盖管理员权限、已移除账号特例、工具启停、Key 来源优先级和查询输出中的 Key 隐藏，使用模拟框架和网络调用，无需真实 Key，也不会消耗真实查询额度。
