"""主进程与适配器子进程之间的匿名管道通信。无本机监听端口，无网络轮询。"""

import json
import logging
import math
import os
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from pathlib import Path

from .configuration import AdapterConfig
from .protocol import (command_preview, parse_conversation, send_result,
                       SYSTEM_CONVERSATION, validate_api_call, validate_poke_params,
                       received_item, validate_send_command, validate_send_params)


LOG = logging.getLogger(__name__)
# 此模块运行在 Agent 进程中：禁止适配器日志传给 Agent 的终端处理器，
# NullHandler 同时阻止 logging.lastResort 将警告写到 stderr。
# 子进程日志继续由界面 QueueLog 显示，stderr 仍持续读取，避免管道阻塞。
LOG.addHandler(logging.NullHandler())
LOG.propagate = False


class AdapterProcessError(RuntimeError):
    pass


class AdapterProcess:
    def __init__(self, config=None):
        self.config = (config or AdapterConfig.from_env()).validate()
        self._condition = threading.Condition()
        self._write_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._events = deque()
        self._pending = {}
        self._ready = False
        self._reader_done = False
        self._closing = False
        self._closed = False
        self.status = "starting"
        self.dropped_messages = 0
        self.dropped_events = 0
        self._stderr_tail = deque(maxlen=5)
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        self.process = subprocess.Popen(
            [sys.executable, "-u", "-m", "napcat_adapter", *self.config.arguments()],
            cwd=Path(__file__).resolve().parents[1], env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1, close_fds=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self._reader = threading.Thread(target=self._read, name="napcat-messages", daemon=True)
        self._errors = threading.Thread(target=self._read_errors, name="napcat-log", daemon=True)
        self._reader.start()
        self._errors.start()
        try:
            deadline = time.monotonic() + self.config.startup_timeout
            with self._condition:
                while not self._ready and not self._reader_done:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise AdapterProcessError("适配器启动超时，未收到就绪回执")
                    self._condition.wait(min(remaining, 0.5))
                if not self._ready:
                    raise AdapterProcessError("适配器启动失败，请检查依赖和 NapCat 日志")
        except BaseException:
            self.close()
            raise

    @property
    def alive(self):
        return not self._closing and not self._reader_done and self.process.poll() is None

    def _read_errors(self):
        try:
            for line in self.process.stderr:
                text = line.rstrip()
                token = os.getenv("NAPCAT_TOKEN", "")
                if token:
                    text = text.replace(token, "[Token 已隐藏]")
                if text:
                    self._stderr_tail.append(text)
        except (OSError, ValueError):
            pass

    def _handle(self, event):
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        with self._condition:
            if kind == "ready":
                if event.get("protocol_version") != 1:
                    raise AdapterProcessError("不支持的适配器通信协议版本")
                self._ready = True
            elif kind == "status":
                self.status = event.get("status", "unknown")
                LOG.info("NapCat 状态：%s", self.status)
            elif kind in ("event", "message", "notice") and event.get("source") == "qq":
                if kind == "event" and not isinstance(event.get("raw_event"), dict):
                    return
                parse_conversation(event.get("conversation_id"))
                if not isinstance(event.get("text"), str):
                    return
                if len(self._events) >= self.config.inbox_capacity:
                    self.dropped_messages += 1
                    self.dropped_events += 1
                    if self.dropped_messages == 1 or self.dropped_messages % 100 == 0:
                        LOG.warning("NapCat 输入队列已满，累计丢弃 %d 条新消息", self.dropped_messages)
                else:
                    self._events.append(event)
            elif kind == "api_frame":
                pending = self._pending.get(event.get("request_id"))
                if pending is not None and isinstance(event.get("response"), dict):
                    progress = pending[3]
                    if len(progress["frames"]) < 128:
                        progress["frames"].append(event["response"])
                    else:
                        progress["overflow"] = True
            elif kind in ("send_pending", "send_started"):
                pending = self._pending.get(event.get("request_id"))
                if pending is not None:
                    if (kind == "send_pending" and event.get("mode") == "manual"
                            and pending[2]["type"] == "send_message" and not pending[0].is_set()):
                        # 普通消息以人工队列受理作为成功；GUI 继续保留待办并记录真实结果。
                        result = send_result(pending[2], "ok")
                        result.update(mode="manual", queued=True, detail="已进入队列，等待人工发送")
                        pending[1].update(result)
                        pending[0].set()
                    timeout = event.get("timeout", self.config.send_timeout)
                    if not isinstance(timeout, (float, int)) or not math.isfinite(timeout) or timeout <= 0:
                        timeout = self.config.send_timeout
                    pending[3]["deadline"] = (None if kind == "send_pending" else
                                              time.monotonic() + timeout)
            elif kind == "send_result":
                request_id = event.get("request_id")
                if isinstance(request_id, str):
                    pending = self._pending.get(request_id)
                    if pending is not None and not pending[0].is_set():
                        pending[1].update(event)
                        pending[0].set()
            elif kind == "error":
                LOG.warning("NapCat 协议错误：%s", event.get("error", "unknown"))
            self._condition.notify_all()

    def _read(self):
        try:
            for line in self.process.stdout:
                try:
                    self._handle(json.loads(line))
                except (ValueError, TypeError):
                    LOG.warning("忽略无效的适配器协议事件")
        except (OSError, AdapterProcessError) as exc:
            LOG.error("适配器通信中断：%s", type(exc).__name__)
        finally:
            with self._condition:
                self._reader_done = True
                for ready, result, original, progress in self._pending.values():
                    if not ready.is_set():
                        result.update(send_result(original, "unknown", error="适配器退出，发送结果未知；不要自动重发"))
                        ready.set()
                self._condition.notify_all()

    def _write(self, command):
        with self._write_lock:
            self.process.stdin.write(json.dumps(command, ensure_ascii=False, separators=(",", ":")) + "\n")
            self.process.stdin.flush()

    def receive(self, timeout=1.0):
        """内部单条 GUI 信封读取；与批量接口共用唯一接收队列。"""
        return self._receive_queue(self._events, timeout)

    def napcat_receive(self):
        """立即取走当前全部已放行事件，按接收顺序返回列表；空队列返回 []。"""
        with self._condition:
            if not self._events and not self._closing and not self.alive:
                raise AdapterProcessError("NapCat 适配器已退出，请检查日志后重新启动主程序")
            batch = list(self._events)
            self._events.clear()
        return [received_item(event["raw_event"]) for event in batch]

    def _receive_queue(self, events, timeout):
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or timeout < 0):
            raise ValueError("接收 timeout 必须为有限的非负秒数")
        deadline = time.monotonic() + timeout
        with self._condition:
            while not events:
                if self._closing:
                    return None
                if self._reader_done or self.process.poll() is not None:
                    raise AdapterProcessError("NapCat 适配器已退出，请检查日志后重新启动主程序")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(min(remaining, 0.5))
            return events.popleft()

    def send(self, conversation_id, message, **options):
        """内部便捷接口；公共 Agent 接口须明确提供 NapCat 目标参数。"""
        try:
            kind, target = parse_conversation(conversation_id)
        except ValueError as exc:
            return send_result(dict(conversation_id=conversation_id), "not_sent", error=str(exc))
        return self.send_request(dict(options, message_type=kind, message=message,
                                      **{"group_id" if kind == "group" else "user_id": target}))

    def send_request(self, params, *, command_type="send_message"):
        command = dict(type=command_type, request_id=uuid.uuid4().hex, params=params)
        try:
            # 校验完整命令前，先从参数计算同一份路由目标。
            validator = validate_poke_params if command_type == "send_poke" else validate_send_params
            conversation, snapshot = validator(params)
            command.update(conversation_id=conversation, params=snapshot)
            validate_send_command(command)
        except ValueError as exc:
            return send_result(command, "not_sent", error=str(exc))
        command["text"] = command_preview(command, snapshot)
        return self._request(command)

    def call_api(self, action, params=None, *, timeout=None, on_frame=None, stream=None):
        params = {} if params is None else params
        command = dict(type="call_api", request_id=uuid.uuid4().hex,
                       conversation_id=SYSTEM_CONVERSATION, action=action, params=params,
                       timeout=timeout, stream=stream)
        try:
            validate_api_call(action, params, timeout)
            _, snapshot = validate_send_command(command)
        except ValueError as exc:
            return send_result(command, "not_sent", error=str(exc))
        if on_frame is not None and not callable(on_frame):
            return send_result(command, "not_sent", error="on_frame 必须是可调用对象")
        command.update(params=snapshot, text=command_preview(command, snapshot))
        return self._request(command, on_frame=on_frame)

    def _request(self, command, *, on_frame=None):
        ready, result = threading.Event(), {}
        frames, collected, frame_bytes = deque(), [], 0
        callback_error = None
        request_id = command["request_id"]
        with self._condition:
            if not self.alive:
                return send_result(command, "not_sent", error="适配器未运行")
            initial_wait = ((command.get("timeout") or self.config.api_timeout) + 5
                            if command["type"] == "call_api" else self.config.send_timeout)
            progress = {"deadline": time.monotonic() + initial_wait,
                        "frames": frames, "overflow": False}
            self._pending[request_id] = (ready, result, command, progress)
        try:
            self._write(command)
            while True:
                with self._condition:
                    batch = list(frames)
                    frames.clear()
                    finished = ready.is_set()
                    if self._closing and not finished:
                        return send_result(command, "unknown", error="适配器正在退出，发送结果未知；不要自动重发")
                    deadline = progress["deadline"]
                    remaining = None if deadline is None else deadline - time.monotonic()
                    if not finished and remaining is not None and remaining <= 0:
                        return send_result(command, "unknown", error="等待发送回执超时；不要自动重发")
                    if not batch and not finished:
                        self._condition.wait(0.5 if remaining is None else min(remaining, 0.5))
                        continue
                # 用户回调在调用线程执行，不阻塞管道读取，也不持有条件锁。
                for frame in batch:
                    if callback_error is not None:
                        continue
                    if on_frame is not None:
                        try:
                            on_frame(frame)
                        except Exception as exc:
                            callback_error = f"流处理回调失败：{type(exc).__name__}"
                    else:
                        frame_bytes += len(json.dumps(frame, ensure_ascii=False).encode("utf-8"))
                        if frame_bytes > 64 * 1024 * 1024:
                            callback_error = "流数据超过 64 MiB，请使用 on_frame 逐帧处理"
                        else:
                            collected.append(frame)
                if finished:
                    break
            if collected:
                result["frames"] = collected
            if callback_error or progress["overflow"]:
                result["stream_error"] = callback_error or "流处理速度不足，待处理帧超过 128 条"
                result["stream_complete"] = False
            elif command["type"] == "call_api":
                result["stream_complete"] = result.get("status") == "ok"
            return result
        except (OSError, ValueError):
            return send_result(command, "unknown", error="管道连接中断，发送结果未知；不要自动重发")
        finally:
            with self._condition:
                self._pending.pop(request_id, None)

    def close(self):
        with self._close_lock:
            if self._closed:
                return
            with self._condition:
                self._closing = True
                self._condition.notify_all()
            try:
                if self.process.poll() is None:
                    self._write(dict(type="shutdown"))
            except (OSError, ValueError):
                pass
            try:
                self.process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
            try:
                self.process.stdin.close()
            except (OSError, ValueError):
                # Windows 上子进程已退出时，关闭带缓冲的写管道可能再次抛错。
                pass
            self._reader.join(timeout=2)
            self._errors.join(timeout=2)
            for stream in (self.process.stdout, self.process.stderr):
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
            self._closed = True
