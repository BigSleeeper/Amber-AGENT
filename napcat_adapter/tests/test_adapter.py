import asyncio
import json
import sys
import unittest
from unittest.mock import patch

from napcat_adapter.client import NapCatAdapter
from napcat_adapter.protocol import normalize_event, validate_poke_params, validate_send_params
from napcat_adapter.ipc import AdapterProcess
from napcat_adapter.configuration import AdapterConfig


def event(group=20, user=10, message_id=1, segments=None):
    return dict(post_type="message", message_type="group", group_id=group,
                user_id=user, self_id=99, message_id=message_id, time=123,
                sender=dict(card="群友", nickname="昵称"),
                message=segments or [dict(type="text", data=dict(text="你好"))])


def command(request_id="r1", conversation_id="qq:group:20", text="你好"):
    parts = conversation_id.split(":")
    kind = parts[1] if len(parts) == 3 else "group"
    target = int(parts[2]) if len(parts) == 3 else 20
    return dict(type="send_message", request_id=request_id,
                conversation_id=conversation_id, text=text,
                params=dict(message_type=kind, message=[dict(type="text", data=dict(text=text))],
                            **{"group_id" if kind == "group" else "user_id": target}))


def poke_event(group=20, user=10, target=99, sender=None):
    raw = dict(post_type="notice", notice_type="notify", sub_type="poke",
               self_id=99, time=123, user_id=user, target_id=target,
               raw_info=[dict(type="nor", txt="拍了拍")], extension={"kept": True})
    if group is not None:
        raw["group_id"] = group
    if sender is not None:
        raw["sender_id"] = sender
    return raw


def poke_command(request_id="p1", group=20, user=10, target=None):
    params = dict(user_id=user)
    if group is not None:
        params["group_id"] = group
    if target is not None:
        params["target_id"] = target
    conversation = f"qq:group:{group}" if group is not None else f"qq:private:{user}"
    return dict(type="send_poke", request_id=request_id, conversation_id=conversation, params=params)


