"""界面的会话策略与人工放行队列；不依赖 Tk 或 Agent。"""

import json
import os
import uuid
from collections import OrderedDict
from pathlib import Path

from .configuration import AdapterConfig
from .protocol import parse_conversation, send_result


class Settings:
    def __init__(self, path, defaults=None):
        self.path = Path(path)
        self.connection = dict(url="ws://127.0.0.1:3001", api_timeout=15.0,
                               reconnect_delay=3.0, require_at=False)
        self.connection.update(defaults or {})
        self.sessions = {}
        self.receive_mode = self.send_mode = "manual"
        self.napcat_config_path = ""
        self.theme = "light"
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.napcat_config_path = str(data.get("napcat_config_path", ""))
            if data.get("theme") in ("light", "dark"):
                self.theme = data["theme"]
            connection = data.get("connection", {})
            self.connection.update({k: v for k, v in connection.items() if k in self.connection})
            self.validate_connection(self.connection)
            for key, value in data.get("sessions", {}).items():
                parse_conversation(key)
                self.sessions[key] = dict(name=str(value.get("name", "")),
                                          enabled=value.get("enabled") is True)
            for field in ("receive_mode", "send_mode"):
                value = data.get(field, "manual")
                if value in ("manual", "auto"):
                    setattr(self, field, value)

    @staticmethod
    def validate_connection(connection):
        AdapterConfig(url=connection["url"], api_timeout=float(connection["api_timeout"]),
                      reconnect_delay=float(connection["reconnect_delay"])).validate()
        if not isinstance(connection["require_at"], bool):
            raise ValueError("require_at 必须为布尔值")

    def save(self):
        self.validate_connection(self.connection)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(dict(connection=self.connection, sessions=self.sessions,
                                            napcat_config_path=self.napcat_config_path,
                                            theme=self.theme,
                                            receive_mode=self.receive_mode, send_mode=self.send_mode),
                                       ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)


class PanelModel:
    def __init__(self, settings, emit, submit, capacity=500):
        self.settings, self.emit, self.submit = settings, emit, submit
        self.capacity = capacity
        self.incoming, self.outgoing = OrderedDict(), OrderedDict()
        self.results = OrderedDict()
        self.dropped = 0

    def discover(self, conversation, name=""):
        parse_conversation(conversation)
        record = self.settings.sessions.setdefault(conversation, dict(name="", enabled=False))
        if name:
            record["name"] = str(name)
        return record

    def enabled(self, conversation):
        return self.settings.sessions.get(conversation, {}).get("enabled", False)

    def set_enabled(self, conversations, enabled):
        for conversation in conversations:
            self.discover(conversation)["enabled"] = bool(enabled)
            if not enabled:
                for key, row in list(self.incoming.items()):
                    if row["event"]["conversation_id"] == conversation and row["state"] == "pending":
                        del self.incoming[key]
                for row in list(self.outgoing.values()):
                    if row["event"]["conversation_id"] == conversation and row["state"] == "pending":
                        self.complete(send_result(row["event"], "not_sent", error="会话已关闭，消息未发送"))

    def _room(self, rows):
        while len(rows) >= self.capacity:
            removable = next((key for key, row in rows.items()
                              if row["state"] not in ("pending", "sending")), None)
            if removable is None:
                return False
            del rows[removable]
        return True

    def receive(self, event):
        conversation = event["conversation_id"]
        self.discover(conversation)
        if not self.enabled(conversation):
            return
        if not self._room(self.incoming):
            self.dropped += 1
            return
        key = uuid.uuid4().hex
        self.incoming[key] = dict(event=dict(event), state="pending")
        if self.settings.receive_mode == "auto":
            self.release_incoming([key])

    def release_incoming(self, keys):
        for key in keys:
            row = self.incoming.get(key)
            if row and row["state"] == "pending" and self.enabled(row["event"]["conversation_id"]):
                self.emit(row["event"])
                row["state"] = "delivered"

    def enqueue(self, command):
        if not isinstance(command, dict) or command.get("type") != "send_message":
            self.emit(dict(type="error", error="需要 send_message 命令"))
            return
        key = command.get("request_id")
        if not isinstance(key, str) or not key:
            self.emit(dict(type="error", error="缺少 request_id"))
            return
        if key in self.results:
            self.emit(self.results[key])
            return
        if key in self.outgoing:
            return
        try:
            conversation = command.get("conversation_id")
            self.discover(conversation)
            if not isinstance(command.get("text"), str) or not command["text"].strip():
                raise ValueError("消息不能为空")
            if not self._room(self.outgoing):
                raise ValueError("发送列表已满，请先处理待发送消息")
            self.outgoing[key] = dict(event=dict(command), state="pending")
            if not self.enabled(conversation):
                raise ValueError("目标会话尚未启用")
        except ValueError as exc:
            self.complete(send_result(command, "not_sent", error=str(exc)))
            return
        self.emit(dict(type="send_pending", request_id=key))
        if self.settings.send_mode == "auto":
            self.release_outgoing([key])

    def release_outgoing(self, keys):
        for key in keys:
            row = self.outgoing.get(key)
            if row and row["state"] == "pending" and self.enabled(row["event"]["conversation_id"]):
                row["state"] = "sending"
                self.submit(row["event"])

    def complete(self, result):
        key = result.get("request_id")
        row = self.outgoing.get(key)
        if row:
            row["state"] = result.get("status", "unknown")
            row["result"] = result
        self.results[key] = result
        while len(self.results) > 4096:
            self.results.popitem(last=False)
        self.emit(result)

    def delete(self, direction, keys):
        rows = self.incoming if direction == "receive" else self.outgoing
        for key in keys:
            row = rows.get(key)
            if row is None or row["state"] == "sending":
                continue
            if direction == "send" and row["state"] == "pending":
                self.complete(send_result(row["event"], "not_sent", error="用户已删除，消息未发送"))
            del rows[key]

    def set_mode(self, direction, mode):
        if mode not in ("manual", "auto"):
            raise ValueError("无效模式")
        setattr(self.settings, direction + "_mode", mode)
        if mode == "auto":
            if direction == "receive":
                self.release_incoming(list(self.incoming))
            else:
                self.release_outgoing(list(self.outgoing))

    def shutdown(self):
        for row in list(self.outgoing.values()):
            if row["state"] in ("pending", "sending"):
                pending = row["state"] == "pending"
                self.complete(send_result(row["event"], "not_sent" if pending else "unknown",
                                          error="适配器退出，未发送" if pending else "适配器退出，发送结果未知；不要自动重发"))
