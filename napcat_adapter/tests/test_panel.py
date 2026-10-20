import json
import asyncio
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

from napcat_adapter.panel_model import PanelModel, Settings
from napcat_adapter.tests.test_agent_api import message
from napcat_adapter.tests.test_adapter import command
from napcat_adapter.tests import test_ipc as ipc_fixtures
from napcat_adapter.ipc import AdapterProcess
from napcat_adapter.configuration import AdapterConfig

try:
    from websockets.asyncio.server import serve
except ImportError:
    serve = None


class PanelModelTests(unittest.TestCase):
    def setUp(self):
        directory = Path(__file__).resolve().parents[1] / ".artifacts"
        directory.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=directory)
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "panel.json"
        self.settings = Settings(self.path)
        self.events, self.submitted = [], []
        self.model = PanelModel(self.settings, self.events.append, self.submitted.append, capacity=2)

    def enable(self, conversation="qq:group:20"):
        self.model.set_enabled([conversation], True)

    def test_first_start_closed_and_manual(self):
        self.model.receive(message())
        self.assertFalse(self.model.incoming)
        self.assertFalse(self.events)
        self.assertFalse(self.model.enabled("qq:group:20"))
        self.assertEqual(self.settings.receive_mode, "manual")
        self.assertEqual(self.settings.send_mode, "manual")
        self.model.enqueue(command())
        self.assertEqual(self.events[-1]["status"], "not_sent")
        self.assertFalse(self.submitted)

    def test_receive_selected_only_and_delete(self):
        self.enable()
        self.model.receive(message())
        self.model.receive(message(text="第二条"))
        keys = list(self.model.incoming)
        self.assertFalse(self.events)
        self.model.release_incoming(keys[:1])
        self.model.release_incoming(keys[:1])
        self.assertEqual(len(self.events), 1)
        self.model.delete("receive", keys[1:])
        self.assertEqual(len(self.events), 1)
        self.assertEqual(len(self.model.incoming), 1)

    def test_auto_modes_drain_pending_and_gate_other_chats(self):
        self.enable()
        self.model.receive(message())
        self.model.set_mode("receive", "auto")
        self.model.receive(message(text="自动"))
        self.model.receive(message(21))
        self.assertEqual(len(self.events), 2)
        self.model.enqueue(command())
        self.assertFalse(self.submitted)
        self.model.set_mode("send", "auto")
        self.model.enqueue(command("r2"))
        self.assertEqual(len(self.submitted), 2)

    def test_send_delete_unblocks_without_network_and_deduplicates(self):
        self.enable()
        self.model.enqueue(command())
        self.assertEqual(self.events[-1]["type"], "send_pending")
        self.model.enqueue(command())
        self.assertEqual(len(self.events), 1)
        self.model.delete("send", ["r1"])
        self.assertEqual(self.events[-1]["status"], "not_sent")
        self.model.enqueue(command())
        self.assertEqual(self.events[-1]["status"], "not_sent")
        self.assertFalse(self.submitted)

    def test_disable_cancels_pending_but_not_inflight(self):
        self.enable()
        self.model.receive(message())
        self.model.enqueue(command())
        self.model.enqueue(command("r2"))
        self.model.release_outgoing(["r2"])
        self.model.set_enabled(["qq:group:20"], False)
        self.assertFalse(self.model.incoming)
        self.assertEqual(self.model.outgoing["r1"]["state"], "not_sent")
        self.assertEqual(self.model.outgoing["r2"]["state"], "sending")
        self.model.delete("send", ["r2"])
        self.assertIn("r2", self.model.outgoing)
        self.assertEqual(len(self.submitted), 1)

    def test_private_and_settings_persistence_without_secret(self):
        self.enable("qq:private:10")
        self.model.discover("qq:group:21", "测试群")
        self.settings.send_mode = "auto"
        self.settings.save()
        reloaded = Settings(self.path)
        self.assertTrue(reloaded.sessions["qq:private:10"]["enabled"])
        self.assertFalse(reloaded.sessions["qq:group:21"]["enabled"])
        self.assertEqual(reloaded.send_mode, "auto")
        self.assertNotIn("token", self.path.read_text(encoding="utf-8").lower())

    def test_capacity_retains_pending_and_shutdown_finishes_requests(self):
        self.enable()
        for _ in range(3):
            self.model.receive(message())
        self.assertEqual(self.model.dropped, 1)
        for key in ("r1", "r2", "r3"):
            self.model.enqueue(command(key))
        self.assertEqual(self.events[-1]["status"], "not_sent")
        self.assertEqual(len(self.model.outgoing), 2)
        self.model.release_outgoing(["r1"])
        self.model.shutdown()
        self.assertEqual(self.model.outgoing["r1"]["state"], "unknown")
        self.assertEqual(self.model.outgoing["r2"]["state"], "not_sent")

    def test_three_pages_queue_actions_and_clean_close(self):
        from napcat_adapter.panel import Panel
        root = tk.Tk()
        root.withdraw()
        errors = patch("napcat_adapter.panel.messagebox.showerror", side_effect=AssertionError("界面出现错误对话框"))
        errors.start()
        self.addCleanup(errors.stop)
        panel = Panel(root, self.settings, self.events.append, auto_connect=False, token="secret-test")
        try:
            self.assertEqual(set(panel.frames), {"主界面", "聊天配置", "NapCat配置"})
            for page in panel.frames:
                panel.show(page)
                root.update_idletasks()
            panel.model.set_enabled(["qq:group:20"], True)
            panel.model.receive(message(text="长消息\n完整内容"))
            panel.refresh()
            key = next(iter(panel.model.incoming))
            panel.trees["receive"].selection_set(key)
            panel.show_detail("receive")
            self.assertIn("完整内容", panel.details["receive"].get("1.0", "end"))
            panel.release("receive")
            self.assertEqual(self.events[-1]["type"], "message")
            panel.append_log("secret-test 不应显示")
            self.assertNotIn("secret-test", panel.log.get("1.0", "end"))
            self.assertTrue(panel.save_connection())
            self.assertNotIn("secret-test", self.path.read_text(encoding="utf-8"))
        finally:
            panel.close()

    def test_gui_eof_with_held_send_and_closed_parent_stdout_exits(self):
        self.settings.connection["url"] = "ws://127.0.0.1:1"
        self.enable()
        self.settings.save()
        environment = dict(os.environ, NAPCAT_PANEL_CONFIG=str(self.path), PYTHONIOENCODING="utf-8")
        child = subprocess.Popen([sys.executable, "-B", "-u", "-m", "napcat_adapter", "--gui"],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, encoding="utf-8", env=environment)
        try:
            self.assertEqual(json.loads(child.stdout.readline())["type"], "ready")
            child.stdin.write(json.dumps(command()) + "\n")
            child.stdin.flush()
            while json.loads(child.stdout.readline())["type"] != "send_pending":
                pass
            child.stdout.close()
            child.stdin.close()
            self.assertEqual(child.wait(timeout=5), 0)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
            for stream in (child.stdin, child.stdout, child.stderr):
                stream.close()

    def test_select_all_and_none_preserve_message_and_chat_policies(self):
        from napcat_adapter.panel import Panel
        root = tk.Tk()
        root.withdraw()
        panel = Panel(root, self.settings, self.events.append, auto_connect=False)
        try:
            panel.model.set_enabled(["qq:group:20", "qq:private:10"], True)
            panel.model.discover("qq:group:21", "关闭的群")
            panel.model.receive(message())
            panel.model.receive(message(text="第二条"))
            panel.model.enqueue(command())
            panel.refresh()
            self.events.clear()
            for direction in ("receive", "send"):
                tree = panel.trees[direction]
                panel.select_rows(tree, True)
                self.assertEqual(set(tree.selection()), set(tree.get_children()))
                panel.select_rows(tree, False)
                self.assertFalse(tree.selection())
            self.assertFalse(self.events)
            panel.chat_type.set("群聊")
            panel.refresh_chats()
            panel.select_rows(panel.chats, True)
            self.assertEqual(set(panel.chats.selection()), {"qq:group:20", "qq:group:21"})
            self.assertFalse(panel.model.enabled("qq:group:21"))
            panel.enable_chats(False)
            self.assertTrue(panel.model.enabled("qq:private:10"))
            self.assertFalse(panel.model.enabled("qq:group:20"))
            panel.select_rows(panel.chats, False)
            self.assertFalse(panel.chats.selection())
        finally:
            panel.close()

    def test_auto_setup_button_configures_both_sides_without_agent_changes(self):
        from napcat_adapter.panel import Panel
        from napcat_adapter.auto_config import saved_token
        path = self.path.with_name("onebot11_12345.json")
        path.write_text(json.dumps(dict(network=dict(websocketServers=[]), untouched="keep")), encoding="utf-8")
        root = tk.Tk()
        root.withdraw()
        panel = Panel(root, self.settings, self.events.append, auto_connect=False)
        try:
            with patch.object(panel.network, "start") as start, \
                    patch("napcat_adapter.panel.find_configs", return_value=[path]), \
                    patch("napcat_adapter.panel.messagebox.showerror", side_effect=AssertionError):
                panel.setup_button.invoke()
                deadline = time.monotonic() + 4
                while (panel.setup_running or str(panel.setup_button["state"]) == "disabled") and time.monotonic() < deadline:
                    root.update()
                    time.sleep(0.01)
                self.assertFalse(panel.setup_running)
                start.assert_called_once()
                url = panel.connection_vars["url"].get()
                token = panel.connection_vars["token"].get()
                self.assertEqual(saved_token(path, url), token)
                self.assertTrue(token)
                saved = Settings(self.path)
                self.assertEqual(saved.napcat_config_path, str(path.resolve()))
                self.assertEqual(saved.connection["url"], url)
                self.assertFalse(saved.sessions)
                self.assertEqual(saved.receive_mode, "manual")
                self.assertNotIn(token, self.path.read_text(encoding="utf-8"))
                self.assertIn("重启 NapCat", panel.setup_status.get())
                panel.append_log(token)
                self.assertNotIn(token, panel.log.get("1.0", "end"))
                self.assertTrue(list(path.parent.glob("onebot11_*.json.*.bak")))
        finally:
            panel.close()

    def test_minimum_window_keeps_message_and_config_controls_visible(self):
        from napcat_adapter.panel import Panel
        root = tk.Tk()
        panel = Panel(root, self.settings, self.events.append, auto_connect=False)
        try:
            root.geometry("1120x740")
            root.update()
            for theme in ("light", "dark"):
                panel.change_theme(theme)
                for page in panel.frames:
                    panel.show(page)
                    root.update()
                for direction, tree in panel.trees.items():
                    self.assertGreater(tree.winfo_height(), 70, direction)
                    self.assertGreater(tree.winfo_width(), 280, direction)
                    detail = panel.details[direction]
                    self.assertGreater(detail.winfo_height(), 60, direction)
                    self.assertLessEqual(detail.winfo_rooty() + detail.winfo_height(), root.winfo_rooty() + root.winfo_height())
                self.assertLessEqual(panel.setup_button.winfo_rootx() + panel.setup_button.winfo_width(), root.winfo_rootx() + root.winfo_width())
                self.assertLessEqual(panel.log.winfo_rooty() + panel.log.winfo_height(), root.winfo_rooty() + root.winfo_height())
        finally:
            panel.close()

    def test_theme_switch_preserves_queues_selection_logs_and_connection(self):
        from napcat_adapter.panel import Panel
        from napcat_adapter.theme import palette_for
        root = tk.Tk()
        root.withdraw()
        panel = Panel(root, self.settings, self.events.append, auto_connect=False, token="theme-secret")
        try:
            panel.model.set_enabled(["qq:group:20"], True)
            panel.model.receive(message(text="切换主题时保留的接收消息"))
            panel.model.enqueue(command())
            panel.refresh()
            receive_key = next(iter(panel.model.incoming))
            panel.trees["receive"].selection_set(receive_key)
            panel.trees["send"].selection_set("r1")
            panel.chats.selection_set("qq:group:20")
            panel.append_log("切换主题时保留的日志")
            panel.show("聊天配置")
            panel.status = "connected"
            before_log = panel.log.get("1.0", "end")
            before_events = list(self.events)
            incoming = panel.model.incoming[receive_key]
            outgoing = panel.model.outgoing["r1"]
            with patch.object(panel.network, "start") as start, patch.object(panel.network, "stop") as stop:
                for name in ("dark", "light", "dark"):
                    panel.theme_buttons[name].invoke()
                    colors = palette_for(name)
                    self.assertEqual(root.cget("bg"), colors["bg"])
                    self.assertEqual(panel.log.cget("bg"), colors["log_bg"])
                    self.assertEqual(panel.details["receive"].cget("bg"), colors["input"])
                    self.assertEqual(panel.current_page, "聊天配置")
                    self.assertEqual(panel.status, "connected")
                    self.assertEqual(panel.trees["receive"].selection(), (receive_key,))
                    self.assertEqual(panel.trees["send"].selection(), ("r1",))
                    self.assertEqual(panel.chats.selection(), ("qq:group:20",))
                    self.assertIs(panel.model.incoming[receive_key], incoming)
                    self.assertIs(panel.model.outgoing["r1"], outgoing)
                    self.assertEqual(panel.log.get("1.0", "end"), before_log)
                    self.assertEqual(self.events, before_events)
                    self.assertEqual(panel.connection_vars["token"].get(), "theme-secret")
                    self.assertEqual(Settings(self.path).theme, name)
                start.assert_not_called()
                stop.assert_not_called()
            self.assertNotIn("theme-secret", self.path.read_text(encoding="utf-8"))
        finally:
            panel.close()

    def test_saved_dark_theme_applies_on_start_and_older_settings_still_load(self):
        from napcat_adapter.panel import Panel
        from napcat_adapter.theme import palette_for
        self.settings.theme = "dark"
        self.settings.save()
        root = tk.Tk()
        root.withdraw()
        panel = Panel(root, Settings(self.path), self.events.append, auto_connect=False)
        try:
            self.assertEqual(panel.settings.theme, "dark")
            self.assertEqual(panel.sidebar.cget("bg"), palette_for("dark")["sidebar"])
            self.assertEqual(panel.style.lookup("Treeview", "background"), palette_for("dark")["card"])
            self.assertEqual(str(panel.theme_buttons["dark"].cget("style")), "SidebarSelected.TButton")
            # 下拉列表在首次显示前和切换后都使用当前主题。
            widgets = list(panel.frames["聊天配置"].winfo_children())
            while widgets:
                widget = widgets.pop()
                if widget.winfo_class() == "TCombobox":
                    popdown = root.tk.call("ttk::combobox::PopdownWindow", str(widget))
                    self.assertEqual(root.tk.call(str(popdown) + ".f.l", "cget", "-background"), palette_for("dark")["card"])
                widgets.extend(widget.winfo_children())
        finally:
            panel.close()
        data = json.loads(self.path.read_text(encoding="utf-8"))
        data.pop("theme")
        self.path.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(Settings(self.path).theme, "light")
        data["theme"] = "unknown-old-theme"
        self.path.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(Settings(self.path).theme, "light")

    def test_home_chat_selection_separates_group_private_messages_and_actions(self):
        from napcat_adapter.panel import Panel
        root = tk.Tk()
        root.withdraw()
        panel = Panel(root, self.settings, self.events.append, auto_connect=False)
        try:
            conversations = ("qq:group:20", "qq:group:21", "qq:private:20")
            panel.model.set_enabled(conversations, True)
            panel.model.discover("qq:group:22", "未开启的群")
            panel.model.discover("qq:private:23", "未开启的私聊")
            incoming = {}
            for index, conversation in enumerate(conversations):
                panel.model.receive(dict(message(text=f"会话{index}接收"), conversation_id=conversation))
                incoming[conversation] = next(reversed(panel.model.incoming))
                panel.model.enqueue(command(f"send-{index}", conversation_id=conversation, text=f"会话{index}发送"))
            panel.model.submit = self.submitted.append
            panel.refresh()
            self.assertEqual(set(panel.home_chats.get_children()), set(conversations))
            self.events.clear()
            for index, conversation in enumerate(conversations):
                panel.home_chats.selection_set(conversation)
                panel.select_home_chat()
                self.assertEqual(panel.active_conversation, conversation)
                self.assertEqual(panel.trees["receive"].get_children(), (incoming[conversation],))
                self.assertEqual(panel.trees["send"].get_children(), (f"send-{index}",))
                self.assertEqual(str(panel.metrics["receive"]["text"]), "1")
                self.assertEqual(str(panel.metrics["send"]["text"]), "1")
                panel.select_rows(panel.trees["receive"], True)
                panel.select_rows(panel.trees["send"], True)
                self.assertIn(f"会话{index}接收", panel.details["receive"].get("1.0", "end"))
                if index == 0:
                    panel.release("receive")
                    panel.release("send")
                elif index == 1:
                    panel.delete("receive")
                    panel.delete("send")
            self.assertEqual([event["conversation_id"] for event in self.events if event["type"] == "message"], [conversations[0]])
            self.assertEqual([event["conversation_id"] for event in self.submitted], [conversations[0]])
            self.assertNotIn(incoming[conversations[1]], panel.model.incoming)
            self.assertNotIn("send-1", panel.model.outgoing)
            self.assertEqual(panel.model.incoming[incoming[conversations[2]]]["state"], "pending")
            self.assertEqual(panel.model.outgoing["send-2"]["state"], "pending")
            panel.home_chats.selection_set(conversations[0])
            panel.select_home_chat()
            self.assertEqual(panel.model.incoming[incoming[conversations[0]]]["state"], "delivered")
            self.assertEqual(panel.model.outgoing["send-0"]["state"], "sending")
            self.assertEqual(panel.trees["receive"].get_children(), (incoming[conversations[0]],))
        finally:
            panel.close()

    def test_home_chat_list_updates_enabled_chats_badges_and_filters(self):
        from napcat_adapter.panel import Panel
        root = tk.Tk()
        root.withdraw()
        panel = Panel(root, self.settings, self.events.append, auto_connect=False)
        try:
            self.assertFalse(panel.home_chats.get_children())
            self.assertIsNone(panel.active_conversation)
            panel.model.set_enabled(["qq:group:20", "qq:group:21", "qq:private:10"], True)
            panel.refresh()
            self.assertEqual(panel.active_conversation, "qq:group:20")
            panel.model.receive(message(21))
            panel.model.enqueue(command("offscreen", conversation_id="qq:group:21"))
            panel.model.discover("qq:group:21", "另一个群")
            panel.refresh()
            self.assertEqual(panel.active_conversation, "qq:group:20")
            self.assertFalse(panel.trees["receive"].get_children())
            self.assertFalse(panel.trees["send"].get_children())
            self.assertIn("另一个群", panel.home_chats.item("qq:group:21", "text"))
            self.assertEqual(panel.home_chats.item("qq:group:21", "values"), ("1 / 1",))
            panel.home_chat_type.set("私聊")
            panel.refresh()
            self.assertEqual(panel.home_chats.get_children(), ("qq:private:10",))
            self.assertEqual(panel.active_conversation, "qq:private:10")
            panel.home_chat_type.set("群聊")
            panel.refresh()
            self.assertEqual(set(panel.home_chats.get_children()), {"qq:group:20", "qq:group:21"})
            panel.chats.selection_set("qq:group:20")
            panel.enable_chats(False)
            self.assertEqual(panel.active_conversation, "qq:group:21")
            self.assertFalse(panel.home_chats.exists("qq:group:20"))
            panel.chats.selection_set("qq:group:21")
            panel.enable_chats(False)
            self.assertIsNone(panel.active_conversation)
            self.assertFalse(panel.home_chats.get_children())
            self.assertFalse(panel.trees["receive"].get_children())
            self.assertFalse(panel.trees["send"].get_children())
            self.assertNotIn("另一个群", panel.details["receive"].get("1.0", "end"))
            panel.home_chat_type.set("全部")
            panel.refresh()
            self.assertEqual(panel.home_chats.get_children(), ("qq:private:10",))
        finally:
            panel.close()

    def test_auto_mode_processes_other_chats_while_current_chat_is_selected(self):
        from napcat_adapter.panel import Panel
        root = tk.Tk()
        root.withdraw()
        panel = Panel(root, self.settings, self.events.append, auto_connect=False)
        try:
            panel.model.set_enabled(["qq:group:20", "qq:group:21"], True)
            panel.model.submit = self.submitted.append
            panel.refresh()
            panel.modes["receive"].set("auto")
            panel.change_mode("receive")
            panel.modes["send"].set("auto")
            panel.change_mode("send")
            panel.model.receive(message(21))
            panel.model.enqueue(command("other", conversation_id="qq:group:21"))
            panel.refresh()
            self.assertEqual(panel.active_conversation, "qq:group:20")
            self.assertEqual(self.events[0]["conversation_id"], "qq:group:21")
            self.assertEqual(self.submitted[0]["conversation_id"], "qq:group:21")
            self.assertFalse(panel.trees["receive"].get_children())
            panel.home_chats.selection_set("qq:group:21")
            panel.select_home_chat()
            self.assertEqual(len(panel.trees["receive"].get_children()), 1)
            self.assertEqual(panel.trees["send"].get_children(), ("other",))
        finally:
            panel.close()
