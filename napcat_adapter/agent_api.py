"""Agent 的稳定接口：自动管理 Python 适配器子进程，通过匿名管道推送消息。"""

import atexit
import json
import threading

from .ipc import AdapterProcess, AdapterProcessError
from .protocol import validate_api_call, validate_poke_params, validate_send_params


_client = None
_client_lock = threading.Lock()


class ProcessClient:
    """对 Agent 保持同步函数接口，进程通信和后台读取全部在适配器内处理。"""

    def __init__(self, config=None, *, transport=None):
        self.transport = transport if transport is not None else AdapterProcess(config)

    def napcat_receive(self) -> list[dict]:
        """批量轮询所有已放行事件，返回 [{type: 枚举字符串, content: 原始字典}]。"""
        return self.transport.napcat_receive()

    def send_message(self, *, message_type, message, group_id=None, user_id=None, **options):
        params = dict(options, message_type=message_type, message=message)
        if group_id is not None:
            params["group_id"] = group_id
        if user_id is not None:
            params["user_id"] = user_id
        try:
            _, params = validate_send_params(params)
        except ValueError as exc:
            result = dict(success=False, status="not_sent", error=str(exc))
        else:
            result = self.transport.send_request(params)
        return json.dumps(result, ensure_ascii=False)

    def close(self):
        self.transport.close()

    def call_api(self, action, params=None, *, timeout=None, on_frame=None, stream=None):
        result = self.transport.call_api(action, params, timeout=timeout, on_frame=on_frame, stream=stream)
        return json.dumps(result, ensure_ascii=False)

    def send_poke(self, *, user_id, group_id=None, target_id=None):
        params = _poke_params(user_id, group_id, target_id)
        try:
            _, params = validate_poke_params(params)
        except ValueError as exc:
            result = dict(success=False, status="not_sent", error=str(exc))
        else:
            result = self.transport.send_request(params, command_type="send_poke")
        return json.dumps(result, ensure_ascii=False)


def _poke_params(user_id, group_id, target_id):
    params = dict(user_id=user_id)
    if group_id is not None:
        params["group_id"] = group_id
    if target_id is not None:
        params["target_id"] = target_id
    return params


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


def napcat_receive() -> list[dict]:
    """立即批量轮询所有事件；空队列返回 []，每项仅含 type 和 content。"""
    return _get_client().napcat_receive()


def send_message(*, message_type, message, group_id=None, user_id=None, **options):
    """按 NapCat 参数显式指定目标与消息，返回 JSON 字符串供工具结果使用。"""
    params = dict(options, message_type=message_type, message=message)
    if group_id is not None:
        params["group_id"] = group_id
    if user_id is not None:
        params["user_id"] = user_id
    try:
        _, params = validate_send_params(params)
    except ValueError as exc:
        return json.dumps(dict(success=False, status="not_sent", error=str(exc)), ensure_ascii=False)
    return _get_client().send_message(**params)


def send_poke(*, user_id, group_id=None, target_id=None):
    """发送群聊/私聊戳一戳；经过会话启用和人工放行，返回 JSON 回执字符串。"""
    params = _poke_params(user_id, group_id, target_id)
    try:
        _, params = validate_poke_params(params)
    except ValueError as exc:
        return json.dumps(dict(success=False, status="not_sent", error=str(exc)), ensure_ascii=False)
    return _get_client().send_poke(**params)


def call_api(action, params=None, *, timeout=None, on_frame=None, stream=None):
    """调用当前 NapCat 任意 OneBot action，返回含完整回执的 JSON 字符串。

    timeout 为放行后网络执行的总时限（秒），不同于 params 内的原生 timeout。
    on_frame 接收每个 stream/reset 中间响应；最终响应在返回值 raw_response。
    """
    try:
        validate_api_call(action, {} if params is None else params, timeout)
        if on_frame is not None and not callable(on_frame):
            raise ValueError("on_frame 必须是可调用对象")
        if stream is not None and not isinstance(stream, bool):
            raise ValueError("stream 必须为布尔值或 None")
    except ValueError as exc:
        return json.dumps(dict(success=False, status="not_sent", error=str(exc)), ensure_ascii=False)
    return _get_client().call_api(action, params, timeout=timeout, on_frame=on_frame, stream=stream)


atexit.register(stop_adapter)
