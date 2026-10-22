"""公共发送、真实策略模型与 IPC 回执状态机之间的模式契约。"""

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock

from napcat_adapter.agent_api import ProcessClient
from napcat_adapter.configuration import AdapterConfig
from napcat_adapter.ipc import AdapterProcess
from napcat_adapter.panel_model import PanelModel, Settings
from napcat_adapter.protocol import send_result


class SendModeTests(unittest.TestCase):
    def setUp(self):
        directory = Path(__file__).resolve().parents[1] / ".artifacts"
        directory.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=directory)
        self.addCleanup(temporary.cleanup)
        self.settings = Settings(Path(temporary.name) / "panel.json")
        self.transport = AdapterProcess.__new__(AdapterProcess)
        self.transport.config = AdapterConfig(send_timeout=2)
        self.transport._condition = threading.Condition()
        self.transport._pending = {}
        self.transport._closing = self.transport._reader_done = False
        self.transport.process = Mock()
        self.transport.process.poll.return_value = None
        self.submitted = []
        self.submitted_event = threading.Event()

        def submit(command):
            self.submitted.append(command)
            self.submitted_event.set()

        self.model = PanelModel(self.settings, self.transport._handle, submit, capacity=2)
        self.model.set_enabled(["qq:group:20", "qq:private:10", "qq:system:0"], True)
        self.transport._write = self.model.enqueue
        self.client = ProcessClient(transport=self.transport)

    def send(self, **options):
        return json.loads(self.client.send_message(message_type="group", group_id=20,
                                                   message="消息", **options))

    def test_manual_group_and_private_return_success_while_still_pending(self):
        for params in (dict(message_type="group", group_id=20),
                       dict(message_type="private", user_id=10)):
            with self.subTest(params=params):
                result = json.loads(self.client.send_message(**params, message="消息"))
                self.assertTrue(result["success"])
                self.assertEqual(result["status"], "ok")
                self.assertEqual(result["mode"], "manual")
                self.assertIs(result["queued"], True)
                self.assertIn("等待人工发送", result["detail"])
                self.assertNotIn("message_id", result)
                self.assertNotIn("raw_response", result)
                self.assertEqual(self.model.outgoing[result["request_id"]]["state"], "pending")
        self.assertFalse(self.submitted)
        self.assertFalse(self.transport._pending)

    def test_manual_rejects_invalid_disabled_and_full_queue(self):
        invalid = json.loads(self.client.send_message(message_type="group", message="消息"))
        self.assertEqual(invalid["status"], "not_sent")
        self.assertFalse(self.model.outgoing)
        self.model.set_enabled(["qq:group:20"], False)
        self.assertEqual(self.send()["status"], "not_sent")
        self.model.set_enabled(["qq:group:20"], True)
        first, second, full = self.send(), self.send(), self.send()
        self.assertTrue(first["success"] and second["success"])
        self.assertEqual(full["status"], "not_sent")
        self.assertFalse(full["success"])
        self.assertNotIn("queued", full)
        self.assertFalse(self.submitted)

    def test_later_manual_deletion_and_failure_only_update_gui(self):
        deleted = self.send()
        self.model.delete("send", [deleted["request_id"]])
        self.assertEqual(self.model.results[deleted["request_id"]]["status"], "not_sent")
        accepted = self.send()
        key = accepted["request_id"]
        self.model.set_mode("send", "auto")
        self.assertEqual(len(self.submitted), 1)
        result = send_result(self.submitted[0], "failed", error="NapCat 拒绝")
        self.model.complete(result)
        self.assertEqual(self.model.outgoing[key]["state"], "failed")
        self.assertTrue(deleted["success"] and accepted["success"])
        self.assertFalse(self.transport._pending)

    def test_immediate_manual_deletion_cannot_overwrite_acceptance(self):
        def enqueue_and_delete(command):
            self.model.enqueue(command)
            self.model.delete("send", [command["request_id"]])
        self.transport._write = enqueue_and_delete
        result = self.send()
        self.assertTrue(result["success"])
        self.assertTrue(result["queued"])
        self.assertEqual(self.model.results[result["request_id"]]["status"], "not_sent")

    def test_shutdown_after_acceptance_keeps_success(self):
        def enqueue_and_shutdown(command):
            self.model.enqueue(command)
            self.model.shutdown()
            self.transport._closing = True
        self.transport._write = enqueue_and_shutdown
        result = self.send()
        self.assertTrue(result["success"])
        self.assertEqual(self.model.outgoing[result["request_id"]]["state"], "not_sent")

    def test_auto_waits_and_returns_each_real_outcome(self):
        self.settings.send_mode = "auto"
        for status in ("ok", "failed", "unknown", "not_sent"):
            with self.subTest(status=status):
                self.submitted_event.clear()
                returned = []
                thread = threading.Thread(target=lambda: returned.append(self.send()))
                thread.start()
                try:
                    self.assertTrue(self.submitted_event.wait(1))
                    self.assertTrue(thread.is_alive())
                    command = self.submitted[-1]
                    expected = send_result(command, status, message_id=123 if status == "ok" else None)
                    if status in ("ok", "failed"):
                        expected.update(data={"message_id": 123} if status == "ok" else None,
                                        raw_response={"status": status, "retcode": 0 if status == "ok" else 1200})
                    self.model.complete(expected)
                    thread.join(1)
                    self.assertEqual(returned, [expected])
                    self.assertNotIn("queued", returned[0])
                finally:
                    self.model.shutdown()
                    thread.join(3)

    def test_manual_advanced_calls_still_wait_for_real_results(self):
        for call in (lambda: self.client.send_poke(user_id=10, group_id=20),
                     lambda: self.client.call_api("get_login_info")):
            with self.subTest(call=call):
                pending = threading.Event()
                returned = []
                def enqueue(command):
                    self.model.enqueue(command)
                    pending.set()
                self.transport._write = enqueue
                thread = threading.Thread(target=lambda: returned.append(json.loads(call())))
                thread.start()
                try:
                    self.assertTrue(pending.wait(1))
                    self.assertTrue(thread.is_alive())
                    key = next(key for key, row in self.model.outgoing.items() if row["state"] == "pending")
                    self.model.delete("send", [key])
                    thread.join(1)
                    self.assertEqual(returned[0]["status"], "not_sent")
                    self.assertNotIn("queued", returned[0])
                finally:
                    self.model.shutdown()
                    thread.join(3)


if __name__ == "__main__":
    unittest.main()
