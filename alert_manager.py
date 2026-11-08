"""低余额 / 低配额后台巡检与主动预警管理器。"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from astrbot.api.star import Context
    from astrbot.core import AstrBotConfig


class QuotaAlertManager:
    """负责在后台按设定周期检查各服务余额，在低于阈值时主动推送提醒。"""

    def __init__(self, plugin: Any, context: Any, config: Any):
        self.plugin = plugin
        self.context = context
        self.config = config
        self._task: asyncio.Task | None = None
        self._last_alert_states: dict[str, float] = {}  # key -> last alerted value (用于防刷屏)
        self._running = False

    def start(self):
        """启动后台巡检任务。"""
        if self._running or self._task is not None:
            return
        if not bool(self.config.get("enable_alert", False)):
            return

        self._running = True
        self._task = asyncio.create_task(self._loop())

    def stop(self):
        """停止后台巡检任务。"""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None

    async def _loop(self):
        """定期巡检主循环。"""
        # 启动后稍作等待，避免和 AstrBot 初始化竞争
        await asyncio.sleep(15)

        while self._running:
            try:
                await self.check_and_notify(manual=False)
            except asyncio.CancelledError:
                break
            except Exception as e:
                try:
                    from astrbot.api import logger
                    logger.warning(f"[tavily_quota] 自动巡检发生异常: {e}")
                except Exception:
                    pass

            # 获取巡检间隔（小时），最低 1 小时，默认 12 小时
            interval_hours = max(1.0, float(self.config.get("alert_interval_hours", 12.0)))
            sleep_seconds = int(interval_hours * 3600)

            try:
                await asyncio.sleep(sleep_seconds)
            except asyncio.CancelledError:
                break

    async def check_and_notify(self, manual: bool = False) -> list[str]:
        """执行巡检，并在命中阈值时向管理员或目标会话发送警报。

        返回本次检测到的告警条目列表。
        """
        thresholds = self.config.get("alert_thresholds", {}) or {}
        deepseek_min = float(thresholds.get("deepseek_min_balance", 5.0))
        openrouter_min = float(thresholds.get("openrouter_min_balance", 1.0))
        tavily_min = int(thresholds.get("tavily_min_remaining", 100))
        github_min = int(thresholds.get("github_min_remaining", 500))

        alerts: list[str] = []

        # 1. 检查 DeepSeek
        ds_keys = self.plugin._get_deepseek_keys(None)
        for key in ds_keys:
            try:
                data = await self.plugin._fetch_deepseek_quota(key)
                if not data:
                    continue
                # 解析总可用余额
                for row in data.get("balance_infos", []):
                    try:
                        tot = float(row.get("total_balance", 0.0))
                        currency = row.get("currency", "CNY")
                        if tot <= deepseek_min:
                            mask = self.plugin._mask_key(key)
                            alerts.append(
                                f"⚠️ 【DeepSeek 余额偏低】\n"
                                f"Key: {mask}\n"
                                f"当前可用: {tot:.2f} {currency}（阈值: {deepseek_min:.2f}）\n"
                                f"请及时前往平台充值以免服务中断。"
                            )
                    except Exception:
                        pass
            except Exception:
                pass

        # 2. 检查 OpenRouter
        or_keys = self.plugin._get_openrouter_keys(None)
        for key in or_keys:
            try:
                data = await self.plugin._fetch_openrouter_quota(key)
                if not data:
                    continue
                credits_info = data.get("credits", {})
                tot_credits = credits_info.get("total_credits")
                tot_usage = credits_info.get("total_usage", 0.0)
                if tot_credits is not None:
                    bal = max(0.0, float(tot_credits) - float(tot_usage))
                    if bal <= openrouter_min:
                        mask = self.plugin._mask_key(key)
                        alerts.append(
                            f"⚠️ 【OpenRouter 余额偏低】\n"
                            f"Key: {mask}\n"
                            f"当前可用余额: ${bal:.4f}（阈值: ${openrouter_min:.2f}）\n"
                            f"请注意补充额度。"
                        )
            except Exception:
                pass

        # 3. 检查 Tavily
        tav_keys = self.plugin._get_api_keys(None)
        for key in tav_keys:
            try:
                data = await self.plugin._fetch_quota(key)
                if not data:
                    continue
                acc = data.get("account", {})
                limit = acc.get("plan_limit")
                usage = acc.get("plan_usage", 0)
                if limit is not None:
                    remaining = limit - usage
                    if remaining <= tavily_min:
                        mask = self.plugin._mask_key(key)
                        alerts.append(
                            f"⚠️ 【Tavily 搜索配额见底】\n"
                            f"Key: {mask}\n"
                            f"当前剩余: {remaining:,} 次（阈值: {tavily_min:,} 次）\n"
                            f"本月配额已消耗 {usage:,} / {limit:,} 次。"
                        )
            except Exception:
                pass

        # 4. 检查 GitHub
        gh_tokens = self.plugin._get_github_tokens(None)
        for token in gh_tokens:
            try:
                data = await self.plugin._fetch_github_quota(token)
                if not data:
                    continue
                core = data.get("core", {})
                rem = core.get("remaining")
                if rem is not None and isinstance(rem, int):
                    if rem <= github_min:
                        mask = self.plugin._mask_key(token)
                        alerts.append(
                            f"⚠️ 【GitHub API 配额不足】\n"
                            f"Token: {mask}\n"
                            f"当前剩余: {rem:,} / {core.get('limit', '未知')} 次（阈值: {github_min:,} 次）"
                        )
            except Exception:
                pass

        # 如果没有告警或未配置接收目标
        if not alerts:
            return []

        # 推送通知
        target_umo = self.config.get("alert_notify_target", "").strip()
        if target_umo:
            await self._send_proactive_alert(target_umo, alerts)

        return alerts

    async def _send_proactive_alert(self, target_umo: str, alerts: list[str]):
        """通过 AstrBot 核心接口向指定目标推送预警。"""
        try:
            msg_text = "🔔 【API 额度与余额告警通知】\n\n" + "\n\n────────────────\n\n".join(alerts)
            try:
                from astrbot.api.event import MessageChain
                chain = MessageChain().message(msg_text)
            except (ImportError, AttributeError):
                chain = msg_text

            if hasattr(self.context, "send_message"):
                await self.context.send_message(target_umo, chain)
        except Exception as e:
            try:
                from astrbot.api import logger
                logger.warning(f"[tavily_quota] 发送预警消息失败: {e}")
            except Exception:
                pass
