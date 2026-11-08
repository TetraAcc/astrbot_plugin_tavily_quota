"""图片看板渲染与低额度预警功能的单元测试。"""

import os
import unittest
from unittest.mock import AsyncMock, Mock

from test_access_and_config import Event, Plugin
from card_renderer import ServiceCardData, render_quota_dashboard
from alert_manager import QuotaAlertManager


class CardAndAlertTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.context = Mock()
        self.context.send_message = AsyncMock(return_value=True)
        self.plugin = Plugin(self.context, {})

    def test_card_renderer_generates_valid_image(self):
        """测试使用 Pillow 绘制暗色看板，验证是否生成有效的 PNG 图片文件。"""
        items = [
            ServiceCardData(
                service="deepseek",
                key_masked="sk-a1b2…c3d4",
                status="ok",
                primary_metric="¥25.80",
                primary_label="可用余额",
                details=["充值: ¥20.00", "赠金: ¥5.80", "状态: 可用"],
            ),
            ServiceCardData(
                service="tavily",
                key_masked="tvly-…99",
                status="warn",
                primary_metric="85 / 1,000 次",
                primary_label="剩余配额",
                details=["套餐: 开发者套餐", "已用: 915 次", "搜索: 890 次"],
                progress_ratio=0.915,
            ),
        ]
        img_path = render_quota_dashboard(items, title="测试额度看板")
        self.assertTrue(os.path.exists(img_path))
        self.assertTrue(img_path.endswith(".png"))
        self.assertGreater(os.path.getsize(img_path), 1000)

        # 验证 PNG 文件头
        with open(img_path, "rb") as f:
            header = f.read(8)
            self.assertEqual(header, b"\x89PNG\r\n\x1a\n")

        # 清理临时文件
        try:
            os.remove(img_path)
        except OSError:
            pass

    async def test_alert_manager_detects_low_balance(self):
        """测试预警管理器检测低额度并触发推送。"""
        self.plugin.config.update({
            "enable_alert": True,
            "alert_notify_target": "test:FriendMessage:admin",
            "alert_thresholds": {
                "deepseek_min_balance": 5.0,
                "tavily_min_remaining": 100,
            },
        })

        # Mock DeepSeek 余额仅剩 2.50 元 (低于 5.0 阈值)
        self.plugin._get_deepseek_keys = Mock(return_value=["sk-low-ds"])
        self.plugin._fetch_deepseek_quota = AsyncMock(return_value={
            "is_available": True,
            "balance_infos": [{
                "currency": "CNY",
                "total_balance": "2.50",
                "topped_up_balance": "2.50",
                "granted_balance": "0.00",
            }],
        })

        # Mock Tavily 剩余 50 次 (低于 100 阈值)
        self.plugin._get_api_keys = Mock(return_value=["tvly-low"])
        self.plugin._fetch_quota = AsyncMock(return_value={
            "account": {
                "plan_limit": 1000,
                "plan_usage": 950,
            }
        })

        self.plugin._get_openrouter_keys = Mock(return_value=[])
        self.plugin._get_github_tokens = Mock(return_value=[])

        alert_mgr = QuotaAlertManager(self.plugin, self.context, self.plugin.config)
        alerts = await alert_mgr.check_and_notify(manual=True)

        self.assertEqual(len(alerts), 2)
        self.assertTrue(any("DeepSeek 余额偏低" in a for a in alerts))
        self.assertTrue(any("Tavily 搜索配额见底" in a for a in alerts))

        # 验证发送接口被调用
        self.context.send_message.assert_awaited_once()
        call_args = self.context.send_message.await_args[0]
        self.assertEqual(call_args[0], "test:FriendMessage:admin")

    async def test_quota_alert_commands(self):
        """测试 /quota alert here 与 check 指令。"""
        event = Event(admin=True)

        # 1. 开启并绑定当前会话
        results = [x async for x in self.plugin.query_all_command(event, "alert here")]
        self.assertIn("已成功开启 API 额度低额预警", results[0])
        self.assertTrue(self.plugin.config["enable_alert"])
        self.assertEqual(self.plugin.config["alert_notify_target"], event.unified_msg_origin)

        # 2. 手动巡检指令
        self.plugin.alert_manager.check_and_notify = AsyncMock(return_value=[])
        check_res = [x async for x in self.plugin.query_all_command(event, "alert check")]
        self.assertIn("所有已配置 API 额度与余额充足", check_res[-1])

    async def test_llm_tools_callable_without_event_argument(self):
        """测试大模型 Function Calling 时不传 event 参数也能直接调用不报 missing argument。"""
        self.plugin.config.update({
            "api_keys": ["test-tvly"],
            "openrouter_api_keys": ["test-or"],
            "deepseek_api_keys": ["test-ds"],
            "github_tokens": ["test-gh"],
        })
        self.plugin._fetch_quota = AsyncMock(return_value={"account": {"plan_limit": 100, "plan_usage": 10}})
        self.plugin._fetch_openrouter_quota = AsyncMock(return_value={"credits": {"total_credits": 10, "total_usage": 1}})
        self.plugin._fetch_deepseek_quota = AsyncMock(return_value={"is_available": True, "balance_infos": []})
        self.plugin._fetch_github_quota = AsyncMock(return_value={"resources": {"core": {"limit": 60, "remaining": 50, "reset": 123456}}})

        for tool_name in (
            "query_tavily_quota_tool",
            "query_openrouter_quota_tool",
            "query_deepseek_quota_tool",
            "query_github_quota_tool",
        ):
            fn = getattr(self.plugin, tool_name)
            # 无参调用（即 LLM 在底层执行时的调用形式）
            res = await fn()
            self.assertIsInstance(res, str)
            self.assertGreater(len(res), 5)


if __name__ == "__main__":
    unittest.main()
