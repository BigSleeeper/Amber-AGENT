import contextlib
import io
import json
import queue
import runpy
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from napcat_adapter.agent_api import ProcessClient
from napcat_adapter.configuration import AdapterConfig


def message(group=20, text="群消息"):
    return dict(type="message", source="qq", conversation_id=f"qq:group:{group}",
                sender_id="10", message_id="123", text=text, timestamp=123, metadata={})


class ProcessApiTests(unittest.TestCase):
    def client(self, messages=()):
        transport = types.SimpleNamespace(receive=Mock(side_effect=list(messages)),
            send=Mock(return_value=dict(success=True, status="ok", message_id="456")), close=Mock())
        return ProcessClient(transport=transport), transport

    def test_receive_shape_and_local_wait(self):
        client, transport = self.client([message(), None])
        received = client.receive_message()
        self.assertEqual(received["conversation_id"], "qq:group:20")
        self.assertIn("群消息", received["content"])
        self.assertIsNone(client.receive_message())
        self.assertEqual(transport.receive.call_count, 2)
        transport.receive.assert_called_with(1.0)

    def test_current_group_binding_and_no_cross_thread_target(self):
        client, transport = self.client([message(), message(21)])
        client.receive_message()
        self.assertTrue(json.loads(client.send_message("回答"))["success"])
        transport.send.assert_called_with("qq:group:20", "回答")
        client.receive_message()
        client.send_message("下一个群")
        transport.send.assert_called_with("qq:group:21", "下一个群")
        results = queue.Queue()
        thread = threading.Thread(target=lambda: results.put(json.loads(client.send_message("不应发送"))))
        thread.start()
        thread.join()
        self.assertEqual(results.get()["status"], "not_sent")
        self.assertEqual(transport.send.call_count, 2)

    def test_no_target_and_empty_text_never_send(self):
        client, transport = self.client([message()])
        self.assertEqual(json.loads(client.send_message("未接收"))["status"], "not_sent")
        client.receive_message()
        self.assertEqual(json.loads(client.send_message(" "))["status"], "not_sent")
        transport.send.assert_not_called()

    def test_start_once_and_stop_once(self):
        from napcat_adapter import agent_api as api
        fake = types.SimpleNamespace(close=Mock())
        with patch.object(api, "_client", None), patch.object(api, "ProcessClient", return_value=fake) as factory:
            self.assertIs(api.start_adapter(), fake)
            self.assertIs(api.start_adapter(), fake)
            api.stop_adapter()
            api.stop_adapter()
        factory.assert_called_once_with()
        fake.close.assert_called_once_with()

    def test_configuration(self):
        with patch.dict("os.environ", NAPCAT_ALLOW_GROUPS="20,21，20", NAPCAT_GROUP_ONLY="1",
                        NAPCAT_REQUIRE_AT="0", NAPCAT_API_TIMEOUT="5", NAPCAT_INBOX_CAPACITY="100"):
            config = AdapterConfig.from_env()
        self.assertEqual(config.allowed_groups, (20, 21))
        self.assertEqual(config.inbox_capacity, 100)
        self.assertIn("--group-only", config.arguments())
        self.assertEqual(config.send_timeout, 10)
        for config in (AdapterConfig(api_timeout=float("nan")), AdapterConfig(inbox_capacity=0),
                       AdapterConfig(url="http://localhost"), AdapterConfig(url="ws://host?token=secret")):
            with self.assertRaises(ValueError):
                config.validate()


class ReservedCodeIntegrationTests(unittest.TestCase):
    def test_main_uses_reserved_receive_and_keeps_group_contexts(self):
        import napcat_adapter.agent_api as api
        agent = types.ModuleType("agent")
        agent.agent_cnt = [0, 0]
        calls = []
        def loop(messages, query):
            calls.append(len(messages))
            messages.append(dict(role="assistant", content="内部结束语"))
        agent.agent_loop = loop
        events = [dict(message(group), content=f"group {group}") for group in (20, 21, 20)]
        with patch.dict(sys.modules, agent=agent), \
                patch.object(api, "receive_message", side_effect=[None, *events, KeyboardInterrupt]), \
                patch.object(api, "start_adapter") as start, \
                patch.object(api, "stop_adapter") as stop, \
                contextlib.redirect_stdout(io.StringIO()):
            result = runpy.run_path(str(Path(__file__).resolve().parents[2] / "main.py"))
        start.assert_called_once_with()
        stop.assert_called_once_with()
        self.assertEqual(calls, [1, 1, 3])
        self.assertEqual(agent.agent_cnt[0], 3)
        self.assertEqual(len(result["qq_contexts"]), 2)

    def test_actual_tools_registration_and_dispatch(self):
        import napcat_adapter.agent_api as api
        config = types.ModuleType("config")
        config.MODEL = "test"
        config.WORKDIR = Path.cwd()
        config.ENVIRONMENT_PROMPT = "test"
        client = types.ModuleType("client")
        client.client = None
        hooks = types.ModuleType("hooks")
        hooks.trigger_hooks = lambda *args: None
        with patch.dict(sys.modules, config=config, client=client, hooks=hooks):
            namespace = runpy.run_path(str(Path(__file__).resolve().parents[2] / "tools.py"))
        call = types.SimpleNamespace(function=types.SimpleNamespace(
            name="send_QQ_message", arguments=json.dumps(dict(text="来自工具"))))
        with patch.object(api, "send_message", return_value='{"success": true}') as send:
            result = namespace["execute_tool"](call)
        self.assertTrue(json.loads(result)["success"])
        send.assert_called_once_with("来自工具")
        schema = next(tool for tool in namespace["MAIN_AGENT_TOOLS"]
                      if tool["function"]["name"] == "send_QQ_message")
        self.assertEqual(set(schema["function"]["parameters"]["properties"]), {"text"})

    def test_agent_error_also_stops_adapter(self):
        import napcat_adapter.agent_api as api
        agent = types.ModuleType("agent")
        agent.agent_cnt = [0, 0]
        agent.agent_loop = Mock(side_effect=RuntimeError("model failed"))
        incoming = dict(message(), content="群消息")
        with patch.dict(sys.modules, agent=agent), \
                patch.object(api, "receive_message", return_value=incoming), \
                patch.object(api, "start_adapter"), \
                patch.object(api, "stop_adapter") as stop, \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "model failed"):
                runpy.run_path(str(Path(__file__).resolve().parents[2] / "main.py"))
        stop.assert_called_once_with()
