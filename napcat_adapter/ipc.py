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
from .protocol import parse_conversation, send_result


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
        self._messages = deque()
        self._pending = {}
        self._ready = False
        self._reader_done = False
        self._closing = False
        self._closed = False
        self.status = "starting"
        self.dropped_messages = 0
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
            elif kind == "message" and event.get("source") == "qq":
                parse_conversation(event.get("conversation_id"))
                if not isinstance(event.get("text"), str):
                    return
                if len(self._messages) >= self.config.inbox_capacity:
                    self.dropped_messages += 1
                    if self.dropped_messages == 1 or self.dropped_messages % 100 == 0:
                        LOG.warning("NapCat 输入队列已满，累计丢弃 %d 条新消息", self.dropped_messages)
                else:
                    self._messages.append(event)
            elif kind in ("send_pending", "send_started"):
                pending = self._pending.get(event.get("request_id"))
                if pending is not None:
                    timeout = event.get("timeout", self.config.send_timeout)
                    if not isinstance(timeout, (float, int)) or not math.isfinite(timeout) or timeout <= 0:
                        timeout = self.config.send_timeout
                    pending[3]["deadline"] = (None if kind == "send_pending" else
                                              time.monotonic() + timeout)
            elif kind == "send_result":
                request_id = event.get("request_id")
                if isinstance(request_id, str):
                    pending = self._pending.get(request_id)
                    if pending is not None:
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
        """等待下一条已推送消息。超时仅用于主循环响应 Ctrl+C，不进行网络查询。"""
        deadline = time.monotonic() + timeout
        with self._condition:
            while not self._messages:
                if self._closing:
                    return None
                if self._reader_done or self.process.poll() is not None:
                    raise AdapterProcessError("NapCat 适配器已退出，请检查日志后重新启动主程序")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(min(remaining, 0.5))
            return self._messages.popleft()

    def send(self, conversation_id, text):
        command = dict(type="send_message", request_id=uuid.uuid4().hex,
                       conversation_id=conversation_id, text=text)
        try:
            parse_conversation(conversation_id)
            if not isinstance(text, str) or not text.strip():
                raise ValueError("text 必须是非空字符串")
        except ValueError as exc:
            return send_result(command, "not_sent", error=str(exc))
        ready, result = threading.Event(), {}
        request_id = command["request_id"]
        with self._condition:
            if not self.alive:
                return send_result(command, "not_sent", error="适配器未运行")
            progress = {"deadline": time.monotonic() + self.config.send_timeout}
            self._pending[request_id] = (ready, result, command, progress)
        try:
            self._write(command)
            with self._condition:
                while not ready.is_set():
                    if self._closing:
                        return send_result(command, "unknown", error="适配器正在退出，发送结果未知；不要自动重发")
                    deadline = progress["deadline"]
                    remaining = None if deadline is None else deadline - time.monotonic()
                    if remaining is not None and remaining <= 0:
                        return send_result(command, "unknown", error="等待发送回执超时；不要自动重发")
                    self._condition.wait(0.5 if remaining is None else min(remaining, 0.5))
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