class ProtocolTests(unittest.TestCase):
    def test_poke_notice_preserves_fields_and_private_actor(self):
        raw = poke_event()
        notice = normalize_event(raw)
        self.assertEqual(notice["type"], "notice")
        self.assertEqual(notice["conversation_id"], "qq:group:20")
        self.assertIsNone(notice["message_id"])
        self.assertEqual(notice["raw_event"], raw)
        raw["extension"]["kept"] = False
        self.assertTrue(notice["raw_event"]["extension"]["kept"])
        private = normalize_event(poke_event(group=None, sender=11))
        self.assertEqual(private["conversation_id"], "qq:private:10")
        self.assertEqual(private["sender_id"], "11")
        self.assertEqual(private["metadata"]["target_id"], "99")

    def test_poke_obeys_filters_without_requiring_message_segments(self):
        self.assertIsNone(normalize_event(poke_event(), allowed_groups={21}))
        self.assertIsNone(normalize_event(poke_event(), allowed_users={11}))
        self.assertIsNone(normalize_event(poke_event(group=None), group_only=True))
        self.assertIsNone(normalize_event(poke_event(group=None, sender=99, target=10)))
        self.assertIsNone(normalize_event(poke_event(user=99, target=10)))
        self.assertIsNone(normalize_event(poke_event(target=11), require_at=True))
        self.assertIsNotNone(normalize_event(poke_event(), require_at=True))
        self.assertIsNone(normalize_event(dict(poke_event(), notice_type="group_recall")))

    def test_invalid_poke_params_are_rejected_and_explicit_target_is_preserved(self):
        for params in (None, {}, dict(user_id=True), dict(user_id=0), dict(user_id="bad"),
                       dict(user_id=10, group_id=0), dict(user_id=10, target_id=-1),
                       dict(user_id=10, message="not a poke field")):
            with self.subTest(params=params), self.assertRaises(ValueError):
                validate_poke_params(params)
        conversation, params = validate_poke_params(dict(user_id="10", target_id="99"))
        self.assertEqual(conversation, "qq:private:10")
        self.assertEqual(params["target_id"], "99")

    def test_missing_or_invalid_send_fields_are_rejected(self):
        valid = dict(message_type="group", group_id=20,
                     message=[dict(type="text", data=dict(text="你好"))])
        invalid = [None, {}, dict(valid, message_type="other"), dict(valid, group_id=None),
                   dict(valid, group_id=True), dict(valid, group_id=1.5), dict(valid, group_id=0),
                   dict(valid, user_id=10), dict(valid, message=None), dict(valid, message=[]),
                   dict(valid, message=" "), dict(valid, message=[dict(type="image")]),
                   dict(valid, auto_escape=1), dict(valid, extra=float("nan"))]
        for params in invalid:
            with self.subTest(params=params), self.assertRaises(ValueError):
                validate_send_params(params)

    def test_complete_raw_event_including_unknown_nested_fields_is_preserved(self):
        raw = dict(event(), group_name="测试群", message_seq=52, real_seq="53",
                   raw_message="你好", message_format="array", font=14,
                   extension={"items": [None, False, {"value": "扩展"}]})
        raw["sender"].update(role="admin", title="头衔", custom=42)
        normalized = normalize_event(raw)
        self.assertEqual(normalized["raw_event"], raw)
        raw["sender"]["role"] = "member"
        raw["extension"]["items"].append("later")
        normalized["metadata"]["segments"][0]["data"]["text"] = "changed"
        self.assertEqual(normalized["raw_event"]["sender"]["role"], "admin")
        self.assertEqual(len(normalized["raw_event"]["extension"]["items"]), 3)
        self.assertEqual(normalized["raw_event"]["message"][0]["data"]["text"], "你好")

    def test_group_shared_conversation(self):
        a, b = normalize_event(event()), normalize_event(event(user=11))
        self.assertEqual(a["conversation_id"], b["conversation_id"])
        self.assertNotEqual(a["sender_id"], b["sender_id"])
        self.assertNotEqual(a["conversation_id"], normalize_event(event(group=21))["conversation_id"])
        self.assertNotIn("role", a)

    def test_filters_and_self(self):
        self.assertIsNone(normalize_event(event(user=99)))
        self.assertIsNone(normalize_event(event(), allowed_groups={21}))
        self.assertIsNone(normalize_event(event(), allowed_users={11}))
        self.assertIsNone(normalize_event(event(), require_at=True))
        self.assertIsNone(normalize_event(dict(post_type="meta_event")))

    def test_private_filter(self):
        raw = dict(event(), message_type="private")
        self.assertEqual(normalize_event(raw)["conversation_id"], "qq:private:10")
        self.assertIsNone(normalize_event(raw, group_only=True))

    def test_mentions_reply_and_media(self):
        raw = event(segments=[
            dict(type="reply", data=dict(id="8")),
            dict(type="at", data=dict(qq="99")),
            dict(type="at", data=dict(qq="42")),
            dict(type="image", data=dict(file="image.png")),
        ])
        message = normalize_event(raw, require_at=True)
        self.assertTrue(message["metadata"]["mentioned_bot"])
        self.assertEqual(message["metadata"]["reply_to"], "8")
        self.assertEqual(message["metadata"]["sender_name"], "群友")
        self.assertIn("@42", message["text"])
        self.assertIn("[图片]", message["text"])

    def test_string_format_rejected(self):
        with self.assertRaises(ValueError):
            normalize_event(dict(event(), message="[CQ:at,qq=99] hello"))


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_poke_uses_dedicated_action_and_void_success_receipt(self):
        adapter = NapCatAdapter("ws://localhost")
        requests = []
        class Socket:
            async def send(self, raw):
                request = json.loads(raw)
                requests.append(request)
                adapter._pending[request["echo"]].set_result(dict(status="ok", retcode=0, data=None))
        adapter._ws = Socket()
        for request in (poke_command(), poke_command(group=None, target=99)):
            result = await adapter._send(request, adapter._ws)
            self.assertTrue(result["success"])
            self.assertEqual(result["action"], "send_poke")
            self.assertNotIn("message_id", result)
            self.assertEqual(requests[-1]["action"], "send_poke")
            self.assertEqual(requests[-1]["params"], request["params"])
        self.assertFalse(adapter._pending)

    async def test_poke_failure_timeout_and_filter_do_not_retry(self):
        adapter = NapCatAdapter("ws://localhost", api_timeout=0.01, allowed_groups={20})
        class Socket:
            calls = 0
            respond = True
            async def send(self, raw):
                self.calls += 1
                if self.respond:
                    adapter._pending[json.loads(raw)["echo"]].set_result(dict(
                        status="failed", retcode=1200, message="packetBackend 不可用"))
        ws = Socket()
        adapter._ws = ws
        self.assertEqual((await adapter._send(poke_command(group=21), ws))["status"], "not_sent")
        self.assertEqual(ws.calls, 0)
        self.assertEqual((await adapter._send(poke_command(), ws))["status"], "failed")
        ws.respond = False
        self.assertEqual((await adapter._send(poke_command(), ws))["status"], "unknown")
        self.assertEqual(ws.calls, 2)
        self.assertFalse(adapter._pending)

    async def test_consecutive_pokes_without_unique_ids_are_not_dropped(self):
        adapter = NapCatAdapter("ws://localhost")
        emitted = []
        adapter._emit = emitted.append
        class Socket:
            def __aiter__(self):
                async def frames():
                    for item in (poke_event(), poke_event(), event(), event()):
                        yield json.dumps(item)
                return frames()
        await adapter._receive(Socket())
        self.assertEqual([item["type"] for item in emitted], ["event"] * 4)
        self.assertEqual([item["raw_event"] for item in emitted],
                         [poke_event(), poke_event(), event(), event()])

    async def test_structured_messages_and_optional_params_reach_napcat_unchanged(self):
        adapter = NapCatAdapter("ws://localhost")
        requests = []
        class Socket:
            async def send(self, raw):
                request = json.loads(raw)
                requests.append(request)
                adapter._pending[request["echo"]].set_result(dict(status="ok", retcode=0, data={}))
        adapter._ws = Socket()
        for message in ([dict(type="at", data=dict(qq="all")),
                         dict(type="image", data=dict(file="base64://test", custom="kept"))],
                        dict(type="text", data=dict(text="单消息段")),
                        "[CQ:at,qq=all]你好"):
            request = command()
            request["params"].update(message=message, auto_escape=False, source="来源")
            result = await adapter._send(request, adapter._ws)
            self.assertTrue(result["success"])
            self.assertEqual(requests[-1]["params"], request["params"])
        request = command()
        request["params"]["group_id"] = 21
        self.assertEqual((await adapter._send(request, adapter._ws))["status"], "not_sent")
        self.assertEqual(len(requests), 3)

    async def test_ack_and_outbound_plain_text(self):
        adapter = NapCatAdapter("ws://localhost")
        class Socket:
            async def send(self, raw):
                request = json.loads(raw)
                self.request = request
                adapter._pending[request["echo"]].set_result(dict(
                    status="ok", retcode=0, data=dict(message_id=456)))
        ws = Socket()
        adapter._ws = ws
        result = await adapter._send(command(text="[CQ:at,qq=all]"), ws)
        self.assertTrue(result["success"])
        self.assertEqual(result["message_id"], "456")
        self.assertEqual(ws.request["params"]["group_id"], 20)
        self.assertEqual(ws.request["params"]["message"][0]["type"], "text")
        self.assertFalse(adapter._pending)

    async def test_known_failure_and_async_ack(self):
        adapter = NapCatAdapter("ws://localhost")
        class Socket:
            status = "failed"
            async def send(self, raw):
                adapter._pending[json.loads(raw)["echo"]].set_result(dict(
                    status=self.status, retcode=100, data=None, wording="denied"))
        ws = Socket()
        adapter._ws = ws
        self.assertEqual((await adapter._send(command(), ws))["status"], "failed")
        ws.status = "async"
        self.assertEqual((await adapter._send(command(), ws))["status"], "unknown")

    async def test_timeout_no_retry(self):
        adapter = NapCatAdapter("ws://localhost", api_timeout=0.01)
        class Socket:
            calls = 0
            async def send(self, raw):
                self.calls += 1
        ws = Socket()
        adapter._ws = ws
        result = await adapter._send(command(), ws)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(ws.calls, 1)
        self.assertFalse(adapter._pending)

    async def test_private_send_action(self):
        adapter = NapCatAdapter("ws://localhost")
        requests = []
        class Socket:
            async def send(self, raw):
                request = json.loads(raw)
                requests.append(request)
                adapter._pending[request["echo"]].set_result(dict(status="ok", retcode=0, data={}))
        adapter._ws = Socket()
        result = await adapter._send(command(conversation_id="qq:private:10"), adapter._ws)
        self.assertTrue(result["success"])
        self.assertEqual(requests[0]["action"], "send_private_msg")
        self.assertEqual(requests[0]["params"]["user_id"], 10)

    async def test_offline_stale_and_invalid_targets(self):
        adapter = NapCatAdapter("ws://localhost", allowed_groups={20}, group_only=True)
        for request in (command(), command(text=" "), command(conversation_id="qq:group:21"),
                        command(conversation_id="qq:private:10"), command(conversation_id="other:20")):
            self.assertEqual((await adapter._send(request, None))["status"], "not_sent")
        adapter._ws = object()
        self.assertEqual((await adapter._send(command(), object()))["status"], "not_sent")

    async def test_each_frame_is_emitted_once_and_echo_is_separate(self):
        adapter = NapCatAdapter("ws://localhost")
        emitted = []
        adapter._emit = emitted.append
        future = asyncio.get_running_loop().create_future()
        adapter._pending["echo"] = future
        class Socket:
            def __aiter__(self):
                async def frames():
                    for item in ("bad-json", json.dumps(event()), json.dumps(event()),
                                 json.dumps(dict(echo="echo", status="ok", retcode=0)),
                                 json.dumps(dict(echo="expired", status="failed", retcode=1404))):
                        yield item
                return frames()
        await adapter._receive(Socket())
        self.assertEqual([item["type"] for item in emitted], ["event", "event"])
        self.assertEqual(future.result()["status"], "ok")

    async def test_request_dedup_and_cached_receipt(self):
        adapter = NapCatAdapter("ws://localhost")
        adapter._commands = asyncio.Queue(maxsize=1)
        adapter._stopped = asyncio.Event()
        output = []
        adapter._emit = output.append
        adapter.accept_command(command())
        adapter.accept_command(command())
        self.assertEqual(adapter._commands.qsize(), 1)
        worker = asyncio.create_task(adapter._sender())
        try:
            await asyncio.wait_for(adapter._commands.join(), 1)
            adapter.accept_command(command())
            self.assertEqual(len(output), 2)
            self.assertEqual(output[0], output[1])
            self.assertTrue(adapter._commands.empty())
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)


