"""权限与配置来源回归测试；不依赖 AstrBot 服务或真实 API Key。"""
import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch


def load_plugin():
    def decorator(*args, **kwargs):
        return lambda function: function

    class Star:
        def __init__(self, context):
            self.context = context

    api = types.ModuleType("astrbot.api")
    api.logger = Mock()
    event = types.ModuleType("astrbot.api.event")
    event.AstrMessageEvent = object
    event.filter = types.SimpleNamespace(
        command=decorator, llm_tool=decorator, on_astrbot_loaded=decorator
    )
    star = types.ModuleType("astrbot.api.star")
    star.Star = Star
    star.Context = object
    star.register = decorator
    core = types.ModuleType("astrbot.core")
    core.AstrBotConfig = dict
    modules = {
        "astrbot": types.ModuleType("astrbot"), "astrbot.api": api,
        "astrbot.api.event": event, "astrbot.api.star": star, "astrbot.core": core,
    }
    path = Path(__file__).resolve().parents[1] / "main.py"
    spec = importlib.util.spec_from_file_location("quota_under_test", path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module.TavilyQuotaPlugin


Plugin = load_plugin()


class Event:
    unified_msg_origin = "test:FriendMessage:user"

    def __init__(self, admin=False, sender="ordinary-user"):
        self.admin = admin
        self.sender = sender

    def is_admin(self):
        return self.admin

    def get_sender_id(self):
        return self.sender

    def plain_result(self, text):
        return text


class AccessAndConfigTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.context = Mock()
        self.context.get_config.return_value = {}
        self.context.activate_llm_tool_async = AsyncMock(return_value=True)
        self.context.deactivate_llm_tool_async = AsyncMock(return_value=True)
        self.plugin = Plugin(self.context, {"admin_only": True, "enable_llm_tool": True})
        self.env = patch.dict(os.environ, {"TAVILY_API_KEY": "", "OPENROUTER_API_KEY": ""})
        self.env.start()
        self.addCleanup(self.env.stop)

    async def test_commands_deny_non_admin_before_reading_keys(self):
        for name in ("query_quota_command", "query_openrouter_command"):
            with self.subTest(command=name):
                self.context.get_config.reset_mock()
                output = [x async for x in getattr(self.plugin, name)(Event())]
                self.assertIn("仅管理员", output[0])
                self.context.get_config.assert_not_called()

    async def test_former_personal_ids_have_no_privilege(self):
        # These values are test fixtures for removed exceptions, never authorization data.
        for sender in ("292801705", "D4DD424330CF05C2D3B2E769109A8451"):
            output = [x async for x in self.plugin.query_quota_command(Event(sender=sender))]
            self.assertIn("仅管理员", output[0])

    async def test_tools_deny_non_admin_before_reading_keys(self):
        for name in ("query_tavily_quota_tool", "query_openrouter_quota_tool"):
            self.context.get_config.reset_mock()
            self.assertIn("仅管理员", await getattr(self.plugin, name)(Event()))
            self.context.get_config.assert_not_called()

    async def test_disabled_tools_deny_even_admin(self):
        self.plugin.config["enable_llm_tool"] = False
        for name in ("query_tavily_quota_tool", "query_openrouter_quota_tool"):
            self.assertIn("已禁用", await getattr(self.plugin, name)(Event(admin=True)))
        self.context.get_config.assert_not_called()

    async def test_public_commands_work_when_admin_only_is_off(self):
        self.plugin.config["admin_only"] = False
        output = [x async for x in self.plugin.query_quota_command(Event())]
        self.assertIn("未检测到", output[0])
        self.context.get_config.assert_called_once_with(umo=Event.unified_msg_origin)

    async def test_authorized_commands_and_tools_query_without_exposing_keys(self):
        self.plugin.config.update(api_keys=["secret-tavily"], openrouter_api_keys=["secret-router"])
        self.plugin._fetch_quota = AsyncMock(return_value={"account": {"plan_limit": 100, "plan_usage": 2}})
        self.plugin._fetch_openrouter_quota = AsyncMock(return_value={"credits": {"total_credits": 10, "total_usage": 2}})
        event = Event(admin=True)
        for name in ("query_quota_command", "query_openrouter_command"):
            output = "\n".join([x async for x in getattr(self.plugin, name)(event)])
            self.assertIn("额度使用情况", output)
            self.assertNotIn("secret-", output)
        for name in ("query_tavily_quota_tool", "query_openrouter_quota_tool"):
            output = await getattr(self.plugin, name)(event)
            self.assertIn("额度使用情况", output)
            self.assertNotIn("secret-", output)
        self.assertEqual(self.plugin._fetch_quota.await_count, 2)
        self.assertEqual(self.plugin._fetch_openrouter_quota.await_count, 2)

    async def test_tool_switch_uses_public_lifecycle_api(self):
        for enabled in (False, True):
            self.plugin.config["enable_llm_tool"] = enabled
            self.context.activate_llm_tool_async.reset_mock()
            self.context.deactivate_llm_tool_async.reset_mock()
            await self.plugin.initialize()
            setter = self.context.activate_llm_tool_async if enabled else self.context.deactivate_llm_tool_async
            self.assertEqual([call.args[0] for call in setter.await_args_list],
                             ["query_tavily_quota", "query_openrouter_quota"])
            other = self.context.deactivate_llm_tool_async if enabled else self.context.activate_llm_tool_async
            other.assert_not_awaited()

    async def test_disabled_tools_do_not_disable_commands(self):
        self.plugin.config["enable_llm_tool"] = False
        output = [x async for x in self.plugin.query_quota_command(Event(admin=True))]
        self.assertIn("未检测到", output[0])

    async def test_startup_reapplies_tool_switch_after_framework_restore(self):
        self.plugin.config["enable_llm_tool"] = False
        await self.plugin.on_astrbot_loaded()
        self.assertEqual(self.context.deactivate_llm_tool_async.await_count, 2)

    def test_explicit_keys_take_priority_and_are_deduplicated(self):
        self.plugin.config.update(api_keys=[" one ", "one", "", None], openrouter_api_keys=" two ")
        self.assertEqual(self.plugin._get_api_keys(Event()), ["one"])
        self.assertEqual(self.plugin._get_openrouter_keys(Event()), ["two"])
        self.context.get_config.assert_not_called()

    def test_reads_only_current_session_via_public_api(self):
        self.context.get_config.return_value = {
            "provider_settings": {"websearch_tavily_key": ["session-key"]},
            "provider_sources": [
                {"provider": "openrouter", "key": ["router", "router"]},
                {"provider": "openrouter", "enable": False, "key": ["disabled"]},
                {"provider": "other", "key": ["unrelated"]},
            ],
        }
        with patch("builtins.open", side_effect=AssertionError("no config file reads")):
            self.assertEqual(self.plugin._get_api_keys(Event()), ["session-key"])
            self.assertEqual(self.plugin._get_openrouter_keys(Event()), ["router"])
        self.assertTrue(all(c.kwargs == {"umo": Event.unified_msg_origin}
                            for c in self.context.get_config.call_args_list))

    def test_environment_is_fallback_not_merged(self):
        with patch.dict(os.environ, {"TAVILY_API_KEY": "env-t", "OPENROUTER_API_KEY": "env-o"}):
            self.assertEqual(self.plugin._get_api_keys(Event()), ["env-t"])
            self.assertEqual(self.plugin._get_openrouter_keys(Event()), ["env-o"])
            self.context.get_config.return_value = {"provider_settings": {"websearch_tavily_key": "session"}}
            self.assertEqual(self.plugin._get_api_keys(Event()), ["session"])

    def test_interface_failure_falls_back_without_reading_files(self):
        self.context.get_config.side_effect = RuntimeError("unavailable")
        with patch.dict(os.environ, {"TAVILY_API_KEY": "env"}), patch("builtins.open", side_effect=AssertionError):
            self.assertEqual(self.plugin._get_api_keys(Event()), ["env"])

    def test_private_reload_command_removed(self):
        self.assertFalse(hasattr(self.plugin, "reload_tavily_cmd"))


if __name__ == "__main__":
    unittest.main()
