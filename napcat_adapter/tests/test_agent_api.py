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
    def test_public_receive_supports_immediate_local_polling(self):
        from napcat_adapter import agent_api as api
        with patch.object(api, "_get_client") as factory:
            factory.return_value.napcat_receive.return_value = []
            self.assertEqual(api.napcat_receive(), [])
            factory.return_value.napcat_receive.assert_called_once_with()

    def test_public_poke_requires_target_and_never_starts_for_invalid_params(self):
        from napcat_adapter import agent_api as api
        with patch.object(api, "_get_client") as factory:
            for params in (dict(user_id=0), dict(user_id=10, group_id=-1),
                           dict(user_id=10, target_id=True)):
                self.assertEqual(json.loads(api.send_poke(**params))["status"], "not_sent")
            with self.assertRaises(TypeError):
                api.send_poke(group_id=20)
        factory.assert_not_called()

    def test_poke_client_passes_explicit_napcat_params(self):
        client, transport = self.client()
        for params in (dict(user_id="10", group_id="20"), dict(user_id="10", target_id="99")):
            self.assertTrue(json.loads(client.send_poke(**params))["success"])
            transport.send_request.assert_called_with(params, command_type="send_poke")

    def test_full_raw_event_is_returned_as_content_dictionary(self):
        raw = {"sender": {"role": "admin", "title": "头衔"},
               "group_name": "测试群", "extension": {"value": [False, None]}}
        batch = [dict(type="message", content=raw)]
        client, _ = self.client([batch])
        received = client.napcat_receive()
        self.assertEqual(received, batch)
        self.assertIsInstance(received[0]["content"], dict)

    def client(self, messages=()):
        transport = types.SimpleNamespace(napcat_receive=Mock(side_effect=list(messages)),
            send_request=Mock(return_value=dict(success=True, status="ok", message_id="456")), close=Mock())
        return ProcessClient(transport=transport), transport

    def test_receive_shape_and_empty_batch(self):
        batch = [dict(type="message", content={"message": "群消息"}),
                 dict(type="meta_event", content={"meta_event_type": "heartbeat"})]
        client, transport = self.client([batch, []])
        self.assertEqual(client.napcat_receive(), batch)
        self.assertEqual(client.napcat_receive(), [])
        self.assertEqual(transport.napcat_receive.call_count, 2)
        transport.napcat_receive.assert_called_with()

    def test_explicit_targets_do_not_depend_on_received_messages_or_thread(self):
        client, transport = self.client()
        params = dict(message_type="group", group_id=20, message="回答", auto_escape=True)
        self.assertTrue(json.loads(client.send_message(**params))["success"])
        transport.send_request.assert_called_with(params)
        results = queue.Queue()
        private = dict(message_type="private", user_id=10,
                       message=[dict(type="text", data=dict(text="私聊"))])
        thread = threading.Thread(target=lambda: results.put(json.loads(client.send_message(**private))))
        thread.start()
        thread.join()
        self.assertTrue(results.get()["success"])
        transport.send_request.assert_called_with(private)
        self.assertEqual(transport.send_request.call_count, 2)

    def test_missing_target_and_empty_message_never_send(self):
        client, transport = self.client()
        for params in (dict(message_type="group", message="无目标"),
                       dict(message_type="private", group_id=20, message="无用户"),
                       dict(message_type="group", group_id=20, message=" ")):
            self.assertEqual(json.loads(client.send_message(**params))["status"], "not_sent")
        with self.assertRaises(TypeError):
            client.send_message("旧接口")
        with self.assertRaises(TypeError):
            client.send_message(message_type="group", group_id=20)
        transport.send_request.assert_not_called()

    def test_public_invalid_request_does_not_start_a_child(self):
        from napcat_adapter import agent_api as api
        with patch.object(api, "_get_client") as factory:
            result = json.loads(api.send_message(message_type="group", message="缺少群号"))
        self.assertEqual(result["status"], "not_sent")
        factory.assert_not_called()

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
