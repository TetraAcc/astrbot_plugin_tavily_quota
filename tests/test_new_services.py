"""新增服务与聚合入口测试；所有 HTTP、配置及凭据均为模拟数据。"""
import ast
import os
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, Mock, patch

from test_access_and_config import Event, Plugin


BALANCE = {
    "is_available": True,
    "balance_infos": [{"currency": "CNY", "total_balance": "28.50",
                       "granted_balance": "10.00", "topped_up_balance": "18.50"}],
}
RATE = {"resources": {"core": {"remaining": 4321, "limit": 5000,
                                 "reset": 1700000000}}}
TOKEN = "test-credential-only-for-mocks-1234"


class Response:
    def __init__(self, status, data):
        self.status, self.data = status, data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self):
        if isinstance(self.data, Exception):
            raise self.data
        return self.data


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return Response(*response)


class NewServicesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.context = Mock()
        self.context.get_config.return_value = {}
        self.context.activate_llm_tool_async = AsyncMock()
        self.context.deactivate_llm_tool_async = AsyncMock()
        self.plugin = Plugin(self.context, {"admin_only": False})
        self.env = patch.dict(os.environ, dict.fromkeys([
            "TAVILY_API_KEY", "OPENROUTER_API_KEY", "DEEPSEEK_API_KEY",
            "GITHUB_TOKEN", "GH_TOKEN",
        ], ""))
        self.env.start()
        self.addCleanup(self.env.stop)

    def http(self, responses):
        session = Session(responses)
        client = self.plugin._fetch_deepseek_quota.__globals__["aiohttp"]
        patched = patch.object(client, "ClientSession", return_value=session)
        return session, patched

    def test_deepseek_explicit_keys_take_priority(self):
        self.plugin.config["deepseek_api_keys"] = [" first ", "first", "", None]
        self.assertEqual(self.plugin._get_deepseek_keys(Event()), ["first"])
        self.context.get_config.assert_not_called()

    def test_deepseek_provider_type_enabled_and_environment_fallback(self):
        self.context.get_config.return_value = {"provider_sources": [
            {"provider": "DeepSeek", "key": "provider-key"},
            {"type": "deepseek_chat", "key": ["type-key", "provider-key"]},
            {"provider": "deepseek", "enable": False, "key": "disabled"},
            {"provider": "other", "key": "unrelated"},
        ]}
        self.assertEqual(self.plugin._get_deepseek_keys(Event()), ["provider-key", "type-key"])
        self.context.get_config.assert_called_with(umo=Event.unified_msg_origin)
        self.context.get_config.side_effect = RuntimeError("mock config unavailable")
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "environment-key"}):
            self.assertEqual(self.plugin._get_deepseek_keys(Event()), ["environment-key"])

    def test_github_config_alias_and_environment_priority(self):
        with patch.dict(os.environ, {"GITHUB_TOKEN": "first-env", "GH_TOKEN": "second-env"}):
            self.assertEqual(self.plugin._get_github_tokens(), ["first-env"])
            self.plugin.config["github_personal_access_tokens"] = ["alias-token"]
            self.assertEqual(self.plugin._get_github_tokens(), ["alias-token"])
            self.plugin.config["github_tokens"] = [" explicit ", "explicit"]
            self.assertEqual(self.plugin._get_github_tokens(), ["explicit"])
        self.plugin.config.clear()
        with patch.dict(os.environ, {"GITHUB_TOKEN": "  ", "GH_TOKEN": "fallback"}):
            self.assertEqual(self.plugin._get_github_tokens(), ["fallback"])
        self.context.get_config.assert_not_called()

    async def test_deepseek_request_headers_timeout_and_no_redirect(self):
        session, patched = self.http([(200, BALANCE)])
        with patched as factory:
            self.assertEqual(await self.plugin._fetch_deepseek_quota(TOKEN), BALANCE)
        self.assertEqual(factory.call_args.kwargs["timeout"].total, 10)
        url, kwargs = session.calls[0]
        self.assertEqual(url, "https://api.deepseek.com/user/balance")
        self.assertEqual(kwargs["headers"], {"Authorization": "Bearer " + TOKEN, "Accept": "application/json"})
        self.assertFalse(kwargs["allow_redirects"])

    async def test_legacy_tavily_transport_keeps_endpoint_and_timeout(self):
        payload = {"account": {"plan_usage": 2, "plan_limit": 100}}
        session, patched = self.http([(200, payload)])
        with patched as factory:
            self.assertEqual(await self.plugin._fetch_quota(TOKEN), payload)
        self.assertEqual(factory.call_args.kwargs["timeout"].total, 10)
        self.assertEqual(session.calls[0][0], "https://api.tavily.com/usage")

    async def test_legacy_openrouter_transport_keeps_both_endpoints(self):
        session, patched = self.http([
            (200, {"data": {"usage_daily": 1}}),
            (200, {"data": {"total_credits": 10, "total_usage": 1}}),
        ])
        with patched as factory:
            result = await self.plugin._fetch_openrouter_quota(TOKEN)
        self.assertEqual(factory.call_args.kwargs["timeout"].total, 10)
        self.assertEqual([url for url, _ in session.calls], [
            "https://openrouter.ai/api/v1/auth/key",
            "https://openrouter.ai/api/v1/credits",
        ])
        self.assertEqual(result["credits"]["total_credits"], 10)
        self.assertEqual(result["key_info"]["usage_daily"], 1)

    async def test_legacy_failure_logs_do_not_echo_response_credentials(self):
        for method, responses in [
            ("_fetch_quota", [(401, {"error": TOKEN})]),
            ("_fetch_openrouter_quota", [(401, {}), (403, {})]),
            ("_fetch_deepseek_quota", [TimeoutError(TOKEN)]),
        ]:
            logger = getattr(self.plugin, method).__globals__["logger"]
            logger.reset_mock()
            _, patched = self.http(responses)
            with patched:
                self.assertIsNone(await getattr(self.plugin, method)(TOKEN))
            self.assertNotIn(TOKEN, repr(logger.mock_calls))

    async def test_deepseek_failures_and_malformed_json_do_not_escape(self):
        for response in [(401, {}), (429, {}), (200, ValueError("bad JSON")),
                         (200, []), (200, {"balance_infos": None}), TimeoutError(TOKEN)]:
            with self.subTest(response=response):
                _, patched = self.http([response])
                with patched:
                    self.assertIsNone(await self.plugin._fetch_deepseek_quota(TOKEN))

    def test_deepseek_multiple_currencies_and_unavailable_state(self):
        data = {"is_available": False, "balance_infos": [
            *BALANCE["balance_infos"], {"currency": "USD", "total_balance": "1.23",
                                       "granted_balance": "0", "topped_up_balance": "1.23"},
        ]}
        text = self.plugin._format_deepseek_message(TOKEN, data)
        for expected in ("不可用", "CNY", "USD", "28.50", "18.50", "10.00", "1.23"):
            self.assertIn(expected, text)
        self.assertNotIn(TOKEN, text)
        self.assertIn("test…1234", text)

    def test_amounts_are_safe_and_unknown_is_not_zero(self):
        for value in (None, True, "NaN", "Infinity", TOKEN):
            self.assertEqual(self.plugin._amount(value), "未知")
        self.assertEqual(self.plugin._amount("0"), "0.00")
        self.assertEqual(self.plugin._mask_key("short"), "***")

    async def test_github_rate_user_and_actions_success(self):
        session, patched = self.http([
            (200, RATE), (200, {"login": "mock-user"}),
            (200, {"total_minutes_used": 12, "included_minutes": 2000}),
        ])
        with patched as factory:
            result = await self.plugin._fetch_github_quota(TOKEN)
        self.assertEqual(factory.call_args.kwargs["timeout"].total, 10)
        self.assertEqual([call[0] for call in session.calls], [
            "https://api.github.com/rate_limit", "https://api.github.com/user",
            "https://api.github.com/users/mock-user/settings/billing/actions",
        ])
        for _, kwargs in session.calls:
            self.assertEqual(kwargs["headers"]["Authorization"], "Bearer " + TOKEN)
            self.assertEqual(kwargs["headers"]["User-Agent"], "AstrBot-QuotaPlugin")
            self.assertEqual(kwargs["headers"]["Accept"], "application/vnd.github+json")
            self.assertFalse(kwargs["allow_redirects"])
        text = self.plugin._format_github_message(TOKEN, result)
        self.assertIn("4,321 / 5,000", text)
        self.assertIn("12.00 / 2000.00", text)
        self.assertRegex(text, r"\d{2}:\d{2}:\d{2}")
        self.assertIn("本地时间", text)
        self.assertNotIn(TOKEN, text)

    async def test_github_actions_permission_or_retirement_is_ignored(self):
        for failure in [(403, {}), (404, {}), (410, {}), (500, {}), TimeoutError(TOKEN)]:
            with self.subTest(failure=failure):
                _, patched = self.http([(200, RATE), (200, {"login": "mock-user"}), failure])
                with patched:
                    result = await self.plugin._fetch_github_quota(TOKEN)
                self.assertEqual(result, {"core": RATE["resources"]["core"]})
                self.assertNotIn("Actions", self.plugin._format_github_message(TOKEN, result))

    async def test_github_optional_user_failure_keeps_rate_result(self):
        for response in [(403, {}), (200, {"login": "../../evil"}), TimeoutError(TOKEN)]:
            session, patched = self.http([(200, RATE), response])
            with patched:
                result = await self.plugin._fetch_github_quota(TOKEN)
            self.assertEqual(len(session.calls), 2)
            self.assertIn("core", result)

    async def test_github_rate_failure_does_not_query_billing(self):
        for response in [(401, {}), (200, []), (200, {"resources": None}), TimeoutError(TOKEN)]:
            session, patched = self.http([response])
            with patched:
                self.assertIsNone(await self.plugin._fetch_github_quota(TOKEN))
            self.assertEqual(len(session.calls), 1)

    def test_github_reset_and_missing_fields_are_not_fabricated(self):
        text = self.plugin._format_github_message(TOKEN, {"core": {"reset": 10 ** 100}})
        self.assertIn("未知 / 未知", text)
        self.assertIn("重置时间: 未知", text)
        text = self.plugin._format_github_message(TOKEN, {"core": {}, "actions": {"total_minutes_used": 0}})
        self.assertIn("2000", text)
        self.assertIn("兼容默认值", text)

    def configure_all(self):
        self.plugin.config.update(api_keys=[TOKEN], openrouter_api_keys=[TOKEN],
                                  deepseek_api_keys=[TOKEN], github_tokens=[TOKEN])
        self.plugin._fetch_quota = AsyncMock(return_value={"account": {"plan_usage": 2, "plan_limit": 100}})
        self.plugin._fetch_openrouter_quota = AsyncMock(return_value={"credits": {"total_credits": 10, "total_usage": 1}})
        self.plugin._fetch_deepseek_quota = AsyncMock(return_value=BALANCE)
        self.plugin._fetch_github_quota = AsyncMock(return_value={"core": RATE["resources"]["core"]})

    async def test_aggregate_queries_all_services_in_order(self):
        self.configure_all()
        order = []
        for name, method in [("Tavily", "_fetch_quota"), ("OpenRouter", "_fetch_openrouter_quota"),
                             ("DeepSeek", "_fetch_deepseek_quota"), ("GitHub", "_fetch_github_quota")]:
            value = getattr(self.plugin, method).return_value
            async def fetch(key, name=name, value=value):
                order.append(name)
                return value
            setattr(self.plugin, method, fetch)
        result = await self.plugin._query_services(Event())
        self.assertEqual(order, ["Tavily", "OpenRouter", "DeepSeek", "GitHub"])
        for name in order:
            self.assertIn(name, result)
        self.assertIn("## 📊 API 配额汇总", result)
        self.assertNotIn(TOKEN, result)

    async def test_aggregate_skips_unconfigured_and_survives_one_failure(self):
        self.plugin.config.update(deepseek_api_keys=[TOKEN], github_tokens=[TOKEN])
        self.plugin._fetch_deepseek_quota = AsyncMock(side_effect=RuntimeError(TOKEN))
        self.plugin._fetch_github_quota = AsyncMock(return_value={"core": RATE["resources"]["core"]})
        text = await self.plugin._query_services(Event())
        self.assertIn("查询失败", text)
        self.assertIn("GitHub", text)
        self.assertNotIn("### tavily", text)
        self.assertNotIn(TOKEN, text)

    async def test_selected_service_does_not_query_other_services(self):
        for selected, method in [("tavily", "_fetch_quota"), ("openrouter", "_fetch_openrouter_quota"),
                                 ("deepseek", "_fetch_deepseek_quota"), ("github", "_fetch_github_quota")]:
            self.configure_all()
            output = [x async for x in self.plugin.query_all_command(Event(), selected)]
            self.assertEqual(len(output), 1)
            getattr(self.plugin, method).assert_awaited_once_with(TOKEN)
            for other in ("_fetch_quota", "_fetch_openrouter_quota", "_fetch_deepseek_quota", "_fetch_github_quota"):
                if other != method:
                    getattr(self.plugin, other).assert_not_awaited()

    async def test_new_commands_and_tools_return_results(self):
        self.configure_all()
        for method in ("query_deepseek_command", "query_github_command"):
            output = [x async for x in getattr(self.plugin, method)(Event())]
            self.assertIn("配额汇总", output[0])
            self.assertNotIn(TOKEN, output[0])
        for method in ("query_deepseek_quota_tool", "query_github_quota_tool"):
            self.assertIn("配额汇总", await getattr(self.plugin, method)(Event()))

    async def test_new_entrypoints_check_admin_before_keys(self):
        self.plugin.config["admin_only"] = True
        for method in ("query_all_command", "query_deepseek_command", "query_github_command"):
            self.assertIn("仅管理员", [x async for x in getattr(self.plugin, method)(Event())][0])
        for method in ("query_deepseek_quota_tool", "query_github_quota_tool"):
            self.assertIn("仅管理员", await getattr(self.plugin, method)(Event()))
        self.context.get_config.assert_not_called()

    async def test_disabled_new_tools_do_not_read_config_or_block_commands(self):
        self.plugin.config["enable_llm_tool"] = False
        for method in ("query_deepseek_quota_tool", "query_github_quota_tool"):
            self.assertIn("已禁用", await getattr(self.plugin, method)(Event()))
        self.context.get_config.assert_not_called()
        self.plugin.config["deepseek_api_keys"] = [TOKEN]
        self.plugin._fetch_deepseek_quota = AsyncMock(return_value=BALANCE)
        self.assertIn("DeepSeek", [x async for x in self.plugin.query_deepseek_command(Event())][0])

    async def test_multiple_keys_continue_after_one_failure(self):
        self.plugin.config["deepseek_api_keys"] = ["mock-first-credential", TOKEN]
        self.plugin._fetch_deepseek_quota = AsyncMock(side_effect=[None, BALANCE])
        text = await self.plugin._query_services(Event(), "deepseek")
        self.assertIn("凭据 1", text)
        self.assertIn("凭据 2", text)
        self.assertIn("28.50", text)
        self.assertEqual(self.plugin._fetch_deepseek_quota.await_count, 2)

    async def test_unknown_service_and_missing_keys_have_clear_messages(self):
        self.assertIn("用法", await self.plugin._query_services(Event(), "unknown"))
        self.context.get_config.assert_not_called()
        self.assertIn("未检测到", await self.plugin._query_services(Event(), "deepseek"))
        self.assertIn("未检测到任何服务", await self.plugin._query_services(Event()))

    def test_command_and_tool_declarations(self):
        tree = ast.parse((Path(__file__).resolve().parents[1] / "main.py").read_text())
        declared = {}
        tools = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            for deco in node.decorator_list:
                if not isinstance(deco, ast.Call) or not isinstance(deco.func, ast.Attribute):
                    continue
                if deco.func.attr == "command":
                    declared[ast.literal_eval(deco.args[0])] = {kw.arg: ast.literal_eval(kw.value) for kw in deco.keywords}
                elif deco.func.attr == "llm_tool":
                    tools.extend(ast.literal_eval(kw.value) for kw in deco.keywords if kw.arg == "name")
        self.assertEqual(declared["quota"]["alias"], {"配额"})
        self.assertEqual(declared["deepseek"]["alias"], {"deep", "deepseek_quota"})
        self.assertEqual(declared["github"]["alias"], {"git", "github_quota"})
        self.assertIn("tavily", declared)
        self.assertIn("openrouter", declared)
        self.assertEqual(set(tools), {"query_tavily_quota", "query_openrouter_quota",
                                      "query_deepseek_quota", "query_github_quota"})


if __name__ == "__main__":
    unittest.main()