try:
    from websockets.asyncio.server import serve
except ImportError:
    serve = None


@unittest.skipIf(serve is None, "需要安装 websockets")
class IPCIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_headless_poke_roundtrip_through_public_api_and_real_pipes(self):
        from napcat_adapter.agent_api import ProcessClient
        raw_notice = poke_event(group=None, sender=11)
        requests = []
        async def server(ws):
            await ws.send(json.dumps(raw_notice))
            request = json.loads(await ws.recv())
            requests.append(request)
            await ws.send(json.dumps(dict(echo=request["echo"], status="ok", retcode=0, data=None)))
            await ws.wait_closed()
        async with serve(server, "127.0.0.1", 0) as service:
            url = f"ws://127.0.0.1:{service.sockets[0].getsockname()[1]}"
            transport = await asyncio.to_thread(AdapterProcess, AdapterConfig(url=url, gui=False, group_only=False))
            client = ProcessClient(transport=transport)
            try:
                deadline = asyncio.get_running_loop().time() + 4
                batch = []
                while not batch and asyncio.get_running_loop().time() < deadline:
                    batch = client.napcat_receive()
                    await asyncio.sleep(0.01)
                self.assertEqual(len(batch), 1)
                received = batch[0]
                self.assertEqual(received["type"], "notice")
                self.assertEqual(received["content"], raw_notice)
                self.assertEqual(client.napcat_receive(), [])
                result = json.loads(await asyncio.to_thread(client.send_poke, user_id="10", target_id="11"))
                self.assertTrue(result["success"])
                self.assertEqual(result["conversation_id"], "qq:private:10")
                self.assertEqual(requests[0]["action"], "send_poke")
                self.assertEqual(requests[0]["params"], dict(user_id="10", target_id="11"))
            finally:
                await asyncio.to_thread(client.close)

    async def test_real_subprocess_jsonl(self):
        received = []
        raw_event = dict(event(), group_name="管道测试群", real_seq="123456",
                         extension={"nested": [1, None, {"kept": True}]})
        peer_done = asyncio.Event()
        async def server(ws):
            self.assertEqual(ws.request.headers.get("Authorization"), "Bearer test-token")
            await ws.send(json.dumps(raw_event))
            request = json.loads(await ws.recv())
            received.append(request)
            await ws.send(json.dumps(dict(echo=request["echo"], status="ok", retcode=0,
                                          data=dict(message_id=789))))
            await peer_done.wait()
        async with serve(server, "127.0.0.1", 0) as service:
            port = service.sockets[0].getsockname()[1]
            with patch.dict("os.environ", NAPCAT_TOKEN="test-token"):
                transport = await asyncio.to_thread(AdapterProcess, AdapterConfig(url=f"ws://127.0.0.1:{port}", send_timeout=2, gui=False))
            try:
                message = await asyncio.to_thread(transport.receive, 4)
                self.assertEqual(message["conversation_id"], "qq:group:20")
                self.assertEqual(message["raw_event"], raw_event)
                params = dict(message_type="group", group_id=20, auto_escape=False,
                              message=[dict(type="text", data=dict(text="中文回复")),
                                       dict(type="at", data=dict(qq="10"))])
                result = await asyncio.to_thread(transport.send_request, params)
                self.assertEqual(result["message_id"], "789")
                self.assertEqual(received[0]["params"]["group_id"], 20)
                self.assertEqual(received[0]["params"]["message"][0]["data"]["text"], "中文回复")
                self.assertEqual(received[0]["params"], params)
            finally:
                peer_done.set()
                await asyncio.to_thread(transport.close)
            self.assertEqual(transport.process.returncode, 0)
            self.assertFalse(transport._pending)

    async def test_reconnect_without_resending(self):
        connections, sent = [], []
        second_connected = asyncio.Event()
        async def server(ws):
            connections.append(ws)
            await ws.send(json.dumps(event(message_id=len(connections))))
            if len(connections) == 1:
                sent.append(json.loads(await ws.recv()))
                await ws.close()  # 发送后断线，没有最终回执。
            else:
                second_connected.set()
                await ws.wait_closed()
        async with serve(server, "127.0.0.1", 0) as service:
            port = service.sockets[0].getsockname()[1]
            transport = await asyncio.to_thread(AdapterProcess, AdapterConfig(url=f"ws://127.0.0.1:{port}", reconnect_delay=0.05, send_timeout=2, gui=False))
            try:
                first = await asyncio.to_thread(transport.receive, 4)
                result = await asyncio.to_thread(transport.send, first["conversation_id"], "一次")
                self.assertEqual(result["status"], "unknown")
                await asyncio.wait_for(second_connected.wait(), 3)
                second = await asyncio.to_thread(transport.receive, 3)
                self.assertEqual(second["message_id"], "2")
                self.assertEqual(len(sent), 1)
            finally:
                await asyncio.to_thread(transport.close)


class EntryTests(unittest.TestCase):
    def test_headless_entry_help(self):
        import subprocess
        for module in ("napcat_adapter",):
            output = subprocess.check_output([sys.executable, "-m", module, "--help"])
            self.assertIn(b"usage:", output)


if __name__ == "__main__":
    unittest.main()
