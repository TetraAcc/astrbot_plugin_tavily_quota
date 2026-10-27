import glob
import json
import os
import aiohttp
from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.core import AstrBotConfig


@register(
    "tavily_quota",
    "Tetra",
    "Tavily & OpenRouter 额度与用量查询插件",
    "1.1.0",
)
class TavilyQuotaPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

    # ──────────────────────────────────────
    #  OpenRouter 相关方法
    # ──────────────────────────────────────

    def _get_openrouter_keys(self) -> list[str]:
        """获取 OpenRouter API Keys：插件配置 → AstrBot 主配置 → 环境变量"""
        keys = []
        # 1. 插件自定义配置
        conf_keys = self.config.get("openrouter_api_keys", [])
        if isinstance(conf_keys, list):
            for k in conf_keys:
                if isinstance(k, str) and k.strip():
                    keys.append(k.strip())
        elif isinstance(conf_keys, str) and conf_keys.strip():
            keys.append(conf_keys.strip())

        if keys:
            return keys

        # 2. 从 AstrBot 主配置的 provider_sources 中查找 openrouter
        try:
            curr_dir = os.path.dirname(os.path.abspath(__file__))
            data_dir = os.path.abspath(os.path.join(curr_dir, "..", ".."))
            config_candidates = [
                os.path.join(data_dir, "cmd_config.json"),
            ]
            config_candidates.extend(glob.glob(os.path.join(data_dir, "config", "abconf_*.json")))

            for conf_file in config_candidates:
                if not os.path.exists(conf_file):
                    continue
                try:
                    with open(conf_file, "r", encoding="utf-8-sig") as f:
                        data = json.load(f)

                    # 在 provider_sources 中查找 openrouter 类型
                    sources = data.get("provider_sources", [])
                    if not isinstance(sources, list):
                        sources = []
                    for src in sources:
                        if not isinstance(src, dict):
                            continue
                        provider = src.get("provider", "")
                        src_type = src.get("type", "")
                        if "openrouter" in provider.lower() or "openrouter" in src_type.lower():
                            src_keys = src.get("key", [])
                            if isinstance(src_keys, list):
                                for k in src_keys:
                                    if isinstance(k, str) and k.strip() and k.strip() not in keys:
                                        keys.append(k.strip())
                            elif isinstance(src_keys, str) and src_keys.strip() and src_keys.strip() not in keys:
                                keys.append(src_keys.strip())
                except Exception as e:
                    logger.debug(f"[tavily_quota] 读取配置 {conf_file} 查找 OpenRouter Key 出错: {e}")
        except Exception as e:
            logger.warning(f"[tavily_quota] 自动定位主配置失败: {e}")

        # 3. 环境变量兜底
        env_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        if env_key and env_key not in keys:
            keys.append(env_key)

        return keys

    async def _fetch_openrouter_quota(self, api_key: str) -> dict | None:
        headers = {"Authorization": f"Bearer {api_key}"}
        result = {}
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                # 1. 查询 API Key 详情与单 Key 限额
                try:
                    async with session.get("https://openrouter.ai/api/v1/auth/key", headers=headers) as resp:
                        if resp.status == 200:
                            key_data = await resp.json()
                            result["key_info"] = key_data.get("data", {})
                        else:
                            text = await resp.text()
                            logger.error(f"[tavily_quota] OpenRouter Key 查询失败 HTTP {resp.status}: {text}")
                except Exception as e:
                    logger.error(f"[tavily_quota] 请求 OpenRouter Key API 异常: {e}")

                # 2. 查询账户真实总充值与消费 (credits)
                try:
                    async with session.get("https://openrouter.ai/api/v1/credits", headers=headers) as resp:
                        if resp.status == 200:
                            cred_data = await resp.json()
                            result["credits"] = cred_data.get("data", {})
                        else:
                            text = await resp.text()
                            logger.error(f"[tavily_quota] OpenRouter Credits 查询失败 HTTP {resp.status}: {text}")
                except Exception as e:
                    logger.error(f"[tavily_quota] 请求 OpenRouter Credits API 异常: {e}")

            if not result.get("key_info") and not result.get("credits"):
                return None
            return result
        except Exception as e:
            logger.error(f"[tavily_quota] 请求 OpenRouter API 出现异常: {e}")
            return None

    def _format_openrouter_message(self, api_key: str, data: dict, show_key: bool = False) -> str:
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
            progress = self._render_progress_bar(int(total_usage * 10000), int(total_credits * 10000)) if total_credits > 0 else ""
        else:
            budget = k_info.get("effective_budget", {})
            limit_usd = budget.get("limit_usd")
            spend_usd = budget.get("spend_usd", 0.0)
            remaining_usd = budget.get("remaining_usd")
            total_credits_str = f"${limit_usd:.2f}" if limit_usd is not None else "无限制"
            total_usage_str = f"${spend_usd:.4f}"
            balance_str = f"${remaining_usd:.4f}" if remaining_usd is not None else "无限制"
            progress = self._render_progress_bar(int(spend_usd * 10000), int(limit_usd * 10000)) if limit_usd and limit_usd > 0 else ""

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
                masked = f"{api_key[:8]}...{api_key[-4:]}" if len(api_key) > 12 else "***"
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
            lines.append(f"🛡️ Key 预算上限: ${key_limit:.2f} (当前Key已用: ${key_spend:.4f})")

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

    def _get_api_keys(self) -> list[str]:
        keys = []
        # 1. 优先读取插件自定义配置中的 keys
        conf_keys = self.config.get("api_keys", [])
        if isinstance(conf_keys, list):
            for k in conf_keys:
                if isinstance(k, str) and k.strip():
                    keys.append(k.strip())
        elif isinstance(conf_keys, str) and conf_keys.strip():
            keys.append(conf_keys.strip())

        if keys:
            return keys

        # 2. 回退到 AstrBot 主配置文件中读取 websearch_tavily_key
        try:
            curr_dir = os.path.dirname(os.path.abspath(__file__))
            data_dir = os.path.abspath(os.path.join(curr_dir, "..", ".."))
            config_candidates = [
                os.path.join(data_dir, "cmd_config.json"),
            ]
            config_candidates.extend(glob.glob(os.path.join(data_dir, "config", "abconf_*.json")))

            for conf_file in config_candidates:
                if not os.path.exists(conf_file):
                    continue
                try:
                    with open(conf_file, "r", encoding="utf-8-sig") as f:
                        data = json.load(f)
                    
                    # 尝试根层级 (cmd_config.json) 或 provider_settings 层级 (abconf_*.json)
                    t_keys = data.get("websearch_tavily_key")
                    if not t_keys:
                        provider_settings = data.get("provider_settings", {})
                        t_keys = provider_settings.get("websearch_tavily_key", [])

                    if isinstance(t_keys, list):
                        for k in t_keys:
                            if isinstance(k, str) and k.strip() and k.strip() not in keys:
                                keys.append(k.strip())
                    elif isinstance(t_keys, str) and t_keys.strip() and t_keys.strip() not in keys:
                        keys.append(t_keys.strip())
                except Exception as e:
                    logger.debug(f"[tavily_quota] 读取配置文件 {conf_file} 出错: {e}")
        except Exception as e:
            logger.warning(f"[tavily_quota] 自动定位主配置失败: {e}")

        # 3. 环境变量兜底
        env_key = os.environ.get("TAVILY_API_KEY", "").strip()
        if env_key and env_key not in keys:
            keys.append(env_key)

        return keys

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
                        text = await resp.text()
                        logger.error(f"[tavily_quota] 查询失败 HTTP {resp.status}: {text}")
                        return None
        except Exception as e:
            logger.error(f"[tavily_quota] 请求 Tavily API 出现异常: {e}")
            return None

    def _render_progress_bar(self, usage: int, limit: int, width: int = 10) -> str:
        if not limit or limit <= 0:
            return ""
        ratio = min(max(usage / limit, 0.0), 1.0)
        filled = int(round(ratio * width))
        empty = width - filled
        pct = ratio * 100
        return f"[{'■' * filled}{'□' * empty}] {pct:.1f}%"

    def _format_quota_message(self, api_key: str, data: dict, show_key: bool = False) -> str:
        acc = data.get("account", {})
        plan = acc.get("current_plan", "未知套餐")
        limit = acc.get("plan_limit")
        usage = acc.get("plan_usage", 0)

        limit_str = f"{limit:,} 次" if limit is not None else "无限制"
        usage_str = f"{usage:,} 次"
        remaining_str = f"{(limit - usage):,} 次" if limit is not None else "无限制"

        progress = self._render_progress_bar(usage, limit) if limit else ""

        search_cnt = acc.get("search_usage", 0)
        extract_cnt = acc.get("extract_usage", 0)
        crawl_cnt = acc.get("crawl_usage", 0)

        lines = [
            "📊 【Tavily 额度使用情况】",
        ]
        if show_key:
            masked_key = f"{api_key[:4]}...{api_key[-2:]}" if len(api_key) > 8 else "***"
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

    @filter.command("tavily", alias={"tavily额度", "查额度", "tavily_quota"})
    async def query_quota_command(self, event: AstrMessageEvent):
        '''查询 Tavily 搜索引擎 API 的额度与使用情况'''
        admin_only = self.config.get("admin_only", False)
        # 管理员限制检查
        if admin_only:
            sender_id = str(event.get_sender_id())
            # 侯哥 QQ 或者带有 admin 权限
            is_master = sender_id in ["292801705", "D4DD424330CF05C2D3B2E769109A8451"]
            is_admin = getattr(event, "is_admin", False)
            if not (is_master or is_admin):
                yield event.plain_result("⚠️ 抱歉，当前仅管理员可查询 Tavily 额度。")
                return

        keys = self._get_api_keys()
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
        admin_only = self.config.get("admin_only", False)
        if admin_only:
            sender_id = str(event.get_sender_id())
            is_master = sender_id in ["292801705", "D4DD424330CF05C2D3B2E769109A8451"]
            is_admin = getattr(event, "is_admin", False)
            if not (is_master or is_admin):
                yield event.plain_result("⚠️ 抱歉，当前仅管理员可查询 OpenRouter 额度。")
                return

        keys = self._get_openrouter_keys()
        if not keys:
            yield event.plain_result("❌ 未检测到 OpenRouter API Key，请在插件配置或全局配置中设置。")
            return

        for key in keys:
            data = await self._fetch_openrouter_quota(key)
            if not data:
                yield event.plain_result("❌ OpenRouter Key 额度查询失败，请检查网络或 Key 状态。")
                continue
            msg = self._format_openrouter_message(key, data, show_key=False)
            yield event.plain_result(msg)

    @filter.command("reload_tavily")
    async def reload_tavily_cmd(self, event: AstrMessageEvent):
        '''重载 tavily 插件'''
        sender_id = str(event.get_sender_id())
        is_master = sender_id in ["292801705", "D4DD424330CF05C2D3B2E769109A8451"]
        if not is_master:
            yield event.plain_result("只有侯哥可以执行重载。")
            return
        sm = getattr(self.context, "_star_manager", None)
        if sm and hasattr(sm, "reload"):
            success, msg = await sm.reload("tavily_quota")
            yield event.plain_result(f"重载结果: {success}, {msg}")
        else:
            yield event.plain_result("未找到 star_manager")

    @filter.llm_tool(name="query_tavily_quota")
    async def query_tavily_quota_tool(self, event: AstrMessageEvent) -> str:
        '''查询当前 Tavily 搜索引擎的额度、已使用次数和剩余可用次数。
        当用户询问 tavily 额度、剩余搜索次数或 API 用量时调用。
        '''
        keys = self._get_api_keys()
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

    @filter.llm_tool(name="query_openrouter_quota")
    async def query_openrouter_quota_tool(self, event: AstrMessageEvent) -> str:
        '''查询当前 OpenRouter API 的额度、已消费金额和剩余额度。
        当用户询问 OpenRouter 额度、剩余费用或 API 用量时调用。
        '''
        keys = self._get_openrouter_keys()
        if not keys:
            return "未检测到配置的 OpenRouter API Key。"

        results = []
        for key in keys:
            data = await self._fetch_openrouter_quota(key)
            if data:
                msg = self._format_openrouter_message(key, data, show_key=False)
                results.append(msg)
            else:
                results.append("OpenRouter Key 额度查询失败。")

        return "\n\n".join(results)
