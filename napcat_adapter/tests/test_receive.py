"""统一批量接收格式、单队列原子取出及 GUI 策略。"""

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock

from napcat_adapter import NapCatReceiveType, napcat_receive
from napcat_adapter.ipc import AdapterProcess, AdapterProcessError
from napcat_adapter.configuration import AdapterConfig
from napcat_adapter.panel_model import PanelModel, Settings
from napcat_adapter.protocol import raw_event_envelope, received_item


class UnifiedReceiveTests(unittest.TestCase):
    def transport(self, capacity=500):
        # 单独验证接收状态机；实际子进程链路另由 full_api/panel 测试覆盖。
        transport = AdapterProcess.__new__(AdapterProcess)
        transport.config = AdapterConfig(inbox_capacity=capacity)
        transport._condition = threading.Condition()
        from collections import deque
        transport._events = deque()
        transport._closing = transport._reader_done = False
        transport.process = Mock()
        transport.process.poll.return_value = None
        transport.dropped_messages = transport.dropped_events = 0
        transport._pending = {}
        return transport

    def test_type_enum_and_exact_dictionary_shape_and_unknown_fallback(self):
        self.assertTrue(callable(napcat_receive))
        self.assertEqual({kind.value for kind in NapCatReceiveType},
                         {"message", "message_sent", "notice", "request", "meta_event", "unknown"})
        for kind in NapCatReceiveType:
            raw = {"post_type": kind.value, "extension": [False, None, 0, {}, []]}
            item = received_item(raw)
            self.assertEqual(set(item), {"type", "content"})
            self.assertIs(type(item["type"]), str)
            self.assertEqual(item, {"type": kind.value, "content": raw})
        for post_type in ("future_type", None, {}, []):
            raw = {"post_type": post_type}
            self.assertEqual(received_item(raw), {"type": "unknown", "content": raw})

    def test_batch_is_ordered_consumed_once_and_independent_snapshot(self):
        transport = self.transport()
        raws = [{"post_type": kind, "order": index, "nested": {"value": [False, None]}}
                for index, kind in enumerate(("message", "notice", "request", "meta_event", "message_sent", "future"))]
        for raw in raws:
            envelope = raw_event_envelope(raw)
            transport._handle(envelope)
        transport._handle({"type": "status", "status": "connected"})
        transport._handle({"type": "send_result", "request_id": "unrelated", "status": "ok"})
        batch = transport.napcat_receive()
        self.assertEqual(batch, [received_item(raw) for raw in raws])
        self.assertEqual(transport.napcat_receive(), [])
        raws[0]["nested"]["value"].append(1)
        self.assertEqual(batch[0]["content"]["nested"]["value"], [False, None])
        transport._handle(raw_event_envelope({"post_type": "notice", "later": True}))
        self.assertEqual(transport.napcat_receive(), [{"type": "notice", "content": {"post_type": "notice", "later": True}}])

    def test_simultaneous_polls_never_deliver_the_same_item_twice(self):
        transport = self.transport()
        for index in range(100):
            transport._handle(raw_event_envelope({"post_type": "notice", "index": index}))
        batches = []
        barrier = threading.Barrier(3)
        def poll():
            barrier.wait()
            batches.append(transport.napcat_receive())
        threads = [threading.Thread(target=poll) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
        indices = [item["content"]["index"] for batch in batches for item in batch]
        self.assertEqual(indices, list(range(100)))
        self.assertEqual(transport.napcat_receive(), [])

    def test_bounded_queue_and_exit_behavior_and_malformed_ipc(self):
        transport = self.transport(capacity=1)
        transport._handle(dict(type="event", source="qq", text="bad", conversation_id="qq:system:0"))
        self.assertEqual(transport.napcat_receive(), [])
        for index in range(2):
            transport._handle(raw_event_envelope({"post_type": "meta_event", "index": index}))
        self.assertEqual(transport.dropped_events, 1)
        transport._reader_done = True
        self.assertEqual(transport.napcat_receive()[0]["content"]["index"], 0)
        with self.assertRaises(AdapterProcessError):
            transport.napcat_receive()

    def test_gui_routes_temp_private_sent_message_and_system_events(self):
        cases = [
            ({"post_type": "message", "message_type": "private", "group_id": 20, "user_id": 10}, "qq:private:10"),
            ({"post_type": "message_sent", "message_type": "private", "user_id": 99, "self_id": 99, "target_id": 10}, "qq:private:10"),
            ({"post_type": "message_sent", "message_type": "group", "target_id": 20}, "qq:group:20"),
            ({"post_type": "notice", "group_id": 20, "user_id": 10}, "qq:group:20"),
            ({"post_type": "request", "request_type": "friend", "user_id": 10}, "qq:private:10"),
            ({"post_type": "meta_event", "meta_event_type": "heartbeat"}, "qq:system:0"),
        ]
        for raw, route in cases:
            envelope = raw_event_envelope(raw)
            self.assertEqual(envelope["conversation_id"], route)
            self.assertEqual(envelope["raw_event"], raw)

    def test_gui_release_is_once_even_with_both_global_and_group_enabled(self):
        with tempfile.TemporaryDirectory() as directory:
            delivered = []
            model = PanelModel(Settings(Path(directory) / "settings.json"), delivered.append, lambda _: None)
            envelope = raw_event_envelope({"post_type": "notice", "group_id": 20})
            model.receive(envelope)
            self.assertFalse(model.incoming)
            model.set_enabled(["qq:system:0", "qq:group:20"], True)
            model.receive(envelope)
            keys = list(model.incoming)
            self.assertEqual(len(keys), 1)
            self.assertFalse(delivered)
            model.release_incoming(keys)
            model.release_incoming(keys)
            self.assertEqual(len(delivered), 1)
            self.assertEqual(delivered[0]["conversation_id"], "qq:group:20")
            model.receive(raw_event_envelope({"post_type": "meta_event"}))
            model.release_incoming(list(model.incoming))
            self.assertEqual(len(delivered), 2)