class ManualWaitTests(unittest.TestCase):
    spawn = ipc_fixtures.IpcTests.spawn
    def test_manual_hold_longer_than_send_timeout(self):
        source = '''
import json, sys, time
def emit(**event): print(json.dumps(event), flush=True)
emit(type="ready", protocol_version=1)
for line in sys.stdin:
    command = json.loads(line)
    if command["type"] == "shutdown": break
    key = command["request_id"]
    emit(type="send_pending", request_id=key)
    time.sleep(0.3)
    emit(type="send_started", request_id=key)
    emit(type="send_result", request_id=key, status="ok", success=True)
'''
        transport = self.spawn(source)
        start = time.monotonic()
        self.assertTrue(transport.send("qq:group:20", "等待人工放行")["success"])
        self.assertGreater(time.monotonic() - start, 0.25)

    def test_manual_delete_returns_not_sent(self):
        source = '''
import json, sys
print(json.dumps(dict(type="ready", protocol_version=1)), flush=True)
for line in sys.stdin:
    c = json.loads(line)
    if c["type"] == "shutdown": break
    print(json.dumps(dict(type="send_pending", request_id=c["request_id"])), flush=True)
    print(json.dumps(dict(type="send_result", request_id=c["request_id"], status="not_sent", success=False)), flush=True)
'''
        transport = self.spawn(source)
        self.assertEqual(transport.send("qq:group:20", "删除")["status"], "not_sent")


