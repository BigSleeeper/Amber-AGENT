"""使用真实匿名管道与轻量假子进程验证 IPC 生命周期，无需 QQ 或模型。"""

import json
import io
import logging
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from napcat_adapter.configuration import AdapterConfig
from napcat_adapter.ipc import AdapterProcess, AdapterProcessError


FIXTURE = r'''
import json, sys, time
def emit(event):
    print(json.dumps(event, ensure_ascii=False), flush=True)
emit(dict(type="ready", protocol_version=1))
for group in (20, 21):
    emit(dict(type="message", source="qq", conversation_id=f"qq:group:{group}",
              text="中文消息", message_id=str(group), sender_id="10", metadata={}))
for line in sys.stdin:
    command = json.loads(line)
    if command["type"] == "shutdown":
        break
    print("send-request", file=sys.stderr, flush=True)
    if command["text"] == "crash":
        sys.exit(7)
    if command["text"] == "timeout":
        continue
    emit(dict(type="send_result", request_id=command["request_id"],
              conversation_id=command["conversation_id"], status="ok", success=True,
              message_id="777"))
'''


class IpcTests(unittest.TestCase):
    def spawn(self, source=FIXTURE, config=None):
        real_popen = subprocess.Popen
        created = []
        def popen(args, **kwargs):
            self.assertEqual(args[:4], [sys.executable, "-u", "-m", "napcat_adapter"])
            self.assertFalse(kwargs.get("shell", False))
            process = real_popen([sys.executable, "-u", "-c", source], **kwargs)
            created.append(process)
            return process
        with patch("napcat_adapter.ipc.subprocess.Popen", side_effect=popen):
            try:
                result = AdapterProcess(config or AdapterConfig(send_timeout=0.1))
            except BaseException:
                self.assertTrue(all(process.poll() is not None for process in created))
                raise
        self.addCleanup(result.close)
        return result

    def test_push_receive_send_and_close(self):
        transport = self.spawn()
        self.assertTrue(transport.alive)
        first, second = transport.receive(2), transport.receive(2)
        self.assertEqual(first["text"], "中文消息")
        self.assertEqual(second["conversation_id"], "qq:group:21")
        result = transport.send(first["conversation_id"], "中文回复\n第二行")
        self.assertEqual(result["message_id"], "777")
        self.assertEqual(result["conversation_id"], "qq:group:20")
        self.assertFalse(transport._pending)
        transport.close()
        transport.close()
        self.assertEqual(transport.process.returncode, 0)
        self.assertFalse(transport._reader.is_alive())
        self.assertFalse(transport._errors.is_alive())

    def test_idle_wait_and_no_protocol_polling(self):
        transport = self.spawn()
        transport.receive(2)
        transport.receive(2)
        started = time.monotonic()
        self.assertIsNone(transport.receive(0.05))
        self.assertGreaterEqual(time.monotonic() - started, 0.04)
        transport.close()
        self.assertEqual(list(transport._stderr_tail), [])

    def test_send_timeout_does_not_resend(self):
        transport = self.spawn()
        self.assertEqual(transport.send("qq:group:20", "timeout")["status"], "unknown")
        self.assertFalse(transport._pending)
        transport.close()
        self.assertEqual(list(transport._stderr_tail).count("send-request"), 1)

    def test_child_crash_wakes_waiting_send(self):
        transport = self.spawn()
        transport.receive(2)
        transport.receive(2)
        self.assertEqual(transport.send("qq:group:20", "crash")["status"], "unknown")
        with self.assertRaises(AdapterProcessError):
            transport.receive(1)
        transport.close()
        self.assertEqual(transport.process.returncode, 7)

    def test_startup_failure_no_orphan(self):
        with self.assertRaises(AdapterProcessError):
            self.spawn('import sys; print("missing dependency", file=sys.stderr); sys.exit(2)')

    def test_bad_protocol_fails_startup(self):
        with self.assertRaises(AdapterProcessError):
            self.spawn('import json; print(json.dumps(dict(type="ready", protocol_version=99)), flush=True)')

    def test_queue_is_bounded_without_blocking_receipts(self):
        transport = self.spawn(config=AdapterConfig(inbox_capacity=1, send_timeout=1))
        result = transport.send("qq:group:20", "reply")
        self.assertTrue(result["success"])
        self.assertEqual(transport.dropped_messages, 1)
        self.assertEqual(transport.receive(1)["conversation_id"], "qq:group:20")

    def test_parent_pipe_eof_stops_child(self):
        # 验证父进程关闭/异常退出时，stdin EOF 足以结束子进程。
        transport = self.spawn()
        transport.process.stdin.close()
        self.assertEqual(transport.process.wait(timeout=3), 0)

    def test_adapter_logs_do_not_reach_agent_handlers(self):
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        root = logging.getLogger()
        previous = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        try:
            transport = self.spawn(config=AdapterConfig(inbox_capacity=1, send_timeout=1))
            self.assertTrue(transport.send("qq:group:20", "reply")["success"])
            transport._handle(dict(type="status", status="connected"))
            transport._handle(dict(type="error", error="adapter-protocol-diagnostic"))
            logging.getLogger("agent-log-check").warning("agent-output-must-remain")
            transport.close()
            self.assertEqual(output.getvalue().strip(), "agent-output-must-remain")
            self.assertIn("send-request", transport._stderr_tail)
            self.assertEqual(transport.dropped_messages, 1)
        finally:
            root.removeHandler(handler)
            root.setLevel(previous)
            handler.close()
