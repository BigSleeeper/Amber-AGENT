"""通用 API 和完整事件通道：本地模拟 NapCat，不操作真实 QQ。"""

import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from websockets.asyncio.server import serve

from napcat_adapter import agent_api
from napcat_adapter.configuration import AdapterConfig
from napcat_adapter.ipc import AdapterProcess
from napcat_adapter.panel_model import PanelModel, Settings
from napcat_adapter.protocol import SYSTEM_CONVERSATION, raw_event_envelope, received_item, validate_send_command


def api_command(action="get_login_info", params=None):
    return dict(type="call_api", request_id="api-1", action=action,
                conversation_id=SYSTEM_CONVERSATION, params={} if params is None else params)


class FullApiPolicyTests(unittest.TestCase):
    def test_invalid_public_calls_do_not_start_process(self):
        with patch.object(agent_api, "_get_client") as factory:
            for args in (("", {}), ("bad ", {}), ("action", []), ("action", {"x": float("nan")})):
                self.assertEqual(json.loads(agent_api.call_api(*args))["status"], "not_sent")
            for options in (dict(timeout=True), dict(timeout=0), dict(timeout=float("inf")),
                            dict(on_frame=1), dict(stream="yes")):
                self.assertEqual(json.loads(agent_api.call_api("action", **options))["status"], "not_sent")
        factory.assert_not_called()

    def test_generic_actions_require_system_gate_and_keep_manual_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            emitted, submitted = [], []
            model = PanelModel(Settings(Path(directory) / "panel.json"), emitted.append, submitted.append)
            model.enqueue(api_command())
            self.assertEqual(emitted[-1]["status"], "not_sent")
            model.set_enabled([SYSTEM_CONVERSATION], True)
            request = dict(api_command(), request_id="second")
            model.enqueue(request)
            self.assertEqual(emitted[-1]["type"], "send_pending")
            self.assertFalse(submitted)
            model.release_outgoing(["second"])
            self.assertEqual(submitted[0]["action"], "get_login_info")
            model.enqueue(dict(api_command(), request_id="third"))
            model.set_enabled([SYSTEM_CONVERSATION], False)
            self.assertEqual(model.outgoing["third"]["state"], "not_sent")
            self.assertEqual(model.outgoing["second"]["state"], "sending")

    def test_raw_events_are_gated_snapshots_and_route_cannot_be_spoofed(self):
        with tempfile.TemporaryDirectory() as directory:
            emitted = []
            model = PanelModel(Settings(Path(directory) / "panel.json"), emitted.append, lambda _: None)
            raw = dict(post_type="request", flag="flag", extension={"values": [False, None]})
            envelope = raw_event_envelope(raw)
            raw["extension"]["values"].append(1)
            model.receive(envelope)
            self.assertFalse(model.incoming)
            model.set_enabled([SYSTEM_CONVERSATION], True)
            model.receive(envelope)
            self.assertFalse(emitted)
            model.release_incoming(list(model.incoming))
            self.assertEqual(emitted[0]["raw_event"]["extension"]["values"], [False, None])
            with self.assertRaises(ValueError):
                validate_send_command(dict(api_command(), conversation_id="qq:group:20"))


class FullApiTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.raw_events = [
            dict(post_type="message", message_type="private", user_id=99, self_id=99,
                 message_id=1, message="[CQ:image,file=x]", unknown={"a": [False, None]}),
            dict(post_type="message_sent", message_type="group", group_id=20, self_id=99),
            dict(post_type="request", request_type="friend", flag="friend-flag"),
            dict(post_type="request", request_type="group", sub_type="invite", flag="group-flag"),
            dict(post_type="notice", notice_type="group_recall", group_id=20),
            dict(post_type="notice", notice_type="group_ban", group_id=20),
            dict(post_type="notice", notice_type="group_upload", file={"id": "file"}),
            dict(post_type="meta_event", meta_event_type="lifecycle"),
            dict(post_type="future_event", extension={"kept": True}),
        ]
        self.raw_events.append(dict(self.raw_events[0]))
        async def peer(ws):
            for event in self.raw_events:
                await ws.send(json.dumps(event))
            async for raw in ws:
                request = json.loads(raw)
                self.requests.append(request)
                action = request["action"]
                base = dict(echo=request["echo"], status="ok", retcode=0, extension=[False, None])
                if action in ("download_file_stream", "stream_failure"):
                    for packet in (dict(type="stream", data_type="file_info", file_size=3),
                                   dict(type="reset", extension=True),
                                   dict(type="stream", data_type="file_chunk", index=0, data="YWJj", size=3)):
                        await ws.send(json.dumps(dict(base, stream="stream-action", data=packet)))
                    if action == "stream_failure":
                        await ws.send(json.dumps(dict(base, stream="stream-action", status="failed",
                                                      retcode=1200, data=dict(type="error"), wording="interrupted")))
                        continue
                    response = dict(base, stream="stream-action", data=dict(type="response", total_bytes=3))
                elif action in ("upload_file_stream", "future_single_stream"):
                    response = dict(base, stream="stream-action", data=dict(type="stream", status="chunk_received"))
                elif action == "never":
                    continue
                elif action == "disconnect":
                    await ws.close()
                    break
                elif action == "unsupported":
                    response = dict(base, status="failed", retcode=1404, data=None, wording="unsupported")
                else:
                    response = dict(base, data=request["params"].get("value", request["params"]))
                await ws.send(json.dumps(response))
        self.service = await serve(peer, "127.0.0.1", 0)
        url = f"ws://127.0.0.1:{self.service.sockets[0].getsockname()[1]}"
        self.transport = await asyncio.to_thread(AdapterProcess, AdapterConfig(
            url=url, gui=False, group_only=True, require_at=True, allowed_groups=(21,), api_timeout=1))
        self.client = agent_api.ProcessClient(transport=self.transport)
        self.received = []
        deadline = asyncio.get_running_loop().time() + 4
        while len(self.received) < len(self.raw_events) and asyncio.get_running_loop().time() < deadline:
            self.received.extend(self.client.napcat_receive())
            await asyncio.sleep(0.01)
        self.assertEqual(self.received, [received_item(raw) for raw in self.raw_events])

    async def asyncTearDown(self):
        await asyncio.to_thread(self.client.close)
        self.service.close()
        await self.service.wait_closed()

    async def call(self, action, params=None, **options):
        return json.loads(await asyncio.to_thread(self.client.call_api, action, params, **options))

    async def test_all_event_types_survive_without_message_filters(self):
        self.assertEqual(len(self.received), len(self.raw_events))
        self.assertEqual(self.client.napcat_receive(), [])
        self.assertTrue(all(set(item) == {"type", "content"} for item in self.received))

    async def test_native_actions_and_falsy_data_and_extensions_roundtrip(self):
        for action, value in zip(("get_login_info", "set_group_ban", "set_friend_add_request",
                                  "upload_group_file", "get_group_msg_history", "future_action"),
                                 (None, False, 0, [], {}, {"custom": [False, None]})):
            params = dict(value=value, extra={"unknown": True})
            result = await self.call(action, params)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["data"], value)
            self.assertEqual(result["raw_response"]["extension"], [False, None])
            self.assertEqual(self.requests[-1]["params"], params)
            self.assertEqual(self.requests[-1]["action"], action)

    async def test_download_frames_and_final_response_are_separate(self):
        frames, threads = [], []
        def callback(frame):
            frames.append(frame)
            threads.append(threading.get_ident())
        result = await self.call("download_file_stream", {"file_id": "file", "chunk_size": 65536},
                                 timeout=2, on_frame=callback)
        self.assertEqual([frame["data"]["type"] for frame in frames], ["stream", "reset", "stream"])
        self.assertEqual(frames[-1]["data"]["data"], "YWJj")
        self.assertEqual(result["data"], dict(type="response", total_bytes=3))
        self.assertTrue(result["stream_complete"])
        self.assertNotIn("frames", result)
        self.assertEqual(len(set(threads)), 1)
        self.assertNotEqual(threads[0], self.transport._reader.ident)
        collected = await self.call("download_file_stream")
        self.assertEqual(len(collected["frames"]), 3)

    async def test_upload_single_frame_and_future_override_do_not_hang(self):
        for action, options in (("upload_file_stream", {}), ("future_single_stream", dict(stream=False))):
            params = dict(stream_id="upload-1", chunk_index=0, total_chunks=1, chunk_data="YWJj",
                          file_size=3, filename="example.txt", file_retention=300000)
            result = await self.call(action, params, timeout=0.5, **options)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["data"]["status"], "chunk_received")
            self.assertEqual(self.requests[-1]["params"], params)

    async def test_failures_preserve_receipts_and_timeout_never_retries(self):
        failure = await self.call("unsupported")
        self.assertEqual(failure["status"], "failed")
        self.assertEqual(failure["raw_response"]["retcode"], 1404)
        stream = await self.call("stream_failure")
        self.assertEqual(stream["status"], "failed")
        self.assertFalse(stream["stream_complete"])
        self.assertEqual(len(stream["frames"]), 3)
        result = await self.call("never", timeout=0.05)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(sum(request["action"] == "never" for request in self.requests), 1)
        result = await self.call("disconnect", timeout=0.5)
        self.assertEqual(result["status"], "unknown")

    async def test_callback_error_does_not_discard_final_business_result(self):
        def broken(frame):
            raise RuntimeError("callback")
        result = await self.call("download_file_stream", on_frame=broken)
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["stream_complete"])
        self.assertIn("RuntimeError", result["stream_error"])
        self.assertEqual(result["data"]["total_bytes"], 3)
