"""Agent 的稳定接口：自动管理 Python 适配器子进程，通过匿名管道推送消息。"""

import atexit
import contextvars
import json
import threading

from .ipc import AdapterProcess, AdapterProcessError


_client = None
_client_lock = threading.Lock()


class ProcessClient:
    """对 Agent 保持同步函数接口，进程通信和后台读取全部在适配器内处理。"""

    def __init__(self, config=None, *, transport=None):
        self.transport = transport if transport is not None else AdapterProcess(config)
        self._current = contextvars.ContextVar("napcat_current_conversation", default=None)

    def receive_message(self, timeout=1.0):
        self._current.set(None)
        message = self.transport.receive(timeout)
        if message is None:
            return None
        self._current.set(message["conversation_id"])
        details = {key: message.get(key) for key in
                   ("conversation_id", "sender_id", "message_id", "timestamp", "metadata")}
        content = (f"[QQ 消息信息 {json.dumps(details, ensure_ascii=False)}]\n"
                   f"{message['text']}")
        return dict(message, content=content)

    def send_message(self, text):
        conversation = self._current.get()
        if conversation is None:
            result = dict(success=False, status="not_sent", error="当前没有绑定的 QQ 会话，请先接收消息")
        elif not isinstance(text, str) or not text.strip():
            result = dict(success=False, status="not_sent", error="text 必须是非空字符串")
        else:
            result = self.transport.send(conversation, text)
        return json.dumps(result, ensure_ascii=False)

    def close(self):
        self._current.set(None)
        self.transport.close()


def _get_client():
    global _client
    with _client_lock:
        if _client is None:
            _client = ProcessClient()
        return _client


def start_adapter():
    """启动并等待子进程就绪；重复调用不会重复启动。无需等待 QQ 连接成功。"""
    return _get_client()


def stop_adapter():
    """正常退出时关闭管道及子进程；重复调用安全。"""
    global _client
    with _client_lock:
        client, _client = _client, None
    if client is not None:
        client.close()


def receive_message():
    """返回一条含 content 的消息字典；阻塞等待至多 1 秒，无消息返回 None。"""
    return _get_client().receive_message()


def send_message(text):
    """返回 JSON 字符串供工具结果使用，发送目标绑定当前线程的消息。"""
    return _get_client().send_message(text)


atexit.register(stop_adapter)
