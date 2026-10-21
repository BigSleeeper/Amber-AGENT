"""正向 WebSocket 传输。只处理 QQ 消息和发送回执，不导入 Agent。"""

import asyncio
import json
import logging
import uuid
from collections import OrderedDict

from .protocol import (parse_conversation, raw_event_envelope,
                       send_result, validate_send_command)


LOG = logging.getLogger(__name__)


class NapCatAdapter:
    def __init__(self, url, token="", *, allowed_groups=None, allowed_users=None,
                 group_only=False, require_at=False, api_timeout=15.0,
                 reconnect_delay=3.0):
        if api_timeout <= 0 or reconnect_delay <= 0:
            raise ValueError("超时和重连间隔必须大于零")
        self.url, self.token = url, token
        self.options = dict(
            allowed_groups=None if allowed_groups is None else set(allowed_groups),
            allowed_users=None if allowed_users is None else set(allowed_users),
            group_only=group_only, require_at=require_at,
        )
        self.api_timeout, self.reconnect_delay = api_timeout, reconnect_delay
        self._ws = None
        self._pending = {}
        self._stream_handlers = {}
        self._single_response = set()
        self._requests = OrderedDict()
        self._inflight = set()
        self._stopped = None
        self._commands = None
        self._emit = None
        self.report_progress = False

    def accept_command(self, command):
        """在事件循环线程调用；命令与收到时的连接绑定，断线后不重放。"""
        if not isinstance(command, dict):
            self._emit({"type": "error", "error": "命令必须是 JSON 对象"})
            return
        if command.get("type") == "shutdown":
            self._stopped.set()
            return
        request_id = command.get("request_id")
        if command.get("type") not in ("send_message", "send_poke", "call_api") or not isinstance(request_id, str) or not request_id:
            self._emit({"type": "error", "error": "需要 send_message/send_poke/call_api 和非空 request_id"})
            return
        if request_id in self._requests:
            self._emit(self._requests[request_id])
            return
        if request_id in self._inflight:
            return  # 同一请求执行中，不再次发送；完成时会输出回执。
        try:
            self._commands.put_nowait((command, self._ws))
            self._inflight.add(request_id)
        except asyncio.QueueFull:
            self._emit(send_result(command, "not_sent", error="发送队列已满"))

    async def _send(self, command, connection):
        try:
            conversation, params = validate_send_command(command)
            kind, target = parse_conversation(conversation)
            if kind == "group":
                groups = self.options["allowed_groups"]
                if groups is not None and target not in groups:
                    raise ValueError("目标群不在允许列表")
            elif kind == "private":
                users = self.options["allowed_users"]
                if self.options["group_only"] or (users is not None and target not in users):
                    raise ValueError("不允许向该私聊发送消息")
        except ValueError as exc:
            return send_result(command, "not_sent", error=str(exc))
        if connection is None or connection is not self._ws:
            return send_result(command, "not_sent", error="连接不可用或已更换，请求未发送")
        echo = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self._pending[echo] = future
        if command["type"] == "call_api":
            self._stream_handlers[echo] = lambda response: self._emit(dict(
                type="api_frame", request_id=command["request_id"], response=response))
            # UploadFileStream 的每次分块请求仅返回一个 type=stream 回执。
            # 下载流则有中间帧和 type=response 终帧；两者不能仅按 stream 标记区分。
            if command.get("stream") is False or (command.get("stream") is None
                                                  and command["action"] == "upload_file_stream"):
                self._single_response.add(echo)
        try:
            async with asyncio.timeout(command.get("timeout") or self.api_timeout):
                await connection.send(json.dumps({
                    "action": (command["action"] if command["type"] == "call_api" else
                               "send_poke" if command["type"] == "send_poke" else f"send_{kind}_msg"),
                    "echo": echo,
                    "params": params,
                }, ensure_ascii=False))
                response = await future
            if response.get("status") == "ok" and response.get("retcode") == 0:
                data = response.get("data")
                result = send_result(command, "ok", message_id=data.get("message_id") if isinstance(data, dict) else None)
            elif response.get("status") == "async":
                result = send_result(command, "unknown", error="接口已受理，尚无最终执行结果")
            else:
                result = send_result(command, "failed", error=str(
                    response.get("wording") or response.get("message") or response.get("retcode")))
            result.update(raw_response=response, data=response.get("data"))
            return result
        except (TimeoutError, ConnectionError, OSError):
            return send_result(command, "unknown", error="未收到最终回执；不要自动重发")
        except Exception as exc:
            LOG.warning("发送异常：%s", type(exc).__name__)
            return send_result(command, "unknown", error="发送结果未知；不要自动重发")
        finally:
            self._pending.pop(echo, None)
            self._stream_handlers.pop(echo, None)
            self._single_response.discard(echo)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()

    async def call_api(self, action, params=None):
        """供适配器界面查询会话目录；与发送消息共用 WebSocket 回执分发。"""
        connection = self._ws
        if connection is None:
            raise ConnectionError("NapCat 尚未连接")
        echo = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self._pending[echo] = future
        try:
            async with asyncio.timeout(self.api_timeout):
                await connection.send(json.dumps(dict(action=action, params=params or {}, echo=echo)))
                response = await future
            if response.get("status") != "ok" or response.get("retcode") != 0:
                raise ValueError("NapCat 查询失败")
            return response.get("data")
        finally:
            self._pending.pop(echo, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()

    async def _sender(self):
        while True:
            command, connection = await self._commands.get()
            try:
                if self.report_progress or command["type"] == "call_api":
                    self._emit(dict(type="send_started", request_id=command["request_id"],
                                    timeout=(command.get("timeout") or self.api_timeout) + 5))
                result = await self._send(command, connection)
                request_id = command["request_id"]
                self._requests[request_id] = result
                if len(self._requests) > 4096:
                    self._requests.popitem(last=False)
                self._emit(result)
            finally:
                self._inflight.discard(command["request_id"])
                self._commands.task_done()

    async def _receive(self, ws):
        async for raw in ws:
            try:
                event = json.loads(raw)
                if not isinstance(event, dict):
                    continue
                echo = event.get("echo")
                if isinstance(echo, str) and echo in self._pending:
                    future = self._pending[echo]
                    if not future.done():
                        data = event.get("data")
                        intermediate = (echo not in self._single_response
                                        and event.get("stream") == "stream-action"
                                        and isinstance(data, dict)
                                        and data.get("type") in ("stream", "reset")
                                        and event.get("status") == "ok" and event.get("retcode") == 0)
                        if intermediate:
                            handler = self._stream_handlers.get(echo)
                            if handler is not None:
                                handler(event)
                        else:
                            future.set_result(event)
                    continue
                # 迟到/无匹配请求的 API 回执不作为上报事件交付。
                if "post_type" not in event and "status" in event and "retcode" in event:
                    continue
                self._emit(raw_event_envelope(event))
            except (ValueError, TypeError, KeyError, AttributeError):
                LOG.warning("忽略无法解析的 OneBot JSON 上报帧")

    async def _connect(self):
        from websockets.asyncio.client import connect

        headers = {"Authorization": f"Bearer {self.token}"} if self.token else None
        while not self._stopped.is_set():
            try:
                async with connect(self.url, additional_headers=headers, proxy=None,
                                   open_timeout=10, close_timeout=2, max_size=50 * 1024 * 1024) as ws:
                    self._ws = ws
                    self._emit({"type": "status", "status": "connected"})
                    await self._receive(ws)
            except Exception as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status in (401, 403):
                    LOG.warning("NapCat 鉴权失败（HTTP %s），请检查连接 Token", status)
                else:
                    LOG.warning("NapCat 连接中断或失败：%s", type(exc).__name__)
            finally:
                self._ws = None
                for future in self._pending.values():
                    if not future.done():
                        future.set_exception(ConnectionError("NapCat 连接断开"))
            self._emit({"type": "status", "status": "disconnected"})
            try:
                await asyncio.wait_for(self._stopped.wait(), self.reconnect_delay)
            except TimeoutError:
                pass

    async def run(self, emit, start_input=None):
        """emit(dict) 输出协议事件；start_input(loop, accept, stop) 可安装输入源。"""
        # 尽早报告缺少依赖，不让后台连接任务静默退出。
        from websockets.asyncio.client import connect  # noqa: F401

        self._emit = emit
        self._commands = asyncio.Queue(maxsize=100)
        self._stopped = asyncio.Event()
        if start_input:
            start_input(asyncio.get_running_loop(), self.accept_command, self._stopped.set)
        tasks = [asyncio.create_task(self._connect()), asyncio.create_task(self._sender())]
        stopper = asyncio.create_task(self._stopped.wait())
        try:
            done, _ = await asyncio.wait([*tasks, stopper], return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            self._stopped.set()
            # 主动正常关闭连接，再取消后台任务，避免向对端发送内部错误关闭码。
            if self._ws is not None:
                try:
                    await asyncio.wait_for(self._ws.close(code=1000, reason="adapter stopped"), 2)
                except (TimeoutError, OSError):
                    pass
            for task in [*tasks, stopper]:
                task.cancel()
            await asyncio.gather(*tasks, stopper, return_exceptions=True)
            self._emit({"type": "status", "status": "stopped"})