@unittest.skipIf(serve is None, "需要安装 websockets")
class PanelProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_disconnect_completes_released_send_without_ack(self):
        from napcat_adapter.panel import Network
        received = asyncio.Event()
        async def peer(ws):
            await ws.recv()
            received.set()
            await ws.wait_closed()
        events = queue.SimpleQueue()
        network = Network(events)
        async with serve(peer, "127.0.0.1", 0) as service:
            url = f"ws://127.0.0.1:{service.sockets[0].getsockname()[1]}"
            network.start(dict(url=url, api_timeout=30, reconnect_delay=1, require_at=False), "")
            try:
                first = await asyncio.to_thread(events.get, True, 3)
                self.assertEqual(first["status"], "connected")
                network.submit(command())
                await asyncio.wait_for(received.wait(), 3)
                network.stop()
                for _ in range(8):
                    result = await asyncio.to_thread(events.get, True, 4)
                    if result["type"] == "send_result":
                        self.assertEqual(result["status"], "unknown")
                        break
                else:
                    self.fail("断开连接未结束发送等待")
            finally:
                network.stop()
                await asyncio.to_thread(network.thread.join, 4)
            self.assertFalse(network.thread.is_alive())
            self.assertFalse(network.outstanding)

    async def test_gui_child_catalog_pipe_receive_send_and_exit(self):
        from napcat_adapter.tests.test_adapter import event
        directory = Path(__file__).resolve().parents[1] / ".artifacts"
        directory.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=directory) as temporary:
            path = Path(temporary) / "panel.json"
            actions = []
            async def peer(ws):
                await ws.send(json.dumps(event()))
                async for raw in ws:
                    request = json.loads(raw)
                    action = request["action"]
                    actions.append(action)
                    data = {"get_group_list": [dict(group_id=20, group_name="测试群")],
                            "get_friend_list": [dict(user_id=10, nickname="测试好友")],
                            "send_group_msg": dict(message_id=1234)}[action]
                    await ws.send(json.dumps(dict(echo=request["echo"], status="ok", retcode=0, data=data)))
            async with serve(peer, "127.0.0.1", 0) as service:
                url = f"ws://127.0.0.1:{service.sockets[0].getsockname()[1]}"
                settings = Settings(path, dict(url=url))
                settings.sessions["qq:group:20"] = dict(enabled=True, name="")
                settings.receive_mode = settings.send_mode = "auto"
                settings.save()
                with patch.dict(os.environ, NAPCAT_PANEL_CONFIG=str(path)):
                    transport = await asyncio.to_thread(AdapterProcess, AdapterConfig(url=url, gui=True))
                try:
                    received = await asyncio.to_thread(transport.receive, 5)
                    self.assertEqual(received["text"], "你好")
                    result = await asyncio.to_thread(transport.send, received["conversation_id"], "GUI管道回复")
                    self.assertEqual(result["message_id"], "1234")
                finally:
                    await asyncio.to_thread(transport.close)
                self.assertEqual(transport.process.returncode, 0)
                self.assertEqual(actions.count("send_group_msg"), 1)
                self.assertIn("get_group_list", actions)
                self.assertIn("get_friend_list", actions)
                saved = Settings(path)
                self.assertFalse(saved.sessions["qq:private:10"]["enabled"])
                self.assertEqual(saved.sessions["qq:group:20"]["name"], "测试群")


if __name__ == "__main__":
    unittest.main()
