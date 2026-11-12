import os
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

import aiohttp
from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.core import AstrBotConfig

import sys
from pathlib import Path

_plugin_dir = str(Path(__file__).resolve().parent)
if _plugin_dir not in sys.path:
    sys.path.insert(0, _plugin_dir)

try:
    from .alert_manager import QuotaAlertManager
    from .card_renderer import ServiceCardData, render_quota_dashboard
except ImportError:
    # 兼容测试直接导入
    from alert_manager import QuotaAlertManager
    from card_renderer import ServiceCardData, render_quota_dashboard


@register(
    "tavily_quota",
    "Tetra",
    "Tavily、OpenRouter、DeepSeek、GitHub、硅基流动、Kimi、智谱与 Serper 额度查询插件",
    "1.4.0",
)
class TavilyQuotaPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.alert_manager = QuotaAlertManager(self, context, config)

    # ──────────────────────────────────────
    #  生命周期与权限
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
            "query_siliconflow_quota", "query_moonshot_quota",
            "query_zhipu_quota", "query_serper_quota",
        ):
            await set_enabled(name)

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self):
        # 框架启动时恢复工具状态，并启动后台预警巡检
        await self.initialize()
        self.alert_manager.start()

    async def terminate(self):
        """插件卸载时停止后台任务。"""
        self.alert_manager.stop()

    def _can_query(self, event: AstrMessageEvent | None = None) -> bool:
        if not self.config.get("admin_only", False):
            return True
        if event is None:
            return True
        try:
            return bool(event.is_admin())
        except Exception:
            return True

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
            logger.warning("[api_quota] 无法通过配置接口获取当前会话配置。")
            return {}

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
                log(f"[api_quota] {service} 查询不可用，HTTP {response.status}。")
        except Exception:
            log = logger.debug if optional else logger.warning
            log(f"[api_quota] {service} 查询失败，可能是网络、超时或响应格式异常。")
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

    # ──────────────────────────────────────
    #  卡片渲染辅助与适配
    # ──────────────────────────────────────

    def _should_render_card(self, event: AstrMessageEvent | None = None, force_img: bool = False, force_text: bool = False) -> bool:
        if force_text:
            return False
        if event is not None and not hasattr(event, "image_result"):
            return False
        if force_img:
            return True
        return bool(self.config.get("render_image", True))

    def _render_card_safe(self, cards: list[ServiceCardData], title: str = "API 配额看板") -> str | None:
        try:
            return render_quota_dashboard(cards, title=title)
        except Exception as e:
            logger.warning(f"[api_quota] 生成图片看板失败，将降级文本: {e}")
            return None

    def _card_tavily(self, key: str, data: dict | None) -> ServiceCardData:
        mask = self._mask_key(key)
        if not data:
            return ServiceCardData(
                service="tavily",
                key_masked=mask,
                status="error",
                primary_metric="查询失败",
                primary_label="剩余配额",
                details=["网络超时或 Key 无效"],
            )
        acc = data.get("account", {})
        plan = acc.get("current_plan", "标准套餐")
        limit = acc.get("plan_limit")
        usage = acc.get("plan_usage", 0)
        rem = (limit - usage) if limit is not None else None
        search_cnt = acc.get("search_usage", 0)

        thresholds = self.config.get("alert_thresholds", {}) or {}
        threshold = int(thresholds.get("tavily_min_remaining", 100))
        status = "warn" if (rem is not None and rem <= threshold) else "ok"

        primary_metric = f"{rem:,} 次" if rem is not None else f"{usage:,} 次"
        primary_label = "剩余配额" if rem is not None else "已用次数"
        progress = (usage / limit) if limit and limit > 0 else None

        details = [f"套餐: {plan}", f"已用: {usage:,} 次", f"搜索: {search_cnt:,} 次"]
        return ServiceCardData(
            service="tavily",
            key_masked=mask,
            status=status,
            primary_metric=primary_metric,
            primary_label=primary_label,
            details=details,
            progress_ratio=progress,
        )

    def _card_openrouter(self, key: str, data: dict | None) -> ServiceCardData:
        mask = self._mask_key(key)
        if not data:
            return ServiceCardData(
                service="openrouter",
                key_masked=mask,
                status="error",
                primary_metric="查询失败",
                primary_label="可用余额",
                details=["请求异常或 Key 无效"],
            )
        k_info = data.get("key_info", {})
        credits_info = data.get("credits", {})
        total_credits = credits_info.get("total_credits")
        total_usage = credits_info.get("total_usage", 0.0)

        bal = 0.0
        if total_credits is not None:
            bal = max(0.0, float(total_credits) - float(total_usage))
            metric = f"${bal:.4f}"
            lbl = "可用余额"
            ratio = (total_usage / total_credits) if total_credits > 0 else None
        else:
            budget = k_info.get("effective_budget", {})
            rem = budget.get("remaining_usd")
            metric = f"${rem:.4f}" if rem is not None else "无限制"
            lbl = "Key 剩余额度"
            ratio = None

        thresholds = self.config.get("alert_thresholds", {}) or {}
        threshold = float(thresholds.get("openrouter_min_balance", 1.0))
        status = "warn" if bal <= threshold else "ok"

        is_free = k_info.get("is_free_tier", False)
        usage_daily = k_info.get("usage_daily", 0.0)
        details = [
            f"类型: {'免费版' if is_free else '付费版'}",
            f"总充值: ${total_credits:.2f}" if total_credits is not None else "额度无限制",
            f"今日消费: ${usage_daily:.4f}",
        ]
        return ServiceCardData(
            service="openrouter",
            key_masked=mask,
            status=status,
            primary_metric=metric,
            primary_label=lbl,
            details=details,
            progress_ratio=ratio,
        )

    def _card_deepseek(self, key: str, data: dict | None) -> ServiceCardData:
        mask = self._mask_key(key)
        if not data:
            return ServiceCardData(
                service="deepseek",
                key_masked=mask,
                status="error",
                primary_metric="查询失败",
                primary_label="可用余额",
                details=["凭据过期或接口异常"],
            )
        available = data.get("is_available")
        infos = [row for row in data.get("balance_infos", []) if isinstance(row, dict)]
        total = 0.0
        topped = 0.0
        granted = 0.0
        currency = "CNY"
        if infos:
            row = infos[0]
            try:
                total = float(row.get("total_balance", 0.0))
                topped = float(row.get("topped_up_balance", 0.0))
                granted = float(row.get("granted_balance", 0.0))
                currency = row.get("currency", "CNY")
            except Exception:
                pass
        symbol = "¥" if currency == "CNY" else "$"

        thresholds = self.config.get("alert_thresholds", {}) or {}
        threshold = float(thresholds.get("deepseek_min_balance", 5.0))
        if available is False:
            status = "danger"
        elif total <= threshold:
            status = "warn"
        else:
            status = "ok"

        details = [
            f"充值: {symbol}{topped:.2f}",
            f"赠送金: {symbol}{granted:.2f}",
            f"状态: {'可用' if available else '异常'}",
        ]
        return ServiceCardData(
            service="deepseek",
            key_masked=mask,
            status=status,
            primary_metric=f"{symbol}{total:.2f}",
            primary_label="可用余额",
            details=details,
            progress_ratio=None,
        )

    def _card_github(self, token: str, data: dict | None) -> ServiceCardData:
        mask = self._mask_key(token)
        if not data:
            return ServiceCardData(
                service="github",
                key_masked=mask,
                status="error",
                primary_metric="查询失败",
                primary_label="Core API 剩余",
                details=["Token 无效或权限不足"],
            )
        core = data.get("core", {})
        rem = core.get("remaining", 0)
        limit = core.get("limit", 5000)
        reset_stamp = core.get("reset")
        reset_str = "未知"
        if type(reset_stamp) in (int, float):
            try:
                reset_str = datetime.fromtimestamp(reset_stamp).astimezone().strftime("%H:%M:%S 重置")
            except Exception:
                pass

        thresholds = self.config.get("alert_thresholds", {}) or {}
        threshold = int(thresholds.get("github_min_remaining", 500))
        status = "warn" if (rem is not None and rem <= threshold) else "ok"
        ratio = (1.0 - (rem / limit)) if limit and limit > 0 else None

        actions = data.get("actions")
        details = [f"重置时间: {reset_str}"]
        if isinstance(actions, dict):
            used = actions.get("total_minutes_used", 0)
            inc = actions.get("included_minutes", 2000)
            details.append(f"Actions: {used}/{inc}分")

        return ServiceCardData(
            service="github",
            key_masked=mask,
            status=status,
            primary_metric=f"{rem:,} / {limit:,}",
            primary_label="Core API 剩余/配额",
            details=details,
            progress_ratio=ratio,
        )

    def _card_siliconflow(self, key: str, data: dict | None) -> ServiceCardData:
        mask = self._mask_key(key)
        if not data:
            return ServiceCardData(
                service="siliconflow",
                key_masked=mask,
                status="error",
                primary_metric="查询失败",
                primary_label="可用余额",
                details=["凭据过期或接口异常"],
            )
        try:
            total = float(data.get("totalBalance", 0.0))
            charge = float(data.get("chargeBalance", 0.0))
            grant = float(data.get("balance", 0.0))
        except Exception:
            total = charge = grant = 0.0
        status_raw = str(data.get("status", "normal")).lower()

        thresholds = self.config.get("alert_thresholds", {}) or {}
        threshold = float(thresholds.get("siliconflow_min_balance", 5.0))
        if status_raw not in ("normal", "ok", "active"):
            status = "danger"
        elif total <= threshold:
            status = "warn"
        else:
            status = "ok"

        details = [
            f"充值: ¥{charge:.2f} · 赠送: ¥{grant:.2f}",
            f"账户状态: {'正常' if status_raw in ('normal', 'ok', 'active') else status_raw}",
        ]
        return ServiceCardData(
            service="siliconflow",
            key_masked=mask,
            status=status,
            primary_metric=f"¥{total:.2f}",
            primary_label="可用总余额",
            details=details,
            progress_ratio=None,
        )

    def _card_moonshot(self, key: str, data: dict | None) -> ServiceCardData:
        mask = self._mask_key(key)
        if not data:
            return ServiceCardData(
                service="moonshot",
                key_masked=mask,
                status="error",
                primary_metric="查询失败",
                primary_label="可用余额",
                details=["凭据过期或接口异常"],
            )
        try:
            avail = float(data.get("available_balance", 0.0))
            cash = float(data.get("cash_balance", 0.0))
            voucher = float(data.get("voucher_balance", 0.0))
        except Exception:
            avail = cash = voucher = 0.0

        thresholds = self.config.get("alert_thresholds", {}) or {}
        threshold = float(thresholds.get("moonshot_min_balance", 5.0))
        status = "warn" if avail <= threshold else "ok"

        details = [
            f"现金余额: ¥{cash:.2f}",
            f"代金券余额: ¥{voucher:.2f}",
        ]
        return ServiceCardData(
            service="moonshot",
            key_masked=mask,
            status=status,
            primary_metric=f"¥{avail:.2f}",
            primary_label="可用余额",
            details=details,
            progress_ratio=None,
        )

    def _card_zhipu(self, key: str, data: dict | None) -> ServiceCardData:
        mask = self._mask_key(key)
        if not data:
            return ServiceCardData(
                service="zhipu",
                key_masked=mask,
                status="error",
                primary_metric="查询失败",
                primary_label="状态",
                details=["凭据过期或接口异常"],
            )
        coding_plan = data.get("coding_plan")
        account = data.get("account")

        thresholds = self.config.get("alert_thresholds", {}) or {}
        threshold = float(thresholds.get("zhipu_min_balance", 5.0))

        status = "ok"
        ratio = None
        details = []

        if isinstance(account, dict) and any(k in account for k in ("availableBalance", "balance", "totalBalance")):
            try:
                avail = float(account.get("availableBalance") or account.get("balance") or 0.0)
            except Exception:
                avail = 0.0
            primary_metric = f"¥{avail:.2f}"
            primary_label = "现金余额"
            if avail <= threshold:
                status = "warn"
            if "totalBalance" in account:
                details.append(f"总资产: ¥{float(account.get('totalBalance', 0.0)):.2f}")
        elif isinstance(coding_plan, dict) and "limits" in coding_plan:
            level = coding_plan.get("level", "标准")
            primary_label = f"Coding Plan ({level})"
            limits = coding_plan.get("limits", [])
            primary_metric = "生效中"
            for lim in limits:
                lim_type = lim.get("type", "")
                if lim_type == "TOKENS_LIMIT" or "TOKEN" in lim_type:
                    rem = lim.get("remaining")
                    tot = lim.get("total")
                    pct = lim.get("percentage", 0)
                    if rem is not None and tot:
                        primary_metric = f"{rem:,} / {tot:,}"
                        ratio = float(pct) / 100.0 if pct else None
                    reset_ts = lim.get("nextResetTime")
                    if reset_ts:
                        try:
                            t_str = datetime.fromtimestamp(reset_ts / 1000).astimezone().strftime("%H:%M 重置")
                            details.append(f"5小时窗口重置: {t_str}")
                        except Exception:
                            pass
                    break
        else:
            primary_metric = "正常"
            primary_label = "服务状态"

        if not details:
            details.append("智谱开放平台 API 正常连接")

        return ServiceCardData(
            service="zhipu",
            key_masked=mask,
            status=status,
            primary_metric=primary_metric,
            primary_label=primary_label,
            details=details,
            progress_ratio=ratio,
        )

    def _card_serper(self, key: str, data: dict | None) -> ServiceCardData:
        mask = self._mask_key(key)
        if not data:
            return ServiceCardData(
                service="serper",
                key_masked=mask,
                status="error",
                primary_metric="查询失败",
                primary_label="剩余搜索额度",
                details=["API Key 无效或请求失败"],
            )
        try:
            credits = int(data.get("credits", 0))
        except Exception:
            credits = 0

        thresholds = self.config.get("alert_thresholds", {}) or {}
        threshold = int(thresholds.get("serper_min_credits", 100))
        status = "warn" if credits <= threshold else "ok"

        return ServiceCardData(
            service="serper",
            key_masked=mask,
            status=status,
            primary_metric=f"{credits:,} 次",
            primary_label="剩余搜索额度",
            details=["Google 搜索 API (Serper.dev)"],
            progress_ratio=None,
        )


    # ──────────────────────────────────────
    #  OpenRouter 相关方法
    # ──────────────────────────────────────

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
                                "[api_quota] OpenRouter Key 查询失败，"
                                f"HTTP {resp.status}。"
                            )
                except Exception:
                    logger.error("[api_quota] 请求 OpenRouter Key API 异常。")

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
                                "[api_quota] OpenRouter Credits 查询失败，"
                                f"HTTP {resp.status}。"
                            )
                except Exception:
                    logger.error(
                        "[api_quota] 请求 OpenRouter Credits API 异常。"
                    )

            if not result.get("key_info") and not result.get("credits"):
                return None
            return result
        except Exception:
            logger.error("[api_quota] 请求 OpenRouter API 出现异常。")
            return None

    def _format_openrouter_message(
        self, api_key: str, data: dict, show_key: bool = False,
    ) -> str:
        k_info = data.get("key_info", {})
        credits_info = data.get("credits", {})

        if not k_info and not credits_info and "data" in data:
            k_info = data.get("data", {})

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
                            f"[api_quota] Tavily 查询失败，HTTP {resp.status}。"
                        )
                        return None
        except Exception:
            logger.error("[api_quota] 请求 Tavily API 出现异常。")
            return None

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

    @filter.command("tavily", alias={"tav"})
    async def query_quota_command(self, event: AstrMessageEvent, arg: str = ""):
        '''查询 Tavily 搜索引擎 API 的额度与使用情况'''
        if not self._can_query(event):
            yield event.plain_result("⚠️ 当前仅管理员可查询 Tavily 额度。")
            return

        keys = self._get_api_keys(event)
        if not keys:
            yield event.plain_result("❌ 未检测到 Tavily API Key，请在插件配置或全局配置中设置。")
            return

        arg_str = str(arg).lower()
        force_text = "text" in arg_str
        force_img = ("img" in arg_str) or ("card" in arg_str) or ("pic" in arg_str)

        cards = []
        messages = []
        for key in keys:
            data = await self._fetch_quota(key)
            if not data:
                messages.append("❌ Tavily Key 额度查询失败，请检查网络或 Key 状态。")
                cards.append(self._card_tavily(key, None))
                continue

            cards.append(self._card_tavily(key, data))
            messages.append(self._format_quota_message(key, data, show_key=False))

        if self._should_render_card(event, force_img=force_img, force_text=force_text):
            img_path = self._render_card_safe(cards, title="Tavily 搜索额度看板")
            if img_path:
                yield event.image_result(img_path)
                return

        for msg in messages:
            yield event.plain_result(msg)

    @filter.command("openrouter", alias={"open", "openrouter额度"})
    async def query_openrouter_command(self, event: AstrMessageEvent, arg: str = ""):
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

        arg_str = str(arg).lower()
        force_text = "text" in arg_str
        force_img = ("img" in arg_str) or ("card" in arg_str) or ("pic" in arg_str)

        cards = []
        messages = []
        for key in keys:
            data = await self._fetch_openrouter_quota(key)
            if not data:
                messages.append(
                    "❌ OpenRouter Key 额度查询失败，请检查网络或 Key 状态。"
                )
                cards.append(self._card_openrouter(key, None))
                continue
            cards.append(self._card_openrouter(key, data))
            messages.append(self._format_openrouter_message(key, data, show_key=False))

        if self._should_render_card(event, force_img=force_img, force_text=force_text):
            img_path = self._render_card_safe(cards, title="OpenRouter 额度看板")
            if img_path:
                yield event.image_result(img_path)
                return

        for msg in messages:
            yield event.plain_result(msg)

    @filter.llm_tool(name="query_tavily_quota")
    async def query_tavily_quota_tool(self, event: AstrMessageEvent | None = None) -> str:
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

    # ──────────────────────────────────────
    #  DeepSeek 相关方法
    # ──────────────────────────────────────

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
            logger.warning("[api_quota] DeepSeek 请求初始化失败。")
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

    # ──────────────────────────────────────
    #  GitHub 相关方法
    # ──────────────────────────────────────

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
            logger.warning("[api_quota] GitHub 请求未完整完成，保留已有查询结果。")
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


    # ──────────────────────────────────────
    #  SiliconFlow (硅基流动) 相关方法
    # ──────────────────────────────────────

    def _get_siliconflow_keys(
        self, event: AstrMessageEvent | None = None,
    ) -> list[str]:
        keys = self._normalize_keys(self.config.get("siliconflow_api_keys", []))
        if keys:
            return keys
        data = self._get_framework_config(event)
        sources = data.get("provider_sources", [])
        for source in sources if isinstance(sources, list) else []:
            if not isinstance(source, dict) or not source.get("enable", True):
                continue
            if any(
                "silicon" in str(source.get(field, "")).lower()
                for field in ("provider", "type")
            ):
                keys.extend(self._normalize_keys(source.get("key", [])))
        return list(dict.fromkeys(keys)) or self._normalize_keys(
            os.environ.get("SILICONFLOW_API_KEY", "") or os.environ.get("SILICON_API_KEY", "")
        )

    async def _fetch_siliconflow_quota(self, api_key: str) -> dict | None:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        }
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10),
            ) as session:
                # 国际站接口；国区（api.siliconflow.cn）的 /v1/user/info 已于 2026 年下线（HTTP 410 / code 20092），
                # 官方未提供替代余额接口，故余额查询统一走国际站，国区 Key 不支持。
                data = await self._get_json(
                    session, "https://api.siliconflow.com/v1/user/info",
                    headers, "SiliconFlow",
                )
        except Exception:
            logger.warning("[api_quota] SiliconFlow 请求初始化失败。")
            return None
        if isinstance(data, dict):
            sub = data.get("data")
            if isinstance(sub, dict):
                return sub
            if "totalBalance" in data or "balance" in data:
                return data
        return None

    def _format_siliconflow_message(self, api_key: str, data: dict) -> str:
        try:
            total = float(data.get("totalBalance", 0.0))
            charge = float(data.get("chargeBalance", 0.0))
            grant = float(data.get("balance", 0.0))
        except Exception:
            total = charge = grant = 0.0
        status_raw = str(data.get("status", "normal"))
        lines = [
            "⚡ 【SiliconFlow (硅基流动) 余额】",
            f"🔑 Key: {self._mask_key(api_key)}",
            f"💰 可用总余额: ¥{total:.2f}",
            f"💳 充值余额: ¥{charge:.2f}",
            f"🎁 赠送体验金: ¥{grant:.2f}",
            f"📌 账户状态: {status_raw}",
        ]
        return "\n".join(lines)

    # ──────────────────────────────────────
    #  Moonshot AI (Kimi) 相关方法
    # ──────────────────────────────────────

    def _get_moonshot_keys(
        self, event: AstrMessageEvent | None = None,
    ) -> list[str]:
        keys = self._normalize_keys(self.config.get("moonshot_api_keys", []))
        if keys:
            return keys
        data = self._get_framework_config(event)
        sources = data.get("provider_sources", [])
        for source in sources if isinstance(sources, list) else []:
            if not isinstance(source, dict) or not source.get("enable", True):
                continue
            if any(
                any(k in str(source.get(field, "")).lower() for k in ("moonshot", "kimi"))
                for field in ("provider", "type")
            ):
                keys.extend(self._normalize_keys(source.get("key", [])))
        return list(dict.fromkeys(keys)) or self._normalize_keys(
            os.environ.get("MOONSHOT_API_KEY", "") or os.environ.get("KIMI_API_KEY", "")
        )

    async def _fetch_moonshot_quota(self, api_key: str) -> dict | None:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        }
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10),
            ) as session:
                data = await self._get_json(
                    session, "https://api.moonshot.cn/v1/users/me/balance",
                    headers, "Moonshot",
                )
        except Exception:
            logger.warning("[api_quota] Moonshot 请求初始化失败。")
            return None
        if isinstance(data, dict):
            sub = data.get("data")
            if isinstance(sub, dict):
                return sub
            if "available_balance" in data:
                return data
        return None

    def _format_moonshot_message(self, api_key: str, data: dict) -> str:
        try:
            avail = float(data.get("available_balance", 0.0))
            cash = float(data.get("cash_balance", 0.0))
            voucher = float(data.get("voucher_balance", 0.0))
        except Exception:
            avail = cash = voucher = 0.0
        lines = [
            "🌙 【Moonshot AI (Kimi) 余额】",
            f"🔑 Key: {self._mask_key(api_key)}",
            f"💰 可用余额: ¥{avail:.2f}",
            f"💵 充值现金: ¥{cash:.2f}",
            f"🎫 优惠券/代金券: ¥{voucher:.2f}",
        ]
        return "\n".join(lines)

    # ──────────────────────────────────────
    #  Zhipu AI (GLM) 相关方法
    # ──────────────────────────────────────

    def _get_zhipu_keys(
        self, event: AstrMessageEvent | None = None,
    ) -> list[str]:
        keys = self._normalize_keys(self.config.get("zhipu_api_keys", []))
        if keys:
            return keys
        data = self._get_framework_config(event)
        sources = data.get("provider_sources", [])
        for source in sources if isinstance(sources, list) else []:
            if not isinstance(source, dict) or not source.get("enable", True):
                continue
            if any(
                any(k in str(source.get(field, "")).lower() for k in ("zhipu", "glm", "bigmodel"))
                for field in ("provider", "type")
            ):
                keys.extend(self._normalize_keys(source.get("key", [])))
        return list(dict.fromkeys(keys)) or self._normalize_keys(
            os.environ.get("ZHIPUAI_API_KEY", "") or os.environ.get("ZHIPU_API_KEY", "") or os.environ.get("GLM_API_KEY", "")
        )

    async def _fetch_zhipu_quota(self, api_key: str) -> dict | None:
        auth_header = api_key if api_key.startswith("Bearer ") else api_key
        headers = {
            "Authorization": auth_header,
            "Accept": "application/json",
        }
        result = {}
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10),
            ) as session:
                try:
                    quota = await self._get_json(
                        session, "https://open.bigmodel.cn/api/monitor/usage/quota/limit",
                        headers, "Zhipu Quota", optional=True,
                    )
                    if isinstance(quota, dict) and quota.get("code") == 200:
                        result["coding_plan"] = quota.get("data", {})
                except Exception:
                    pass

                try:
                    account = await self._get_json(
                        session, "https://open.bigmodel.cn/api/biz/account/query-customer-account-report",
                        headers, "Zhipu Account", optional=True,
                    )
                    if isinstance(account, dict) and account.get("code") == 200:
                        result["account"] = account.get("data", {})
                except Exception:
                    pass
        except Exception:
            logger.warning("[api_quota] Zhipu 请求初始化失败。")
            return None
        return result or None

    def _format_zhipu_message(self, api_key: str, data: dict) -> str:
        lines = [
            "✨ 【Zhipu AI (智谱/GLM) 配额与余额】",
            f"🔑 Key: {self._mask_key(api_key)}",
        ]
        account = data.get("account")
        if isinstance(account, dict):
            avail = account.get("availableBalance") or account.get("balance")
            if avail is not None:
                lines.append(f"💰 现金余额: ¥{float(avail):.2f}")
            if "totalBalance" in account:
                lines.append(f"📊 总资产: ¥{float(account.get('totalBalance', 0.0)):.2f}")
        coding = data.get("coding_plan")
        if isinstance(coding, dict):
            level = coding.get("level", "标准")
            lines.append(f"📦 订阅套餐: {level}")
            for lim in coding.get("limits", []):
                lim_type = lim.get("type", "")
                if "TOKEN" in lim_type:
                    rem = lim.get("remaining")
                    tot = lim.get("total")
                    pct = lim.get("percentage")
                    if rem is not None and tot is not None:
                        lines.append(f"⏳ 5小时 Token 窗口: {rem:,} / {tot:,} ({pct}%)")
        return "\n".join(lines)

    # ──────────────────────────────────────
    #  Serper (Google Search) 相关方法
    # ──────────────────────────────────────

    def _get_serper_keys(
        self, event: AstrMessageEvent | None = None,
    ) -> list[str]:
        keys = self._normalize_keys(self.config.get("serper_api_keys", []))
        if keys:
            return keys
        data = self._get_framework_config(event)
        for k in ("websearch_serper_key", "serper_api_key", "serper_key"):
            val = data.get(k)
            if val:
                keys.extend(self._normalize_keys(val))
        return list(dict.fromkeys(keys)) or self._normalize_keys(
            os.environ.get("SERPER_API_KEY", "")
        )

    async def _fetch_serper_quota(self, api_key: str) -> dict | None:
        headers = {
            "X-API-KEY": api_key,
            "Accept": "application/json",
        }
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10),
            ) as session:
                data = await self._get_json(
                    session, "https://google.serper.dev/credits",
                    headers, "Serper",
                )
        except Exception:
            logger.warning("[api_quota] Serper 请求初始化失败。")
            return None
        if isinstance(data, dict) and "credits" in data:
            return data
        return None

    def _format_serper_message(self, api_key: str, data: dict) -> str:
        try:
            credits = int(data.get("credits", 0))
        except Exception:
            credits = 0
        lines = [
            "🔍 【Serper (Google 搜索) 配额】",
            f"🔑 Key: {self._mask_key(api_key)}",
            f"📊 剩余搜索次数: {credits:,} 次",
            "🌐 服务: Google 搜索 API (Serper.dev)",
        ]
        return "\n".join(lines)

    # ──────────────────────────────────────
    #  聚合查询与指令分发
    # ──────────────────────────────────────

    async def _query_services(
        self, event: AstrMessageEvent | None = None, service: str = "",
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
            "siliconflow": (
                self._get_siliconflow_keys, self._fetch_siliconflow_quota,
                self._format_siliconflow_message,
            ),
            "moonshot": (
                self._get_moonshot_keys, self._fetch_moonshot_quota,
                self._format_moonshot_message,
            ),
            "zhipu": (
                self._get_zhipu_keys, self._fetch_zhipu_quota,
                self._format_zhipu_message,
            ),
            "serper": (
                self._get_serper_keys, self._fetch_serper_quota,
                self._format_serper_message,
            ),
        }
        alias_map = {
            "tav": "tavily",
            "open": "openrouter",
            "deep": "deepseek",
            "git": "github",
            "sili": "siliconflow",
            "silicon": "siliconflow",
            "kimi": "moonshot",
            "moonshot": "moonshot",
            "glm": "zhipu",
            "zhipu": "zhipu",
            "serper": "serper",
        }
        service = service.strip().lower()
        if service in alias_map:
            service = alias_map[service]
        if service and service not in services:
            return (
                "用法：/quota [tavily|openrouter|deepseek|github|silicon|moonshot|glm|serper]；"
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
                    logger.warning(f"[api_quota] {name} 查询或格式化失败，继续其他服务。")
                    message = "❌ 查询失败，请检查网络、凭据或接口状态。"
                blocks.append(title + "\n" + message)
        if not blocks:
            return "未检测到任何服务的 API Key / Token，请先配置。"
        return "## 📊 API 配额汇总\n\n" + "\n\n---\n\n".join(blocks)

    async def _collect_cards_for_services(
        self, event: AstrMessageEvent | None = None, service: str = "",
    ) -> list[ServiceCardData]:
        services = {
            "tavily": (self._get_api_keys, self._fetch_quota, self._card_tavily),
            "openrouter": (self._get_openrouter_keys, self._fetch_openrouter_quota, self._card_openrouter),
            "deepseek": (self._get_deepseek_keys, self._fetch_deepseek_quota, self._card_deepseek),
            "github": (self._get_github_tokens, self._fetch_github_quota, self._card_github),
            "siliconflow": (self._get_siliconflow_keys, self._fetch_siliconflow_quota, self._card_siliconflow),
            "moonshot": (self._get_moonshot_keys, self._fetch_moonshot_quota, self._card_moonshot),
            "zhipu": (self._get_zhipu_keys, self._fetch_zhipu_quota, self._card_zhipu),
            "serper": (self._get_serper_keys, self._fetch_serper_quota, self._card_serper),
        }
        alias_map = {
            "tav": "tavily",
            "open": "openrouter",
            "deep": "deepseek",
            "git": "github",
            "sili": "siliconflow",
            "silicon": "siliconflow",
            "kimi": "moonshot",
            "moonshot": "moonshot",
            "glm": "zhipu",
            "zhipu": "zhipu",
            "serper": "serper",
        }
        service = service.strip().lower()
        if service in alias_map:
            service = alias_map[service]
        if service and service not in services:
            return []

        cards: list[ServiceCardData] = []
        for name in ([service] if service else services):
            get_keys, fetch, card_builder = services[name]
            keys = get_keys(event)
            for key in keys:
                try:
                    data = await fetch(key)
                    cards.append(card_builder(key, data))
                except Exception:
                    cards.append(card_builder(key, None))
        return cards

    @filter.command("quota", alias={"配额"})
    async def query_all_command(
        self, event: AstrMessageEvent, service: str = "",
    ):
        """查询全部已配置服务，或使用 /quota 服务名 查询指定服务。"""
        if not self._can_query(event):
            yield event.plain_result("⚠️ 当前仅管理员可查询 API 额度。")
            return

        raw_arg = service.strip()
        arg_lower = raw_arg.lower()

        # 1. 预警子指令：/quota alert [here|on|off|check|status]
        if arg_lower.startswith(("alert", "预警", "告警")):
            parts = arg_lower.split()
            sub_action = parts[1] if len(parts) > 1 else "status"
            if sub_action in ("here", "bind", "绑定", "开启", "on"):
                self.config["enable_alert"] = True
                self.config["alert_notify_target"] = getattr(event, "unified_msg_origin", "")
                self.alert_manager.start()
                hours = self.config.get("alert_interval_hours", 12)
                yield event.plain_result(
                    f"✅ 【已成功开启 API 额度低额预警】\n"
                    f"🎯 推送目标已绑定当前会话: {getattr(event, 'unified_msg_origin', '当前会话')}\n"
                    f"⏱️ 巡检周期: 每 {hours} 小时\n"
                    f"💡 可发送 /quota alert check 立即执行一次巡检测试。"
                )
                return
            elif sub_action in ("off", "close", "关闭"):
                self.config["enable_alert"] = False
                self.alert_manager.stop()
                yield event.plain_result("🛑 【已关闭 API 额度自动巡检与预警】。")
                return
            elif sub_action in ("check", "test", "测试", "检查"):
                yield event.plain_result("🔍 正在执行 API 额度巡检，请稍候...")
                alerts = await self.alert_manager.check_and_notify(manual=True)
                if alerts:
                    yield event.plain_result(
                        "⚠️ 【巡检结果：发现低额度/异常项】\n\n" + "\n\n────────────────\n\n".join(alerts)
                    )
                else:
                    yield event.plain_result("✅ 【巡检结果：所有已配置 API 额度与余额充足！】")
                return
            else:
                enabled = bool(self.config.get("enable_alert", False))
                target = self.config.get("alert_notify_target") or "未设置（仅通知管理员）"
                hours = self.config.get("alert_interval_hours", 12)
                yield event.plain_result(
                    f"🔔 【API 额度预警状态】\n"
                    f"📌 状态: {'已开启' if enabled else '已关闭'}\n"
                    f"🎯 接收目标: {target}\n"
                    f"⏱️ 巡检间隔: 每 {hours} 小时\n\n"
                    f"常用指令：\n"
                    f"• /quota alert here - 绑定当前会话并开启预警\n"
                    f"• /quota alert off - 关闭预警\n"
                    f"• /quota alert check - 立即触发一次巡检测试"
                )
                return

        # 2. 判断是否出图
        force_text = "text" in arg_lower
        force_img = ("img" in arg_lower) or ("card" in arg_lower) or ("pic" in arg_lower)

        # 过滤控制字符
        clean_service = arg_lower
        for flag in ("text", "img", "card", "pic"):
            clean_service = clean_service.replace(flag, "").strip()

        # 出图看板模式
        if self._should_render_card(event, force_img=force_img, force_text=force_text):
            cards = await self._collect_cards_for_services(event, clean_service)
            if cards:
                img_path = self._render_card_safe(cards, title="API 配额与余额看板")
                if img_path:
                    yield event.image_result(img_path)
                    return

        # 回退纯文本
        yield event.plain_result(await self._query_services(event, clean_service))

    @filter.command("deepseek", alias={"deep"})
    async def query_deepseek_command(self, event: AstrMessageEvent, arg: str = ""):
        """查询 DeepSeek 余额。"""
        if not self._can_query(event):
            yield event.plain_result("⚠️ 当前仅管理员可查询 API 额度。")
            return

        arg_str = str(arg).lower()
        force_text = "text" in arg_str
        force_img = ("img" in arg_str) or ("card" in arg_str) or ("pic" in arg_str)

        keys = self._get_deepseek_keys(event)
        if not keys:
            yield event.plain_result("❌ 未检测到 deepseek 配置的 API Key / Token。")
            return

        if self._should_render_card(event, force_img=force_img, force_text=force_text):
            cards = []
            for key in keys:
                data = await self._fetch_deepseek_quota(key)
                cards.append(self._card_deepseek(key, data))
            img_path = self._render_card_safe(cards, title="DeepSeek 余额看板")
            if img_path:
                yield event.image_result(img_path)
                return

        yield event.plain_result(await self._query_services(event, "deepseek"))

    @filter.command("github", alias={"git"})
    async def query_github_command(self, event: AstrMessageEvent, arg: str = ""):
        """查询 GitHub API Rate Limit 及可获取的 Actions 配额。"""
        if not self._can_query(event):
            yield event.plain_result("⚠️ 当前仅管理员可查询 API 额度。")
            return

        arg_str = str(arg).lower()
        force_text = "text" in arg_str
        force_img = ("img" in arg_str) or ("card" in arg_str) or ("pic" in arg_str)

        tokens = self._get_github_tokens(event)
        if not tokens:
            yield event.plain_result("❌ 未检测到 github 配置的 API Key / Token。")
            return

        if self._should_render_card(event, force_img=force_img, force_text=force_text):
            cards = []
            for token in tokens:
                data = await self._fetch_github_quota(token)
                cards.append(self._card_github(token, data))
            img_path = self._render_card_safe(cards, title="GitHub 配额看板")
            if img_path:
                yield event.image_result(img_path)
                return

        yield event.plain_result(await self._query_services(event, "github"))

    async def _query_new_tool(
        self, event: AstrMessageEvent | None = None, service: str = "",
    ) -> str:
        if not self.config.get("enable_llm_tool", True):
            return "额度查询工具已禁用。"
        return await self._query_services(event, service)

    @filter.llm_tool(name="query_deepseek_quota")
    async def query_deepseek_quota_tool(self, event: AstrMessageEvent | None = None) -> str:
        """查询 DeepSeek 的可用状态、币种、总余额、充值余额和赠送体验金。"""
        return await self._query_new_tool(event, "deepseek")

    @filter.llm_tool(name="query_github_quota")
    async def query_github_quota_tool(self, event: AstrMessageEvent | None = None) -> str:
        """查询 GitHub Core API 配额、重置时间及可获取的 Actions 分钟配额。"""
        return await self._query_new_tool(event, "github")

    @filter.llm_tool(name="query_openrouter_quota")
    async def query_openrouter_quota_tool(
        self, event: AstrMessageEvent | None = None,
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


    @filter.command("silicon", alias={"sili"})
    async def query_siliconflow_command(self, event: AstrMessageEvent, arg: str = ""):
        """查询 SiliconFlow (硅基流动) 余额。"""
        if not self._can_query(event):
            yield event.plain_result("⚠️ 当前仅管理员可查询 API 额度。")
            return

        arg_str = str(arg).lower()
        force_text = "text" in arg_str
        force_img = ("img" in arg_str) or ("card" in arg_str) or ("pic" in arg_str)

        keys = self._get_siliconflow_keys(event)
        if not keys:
            yield event.plain_result("❌ 未检测到 siliconflow 配置的 API Key。")
            return

        if self._should_render_card(event, force_img=force_img, force_text=force_text):
            cards = []
            for key in keys:
                data = await self._fetch_siliconflow_quota(key)
                cards.append(self._card_siliconflow(key, data))
            img_path = self._render_card_safe(cards, title="SiliconFlow 余额看板")
            if img_path:
                yield event.image_result(img_path)
                return

        yield event.plain_result(await self._query_services(event, "siliconflow"))

    @filter.command("moonshot", alias={"kimi"})
    async def query_moonshot_command(self, event: AstrMessageEvent, arg: str = ""):
        """查询 Moonshot AI (Kimi) 余额。"""
        if not self._can_query(event):
            yield event.plain_result("⚠️ 当前仅管理员可查询 API 额度。")
            return

        arg_str = str(arg).lower()
        force_text = "text" in arg_str
        force_img = ("img" in arg_str) or ("card" in arg_str) or ("pic" in arg_str)

        keys = self._get_moonshot_keys(event)
        if not keys:
            yield event.plain_result("❌ 未检测到 moonshot/kimi 配置的 API Key。")
            return

        if self._should_render_card(event, force_img=force_img, force_text=force_text):
            cards = []
            for key in keys:
                data = await self._fetch_moonshot_quota(key)
                cards.append(self._card_moonshot(key, data))
            img_path = self._render_card_safe(cards, title="Moonshot 余额看板")
            if img_path:
                yield event.image_result(img_path)
                return

        yield event.plain_result(await self._query_services(event, "moonshot"))

    @filter.command("glm")
    async def query_zhipu_command(self, event: AstrMessageEvent, arg: str = ""):
        """查询 Zhipu AI (智谱/GLM) 配额与余额。"""
        if not self._can_query(event):
            yield event.plain_result("⚠️ 当前仅管理员可查询 API 额度。")
            return

        arg_str = str(arg).lower()
        force_text = "text" in arg_str
        force_img = ("img" in arg_str) or ("card" in arg_str) or ("pic" in arg_str)

        keys = self._get_zhipu_keys(event)
        if not keys:
            yield event.plain_result("❌ 未检测到 zhipu/glm 配置的 API Key。")
            return

        if self._should_render_card(event, force_img=force_img, force_text=force_text):
            cards = []
            for key in keys:
                data = await self._fetch_zhipu_quota(key)
                cards.append(self._card_zhipu(key, data))
            img_path = self._render_card_safe(cards, title="Zhipu AI 配额看板")
            if img_path:
                yield event.image_result(img_path)
                return

        yield event.plain_result(await self._query_services(event, "zhipu"))

    @filter.command("serper")
    async def query_serper_command(self, event: AstrMessageEvent, arg: str = ""):
        """查询 Serper (Google 搜索 API) 剩余配额。"""
        if not self._can_query(event):
            yield event.plain_result("⚠️ 当前仅管理员可查询 API 额度。")
            return

        arg_str = str(arg).lower()
        force_text = "text" in arg_str
        force_img = ("img" in arg_str) or ("card" in arg_str) or ("pic" in arg_str)

        keys = self._get_serper_keys(event)
        if not keys:
            yield event.plain_result("❌ 未检测到 serper 配置的 API Key。")
            return

        if self._should_render_card(event, force_img=force_img, force_text=force_text):
            cards = []
            for key in keys:
                data = await self._fetch_serper_quota(key)
                cards.append(self._card_serper(key, data))
            img_path = self._render_card_safe(cards, title="Serper 配额看板")
            if img_path:
                yield event.image_result(img_path)
                return

        yield event.plain_result(await self._query_services(event, "serper"))

    @filter.llm_tool(name="query_siliconflow_quota")
    async def query_siliconflow_quota_tool(self, event: AstrMessageEvent | None = None) -> str:
        """查询 SiliconFlow (硅基流动) 的总余额、充值余额、赠送体验金与账户状态。"""
        return await self._query_new_tool(event, "siliconflow")

    @filter.llm_tool(name="query_moonshot_quota")
    async def query_moonshot_quota_tool(self, event: AstrMessageEvent | None = None) -> str:
        """查询 Moonshot AI (Kimi) 的可用余额、现金余额与代金券余额。"""
        return await self._query_new_tool(event, "moonshot")

    @filter.llm_tool(name="query_zhipu_quota")
    async def query_zhipu_quota_tool(self, event: AstrMessageEvent | None = None) -> str:
        """查询 Zhipu AI (智谱/GLM) 的账户余额或 Coding Plan 订阅配额使用情况。"""
        return await self._query_new_tool(event, "zhipu")

    @filter.llm_tool(name="query_serper_quota")
    async def query_serper_quota_tool(self, event: AstrMessageEvent | None = None) -> str:
        """查询 Serper (Google 网页搜索 API) 的剩余可用调用次数配额。"""
        return await self._query_new_tool(event, "serper")
