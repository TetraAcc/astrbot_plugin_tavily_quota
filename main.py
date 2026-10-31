import os
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

import aiohttp
from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.core import AstrBotConfig


@register(
    "tavily_quota",
    "Tetra",
    "Tavily、OpenRouter、DeepSeek 与 GitHub 额度查询插件",
    "1.2.1",
)
class TavilyQuotaPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

    # ──────────────────────────────────────
    #  OpenRouter 相关方法
    # ──────────────────────────────────────

    async def initialize(self):
        """通过公开接口同步工具开关；工具入口仍会逐次校验权限。"""
        enabled = bool(self.config.get("enable_llm_tool", True))
        set_enabled = (
            self.context.activate_llm_tool_async
            if enabled
            else self.context.deactivate_llm_tool_async
        )
        for name in (
            "query_tavily_quota", "query_openrouter_quota",
            "query_deepseek_quota", "query_github_quota",
        ):
            await set_enabled(name)

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self):
        # 框架启动时会恢复工具状态，完成后再次应用插件开关。
        await self.initialize()

    def _can_query(self, event: AstrMessageEvent) -> bool:
        if not self.config.get("admin_only", False):
            return True
        return bool(event.is_admin())

    @staticmethod
    def _normalize_keys(value) -> list[str]:
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, (list, tuple)):
            return []
        return list(dict.fromkeys(
            k.strip() for k in value if isinstance(k, str) and k.strip()
        ))

    def _get_framework_config(self, event: AstrMessageEvent | None = None):
        """仅通过公开接口获取当前会话配置，不遍历其他配置档。"""
        try:
            return self.context.get_config(
                umo=event.unified_msg_origin if event is not None else None
            )
        except Exception:
            logger.warning("[tavily_quota] 无法通过配置接口获取当前会话配置。")
            return {}

    def _get_openrouter_keys(
        self, event: AstrMessageEvent | None = None,
    ) -> list[str]:
        """插件配置 → 当前会话配置接口 → 环境变量。"""
        keys = self._normalize_keys(self.config.get("openrouter_api_keys", []))
        if keys:
            return keys

        data = self._get_framework_config(event)
        sources = data.get("provider_sources", [])
        if isinstance(sources, list):
            for source in sources:
                if (
                    not isinstance(source, dict)
                    or not source.get("enable", True)
                ):
                    continue
                provider = str(source.get("provider", "")).lower()
                source_type = str(source.get("type", "")).lower()
                if "openrouter" in provider or "openrouter" in source_type:
                    keys.extend(self._normalize_keys(source.get("key", [])))

        return list(dict.fromkeys(keys)) or self._normalize_keys(
            os.environ.get("OPENROUTER_API_KEY", "")
        )

    async def _fetch_openrouter_quota(self, api_key: str) -> dict | None:
        headers = {"Authorization": f"Bearer {api_key}"}
        result = {}
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                # 1. 查询 API Key 详情与单 Key 限额
                try:
                    async with session.get(
                        "https://openrouter.ai/api/v1/auth/key",
                        headers=headers,
                    ) as resp:
                        if resp.status == 200:
                            key_data = await resp.json()
                            result["key_info"] = key_data.get("data", {})
                        else:
                            logger.error(
                                "[tavily_quota] OpenRouter Key 查询失败，"
                                f"HTTP {resp.status}。"
                            )
                except Exception:
                    logger.error("[tavily_quota] 请求 OpenRouter Key API 异常。")

                # 2. 查询账户真实总充值与消费 (credits)
                try:
                    async with session.get(
                        "https://openrouter.ai/api/v1/credits",
                        headers=headers,
                    ) as resp:
                        if resp.status == 200:
                            cred_data = await resp.json()
                            result["credits"] = cred_data.get("data", {})
                        else:
                            logger.error(
                                "[tavily_quota] OpenRouter Credits 查询失败，"
                                f"HTTP {resp.status}。"
                            )
                except Exception:
                    logger.error(
                        "[tavily_quota] 请求 OpenRouter Credits API 异常。"
                    )

            if not result.get("key_info") and not result.get("credits"):
                return None
            return result
        except Exception:
            logger.error("[tavily_quota] 请求 OpenRouter API 出现异常。")
            return None

    def _format_openrouter_message(
        self, api_key: str, data: dict, show_key: bool = False,
    ) -> str:
        k_info = data.get("key_info", {})
        credits_info = data.get("credits", {})

        # 兼容旧格式
        if not k_info and not credits_info and "data" in data:
            k_info = data.get("data", {})

        # 账户真实充值与消费
        total_credits = credits_info.get("total_credits")
        total_usage = credits_info.get("total_usage", 0.0)

        has_credits = total_credits is not None
        if has_credits:
            account_balance = max(0.0, total_credits - total_usage)
            balance_str = f"${account_balance:.4f}"
            total_credits_str = f"${total_credits:.2f}"
            total_usage_str = f"${total_usage:.4f}"
            progress = self._render_progress_bar(
                int(total_usage * 10000), int(total_credits * 10000),
            ) if total_credits > 0 else ""
        else:
            budget = k_info.get("effective_budget", {})
            limit_usd = budget.get("limit_usd")
            spend_usd = budget.get("spend_usd", 0.0)
            remaining_usd = budget.get("remaining_usd")
            total_credits_str = (
                f"${limit_usd:.2f}" if limit_usd is not None else "无限制"
            )
            total_usage_str = f"${spend_usd:.4f}"
            balance_str = (
                f"${remaining_usd:.4f}" if remaining_usd is not None else "无限制"
            )
            progress = self._render_progress_bar(
                int(spend_usd * 10000), int(limit_usd * 10000),
            ) if limit_usd and limit_usd > 0 else ""

        is_free = k_info.get("is_free_tier", False)
        expires_at = k_info.get("expires_at", "")

        usage_daily = k_info.get("usage_daily", 0.0)
        usage_weekly = k_info.get("usage_weekly", 0.0)
        usage_monthly = k_info.get("usage_monthly", 0.0)

        lines = [
            "📊 【OpenRouter 额度使用情况】",
        ]
        if show_key:
            label = k_info.get("label", "")
            if label:
                lines.append(f"🔑 密钥: {label}")
            else:
                masked = (
                    f"{api_key[:8]}...{api_key[-4:]}"
                    if len(api_key) > 12 else "***"
                )
                lines.append(f"🔑 密钥: {masked}")

        lines.extend([
            f"🏷️ 类型: {'免费版' if is_free else '付费版'}",
            f"💰 账户总充值: {total_credits_str}",
            f"📈 账户已消费: {total_usage_str}",
            f"✨ 实际可用余额: {balance_str}",
        ])
        if progress:
            lines.append(f"🔋 消费进度: {progress}")

        # 如果存在单 Key 预算上限且与总充值不同，单独列出提示
        budget = k_info.get("effective_budget", {})
        key_limit = budget.get("limit_usd")
        if key_limit is not None and key_limit != total_credits:
            key_spend = budget.get("spend_usd", 0.0)
            lines.append("──────────────")
            lines.append(
                f"🛡️ Key 预算上限: ${key_limit:.2f} "
                f"(当前Key已用: ${key_spend:.4f})"
            )

        lines.append("──────────────")
        lines.append(f"📅 今日消费: ${usage_daily:.4f}")
        lines.append(f"📅 本周消费: ${usage_weekly:.4f}")
        lines.append(f"📅 本月消费: ${usage_monthly:.4f}")
        if expires_at:
            lines.append(f"⏰ 过期时间: {expires_at[:10]}")
        return "\n".join(lines)

    # ──────────────────────────────────────
    #  Tavily 相关方法
    # ──────────────────────────────────────

    def _get_api_keys(
        self, event: AstrMessageEvent | None = None,
    ) -> list[str]:
        """插件配置 → 当前会话配置接口 → 环境变量。"""
        keys = self._normalize_keys(self.config.get("api_keys", []))
        if keys:
            return keys

        data = self._get_framework_config(event)
        settings = data.get("provider_settings", {})
        keys = self._normalize_keys(
            settings.get("websearch_tavily_key", [])
            if isinstance(settings, dict) else []
        )
        if not keys:
            keys = self._normalize_keys(data.get("websearch_tavily_key", []))
        return keys or self._normalize_keys(
            os.environ.get("TAVILY_API_KEY", "")
        )

    async def _fetch_quota(self, api_key: str) -> dict | None:
        url = "https://api.tavily.com/usage"
        headers = {
            "Authorization": f"Bearer {api_key}"
        }
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=headers) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    else:
                        logger.error(
                            f"[tavily_quota] Tavily 查询失败，HTTP {resp.status}。"
                        )
                        return None
        except Exception:
            logger.error("[tavily_quota] 请求 Tavily API 出现异常。")
            return None

    def _render_progress_bar(
        self, usage: int, limit: int, width: int = 10,
    ) -> str:
        if not limit or limit <= 0:
            return ""
        ratio = min(max(usage / limit, 0.0), 1.0)
        filled = int(round(ratio * width))
        empty = width - filled
        pct = ratio * 100
        return f"[{'■' * filled}{'□' * empty}] {pct:.1f}%"

    def _format_quota_message(
        self, api_key: str, data: dict, show_key: bool = False,
    ) -> str:
        acc = data.get("account", {})
        plan = acc.get("current_plan", "未知套餐")
        limit = acc.get("plan_limit")
        usage = acc.get("plan_usage", 0)

        limit_str = f"{limit:,} 次" if limit is not None else "无限制"
        usage_str = f"{usage:,} 次"
        remaining_str = (
            f"{(limit - usage):,} 次" if limit is not None else "无限制"
        )

        progress = self._render_progress_bar(usage, limit) if limit else ""

        search_cnt = acc.get("search_usage", 0)
        extract_cnt = acc.get("extract_usage", 0)
        crawl_cnt = acc.get("crawl_usage", 0)

        lines = [
            "📊 【Tavily 额度使用情况】",
        ]
        if show_key:
            masked_key = (
                f"{api_key[:4]}...{api_key[-2:]}"
                if len(api_key) > 8 else "***"
            )
            lines.append(f"🔑 密钥: {masked_key}")
        lines.extend([
            f"🏷️ 套餐: {plan}",
            f"🎯 配额: {limit_str}",
            f"📈 已用: {usage_str}",
            f"✨ 剩余: {remaining_str}",
        ])
        if progress:
            lines.append(f"🔋 进度: {progress}")
        lines.append("──────────────")
        lines.append(f"🔍 搜索: {search_cnt:,} 次")
        lines.append(f"📑 抽取: {extract_cnt:,} 次")
        lines.append(f"🕸️ 抓取: {crawl_cnt:,} 次")

        return "\n".join(lines)

    @filter.command("tavily", alias={"tavily额度", "查额度"})
    async def query_quota_command(self, event: AstrMessageEvent):
        '''查询 Tavily 搜索引擎 API 的额度与使用情况'''
        if not self._can_query(event):
            yield event.plain_result("⚠️ 当前仅管理员可查询 Tavily 额度。")
            return

        keys = self._get_api_keys(event)
        if not keys:
            yield event.plain_result("❌ 未检测到 Tavily API Key，请在插件配置或全局配置中设置。")
            return

        for key in keys:
            data = await self._fetch_quota(key)
            if not data:
                yield event.plain_result("❌ Tavily Key 额度查询失败，请检查网络或 Key 状态。")
                continue

            msg = self._format_quota_message(key, data, show_key=False)
            yield event.plain_result(msg)

    @filter.command("openrouter", alias={"open", "openrouter额度"})
    async def query_openrouter_command(self, event: AstrMessageEvent):
        '''查询 OpenRouter API 的额度与使用情况'''
        if not self._can_query(event):
            yield event.plain_result("⚠️ 当前仅管理员可查询 OpenRouter 额度。")
            return

        keys = self._get_openrouter_keys(event)
        if not keys:
            yield event.plain_result(
                "❌ 未检测到 OpenRouter API Key，请在插件配置或全局配置中设置。"
            )
            return

        for key in keys:
            data = await self._fetch_openrouter_quota(key)
            if not data:
                yield event.plain_result(
                    "❌ OpenRouter Key 额度查询失败，请检查网络或 Key 状态。"
                )
                continue
            msg = self._format_openrouter_message(key, data, show_key=False)
            yield event.plain_result(msg)

    @filter.llm_tool(name="query_tavily_quota")
    async def query_tavily_quota_tool(self, event: AstrMessageEvent) -> str:
        '''查询当前 Tavily 搜索引擎的额度、已使用次数和剩余可用次数。
        当用户询问 tavily 额度、剩余搜索次数或 API 用量时调用。
        '''
        if not self.config.get("enable_llm_tool", True):
            return "额度查询工具已禁用。"
        if not self._can_query(event):
            return "当前仅管理员可查询 API 额度。"

        keys = self._get_api_keys(event)
        if not keys:
            return "未检测到配置的 Tavily API Key。"

        results = []
        for key in keys:
            data = await self._fetch_quota(key)
            if data:
                msg = self._format_quota_message(key, data, show_key=False)
                results.append(msg)
            else:
                results.append("Tavily Key 额度查询失败。")

        return "\n\n".join(results)

    def _get_deepseek_keys(
        self, event: AstrMessageEvent | None = None,
    ) -> list[str]:
        """插件配置 → 当前会话启用的 DeepSeek Provider → 环境变量。"""
        keys = self._normalize_keys(self.config.get("deepseek_api_keys", []))
        if keys:
            return keys
        data = self._get_framework_config(event)
        sources = data.get("provider_sources", [])
        for source in sources if isinstance(sources, list) else []:
            if not isinstance(source, dict) or not source.get("enable", True):
                continue
            if any(
                "deepseek" in str(source.get(field, "")).lower()
                for field in ("provider", "type")
            ):
                keys.extend(self._normalize_keys(source.get("key", [])))
        return list(dict.fromkeys(keys)) or self._normalize_keys(
            os.environ.get("DEEPSEEK_API_KEY", "")
        )

    def _get_github_tokens(
        self, event: AstrMessageEvent | None = None,
    ) -> list[str]:
        """兼容两种插件配置名称；不读取其他插件或 Git 凭据。"""
        for field in ("github_tokens", "github_personal_access_tokens"):
            tokens = self._normalize_keys(self.config.get(field, []))
            if tokens:
                return tokens
        return self._normalize_keys(
            os.environ.get("GITHUB_TOKEN", "")
        ) or self._normalize_keys(os.environ.get("GH_TOKEN", ""))

    @staticmethod
    def _mask_key(key: str) -> str:
        return f"{key[:4]}…{key[-4:]}" if len(key) > 12 else "***"

    @staticmethod
    def _amount(value: object) -> str:
        """仅格式化有限数值，不把接口中的任意文字输出到聊天。"""
        if value is None or isinstance(value, bool):
            return "未知"
        try:
            number = Decimal(str(value))
            return f"{number:.2f}" if number.is_finite() else "未知"
        except (InvalidOperation, ValueError, TypeError):
            return "未知"

    @staticmethod
    async def _get_json(
        session: aiohttp.ClientSession,
        url: str,
        headers: dict[str, str],
        service: str,
        optional: bool = False,
    ) -> dict | None:
        """新服务共用的只读请求；不记录响应正文或凭据。"""
        try:
            async with session.get(
                url, headers=headers, allow_redirects=False,
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    if isinstance(data, dict):
                        return data
                log = logger.debug if optional else logger.warning
                log(f"[tavily_quota] {service} 查询不可用，HTTP {response.status}。")
        except Exception:
            log = logger.debug if optional else logger.warning
            log(f"[tavily_quota] {service} 查询失败，可能是网络、超时或响应格式异常。")
        return None

    async def _fetch_deepseek_quota(self, api_key: str) -> dict | None:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        }
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10),
            ) as session:
                data = await self._get_json(
                    session, "https://api.deepseek.com/user/balance",
                    headers, "DeepSeek",
                )
        except Exception:
            logger.warning("[tavily_quota] DeepSeek 请求初始化失败。")
            return None
        if data is not None and isinstance(data.get("balance_infos"), list):
            return data
        return None

    def _format_deepseek_message(self, api_key: str, data: dict) -> str:
        available = data.get("is_available")
        status = (
            "可用" if available is True
            else "不可用" if available is False else "未知"
        )
        lines = [
            "💰 【DeepSeek 余额使用情况】",
            f"🔑 Key: {self._mask_key(api_key)}",
            f"✅ 可用状态: {status}",
        ]
        infos = [
            row for row in data.get("balance_infos", [])
            if isinstance(row, dict)
        ]
        if not infos:
            lines.append("未返回余额明细。")
        for row in infos:
            currency = row.get("currency")
            currency = currency if currency in ("CNY", "USD") else "未知币种"
            lines.extend([
                f"💱 币种: {currency}",
                f"✨ 总可用余额: {self._amount(row.get('total_balance'))}",
                f"💳 充值余额: {self._amount(row.get('topped_up_balance'))}",
                f"🎁 赠送体验金: {self._amount(row.get('granted_balance'))}",
            ])
        return "\n".join(lines)

    async def _fetch_github_quota(self, token: str) -> dict | None:
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "AstrBot-QuotaPlugin",
        }
        result = {}
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10),
            ) as session:
                rate = await self._get_json(
                    session, "https://api.github.com/rate_limit",
                    headers, "GitHub Rate Limit",
                )
                resources = rate.get("resources") if rate else None
                if not isinstance(resources, dict):
                    return None
                core = resources.get("core")
                if not isinstance(core, dict):
                    return None
                result["core"] = core
                # 旧 Actions 计费接口已退役；仅作兼容探测。
                # 附加查询失败不能丢弃已获得的 Rate Limit。
                user = await self._get_json(
                    session, "https://api.github.com/user",
                    headers, "GitHub 用户", optional=True,
                )
                login = user.get("login") if user else None
                if isinstance(login, str) and re.fullmatch(
                    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", login,
                ):
                    actions = await self._get_json(
                        session,
                        "https://api.github.com/users/"
                        f"{login}/settings/billing/actions",
                        headers, "GitHub Actions", optional=True,
                    )
                    if actions and self._amount(
                        actions.get("total_minutes_used")
                    ) != "未知":
                        result["actions"] = actions
        except Exception:
            logger.warning("[tavily_quota] GitHub 请求未完整完成，保留已有查询结果。")
        return result or None

    def _format_github_message(self, token: str, data: dict) -> str:
        core = data.get("core", {})

        def count(value: object) -> str:
            if type(value) is int and value >= 0:
                return f"{value:,}"
            return "未知"

        reset = "未知"
        stamp = core.get("reset")
        if type(stamp) in (int, float):
            try:
                reset = datetime.fromtimestamp(stamp).astimezone().strftime(
                    "%Y-%m-%d %H:%M:%S %Z（本地时间）",
                )
            except (ValueError, OverflowError, OSError):
                pass
        lines = [
            "🐙 【GitHub 额度使用情况】",
            f"🔑 Token: {self._mask_key(token)}",
            "📊 Core API 剩余 / 总配额: "
            f"{count(core.get('remaining'))} / {count(core.get('limit'))}",
            f"⏰ 重置时间: {reset}",
        ]
        actions = data.get("actions")
        if isinstance(actions, dict):
            included = actions.get("included_minutes", 2000)
            lines.append(
                "⚙️ Actions 已用 / 包含分钟: "
                f"{self._amount(actions.get('total_minutes_used'))} / "
                f"{self._amount(included)}"
            )
            if "included_minutes" not in actions:
                lines.append("包含分钟数未返回，2000 为兼容默认值，并非已核实套餐额度。")
        return "\n".join(lines)

    async def _query_services(
        self, event: AstrMessageEvent, service: str = "",
    ) -> str:
        if not self._can_query(event):
            return "⚠️ 当前仅管理员可查询 API 额度。"
        services = {
            "tavily": (
                self._get_api_keys, self._fetch_quota,
                self._format_quota_message,
            ),
            "openrouter": (
                self._get_openrouter_keys, self._fetch_openrouter_quota,
                self._format_openrouter_message,
            ),
            "deepseek": (
                self._get_deepseek_keys, self._fetch_deepseek_quota,
                self._format_deepseek_message,
            ),
            "github": (
                self._get_github_tokens, self._fetch_github_quota,
                self._format_github_message,
            ),
        }
        service = service.strip().lower()
        if service and service not in services:
            return (
                "用法：/quota [tavily|openrouter|deepseek|github]；"
                "不指定服务时查询全部已配置服务。"
            )
        blocks = []
        for name in ([service] if service else services):
            get_keys, fetch, formatter = services[name]
            keys = get_keys(event)
            if not keys:
                if service:
                    return f"❌ 未检测到 {name} 配置的 API Key / Token。"
                continue
            for index, key in enumerate(keys, 1):
                title = f"### {name} · 凭据 {index}"
                try:
                    data = await fetch(key)
                    message = (
                        formatter(key, data) if data
                        else "❌ 查询失败，请检查网络、凭据或接口状态。"
                    )
                except Exception:
                    logger.warning(f"[tavily_quota] {name} 查询或格式化失败，继续其他服务。")
                    message = "❌ 查询失败，请检查网络、凭据或接口状态。"
                blocks.append(title + "\n" + message)
        if not blocks:
            return "未检测到任何服务的 API Key / Token，请先配置。"
        return "## 📊 API 配额汇总\n\n" + "\n\n---\n\n".join(blocks)

    @filter.command("quota", alias={"配额"})
    async def query_all_command(
        self, event: AstrMessageEvent, service: str = "",
    ):
        """查询全部已配置服务，或使用 /quota 服务名 查询指定服务。"""
        yield event.plain_result(await self._query_services(event, service))

    @filter.command("deepseek", alias={"deep"})
    async def query_deepseek_command(self, event: AstrMessageEvent):
        """查询 DeepSeek 余额。"""
        yield event.plain_result(await self._query_services(event, "deepseek"))

    @filter.command("github", alias={"git"})
    async def query_github_command(self, event: AstrMessageEvent):
        """查询 GitHub API Rate Limit 及可获取的 Actions 配额。"""
        yield event.plain_result(await self._query_services(event, "github"))

    async def _query_new_tool(
        self, event: AstrMessageEvent, service: str,
    ) -> str:
        if not self.config.get("enable_llm_tool", True):
            return "额度查询工具已禁用。"
        return await self._query_services(event, service)

    @filter.llm_tool(name="query_deepseek_quota")
    async def query_deepseek_quota_tool(self, event: AstrMessageEvent) -> str:
        """查询 DeepSeek 的可用状态、币种、总余额、充值余额和赠送体验金。"""
        return await self._query_new_tool(event, "deepseek")

    @filter.llm_tool(name="query_github_quota")
    async def query_github_quota_tool(self, event: AstrMessageEvent) -> str:
        """查询 GitHub Core API 配额、重置时间及可获取的 Actions 分钟配额。"""
        return await self._query_new_tool(event, "github")

    @filter.llm_tool(name="query_openrouter_quota")
    async def query_openrouter_quota_tool(
        self, event: AstrMessageEvent,
    ) -> str:
        '''查询当前 OpenRouter API 的额度、已消费金额和剩余额度。
        当用户询问 OpenRouter 额度、剩余费用或 API 用量时调用。
        '''
        if not self.config.get("enable_llm_tool", True):
            return "额度查询工具已禁用。"
        if not self._can_query(event):
            return "当前仅管理员可查询 API 额度。"

        keys = self._get_openrouter_keys(event)
        if not keys:
            return "未检测到配置的 OpenRouter API Key。"

        results = []
        for key in keys:
            data = await self._fetch_openrouter_quota(key)
            if data:
                msg = self._format_openrouter_message(
                    key, data, show_key=False,
                )
                results.append(msg)
            else:
                results.append("OpenRouter Key 额度查询失败。")

        return "\n\n".join(results)
