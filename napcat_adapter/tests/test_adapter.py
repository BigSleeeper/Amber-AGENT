import asyncio
import json
import sys
import unittest
from unittest.mock import patch

from napcat_adapter.client import NapCatAdapter
from napcat_adapter.protocol import normalize_event
from napcat_adapter.ipc import AdapterProcess
from napcat_adapter.configuration import AdapterConfig


def event(group=20, user=10, message_id=1, segments=None):
    return dict(post_type="message", message_type="group", group_id=group,
                user_id=user, self_id=99, message_id=message_id, time=123,
                sender=dict(card="群友", nickname="昵称"),
                message=segments or [dict(type="text", data=dict(text="你好"))])


def command(request_id="r1", conversation_id="qq:group:20", text="你好"):
    return dict(type="send_message", request_id=request_id,
                conversation_id=conversation_id, text=text)


class ProtocolTests(unittest.TestCase):
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

    async def test_event_dedup_and_echo(self):
        adapter = NapCatAdapter("ws://localhost")
        emitted = []
        adapter._emit = emitted.append
        future = asyncio.get_running_loop().create_future()
        adapter._pending["echo"] = future
        class Socket:
            def __aiter__(self):
                async def frames():
                    for item in ("bad-json", json.dumps(event()), json.dumps(event()),
                                 json.dumps(dict(echo="echo", status="ok", retcode=0))):
                        yield item
                return frames()
        await adapter._receive(Socket())
        self.assertEqual(len(emitted), 1)
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
    async def test_real_subprocess_jsonl(self):
        received = []
        peer_done = asyncio.Event()
        async def server(ws):
            self.assertEqual(ws.request.headers.get("Authorization"), "Bearer test-token")
            await ws.send(json.dumps(event()))
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
                result = await asyncio.to_thread(transport.send, message["conversation_id"], "中文回复")
                self.assertEqual(result["message_id"], "789")
                self.assertEqual(received[0]["params"]["group_id"], 20)
                self.assertEqual(received[0]["params"]["message"][0]["data"]["text"], "中文回复")
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
