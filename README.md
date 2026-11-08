# 全能 API 额度与余额监控 (astrbot_plugin_api_quota)

当前版本：**v1.4.0** · [更新日志](CHANGELOG.md)

支持多个 Key、暗色图片看板、未配置服务自动隔离隐藏、低额度自动预警、大模型自然语言查询和管理员权限控制。

## 指令与功能

| 服务 | 指令 | 查询内容 | 说明 |
| --- | --- | --- | --- |
| Tavily | `/tav`、`/tavily` | 套餐、已用与剩余额度 | 支持 `/tav img` 或 `/tav text` |
| OpenRouter | `/open`、`/openrouter` | 账户余额、消费和 Key 预算 | 支持 `/open img` 或 `/open text` |
| DeepSeek | `/deep`、`/deepseek` | 可用状态、币种、总余额、充值金和体验金 | 支持 `/deep img` 或 `/deep text` |
| GitHub | `/git`、`/github` | Core API 剩余次数、配额和重置时间 | 支持 `/git img` 或 `/git text` |
| SiliconFlow (硅基流动) | `/sili`、`/silicon` | 可用总余额、充值余额、赠送金及账户状态 | 支持 `/sili img` 或 `/sili text` |
| Moonshot AI (Kimi) | `/kimi`、`/moonshot` | 可用总余额、现金余额与优惠代金券 | 支持 `/kimi img` 或 `/kimi text` |
| Zhipu AI (智谱/GLM) | `/glm` | 账户现金余额、总资产与 Coding Plan 窗口配额 | 支持 `/glm img` 或 `/glm text` |
| Serper (Google Search) | `/serper` | Google 搜索 API 剩余调用配额 | 支持 `/serper img` 或 `/serper text` |
| 全部服务 | `/quota` | 汇总已配置服务看板（未配置项自动隐藏） | 默认渲染高颜值仪表盘图片 |
| 额度预警 | `/quota alert here` | 绑定当前聊天会话并开启低额度预警 | 管理员指令 |
| 预警测试 | `/quota alert check` | 立即执行一次额度巡检并汇报结果 | 管理员指令 |
| 关闭预警 | `/quota alert off` | 停止自动巡检与预警 | 管理员指令 |

> 💡 **提示**：
> 1. 开启 LLM 工具后，可直接向机器人提问“查一下 DeepSeek 余额”、“硅基流动还剩多少钱”或“Tavily 还能搜几次”，大模型将自动调用对应工具返回文本。
> 2. 默认在支持发送图片的平台上优先呈现**暗色仪表盘卡片图**，若需纯文本可在任意指令后加 `text`（例如 `/quota text`、`/sili text`）。
> 3. `/quota` 会自动识别并只展示你配置了凭据的平台，未配置或未启用的平台绝不会多余显示。

---

## 🔔 低额度主动巡检与预警

无需人工时常查询，插件可在后台定时自动巡检各服务余额：
- **快捷绑定**：管理员在目标群或私聊窗口发送 `/quota alert here`，插件自动记录当前会话 `UMO` 并开启巡检。
- **阈值监控**：
  - DeepSeek 余额低于 **¥5.0**（可配）
  - OpenRouter 余额低于 **$1.0**（可配）
  - Tavily 剩余搜索次数低于 **100 次**（可配）
  - GitHub Core API 剩余次数低于 **500 次**（可配）
  - SiliconFlow (硅基流动) 余额低于 **¥5.0**（可配）
  - Moonshot AI (Kimi) 余额低于 **¥5.0**（可配）
  - Zhipu AI (智谱/GLM) 余额低于 **¥5.0**（可配）
  - Serper 剩余搜索次数低于 **100 次**（可配）
- **克制防扰**：低额度状态未恶化前不会在每个周期重复轰炸，安心护航机器人运行。

---

## 配置说明

在 AstrBot WebUI 的插件配置页填写，保存后重载插件。

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `api_keys` | `[]` | Tavily Key 列表（留空自动读取会话配置或环境变量） |
| `openrouter_api_keys` | `[]` | OpenRouter Key 列表（留空自动读取会话配置或环境变量） |
| `deepseek_api_keys` | `[]` | DeepSeek Key 列表（留空自动读取当前启用的 Provider） |
| `github_tokens` | `[]` | GitHub PAT 列表（仅读取专用配置或环境变量） |
| `siliconflow_api_keys` | `[]` | SiliconFlow (硅基流动) Key 列表（留空读取 Provider 或环境变量） |
| `moonshot_api_keys` | `[]` | Moonshot AI (Kimi) Key 列表（留空读取 Provider 或环境变量） |
| `zhipu_api_keys` | `[]` | Zhipu AI (智谱/GLM) Key 列表（留空读取 Provider 或环境变量） |
| `serper_api_keys` | `[]` | Serper Key 列表（留空读取环境变量 `SERPER_API_KEY`） |
| `render_image` | `true` | 启用暗色图片看板（零外部浏览器依赖，异常自动降级为文本） |
| `enable_alert` | `false` | 启用低额度后台自动巡检与预警推送 |
| `alert_interval_hours` | `12.0` | 自动巡检间隔时间（小时） |
| `alert_notify_target` | `""` | 预警通知接收会话（建议在会话中直接发 `/quota alert here` 自动绑定） |
| `alert_thresholds` | 对象 | 各平台的预警数值阈值对象 |
| `admin_only` | `false` | 仅限 AstrBot 管理员查询 |
| `enable_llm_tool` | `true` | 启用自然语言查询工具；关闭不影响命令 |

---

## 安全规范与排障

- **凭据脱敏**：任何输出均做掩码处理，日志不记录响应正文与密钥。
- **安全隔离**：严格按会话（UMO）隔离凭据，**绝不读取**本机 Git 凭据（防止越权）。
- **图片降级**：基于 Pillow 纯 Python 绘制，自带开源 CJK 字体，无外部网络依赖，跨平台（Windows / Linux / SteamOS / macOS）100% 杜绝豆腐块乱码；遇到未装图像库时优雅降级为纯文本。
