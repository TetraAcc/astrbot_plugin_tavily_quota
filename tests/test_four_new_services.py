"""针对 SiliconFlow、Moonshot、Zhipu (GLM) 与 Serper 的完整单元测试。"""
import os
import unittest
from unittest.mock import AsyncMock, Mock, patch

from test_access_and_config import Event, Plugin
from card_renderer import render_quota_dashboard


class FourNewServicesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.context = Mock()
        self.context.get_config.return_value = {}
        self.context.activate_llm_tool_async = AsyncMock()
        self.context.deactivate_llm_tool_async = AsyncMock()
        self.plugin = Plugin(self.context, {"enable_llm_tool": True, "render_image": True})

    # ──────────────────────────────────────
    #  1. SiliconFlow 测试
    # ──────────────────────────────────────

    def test_siliconflow_keys_priority(self):
        self.plugin.config["siliconflow_api_keys"] = ["sk-sili-config"]
        self.assertEqual(self.plugin._get_siliconflow_keys(Event()), ["sk-sili-config"])

        self.plugin.config["siliconflow_api_keys"] = []
        self.context.get_config.return_value = {
            "provider_sources": [{"provider": "siliconflow", "key": ["sk-sili-session"], "enable": True}]
        }
        self.assertEqual(self.plugin._get_siliconflow_keys(Event()), ["sk-sili-session"])

        self.context.get_config.return_value = {}
        with patch.dict(os.environ, {"SILICONFLOW_API_KEY": "sk-sili-env"}):
            self.assertEqual(self.plugin._get_siliconflow_keys(Event()), ["sk-sili-env"])

    async def test_siliconflow_card_and_format(self):
        data = {
            "name": "tester",
            "status": "normal",
            "totalBalance": "18.50",
            "chargeBalance": "8.50",
            "balance": "10.00",
        }
        card = self.plugin._card_siliconflow("sk-sili-1234567890", data)
        self.assertEqual(card.service, "siliconflow")
        self.assertEqual(card.status, "ok")
        self.assertEqual(card.primary_metric, "¥18.50")
        self.assertIn("¥8.50", card.details[0])

        # 测试低额警告
        card_low = self.plugin._card_siliconflow("sk-sili-1234567890", {**data, "totalBalance": "2.00"})
        self.assertEqual(card_low.status, "warn")

        msg = self.plugin._format_siliconflow_message("sk-sili-1234567890", data)
        self.assertIn("18.50", msg)
        self.assertIn("8.50", msg)

    # ──────────────────────────────────────
    #  2. Moonshot AI 测试
    # ──────────────────────────────────────

    def test_moonshot_keys_priority(self):
        self.plugin.config["moonshot_api_keys"] = ["sk-kimi-config"]
        self.assertEqual(self.plugin._get_moonshot_keys(Event()), ["sk-kimi-config"])

        self.plugin.config["moonshot_api_keys"] = []
        self.context.get_config.return_value = {
            "provider_sources": [{"provider": "moonshot-kimi", "key": ["sk-kimi-session"], "enable": True}]
        }
        self.assertEqual(self.plugin._get_moonshot_keys(Event()), ["sk-kimi-session"])

        self.context.get_config.return_value = {}
        with patch.dict(os.environ, {"MOONSHOT_API_KEY": "sk-kimi-env"}):
            self.assertEqual(self.plugin._get_moonshot_keys(Event()), ["sk-kimi-env"])

    async def test_moonshot_card_and_format(self):
        data = {
            "available_balance": 25.0,
            "cash_balance": 15.0,
            "voucher_balance": 10.0,
        }
        card = self.plugin._card_moonshot("sk-kimi-1234567890", data)
        self.assertEqual(card.service, "moonshot")
        self.assertEqual(card.status, "ok")
        self.assertEqual(card.primary_metric, "¥25.00")

        msg = self.plugin._format_moonshot_message("sk-kimi-1234567890", data)
        self.assertIn("25.00", msg)
        self.assertIn("15.00", msg)

    # ──────────────────────────────────────
    #  3. Zhipu AI 测试
    # ──────────────────────────────────────

    def test_zhipu_keys_priority(self):
        self.plugin.config["zhipu_api_keys"] = ["test.zhipu-config"]
        self.assertEqual(self.plugin._get_zhipu_keys(Event()), ["test.zhipu-config"])

        self.plugin.config["zhipu_api_keys"] = []
        self.context.get_config.return_value = {
            "provider_sources": [{"provider": "zhipu-ai", "key": ["test.zhipu-session"], "enable": True}]
        }
        self.assertEqual(self.plugin._get_zhipu_keys(Event()), ["test.zhipu-session"])

    async def test_zhipu_card_and_format(self):
        # 现金账户模式
        data_acc = {
            "account": {"availableBalance": 50.0, "totalBalance": 100.0},
            "coding_plan": None,
        }
        card = self.plugin._card_zhipu("test.zhipu-123456", data_acc)
        self.assertEqual(card.service, "zhipu")
        self.assertEqual(card.status, "ok")
        self.assertEqual(card.primary_metric, "¥50.00")

        # Coding Plan 模式
        data_plan = {
            "account": None,
            "coding_plan": {
                "level": "Pro",
                "limits": [{
                    "type": "TOKENS_LIMIT",
                    "remaining": 16000,
                    "total": 20000,
                    "percentage": 20,
                    "nextResetTime": 1780000000000,
                }]
            }
        }
        card_plan = self.plugin._card_zhipu("test.zhipu-123456", data_plan)
        self.assertEqual(card_plan.primary_metric, "16,000 / 20,000")
        self.assertIn("Pro", card_plan.primary_label)

    # ──────────────────────────────────────
    #  4. Serper 测试
    # ──────────────────────────────────────

    def test_serper_keys_priority(self):
        self.plugin.config["serper_api_keys"] = ["serper-config-key"]
        self.assertEqual(self.plugin._get_serper_keys(Event()), ["serper-config-key"])

        self.plugin.config["serper_api_keys"] = []
        with patch.dict(os.environ, {"SERPER_API_KEY": "serper-env-key"}):
            self.assertEqual(self.plugin._get_serper_keys(Event()), ["serper-env-key"])

    async def test_serper_card_and_format(self):
        data = {"credits": 2500}
        card = self.plugin._card_serper("serper-key-123456", data)
        self.assertEqual(card.service, "serper")
        self.assertEqual(card.status, "ok")
        self.assertEqual(card.primary_metric, "2,500 次")

        # 低配额
        card_low = self.plugin._card_serper("serper-key-123456", {"credits": 50})
        self.assertEqual(card_low.status, "warn")

        msg = self.plugin._format_serper_message("serper-key-123456", data)
        self.assertIn("2,500 次", msg)

    # ──────────────────────────────────────
    #  5. 看板渲染完整验证
    # ──────────────────────────────────────

    def test_render_all_services_card(self):
        cards = [
            self.plugin._card_tavily("tav-1234567890", {"account": {"plan_limit": 1000, "plan_usage": 150}}),
            self.plugin._card_openrouter("sk-or-1234567890", {"credits": {"total_credits": 10.0, "total_usage": 2.5}}),
            self.plugin._card_deepseek("sk-ds-1234567890", {"is_available": True, "balance_infos": [{"total_balance": "100.00"}]}),
            self.plugin._card_github("ghp_1234567890123456", {"core": {"remaining": 4900, "limit": 5000}}),
            self.plugin._card_siliconflow("sk-sili-1234567890", {"totalBalance": "18.50", "chargeBalance": "8.50", "balance": "10.00"}),
            self.plugin._card_moonshot("sk-kimi-1234567890", {"available_balance": 25.0, "cash_balance": 15.0, "voucher_balance": 10.0}),
            self.plugin._card_zhipu("test.zhipu-123456", {"account": {"availableBalance": 50.0}}),
            self.plugin._card_serper("serper-key-123456", {"credits": 2500}),
        ]
        img_path = render_quota_dashboard(cards, title="全源 API 看板测试")
        self.assertTrue(os.path.exists(img_path))
        self.assertTrue(os.path.getsize(img_path) > 1000)

    # ──────────────────────────────────────
    #  6. 隔离性验证：未配置的服务在 /quota 中隐藏
    # ──────────────────────────────────────

    async def test_unconfigured_services_hidden_in_quota(self):
        # 仅配置 SiliconFlow
        self.plugin.config["siliconflow_api_keys"] = ["sk-sili-key"]
        self.plugin._fetch_siliconflow_quota = AsyncMock(return_value={"totalBalance": "18.50"})

        cards = await self.plugin._collect_cards_for_services(Event())
        # 结果应仅包含 1 个卡片（siliconflow），其他未配置的全部不显示！
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].service, "siliconflow")

        # 文本模式汇总同样仅显示 siliconflow
        text_summary = await self.plugin._query_services(Event())
        self.assertIn("siliconflow", text_summary.lower())
        self.assertNotIn("deepseek", text_summary.lower())
        self.assertNotIn("openrouter", text_summary.lower())
        self.assertNotIn("serper", text_summary.lower())

    # ──────────────────────────────────────
    #  7. 指令调用与别名分发测试（确保无参数调用不报错）
    # ──────────────────────────────────────

    async def test_commands_execution_without_params(self):
        event = Event(admin=True)
        self.plugin.config["siliconflow_api_keys"] = ["sk-sili-key"]
        self.plugin.config["moonshot_api_keys"] = ["sk-kimi-key"]
        self.plugin.config["zhipu_api_keys"] = ["test.zhipu-key"]
        self.plugin.config["serper_api_keys"] = ["serper-key"]

        self.plugin._fetch_siliconflow_quota = AsyncMock(return_value={"totalBalance": "10.0"})
        self.plugin._fetch_moonshot_quota = AsyncMock(return_value={"available_balance": 10.0})
        self.plugin._fetch_zhipu_quota = AsyncMock(return_value={"account": {"availableBalance": 10.0}})
        self.plugin._fetch_serper_quota = AsyncMock(return_value={"credits": 1000})

        # 指令以 0 参数（默认值）调用，测试返回有效结果且不抛异常
        res_sili = [x async for x in self.plugin.query_siliconflow_command(event)]
        self.assertTrue(len(res_sili) > 0)

        res_moon = [x async for x in self.plugin.query_moonshot_command(event)]
        self.assertTrue(len(res_moon) > 0)

        res_zhipu = [x async for x in self.plugin.query_zhipu_command(event)]
        self.assertTrue(len(res_zhipu) > 0)

        res_serper = [x async for x in self.plugin.query_serper_command(event)]
        self.assertTrue(len(res_serper) > 0)

        res_all = [x async for x in self.plugin.query_all_command(event)]
        self.assertTrue(len(res_all) > 0)


if __name__ == "__main__":
    unittest.main()
